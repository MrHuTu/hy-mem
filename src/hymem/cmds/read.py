"""读路径命令：search / list / get / history / profile / outcome。

S2 拆分（docs/specs/2026-11-02-cli-split-ports/）：本模块从 hymem/cli.py 按域拆入，
逻辑零变化——全局状态存于 cmds.base，跨模块经 _base 属性读写。
"""
from __future__ import annotations

import sys

from hymem.cmds.base import (  # noqa: F401
    EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, _embedding_unhealthy, _filter_by_tags,
    _flatten_search, _in_range, _item_ts, _parse_when, _pending_jobs,
    _remember_auto, c, config_defaults_user, fail, identity,
    identity_lists, json, make_client, ok, os,
    require_ready, sys,
)
from hymem.cmds import base as _base  # 全局状态经 _base 读写


def _cmd_search_scene(args, scene: str) -> int:
    """S2：生产力场景检索——facts/experiences 双路 + chain_block（S4 开关）。"""
    ws = getattr(args, "workspace", None)
    branch = getattr(args, "branch", None)
    if branch and not ws:
        return fail("--branch 需要同时传 --workspace", EXIT_USAGE)
    chain_block = bool(getattr(args, "chain_block", False))
    if chain_block:
        os.environ["MEMORY_CODING_CHAIN_BLOCK_ENABLED"] = "true"
    client = make_client(args.mode)
    try:
        r = client.search(
            args.query,
            scene=scene,
            workspace_id=ws or None,
            branch=branch or None,
            limit=args.limit,
            user_ids=[identity(args).get("user_id") or config_defaults_user()],
            **({"include_sources": [args.source_filter]} if getattr(args, "source_filter", None) else {}),
        )
    except Exception as e:  # noqa: BLE001
        return fail(f"scene 检索失败：{type(e).__name__}: {e}")
    if chain_block:
        # SDK 在带 workspace_id 时会把链全滤掉（上游 _in_scope 拿仓名比文件相对
        # 路径），这里用同一份链库兜底补块；无 workspace 时 SDK 自己就出了块。
        from hymem.core.searchops import fill_chain_block

        r = fill_chain_block(
            client, r, args.query,
            user_id=identity(args).get("user_id") or config_defaults_user(),
            workspace_id=ws,
        )
    facts = r.get("facts") or []
    experiences = r.get("experiences") or []
    payload = {
        "scene": r.get("scene"), "count": len(facts) + len(experiences),
        "facts": facts, "experiences": experiences,
        "chain_used": r.get("chain_used") or [],
        "chain_block": (r.get("chain_block") or "")[:2000] or None,
        "chain_note": r.get("chain_note"),
        "elapsed_ms": r.get("elapsed_ms"),
    }
    human_parts = [f"[{scene}] 事实 {len(facts)} 条、经验 {len(experiences)} 条"]
    if ws:
        human_parts.append(f"workspace={ws}" + (f"/{branch}" if branch else ""))
    human = "，".join(human_parts)
    if not args.json:
        for f_ in facts:
            print(f"  [fact] {str(f_.get('statement') or f_.get('content') or f_)[:100]}")
        for e_ in experiences:
            print(f"  [exp ] {str(e_.get('solution') or e_.get('task') or e_)[:100]}")
        cb = r.get("chain_block") or ""
        if cb:
            print(f"  [chain] {cb[:400]}")
    return ok(payload, args.json, human)


def cmd_outcome(args) -> int:
    """S2：按 request_id 查异步写入结果（SDK get_write_outcome）——比 queue_status
    的 job 状态更进一步：直接看 SDK 写成了什么（含 scene 写入）。"""
    rc = require_ready("查询写入结果")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        r = client.get_write_outcome(args.request_id)
    except Exception as e:  # noqa: BLE001
        return fail(f"查询失败：{type(e).__name__}: {e}")
    if r is None:
        return fail(f"request_id {args.request_id} 无结果（仍在写入或不存在）", EXIT_RUNTIME)
    return ok(r, args.json, str(r)[:200])


