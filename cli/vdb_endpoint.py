"""chroma server 端点管理：单进程独占数据，其余进程走 HttpClient.

背景（2026-10-28 事故）：chroma embedded 是单进程嵌入式库，不支持跨进程共享。
后台 worker 写入并翻转 supersede 状态后，长驻交互进程的内存索引不更新，
向量召回池直接为空（流水实测 pool_size=30, returned=0，且不自愈）。

架构（0.9.10 起，两角色分离）：

  hy-mem serve    数据所有者。长驻、单例、独占向量库目录，端口文件里登记
                  owner（pid）。空闲超时自行退出，退出时只删自己的登记。
  hy-mem worker   队列消费者。天然短命，**永不起 server**，自己也是 HttpClient
                  客户端；需要端点时由 ensure_endpoint 去拉起 serve。

这样拆的理由（0.9.9 的教训）：把一个「必须长驻单例」的角色和一个「跑完就退」
的角色塞进同一个进程，消费者退出就会顺手把数据所有者带走，造成端点悬空、
端口文件被误删、以及随后的静默降级。

客户端侧契约：
  1. 外部显式配置的 MEMORY_VECTOR_HOST（无本模块来源标记）一律不碰；
  2. 自己注入的端点（带 MEMORY_VECTOR_HOST_SRC=hymem）**每次复验心跳**，
     失效就清掉重取——0.9.9 把它当外部配置，进程内一旦失效就永久坏掉；
  3. 端口文件的主人进程还活着却连不上 → 抛 EndpointUnavailable（明确失败）。
     静默回落嵌入式会读到另一个进程写过的旧索引，比报错危险得多。
     没有主人（谁都没起过 server）时才允许回落，并打醒目的 stderr 提示。
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
OWNER_LABEL = "serve"          # 端口文件里的角色名，便于人工排查
SRC_ENV = "MEMORY_VECTOR_HOST_SRC"
SRC_VALUE = "hymem"            # 只有带这个标记的 HOST 才是「自己注入的」
_HEARTBEAT_PATH = "/api/v2/heartbeat"

# 最近一次 HTTP 请求时间（serve 的空闲退出依据）。ASGI 包装层写入。
_last_activity = time.time()
_activity_lock = threading.Lock()


class EndpointUnavailable(RuntimeError):
    """端点本该可用却拿不到（数据所有者活着但连不上）。

    不静默降级成嵌入式：那会读到别人写过的旧索引，让调用方在「看似成功」
    的结果里拿到过期记忆。
    """


# ---------------------------------------------------------------- 端口文件

def port_file() -> Path:
    """server.port 的位置：与队列库同级（MEMORY_DATA_DIR）。"""
    return store.data_dir() / PORT_FILE_NAME


def pid_alive(pid: int | None) -> bool:
    """进程是否还活着。

    实现在 store 里（闸门也要用），这里转发一下，避免「端点 → store」与
    「闸门 → 端点」两套判定漂移：只要两处对「死」的判断不一致，就会出现
    「端口文件被当成活的」或者「活着的登记被清掉」。
    """
    return store.pid_alive(pid)


def _proc_start_time(pid: int) -> float | None:
    """进程创建时间（unix 秒）。拿不到返回 None —— 判定宁可放过，不可误杀。"""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                ctime, etime, ktime, utime = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(
                    handle,
                    ctypes.byref(ctime),
                    ctypes.byref(etime),
                    ctypes.byref(ktime),
                    ctypes.byref(utime),
                ):
                    return None
                ticks = (ctime.dwHighDateTime << 32) | ctime.dwLowDateTime
                return ticks / 10_000_000 - 11644473600  # FILETIME → unix 纪元
            finally:
                k32.CloseHandle(handle)
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
        # 第 22 个字段是 starttime（开机后的时钟滴答数）；comm 里可能有空格，从右往左数
        fields = stat[stat.rfind(")") + 2:].split()
        ticks = float(fields[19])
        btime = 0.0
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = float(line.split()[1])
                break
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:  # noqa: BLE001  平台差异太大，拿不到就当不知道
        return None


def pid_reused(info: dict) -> bool:
    """端口文件里的 pid 是不是被系统分配给了别的进程。

    为什么必须判：Windows 的 pid 回收很快，一个被强杀的 serve 留下的登记
    （pid 209972）几分钟后可能指向随便一个进程 —— node、explorer…… 只看
    `pid_alive` 就会认定「主人还活着」，接着心跳当然连不上，于是**整轮命令
    全部抛 EndpointUnavailable**：这正是 2026-10-28 那次「一个会话全废」的
    另一种入口（实测撞到过：登记说 serve，实际是个 node 进程）。

    判据用进程创建时间：serve 登记 started_at 时自己早就起来了，所以
    `创建时间 <= started_at`；反过来就说明这个 pid 已经换了人间。
    """
    started = float(info.get("started_at") or 0)
    if started <= 0:
        return False  # 老格式登记（没有 started_at），没有证据就不动
    created = _proc_start_time(int(info.get("pid") or 0))
    if created is None:
        return False  # 拿不到创建时间（权限/非 /proc 系统）：保持原有语义
    return created > started + 3.0


def write_port_file(port: int, owner: str = OWNER_LABEL, pid: int | None = None) -> None:
    data = {
        "port": int(port),
        "pid": int(pid if pid is not None else os.getpid()),
        "owner": owner,
        "started_at": time.time(),
    }
    pf = port_file()
    pf.parent.mkdir(parents=True, exist_ok=True)
    tmp = pf.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(pf)  # 原子替换：读方不会看到半截 JSON


def read_port_info() -> dict | None:
    """读端口文件完整信息。内容缺失/损坏/字段非法一律 None（容错优先）。"""
    try:
        data = json.loads(port_file().read_text(encoding="utf-8"))
        port = int(data["port"])
        if not (0 < port < 65536):
            return None
        pid = int(data.get("pid") or 0)
        return {
            "port": port,
            "pid": pid,
            "owner": str(data.get("owner") or ""),
            "started_at": float(data.get("started_at") or 0),
        }
    except Exception:  # noqa: BLE001  任何读不出有效端口的情况都视为无端点
        return None


def read_port() -> int | None:
    info = read_port_info()
    return info["port"] if info else None


def remove_port_file(expect_pid: int | None = None) -> bool:
    """删端口文件。给 expect_pid 时只删「主人是自己」的那份。

    0.9.9 的 F2：worker 退出无条件删文件，把还活着的宿主的登记删掉了，
    下一个客户端就只能静默降级。归属校验是这一层的硬要求。
    """
    try:
        if expect_pid is not None:
            info = read_port_info()
            if info is None:
                return False
            if info["pid"] != int(expect_pid):
                return False
        port_file().unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 心跳

def heartbeat_ok(port: int, timeout: float = 1.0) -> bool:
    """chroma /api/v2/heartbeat 探活。走 netprobe（出站唯一出口）且
    direct=True——本机回环绝不能被系统/环境代理劫持；也不用 chromadb：
    客户端在决定要不要拉起 serve 之前不该先付 SDK 导入的成本。"""
    res = netprobe.get_json(
        f"http://127.0.0.1:{port}{_HEARTBEAT_PATH}", timeout=timeout, direct=True
    )
    return isinstance(res, netprobe.Sent) and res.ok


# ---------------------------------------------------------------- serve 侧

def sdk_persist_dir() -> Path:
    """与 hy_memory.config 的向量库目录解析保持一字不差：
    MEMORY_DATA_DIR/data/vector_db（未设则 ~/.hy_memory/data/vector_db）。
    两边必须指向同一目录，否则 server 与 SDK 各自建库。"""
    root = os.getenv("MEMORY_DATA_DIR") or str(Path.home() / ".hy_memory")
    return Path(root) / "data" / "vector_db"


def last_activity() -> float:
    with _activity_lock:
        return _last_activity


def touch_activity() -> None:
    global _last_activity
    with _activity_lock:
        _last_activity = time.time()


class _ServerHandle:
    """uvicorn.Server + 线程句柄。stop 用线程 join——uvicorn 的 exited 是
    asyncio 事件，同步代码 .wait() 不可靠。"""

    def __init__(self, srv: Any, thread: threading.Thread, port: int):
        self._srv = srv
        self._thread = thread
        self.port = port

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not getattr(self._srv, "should_exit", False)

    def stop(self, join_s: float = 5.0) -> None:
        try:
            self._srv.should_exit = True
            self._thread.join(join_s)
        except Exception:  # noqa: BLE001  daemon 线程随进程退出兜底
            pass


def _patch_api_embeddings(api: Any) -> None:
    """chroma 1.5.9 server 的 tolist 兼容垫片（实例级，幂等）。

    故障链（2026-10-28 实测）：跨进程数据在向量段里走 brute force/queue
    路径，GetResult.embeddings 是 list；FastAPI 响应层硬编码
    `cast(Embedding, embedding).tolist()` 直接 AttributeError——HTTP 的
    get/query 一带 embeddings 就 500，SDK 的 get_by_ids / get_embeddings
    全部静默返回空，检索管线的链式去重失效，superseded 条目漏进最终结果。

    修法：在 API 实例出口（_get/_query）把 embeddings 归一化为 numpy，
    三处响应转换全部恢复。实例级包装不依赖 chroma_api_impl 解析出的具体
    类（1.5.9 默认是 RustBindingsAPI，不是 SegmentAPI）。垫片绝不放大故障。
    """
    if getattr(api, "_hymem_numpy_patched", False):
        return

    import numpy as np

    def _norm(embs: Any) -> Any:
        if embs is None:
            return None
        try:
            return [
                e if hasattr(e, "tolist") else np.asarray(e, dtype=np.float32)
                for e in embs
            ]
        except Exception:  # noqa: BLE001
            return embs

    orig_get = api._get
    orig_query = api._query

    def _get(*a: Any, **kw: Any) -> Any:
        res = orig_get(*a, **kw)
        if isinstance(res, dict) and res.get("embeddings") is not None:
            res["embeddings"] = _norm(res["embeddings"])
        return res

    def _query(*a: Any, **kw: Any) -> Any:
        res = orig_query(*a, **kw)
        if isinstance(res, dict) and res.get("embeddings") is not None:
            res["embeddings"] = [
                _norm(per_query) if per_query is not None else None
                for per_query in res["embeddings"]
            ]
        return res

    api._get = _get  # type: ignore[method-assign]
    api._query = _query  # type: ignore[method-assign]
    api._hymem_numpy_patched = True  # type: ignore[attr-defined]


def _instrument(app: Any) -> Any:
    """ASGI 包装：记录最后一次请求时间，供 serve 的空闲退出判断。

    不引 starlette 中间件——uvicorn 只要求 app 是可调用的 (scope, receive, send)。
    lifespan scope 也原样透传，FastAPI 的启动钩子照常执行。
    """

    async def _wrapped(scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            touch_activity()
        await app(scope, receive, send)

    return _wrapped


def start_server_thread(persist_dir: Path, host: str = "127.0.0.1") -> tuple[int, _ServerHandle]:
    """在当前进程内起 chroma server 线程，返回 (port, handle)。

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
    server = ChromaFastAPI(settings)
    _patch_api_embeddings(server._api)
    app = _instrument(server.app())

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
    touch_activity()
    return port, _ServerHandle(srv, t, port)


