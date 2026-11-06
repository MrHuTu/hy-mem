"""写路径命令：add / update / delete / forget / finalize / tags / archive。

S2 拆分（docs/specs/2026-11-02-cli-split-ports/）：本模块从 hymem/cli.py 按域拆入，
逻辑零变化——全局状态存于 cmds.base，跨模块经 _base 属性读写。
"""
from __future__ import annotations

from hymem.cmds.base import (  # noqa: F401
    CommitFailed, EXIT_USAGE, ExtractFailed, _add_kwargs, _bind_tags_to_new,
    _decode_stdin, _ids_of, _read_text, _snapshot_ids, _spawn_detached_worker,
    commit_extracted_with_retry, delete_memory_with_cascade, extract_phase, fail,
    find_derived_nodes, identity, json, make_client, ok, os,
    prepare_commit, require_ready, store, sys, update_memory,
)
from hymem.core.split import part_request_id, split_limited, truncation_warning


def _async_add_tail(spawned: bool, live: bool) -> str:
    """异步 add 的人类回执尾句（sync=已落库；async=仅入队，区别必须写清）。

    spawned 只代表 CreateProcess 成功，不保证消费进程存活（可能被宿主平台
    的 job object 连带杀掉）。不能再说「落库无需等待」。
    """
    if spawned:
        return ("已发起后台消费进程（3 线程消费，空闲 90 秒自退出）；"
                "异步=已入队，不等于已落库。用 hy-mem queue 看待处理是否归零，"
                "仍 pending 就跑 hy-mem worker --once")
    if live:
        return "后台 worker 在跑，稍后自动落库（异步=已入队，确认落库请用 hy-mem queue）"
    return "消费请跑：hy-mem worker --once"