def cmd_profile(args) -> int:
    """S6：读 L0 用户画像（SDK get_user_basic_info，零 LLM/零 embedding）。

    L0 basic_info 是 add 时顺带抽取的「这个人是谁」——姓名/职业/所在地/偏好，
    与检索出来的 L1 事实是两层。以前只有写入没有读取，等于白抽。
    S3（docs/specs/2026-11-02-sdk-alignment-gaps/）：--full 走 get_user_profile
    ——SDK 组装好的完整画像（ultra 附 balanced catalog），仍零 LLM/零 embedding。
    """
    rc = require_ready("读取用户画像")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    uid = identity(args).get("user_id") or config_defaults_user()
    full = bool(getattr(args, "full", False))
    try:
        if full:
            r = client.get_user_profile(user_id=uid) or {}
        else:
            r = client.get_user_basic_info(user_id=uid) or {}
    except Exception as e:  # noqa: BLE001
        return fail(f"读取画像失败：{type(e).__name__}: {e}",
                    hint="确认身份与数据目录正确；排查用 hy-mem doctor")
    if full:
        # 组装画像结构由 SDK 决定（basic/memories/catalog 各层），原样透传 +
        # 摘要行；未知键不猜，方便 SDK 演进时不用改这里。
        payload = {"user": uid, "mode": "full", "profile": r}
        if args.json:
            return ok(payload, True)
        lines = [f"用户 {uid} 的完整画像（SDK get_user_profile）", ""]
        for k, v in r.items():
            if isinstance(v, (str, int, float, bool)) and v not in ("", None):
                lines.append(f"  {k}: {v}")
            elif isinstance(v, list):
                lines.append(f"  {k}: {len(v)} 条")
            elif isinstance(v, dict):
                lines.append(f"  {k}: {len(v)} 项")
        return ok(payload, args.json, "\n".join(lines))
    mems = [m for m in (r.get("memories") or []) if isinstance(m, dict)]
    payload = {
        "user": uid,
        "count": len(mems),
        "memories": mems,
        "elapsed_ms": r.get("elapsed_ms"),
    }
    if not mems:
        return ok(payload, args.json,
                  f"用户 {uid} 还没有 L0 画像记忆——add 写入时会顺带抽取基础信息")
    lines = [f"用户 {uid} 的 L0 画像（{len(mems)} 条）", ""]
    for m in mems:
        c = str(m.get("content") or "").strip()
        if c:
            lines.append(f"  - {c[:200]}")
    return ok(payload, args.json, "\n".join(lines))


