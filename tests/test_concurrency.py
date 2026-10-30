# -*- coding: utf-8 -*-
"""并发压测：模拟多子代理同时读写 hy-mem，验证不卡死、不丢数据。

场景：多智能体框架下每个子代理各拉起自己的 CLI/MCP 进程，同时打同一份
数据目录。防护机制（store 层）：
- sqlite WAL + busy_timeout=30s：读读并行、写写排队；
- WriteLock 跨进程排他写锁（msvcrt/flock，180s 超时降级）；
- worker 心跳去重：并发 add --async 只拉起一个 worker。

用法：python tests/test_concurrency.py [exe路径]（缺省用源码口径）
判定：全部进程退出码 0、无锁超时、drain 后队列无死信 → PASS
隔离：临时沙箱数据目录（HY_MEM_CONFIG + MEMORY_DATA_DIR），绝不打真实库。
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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import _secrets  # noqa: E402  密钥只从环境变量取，见 scripts/_secrets.py


def _make_sandbox() -> tuple[Path, dict]:
    """临时数据目录 + 隔离 env。返回 (沙箱根, env)。"""
    sandbox = Path(tempfile.mkdtemp(prefix="hymem-conc-"))
    cfg = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
    cfg["storage"]["dataDir"] = str(sandbox / "data")
    cfg = _secrets.sandbox_config(cfg)   # 沙箱配置不留密钥，靠环境变量
    # 固定 pro：本套测的是「多进程并发写同一库」，而 ultra 的 kuzu 图库是独占锁，
    # 第二个进程必然 `GraphStore (kuzu) init failed`（2026-10-30 实测：2 进程并发
    # 1 成 1 败；8 进程风暴全灭）。并发能力本身就是 pro/lite 的语义，与用户真实
    # mode 无关——跟随真实 mode 会让本套在本地切 ultra 后必红。
    cfg["mode"] = "pro"
    (sandbox / "cfg.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    env = {
        **os.environ,
        "HY_MEM_CONFIG": str(sandbox / "cfg.json"),
        "HY_MEM_HOME": str(REPO),
        "MEMORY_DATA_DIR": str(sandbox / "data"),
        "NO_COLOR": "1",
    }
    return sandbox, env


def main() -> int:
    exe = sys.argv[1] if len(sys.argv) > 1 else None
    if not _secrets.have_all():
        print(f"跳过 test_concurrency：缺少真实密钥（{_secrets.describe()}）")
        print("  并发写入会真实调用模型，没有密钥跑出来的结论不可信。")
        return 0
    sandbox, ENV = _make_sandbox()

    def cli_base():
        return [exe] if exe else [PY, "-m", "hymem"]

    def mcp_base():
        return [exe, "mcp"] if exe else [PY, "-m", "hymem", "mcp"]

    t0 = time.time()
    procs: dict[str, subprocess.Popen] = {}

    # --- 4 个并发异步写（模拟 4 个子代理同时「记一下」）---
    # 文本必须是事实型表述：extractor 会把合成测试句合理判为「无可提取事实」，
    # 只落 L1 shadow 层 → 检索 0 命中（SDK 正常设计，见 test_mcp 同款注释）。
    for i in range(4):
        procs[f"add-{i}"] = subprocess.Popen(
            [*cli_base(), "add", "--async",
             f"用户偏好：hy-mem 并发压测第 {i} 条——多子代理同时写入时用 --async 入队，"
             f"队列由 SQLite 持久化，worker 3 车道消费（验收标记 conc-{i}）", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            env=ENV,
        )
    # --- 2 个 MCP 客户端进程，各做一轮 search+list（模拟并发检索）---
    mcp_msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "load", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "list_memories", "arguments": {"limit": 5}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "search_memory", "arguments": {"query": "hy-mem 并发压测 多子代理同时写入", "limit": 3}}},
    ]
    mcp_stdin = "\n".join(json.dumps(m, ensure_ascii=False) for m in mcp_msgs) + "\n"
    for i in range(2):
        procs[f"mcp-{i}"] = subprocess.Popen(
            mcp_base(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", env=ENV,
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

    # --- 等 detached worker 全部落库（add --async 会自动拉起 worker，10~50s/条）---
    deadline = time.time() + 300
    stats = {}
    while time.time() < deadline:
        q = subprocess.run([*cli_base(), "queue", "--json"], capture_output=True, text=True,
                           encoding="utf-8", timeout=60, env=ENV)
        try:
            stats = json.loads(q.stdout).get("stats", {})
        except json.JSONDecodeError:
            stats = {}
        print(f"  [wait] queue = {stats}")
        if stats.get("pending") == 0 and stats.get("running") == 0:
            break
        time.sleep(3)
    q = subprocess.run([*cli_base(), "queue", "--json"], capture_output=True, text=True,
                       encoding="utf-8", timeout=60, env=ENV)
    try:
        qd = json.loads(q.stdout)
        dead = qd.get("dead") or qd.get("deadLetter") or 0
    except json.JSONDecodeError:
        dead = "?"
    sr = subprocess.run([*cli_base(), "search", "hy-mem 并发压测 多子代理同时写入", "--json"],
                        capture_output=True, text=True, encoding="utf-8", timeout=120, env=ENV)
    hits = len(json.loads(sr.stdout).get("results", [])) if sr.returncode == 0 else -1

    print(f"\n队列死信: {dead} | 落库检索命中: {hits} 条（应≥1）")
    rc = 0
    if fails or dead not in (0, "?") or hits < 1:
        print("FAIL")
        for name, rc_, e, o in fails:
            print(f"  {name} rc={rc_}\n    err: {e}\n    out: {o}")
        rc = 1
    else:
        print(f"PASS：{len(procs)} 进程并发，全部正常，总耗时 {time.time()-t0:.1f}s")

    # --- R2/R3/R4 回归：并发 sync add 风暴——失败形态只许干净失败 ---
    # readonly 裸崩 / 其他非零退出都算回归（§0.B2）。4 轮 × 2 进程。
    print("\n并发 sync add 风暴（4 轮 × 2 进程，失败形态分类）")
    storm_ok = storm_lock = storm_busy = storm_extract = 0
    for r in range(4):
        sps = [subprocess.Popen(
            [*cli_base(), "--json", "add", f"并发sync风暴-r{r}-p{i}：失败形态分类取证", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=ENV,
        ) for i in range(2)]
        for i, p in enumerate(sps):
            out, err = p.communicate(timeout=240)
            if p.returncode == 0:
                storm_ok += 1
                continue
            code = ""
            try:
                code = (json.loads((out or "").strip().splitlines()[-1]).get("error") or {}).get("code", "")
            except Exception:  # noqa: BLE001
                code = ""
            if code in ("E_LOCK", "E_DB_BUSY", "E_EXTRACT"):
                # 允许的干净失败形态：E_LOCK=锁快失败（R3）；E_DB_BUSY=瞬态
                # 重试耗尽后的结构化失败（R2.3）；E_EXTRACT=LLM 网关空响应
                # 重试耗尽（R4.9，锁外抽取段、零写入可重试）。
                if code == "E_LOCK":
                    storm_lock += 1
                elif code == "E_EXTRACT":
                    storm_extract += 1
                else:
                    storm_busy += 1
            else:
                fails.append((f"storm-r{r}-p{i}", p.returncode, code, (err or "")[-150:]))
    print(f"  storm: ok={storm_ok} e_lock={storm_lock} e_db_busy={storm_busy} e_extract={storm_extract} bad={len([f for f in fails if f[0].startswith('storm')])}")
    if [f for f in fails if f[0].startswith("storm")]:
        rc = 1
        print("FAIL：风暴出现裸崩/未分类失败（见上方明细）")
    elif rc == 0:
        print(f"PASS：风暴 8 进程无裸崩（ok={storm_ok}，E_LOCK={storm_lock}，E_DB_BUSY={storm_busy}，E_EXTRACT={storm_extract}）")

    # --- 清理沙箱（Windows 上 WAL 句柄可能延迟释放，失败不阻塞判定）---
    try:
        import shutil

        shutil.rmtree(sandbox, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
