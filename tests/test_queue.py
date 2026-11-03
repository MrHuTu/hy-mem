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

    # ---- 9. P0-1 批路径语义：带 tags 不入批；失败码/空 ids 不被绕过 ----
    print("\nP0-1 批处理语义")

    from hymem.cmds import queue as queue_cmd
    from hymem.core import two_phase

    def _row(jid, text, **extra):
        return {
            "id": jid, "kind": "add", "mode": "pro",
            "payload": json.dumps({"text": text, "identity": {"user_id": "u1"}, **extra},
                                  ensure_ascii=False),
        }

    check("带 tags 的 add 不入批（回退单条路径，标签不丢）",
          queue_cmd._batch_compat(_row(1, "带标签", tags=["proj:x"])) is False)
    check("无显式字段的 add 仍可入批",
          queue_cmd._batch_compat(_row(2, "普通")) is True)

    results_by_text = {
        "ok": {"success": True, "memory_id": "l1-ok", "memory_ids": ["m-ok"]},
        "sdk-fail": {"success": False, "error_message": "boom",
                     "memory_id": "l1-f", "memory_ids": ["m-f"]},
        "agent-fail": {"success": True, "agent_error_code": "EMPTY_RESPONSE",
                       "agent_error": "空响应", "memory_id": "l1-a", "memory_ids": ["m-a"]},
        "no-ids": {"success": True, "memory_ids": [], "memory_id": None},
    }
    real = (two_phase.extract_phase, two_phase.prepare_commit, two_phase.batch_commit)

    def fake_extract(client, text, **kw):
        return {"text": text}

    def fake_prepare(client, text, ar, **kw):
        return {"text": text}

    def fake_batch(client, jobs):
        return [dict(results_by_text[j["text"]]) for j in jobs]

    two_phase.extract_phase, two_phase.prepare_commit, two_phase.batch_commit = (
        fake_extract, fake_prepare, fake_batch,
    )
    try:
        rows = [_row(11, "ok"), _row(12, "sdk-fail"), _row(13, "agent-fail"), _row(14, "no-ids")]
        outcomes = queue_cmd._execute_batch(rows, lambda mode: object())
    finally:
        two_phase.extract_phase, two_phase.prepare_commit, two_phase.batch_commit = real
    by_id = {job_id: exc for job_id, _r, _ids, _note, exc in outcomes}
    check("批路径 success=False 记为失败（不标 done）", by_id.get(12) is not None, str(by_id.get(12)))
    check("批路径 agent_error_code 记为失败", by_id.get(13) is not None, str(by_id.get(13)))
    check("批路径空 ids 记为失败（防抽取失败被吞）", by_id.get(14) is not None, str(by_id.get(14)))
    check("批路径正常任务仍成功", by_id.get(11) is None)

    # ---- 10. P1-2 快照完整性：不完整/失败时禁止打标签 ----
    print("\nP1-2 标签快照完整性")
    from hymem.backends.hymemory import searchops as so

    def _page(ids):
        return {"vdb": {"memories": [{"memory_id": i} for i in ids]}}

    class _PagedClient:
        def __init__(self, pages):
            self.pages = list(pages)
            self.calls = 0

        def list_memories(self, limit=200, offset=0, **kw):
            if self.calls >= len(self.pages):
                raise RuntimeError("listing failed")
            page = self.pages[self.calls]
            self.calls += 1
            return page

    full = _page([f"i{n}" for n in range(200)])
    ids, complete = so._snapshot_ids(_PagedClient([full]), {"user_id": "u"}, cap=200)
    check("快照达到上限时 complete=False", complete is False and len(ids) == 200, f"{complete}|{len(ids)}")
    ids, complete = so._snapshot_ids(_PagedClient([_page(["a", "b"]), _page([])]), {"user_id": "u"})
    check("完整分页解包 vdb 且 complete=True", complete is True and ids == {"a", "b"}, f"{complete}|{ids}")
    ids, complete = so._snapshot_ids(_PagedClient([_page(["a"])]), {"user_id": "u"})
    check("中途抛错保留部分 id 且 complete=False（不再静默清空）",
          complete is False and ids == {"a"}, f"{complete}|{ids}")

    bound: list[list[str]] = []
    real_bind = so._bind_tags
    so._bind_tags = lambda ids, tags, source="user": bound.append(list(ids)) or len(ids)
    try:
        _t, note = so._bind_tags_to_new(
            _PagedClient([_page(["old"])]), {"user_id": "u"},
            before={"old"}, returned=["new"], tags=["t"], before_complete=False,
        )
        check("写入前快照不完整：跳过绑定并写 note",
              bound == [] and "不完整" in note, f"{bound}|{note}")
        _t, note = so._bind_tags_to_new(
            _PagedClient([_page(["old"])]), {"user_id": "u"},
            before={"old"}, returned=["new"], tags=["t"],
        )
        check("写入后快照不完整：跳过绑定并写 note",
              bound == [] and "不完整" in note, f"{bound}|{note}")
        _t, note = so._bind_tags_to_new(
            _PagedClient([_page(["old"]), _page(["old", "new"]), _page([])]), {"user_id": "u"},
            before={"old"}, returned=["new"], tags=["t"],
        )
        check("完整快照只给新节点打标签", bound[-1] == ["new"] and note == "", f"{bound}|{note}")
    finally:
        so._bind_tags = real_bind

    # worker 调用方：快照失败时同样必须跳过
    wclient = type("WC", (), {"_mode": "lite"})()
    wclient.add = lambda text, **kw: {"success": True, "memory_id": "l1w", "memory_ids": ["neww"]}
    wclient.list_memories = lambda **kw: (_ for _ in ()).throw(RuntimeError("list boom"))
    wrow = {"id": 90, "kind": "add", "mode": "lite",
            "payload": json.dumps({"text": "x", "identity": {"user_id": "u"}, "tags": ["tw"]},
                                  ensure_ascii=False)}
    worker_bound: list[list[str]] = []
    so._bind_tags = lambda ids, tags, source="user": worker_bound.append(list(ids)) or len(ids)
    try:
        _r, _ids, wnote = queue_cmd._execute_job(conn, wrow, lambda mode: wclient)
    finally:
        so._bind_tags = real_bind
    check("worker 快照失败时跳过标签绑定并写 note",
          worker_bound == [] and "未绑定" in (wnote or ""), f"{worker_bound}|{wnote}")

    # ---- 11. P1-3 lane 领取/统计异常必须可诊断且不静默退出 0 ----
    print("\nP1-3 lane 异常可诊断")
    import types

    captured: dict = {}
    real_ok = queue_cmd.ok

    def _cap_ok(obj, as_json, human=None):
        if isinstance(obj, dict):
            captured.update(obj)
        return real_ok(obj, as_json, human)

    queue_cmd.ok = _cap_ok
    real_require = queue_cmd.require_ready
    queue_cmd.require_ready = lambda *a, **k: None
    real_claim_next, real_claim_batch = store.claim_next, store.claim_next_batch

    def _boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    store.claim_next, store.claim_next_batch = _boom, _boom
    try:
        wargs = types.SimpleNamespace(
            lanes=1, no_recover=True, once=True, idle_exit=0, timeout=0,
            max=0, poll=0.01, json=True, no_fast_exit=True,
        )
        wrc = queue_cmd.cmd_worker(wargs)
    finally:
        store.claim_next, store.claim_next_batch = real_claim_next, real_claim_batch
        queue_cmd.require_ready = real_require
        queue_cmd.ok = real_ok
    check("lane 领取异常时 worker 非零退出", wrc != 0, f"rc={wrc}")
    check("输出含 lane_errors 诊断",
          any("database is locked" in str(e) for e in (captured.get("lane_errors") or [])),
          str(captured.get("lane_errors")))
    check("输出标记 ok=False", captured.get("ok") is False, str(captured.get("ok")))

    # ---- 12. P1-4 完成态保留 / shadow_only 显式列 / stats 复用 ----
    print("\nP1-4 队列保留与读取放大")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    check("jobs 有显式 shadow_only 列", "shadow_only" in cols, f"cols={sorted(cols)}")

    conn.execute("DELETE FROM jobs")
    now = time.time()

    def _ins_job(status: str, age_days: float, error: str | None = None) -> int:
        cur = conn.execute(
            "INSERT INTO jobs (kind,payload,status,attempts,max_attempts,error,created_at,finished_at)"
            " VALUES ('add','{}',?,1,3,?,?,?)",
            (status, error, now - age_days * 86400, now - age_days * 86400),
        )
        return int(cur.lastrowid)

    old_done = _ins_job("done", 40)
    kept_done = _ins_job("done", 2)
    old_dead = _ins_job("dead", 400, "LLM 持续失败")
    old_pending = _ins_job("pending", 40)
    purge = getattr(store, "purge_finished_jobs", None)
    if purge is None:
        check("完成态保留策略函数存在", False, "缺少 purge_finished_jobs")
    else:
        purge(conn, keep_days=30)
        left = {r["id"] for r in conn.execute("SELECT id FROM jobs").fetchall()}
        check("超期 done 被清理", old_done not in left, f"left={sorted(left)}")
        check("保留期内 done 保留", kept_done in left, f"left={sorted(left)}")
        check("dead（失败）永不清理", old_dead in left, f"left={sorted(left)}")
        check("pending 不受保留策略影响", old_pending in left, f"left={sorted(left)}")

    if "shadow_only" in cols:
        conn.execute("DELETE FROM jobs")
        sid = store.enqueue(conn, {"text": "shadow"})
        store.claim_next(conn, "w-shadow")
        store.mark_done(conn, sid, [], extra={"note": "shadow_only: 只落影子节点"})
        hit = conn.execute("SELECT shadow_only FROM jobs WHERE id=?", (sid,)).fetchone()
        check("mark_done 的 shadow_only note 写进显式列",
              hit and hit["shadow_only"] == 1, f"{dict(hit) if hit else None}")
        check("queue_stats 的 shadow_only 来自显式列",
              store.queue_stats(conn)["shadow_only"] == 1, str(store.queue_stats(conn)["shadow_only"]))
        check("requeue_shadow 按显式列重投", store.requeue_shadow(conn) == 1)
        requeued = conn.execute("SELECT status, shadow_only FROM jobs WHERE id=?", (sid,)).fetchone()
        check("重投后清掉 shadow_only 标记",
              requeued and requeued["status"] == "pending" and requeued["shadow_only"] == 0,
              f"{dict(requeued) if requeued else None}")
        # 仅有旧式 error 文本、列=0 的行不该再被统计/重投
        legacy = _ins_job("done", 0)
        conn.execute("UPDATE jobs SET error='shadow_only: legacy text' WHERE id=?", (legacy,))
        check("error 文本不再影响 shadow_only 统计",
              store.queue_stats(conn)["shadow_only"] == 0, str(store.queue_stats(conn)["shadow_only"]))
        check("requeue_shadow 不再按 error LIKE 重投", store.requeue_shadow(conn) == 0)

    conn.execute("DELETE FROM jobs")
    for _ in range(3):
        store.enqueue(conn, {"text": "stats-hint"})
    try:
        r0 = store.claim_next_batch(conn, "w-hint", limit=4, stats={"pending": 0})
        check("stats.pending=0 时批领取直接空返回", r0 == [], f"got {len(r0)}")
        r2 = store.claim_next_batch(conn, "w-hint", limit=4, stats={"pending": 2})
        check("stats.pending 作为批上限", len(r2) == 2, f"got {len(r2)}")
    except TypeError as e:
        check("claim_next_batch 接受 stats 参数", False, f"{type(e).__name__}: {e}")

    # worker 每轮只查一次 stats：1 条任务的一轮消费 + 收工，旧实现 4 次，新实现 3 次
    conn.execute("DELETE FROM jobs")
    store.enqueue(conn, {"text": "one"})
    real_stats = store.queue_stats
    calls = {"n": 0}

    def _count_stats(c):
        calls["n"] += 1
        return real_stats(c)

    store.queue_stats = _count_stats
    real_exec = queue_cmd._execute_job
    real_require = queue_cmd.require_ready
    queue_cmd._execute_job = lambda c, r, g: ({}, ["m"], None)
    queue_cmd.require_ready = lambda *a, **k: None
    try:
        wargs = types.SimpleNamespace(
            lanes=1, no_recover=True, once=True, idle_exit=0, timeout=0,
            max=0, poll=0.01, json=True, no_fast_exit=True,
        )
        queue_cmd.cmd_worker(wargs)
    finally:
        store.queue_stats = real_stats
        queue_cmd._execute_job = real_exec
        queue_cmd.require_ready = real_require
    check("一轮消费+收工只读 3 次 stats（旧实现 4 次）", calls["n"] == 3, f"calls={calls['n']}")

    conn.close()
    n_pass = sum(results)
    print(f"\n{'全部通过' if n_pass == len(results) else '存在失败'}（{n_pass}/{len(results)}）")
    import shutil

    shutil.rmtree(sandbox, ignore_errors=True)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