def cmd_search(args) -> int:
    rc = require_ready("检索记忆")
    if rc is not None:
        return rc
    # S2 scene 分路（2026-10-30 scene spec）：--scene working/coding 走生产力
    # 检索（facts/experiences 双路返回）；缺省 chat 路径零变化。
    scene = getattr(args, "scene", None)
    if scene in ("working", "coding"):
        if not (args.query or "").strip() or getattr(args, "queries", None):
            return fail("scene 检索只支持单个位置参数 query（不支持 --queries）", EXIT_USAGE)
        return _cmd_search_scene(args, scene)
    # 多路检索（S1，docs/specs/2026-11-02-sdk-alignment-gaps/）：--queries 逗号
    # 分隔多个查询（SDK queries= 并行召回融合）；与位置参数 query 互斥。
    raw_q = getattr(args, "queries", None) or ""
    queries = [q.strip() for q in raw_q.replace("，", ",").split(",") if q.strip()]
    if queries and len(queries) > 5:
        return fail("--queries 最多 5 路（SDK 融合收益在 3~5 路，多了纯烧钱）", EXIT_USAGE)
    if queries and (args.query or "").strip():
        return fail("--queries 与位置参数 query 二选一，不要都传", EXIT_USAGE)
    if not queries and not (args.query or "").strip():
        return fail("search 需要查询语句：位置参数 query 或 --queries \"a,b\"", EXIT_USAGE)
    primary = queries[0] if queries else args.query
    client = make_client(args.mode)
    try:
        # 默认阈值按实测调过：qwen3.7-flash 的分数带更窄
        #   噪声 0.28 / 近邻干扰 0.33 / 合法改写 0.355~0.372 / 直命中 0.5~0.66
        # SDK 默认 0.4 会漏掉全部改写问法，0.35 是这批样本的最优分割点。
        min_score = args.min_score
        if min_score is None:
            min_score = float(os.getenv("HY_MEM_MIN_SCORE") or 0.35)

        want_tags = [t for t in (args.tag or []) if t.strip()]
        # 标签在 SDK 里不是查询条件，只能后过滤 → 必须多取，否则过滤后可能空手
        fetch_limit = args.limit if not want_tags else min(200, max(args.limit * 10, 30))

        # 时间窗：since 走 SDK 原生 created_after（检索端就裁掉，省得起一堆再扔），
        # until 与无时间戳兜底放行逻辑在本地过滤（与 MCP search_memory 同一套助手）。
        try:
            since = _parse_when(getattr(args, "since", None))
            until = _parse_when(getattr(args, "until", None))
        except ValueError as e:
            return fail(str(e), EXIT_USAGE)  # 坏时间格式是用法错，exit 1
        kw = {"created_after": since} if since is not None else {}

        # S3：L6 由我们自己跑正路。给 SDK 传 schema_limit=0 把它的「正路语义 +
        # 反路证据反查」一起关掉——反路不看 query，且注入时 score 被 RRF 融合分
        # 覆盖（0.01~0.03，与语义分不是一个尺度），调用方没法过滤也没法解释。
        want_schema = bool(getattr(args, "schema", True))
        from hymem.core import config as _cfgmod_l6

        _ev = (_cfgmod_l6.load().get("evolve") or {})
        schema_limit = int(
            getattr(args, "schema_limit", 0)
            or _ev.get("schemaLimit")
            or 3
        )
        schema_min = float(_ev.get("schemaMinScore") or 0.25)

        r = client.search(
            primary,
            **({"queries": queries} if queries else {}),
            search_mode=getattr(args, "search_mode", None) or "default",
            rerank=bool(getattr(args, "rerank", True)),
            limit=fetch_limit,
            min_score=min_score,
            schema_limit=0,
            **kw,
            **identity_lists(args),
        )
        items = _flatten_search(r)
        # schema 单独取、单独给，绝不混进 items：混进去就会在 normal 命中不足
        # limit 时补位冒充搜索结果，agent 会拿一条无关归纳当事实用。
        schemas: list[dict] = []
        schema_state = "off"
        if want_schema:
            uid = identity(args).get("user_id") or config_defaults_user()
            from hymem.core.searchops import fetch_l6_schemas

            schemas, schema_state = fetch_l6_schemas(
                client, primary, user_id=uid,
                limit=schema_limit, min_score=schema_min,
            )
        if not items and not want_tags:
            # 空结果有两种截然不同的原因：真的没有相似记忆，或者嵌入端点挂了。
            # SDK 会吞掉异常并返回空列表（日志里才有 401），不区分的话 agent
            # 会把「密钥失效」当成「没有相关记忆」，一路错下去。空手时补一次
            # 真实探测，把真实原因说出来。
            why = _embedding_unhealthy()
            if why:
                return fail(
                    f"检索失败：嵌入端点在报错（{why}）—— 这不代表没有相关记忆。"
                    "修：hy-mem setup --step embedding",
                    EXIT_RUNTIME,
                )
        _remember_auto(items)
        if since is not None or until is not None:
            items = [x for x in items if _in_range(_item_ts(x), since, until)]
        if want_tags:
            items = _filter_by_tags(items, want_tags, args.tag_mode)
        items = items[: args.limit]

        if args.json:
            _payload = {
                "count": len(items),
                "min_score": min_score,
                "elapsed_ms": r.get("elapsed_ms"),
                "tags_filter": want_tags,
                "results": items,
                "schemas": schemas,
            }
            # state → (available, note) 的唯一口径在 searchops.schema_state_meta：
            # disabled（非 ultra）/ graph_unavailable（kuzu 锁被抢）/ empty（有料被
            # 阈值滤空，available 保持 true）/ no_embedding / error。这里不再各写各的。
            from hymem.core.searchops import schema_state_meta

            _payload["schema_available"], _snote = schema_state_meta(schema_state, schema_min)
            if _snote:
                _payload["schema_note"] = _snote
            if not items:
                # agent 只看 JSON，这条线索必须进结构体，否则它只会得出
                # 「没有相关记忆」的结论，而真相是「还没落库」。
                _pend = _pending_jobs()
                if _pend:
                    _payload["pending_jobs"] = _pend
                    _payload["hint"] = f"队列里还有 {_pend} 条未落库，先跑 hy-mem worker --once"
            return ok(_payload, True)
        if not items:
            extra = f"（标签过滤：{','.join(want_tags)}）" if want_tags else ""
            print(f"（无命中）{extra}")
            pend = _pending_jobs()
            if pend:
                print(
                    c.dim(f"  但队列里还有 {pend} 条没落库 —— 刚写的记忆很可能就在其中。")
                )
                print(c.dim("  先跑一次：hy-mem worker --once（pro 模式每条约 10~50s）"))
            return EXIT_OK
        lines = []
        for i, it in enumerate(items, 1):
            score = it.get("score")
            s = f"{score:.3f}" if isinstance(score, (int, float)) else "  -  "
            layer = it.get("layer") or ""
            tg = it.get("_tags") or it.get("tags") or []
            tagstr = f"  #{','.join(str(t) for t in tg)}" if tg else ""
            head = f"{i:>2}. [{s}] "
            body = it.get("content") or it.get("text") or ""
            body_lines = str(body).split("\n")
            lines.append(head + body_lines[0] + (f"  <{layer}>" if layer else "") + tagstr)
            # 多行内容（演化链的 Current/History）按原文缩进对齐展开，不再压平成
            # 一行再截断 —— 截断会把 History 的后半段悄悄吃掉。
            pad = " " * len(head)
            for extra in body_lines[1:]:
                lines.append(pad + extra)
        # L6 单独一段：它不是「检索结果」，是「你碰到的这些事实背后有什么模式」。
        # 混进上面的编号列表会让人以为它是按相关度排出来的第 N 条记忆。
        if schemas:
            lines.append("")
            lines.append(c.dim(f"L6 行为模式（{len(schemas)} 条，按语义相似度）"))
            for i, s in enumerate(schemas, 1):
                txt = str(s.get("content") or "").replace("\n", " ")
                lines.append(f"{i:>2}. [{s.get('score'):.3f}] {txt}")
        elif want_schema:
            from hymem.core.searchops import schema_state_meta

            _avail, _note = schema_state_meta(schema_state, schema_min)
            if _note:
                lines.append("")
                lines.append(c.dim(f"（{_note}）"))
        return ok(items, False, "\n".join(lines))
    finally:
        client.close()


