# -*- coding: utf-8 -*-
"""队列消费核心逻辑测试（纯 sqlite 层，零网络、零 LLM，秒级）。

覆盖 0.9.8 的两处结构性改动：
1. 延迟重试队列：jobs.not_before 列 + 旧库幂等迁移 + claim 只领到期任务
   + mark_failed 退避曲线 + retry/requeue_stale 清 not_before。
2. 拉起闸门：detached supervisor 恒 1 个（cap=1 串行化）。

跑法：.venv/Scripts/python.exe tests/test_queue.py
隔离：沙箱 MEMORY_DATA_DIR，绝不碰真实队列库。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(bool(passed))
    mark = "PASS" if passed else "FAIL"
    print(f"  {mark}  {name}" + (f"  → {detail}" if detail else ""))


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="hymem-queue-"))
    os.environ["MEMORY_DATA_DIR"] = str(sandbox)
    os.environ.pop("HY_MEM_CONFIG", None)

    from hymem.core import store

    conn = store.connect()

    # ---- 1. 旧库幂等迁移：无 not_before 列的库 connect 后自动补列 ----
    legacy = sandbox / "legacy.db"
    lc = sqlite3.connect(str(legacy))
    lc.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL DEFAULT 'add',"
        " payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
        " max_attempts INTEGER NOT NULL DEFAULT 3, mode TEXT, error TEXT, memory_ids TEXT,"
        " created_at REAL NOT NULL, started_at REAL, finished_at REAL, worker TEXT);"
    )
    lc.commit()
    lc.close()
    os.environ["MEMORY_DATA_DIR"] = str(sandbox / "legacy")  # 指向只有 legacy.db 的目录？不——直接对原库降级测试
    os.environ["MEMORY_DATA_DIR"] = str(sandbox)

    # 直接对当前库做降级模拟：删表重建为旧 schema，再 connect 看迁移
    conn.close()
    raw = sqlite3.connect(str(store.db_path()))
    raw.execute("DROP TABLE jobs")
    raw.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL DEFAULT 'add',"
        " payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
        " max_attempts INTEGER NOT NULL DEFAULT 3, mode TEXT, error TEXT, memory_ids TEXT,"
        " created_at REAL NOT NULL, started_at REAL, finished_at REAL, worker TEXT);"
    )
    raw.commit()
    raw.close()
    conn = store.connect()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    check("旧库迁移：connect 后自动补 not_before 列", "not_before" in cols, f"cols={sorted(cols)}")
    # 再 connect 一次不报 duplicate column（幂等）
    conn2 = store.connect()
    conn2.close()
    check("迁移幂等：二次 connect 不报错", True)

    # ---- 2. claim_next 原子性：两个连接抢同一行，只有一个领到 ----
    jid = store.enqueue(conn, {"text": "job-A"}, max_attempts=3)
    other = store.connect()
    r1 = store.claim_next(conn, "w1")
    r2 = store.claim_next(other, "w2")
    got = (r1 is not None) + (r2 is not None)
    check("claim 原子性：同一任务只被领一次", got == 1, f"claimed={got}")
    if r1 is not None:
        store.mark_done(conn, r1["id"], ["m1"])
    other.close()

    # ---- 3. 退避曲线：mark_failed 写 not_before，claim 跳过未到期任务 ----
    jid = store.enqueue(conn, {"text": "flaky"}, max_attempts=3)
    row = store.claim_next(conn, "w1")
    store.mark_failed(conn, row["id"], "boom", retry=True)
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
    d1 = j["not_before"] - time.time()
    check("首次失败：not_before ≈ +2s", 1.0 < d1 <= 2.5, f"delay={d1:.2f}s")
    check("未到期任务 claim 不到", store.claim_next(conn, "w1") is None)
    check("queue_stats.delayed 计数=1", store.queue_stats(conn)["delayed"] == 1)
    # 人为把 not_before 挪到过去 → 可领
    conn.execute("UPDATE jobs SET not_before=? WHERE id=?", (time.time() - 1, row["id"]))
    row2 = store.claim_next(conn, "w1")
    check("到期后任务可领", row2 is not None and row2["id"] == row["id"] and row2["attempts"] == 2)
    store.mark_failed(conn, row["id"], "boom", retry=True)
    d2 = conn.execute("SELECT not_before FROM jobs WHERE id=?", (row["id"],)).fetchone()["not_before"] - time.time()
    check("二次失败退避翻倍（≈+4s）", 3.0 < d2 <= 4.5, f"delay={d2:.2f}s")

    # ---- 4. 重试预算耗尽 → dead；retry 重投清 not_before ----
    conn.execute("UPDATE jobs SET not_before=? WHERE id=?", (time.time() - 1, row["id"]))
    row3 = store.claim_next(conn, "w1")  # attempts=3
    check("第三次可领（attempts=3）", row3 is not None and row3["attempts"] == 3)
    state = store.mark_failed(conn, row3["id"], "boom", retry=True)
    check("attempts 耗尽进死信", state == "dead" and store.queue_stats(conn)["dead"] == 1)
    # 死信在到期窗口外也领不到（状态是 dead）
    check("死信不可领", store.claim_next(conn, "w1") is None)
    # 模拟 hy-mem retry dead：清 not_before
    cur = conn.execute(
        "UPDATE jobs SET status='pending', attempts=0, error=NULL, started_at=NULL, worker=NULL, "
        "not_before=NULL WHERE status='dead'"
    )
    r4 = store.claim_next(conn, "w1")
    check("retry 重投后立即可领", cur.rowcount == 1 and r4 is not None and r4["attempts"] == 1)

    # ---- 5. requeue_stale：running→pending 且清 not_before ----
    store.mark_done(conn, r4["id"], ["m2"])
    jid = store.enqueue(conn, {"text": "stale-running"}, max_attempts=3)
    row = store.claim_next(conn, "w1")
    conn.execute("UPDATE jobs SET not_before=?, started_at=? WHERE id=?",
                 (time.time() + 999, time.time() - 1000, row["id"]))
    n = store.requeue_stale(conn, older_than=900)
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
    check("requeue_stale 回收并清 not_before", n == 1 and j["status"] == "pending" and j["not_before"] is None)
    r5 = store.claim_next(conn, "w1")
    check("回收后立即可领", r5 is not None and r5["id"] == row["id"])
    store.mark_done(conn, row["id"], ["m3"])

    # ---- 6. 拉起闸门：cap=1 串行化（WriteLock 外调用，但只测行级逻辑）----
    conn.execute("DELETE FROM worker_reg")
    ok1 = store.claim_worker_slot(conn, 1)
    ok2 = store.claim_worker_slot(conn, 1)  # 占位行已在，第二把闸门应拒绝
    check("spawn 闸门 cap=1：首个放行、第二个拒绝", ok1 is True and ok2 is False)
    # worker 真实注册后闸门仍拒绝；leave 后放行
    store.heartbeat(conn, "host:123")
    ok3 = store.claim_worker_slot(conn, 1)
    check("存活 supervisor 注册后闸门拒绝", ok3 is False)
    store.worker_leave(conn, "host:123")
    conn.execute("DELETE FROM worker_reg WHERE id='_spawn_intent'")
    ok4 = store.claim_worker_slot(conn, 1)
    check("leave 后闸门放行", ok4 is True)

    # ---- 7. queue_stats delayed 与 pending 口径 ----
    st = store.queue_stats(conn)
    check("stats 口径：delayed≤pending，无死信残留", st["delayed"] <= st["pending"] and st["dead"] == 0,
          f"stats={st}")

    # ---- 8. MCP queue_status 的 shadow_only hint（2026-10-29 实测事故回归）----
    # 现场：job 87 状态 done、error 标 shadow_only（只落 l1_raw 影子、检索不到），
    # hint 却说「已全部落库，可以 search_memory 验证内容」。done ≠ 可检索，
    # shadow 提示必须压过一切「可检索」话术。
    from hymem.mcp import server as mcp_server

    conn.execute(
        "INSERT INTO jobs (id, status, attempts, max_attempts, payload, memory_ids, error, created_at) "
        "VALUES (9001, 'done', 1, 3, ?, '[]', ?, 0)",
        (json.dumps({"text": "影子任务"}), "shadow_only: 抽取未产出可检索节点"),
    )
    conn.commit()
    qs = json.loads(mcp_server._tool_queue_status({"job_ids": [9001]}))
    check("shadow_only job 的 hint 说「检索不到」并给 retry --shadow",
          ("检索不到" in qs["hint"]) and ("retry --shadow" in qs["hint"]), qs["hint"][:120])
    check("shadow 提示压过「可以 search 验证」话术",
          "可以 search_memory 验证" not in qs["hint"], qs["hint"][:120])
    # 对照：正常 done（无 error）仍走「可以验证」话术
    conn.execute(
        "INSERT INTO jobs (id, status, attempts, max_attempts, payload, memory_ids, error, created_at) "
        "VALUES (9002, 'done', 1, 3, ?, '[]', NULL, 0)",
        (json.dumps({"text": "正常任务"}),),
    )
    conn.commit()
    qs2 = json.loads(mcp_server._tool_queue_status({"job_ids": [9002]}))
    check("正常 done 仍提示可 search 验证", "可以 search_memory 验证" in qs2["hint"], qs2["hint"][:120])

    conn.close()
    n_pass = sum(results)
    print(f"\n{'全部通过' if n_pass == len(results) else '存在失败'}（{n_pass}/{len(results)}）")
    import shutil

    shutil.rmtree(sandbox, ignore_errors=True)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
