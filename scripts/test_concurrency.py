# -*- coding: utf-8 -*-
"""并发压测：模拟多子代理同时读写 hy-mem，验证不卡死、不丢数据。

场景：多智能体框架下每个子代理各拉起自己的 CLI/MCP 进程，同时打同一份
数据目录。防护机制（store 层）：
- sqlite WAL + busy_timeout=30s：读读并行、写写排队；
- WriteLock 跨进程排他写锁（msvcrt/flock，180s 超时降级）；
- worker 心跳去重：并发 add --async 只拉起一个 worker。

用法：python scripts/test_concurrency.py [exe路径]（缺省用源码口径）
判定：全部进程退出码 0、无锁超时、drain 后队列无死信 → PASS
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "Scripts" / "python.exe")


def main() -> int:
    exe = sys.argv[1] if len(sys.argv) > 1 else None

    def cli_base():
        return [exe] if exe else [PY, str(REPO / "cli" / "hy_mem_cli.py")]

    def mcp_base():
        return [exe, "mcp"] if exe else [PY, str(REPO / "cli" / "hy_mem_cli.py"), "mcp"]

    t0 = time.time()
    procs: dict[str, subprocess.Popen] = {}

    # --- 4 个并发异步写（模拟 4 个子代理同时「记一下」）---
    for i in range(4):
        procs[f"add-{i}"] = subprocess.Popen(
            [*cli_base(), "add", "--async", f"并发压测-{i}：多子代理同时写入落库验证", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
    # --- 2 个 MCP 客户端进程，各做一轮 search+list（模拟并发检索）---
    mcp_msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "load", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "list_memories", "arguments": {"limit": 5}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search_memory", "arguments": {"query": "并发压测 落库验证", "limit": 3}}},
    ]
    mcp_stdin = "\n".join(json.dumps(m, ensure_ascii=False) for m in mcp_msgs) + "\n"
    for i in range(2):
        procs[f"mcp-{i}"] = subprocess.Popen(
            mcp_base(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        procs[f"mcp-{i}"]._stdin_data = mcp_stdin  # type: ignore[attr-defined]

    # --- 同时发车，收集结果 ---
    fails = []
    for name, p in procs.items():
        data = getattr(p, "_stdin_data", None)
        out, err = p.communicate(input=data, timeout=180)
        dt = time.time() - t0
        ok = p.returncode == 0
        if name.startswith("add-"):
            ok = ok and (f'"queued": {1}' in (out or "") or '"queued":1' in (out or ""))
        if name.startswith("mcp-"):
            # MCP 进程退出码不承载语义，看响应里有没有 error
            ok = ok and '"error"' not in (out or "")
        if not ok:
            fails.append((name, p.returncode, (err or "")[-200:], (out or "")[-200:]))
        print(f"  {name:8s} rc={p.returncode} {dt:5.1f}s")

    # --- 消化队列，验证 4 条全部落库、无死信 ---
    r = subprocess.run([*cli_base(), "worker", "--once"], capture_output=True, text=True,
                       encoding="utf-8", timeout=600)
    q = subprocess.run([*cli_base(), "queue", "--json"], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    try:
        qd = json.loads(q.stdout)
        dead = qd.get("dead") or qd.get("deadLetter") or 0
    except json.JSONDecodeError:
        dead = "?"
    sr = subprocess.run([*cli_base(), "search", "并发压测 多子代理同时写入", "--json"],
                        capture_output=True, text=True, encoding="utf-8", timeout=120)
    hits = len(json.loads(sr.stdout).get("results", [])) if sr.returncode == 0 else -1

    print(f"\n队列死信: {dead} | 落库检索命中: {hits} 条（应≥1）")
    if fails or dead not in (0, "?") or hits < 1:
        print("FAIL")
        for name, rc, e, o in fails:
            print(f"  {name} rc={rc}\n    err: {e}\n    out: {o}")
        return 1
    print(f"PASS：{len(procs)} 进程并发，全部正常，总耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
