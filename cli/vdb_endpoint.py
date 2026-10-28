"""chroma server 端点管理：worker 独占数据，其余进程走 HttpClient.

背景（2026-10-28 事故）：chroma embedded 是单进程嵌入式库，不支持跨进程共享。
后台 worker 写入并翻转 supersede 状态后，长驻交互进程的内存索引不更新，
向量召回池直接为空（流水实测 pool_size=30, returned=0，且不自愈）。

收敛后的架构：数据唯一所有者是 worker 进程内的 chroma server 线程；
其余进程（CLI / 交互 UI / MCP stdio）由 SDK 按 MEMORY_VECTOR_HOST /
MEMORY_VECTOR_PORT 环境变量走 HttpClient——SDK 的 vector_store_chroma
原生支持该路径（host 有值即 HttpClient），本模块不实现任何连接与入库逻辑。

职责边界：
  worker 侧：把 chroma server 线程跑起来 + 端口文件写入/清理
  客户端侧：读端口文件 → 心跳校验 → 设环境变量；
            端点不可用 → 拉起 worker → 轮询就绪（复用现有防重闸门）
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import netprobe
import store

PORT_FILE_NAME = "server.port"
_HEARTBEAT_PATH = "/api/v2/heartbeat"


# ---------------------------------------------------------------- 端口文件

def port_file() -> Path:
    """server.port 的位置：与队列库同级（MEMORY_DATA_DIR）。"""
    return store.data_dir() / PORT_FILE_NAME


def write_port_file(port: int) -> None:
    data = {"port": int(port), "pid": os.getpid(), "started_at": time.time()}
    pf = port_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data), encoding="utf-8")


def read_port() -> int | None:
    """读端口文件。内容缺失/损坏/字段非法一律返回 None（容错优先）。"""
    try:
        data = json.loads(port_file().read_text(encoding="utf-8"))
        port = int(data["port"])
        if 0 < port < 65536:
            return port
    except Exception:  # noqa: BLE001  任何读不出有效端口的情况都视为无端点
        return None
    return None


def remove_port_file() -> None:
    try:
        port_file().unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 心跳

def heartbeat_ok(port: int, timeout: float = 1.0) -> bool:
    """chroma /api/v2/heartbeat 探活。走 netprobe（出站唯一出口）且
    direct=True——本机回环绝不能被系统/环境代理劫持；也不用 chromadb：
    客户端在决定要不要拉起 worker 之前不该先付 SDK 导入的成本。"""
    res = netprobe.get_json(
        f"http://127.0.0.1:{port}{_HEARTBEAT_PATH}", timeout=timeout, direct=True
    )
    return isinstance(res, netprobe.Sent) and res.ok


# ---------------------------------------------------------------- worker 侧

def sdk_persist_dir() -> Path:
    """与 hy_memory.config 的向量库目录解析保持一字不差：
    MEMORY_DATA_DIR/data/vector_db（未设则 ~/.hy_memory/data/vector_db）。
    两边必须指向同一目录，否则 server 与 SDK 各自建库。"""
    root = os.getenv("MEMORY_DATA_DIR") or str(Path.home() / ".hy_memory")
    return Path(root) / "data" / "vector_db"


class _ServerHandle:
    """uvicorn.Server + 线程句柄。stop 用线程 join——uvicorn 的 exited 是
    asyncio 事件，同步代码 .wait() 不可靠。"""

    def __init__(self, srv: Any, thread: threading.Thread):
        self._srv = srv
        self._thread = thread

    def stop(self, join_s: float = 5.0) -> None:
        try:
            self._srv.should_exit = True
            self._thread.join(join_s)
        except Exception:  # noqa: BLE001  daemon 线程随进程退出兜底
            pass


def start_server_thread(persist_dir: Path, host: str = "127.0.0.1") -> tuple[int, Any]:
    """在当前进程内起 chroma server 线程，返回 (port, uvicorn.Server)。

    依赖 fastapi 与 opentelemetry-instrumentation-fastapi（chromadb 1.5.9
    未把前者列为运行时依赖，requirements.txt 已补）。uvicorn 线程内不装
    信号处理器，随宿主进程退出回收。
    """
    s = socket.socket()
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()

    from chromadb.config import Settings
    from chromadb.server.fastapi import FastAPI as ChromaFastAPI

    # 注意一：is_persistent 必须显式 True——只传 persist_directory 时 server
    #   跑成内存库（实测：sqlite 有 62 条而 server 端 collections=[]，检索
    #   静默返回空）。注意二：chroma_api_impl 不要设置——它选的是 System 内
    #   的 API 实现，传 server 类会导致 System 递归构造自身（TypeError 套娃）。
    settings = Settings(
        is_persistent=True,
        persist_directory=str(persist_dir),
        chroma_server_host=host,
        chroma_server_http_port=port,
        anonymized_telemetry=False,
    )
    app = ChromaFastAPI(settings).app()

    import uvicorn
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True, name="chroma-server")
    t.start()
    deadline = time.time() + 15
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    if not srv.started:
        raise RuntimeError("chroma server 线程 15s 未就绪")
    return port, _ServerHandle(srv, t)


def start_for_worker() -> tuple[int, Any] | None:
    """worker 入口：起 server 线程并登记端口文件。失败返回 None（调用方
    回落嵌入式，行为与 0.9.8 一致），不阻断消费主流程。"""
    try:
        port, srv = start_server_thread(sdk_persist_dir())
    except Exception as e:  # noqa: BLE001
        print(f"hy-mem: chroma server 启动失败，本 worker 回落嵌入式：{type(e).__name__}: {e}",
              file=sys.stderr)
        return None
    write_port_file(port)
    return port, srv


def stop_server_thread(handle: Any, port: int, join_s: float = 5.0) -> None:
    remove_port_file()
    handle.stop(join_s)


# ---------------------------------------------------------------- 客户端侧

def _spawn_worker() -> bool:
    """懒导入防循环依赖：hy_mem_cli 顶层 import 本模块。

    端点场景的 worker 是「server 宿主」：检索不入队，30s 早退会让每次
    检索都重新冷启（~7s 惩罚）。因此禁用早退 + 拉长空闲超时到 5min：
    首次 ~10s，之后 5 分钟内的连续检索都是 ~3s 的热路径。
    """
    from hy_mem_cli import _spawn_detached_worker
    return _spawn_detached_worker(idle_exit=300.0, extra_args=["--no-fast-exit"])


def ensure_endpoint(spawn: Callable[[], bool] | None = None,
                    wait_s: float = 15.0) -> int | None:
    """make_client 前调用：保证本进程的 SDK 走 HttpClient。

    流程：外部已配置 MEMORY_VECTOR_HOST → 尊重不动；
    端口文件有效且心跳可达 → 注入 env 返回端口；
    文件缺失或心跳失败 → 清掉过期文件 → 拉起 worker（复用 cap=1 防重闸门）
    → 轮询端口文件 + 心跳 → 注入 env；
    全程失败 → 返回 None（调用方回落嵌入式，stderr 留痕）。

    worker 进程自身在启动 server 后已设 env，走到这里直接返回，不会自拉自。
    """
    if os.getenv("MEMORY_VECTOR_HOST"):
        return None  # 外部显式配置（远程 chroma / qdrant），不覆盖

    def _adopt(port: int) -> int:
        os.environ["MEMORY_VECTOR_HOST"] = "127.0.0.1"
        os.environ["MEMORY_VECTOR_PORT"] = str(port)
        # chroma 的 HttpClient 底层是 httpx，会吃 HTTP(S)_PROXY 环境变量与
        # Windows 系统代理——本机回环一旦被代理接管就是 502（实测）。注入
        # NO_PROXY 保证回环直连；已有的 NO_PROXY 条目保留。
        parts = [p.strip() for p in (os.environ.get("NO_PROXY")
                                     or os.environ.get("no_proxy") or "").split(",")
                 if p.strip()]
        missing = ({"127.0.0.1", "localhost"} - set(parts))
        if missing:
            merged = ",".join(parts + sorted(missing))
            os.environ["NO_PROXY"] = merged
            os.environ["no_proxy"] = merged
        return port

    port = read_port()
    if port and heartbeat_ok(port):
        return _adopt(port)
    if port:
        remove_port_file()  # 过期残留，清掉再等新 worker 登记

    do_spawn = spawn or _spawn_worker
    try:
        do_spawn()
    except Exception:  # noqa: BLE001  拉起失败不致命，走轮询兜底（可能已有 worker 在起）
        pass

    deadline = time.time() + wait_s
    while time.time() < deadline:
        port = read_port()
        if port and heartbeat_ok(port):
            return _adopt(port)
        time.sleep(0.15)
    print("hy-mem: 未能获得 chroma server 端点，回落嵌入式向量库"
          "（多进程并发下可能出现读到旧数据/空结果，建议检查 worker）",
          file=sys.stderr)
    return None
