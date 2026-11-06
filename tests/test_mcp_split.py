# -*- coding: utf-8 -*-
"""MCP add_memory 的 split（本批：与 CLI `add --split` 对齐）。

覆盖：
  A. 异步路径：不传 split 逐字段等价；split=true 按行拆条入队；
     上限 50 截断 + warning；空文本/单条不拆；空行不算条。
  B. 同步路径：逐条 extract→commit；memory_ids 与拆条数对应；
     标签只绑一次（与 CLI _bind_all_once 同口径）；拆分后每条都能被检索到。
  C. scene 路径：split 不生效（CLI 同款），回执显式标 split_ignored，不静默。
  D. 分片幂等 id：多片按 `<rid>#<i>` 派生（唯一 + 稳定），单片/无 rid 不变。
  E. CLI --split：与 MCP 共用 SPLIT_MAX=50，截断告警；分片 id 同样派生。
  F. CLI --scene --split：与 MCP 同为「忽略 + 显式标注」，不再静默。

跑法：
    .venv/Scripts/python.exe tests/test_mcp_split.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

results: list[bool] = []
_TMP: Path | None = None


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(bool(passed))
    print(("  PASS  " if passed else "  FAIL  ") + name + (f"  → {detail}" if detail else ""))


def _setup_env() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="hymem-split-"))
    (tmp / "data").mkdir()
    cfg = tmp / "config.json"
    cfg.write_text(json.dumps({"mode": "pro", "identity": {"userId": "u1"}}),
                   encoding="utf-8")
    os.environ["MEMORY_DATA_DIR"] = str(tmp / "data")
    os.environ["HY_MEM_CONFIG"] = str(cfg)
    os.environ["MEMORY_CODING_ENABLED"] = "true"
    os.environ.pop("HY_MEM_ASYNC", None)
    return tmp


class FakeAddChat:
    """最小 client：scene 写入只记录实参。"""

    _mode = "pro"

    def __init__(self):
        self.calls: list[dict] = []

    def add(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        return {
            "success": True, "scene": "productivity", "variant": kw.get("scene"),
            "fact_ids": ["f1"], "experience_ids": [],
        }

    def close(self):
        pass


class _SearchChat:
    """读路径 fake：按 item 列表返回，用于验证「每条拆分件都能检索到」。"""

    _mode = "pro"

    def __init__(self, items):
        self.items = list(items)

    def search(self, query=None, **kw):
        return {"memories": {"normal": self.items}, "elapsed_ms": 1.0}

    def list_memories(self, limit=200, offset=0, **kw):
        return {"vdb": {"memories": self.items[:limit], "total": len(self.items)}}

    def close(self):
        pass


def _payloads(job_ids: list[int]) -> list[dict]:
    from hymem.core import store

    conn = store.connect()
    try:
        return [j.get("payload") or {} for j in store.get_jobs(conn, list(job_ids))]
    finally:
        conn.close()


def _cli_add(argv: list[str], client=None) -> tuple[int, dict]:
    """跑真实 cmd_add（store/队列都是真的，只把 ready/client/worker 拉起挡掉）。"""
    import contextlib
    import io

    from hymem.cmds import write as wmod
    from hymem.cmds.parser import build_parser

    real = (wmod.require_ready, wmod.make_client, wmod._spawn_detached_worker)
    try:
        wmod.require_ready = lambda *a, **k: None
        wmod.make_client = lambda mode: client
        wmod._spawn_detached_worker = lambda: False
        args = build_parser().parse_args(["--json", "-u", "u1", "add", *argv])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wmod.cmd_add(args)
        return rc, json.loads(buf.getvalue().strip() or "{}")
    finally:
        (wmod.require_ready, wmod.make_client, wmod._spawn_detached_worker) = real


def main() -> int:
    global _TMP
    _TMP = _setup_env()

    from hymem.core.timefilter import ParamError
    from hymem.mcp import server as scmod

    # 后台 worker 不要在测试里被拉起：假 job 会被真 worker 消费掉
    import hymem.core.procs as procsmod

    _orig_ensure = procsmod.ensure_worker_for_pending
    procsmod.ensure_worker_for_pending = lambda conn, stats=None: (False, False)

    def add(args: dict) -> dict:
        return json.loads(scmod._tool_add_memory(args))

    LEGACY_KEYS = {"mode", "job_ids", "pending", "worker_spawned", "worker_live",
                   "progress_tool", "note"}

    try:
        # ---- A. 异步路径 ----
        print("\n[A] 异步路径")
        _base = add({"text": "单条事实：hy-mem 的队列用 SQLite。"})
        check("A0 不传 split：回执字段与改前逐字段等价",
              set(_base) == LEGACY_KEYS and len(_base.get("job_ids") or []) == 1,
              f"keys={sorted(set(_base) - LEGACY_KEYS)} n={len(_base.get('job_ids') or [])}")

        _lines = ["事实一：用户偏好 pnpm。", "事实二：仓库在 d:/tools_open。",
                  "事实三：单测用脚本式 check。"]
        _r1 = add({"text": "\n".join(_lines), "split": True})
        _p1 = [p.get("text") for p in _payloads(_r1.get("job_ids") or [])]
        check("A1 split=true 按行拆成 3 条入队", _p1 == _lines, f"got={_p1!r}")
        check("A2 回执能看出 1 条原文拆成 N 条（count/split.parts）",
              _r1.get("count") == 3 and (_r1.get("split") or {}).get("parts") == 3,
              f"count={_r1.get('count')!r} split={_r1.get('split')!r}")

        _many = "\n".join(f"第{i}条规范" for i in range(55))
        _r2 = add({"text": _many, "split": True})
        _n2 = len(_r2.get("job_ids") or [])
        _w2 = str(_r2.get("warning") or "")
        check("A3 超过上限 50 截断，回执 warning 说明剩余条数",
              _n2 == 50 and "5" in _w2 and (_r2.get("split") or {}).get("truncated") == 5,
              f"n={_n2} warning={_w2!r} split={_r2.get('split')!r}")

        _r3 = add({"text": "只有一条，不该被拆。", "split": True})
        _p3 = [p.get("text") for p in _payloads(_r3.get("job_ids") or [])]
        check("A4 单条文本不拆（parts=1，原文不变）",
              _p3 == ["只有一条，不该被拆。"]
              and (_r3.get("split") or {}).get("parts") == 1,
              f"got={_p3!r} split={_r3.get('split')!r}")

        _r4 = add({"text": "  a  \n\n   \n b ", "split": True})
        _p4 = [p.get("text") for p in _payloads(_r4.get("job_ids") or [])]
        check("A5 空行不计条，行首尾空白被裁", _p4 == ["a", "b"], f"got={_p4!r}")

        _empty_ok = False
        try:
            add({"text": "   \n  ", "split": True})
        except ParamError:
            _empty_ok = True
        check("A6 空白文本仍按原契约报 ParamError", _empty_ok)

        # ---- B. 同步路径（两段式管线用桩，断言落在真实调用参数上）----
        print("\n[B] 同步路径")
        import hymem.core.two_phase as tpmod

        _orig = (tpmod.extract_phase, tpmod.prepare_commit, tpmod.commit_extracted_with_retry)
        _cap: dict[str, list] = {"extract": [], "commit": []}

        def _fake_extract(client, text, **kw):
            _cap["extract"].append(text)
            return {"fake": True}

        def _fake_prepare(client, text, agent_result, **kw):
            return {"fake": True}

        def _fake_commit(client, text, agent_result, prepared=None, **kw):
            _cap["commit"].append(text)
            mid = f"id{len(_cap['commit'])}"
            return {"success": True, "memory_ids": [mid], "memory_id": mid}

        tpmod.extract_phase = _fake_extract
        tpmod.prepare_commit = _fake_prepare
        tpmod.commit_extracted_with_retry = _fake_commit

        _s1 = add({"text": "\n".join(_lines), "split": True, "async": False})
        check("B1 sync split 逐条抽取（3 次，文本顺序不变）",
              _cap["extract"] == _lines, f"got={_cap['extract']!r}")
        check("B2 sync 回执 memory_ids 数量与拆条数对应",
              _s1.get("memory_ids") == ["id1", "id2", "id3"]
              and _s1.get("count") == 3 and (_s1.get("split") or {}).get("parts") == 3,
              f"memory_ids={_s1.get('memory_ids')!r} count={_s1.get('count')!r}")

        _cap["extract"].clear()
        _cap["commit"].clear()
        _s2 = add({"text": "只有一条同步写入。", "async": False})
        check("B3 sync 不传 split：行为与改前等价（单次抽取、旧字段）",
              _cap["extract"] == ["只有一条同步写入。"]
              and set(_s2) == {"mode", "memory_ids", "tags"},
              f"extract={_cap['extract']!r} keys={sorted(_s2)}")

        # 拆条 + 标签：只绑一次、绑到全部分片（对齐 CLI _bind_all_once）
        import hymem.core.searchops as shim

        _bind_calls: list[list[str]] = []
        _orig_bind = shim._bind_tags_to_new
        _orig_snap = shim._snapshot_ids
        shim._snapshot_ids = lambda client, ident: (set(), True)

        def _fake_bind(client, ident, before, returned, tags, **kw):
            _bind_calls.append(list(returned))
            return list(returned), ""

        shim._bind_tags_to_new = _fake_bind
        try:
            _cap["extract"].clear()
            _cap["commit"].clear()
            _s3 = add({"text": "甲\n乙\n丙", "split": True, "async": False,
                       "tags": ["t1"]})
        finally:
            shim._bind_tags_to_new = _orig_bind
            shim._snapshot_ids = _orig_snap
        check("B4 sync split + tags：标签对 3 个分片只绑一次",
              _bind_calls == [["id1", "id2", "id3"]]
              and _s3.get("tags") == ["t1"],
              f"bind={_bind_calls!r} tags={_s3.get('tags')!r}")

        _cap["commit"].clear()
        _cap["extract"].clear()
        _s4 = add({"text": "\n".join(f"规范{i}" for i in range(55)),
                   "split": True, "async": False})
        check("B5 sync split 同样受 50 上限约束并 warning",
              len(_s4.get("memory_ids") or []) == 50
              and (_s4.get("split") or {}).get("truncated") == 5
              and "5" in str(_s4.get("warning") or ""),
              f"n={len(_s4.get('memory_ids') or [])} split={_s4.get('split')!r}")

        tpmod.extract_phase, tpmod.prepare_commit, tpmod.commit_extracted_with_retry = _orig

        # 拆分后每条独立成记忆 → 每一条都能被检索命中
        from hymem.core.searchops import searchcore

        _items = [{"memory_id": f"id{i}", "content": t, "score": 0.9}
                  for i, t in enumerate(_lines, 1)]
        _hits = []
        for t in _lines:
            _r = searchcore(_SearchChat(_items), t)
            _hits.append([h.get("memory_id") for h in (_r.get("items") or [])])
        check("B6 拆分后的每条都能独立检索到",
              all(h for h in _hits), f"hits={_hits!r}")

        # ---- C. scene 路径 ----
        print("\n[C] scene 路径（split 不生效，必须显式说明）")
        _fake = FakeAddChat()
        _old_client = scmod._CLIENT
        scmod._CLIENT = _fake
        try:
            _c1 = add({"scene": "working",
                       "messages": [{"role": "assistant",
                                     "tool_calls": [{"id": "c1", "type": "function"}]}],
                       "split": True})
            _c2 = add({"scene": "working",
                       "messages": [{"role": "assistant",
                                     "tool_calls": [{"id": "c2", "type": "function"}]}]})
            _calls = len(_fake.calls)
        finally:
            scmod._CLIENT = _old_client
        check("C1 scene + split：仍按整段轨迹写一次，回执标 split_ignored",
              _calls == 2 and _c1.get("split_ignored") == "scene"
              and "split" in str(_c1.get("warning") or ""),
              f"calls={_calls} split_ignored={_c1.get('split_ignored')!r} "
              f"warning={_c1.get('warning')!r}")
        check("C2 scene 不传 split：回执不多出 split_ignored 字段",
              "split_ignored" not in _c2, f"keys={sorted(_c2)}")

        # ---- D. 分片幂等 id 派生（修「多片共用同一 id → 只落第一条」）----
        print("\n[D] 分片幂等 id")
        _three = ["规范甲", "规范乙", "规范丙"]
        _d1 = add({"text": "\n".join(_three), "split": True, "request_id": "rid-1"})
        _rids = [p.get("request_id") for p in _payloads(_d1.get("job_ids") or [])]
        check("D1 每片 request_id 唯一且为 <rid>#<i>",
              _rids == ["rid-1#1", "rid-1#2", "rid-1#3"], f"got={_rids!r}")
        check("D2 回执标注已派生（request_id_derived / prefix）",
              (_d1.get("split") or {}).get("request_id_derived") is True
              and (_d1.get("split") or {}).get("request_id_prefix") == "rid-1#",
              f"split={_d1.get('split')!r}")
        _d2 = add({"text": "\n".join(_three), "split": True, "request_id": "rid-1"})
        _rids2 = [p.get("request_id") for p in _payloads(_d2.get("job_ids") or [])]
        check("D3 同输入重试得到同样分片 id（幂等键稳定）",
              _rids2 == _rids, f"first={_rids!r} again={_rids2!r}")
        _d3 = add({"text": "\n".join(_three), "split": True})
        check("D4 不传 request_id：不派生、payload 不带该键",
              all("request_id" not in p for p in _payloads(_d3.get("job_ids") or [])),
              f"got={[p.get('request_id') for p in _payloads(_d3.get('job_ids') or [])]!r}")
        _d4 = add({"text": "只有一条", "split": True, "request_id": "rid-2"})
        _d4p = _payloads(_d4.get("job_ids") or [])
        check("D5 单条文本不派生（沿用原 request_id）",
              [p.get("request_id") for p in _d4p] == ["rid-2"]
              and "request_id_derived" not in (_d4.get("split") or {}),
              f"rids={[p.get('request_id') for p in _d4p]!r} split={_d4.get('split')!r}")

        # sync 路径同样按片派生（断言落在传给提交段的真实 kwargs 上）
        _commit_kw: list[dict] = []
        _orig_set = (tpmod.extract_phase, tpmod.prepare_commit,
                     tpmod.commit_extracted_with_retry)

        def _noop_extract(client, text, **kw):
            return {"fake": True}

        def _noop_prepare(client, text, agent_result, **kw):
            return {"fake": True}

        def _cap_commit(client, text, agent_result, prepared=None, **kw):
            _commit_kw.append(kw)
            return {"success": True, "memory_ids": [f"id{len(_commit_kw)}"]}

        tpmod.extract_phase = _noop_extract
        tpmod.prepare_commit = _noop_prepare
        tpmod.commit_extracted_with_retry = _cap_commit
        try:
            _d5 = add({"text": "\n".join(_three), "split": True, "async": False,
                       "request_id": "rid-s"})
        finally:
            (tpmod.extract_phase, tpmod.prepare_commit,
             tpmod.commit_extracted_with_retry) = _orig_set
        check("D6 sync 每片提交用唯一 request_id",
              [k.get("request_id") for k in _commit_kw]
              == ["rid-s#1", "rid-s#2", "rid-s#3"]
              and len(_d5.get("memory_ids") or []) == 3,
              f"kw={[k.get('request_id') for k in _commit_kw]!r} "
              f"ids={_d5.get('memory_ids')!r}")

        # ---- E. CLI --split 共用同一上限与派生规则 ----
        print("\n[E] CLI --split")
        _rc1, _p1 = _cli_add(["--split", "--async",
                              "\n".join(f"第{i}条规范" for i in range(55))])
        check("E1 CLI 55 行只写 50 条并告警",
              _rc1 == 0 and _p1.get("queued") == 50
              and (_p1.get("split") or {}).get("truncated") == 5
              and "5" in str(_p1.get("warning") or ""),
              f"rc={_rc1} queued={_p1.get('queued')!r} "
              f"split={_p1.get('split')!r} warning={_p1.get('warning')!r}")

        _rc2, _p2 = _cli_add(["--split", "--async", "--request-id", "rid-cli",
                              "甲\n乙\n丙"])
        _crids = [p.get("request_id") for p in _payloads(_p2.get("job_ids") or [])]
        check("E2 CLI 分片也按 <rid>#<i> 派生（多片不再共用同一 id）",
              _crids == ["rid-cli#1", "rid-cli#2", "rid-cli#3"],
              f"got={_crids!r}")

        _rc3, _p3 = _cli_add(["--async", "不拆的普通单条"])
        check("E3 CLI 不传 --split：回执字段与改前等价（无 split/warning）",
              _rc3 == 0 and "split" not in _p3 and "warning" not in _p3
              and _p3.get("queued") == 1,
              f"keys={sorted(_p3)} queued={_p3.get('queued')!r}")

        # ---- F. CLI --scene --split：忽略但必须显式标注 ----
        print("\n[F] CLI --scene --split")
        _trace = _TMP / "trace.json"
        _trace.write_text(json.dumps([
            {"role": "assistant", "tool_calls": [{"id": "t1", "type": "function"}]}
        ]), encoding="utf-8")
        _fake_scene = FakeAddChat()
        _rcf, _pf = _cli_add(["--scene", "working", "--trace", str(_trace),
                              "--split", "整段轨迹"], _fake_scene)
        _rcf2, _pf2 = _cli_add(["--scene", "working", "--trace", str(_trace)],
                               FakeAddChat())
        check("F1 CLI scene+split：回执标 split_ignored 并给 warning",
              _rcf == 0 and _pf.get("split_ignored") == "scene"
              and "未拆分" in str(_pf.get("warning") or ""),
              f"rc={_rcf} split_ignored={_pf.get('split_ignored')!r} "
              f"warning={_pf.get('warning')!r}")
        check("F2 CLI scene+split：仍只调用一次 add（不拆成多次 scene 写入）",
              _pf.get("ok") is True and len(_fake_scene.calls) == 1,
              f"calls={len(_fake_scene.calls)}")
        check("F3 CLI scene 不传 split：回执不多出 split_ignored",
              _rcf2 == 0 and "split_ignored" not in _pf2,
              f"keys={sorted(_pf2)}")
    finally:
        procsmod.ensure_worker_for_pending = _orig_ensure

    bad = results.count(False)
    print(f"\n{'全部通过' if bad == 0 else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