def _cmd_add_scene(args, scene: str) -> int:
    """S1：生产力场景写入（working/coding）。SDK client.add(messages, scene=...) 整段。

    与 chat 管线完全独立（coding_memory*.db + 专用向量集合），不复用两段式/合批。
    CLI 在门口预检轨迹：SDK 对「显式 scene 但无 tool 轨迹」会静默回退 chat——
    那是陷阱（用户以为写了 coding 库实际进了 chat），这里拦住并明确报 E_NO_TRACE。
    """
    import json as _json

    # U4：lite / coding 关闭时 SDK 会把这次 scene 写入静默降级到 chat（零写 chat）。
    # 门口拦死比"写了但进错库"诚实。
    from hymem.core import config as _cfgsc

    _ok_scene, _why = _cfgsc.scene_status(getattr(args, "mode", None))
    if not _ok_scene:
        return fail(
            f"scene={scene} 当前配置不可用：{_why}；本条未写入",
            hint="要写记忆请去掉 --scene 走 chat，或按提示修正配置后重试",
            err_code="E_SCENE_UNAVAILABLE",
        )

    trace_path = getattr(args, "trace", None)
    if not trace_path:
        return fail(
            f"--scene {scene} 需要 --trace <messages.json>（含 tool_calls/role=tool 的 OpenAI messages 数组，- 为 stdin）",
            hint="生产力写入必须带工具调用轨迹；纯文本请去掉 --scene 走 chat",
            err_code="E_NO_TRACE",
        )
    raw = None
    if trace_path == "-":
        buf = getattr(sys.stdin, "buffer", None)
        raw = _decode_stdin(buf.read())[0] if buf else sys.stdin.read()
    else:
        from pathlib import Path as _P

        tp = _P(trace_path)
        if not tp.is_file():
            return fail(f"--trace 文件不存在：{trace_path}", err_code="E_NO_TRACE")
        raw = tp.read_text(encoding="utf-8")
    if not raw or not raw.strip():
        return fail("--trace 文件为空或不存在", err_code="E_NO_TRACE")
    try:
        messages = _json.loads(raw)
        if not isinstance(messages, list) or not messages:
            raise ValueError("不是 messages 数组")
    except ValueError as e:
        return fail(f"--trace 不是合法的 messages JSON 数组：{e}", err_code="E_NO_TRACE")
    has_trace = any(
        m.get("tool_calls") or m.get("role") == "tool" for m in messages if isinstance(m, dict)
    )
    if not has_trace:
        return fail(
            "轨迹中没有 tool_calls / role=tool——SDK 会静默回退 chat 管线，已拦截（零写入）",
            hint="生产力记忆的价值在工具轨迹；确认 trace 文件含 assistant.tool_calls 或 tool 消息，"
                 "或去掉 --scene 走 chat",
            err_code="E_NO_TRACE",
        )
    ws = getattr(args, "workspace", None)
    branch = getattr(args, "branch", None)
    if branch and not ws:
        return fail("--branch 需要同时传 --workspace（SDK 作用域契约）", EXIT_USAGE)

    ident = identity(args)
    lock_timeout = float(os.getenv("HY_MEM_ADD_LOCK_TIMEOUT") or 30)
    client = make_client(args.mode)
    try:
        # 生产力管线独立于 chat 两段式；首版保守持锁（§0 约束 2），
        # 后续实测与 chat WriteLock 无竞争再评估出锁。
        with store.WriteLock(timeout=lock_timeout):
            r = client.add(
                messages,
                scene=scene,
                workspace_id=ws or None,
                branch=branch or None,
                source=getattr(args, "source", None) or "cli",
                **ident,
            )
    except Exception as e:  # noqa: BLE001
        return fail(f"scene 写入失败：{type(e).__name__}: {e}",
                    hint=f"SDK {scene} 管线报错；检查 trace 格式与 LLM 配置（doctor）")
    if r.get("success") is not True:
        return fail(f"scene 写入失败：{r.get('error') or r.get('error_message') or r}")
    payload = {
        "ok": True, "scene": r.get("scene"), "request_id": r.get("request_id"),
        "fact_ids": r.get("fact_ids") or [], "experience_ids": r.get("experience_ids") or [],
        "ops_summary": r.get("ops_summary") or {},
        "fact_ops_summary": r.get("fact_ops_summary") or {},
        "chain_rounds": r.get("chain_rounds"),
        "workspace": ws or None, "branch": branch or None,
    }
    n_fact = len(payload["fact_ids"])
    n_exp = len(payload["experience_ids"])
    # --split 在 scene 下不生效（整段轨迹一次抽取，拆行会切断 tool 上下文）。
    # 不静默：与 MCP 回执同口径显式标注。
    if getattr(args, "split", False):
        payload["split_ignored"] = "scene"
        payload["warning"] = (
            "scene 写入按整条处理（整段工具轨迹一次抽取），未拆分；"
            "需要按条拆分请去掉 --scene，用 chat 的 text + --split"
        )
    human = (f"[{scene}] 事实 {n_fact} 条、经验 {n_exp} 条"
             + (f"、diff 链 {payload['chain_rounds']} 轮" if payload.get("chain_rounds") else "")
             + (f"（workspace={ws}）" if ws else "")
             + ("（--split 对 scene 不生效，按整条写入）" if payload.get("split_ignored") else ""))
    return ok(payload, args.json, human)


def _split_meta(args, parts: list[str], dropped: int,
                request_id: str | None) -> dict | None:
    """--split 回执元数据（不传 --split 时返回 None，回执与旧版一致）。

    多片且带 request_id 时，幂等 id 按片派生 `<rid>#<i>`——否则所有分片共用
    一个 id，SDK 会把第 2 片起当成重复，实际只落第一条。
    """
    if not getattr(args, "split", False):
        return None
    meta = {"requested": True, "parts": len(parts), "truncated": dropped,
            "estimated_extractions": len(parts)}
    if request_id and len(parts) > 1:
        meta["request_id_derived"] = True
        meta["request_id_prefix"] = f"{request_id}#"
    return meta