def cmd_list(args) -> int:
    rc = require_ready("读取记忆列表")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        want_tags = [t for t in (args.tag or []) if t.strip()]
        fetch_limit = args.limit if not want_tags else min(500, max(args.limit * 10, 50))
        _ident = identity(args)
        _no_ident_hint = None
        if not _ident:
            # SDK 的 list_by_user 无 user 过滤会直接拒绝（返回 0 条而非报错），
            # 不提示的话用户会以为库里真没数据
            _no_ident_hint = "未指定 -u/--user：SDK 拒绝无用户的全库列举，恒返回 0 条；请带 -u <用户> 重试"
            print(c.dim(f"  {_no_ident_hint}"), file=sys.stderr)
        r = client.list_memories(limit=fetch_limit, offset=args.offset, order=args.order, **_ident)
        vdb = r.get("vdb") or {}
        items = vdb.get("memories") or []
        source = "vdb"
        # lite 模式下 l1_raw 不进 vdb 列表，回退到最近历史
        if not items and args.user:
            try:
                items = client.get_recent_history(user_id=args.user, limit=fetch_limit) or []
                source = "recent"
            except Exception:  # noqa: BLE001
                items = []
        _remember_auto(items)
        if want_tags:
            items = _filter_by_tags(items, want_tags, args.tag_mode)
        items = items[: args.limit]
        if args.json:
            payload = {"count": len(items), "source": source, "tags_filter": want_tags, "memories": items}
            if _no_ident_hint:
                payload["hint"] = _no_ident_hint
            return ok(payload, True)
        if not items:
            print("（空）" if not want_tags else f"（无匹配标签 {','.join(want_tags)} 的记忆）")
            _pend = _pending_jobs()
            if _pend:
                print(c.dim(f"  队列里还有 {_pend} 条没落库（hy-mem worker --once 消费）"))
            return EXIT_OK
        lines = []
        for it in items:
            if not isinstance(it, dict):
                lines.append(str(it)[:140])
                continue
            mid = (it.get("memory_id") or it.get("id") or "?")
            mid = mid if isinstance(mid, str) else str(mid)
            body = it.get("content") or it.get("new_memory") or it.get("text") or ""
            tg = it.get("_tags") or it.get("tags") or []
            tagstr = f"  #{','.join(str(t) for t in tg)}" if tg else ""
            head = f"{mid[:18]:<18} "
            body_lines = str(body).split("\n")
            lines.append(head + body_lines[0] + tagstr)
            pad = " " * len(head)
            for extra in body_lines[1:]:
                lines.append(pad + extra)
        human = "\n".join(lines)
        _pend = _pending_jobs()
        if _pend:
            human += c.dim(f"\n\n（队列里还有 {_pend} 条没落库，可能是刚写的 —— hy-mem worker --once 消费）")
        return ok(items, False, human)
    finally:
        client.close()


