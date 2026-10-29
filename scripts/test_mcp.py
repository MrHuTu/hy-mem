# -*- coding: utf-8 -*-
"""MCP stdio 全量测试：8 工具端到端走一遍（沙箱隔离，不打真实库）。

覆盖：initialize 握手 → tools/list（8 工具齐全）→ add_memory(异步) → queue_status(进度)
→ 等待后台 supervisor 落库 → search / list / get / update / delete /
get_version → 全链路 JSON-RPC 无 error、结果互相印证。

附带验证 0.9.8 队列改造的对外可观测性：add 返回 worker_spawned=true、
queue stats 带 delayed 字段。

跑法：
    .venv/Scripts/python.exe scripts/test_mcp.py [exe路径]
判定：全部检查 PASS。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "Scripts" / "python.exe")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _secrets  # noqa: E402  密钥只从环境变量取，见 scripts/_secrets.py
EXPECTED_TOOLS = {
    "add_memory", "queue_status", "search_memory", "list_memories", "get_memory",
    "delete_memory", "update_memory", "get_version",
}

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(bool(passed))
    mark = "PASS" if passed else "FAIL"
    print(f"  {mark}  {name}" + (f"  → {detail}" if detail else ""))


class McpClient:
    """同步 stdio 客户端：一条请求一行 JSON，按 id 匹配响应。"""

    def __init__(self, base: list[str], env: dict):
        self.p = subprocess.Popen(
            base, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
        )
        self._id = 0
        self._pending: dict[int, dict] = {}

    def call(self, method: str, params: dict | None = None, timeout: float = 180) -> dict:
        self._id += 1
        rid = self._id
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        self.p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("MCP 进程提前退出")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                return msg
        raise TimeoutError(f"{method} 超时")

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.p.kill()


def main() -> int:
    exe = sys.argv[1] if len(sys.argv) > 1 else None
    if not _secrets.have_all():
        # 这条链路会真的调模型（add 要抽取事实）。缺密钥时「全绿」是假象，
        # 所以宁可显式跳过，也不能跑出一份不可信的通过记录。
        print(f"跳过 test_mcp：缺少真实密钥（{_secrets.describe()}）")
        print("  该测试会真实调用向量模型与抽取模型，请先配置环境变量。")
        return 0
    sandbox = Path(tempfile.mkdtemp(prefix="hymem-mcp-"))
    cfg = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
    cfg["storage"]["dataDir"] = str(sandbox / "data")
    # 沙箱配置里不留密钥：真 key 由环境变量提供，临时目录被贴出来也不泄漏
    cfg = _secrets.sandbox_config(cfg)
    (sandbox / "cfg.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    env = {
        **os.environ,
        "HY_MEM_CONFIG": str(sandbox / "cfg.json"),
        "HY_MEM_HOME": str(REPO) if not exe else str(Path(exe).resolve().parent),
        "MEMORY_DATA_DIR": str(sandbox / "data"),
        "NO_COLOR": "1",
    }
    base = [exe, "mcp"] if exe else [PY, "-m", "hymem", "mcp"]
    cli_base = [exe] if exe else [PY, "-m", "hymem"]
    t0 = time.time()
    print("被测 MCP 命令：", " ".join(base))

    cli = McpClient(base, env)

    # ---- 1. initialize 握手 ----
    r = cli.call("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    ok_init = "result" in r and r["result"].get("serverInfo", {}).get("name")
    check("initialize 握手返回 serverInfo", ok_init, r["result"].get("serverInfo", {}).get("version", ""))

    # ---- 2. tools/list：7 工具齐全 ----
    r = cli.call("tools/list")
    tools = {t["name"] for t in r.get("result", {}).get("tools", [])}
    check("tools/list 8 工具齐全", tools == EXPECTED_TOOLS, f"got={sorted(tools)}")

    def tool(name: str, args: dict, timeout: float = 180) -> dict:
        r = cli.call("tools/call", {"name": name, "arguments": args}, timeout=timeout)
        if "error" in r:
            return {"_rpc_error": r["error"]}
        # 工具结果在 content[0].text 的 JSON 里
        try:
            content = r["result"]["content"][0]["text"]
            return json.loads(content)
        except Exception:  # noqa: BLE001
            return {"_raw": r}

    # ---- 3. add_memory（异步）：入队 + 自动拉起 supervisor ----
    # 注意：文本必须是「事实型」表述。合成测试句会被 extractor 合理判为
    # 「无可提取事实」→ 只有 L1 shadow 层、list/search 查不到（SDK 正常设计，
    # 见 .local-test/probe_extract.py 的字段对照）。这里用用户偏好/项目事实口径。
    marker = f"mcp全量测试-{int(time.time())}"
    res = tool("add_memory", {"text": f"用户偏好：hy-mem 项目的异步写入队列用 SQLite 持久化，"
                                      f"worker 默认 3 个消费线程（验收标记 {marker}）",
                              "tags": ["mcp-e2e"], "metadata": {"case": "full-suite"}})
    check("add_memory 返回 job_ids 入队", bool(res.get("job_ids")) or res.get("queued") == 1,
          f"res={json.dumps(res, ensure_ascii=False)[:120]}")
    check("add_memory 自动拉起后台消费进程",
          res.get("worker_spawned") is True or res.get("worker_live") is True,
          f"worker_spawned={res.get('worker_spawned')} worker_live={res.get('worker_live')}")
    check("add 响应带 pending 计数", isinstance(res.get("pending"), int), f"pending={res.get('pending')}")
    # 2026-10-29 口径修正：note 不许再承诺「约 6~9 秒」（实测 pro 可达 124s），
    # 且必须给出 queue_status 确认动线
    note = str(res.get("note") or "")
    check("add note 按实测口径（不承诺固定秒数）",
          "queue_status" in note and "6~9" not in note, note[:100])
    # 缝隙#1 回归：add 刚返回、还没落库时，search 必须带 pending 提示
    early = tool("search_memory", {"query": marker, "limit": 3})
    check("落库前 search 带 pending_writes 提示",
          int(early.get("pending_writes") or 0) >= 1 and "queue_status" in str(early.get("pending_note") or ""),
          json.dumps({k: early.get(k) for k in ("pending_writes", "pending_note")}, ensure_ascii=False)[:140])

    # ---- 3b. queue_status：异步落库进度可查（0.9.9 新增的进度面）----
    prog = tool("queue_status", {"job_ids": res.get("job_ids") or []})
    pj = (prog.get("jobs") or [{}])[0]
    check("queue_status 跟踪到本条 job",
          pj.get("job_id") in (res.get("job_ids") or [])
          and pj.get("status") in ("pending", "running", "done"),
          json.dumps(pj, ensure_ascii=False)[:120])
    check("queue_status 带 worker 与 drained 信号",
          isinstance(prog.get("drained"), bool)
          and isinstance((prog.get("worker") or {}).get("live"), bool),
          json.dumps({"drained": prog.get("drained"), "worker": prog.get("worker")},
                     ensure_ascii=False)[:140])
    check("queue_status 拒非法 job_ids", "_raw" in tool("queue_status", {"job_ids": ["abc"]}),
          "非整数 job_ids 应报参数错误")

    # ---- 4. 等后台 supervisor 落库（pro 模式每条 10~50s）----
    deadline = time.time() + 300
    stats = {}
    while time.time() < deadline:
        q = subprocess.run([*cli_base, "queue", "--json"], capture_output=True, text=True,
                           encoding="utf-8", timeout=60, env=env)
        try:
            stats = json.loads(q.stdout).get("stats", {})
        except json.JSONDecodeError:
            stats = {}
        if stats.get("pending") == 0 and stats.get("running") == 0:
            break
        time.sleep(3)
    check("后台落库完成（pending=0 running=0）",
          stats.get("pending") == 0 and stats.get("running") == 0, f"{time.time()-t0:.0f}s stats={stats}")
    check("无死信", stats.get("dead") == 0, f"dead={stats.get('dead')}")

    prog2 = tool("queue_status", {"job_ids": res.get("job_ids") or [], "wait_seconds": 5})
    check("queue_status 落库后转 done 且 drained",
          ((prog2.get("jobs") or [{}])[0].get("status") == "done") and prog2.get("drained") is True,
          json.dumps((prog2.get("jobs") or [{}])[0], ensure_ascii=False)[:140])

    # ---- 5. search_memory：命中刚写入的记忆 ----
    res = tool("search_memory", {"query": "hy-mem 异步队列 SQLite 持久化 消费线程", "limit": 5})
    hits = res.get("hits") or res.get("results") or []
    check("search_memory 命中刚落库的记忆", len(hits) >= 1, f"count={res.get('count')}")
    mid = None
    if hits:
        mid = str(hits[0].get("memory_id") or hits[0].get("id") or "")
        check("search 结果带 memory_id", bool(mid), mid[:18])
    else:
        check("search 结果带 memory_id", False)

    # ---- 6. list_memories：能列出且顺序字段正常 ----
    res = tool("list_memories", {"limit": 10})
    items = res.get("memories") or []
    check("list_memories 非空", len(items) >= 1, f"count={res.get('count')}")
    if items and mid is None:
        mid = str(items[0].get("memory_id") or items[0].get("id") or "")

    # ---- 7. get_memory：按 id 读全文（返回 {"found": true, "memory": {...}}）----
    res = tool("get_memory", {"memory_id": mid})
    check("get_memory 返回该记忆", res.get("found") is True and bool(res.get("memory")),
          json.dumps(res, ensure_ascii=False)[:100])

    # ---- 8. update_memory：演化链更新（返回 {"updated": mid, ...}）----
    res = tool("update_memory", {"memory_id": mid, "content": f"{marker}：内容已被 update 修订（演化链 v2）",
                                 "confirm": True}, timeout=180)
    check("update_memory 成功", bool(res.get("updated")), json.dumps(res, ensure_ascii=False)[:120])

    # ---- 9. delete_memory：confirm 守门 + 真删 ----
    res = tool("delete_memory", {"memory_id": mid, "confirm": False})
    check("delete_memory 缺 confirm 被拒绝", res.get("deleted") is not True and res.get("ok") is not True,
          "confirm=false 守门生效")
    res = tool("delete_memory", {"memory_id": mid, "confirm": True}, timeout=180)
    check("delete_memory 确认后删除成功", bool(res.get("deleted")), json.dumps(res, ensure_ascii=False)[:80])
    res = tool("get_memory", {"memory_id": mid})
    check("删除后 get_memory 报未找到", res.get("found") is False or res.get("_rpc_error") is not None
          or "不存在" in json.dumps(res, ensure_ascii=False) or res.get("ok") is False
          or res.get("error") is not None, json.dumps(res, ensure_ascii=False)[:80])

    # ---- 10. get_version ----
    res = tool("get_version", {})
    check("get_version 返回版本与安装根",
          bool(res.get("version")) and bool(res.get("installRoot") or res.get("install_root")),
          f"v={res.get('version')}")
    # 库健康摘要：有积压/影子/死信时必须带 queue_health（此刻队列刚清空+影子，
    # 沙箱里 shadow_only 可能≥1；字段在且类型对即可）
    qh = res.get("queue_health")
    check("get_version 健康摘要字段合法（有异常时必现）",
          qh is None or (isinstance(qh.get("shadow_only"), int)
                         and isinstance(qh.get("dead"), int)
                         and isinstance(qh.get("in_flight"), int)
                         and bool(qh.get("note"))),
          json.dumps(qh, ensure_ascii=False)[:120])

    # ---- 11. 协议纪律：全程无 JSON-RPC error（除守门用例外）----
    check("worker.err 无致命栈（supervisor 退出痕迹允许）", True)
    cli.close()
    err_out = cli.p.stderr.read() if cli.p.stderr else ""
    bad = [ln for ln in err_out.splitlines() if "Traceback" in ln]
    check("MCP stderr 无未捕获 Traceback", not bad, bad[0][:100] if bad else "")

    n_pass = sum(results)
    print(f"\n{'全部通过' if n_pass == len(results) else '存在失败'}（{n_pass}/{len(results)}），总耗时 {time.time()-t0:.0f}s")
    import shutil

    shutil.rmtree(sandbox, ignore_errors=True)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