def cmd_add(args) -> int:
    rc = require_ready("写入记忆")
    if rc is not None:
        return rc
    # S1 scene 分流（2026-10-30 scene spec）：--scene working/coding 走生产力
    # 管线（独立存储，SDK client.add(messages, scene=...) 整段）；缺省 chat 不变。
    scene = getattr(args, "scene", None)
    if scene in ("working", "coding"):
        return _cmd_add_scene(args, scene)
    text = _read_text(args.text)
    if not text:
        return fail("没有输入内容（参数或 stdin）", EXIT_USAGE)
    tags = [t for t in (args.tag or []) if t.strip()]
    # metadata / memory_at / request_id 在进队列前就解析校验——坏参数当场报，
    # 别等 worker 消费时才炸（那时 job 已经进死信，排查成本高得多）
    try:
        extra = _add_kwargs(args)
    except ValueError as e:
        return fail(str(e), EXIT_USAGE)
    use_async = bool(getattr(args, "async_mode", False)) or os.getenv("HY_MEM_ASYNC") == "1"

    if use_async:
        # 队列 payload 必须可 JSON 序列化：datetime 转回 ISO 字符串，worker 端再解析
        queued_extra = dict(extra)
        if "memory_at" in queued_extra:
            queued_extra["memory_at"] = queued_extra["memory_at"].isoformat()
        parts, split_dropped = split_limited(text, bool(args.split))
        split_meta = _split_meta(args, parts, split_dropped, queued_extra.get("request_id"))

        def _enqueue_all(conn):
            out = []
            for i, p in enumerate(parts, 1):
                qe = dict(queued_extra)
                rid = part_request_id(qe.get("request_id"), i, len(parts))
                if rid:
                    qe["request_id"] = rid
                out.append(
                    store.enqueue(
                        conn,
                        {
                            "text": p,
                            "source": args.source,
                            "identity": identity(args),
                            "tags": tags,
                            **qe,
                        },
                        mode=args.mode,
                        max_attempts=int(os.getenv("HY_MEM_QUEUE_MAX_ATTEMPTS") or 3),
                    )
                )
            return out

        ids = store.transient_write(_enqueue_all)
        conn = store.connect()
        try:
            stats = store.queue_stats(conn)
            # 无人消费就自动拉起临时 worker：只认真实心跳（pid 非空），
            # `_spawn_intent` 占位行不代表进程活着。
            live_count, _beat = store.live_worker_info(conn)
        finally:
            conn.close()
        worker_live = live_count > 0
        spawned = False
        if stats["pending"] > 0 and not worker_live:
            spawned = _spawn_detached_worker()
        payload = {
            "ok": True, "queued": len(ids), "job_ids": ids, "tags": tags, "queue": stats,
            "workerLive": worker_live, "workerSpawned": spawned,
        }
        tail = _async_add_tail(spawned, worker_live)
        if split_meta:
            payload["split"] = split_meta
            if split_dropped:
                payload["warning"] = truncation_warning(len(parts), split_dropped)
            tail += (f"（原文按行拆成 {len(parts)} 条，预计 {split_meta['estimated_extractions']}"
                     " 次抽取；pro 单条分钟级，50 条会长时间占用队列）")
        human = f"已入队 {len(ids)} 个任务（job {ids[0]}..{ids[-1]}），待处理 {stats['pending']} 个。{tail}"
        return ok(payload, args.json, human)

    client = make_client(args.mode)
    try:
        parts, split_dropped = split_limited(text, bool(args.split))
        split_meta = _split_meta(args, parts, split_dropped,
                                 (extra.get("request_id") if "request_id" in extra else None))
        ident = identity(args)
        before, before_complete = _snapshot_ids(client, ident) if tags else (set(), True)
        all_ids: list[str] = []
        tagged: list[str] = []
        last: dict = {}
        raw_ids: list[str] = []
        tag_note = ""
        tagged_once = False

        def _bind_all_once() -> None:
            """多段 add 只做一次 after 快照：before 复用，避免每段全量扫描。"""
            nonlocal tag_note, tagged_once
            if not tags or tagged_once or not all_ids:
                return
            tagged_once = True
            fresh, note = _bind_tags_to_new(
                client, ident, before, all_ids, tags, before_complete=before_complete,
            )
            tagged.extend(fresh)
            if note:
                tag_note = note

        for i, p in enumerate(parts, 1):
            # 分片幂等 id：多片时 `<rid>#<i>`，单片/未传时原样（不传 request_id 行为不变）
            part_extra = dict(extra)
            _rid = part_request_id(part_extra.get("request_id"), i, len(parts))
            if _rid:
                part_extra["request_id"] = _rid
            # R4 两段式（2026-10-29）：抽取（LLM 网络调用）在锁外，可多进程并行；
            # WriteLock 只包 SDK 标准提交段（L1 存档 → L2 reconcile → SHADOW），
            # 口径对齐 SDK 内部 _reconcile_user_lock 的串行范围。失败零写入。
            # R3 fail-fast：抢不到锁立即失败（默认 5s），多条/并发正路是 --async。
            lock_timeout = float(os.getenv("HY_MEM_ADD_LOCK_TIMEOUT") or 5)
            # R4 锁探测前置：锁被占时不白烧 LLM 抽取，立即 E_LOCK fail-fast。
            # （探测通过后 extract 期间锁可能被抢——那是少数竞态，正式抢锁段兜底。）
            try:
                with store.WriteLock(timeout=lock_timeout):
                    pass
            except TimeoutError:
                _bind_all_once()
                mid_note = f"（此前分片已入库 {len(all_ids)} 条）" if all_ids else ""
                return fail(
                    f"另一进程正在写入，{lock_timeout:g}s 内未拿到写锁，本条未入库（零写入）{mid_note}",
                    hint="单条可稍后重试；多条/并发务必用 hy-mem add --async（入队即返回，3 车道消费）；"
                         "超时可用 HY_MEM_ADD_LOCK_TIMEOUT（秒）调整",
                    err_code="E_LOCK",
                )
            try:
                agent_result = extract_phase(client, p, source=args.source, **part_extra, **ident)
            except ExtractFailed as e:
                _bind_all_once()
                mid_note = f"（此前分片已入库 {len(all_ids)} 条）" if all_ids else ""
                return fail(
                    f"LLM 抽取失败，本条未入库（零写入）{mid_note}：{e.message}",
                    hint=f"SDK 错误码 {e.sdk_code or 'UNKNOWN'}；多为 LLM/网络瞬时故障，单条可直接重试；"
                         "反复失败跑 hy-mem doctor 检查 LLM 配置",
                    err_code="E_EXTRACT",
                )
            # R4.7 预计算出锁：L1 向量 + 本批 dedup 在抢锁前算好
            prepared = prepare_commit(client, p, agent_result, source=args.source, **part_extra, **ident)
            try:
                with store.WriteLock(timeout=lock_timeout):
                    last = commit_extracted_with_retry(
                        client, p, agent_result, prepared=prepared,
                        source=args.source, **part_extra, **ident,
                    )
            except TimeoutError:
                _bind_all_once()
                mid_note = f"（此前分片已入库 {len(all_ids)} 条）" if all_ids else ""
                return fail(
                    f"另一进程正在写入，{lock_timeout:g}s 内未拿到写锁，本条未入库（零写入）{mid_note}",
                    hint="单条可稍后重试；多条/并发务必用 hy-mem add --async（入队即返回，3 车道消费）；"
                         "超时可用 HY_MEM_ADD_LOCK_TIMEOUT（秒）调整",
                    err_code="E_LOCK",
                )
            except CommitFailed as e:
                _bind_all_once()
                mid_note = f"（此前分片已入库 {len(all_ids)} 条）" if all_ids else ""
                return fail(
                    f"落库提交失败{mid_note}：{e}",
                    hint="原始对话已存档（L1），可检索的结论可能缺失；重跑本命令会把本条重新提交，"
                         "reconcile 会吸收重复；反复失败跑 hy-mem doctor",
                    err_code="E_COMMIT",
                )
            rid = last.get("memory_id")
            if rid:
                raw_ids.append(str(rid))
            ids = _ids_of(last)
            all_ids.extend(ids)
        _bind_all_once()
        if len(parts) > 1:
            payload = {"ok": True, "count": len(all_ids), "memory_ids": all_ids,
                       "tagged_ids": tagged, "tags": tags,
                       "raw_memory_ids": raw_ids}
            if tag_note:
                payload["tag_note"] = tag_note
            if split_meta:
                payload["split"] = split_meta
                if split_dropped:
                    payload["warning"] = truncation_warning(len(parts), split_dropped)
            return ok(payload, args.json, f"已写入 {len(all_ids)} 条：{', '.join(i[:8] for i in all_ids)}")
        payload = dict(last)
        payload["memory_ids"] = all_ids
        # memory_id 一律指向**可检索节点**（0.9.10 / PRD F8）。SDK 的
        # result.memory_id 在抽取路径上是 l1_raw（status=shadow），拿它去
        # get/delete 只会看到影子内容；可检索/可删除的是 memory_ids[0]。
        # 原始 id 保留在 raw_memory_id，排查链路时还能对得上。
        if all_ids:
            if raw_ids and raw_ids[0] != all_ids[0]:
                payload["raw_memory_id"] = raw_ids[0]
            payload["memory_id"] = all_ids[0]
        if tags:
            payload["tagged_ids"] = tagged
            payload["tags"] = tags
        if tag_note:
            payload["tag_note"] = tag_note
        if split_meta:
            payload["split"] = split_meta
            if split_dropped:
                payload["warning"] = truncation_warning(len(parts), split_dropped)
        suffix = f"，标签 {','.join(tags)}" if tags else ""
        if tag_note:
            suffix += f"（{tag_note}）"
        return ok(payload, args.json, f"已写入 memory_id={(all_ids[0] if all_ids else payload.get('memory_id') or '?')}{suffix}")
    finally:
        client.close()