def cmd_get(args) -> int:
    rc = require_ready("读取记忆")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        r = client.get(args.memory_id)
        if r is None:
            return fail(f"未找到 {args.memory_id}")
        human = json.dumps(r, ensure_ascii=False, indent=2, default=str)
        if str(r.get("layer") or "") == "l1_raw" or str(r.get("status") or "") == "shadow":
            # 影子节点不是「记忆」：抽取没产出可检索事实时它才会被留下。
            # 不写清楚，用户会拿 add 的旧 id 来 get，然后以为记忆坏了。
            human += ("\n\n提示：这是 l1_raw 影子节点（抽取未产出可检索事实），"
                      "检索不到它属正常。可检索节点用 search / list 查，"
                      "或看 add 返回的 memory_ids。")
        return ok(r, args.json, human)
    finally:
        client.close()


def cmd_history(args) -> int:
    """查一条记忆的变更历史（演化链）：每次改动的旧版本都在这里。"""
    rc = require_ready("查询记忆历史")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        items = client.history(args.memory_id) or []
        if args.json:
            return ok({"count": len(items), "memory_id": args.memory_id, "history": items}, True)
        if not items:
            print(f"（{args.memory_id} 没有变更记录——从没被 update 过）")
            return EXIT_OK
        lines = []
        for i, it in enumerate(items, 1):
            body = it.get("memory") or it.get("content") or it.get("text") or ""
            when = it.get("gmt_created") or it.get("created_at") or it.get("when") or ""
            event = it.get("event") or it.get("action") or ""
            head = f"{i:>2}. [{when}]{' ' + str(event) if event else ''} "
            body_lines = str(body).split("\n")
            lines.append(head + body_lines[0])
            pad = " " * len(head)
            for extra in body_lines[1:]:
                lines.append(pad + extra)
        return ok(items, False, "\n".join(lines))
    finally:
        client.close()
