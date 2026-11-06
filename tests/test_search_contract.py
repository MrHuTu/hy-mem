"""检索契约与可观测性回归（2026-11-06 第一批）。

覆盖四件事，断言都落在「真实调用参数」或「真实 sqlite」上：
  A. queries 契约：SDK 只认最后一条，回执必须如实回显 target_query；
  B. 空结果诊断：effective_min_score / raw_candidates / 各段过滤计数 / empty_reason；
  C. manual_tags 从本地 tags 表取；MCP search/list 支持 tags 过滤；
  D. L6 schema 查询与检索目标对齐（queries[-1]），空 query 跳过并标 no_query。

跑法：
    .venv/Scripts/python.exe tests/test_search_contract.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(bool(passed))
    print(("  PASS  " if passed else "  FAIL  ") + name + (f"  → {detail}" if detail else ""))


class FakeChat:
    """最小 chat client：记录 search 实参，可配 items/total。"""

    _mode = "pro"

    def __init__(self, items=None, total=None, record=None):
        self.items = list(items or [])
        self.total = total
        self.record = record or {}
        self.calls: list[tuple] = []
        self.list_calls: list[dict] = []

    def search(self, query=None, **kw):
        self.calls.append((query, kw))
        if self.items:
            return {"memories": {"normal": self.items}, "elapsed_ms": 1.0}
        return {"memories": {"normal": []}, "elapsed_ms": 1.0}

    def list_memories(self, limit: int = 200, offset: int = 0, **kw):
        self.list_calls.append({"limit": limit, "offset": offset, **kw})
        total = len(self.items) if self.total is None else self.total
        return {"vdb": {"memories": self.items[:limit], "total": total}}

    def get(self, mid):
        return self.record

    def close(self):
        pass


def _cli_search(argv: list[str], client):
    from hymem.cmds import read as readmod
    from hymem.cmds.parser import build_parser

    real_make, real_ready = readmod.make_client, readmod.require_ready
    try:
        readmod.make_client = lambda mode: client
        readmod.require_ready = lambda *a, **k: None
        args = build_parser().parse_args(["--json", "-u", "u1", "search", *argv])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = readmod.cmd_search(args)
        return rc, json.loads(buf.getvalue().strip() or "{}")
    finally:
        readmod.make_client, readmod.require_ready = real_make, real_ready


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hymem-contract-"))
    (tmp / "data").mkdir()
    cfg = tmp / "config.json"
    cfg.write_text(json.dumps({"mode": "pro", "identity": {"userId": "u1"}}), encoding="utf-8")
    os.environ["MEMORY_DATA_DIR"] = str(tmp / "data")
    os.environ["HY_MEM_CONFIG"] = str(cfg)
    os.environ.pop("MEMORY_CODING_ENABLED", None)

    from hymem.core import store
    from hymem.core.searchops import searchcore
    import hymem.backends.hymemory.searchops as so
    from hymem.mcp import server as scmod

    # ---- A. queries 契约：只认最后一条，回执回显 target_query ----
    print("\n[A] queries 契约")
    _fake = FakeChat(items=[{"memory_id": "m1", "content": "x", "score": 0.9}])
    _r = searchcore(_fake, "q1", queries=["q1", "q2"])
    check("A1 queries 时传给 SDK 的 query 必须为 None（SDK 二者互斥）",
          _fake.calls and _fake.calls[0][0] is None, f"got={_fake.calls[0][0]!r}")
    check("A2 searchcore 回执 target_query=queries[-1]",
          _r.get("target_query") == "q2", f"got={_r.get('target_query')!r}")

    _fake_mcp = FakeChat(items=[{"memory_id": "m1", "content": "x", "score": 0.9}])
    _old = scmod._CLIENT
    scmod._CLIENT = _fake_mcp
    try:
        _mcp = json.loads(scmod._tool_search_memory({"queries": ["a", "b"]}))
    finally:
        scmod._CLIENT = _old
    check("A3 MCP 回执 target_query=b", _mcp.get("target_query") == "b",
          f"got={_mcp.get('target_query')!r}")

    _fake_cli = FakeChat(items=[{"memory_id": "m1", "content": "x", "score": 0.9}])
    _rc, _cli = _cli_search(["--queries", "a,b"], _fake_cli)
    check("A4 CLI 回执 target_query=b", _rc == 0 and _cli.get("target_query") == "b",
          f"rc={_rc} got={_cli.get('target_query')!r}")
    check("A5 CLI 传给 SDK 的目标也只有最后一条", _fake_cli.calls and _fake_cli.calls[0][0] is None,
          f"got={_fake_cli.calls[0][0]!r}")

    # ---- B. 空结果诊断 ----
    print("\n[B] 空结果诊断")
    _f = FakeChat(items=[{"memory_id": "m1", "content": "x", "score": 0.5,
                          "created_at": 100}])
    conn = store.connect()
    try:
        store.bind_tags(conn, ["m1"], ["proj:x"], source="user")
    finally:
        conn.close()
    _rb = searchcore(_f, "q", min_score=None, tags=["missing"])
    check("B1 effective_min_score 为 SDK 真实回退值 0.3",
          _rb.get("effective_min_score") == 0.3, f"got={_rb.get('effective_min_score')!r}")
    check("B2 raw/时间/标签三段计数可区分",
          (_rb.get("raw_candidates"), _rb.get("after_time_filter"),
           _rb.get("after_tag_filter")) == (1, 1, 0),
          f"got=({_rb.get('raw_candidates')},{_rb.get('after_time_filter')},"
          f"{_rb.get('after_tag_filter')})")
    check("B3 被标签过滤空 → empty_reason=tag_filtered",
          _rb.get("empty_reason") == "tag_filtered", f"got={_rb.get('empty_reason')!r}")

    _rb2 = searchcore(FakeChat(items=[{"memory_id": "m1", "content": "x", "score": 0.5,
                                       "created_at": 100}]), "q", until=50)
    check("B4 被时间窗过滤空 → empty_reason=time_filtered",
          _rb2.get("empty_reason") == "time_filtered", f"got={_rb2.get('empty_reason')!r}")

    _rb3 = searchcore(FakeChat(items=[], total=5), "q")
    check("B5 库里有 5 条但阈值下无命中 → below_threshold_or_no_match",
          _rb3.get("empty_reason") == "below_threshold_or_no_match"
          and _rb3.get("library_total") == 5,
          f"got={_rb3.get('empty_reason')!r} total={_rb3.get('library_total')!r}")

    _rb4 = searchcore(FakeChat(items=[], total=0), "q")
    check("B6 库里确实没有 → empty_library",
          _rb4.get("empty_reason") == "empty_library", f"got={_rb4.get('empty_reason')!r}")

    _mcp_diag = json.loads(scmod._tool_search_memory({"query": "q", "min_score": 0.0})) \
        if False else None  # MCP 空库走真实 client，这里只验 searchcore 契约
    _ = _mcp_diag

    # ---- C. manual_tags 读本地 tags 表 + MCP tags 过滤 ----
    print("\n[C] 本地标签与 MCP tags 过滤")
    _items = [
        {"memory_id": "m1", "content": "one", "score": 0.9, "tags": ["auto1"]},
        {"memory_id": "m2", "content": "two", "score": 0.8, "tags": ["auto2"]},
    ]
    conn = store.connect()
    try:
        store.bind_tags(conn, ["m2"], ["area:c"], source="user")
    finally:
        conn.close()

    _fget = FakeChat(items=_items, record={
        "memory_id": "m2", "content": "two", "tags": ["auto2"]})
    _old = scmod._CLIENT
    scmod._CLIENT = _fget
    try:
        _g = json.loads(scmod._tool_get_memory({"memory_id": "m2"}))
        _l = json.loads(scmod._tool_list_memories({"limit": 10}))
        _s = json.loads(scmod._tool_search_memory({"query": "q", "tags": ["area:c"]}))
        _l2 = json.loads(scmod._tool_list_memories({"tags": ["area:c"]}))
    finally:
        scmod._CLIENT = _old
    check("C1 get_memory.manual_tags 来自本地 tags 表",
          _g.get("memory", {}).get("manual_tags") == ["area:c"],
          f"got={_g.get('memory', {}).get('manual_tags')!r}")
    _m2 = next((m for m in _l.get("memories") or [] if m.get("memory_id") == "m2"), {})
    check("C2 list_memories.manual_tags 来自本地 tags 表",
          _m2.get("manual_tags") == ["area:c"], f"got={_m2.get('manual_tags')!r}")
    check("C3 search_memory 支持 tags 过滤（只剩 m2）",
          [h.get("memory_id") for h in _s.get("hits") or []] == ["m2"],
          f"got={[h.get('memory_id') for h in _s.get('hits') or []]}")
    check("C4 list_memories 支持 tags 过滤（只剩 m2）",
          [m.get("memory_id") for m in _l2.get("memories") or []] == ["m2"],
          f"got={[m.get('memory_id') for m in _l2.get('memories') or []]}")

    # ---- D. L6 schema 查询与检索目标对齐 ----
    print("\n[D] L6 schema_query 对齐")
    _cap: dict = {}
    _orig_fetch = so.fetch_l6_schemas

    def _fake_fetch(client, query, **kw):
        _cap["q"] = query
        return [], "off"

    so.fetch_l6_schemas = _fake_fetch
    try:
        searchcore(FakeChat(items=_items), "q1", queries=["q1", "q2"], schema=True)
        _cap_mcp = None
        _old = scmod._CLIENT
        scmod._CLIENT = FakeChat(items=_items)
        try:
            scmod._tool_search_memory({"queries": ["a", "b"]})
            _cap_mcp = _cap.get("q")
        finally:
            scmod._CLIENT = _old
        _cli_fake = FakeChat(items=_items)
        _cli_search(["--queries", "a,b"], _cli_fake)
        _cap_cli = _cap.get("q")
    finally:
        so.fetch_l6_schemas = _orig_fetch
    check("D1 searchcore 的 schema_query=queries[-1]",
          _cap.get("q") == "b", f"last={_cap.get('q')!r}")
    check("D2 MCP 的 schema_query=queries[-1]", _cap_mcp == "b", f"got={_cap_mcp!r}")
    check("D3 CLI 的 schema_query=queries[-1]", _cap_cli == "b", f"got={_cap_cli!r}")

    # ---- E. 空 query 跳过 L6 并标 no_query ----
    print("\n[E] 空 query 的 L6 跳过")
    class _GS:
        _available = True

    class _Ultra:
        _mode = "ultra"
        _graph_store = _GS()
        _embed_service = object()

    _schemas, _state = so.fetch_l6_schemas(_Ultra(), "")
    check("E1 空 query 直接 skip，state=no_query",
          _schemas == [] and _state == "no_query", f"state={_state!r}")

    bad = results.count(False)
    print(f"\n{'全部通过' if bad == 0 else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
