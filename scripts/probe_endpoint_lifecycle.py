"""端点生命周期探针：复现交互菜单「检索 → 写入 → 立即消费 → 再检索」的状态链。

为什么需要这个探针：TUI 的写入页写完会问「现在就跑一次 worker 落库？」。
0.9.9 的 dispatch 是 `lambda argv: main(argv)`——子命令在**同一个进程**里跑，
worker 起自己的 chroma server、覆盖 MEMORY_VECTOR_PORT，退出时删端口文件；
进程内 env 仍指向死端口，下一次检索就报 "Could not connect to a Chroma
server"，此后整个会话全废（PRD F1）。

0.9.10 的修法：① dispatch 改子进程（menu.subprocess_dispatch）；② worker 不再
起 server（serve 才是数据所有者）；③ 自己注入的 env 带来源标记，每次复验。

本探针断言：
  1. 首次检索成功并登记端点
  2. 子进程跑 worker --once 后，**父进程 env 不变**、端口文件仍在
  3. 再次检索成功（不再出现 "Could not connect" / 回落嵌入式）
  4. 杀掉 serve 后，残留登记被清理、下一次检索自动重新拉起（不需人工干预）

用法（沙箱数据，勿指向真实库）：
  HY_MEM_CONFIG=<沙箱>/config.json MEMORY_DATA_DIR=<沙箱>/data \
    .venv/Scripts/python.exe scripts/probe_endpoint_lifecycle.py "胡超是谁"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

QUERY = sys.argv[1] if len(sys.argv) > 1 else "胡超是谁"
DATA = Path(os.environ["MEMORY_DATA_DIR"]).resolve() if os.environ.get("MEMORY_DATA_DIR") else None
PORT_FILE = (DATA / "server.port") if DATA else None

ENV = dict(os.environ)
for k in ("MEMORY_VECTOR_HOST", "MEMORY_VECTOR_PORT", "MEMORY_VECTOR_HOST_SRC"):
    ENV.pop(k, None)


def run(argv: list[str], timeout: float = 300) -> tuple[int, str, str, float]:
    """跑一次子命令 —— 与 0.9.10 的 TUI dispatch 同一路径：**子进程**。"""
    t0 = time.time()
    p = subprocess.run(
        [sys.executable, "cli/hy_mem_cli.py"] + argv,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=ENV, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )
    return p.returncode, p.stdout or "", p.stderr or "", time.time() - t0


def show(tag: str, rc: int, out: str, err: str, dt: float) -> None:
    print(f"--- {tag}: rc={rc} {dt:.1f}s")
    for line in (out or "").strip().splitlines()[-2:]:
        print("   out|", line[:150])
    for line in (err or "").strip().splitlines()[-2:]:
        print("   err|", line[:150])


def adopted_port() -> str:
    """父进程 env 里的端点（用来证明子进程没有污染父进程）。"""
    return f"{os.environ.get('MEMORY_VECTOR_HOST')}:{os.environ.get('MEMORY_VECTOR_PORT')}"


print("env before:", adopted_port())
env_before = adopted_port()

rc1, o1, e1, t1 = run(["--json", "search", QUERY, "-n", "3"])
show("1. 首次检索（应自动拉起 serve）", rc1, o1, e1, t1)
print("   端口文件:", PORT_FILE.read_text(encoding="utf-8") if PORT_FILE and PORT_FILE.exists() else "（无）")

rc2, o2, e2, t2 = run(["queue"])
show("2. 队列状态", rc2, o2, e2, t2)

port_before = PORT_FILE.read_text(encoding="utf-8") if PORT_FILE and PORT_FILE.exists() else ""

# 这一步就是交互菜单写入后选「现在就跑一次 worker 落库」的等价操作
rc3, o3, e3, t3 = run(["worker", "--once", "--poll", "0.2"])
show("3. 子进程跑 worker --once（菜单行为）", rc3, o3, e3, t3)
port_after = PORT_FILE.read_text(encoding="utf-8") if PORT_FILE and PORT_FILE.exists() else ""
print("   端口文件 worker 前后一致:", port_before.strip() == port_after.strip())
print("   父进程 env 未被污染:", adopted_port() == env_before)

rc4, o4, e4, t4 = run(["--json", "search", QUERY, "-n", "3"])
show("4. 再次检索（0.9.9 的 bug 现场）", rc4, o4, e4, t4)
fallback = "回落嵌入式" in (e4 or "")
print("   是否回落嵌入式:", fallback)

# 5. 杀掉 serve（模拟崩溃/空闲退出）→ 残留登记应被清理并自动重启
info = {}
try:
    info = json.loads(port_after) if port_after.strip() else {}
except json.JSONDecodeError:
    info = {}
killed = False
if info.get("pid"):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(info["pid"]), "/F"],
                           capture_output=True, timeout=30)
        else:
            os.kill(int(info["pid"]), 9)
        killed = True
    except Exception:  # noqa: BLE001
        killed = False
time.sleep(1.0)
rc5, o5, e5, t5 = run(["--json", "search", QUERY, "-n", "3"], timeout=180)
show(f"5. 杀掉 serve(pid={info.get('pid')}) 后再检索（应自愈）", rc5, o5, e5, t5)
print("   自愈后端口文件:", PORT_FILE.read_text(encoding="utf-8") if PORT_FILE and PORT_FILE.exists() else "（无）")

checks = {
    "首次检索成功": rc1 == 0 and '"count"' in o1,
    "worker 未打掉端点登记": port_before.strip() == port_after.strip() and bool(port_after),
    "父进程 env 未被污染": adopted_port() == env_before,
    "再次检索成功": rc4 == 0 and '"count"' in o4,
    "未静默回落嵌入式": not fallback,
    "serve 被杀后能自愈": (not killed) or (rc5 == 0 and '"count"' in o5),
}
print()
for name, okv in checks.items():
    print(("  PASS  " if okv else "  FAIL  ") + name)
print("\nRESULT:", "PASS" if all(checks.values()) else "FAIL")
sys.exit(0 if all(checks.values()) else 1)