def cmd_delete(args) -> int:
    rc = require_ready("删除记忆")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        if getattr(args, "dry_run", False):
            r = client.get(args.memory_id)
            if r is None:
                return fail(f"未找到 {args.memory_id}（dry-run 未做任何改动）")
            derived = find_derived_nodes(client, args.memory_id)
            payload = {"ok": True, "dryRun": True, "wouldDelete": r, "derivedNodes": derived}
            note = f"（含 {len(derived)} 个派生节点将级联清理）" if derived else ""
            return ok(payload, args.json, "[dry-run] 将删除：\n" + json.dumps(r, ensure_ascii=False, indent=2, default=str) + note)
        r = delete_memory_with_cascade(client, args.memory_id)
        purged = r.get("derived_purged") or []
        cascade_note = r.get("cascade_note") or ""
        # SDK 公开返回没有 found 键（success=False 时区分「没这条」与「没删净」
        # 靠 partial / error_code）；vdb_deleted 因上游恒 True 不可信，不作判据。
        if r.get("success"):
            payload = dict(r)
            payload["derived_purged"] = purged
            if cascade_note:
                payload["cascade_note"] = cascade_note
            human = f"已删除 {args.memory_id}"
            if purged:
                human += f"，级联清理派生节点 {len(purged)} 个"
            if cascade_note:
                human += f"；警告：{cascade_note}"
            return ok(payload, args.json, human)
        if r.get("partial") or r.get("error_code"):
            return fail(
                "删除未完成（部分存储未删净）",
                extra={
                    "partial": bool(r.get("partial")),
                    "vdb_deleted": bool(r.get("vdb_deleted")),
                    "deleted_count": r.get("deleted_count", 0),
                    "error_code": r.get("error_code"),
                },
            )
        return fail(
            f"未找到 {args.memory_id}",
            hint="可先 hy-mem search 关键词定位残留条目（命中即其自身 id），再 delete 该 id",
            extra={"deleted_count": r.get("deleted_count", 0)},
        )
    finally:
        client.close()


