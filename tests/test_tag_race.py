# -*- coding: utf-8 -*-
"""并发写入时标签归属：两个 add 交错，标签只能落在各自的节点上。

为什么单独立一条：`_bind_tags_to_new` 用「按 user 全局 before/after 差分」判定
本次新节点。两个写者交错时，后完成者的 after 快照会包含对方的节点，
标签就被错绑到别人的记忆上——不损坏数据，但 `_filter_by_tags` 从此筛不到自己的那条。

这里用真线程 + 真 SQLite，不用 mock 模拟时序：两个线程在 extract 阶段用
barrier 对齐（此刻两侧的 before 快照都已完成），随后各自提交一个节点。
这正是 worker 多 lane 下「两个带标签的 job 同时开始」的真实交错形态。
"""
import json
import os
import tempfile
import threading
from pathlib import Path

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    print(("  PASS  " if passed else "  FAIL  ") + name + (f"  → {detail}" if detail else ""))


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="hymem-tagrace-"))
    os.environ["MEMORY_DATA_DIR"] = str(sandbox)
    os.environ.pop("HY_MEM_CONFIG", None)

    from hymem.core import store
    from hymem.cmds import queue as queue_cmd

    conn = store.connect()

    class _Vec:
        """模拟向量库（只存 id），线程安全。"""

        def __init__(self):
            self._lock = threading.Lock()
            self.ids: list[str] = []

        def add(self, mid: str) -> None:
            with self._lock:
                self.ids.append(mid)

        def page(self, offset: int) -> dict:
            with self._lock:
                ids = list(self.ids)
            return {"vdb": {"memories": [{"memory_id": i} for i in ids[offset:offset + 200]],
                            "total": len(ids)}}

    class _Client:
        _mode = "pro"

        def __init__(self, vec: _Vec, mid: str, barrier: threading.Barrier):
            self.vec, self.mid, self.barrier = vec, mid, barrier

        def list_memories(self, limit: int = 200, offset: int = 0, **kw):
            return self.vec.page(offset)

        def close(self):
            pass

    real_x, real_p, real_c = (queue_cmd.extract_phase, queue_cmd.prepare_commit,
                              queue_cmd.commit_extracted_with_retry)
    rounds_bad: list[str] = []
    ROUNDS = 5

    try:
        for r in range(1, ROUNDS + 1):
            vec = _Vec()
            barrier = threading.Barrier(2)
            mid_a, mid_b = f"r{r}-a", f"r{r}-b"
            clients = {1: _Client(vec, mid_a, barrier), 2: _Client(vec, mid_b, barrier)}
            rows = {
                1: {"id": r * 10 + 1, "kind": "add", "mode": "pro",
                    "payload": json.dumps({"text": "A", "identity": {"user_id": "u1"},
                                           "tags": ["alpha"]}, ensure_ascii=False)},
                2: {"id": r * 10 + 2, "kind": "add", "mode": "pro",
                    "payload": json.dumps({"text": "B", "identity": {"user_id": "u1"},
                                           "tags": ["beta"]}, ensure_ascii=False)},
            }

            def fake_extract(client, text, **kw):
                # 两侧 before 快照都已完成，再一起进入提交段
                barrier.wait(timeout=10)
                return {"text": text}

            def fake_prepare(client, text, agent_result, **kw):
                return {"text": text}

            def fake_commit(client, text, agent_result, *, prepared=None,
                            attempts=4, **kw):
                vec.add(client.mid)
                return {"success": True, "memory_ids": [client.mid]}

            queue_cmd.extract_phase = fake_extract
            queue_cmd.prepare_commit = fake_prepare
            queue_cmd.commit_extracted_with_retry = fake_commit

            errs: list[str] = []
            bound: dict[int, list[str]] = {}

            def run(jid: int) -> None:
                try:
                    _r, ids, _n = queue_cmd._execute_job(
                        conn, rows[jid], lambda mode, c=clients[jid]: c)
                    bound[jid] = ids
                except Exception as e:  # noqa: BLE001
                    errs.append(f"job{jid}: {type(e).__name__}: {e}")

            ts = [threading.Thread(target=run, args=(i,)) for i in (1, 2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=30)
                assert not t.is_alive(), "线程超时（barrier 死锁？）"

            tag_map = store.tags_for(conn, [mid_a, mid_b])
            got_a = set(tag_map.get(mid_a) or [])
            got_b = set(tag_map.get(mid_b) or [])
            if got_a != {"alpha"} or got_b != {"beta"}:
                rounds_bad.append(
                    f"round{r}: a_tags={sorted(got_a)} b_tags={sorted(got_b)} "
                    f"bound={bound} errs={errs}")
    finally:
        queue_cmd.extract_phase, queue_cmd.prepare_commit, \
            queue_cmd.commit_extracted_with_retry = real_x, real_p, real_c

    check(f"{ROUNDS} 轮并发 add：标签只落在各自节点（不串绑）",
          not rounds_bad, "; ".join(rounds_bad) if rounds_bad else "全部干净")

    conn.close()
    bad = results.count(False)
    print("\n全部通过" if bad == 0 else f"\n失败 {bad} 项")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