def stop_server_thread(handle: Any, port: int, join_s: float = 5.0) -> None:
    """停 server 并删登记——只删自己的（owner 校验）。"""
    remove_port_file(expect_pid=os.getpid())
    handle.stop(join_s)


# ---------------------------------------------------------------- 客户端侧

def _inject_env(port: int) -> int:
    """注入端点 env 并打来源标记（复验依据，见模块 docstring 第 2 条）。"""
    os.environ["MEMORY_VECTOR_HOST"] = "127.0.0.1"
    os.environ["MEMORY_VECTOR_PORT"] = str(port)
    os.environ[SRC_ENV] = SRC_VALUE
    # chroma 的 HttpClient 底层是 httpx，会吃 HTTP(S)_PROXY 环境变量与
    # Windows 系统代理——本机回环一旦被代理接管就是 502（实测）。注入
    # NO_PROXY 保证回环直连；已有的 NO_PROXY 条目保留。
    parts = [p.strip() for p in (os.environ.get("NO_PROXY")
                                 or os.environ.get("no_proxy") or "").split(",")
             if p.strip()]
    missing = {"127.0.0.1", "localhost"} - set(parts)
    if missing:
        merged = ",".join(parts + sorted(missing))
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged
    return port


def clear_injected_env() -> None:
    """只清自己注入的那份 env；外部配置（无标记）不动。"""
    if os.getenv(SRC_ENV) != SRC_VALUE:
        return
    for k in ("MEMORY_VECTOR_HOST", "MEMORY_VECTOR_PORT", SRC_ENV):
        os.environ.pop(k, None)