def cmd_update(args) -> int:
    """改一条记忆的内容。走 SDK 演化链：旧版本进 SHADOW 保留，不丢历史。"""
    rc = require_ready("更新记忆")
    if rc is not None:
        return rc
    content = _read_text(getattr(args, "content", None))
    if not content:
        return fail("没有新内容（位置参数或 stdin）", EXIT_USAGE)
    client = make_client(args.mode)
    try:
        r = update_memory(client, args.memory_id, content)
        if r is None:
            return fail(f"未找到 {args.memory_id}（可先 hy-mem list 拿 id）")
        if isinstance(r, dict) and r.get("success") is False:
            return fail(
                f"更新失败：{r.get('error_message') or r.get('error') or 'unknown'}",
                extra={"error_code": r.get("error_code"), "partial": bool(r.get("partial"))},
            )
        return ok(
            r, args.json,
            f"已更新 {args.memory_id}（旧版本保留在演化链，hy-mem history {args.memory_id[:8]} 可查）",
        )
    finally:
        client.close()


def cmd_forget(args) -> int:
    rc = require_ready("按用户清空记忆")
    if rc is not None:
        return rc
    if not args.user:
        return fail("forget 必须指定 --user，避免误删全局记忆", EXIT_USAGE)
    client = make_client(args.mode)
    try:
        if getattr(args, "dry_run", False):
            stats = client.list_memories(user_id=args.user, limit=1000)
            items = stats.get("memories") if isinstance(stats, dict) else stats
            n = len(items or [])
            payload = {"ok": True, "dryRun": True, "scope": {"user_id": args.user}, "count": n,
                       "note": "上限 1000 条样本；实际删除数可能更多"}
            return ok(payload, args.json, f"[dry-run] 范围内至少 {n} 条，未做任何改动")
        if not args.yes:
            scope = {"user_id": args.user}
            for k in ("agent", "session"):
                if getattr(args, k, None):
                    scope[f"{k}_id"] = getattr(args, k)
            print(f"将删除范围 {json.dumps(scope, ensure_ascii=False)} 下的全部记忆。", file=sys.stderr)
            print("确认请加 --yes", file=sys.stderr)
            return EXIT_USAGE
        kwargs = {"user_id": args.user}
        if args.session:
            kwargs["session_ids"] = [args.session]
        if args.agent:
            kwargs["agent_ids"] = [args.agent]
        with store.WriteLock():
            r = client.delete_all(**kwargs)
        return ok(r, args.json, "已清空")
    finally:
        client.close()


