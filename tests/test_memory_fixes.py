"""2026-11-06 回归：新增不可检索 / 误合并 / 删除假失败 / 标签过滤 / 健康口径。

五项机制各自一条最小用例，全部跑在 fake client + 临时数据目录上，
不连 LLM、不碰真实记忆库：

  P1 reconcile 空操作或幻觉 id → 必须直存兜底，回执不得是空 memory_ids；
  P2 相似度闸门：与既有记忆不相关（<0.85）的新事实不经 reconcile 合并；
  P3 图库被锁（graph_unavailable）不得把已生效的 VDB 删除报成失败；
  P4 search --tag 必须多轮补取候选；update_memory 支持整组替换标签；
  P5 写入延迟取 jobs 实际耗时中位数；shadow 计数按节点/任务分开；
     扫描分页按 id 去重。

跑法：
    .venv/Scripts/python.exe tests/test_memory_fixes.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(bool(passed))
    print(("  PASS  " if passed else "  FAIL  ") + name + (f"  → {detail}" if detail else ""))


class _Loop:
    """把协程跑完（SDK 的 _loop_thread.run 语义的最小替身）。"""

    @staticmethod
    def run(coro):
        return asyncio.run(coro)


class FakeVS:
    """最小向量库：可配 top1 相似度、落库 id、id 复核结果。"""

    def __init__(self, *, best_score=0.0, stored=None, exists=None):
        self.best_score = best_score
        self.stored = list(stored or [])
        self.exists = set(exists or [])
        self.upserts: list = []
        self.search_calls: list[dict] = []

    async def search(self, query_embedding, **kw):
        self.search_calls.append(kw)
        return [{"node_id": "old", "score": self.best_score}] if self.best_score else []

    async def upsert(self, node):
        self.upserts.append(node)
        return "l1-1"

    async def get_by_ids(self, ids):
        return [SimpleNamespace(node_id=i) for i in ids if i in self.exists]


class FakeWriter:
    def __init__(self, vs, *, reconcile_result=None, direct_result=None):
        self._vs = vs
        self.reconcile_result = reconcile_result or ([], None, {}, {})
        self.direct_result = direct_result or (["new1"], None, {"add": 1, "total": 1}, {})
        self.reconcile_calls: list[list[str]] = []
        self.direct_calls: list[list[dict]] = []
        self.embed_service = SimpleNamespace(
            embed_batch=self._embed_batch,
            embed_queued_with_meta=self._embed_one,
        )
        self.config = SimpleNamespace(
            llm=SimpleNamespace(enable_intention=False),
            basic_profile=SimpleNamespace(effective_fields=lambda: {}),
        )

    @staticmethod
    async def _embed_batch(texts):
        return [[0.1, 0.2] for _ in texts]

    @staticmethod
    async def _embed_one(text):
        return [0.1, 0.2], {}

    async def _get_vector_store(self):
        return self._vs

    async def _reconcile_and_store(self, new_memory_texts, **kw):
        self.reconcile_calls.append(list(new_memory_texts))
        return self.reconcile_result

    async def _direct_store(self, new_memories_meta, **kw):
        self.direct_calls.append(list(new_memories_meta))
        return self.direct_result

    def _collect_intentions(self, info):
        return []

    def _remember_existing_tags(self, *a, **k):
        return None


class FakeClient:
    def __init__(self, writer):
        self._loop_thread = _Loop()
        self._registry = SimpleNamespace(get_writer=self._get_writer)
        self._writer = writer

    async def _get_writer(self):
        return self._writer


def _prepared(texts, *, with_embedding=True):
    from hy_memory.pipelines.base import WriteRequest

    wr = WriteRequest(
        content="t", user_id="u1", agent_id="a1", source="cli",
        session_id="s1", content_type="text",
    )
    return {
        "wr": wr,
        "request_id": "r1",
        "l1_embedding": [0.1, 0.2] if with_embedding else None,
        "basic_info": None,
        "info": {},
        "new_texts": list(texts),
        "new_metas": [{"text": t} for t in texts],
        "precomputed": {t: [0.1, 0.2] for t in texts} if with_embedding else {},
    }


def _agent_result():
    return SimpleNamespace(extracted_info={})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hymem-fixes-"))
    os.environ["MEMORY_DATA_DIR"] = str(tmp / "data")
    os.environ.pop("HY_MEM_RECONCILE_MIN_SIM", None)

    from hymem.core import store
    from hymem.backends.hymemory import two_phase as tp

    # ---- P1/P2：commit 的闸门、幻觉 id 复核、空操作兜底 ----
    print("\n[P1/P2] reconcile 空操作 / 幻觉 id / 相似度闸门")

    def _commit(vs, writer, texts):
        client = FakeClient(writer)
        return tp.commit_extracted(
            client, "原始对话", _agent_result(), prepared=_prepared(texts)
        )

    # P1-a: reconcile 返回空 ops（日志实测 parsed="[]"）→ 直存兜底
    vs = FakeVS(best_score=0.95, exists={"new1"})
    w = FakeWriter(vs)
    r = _commit(vs, w, ["会话超时设为 30 分钟"])
    check("P1a reconcile 空操作时回退直存（不再只剩影子）",
          r["memory_ids"] == ["new1"] and r["stored_via"] == "direct_fallback",
          f"ids={r['memory_ids']} via={r['stored_via']}")
    check("P1b 回执带明确 warning，调用方不会误以为零写入",
          bool(r.get("warning")) and "direct_fallback" in r["warning"], str(r.get("warning")))

    # P1-c: reconcile 返回库里不存在的 id（日志 12c8a2e1…）→ 剔除并兜底
    vs = FakeVS(best_score=0.95, exists={"n1"})
    w = FakeWriter(
        vs,
        reconcile_result=(["ghost-id"], None, {"update": 1}, {}),
        direct_result=(["n1"], None, {"add": 1, "total": 1}, {}),
    )
    r = _commit(vs, w, ["1.1.8 命名规范"])
    check("P1c 幻觉 id 被剔除且回退直存",
          r["reconcile_rejected_ids"] == ["ghost-id"] and r["memory_ids"] == ["n1"],
          f"rejected={r['reconcile_rejected_ids']} ids={r['memory_ids']}")
    check("P1d 幻觉 id 不算「并入既有」（merged_existing=false）",
          r["merged_existing"] is False, str(r["merged_existing"]))

    # P2: 相似度 0.60（日志实测误合并区间）→ 不送 reconcile，直接 ADD
    vs = FakeVS(best_score=0.60, exists={"d1"})
    w = FakeWriter(vs, direct_result=(["d1"], None, {"add": 1, "total": 1}, {}))
    r = _commit(vs, w, ["1.1.8 命名相关的新条目"])
    check("P2a 相似度低于闸门（0.60<0.85）不进入 reconcile",
          w.reconcile_calls == [] and w.direct_calls, f"reconcile={w.reconcile_calls}")
    check("P2b 该事实走直存并如实标记 stored_via=direct",
          r["memory_ids"] == ["d1"] and r["stored_via"] == "direct",
          f"via={r['stored_via']} ids={r['memory_ids']}")

    # P2-c: 相似度 0.95 → 仍交给 reconcile 判合并，但必须回执 merged_existing
    vs = FakeVS(best_score=0.95, exists={"old1"})
    w = FakeWriter(
        vs,
        reconcile_result=(["old1"], None, {"update": 1, "total": 1}, {}),
        direct_result=([], None, {}, {}),
    )
    r = _commit(vs, w, ["用户喜欢 Rust 语言"])
    check("P2c 高相似度仍走 reconcile，并回执 merged_existing + warning",
          r["merged_existing"] is True and "并入既有记忆" in (r.get("warning") or ""),
          f"warning={r.get('warning')}")

    # ---- P3：图库被锁时删除不得报假失败 ----
    print("\n[P3] 删除回执（graph_unavailable）")

    from hymem.backends.hymemory import clientops

    class FakeGraph:
        _available = False

    class FakeDeleteClient:
        _graph_store = FakeGraph()

        def __init__(self):
            self.gone = False

        def delete(self, mid):
            self.gone = True
            return {
                "success": False, "deleted_count": 0, "partial": True,
                "vdb_deleted": True, "graph_deleted": False,
                "graph_cleanup_ok": False, "error_code": "delete_incomplete",
            }

        def get(self, mid):
            return None if self.gone else {"memory_id": mid}

    dc = FakeDeleteClient()
    mid = "shadow-1"
    conn = store.connect()
    try:
        store.enqueue(conn, {"text": "x", "source": "cli", "identity": {"user_id": "u1"}})
        conn.execute(
            "UPDATE jobs SET status='done', shadow_only=1, memory_ids=? WHERE id=1", (mid,)
        )
    finally:
        conn.close()
    r = clientops.delete_memory_with_cascade(dc, mid)
    check("P3a VDB 已删 + 图库被锁 → success=true / partial=false",
          r.get("success") is True and r.get("partial") is False,
          f"success={r.get('success')} partial={r.get('partial')}")
    check("P3b deleted_count=1，同时保留 sdk 原始错误码供排障",
          r.get("deleted_count") == 1 and r.get("sdk_error_code") == "delete_incomplete",
          f"count={r.get('deleted_count')} sdk={r.get('sdk_error_code')}")
    check("P3c graph_deleted 仍为 false，但标注 graph_unavailable/graph_skipped",
          r.get("graph_deleted") is False and r.get("graph_unavailable") is True
          and r.get("graph_skipped") is True, f"{r.get('graph_unavailable')}")
    conn = store.connect()
    try:
        row = conn.execute("SELECT shadow_only FROM jobs WHERE id=1").fetchone()
    finally:
        conn.close()
    check("P3d 节点已删 → 对应 shadow 任务回执销账（shadow_only=0）",
          int(row[0]) == 0, f"shadow_only={row[0]}")

    # ---- P4：标签过滤要多轮补取；update_memory 支持改标签 ----
    print("\n[P4] 标签过滤 / update tags")

    from hymem.core.searchops import searchcore

    class FakeTagClient:
        _mode = "pro"

        def __init__(self):
            self.calls: list[int] = []

        def search(self, query=None, **kw):
            limit = int(kw.get("limit") or 5)
            self.calls.append(limit)
            items = [{"memory_id": f"u{i}", "content": "无关", "score": 0.5}
                     for i in range(min(limit, 30))]
            if limit >= 90:
                items.append({"memory_id": "tagged1", "content": "需求拆分", "score": 0.4})
            return {"memories": {"normal": items}, "elapsed_ms": 1.0}

    conn = store.connect()
    try:
        store.bind_tags(conn, ["tagged1"], ["需求拆分"], source="user")
    finally:
        conn.close()
    tc = FakeTagClient()
    r = searchcore(
        tc, "契约变更登记", tags=["需求拆分"], tag_mode="all",
        fetch_limit=30, limit=3, schema=False, remember_auto=False,
        pending_always=False, probe_empty=False,
    )
    check("P4a 目标标签条目排在 top-k 之外仍能被检出",
          [i["memory_id"] for i in r["items"]] == ["tagged1"], str(r.get("items"))[:80])
    check("P4b 发生了多轮补取（tag_fetch_rounds>1）",
          int(r.get("tag_fetch_rounds") or 0) > 1, f"rounds={r.get('tag_fetch_rounds')} calls={tc.calls}")

    class FakeUpdClient:
        def __init__(self):
            self.updated = []

        def get(self, mid):
            return {"memory_id": mid}

        def update(self, mid, content):
            self.updated.append((mid, content))
            return {"success": True}

    uc = FakeUpdClient()
    clientops.update_memory(uc, "m-tag", None, ["甲", "乙"])
    conn = store.connect()
    try:
        got = store.tags_for(conn, ["m-tag"], source="user").get("m-tag", [])
    finally:
        conn.close()
    check("P4c 只改标签（content=None）也能写入",
          sorted(got) == ["乙", "甲"], str(got))
    clientops.update_memory(uc, "m-tag", None, ["丙"])
    conn = store.connect()
    try:
        got2 = store.tags_for(conn, ["m-tag"], source="user").get("m-tag", [])
    finally:
        conn.close()
    check("P4d 再次更新按整组替换（旧用户标签被清掉）", got2 == ["丙"], str(got2))
    check("P4e 未传 content 时不触发 SDK 正文更新", uc.updated == [], str(uc.updated))

    # ---- P5：延迟看真实 jobs；分页去重 ----
    print("\n[P5] 健康口径")

    from hymem.cmds import maint

    now = time.time()
    conn = store.connect()
    try:
        for dur in (9.0, 5.0, 3.5):
            conn.execute(
                "INSERT INTO jobs (payload, status, created_at, started_at, finished_at) "
                "VALUES ('{}', 'done', ?, ?, ?)",
                (now - dur, now - dur, now),
            )
    finally:
        conn.close()
    est, samples = maint._observed_write_latency_s()
    check("P5a 写入延迟取 jobs 实际耗时中位数（5.0s，不再是 361.6s）",
          est is not None and abs(est - 5.0) < 1e-6 and samples >= 3,
          f"est={est} samples={samples}")

    class PagedClient:
        def __init__(self):
            self.pages = [["a", "b"], ["b", "c"], []]
            self.i = 0

        def list_memories(self, limit=100, offset=0, **kw):
            page = self.pages[min(self.i, len(self.pages) - 1)]
            self.i += 1
            return {"vdb": {"memories": [{"memory_id": x} for x in page], "total": 3}}

    scanned = maint._scan_all_memories(PagedClient(), {}, page=2)
    check("P5b 分页错位时按 id 去重，不重复计数",
          [x.get("memory_id") for x in scanned] == ["a", "b", "c"],
          str(scanned))

    bad = results.count(False)
    print(f"\n{'全部通过' if bad == 0 else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