def _spawn_serve() -> bool:
    """拉起 detached serve（数据所有者）。懒导入防循环依赖。"""
    from hy_mem_cli import _spawn_detached_serve
    return _spawn_detached_serve()


def _wait_ready(wait_s: float, owner_pid: int | None) -> int | None:
    """轮询端口文件 + 心跳，直到就绪或超时。

    owner_pid 给定时只认「别人登记的」（自己刚拉起的 serve 是别的进程，
    这里天然满足）；主人变了就提前结束等待——说明我们的 serve 抢输了。
    """
    deadline = time.time() + max(0.2, wait_s)
    while time.time() < deadline:
        info = read_port_info()
        if info and heartbeat_ok(info["port"]):
            return info["port"]
        if owner_pid is not None and info and info["pid"] != owner_pid:
            return None  # 归宿已易主，交给上层重新判断
        time.sleep(0.15)
    info = read_port_info()
    if info and heartbeat_ok(info["port"]):
        return info["port"]
    return None


def ensure_endpoint(spawn: Callable[[], bool] | None = None,
                    wait_s: float = 20.0) -> int | None:
    """make_client 前调用：保证本进程的 SDK 走 HttpClient。

    分支顺序（每一条都对应一个 2026-10-28 实测缺陷）：
      1. HOST 有值且无本模块标记 → 外部配置（远程 chroma/qdrant），不动；
      2. HOST 有值且带标记 → 自己注入的，先复验心跳；死了就清掉重取（F4）；
      3. 端口文件的主人活着且心跳可达 → 采纳；
      4. 主人活着但连不上 → 等一轮，仍不行就抛 EndpointUnavailable（F3）；
      5. 主人已死 → 清掉残留登记，拉起 serve 再等（F2/F5/F10）；
      6. 拉起失败/超时且从无主人 → 回落嵌入式，stderr 明确告警。

    返回端口表示已注入 env；返回 None 表示走嵌入式。
    """
    host = (os.getenv("MEMORY_VECTOR_HOST") or "").strip()
    if host and os.getenv(SRC_ENV) != SRC_VALUE:
        return None  # 外部显式配置，尊重不动
    if host:
        port = int(os.getenv("MEMORY_VECTOR_PORT") or 0)
        if port and heartbeat_ok(port):
            return port  # 自己注入的还活着，直接复用（热路径零额外开销）
        clear_injected_env()

    info = read_port_info()
    owner_alive = bool(info and pid_alive(info["pid"]))
    if owner_alive and info and pid_reused(info):
        # pid 被系统复用了：那不是我们的 serve，登记等同于残留
        owner_alive = False
    if info and not owner_alive:
        # 主人已经没了：残留登记会误导后续所有客户端，清掉再拉
        remove_port_file(expect_pid=info["pid"])
        info, owner_alive = None, False

    if owner_alive and info:
        port = _wait_ready(wait_s, info["pid"])
        if port:
            return _inject_env(port)
        raise EndpointUnavailable(
            f"向量库服务（pid {info['pid']}, :{info['port']}）进程还在但连不上心跳。"
            "为避免读到旧索引，本次不回落嵌入式。"
        )

    do_spawn = spawn or _spawn_serve
    try:
        do_spawn()
    except Exception:  # noqa: BLE001  拉起失败不致命，走轮询兜底（可能已有 serve 在起）
        pass

    port = _wait_ready(wait_s, None)
    if port:
        return _inject_env(port)

    # 期间可能有人抢先在起主人：再确认一次，避免把「正在启动」误判为「没人」
    info2 = read_port_info()
    if info2 and pid_alive(info2["pid"]) and not pid_reused(info2):
        raise EndpointUnavailable(
            f"向量库服务（pid {info2['pid']}, :{info2['port']}）已登记但未在 "
            f"{wait_s:.0f}s 内就绪。可跑 `hy-mem serve` 手动启动看报错。"
        )
    print(
        "hy-mem: 未能启动向量库服务（serve），本次回落嵌入式向量库。"
        "单进程使用没问题；多进程/多窗口并发时会读到旧数据，"
        "建议跑 `hy-mem serve` 看具体报错。",
        file=sys.stderr,
    )
    return None
