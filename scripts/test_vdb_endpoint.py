"""vdb_endpoint 自测 —— 端口文件归属 / 心跳 / 端点注入 / 自注入复验 / server 线程.

背景（2026-10-28 事故）：chroma embedded 多进程共享导致长驻进程召回池为空。
收敛后由 `hy-mem serve` 独占数据（长驻单例），其余进程经
MEMORY_VECTOR_HOST/PORT 走 HttpClient。本文件验证端点层契约：

  1. 端口文件读写、容错、**归属校验**（只删自己登记的）
  2. 心跳探测（本地回环，不发外部网络请求）
  3. ensure_endpoint 分支：外部配置不动 / 主人活着必须成功否则明确失败 /
     主人已死则清残留并拉起 / 自注入的 env 每次复验
  4. 集成：真实 chroma server 线程 起/停 + HttpClient 读写 + owner 语义

用法：.venv/Scripts/python.exe scripts/test_vdb_endpoint.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))

TMP = Path(tempfile.mkdtemp(prefix="hy-mem-test-vdb-"))
os.environ["MEMORY_DATA_DIR"] = str(TMP / "data")
os.environ["HY_MEM_TUI"] = "0"
(TMP / "data").mkdir(parents=True, exist_ok=True)

import vdb_endpoint  # noqa: E402

results: list[bool] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _FakeHeartbeat(BaseHTTPRequestHandler):
    """伪装 chroma 的 /api/v2/heartbeat，只回 200。"""

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):  # 静默
        pass


def _start_fake_heartbeat() -> tuple[int, ThreadingHTTPServer]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHeartbeat)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], srv


def _dead_pid() -> int:
    """拿一个确定已经退出的 pid（用于「主人已死」分支）。"""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=30)
    return p.pid


def _clean_env() -> None:
    for k in ("MEMORY_VECTOR_HOST", "MEMORY_VECTOR_PORT", "MEMORY_VECTOR_HOST_SRC"):
        os.environ.pop(k, None)


def main() -> int:
    print(f"临时目录 {TMP}")

    # ---- 1. 端口文件 ----
    print("\n端口文件：读写 / 容错 / 归属")
    vdb_endpoint.write_port_file(12345)
    check("写入后能读回", vdb_endpoint.read_port() == 12345)
    info = vdb_endpoint.read_port_info() or {}
    check("登记了主人 pid 与角色",
          info.get("pid") == os.getpid() and info.get("owner") == "serve", str(info))
    check("别人的 pid 删不掉我的登记",
          vdb_endpoint.remove_port_file(expect_pid=os.getpid() + 1) is False
          and vdb_endpoint.read_port() == 12345)
    check("自己的 pid 能删",
          vdb_endpoint.remove_port_file(expect_pid=os.getpid()) is True
          and vdb_endpoint.read_port() is None)
    vdb_endpoint.port_file().write_text("not-json", encoding="utf-8")
    check("垃圾内容 → None", vdb_endpoint.read_port() is None)
    vdb_endpoint.port_file().write_text(json.dumps({"port": "x"}), encoding="utf-8")
    check("port 非整数 → None", vdb_endpoint.read_port() is None)
    vdb_endpoint.remove_port_file()
    check("删除后 → None", vdb_endpoint.read_port() is None)
    vdb_endpoint.remove_port_file()  # 幂等
    check("重复删除不抛错", True)

    # ---- 2. 心跳 ----
    print("\n心跳探测（仅本地回环）")
    closed = _free_port()
    check("未监听端口 → False", vdb_endpoint.heartbeat_ok(closed, timeout=0.5) is False)
    hport, hsrv = _start_fake_heartbeat()
    try:
        check("伪造心跳 200 → True", vdb_endpoint.heartbeat_ok(hport, timeout=1.0) is True)
    finally:
        hsrv.shutdown()

    # ---- 3. ensure_endpoint ----
    print("\n端点注入分支")
    _clean_env()
    os.environ["MEMORY_VECTOR_HOST"] = "10.0.0.9"
    os.environ["MEMORY_VECTOR_PORT"] = "9999"
    vdb_endpoint.write_port_file(_free_port())
    port = vdb_endpoint.ensure_endpoint(spawn=lambda: False, wait_s=1.0)
    check("外部已配置（无来源标记）→ 不动 env",
          port is None and os.environ["MEMORY_VECTOR_HOST"] == "10.0.0.9")
    _clean_env()
    vdb_endpoint.remove_port_file()

    # 3.1 主人活着（pid=本进程）但心跳不通 → 明确失败，不静默回落（PRD F3）
    vdb_endpoint.write_port_file(closed)
    try:
        vdb_endpoint.ensure_endpoint(spawn=lambda: False, wait_s=0.6)
        check("主人活着但连不上 → 抛 EndpointUnavailable", False, "没有抛异常")
    except vdb_endpoint.EndpointUnavailable as e:
        check("主人活着但连不上 → 抛 EndpointUnavailable", True, str(e)[:80])
    check("失败时不注入 env", "MEMORY_VECTOR_HOST" not in os.environ)
    vdb_endpoint.remove_port_file()

    # 3.2 主人已死 → 清残留 + 拉起（F2/F10）
    dead = _dead_pid()
    vdb_endpoint.write_port_file(closed, pid=dead)
    spawned: list[bool] = []

    def _spawn_noop() -> bool:
        spawned.append(True)
        return False

    port = vdb_endpoint.ensure_endpoint(spawn=_spawn_noop, wait_s=1.0)
    check("主人已死 + spawn 失败 → None", port is None)
    check("陈旧登记被清理", vdb_endpoint.read_port() is None)
    check("spawn 被尝试过", bool(spawned))
    check("失败时不设 env", "MEMORY_VECTOR_HOST" not in os.environ)

    # 3.3 拉起后轮询到就绪 → 采纳并打来源标记
    hport2, hsrv2 = _start_fake_heartbeat()

    def _spawn_slow() -> bool:
        def _late():
            time.sleep(0.3)
            vdb_endpoint.write_port_file(hport2)
        threading.Thread(target=_late, daemon=True).start()
        return True

    port = vdb_endpoint.ensure_endpoint(spawn=_spawn_slow, wait_s=5.0)
    check("拉起后轮询到就绪 → 返回端口", port == hport2)
    check("env 被正确注入",
          os.environ.get("MEMORY_VECTOR_HOST") == "127.0.0.1"
          and os.environ.get("MEMORY_VECTOR_PORT") == str(hport2))
    check("注入时打来源标记", os.environ.get("MEMORY_VECTOR_HOST_SRC") == "hymem")

    # 3.4 自注入的端点失效 → 清掉重取（F4）；热路径仍直接复用
    check("自注入且可达 → 直接复用（不额外拉起）",
          vdb_endpoint.ensure_endpoint(spawn=lambda: spawned.append("bad"), wait_s=1.0) == hport2)
    hsrv2.shutdown()
    time.sleep(0.2)
    vdb_endpoint.remove_port_file()
    port = vdb_endpoint.ensure_endpoint(spawn=lambda: False, wait_s=0.8)
    check("自注入端点失效 → 清 env 并重新获取", port is None and "MEMORY_VECTOR_HOST" not in os.environ)
    _clean_env()
    vdb_endpoint.remove_port_file()

    # ---- 4. 集成：真实 chroma server 线程 ----
    print("\nchroma server 线程（集成，较慢）")
    t0 = time.time()
    persist = TMP / "vdb"
    persist.mkdir(parents=True, exist_ok=True)
    try:
        sport, srv = vdb_endpoint.start_server_thread(persist)
        check("server 线程就绪", bool(sport))
        check("心跳可达", vdb_endpoint.heartbeat_ok(sport, timeout=2.0) is True)
        check("活动时间被刷新（serve 空闲退出依据）",
              time.time() - vdb_endpoint.last_activity() < 30)

        import chromadb
        c = chromadb.HttpClient(host="127.0.0.1", port=sport)
        col = c.get_or_create_collection("probe_vdb", metadata={"hnsw:space": "cosine"})
        col.upsert(ids=["a"], embeddings=[[0.1] * 8], documents=["v1"])
        col.upsert(ids=["a"], embeddings=[[0.9] * 8], documents=["v2"])
        got = col.get(ids=["a"])
        check("HttpClient 写读一致", (got.get("documents") or [""])[0] == "v2")

        # bug 场景回归（2026-10-28 事故形态）：col 是 upsert 之前建立的
        # 「老客户端」，必须立刻看到自己/别人后续的写入——embedded 多进程
        # 下这一步会读到过期索引。
        col.upsert(ids=["a"], embeddings=[[0.5] * 8], documents=["v3"])
        got_old = col.get(ids=["a"])
        check("老客户端立见新写入（bug 场景回归）",
              (got_old.get("documents") or [""])[0] == "v3")

        # 归属语义（PRD F2）：停 server 只能删自己的登记，不能误删别人的
        vdb_endpoint.write_port_file(_free_port(), pid=os.getpid() + 1)
        vdb_endpoint.stop_server_thread(srv, sport)
        time.sleep(0.2)
        check("停 server 不误删别人的登记", vdb_endpoint.read_port() is not None)
        check("停后心跳不可达", vdb_endpoint.heartbeat_ok(sport, timeout=0.5) is False)
        vdb_endpoint.remove_port_file()

        # 持久性契约（防「内存库」回归：只传 persist_directory 不传
        # is_persistent 时 server 静默变内存库，检索永远空——2026-10-28 实测）
        sport2, srv2 = vdb_endpoint.start_server_thread(persist)
        c2 = chromadb.HttpClient(host="127.0.0.1", port=sport2)
        got2 = c2.get_collection("probe_vdb").get(ids=["a"])
        check("重启后数据仍在（is_persistent 生效）",
              (got2.get("documents") or [""])[0] == "v3")
        # 跨进程读 embeddings 契约（2026-10-28 故障形态）：chroma 1.5.9
        # server 对队列期记录（brute force/queue 路径）返回 list，
        # FastAPI 响应层 .tolist() 直接 AttributeError——链式去重
        # （get_by_ids/get_embeddings）全挂，superseded 条目漏进检索结果。
        # 修复：vdb_endpoint 归一化 SegmentAPI._get/_query 的 embeddings。
        try:
            got3 = c2.get_collection("probe_vdb").get(
                ids=["a"], include=["embeddings"])
            embs = got3.get("embeddings")
            # chroma 客户端可能把 embeddings 反序列化为 numpy，禁用 or 兜底
            emb = embs[0] if embs is not None and len(embs) > 0 else None
            check("重启后 HTTP 读 embeddings（tolist 兼容）",
                  emb is not None and len(emb) == 8,
                  f"type={type(emb).__name__} len={len(emb) if emb is not None else 'NA'}")
        except Exception as e:  # noqa: BLE001
            check("重启后 HTTP 读 embeddings（tolist 兼容）", False,
                  f"{type(e).__name__}: {str(e)[:120]}")
        vdb_endpoint.stop_server_thread(srv2, sport2)
        print(f"  （集成段耗时 {time.time() - t0:.1f}s）")
    except Exception as e:  # noqa: BLE001
        check("chroma 集成段异常", False, f"{type(e).__name__}: {e}")

    ok = all(results)
    print(f"\n{'全部通过' if ok else '存在失败'}：{results.count(True)}/{len(results)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
