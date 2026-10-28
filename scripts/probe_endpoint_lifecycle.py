"""端点生命周期探针：复现交互菜单「检索 → 写入 → 立即消费 → 再检索」的状态链。

为什么需要这个探针：TUI 的 dispatch 是 `lambda argv: main(argv)`——子命令在
*同一个进程*里执行（见 hy_mem_cli.py:2445）。于是 `worker --once` 会在交互
进程内起自己的 chroma server 线程、覆盖 MEMORY_VECTOR_PORT，退出时再删掉
端口文件、停掉线程；但进程内的 env 仍指向那个已死的端口。下一次检索
ensure_endpoint 看到 MEMORY_VECTOR_HOST 有值就直接返回（当成外部配置），
不复验→SDK 连死端口→"Could not connect to a Chroma server"。

用法（沙箱数据，勿指向真实库）：
  MEMORY_DATA_DIR=<沙箱>/data HY_MEM_CONFIG=<安装>/config.json \
    .venv/Scripts/python.exe scripts/probe_endpoint_lifecycle.py "胡超是谁"
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cli"))

QUERY = sys.argv[1] if len(sys.argv) > 1 else "胡超是谁"


def run(argv: list[str]) -> tuple[int, str, str]:
    """跑一次子命令（与 TUI dispatch 完全同一路径：进程内 main()）。"""
    from hy_mem_cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as e:  # argparse 用法错误
            rc = int(e.code or 0)
        except BaseException as e:  # noqa: BLE001 探针要看到原始异常
            print(f"{type(e).__name__}: {e}", file=err)
            rc = -1
    return rc, out.getvalue(), err.getvalue()


def show(tag: str, rc: int, out: str, err: str) -> None:
    tail_out = (out or "").strip().splitlines()
    print(f"--- {tag}: rc={rc}")
    for line in tail_out[-3:]:
        print("   out|", line[:180])
    for line in (err or "").strip().splitlines()[-3:]:
        print("   err|", line[:180])


print("env before:", {k: os.environ.get(k) for k in
                      ("MEMORY_VECTOR_HOST", "MEMORY_VECTOR_PORT")})

rc1, o1, e1 = run(["--json", "search", QUERY, "-n", "3"])
show("1. 首次检索（应自动拉起 server 宿主）", rc1, o1, e1)
print("   env after search:", os.environ.get("MEMORY_VECTOR_HOST"),
      os.environ.get("MEMORY_VECTOR_PORT"))

rc2, o2, e2 = run(["queue"])
show("2. 队列状态", rc2, o2, e2)

# 这一步就是交互菜单写入后选「现在就跑一次 worker 落库」的等价操作
rc3, o3, e3 = run(["worker", "--once", "--poll", "0.2"])
show("3. 进程内跑 worker --once（菜单行为）", rc3, o3, e3)
print("   env after worker:", os.environ.get("MEMORY_VECTOR_HOST"),
      os.environ.get("MEMORY_VECTOR_PORT"))
print("   port file exists:", (Path(os.environ["MEMORY_DATA_DIR"]) / "server.port").exists())

rc4, o4, e4 = run(["--json", "search", QUERY, "-n", "3"])
show("4. 再次检索（bug 现场：应能自愈，实际报 Could not connect）", rc4, o4, e4)

ok = rc4 == 0 and '"ok": true' in (o4 or "")
print("\nRESULT:", "PASS" if ok else "FAIL（端点未自愈）")
sys.exit(0 if ok else 1)