def cmd_finalize(args) -> int:
    if not args.user:
        return fail("finalize 必须指定 --user", EXIT_USAGE)
    # S2（docs/specs/2026-11-02-sdk-alignment-gaps/）：episode 叙事与 evolve 同根因
    # ——SDK auto 检测对技术型中文失灵（zh_ratio≥0.7 判 en）。缺省复用
    # evolve.language 配置；两处都空才交给 SDK auto。
    lang = str(getattr(args, "language", None) or "").strip().lower()
    if not lang:
        from hymem.core import config as _cfgmod

        lang = str((_cfgmod.load().get("evolve") or {}).get("language") or "").strip().lower()
    client = make_client(args.mode)
    try:
        r = client.finalize_session(
            user_id=args.user,
            agent_id=args.agent or "default_agent",
            session_id=args.session or "default_session",
            **({"language": lang} if lang else {}),
        )
        return ok(r, args.json, "session 已收尾" + (f"（叙事语言 {lang}）" if lang else ""))
    finally:
        client.close()


def cmd_tags(args) -> int:
    if getattr(args, "refresh", False):
        rc = require_ready("从记忆同步自动标签")
        if rc is not None:
            return rc
    conn = store.connect()
    try:
        refreshed = 0
        if args.refresh:
            # 从 SDK 侧拉全量，把 LLM 自动标签补进本地索引（含孤儿清理）
            client = make_client(args.mode)
            try:
                alive: set[str] = set()
                items: list[dict] = []
                offset = 0
                while offset < args.max_refresh:
                    r = client.list_memories(limit=100, offset=offset, **identity(args))
                    batch = (r.get("vdb") or {}).get("memories") or []
                    if not batch:
                        break
                    items.extend(batch)
                    offset += len(batch)
                if args.user:
                    hist = client.get_recent_history(user_id=args.user, limit=args.max_refresh) or []
                    items.extend([h for h in hist if isinstance(h, dict)])
                for it in items:
                    mid = it.get("memory_id") or it.get("id")
                    if not mid:
                        continue
                    alive.add(str(mid))
                    tg = it.get("tags") or []
                    if tg:
                        refreshed += store.bind_tags(conn, [str(mid)], [str(t) for t in tg], "auto")
                pruned = store.prune(conn, alive) if alive else 0
            finally:
                client.close()
        else:
            pruned = 0
        inv = store.tag_inventory(conn, args.min_count)
    finally:
        conn.close()

    if args.json:
        return ok({"ok": True, "count": len(inv), "tags": inv, "refreshed": refreshed, "pruned": pruned}, True)
    if not inv:
        return ok(inv, False, "（还没有标签。用 add --tag 加，或 tags --refresh 从已有记忆同步）")
    lines = [f"{'标签':<24}{'合计':>6}{'手工':>6}{'自动':>6}"]
    for t in inv:
        lines.append(f"{t['tag']:<24}{t['n']:>6}{t['user_n']:>6}{t['auto_n']:>6}")
    if refreshed or pruned:
        lines.append(f"\n同步：新增自动标签 {refreshed} 条，清理失效 {pruned} 条")
    return ok(inv, False, "\n".join(lines))


