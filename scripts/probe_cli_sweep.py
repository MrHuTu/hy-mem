"""CLI 功能点巡检：一次性跑完常用子命令，只记录结果，不改代码。

背景（2026-10-28）：用户要求「发现 bug 先别修，先把各功能点测一遍，记成 PRD，
再一次性修」——反复打包修单个 bug 太费时间。本脚本就是那轮的取证工具。

用法：
  HY_MEM_CONFIG=<沙箱>/config.json \
    python scripts/probe_cli_sweep.py            # 默认打已装 exe
  HY_MEM_CONFIG=... HYMEM_EXE=<别的 exe> python scripts/probe_cli_sweep.py

注意：会写入/删除一条测试记忆（标签「冒烟巡检」），务必指向沙箱配置。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

EXE = Path(os.getenv("HYMEM_EXE", r"D:\tools_open\hy-mem-0.9.9-win64\hy-mem.exe"))
CFG = os.environ.get("HY_MEM_CONFIG", "")
OUT = Path(os.getenv("HYMEM_SWEEP_OUT", Path.home() / "hymem_sweep.json"))
TAG = "冒烟巡检"

if not CFG:
    sys.exit("必须设置 HY_MEM_CONFIG（指向沙箱配置，避免污染真实库）")

ENV = dict(os.environ)
ENV["HY_MEM_CONFIG"] = CFG
ENV.pop("MEMORY_VECTOR_HOST", None)
ENV.pop("MEMORY_VECTOR_PORT", None)

results: list[dict] = []
_worker: subprocess.Popen | None = None


def data_dir() -> Path:
    cfg = json.loads(Path(CFG).read_text(encoding="utf-8"))
    return Path(cfg["storage"]["dataDir"])


def port_file() -> Path:
    return data_dir() / "server.port"


def start_host_worker() -> None:
    """自己持有一个 worker 子进程当「端点宿主」。

    为什么必须由脚本托管：Bash 工具在命令结束后会收割整棵进程树，
    CLI 自己 detached 拉起的 worker 活不过一次命令，导致 HTTP 路径根本
    测不到（2026-10-28 实测：port 文件反复消失其实是宿主被外部杀掉）。
    """
    global _worker
    err = open(data_dir() / "logs" / "worker_sweep.err", "wb")
    _worker = subprocess.Popen(
        [str(EXE), "worker", "--no-fast-exit", "--idle-exit", "1800",
         "--poll", "1.0", "--lanes", "1"],
        env=ENV, cwd=str(EXE.parent),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
    )
    deadline = time.time() + 40
    while time.time() < deadline:
        if port_file().exists():
            print(f"        宿主 worker pid={_worker.pid} 端口文件就绪：{port_file().read_text(encoding='utf-8')}")
            return
        time.sleep(0.3)
    print("        !! 宿主 worker 40s 未写出端口文件")


def stop_host_worker() -> None:
    global _worker
    if _worker and _worker.poll() is None:
        _worker.terminate()
        try:
            _worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _worker.kill()
    _worker = None


def run(name: str, argv: list[str], timeout: float = 180.0,
        expect_rc: int = 0, stdin_text: str | None = None) -> tuple[int, str, str, float]:
    t0 = time.time()
    try:
        p = subprocess.run(
            [str(EXE)] + argv, env=ENV, cwd=str(EXE.parent),
            input=(stdin_text.encode("utf-8") if stdin_text is not None else None),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
        rc, out, err = p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        rc, out, err = -9, "", f"TIMEOUT {timeout}s"
    dt = time.time() - t0
    rec = {"name": name, "argv": argv, "rc": rc, "ms": round(dt * 1000),
           "stdout": out[:4000], "stderr": err[:2000]}
    ok = rc == expect_rc
    rec["ok"] = ok
    results.append(rec)
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<26} rc={rc} {dt*1000:7.0f}ms")
    if not ok:
        for line in ((err or out).strip().splitlines() or ["(无输出)"])[-3:]:
            print("        |", line[:180])
    return rc, out, err, dt


def jload(out: str) -> dict:
    try:
        return json.loads(out.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {}


def queue_stats() -> dict:
    p = subprocess.run([str(EXE), "queue", "--json"], env=ENV, cwd=str(EXE.parent),
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    return jload(p.stdout.decode("utf-8", "replace")).get("stats") or {}


def wait_jobs(timeout: float = 300.0) -> dict:
    """等队列排空——pro 模式单条写入 25~65s，写完立刻检索必然 0 条。"""
    deadline = time.time() + timeout
    st: dict = {}
    while time.time() < deadline:
        st = queue_stats()
        if st.get("pending", 0) == 0 and st.get("running", 0) == 0:
            return st
        time.sleep(2)
    return st


print("\n== 起一个宿主 worker（脚本托管，避免沙箱收割）==")
start_host_worker()

# ---------------------------------------------------------------- 只读类
run("version --json", ["version", "--json"])
run("help --json", ["help", "--json"])
run("help search", ["help", "search"])
run("config show --json", ["config", "show", "--json"])
run("doctor --json", ["doctor", "--json"])
run("stats --json", ["stats", "--json"])
run("stats --health --json", ["stats", "--health", "--json"])
run("list -n 3 --json", ["list", "-n", "3", "--json"])
run("tags --json", ["tags", "--json"])
run("queue --json", ["queue", "--json"])
run("skill", ["skill"])

rc, out, _err, _dt = run("search 冷启动 --json", ["search", "胡超是谁", "-n", "3", "--json"])
hits = jload(out).get("results") or []
seed_id = hits[0].get("memory_id") if hits else None
print(f"        首次检索命中 {len(hits)} 条，seed_id={seed_id}")

if seed_id:
    run("get --json", ["get", seed_id, "--json"])
    run("history --json", ["history", seed_id, "--json"])

# ---------------------------------------------------------------- 写入链路
run("add --async --json", ["add", "--async", "冒烟巡检：hy-mem 回归测试写入的一条记忆，可安全删除。",
                           "--tag", TAG, "--json"])
rc, out, _e, _dt = run("queue 待处理 --json", ["queue", "--json"])
pend = jload(out).get("stats", {}).get("pending")
print(f"        队列 pending={pend}")
results[-1]["ok"] = results[-1]["ok"] and bool(pend)
print(f"        -> 入队后确有待处理 {'PASS' if results[-1]['ok'] else 'FAIL'}")

st = wait_jobs(300)
print(f"        等待落库结束：pending={st.get('pending')} running={st.get('running')} done={st.get('done')}")
run("worker --once --json", ["worker", "--once", "--json"], timeout=420)

rc, out, _e, _dt = run("search 新记忆 --json", ["search", "冒烟巡检", "-n", "5", "--json"])
new_hits = jload(out).get("results") or []
new_id = None
for h in new_hits:
    if TAG in (h.get("tags") or []):
        new_id = h.get("memory_id")
        break
if not new_id and new_hits:
    new_id = new_hits[0].get("memory_id")
print(f"        命中 {len(new_hits)} 条，new_id={new_id}")
results[-1]["ok"] = results[-1]["ok"] and bool(new_hits)
print(f"        -> 新记忆可检索 {'PASS' if results[-1]['ok'] else 'FAIL'}")

if new_id:
    run("tag 绑定", ["tag", new_id, "巡检二级"])
    run("get 看标签", ["get", new_id, "--json"])
    run("untag 解绑", ["untag", new_id, "巡检二级"])
    run("update --yes --json", ["update", new_id, "冒烟巡检：内容已更新（回归测试）。", "--yes", "--json"],
        timeout=300)
    run("search 更新后 --json", ["search", "内容已更新", "-n", "5", "--json"])

# ---------------------------------------------------------------- 端点生命周期（跨进程）
rc, out, _e, _dt = run("search 二次（新进程）", ["search", "胡超是谁", "-n", "3", "--json"])
print(f"        端口文件存在={port_file().exists()} 内容={port_file().read_text(encoding='utf-8') if port_file().exists() else '-'}")

print("\n== 杀掉宿主 worker，模拟空闲早退/崩溃 ==")
stop_host_worker()
time.sleep(1)
print(f"        端口文件残留={port_file().exists()}")
rc, out, err, dt = run("search 宿主死后（应自愈）", ["search", "胡超是谁", "-n", "3", "--json"], timeout=120)
print(f"        自愈结果 rc={rc} 耗时={dt:.1f}s 回落嵌入式={'回落嵌入式' in err}")
print(f"        端口文件重建={port_file().exists()}")
results[-1]["ok"] = results[-1]["ok"] and "回落嵌入式" not in err
print(f"        -> 自愈判定 {'PASS' if results[-1]['ok'] else 'FAIL'}")

# ---------------------------------------------------------------- 清理
if new_id:
    run("delete --yes --json", ["delete", new_id, "--yes", "--json"])
    run("get 已删（应失败）", ["get", new_id, "--json"], expect_rc=1)
run("drain --json", ["drain", "--timeout", "5", "--json"])
run("retry dead --json", ["retry", "dead", "--json"])

OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
failed = [r for r in results if not r["ok"]]
print(f"\n合计 {len(results)} 项，FAIL {len(failed)} 项；报告：{OUT}")
for r in failed:
    print("  FAIL:", r["name"], "|", (r["stderr"] or r["stdout"]).strip().splitlines()[-1][:160] if (r["stderr"] or r["stdout"]).strip() else r["rc"])
sys.exit(1 if failed else 0)
