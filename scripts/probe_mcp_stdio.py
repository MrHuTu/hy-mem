"""MCP stdio 实测：对已安装 exe 走真实 stdio 协议。

覆盖：initialize → tools/list → tools/call(search_memory) → add_memory（异步）
不依赖任何第三方客户端库，直接按 JSON-RPC 行协议对话。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

EXE = Path(os.getenv("HYMEM_EXE", r"D:\tools_open\hy-mem-0.9.9-win64\hy-mem.exe"))


def main() -> int:
    p = subprocess.Popen(
        [str(EXE), "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        bufsize=1,
    )
    seq = {"n": 0}

    def rpc(method: str, params: dict | None = None, timeout: float = 60.0):
        seq["n"] += 1
        msg = {"jsonrpc": "2.0", "id": seq["n"], "method": method}
        if params is not None:
            msg["params"] = params
        p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        p.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("stdout EOF")
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == msg["id"]:
                return obj
        raise TimeoutError(method)

    def notify(method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        p.stdin.flush()

    init = rpc("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1.0"},
    })
    srv = init["result"].get("serverInfo", {})
    print(f"[init] {srv.get('name')} {srv.get('version')} proto={init['result'].get('protocolVersion')}")
    notify("notifications/initialized")

    tools = rpc("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    print(f"[tools] {len(names)}: {', '.join(names)}")

    r = rpc("tools/call", {"name": "search_memory", "arguments": {"query": "胡超是谁", "limit": 3}})
    text = r["result"]["content"][0]["text"]
    print("[search raw]", text[:600])
    data = json.loads(text)
    items = data.get("hits") or data.get("memories") or data.get("results") or []
    print(f"[search] count={len(items)}")
    for it in items:
        print("   -", str(it.get("content") or it.get("text"))[:70].replace("\n", " "))

    marker = f"mcp探针-{int(time.time())}"
    a = rpc("tools/call", {"name": "add_memory", "arguments": {
        "text": f"用户偏好：hy-mem 的 MCP 服务用 stdio 协议，由宿主拉起（{marker}）",
        "tags": ["mcp-probe"],
    }})
    ad = json.loads(a["result"]["content"][0]["text"])
    print(f"[add] job_ids={ad.get('job_ids')} pending={ad.get('pending')} "
          f"spawned={ad.get('worker_spawned')} live={ad.get('worker_live')}")

    p.stdin.close()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
    err = (p.stderr.read() or "").strip()
    bad = [ln for ln in err.splitlines() if "Traceback" in ln]
    print(f"[stderr] {len(err.splitlines())} 行，Traceback {len(bad)} 处")
    if bad:
        print(err[-1500:])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