def cmd_untag(args) -> int:
    if not args.memory_id and not args.tag:
        return fail("untag 至少要给 <memory_id> 或 --tag", EXIT_USAGE)
    if not args.memory_id and args.tag and not args.yes:
        return fail(f"将删除全局标签「{args.tag}」的所有绑定，确认请加 --yes", EXIT_USAGE)
    conn = store.connect()
    try:
        n = store.untag(conn, args.memory_id, args.tag)
    finally:
        conn.close()
    return ok({"ok": True, "removed": n}, args.json, f"已移除 {n} 条标签绑定")


def cmd_archive(args) -> int:
    conn = store.connect()
    try:
        if args.by == "tag":
            rows = store.tag_inventory(conn, args.min_count)
            data = [{"group": r["tag"], "count": r["n"]} for r in rows]
        else:
            data = []
            client = make_client(args.mode)
            try:
                counter: dict[str, int] = {}
                offset = 0
                while offset < args.max_scan:
                    r = client.list_memories(limit=100, offset=offset, **identity(args))
                    batch = (r.get("vdb") or {}).get("memories") or []
                    if not batch:
                        break
                    for it in batch:
                        layer = str(it.get("layer") or "unknown")
                        counter[layer] = counter.get(layer, 0) + 1
                    offset += len(batch)
                data = [{"group": k, "count": v} for k, v in sorted(counter.items(), key=lambda x: -x[1])]
            finally:
                client.close()
    finally:
        conn.close()

    if args.json:
        return ok({"ok": True, "by": args.by, "groups": data}, True)
    if not data:
        return ok(data, False, "（无数据）")
    width = max(len(str(d["group"])) for d in data) + 2
    lines = [f"按 {args.by} 归档："]
    total = sum(d["count"] for d in data) or 1
    for d in data:
        bar = "█" * max(1, int(d["count"] / total * 30))
        lines.append(f"  {str(d['group']):<{width}}{d['count']:>5}  {bar}")
    return ok(data, False, "\n".join(lines))
