# -*- coding: utf-8 -*-
"""R4（2026-10-29）：同步 add 两段式 —— 遵守 SDK 设计规范的锁粒度收窄。
S2（2026-10-30 batch-commit）：batch_commit 批量提交段。


背景（docs/specs/2026-10-29-fix-delete-ghost-and-add-readonly §R4）：
hy-mem 的跨进程 WriteLock 此前覆盖整个 client.add（含 LLM 抽取 8~40s），
导致同步 add 实际串行窗口过大。SDK 内部并发模型本身就是细粒度的
（writer.py: _reconcile_user_lock 只串行 reconcile 提交段），粗的是我们
这层锁。本模块把同步 add 拆成两段，锁只包 SDK 标准提交段：

  Phase 1 extract_phase（锁外，可并行）：
      mem_agent.process_add(mode=EXTRACT_ONLY) —— 纯 LLM 网络调用，
      无共享状态。tags 预取（只读）也在这里。

  Phase 1.5 prepare_commit（锁外，R4.7 追加）：
      L1 原文 embed + facts 收集 + 本批 dedup（含各 fact 向量）+
      WriteRequest 构造。纯函数段不读不写库状态（dedup 是本批内互相
      判重，writer.py:452 实读确认不查库）——此前在锁内属「陪绑」。

  Phase 2 commit_extracted（锁内，调用方持 WriteLock 后调用）：
      逐行对齐 MemoryWriter.write() 的提交段（SDK 设计规范，
      writer.py 模块注释「原始内容始终存为 L1_RAW → 提取高层信息
      → 提取成功后 L1_RAW 降级 SHADOW」）：
        1. embed → MemoryNode(L1_RAW, ACTIVE) → upsert → memory_id（L1 存档）
        2. basic_info → upsert_basic_profile（L0 演化链，best-effort）
        3. _collect_new_memories → [>=2 条] _dedup_extracted
        4. _reconcile_and_store(source_raw_memory_id=memory_id)   ← L2 挂 L1 根
           （RECONCILE_ENABLED=false 时 _direct_store，同样透传根 id）
        5. intentions → _store_intentions（L7，best-effort）
        6. L1 → SHADOW（成功路径；reconcile 失败时留 ACTIVE，同 SDK
           「数据可能还有救，留档待重试」语义）

与 SDK 标准 write() 的显式差异（有意为之，非遗漏）：
  - 顺序颠倒：SDK 是 L1 先入库再 extract（extract 失败时 L1 留 ACTIVE）；
    两段式 extract 在锁外先行，失败即零写入 fail-fast（R2 语义）。
  - 观测面省略：tracer span / pipeline step 埋点 / WRITE_SUMMARY 等
    pipeline_logs 不复刻（数据面完整，观测面归 SDK 标准 add 路径）。
  - rolling summary / session_episode 段不触发（hy-mem 同步 add 只传纯文本，
    SDK 对纯文本路径这些段本来就是 no-op 或可选）。

内部耦合点（SDK 升级须回归本文件，全部集中在 _writer_of 与两函数内）：
  client._registry.get_writer / _loop_thread、writer.mem_agent /
  embed_service / _get_vector_store / _collect_existing_tags /
  _collect_new_memories / _dedup_extracted / _reconcile_and_store /
  _direct_store / _collect_intentions / _store_intentions / config.*、
  MemoryNode / MemoryLayer / MemoryStatus / SourceType / WriteRequest、
  upsert_basic_profile、writer._RECONCILE_ENABLED。
不修改 SDK 源码；SDK 若官方提供「extracted + L1 存档」公开入口，应替换。
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ExtractFailed(Exception):
    """Phase 1 LLM 抽取失败（零写入，本条可整条重试）。

    携带 SDK 的错误码与原始信息，供 errors.classify_error 归类出口。
    """

    def __init__(self, sdk_code: Any, message: str):
        self.sdk_code = sdk_code
        self.message = message
        super().__init__(f"[{sdk_code}] {message}" if sdk_code else message)


class CommitFailed(Exception):
    """Phase 2 提交段失败（可能已部分写入：L1 存档节点已 upsert）。"""


# ---------------------------------------------------------------------------
# SDK 装配（内部耦合集中点）
# ---------------------------------------------------------------------------


def _writer_of(client: object):
    """拿 MemoryWriter 实例（SDK client 内部 registry，与 client.add 同源）。"""
    reg = getattr(client, "_registry", None)
    if reg is None:
        raise RuntimeError("SDK client 结构变化：找不到 _registry")
    return client._loop_thread.run(reg.get_writer())


def _run(client: object, coro):
    """在 SDK 的内部事件循环线程上执行协程（与 client.add 同通道）。"""
    return client._loop_thread.run(coro)


# ---------------------------------------------------------------------------
# reconcile 准入闸门 / 落库结果复核（P1 + P2）
# ---------------------------------------------------------------------------

# reconcile 候选窗口是「宽进」（约 0.4 起），LLM 会把语义不相关的新事实
# 误合并进既有条目。低于该相似度的事实直接走 _direct_store（ADD），
# 只有确实接近既有记忆的事实才交给 reconcile 判合并/演化。
_RECONCILE_MIN_SIM_DEFAULT = 0.85


def _reconcile_min_sim() -> float:
    raw = (os.getenv("HY_MEM_RECONCILE_MIN_SIM") or "").strip()
    if not raw:
        return _RECONCILE_MIN_SIM_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        print(
            f"[hy-mem] 非法环境变量 HY_MEM_RECONCILE_MIN_SIM={raw!r}，"
            f"按默认 {_RECONCILE_MIN_SIM_DEFAULT} 处理",
            file=sys.stderr,
        )
        return _RECONCILE_MIN_SIM_DEFAULT
    return max(0.0, min(1.0, value))


def _best_existing_similarity(run, vs, embedding: Any, user_id: str) -> float:
    """新事实与既有 L2/L4 记忆的最高相似度（0~1）。"""
    from hy_memory.models.memory import MemoryLayer, MemoryStatus

    hits = run(
        vs.search(
            embedding,
            user_id=user_id,
            layers=[MemoryLayer.L2_FACT, MemoryLayer.L4_IDENTITY],
            limit=1,
            status_filter=[MemoryStatus.ACTIVE],
            only_latest=True,
        )
    )
    if not hits:
        return 0.0
    top = hits[0] or {}
    try:
        return float(top.get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _split_facts_for_reconcile(run, vs, texts, metas, precomputed, user_id):
    """按相似度闸门把事实分成 reconcile 组与直存组。

    返回 (recon_texts, recon_metas, direct_texts, direct_metas, scores)。
    查询失败时保守地把事实交给 reconcile（不拦截写入）。
    """
    gate = _reconcile_min_sim()
    recon_texts, recon_metas, direct_texts, direct_metas = [], [], [], []
    scores: dict = {}
    for text, meta in zip(texts, metas):
        emb = (precomputed or {}).get(text)
        if not emb or not user_id:
            recon_texts.append(text)
            recon_metas.append(meta)
            continue
        try:
            best = _best_existing_similarity(run, vs, emb, user_id)
        except Exception:  # noqa: BLE001  闸门查询失败不阻断写入
            recon_texts.append(text)
            recon_metas.append(meta)
            continue
        scores[text] = best
        if best < gate:
            direct_texts.append(text)
            direct_metas.append(meta)
        else:
            recon_texts.append(text)
            recon_metas.append(meta)
    return recon_texts, recon_metas, direct_texts, direct_metas, scores


def _verify_memory_ids(run, vs, ids: list[str]) -> tuple[list[str], list[str]]:
    """复核落库 id 真实存在，过滤 reconcile LLM 幻觉出的 id。"""
    if not ids:
        return [], []
    unique = list(dict.fromkeys(ids))
    try:
        nodes = run(vs.get_by_ids(unique)) if hasattr(vs, "get_by_ids") else []
        found = {getattr(n, "node_id", "") for n in (nodes or [])}
    except Exception:  # noqa: BLE001  复核失败时不误删合法 id
        return unique, []
    return [i for i in unique if i in found], [i for i in unique if i not in found]


# ---------------------------------------------------------------------------
# Phase 1：抽取（锁外）
# ---------------------------------------------------------------------------


def extract_phase(client: object, text: str, **kw):
    """LLM 抽取：读库取已有 tags + process_add(EXTRACT_ONLY)。不写任何数据。

    kw 与 client.add 同签名（user_id / agent_id / session_id / source /
    metadata / memory_at / request_id / meta）。
    返回 AgentResult（SDK 对象）；失败抛 ExtractFailed。
    """
    from hy_memory.agent.mem_agent import ProcessMode

    writer = _writer_of(client)
    user_id = str(kw.get("user_id") or "")
    agent_id = str(kw.get("agent_id") or "default_agent")
    vs = _run(client, writer._get_vector_store())

    # 已有 tags 预取（只读；SDK 侧带 TTL 缓存，失败静默空列表）
    existing_tags = _run(
        client, writer._collect_existing_tags(vs, user_id, agent_id)
    )

    # EMPTY_RESPONSE 瞬态重试（R4.9）：并发负载下网关偶发空响应（风暴实测
    # 1/8 概率）。与 R2 哲学一致——瞬时故障换次重跑，不是干净失败。
    # 仅对 EMPTY_RESPONSE 重试；其余错误码（配额/鉴权/内容审查）直接抛。
    attempts = 3
    last_err: tuple = (None, "LLM 抽取失败")
    for i in range(attempts):
        result = _run(
            client,
            writer.mem_agent.process_add(
                content=text,
                context={"uid": user_id, "agent_id": agent_id},
                mode=ProcessMode.EXTRACT_ONLY,
                memory_at=kw.get("memory_at"),
                existing_tags=existing_tags,
                basic_profile_fields=writer.config.basic_profile.effective_fields(),
            ),
        )
        if result is not None and getattr(result, "success", False):
            return result
        code = getattr(result, "error_code", None) if result is not None else None
        last_err = (code, (getattr(result, "error", None) if result is not None else None) or "LLM 抽取失败")
        if code != "EMPTY_RESPONSE":
            break  # 非瞬态错误码：重试无益
        import time as _t

        _t.sleep(0.5 * (i + 1))
    raise ExtractFailed(last_err[0], last_err[1])


# ---------------------------------------------------------------------------
# Phase 1.5：提交预计算（锁外）——纯函数段，不读不写库状态
# ---------------------------------------------------------------------------


def prepare_commit(client: object, text: str, agent_result: Any, **kw) -> dict:
    """锁外预计算 commit 所需的一切：L1 向量、facts 收集/dedup、WriteRequest。

    这些步骤不依赖库当前状态（dedup 是本批内互相判重，深读 writer.py:452
    确认不查库），放锁内是「陪绑」——白占锁时间。extract 后、抢锁前调用；
    结果 dict 整体传给 commit_extracted(prepared=...)。
    失败语义：embed/dedup 失败不致命（SDK 同款 non-fatal 降级——commit 内
    会按需重算），此处只把可算的算好。
    """
    from hy_memory.pipelines.base import WriteRequest

    writer = _writer_of(client)
    run = lambda coro: _run(client, coro)  # noqa: E731

    user_id = str(kw.get("user_id") or "")
    agent_id = str(kw.get("agent_id") or "default_agent")
    session_id = str(kw.get("session_id") or "default_session")
    source = str(kw.get("source") or "default")
    request_id = str(kw.get("request_id") or "") or str(uuid.uuid4())

    wr = WriteRequest(
        content=text,
        user_id=user_id,
        agent_id=agent_id,
        source=source,
        session_id=session_id,
        content_type="text",
        extra={"agent_mode": "full", "mode": getattr(client, "_mode", "pro")},
        memory_at=kw.get("memory_at"),
        request_id=request_id,
        client_meta=kw.get("meta"),
    )

    # L1 原文向量（纯函数：文本 → 向量）
    l1_embedding = None
    try:
        l1_embedding, _ = run(writer.embed_service.embed_queued_with_meta(text))
    except Exception:  # noqa: BLE001
        l1_embedding = None  # commit 内兜底重算

    # facts 收集 + 本批 dedup（纯本批计算，不查库）
    info = dict(getattr(agent_result, "extracted_info", None) or {})
    basic_info = info.pop("basic_info", None)  # commit 内走 L0 演化链
    new_texts, new_metas = writer._collect_new_memories(info)
    precomputed: dict = {}
    if len(new_texts) >= 2:
        try:
            new_texts, new_metas, precomputed = run(
                writer._dedup_extracted(new_texts, new_metas, wr, request_id)
            )
        except Exception:  # noqa: BLE001
            precomputed = {}
    # 单条事实也要有向量：reconcile 准入闸门依赖它判相似度（锁外预算，
    # 锁内 reconcile 复用同一份向量，不额外增加提交段耗时）。
    missing = [t for t in new_texts if t not in precomputed]
    if missing:
        try:
            embs = run(writer.embed_service.embed_batch(missing))
            for one, emb in zip(missing, embs or []):
                if emb:
                    precomputed[one] = emb
        except Exception:  # noqa: BLE001  向量缺失时闸门跳过，回落 reconcile
            pass

    return {
        "wr": wr,
        "request_id": request_id,
        "l1_embedding": l1_embedding,
        "basic_info": basic_info,
        "info": info,
        "new_texts": new_texts,
        "new_metas": new_metas,
        "precomputed": precomputed,
    }


# ---------------------------------------------------------------------------
# Phase 2：提交（锁内，SDK 标准提交段）
# ---------------------------------------------------------------------------


def commit_extracted(client: object, text: str, agent_result: Any, *, prepared: dict | None = None, **kw) -> dict:
    """SDK 标准提交段：L1 存档 → L2 reconcile（挂根）→ L1→SHADOW。

    调用方必须已持有 hy-mem WriteLock。返回与 client.add 同形态的 dict。
    prepared：prepare_commit 的预计算结果（锁外算好的向量/dedup/请求对象）；
    缺省时锁内现算（向后兼容旧调用）。
    """
    from hy_memory.models.memory import MemoryLayer, MemoryNode, MemoryStatus, SourceType
    from hy_memory.pipelines import writer as writer_mod
    from hy_memory.agent.tools.basic_profile import upsert_basic_profile

    t0 = time.monotonic()
    writer = _writer_of(client)
    run = lambda coro: _run(client, coro)  # noqa: E731

    if prepared is None:
        prepared = prepare_commit(client, text, agent_result, **kw)
    wr = prepared["wr"]
    request_id = prepared["request_id"]
    user_id = wr.user_id
    agent_id = wr.agent_id or "default_agent"
    session_id = wr.session_id or "default_session"
    source = wr.source
    memory_at = wr.memory_at

    vs = run(writer._get_vector_store())

    # 1. L1 存档（writer.write() 同款：L1_RAW / ACTIVE / EXPLICIT）。
    #    向量已锁外算好（prepared.l1_embedding），锁内只在缺失时兜底重算。
    embedding = prepared["l1_embedding"]
    if embedding is None:
        embedding, _ = run(writer.embed_service.embed_queued_with_meta(text))
    mem_node = MemoryNode(
        user_id=user_id,
        agent_id=agent_id or "default_agent",
        session_id=session_id or "default_session",
        source=source,
        layer=MemoryLayer.L1_RAW,
        content=text,
        source_type=SourceType.EXPLICIT,
        status=MemoryStatus.ACTIVE,
        is_latest=True,
        embedding=embedding,
        memory_at=memory_at or datetime.now(),
        custom={},
    )
    memory_id = run(vs.upsert(mem_node))

    info = dict(prepared["info"])
    basic_info = prepared["basic_info"]

    # 2. basic_info → L0 演化链（best-effort，SDK 同款 non-fatal）
    if isinstance(basic_info, dict) and basic_info:
        try:
            run(
                upsert_basic_profile(
                    user_id=user_id,
                    kv=basic_info,
                    vector_store=vs,
                    embed_service=writer.embed_service,
                    allowed_fields=list(
                        writer.config.basic_profile.effective_fields().keys()
                    ),
                )
            )
        except Exception:  # noqa: BLE001
            pass

    # 3+4. facts → L2（reconcile 判重/合并/演化，source_raw_memory_id 挂 L1 根）。
    #      dedup 与向量已锁外算好（prepared），锁内只剩判定 LLM + 落库。
    new_texts = prepared["new_texts"]
    new_metas = prepared["new_metas"]
    precomputed = prepared["precomputed"]
    recon_error = None
    stored_ids: list = []
    ops_counts: dict = {}
    recon_tokens: dict = {}
    all_texts = list(new_texts)
    all_metas = list(new_metas)
    sim_scores: dict = {}
    direct_texts: list = []
    direct_metas: list = []
    direct_ids: list = []
    recon_metas: list = []
    rejected_ids: list = []
    reconcile_store_ran = False
    stored_via = "reconcile" if all_texts else "none"
    if new_texts:
        recon = writer_mod._RECONCILE_ENABLED
        if recon:
            # 相似度闸门（P2）：语义不相关的新事实不交给 reconcile 合并，
            # 直接 ADD，避免被误并入既有条目后正文静默丢失。
            new_texts, new_metas, direct_texts, direct_metas, sim_scores = (
                _split_facts_for_reconcile(
                    run, vs, new_texts, new_metas, precomputed, user_id
                )
            )
        recon_metas = list(new_metas)
        if new_texts:
            if recon:
                reconcile_store_ran = True
                stored_ids, recon_error, ops_counts, recon_tokens = run(
                    writer._reconcile_and_store(
                        new_memory_texts=new_texts,
                        new_memories_meta=new_metas,
                        request=wr,
                        vector_store=vs,
                        req_id=request_id,
                        source_raw_memory_id=memory_id,
                        precomputed_embeddings=precomputed,
                    )
                )
            else:
                stored_via = "direct"
                stored_ids, recon_error, ops_counts, recon_tokens = run(
                    writer._direct_store(
                        new_memories_meta=new_metas,
                        request=wr,
                        vector_store=vs,
                        req_id=request_id,
                        source_raw_memory_id=memory_id,
                        precomputed_embeddings=precomputed,
                    )
                )
            if recon_error is not None:
                # reconcile 失败：L1 留 ACTIVE（SDK「数据可救」语义），整体判失败
                raise CommitFailed(
                    f"RECONCILE_FAILED: {recon_error}; l1_memory_id={memory_id}"
                )
        if direct_texts:
            direct_ids, d_err, d_counts, d_tokens = run(
                writer._direct_store(
                    new_memories_meta=direct_metas,
                    request=wr,
                    vector_store=vs,
                    req_id=request_id,
                    source_raw_memory_id=memory_id,
                    precomputed_embeddings={
                        t: precomputed[t] for t in direct_texts if t in precomputed
                    },
                )
            )
            if d_err is not None:
                raise CommitFailed(
                    f"DIRECT_STORE_FAILED: {d_err}; l1_memory_id={memory_id}"
                )
            stored_ids.extend(direct_ids)
            if ops_counts:
                ops_counts["add"] = int(ops_counts.get("add") or 0) + len(direct_ids)
                ops_counts["total"] = int(ops_counts.get("total") or 0) + len(direct_ids)
            recon_tokens = {
                k: int(recon_tokens.get(k) or 0) + int(d_tokens.get(k) or 0)
                for k in set(recon_tokens) | set(d_tokens)
            }

        # 全部事实都被闸门拦下时，本次写入实质是直存，回执不能写 reconcile
        if direct_ids and not reconcile_store_ran:
            stored_via = "direct"

        # 落库结果复核（P1）：reconcile 偶尔返回库里不存在的 id（LLM 幻觉），
        # 直接透传会让调用方以为写成功却检索不到。
        stored_ids, rejected_ids = _verify_memory_ids(run, vs, stored_ids)

        # reconcile 返回空操作（LLM 判定「无需变更」）或全为幻觉 id 时，
        # 回退直存，避免新增内容只留一条检索不到的 L1 影子。
        if not stored_ids:
            fb_ids, fb_err, _, fb_tokens = run(
                writer._direct_store(
                    # 直存组已有可检索节点时只回退 reconcile 组，避免重复写一份
                    new_memories_meta=recon_metas if direct_ids else all_metas,
                    request=wr,
                    vector_store=vs,
                    req_id=request_id,
                    source_raw_memory_id=memory_id,
                    precomputed_embeddings=precomputed,
                )
            )
            if fb_err is not None:
                raise CommitFailed(
                    f"DIRECT_FALLBACK_FAILED: {fb_err}; l1_memory_id={memory_id}"
                )
            stored_ids = list(fb_ids)
            stored_via = "direct_fallback"
            if ops_counts:
                ops_counts["add"] = int(ops_counts.get("add") or 0) + len(stored_ids)
                ops_counts["total"] = int(ops_counts.get("total") or 0) + len(stored_ids)
            recon_tokens = {
                k: int(recon_tokens.get(k) or 0) + int(fb_tokens.get(k) or 0)
                for k in set(recon_tokens) | set(fb_tokens)
            }

    # 5. intentions → L7（best-effort，SDK 同款开关与跳过逻辑）
    if getattr(writer.config.llm, "enable_intention", True):
        try:
            intentions = writer._collect_intentions(info)
            if intentions:
                run(
                    writer._store_intentions(
                        intentions=intentions,
                        request=wr,
                        vector_store=vs,
                        req_id=request_id,
                    )
                )
        except Exception:  # noqa: BLE001
            pass

    # 6. L1 → SHADOW（成功路径守卫：不看提取出几条，「没什么可记」也是正常结论）
    if memory_id and mem_node.layer == MemoryLayer.L1_RAW:
        try:
            mem_node.status = MemoryStatus.SHADOW
            run(vs.upsert(mem_node))
        except Exception:  # noqa: BLE001  SDK 同款 warning 不致命
            pass

    # tag 缓存增量预热（SDK write() 同款，失败无碍）
    try:
        new_tags = {
            str(t)
            for m in new_metas
            for t in ((m or {}).get("tags") or [])
            if str(t).strip()
        }
        if new_tags:
            writer._remember_existing_tags(user_id, agent_id or "default_agent", new_tags)
    except Exception:  # noqa: BLE001
        pass

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    try:
        extract_tok = {
            "prompt": getattr(agent_result, "extract_prompt_tokens", 0) or 0,
            "completion": getattr(agent_result, "extract_completion_tokens", 0) or 0,
            "total": getattr(agent_result, "extract_tokens_used", 0) or 0,
        }
    except Exception:  # noqa: BLE001
        extract_tok = {"prompt": 0, "completion": 0, "total": 0}

    merged_existing = bool(
        int(ops_counts.get("update") or 0) + int(ops_counts.get("supersede") or 0)
    ) and stored_via != "direct_fallback"
    warning = None
    if rejected_ids:
        warning = (
            "reconcile 返回了库中不存在的 id，已剔除："
            + ",".join(rejected_ids)
        )
    if stored_via == "direct_fallback":
        warning = (
            (warning + "；" if warning else "")
            + "reconcile 未产出可检索节点，已回退直存（direct_fallback）"
        )
    if merged_existing:
        warning = (
            (warning + "；" if warning else "")
            + "本次内容并入既有记忆（未新建节点），如需独立条目请先 search 确认"
        )

    return {
        "success": True,
        "scene": "chat",
        "memory_id": memory_id,          # L1 根（与 SDK add 同义）
        "memory_ids": list(stored_ids),  # 可检索节点（L2/L3…）
        "request_id": request_id,
        "elapsed_ms": elapsed_ms,
        "error_code": None,
        "error_message": None,
        "extract_tokens": extract_tok,
        "reconcile_ops": ops_counts,
        "reconcile_tokens": recon_tokens,
        "stored_via": stored_via,
        "reconcile_rejected_ids": rejected_ids,
        "reconcile_gate": {
            "min_sim": _reconcile_min_sim(),
            "bypassed": len(direct_ids),
            "max_sim": round(max(sim_scores.values()), 4) if sim_scores else None,
        },
        "merged_existing": merged_existing,
        "warning": warning,
        "two_phase": True,               # 路径标记，诊断用
    }


def commit_extracted_with_retry(
    client: object, text: str, agent_result: Any, *, prepared: dict | None = None, attempts: int = 4, **kw
) -> dict:
    """commit_extracted 包瞬态重试（readonly/locked 换次重跑），口径同
    clientops.add_sync_with_retry：部分写入后的重试可能产生重复条目，
    reconcile 判重/合并会在后续写入时吸收。prepared 可跨重试复用（预计算
    不受瞬态占用影响）。"""
    import time as _time

    from hymem.core.clientops import is_transient_db_error

    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return commit_extracted(client, text, agent_result, prepared=prepared, **kw)
        except CommitFailed:
            raise  # reconcile 决策失败不是瞬态占用，重试无益
        except Exception as e:  # noqa: BLE001
            if not is_transient_db_error(e) or i == attempts - 1:
                raise
            last = e
            _time.sleep(0.3 * (i + 1))
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------
# S2（2026-10-30 batch-commit）：多条 job 合并提交段
# docs/specs/2026-10-30-batch-commit —— P2 贴齐 SDK：只用原生批量入口
# ---------------------------------------------------------------------------


def _batch_dedup(writer, run, jobs: list[dict]) -> tuple[list[str], list[dict], dict[str, int], dict]:
    """① 批级 dedup：跨 job 的重复 facts 收敛为一条（gmt 按序保首个）。

    返回 (all_texts, all_metas, job_of_text, precomputed)；dedup 失败 non-fatal，
    未去重的原样进判定（与 batch_commit 文档一致）。
    """
    all_texts: list[str] = []
    all_metas: list[dict] = []
    job_of_text: dict[str, int] = {}   # content → job 下标（精确映射）
    per_job_texts: list[set[str]] = [set() for _ in jobs]
    for ji, j in enumerate(jobs):
        for t, m in zip(j["prepared"]["new_texts"], j["prepared"]["new_metas"]):
            if t in job_of_text:
                continue  # 跨 job 重复：保首个（FIFO 语义），后到的 job 不重复落
            job_of_text[t] = ji
            per_job_texts[ji].add(t)
            all_texts.append(t)
            all_metas.append(m)
    precomputed: dict = {}
    for j in jobs:
        precomputed.update(j["prepared"]["precomputed"] or {})
    if len(all_texts) >= 2:
        try:
            all_texts, all_metas, _pd = run(
                writer._dedup_extracted(
                    all_texts, all_metas, jobs[0]["prepared"]["wr"],
                    jobs[0]["prepared"]["request_id"],
                )
            )
            precomputed.update(_pd or {})
            job_of_text = {t: job_of_text[t] for t in all_texts if t in job_of_text}
        except Exception:  # noqa: BLE001  dedup 失败 non-fatal，未去重直接进判定
            pass
    return all_texts, all_metas, job_of_text, precomputed


def _batch_l1_archive(writer, run, vs, jobs: list[dict]) -> tuple[list[str], list]:
    """② 逐 job L1 存档（各自原文/向量；SDK 规范：原始内容始终存 L1_RAW）。"""
    from hy_memory.models.memory import (
        MemoryLayer,
        MemoryNode,
        MemoryStatus,
        SourceType,
    )

    l1_ids: list[str] = []
    l1_nodes: list = []
    for j in jobs:
        wr = j["prepared"]["wr"]
        emb = j["prepared"]["l1_embedding"]
        node = MemoryNode(
            user_id=wr.user_id,
            agent_id=wr.agent_id or "default_agent",
            session_id=wr.session_id or "default_session",
            source=wr.source,
            layer=MemoryLayer.L1_RAW,
            content=wr.content,
            source_type=SourceType.EXPLICIT,
            status=MemoryStatus.ACTIVE,
            is_latest=True,
            embedding=emb or run(writer.embed_service.embed_queued_with_meta(wr.content))[0],
            memory_at=wr.memory_at,
            custom={},
        )
        l1_ids.append(run(vs.upsert(node)))
        l1_nodes.append(node)
    return l1_ids, l1_nodes


def _batch_reconcile(writer, run, vs, jobs: list[dict], all_texts: list[str],
                     all_metas: list[dict], precomputed: dict,
                     l1_ids: list[str]) -> tuple[list[str], dict, dict, dict]:
    """③ 一次判定（空 root；RECONCILE_ENABLED=false 走 _direct_store 同样空 root）。

    返回 (stored_ids, ops_counts, recon_tokens, recon_meta)；reconcile 整体
    失败抛 CommitFailed。recon_meta 记录闸门/复核/回退等诊断信息。
    """
    from hy_memory.pipelines import writer as writer_mod

    stored_ids: list[str] = []
    recon_error = None
    ops_counts: dict = {}
    recon_tokens: dict = {}   # 先初始化：all_texts 为空（全批无可提取事实）时
    recon_meta: dict = {
        "stored_via": "none",
        "rejected_ids": [],
        "gate_bypassed": 0,
        "gate_min_sim": None,
        "merged_existing": False,
    }
    if all_texts:              # 不进判定，⑤ 结果段仍要引用——曾致 UnboundLocalError
        base_wr = jobs[0]["prepared"]["wr"]
        req_id = jobs[0]["prepared"]["request_id"]
        origin_metas = list(all_metas)
        direct_texts: list[str] = []
        direct_metas: list[dict] = []
        direct_ids: list[str] = []
        if writer_mod._RECONCILE_ENABLED:
            recon_meta["gate_min_sim"] = _reconcile_min_sim()
            all_texts, all_metas, direct_texts, direct_metas, _scores = (
                _split_facts_for_reconcile(
                    run, vs, all_texts, all_metas, precomputed, base_wr.user_id
                )
            )
            recon_meta["gate_bypassed"] = len(direct_texts)
        recon_metas = list(all_metas)
        if all_texts:
            if writer_mod._RECONCILE_ENABLED:
                recon_meta["stored_via"] = "reconcile"
                stored_ids, recon_error, ops_counts, recon_tokens = run(
                    writer._reconcile_and_store(
                        new_memory_texts=all_texts,
                        new_memories_meta=all_metas,
                        request=base_wr,
                        vector_store=vs,
                        req_id=req_id,
                        source_raw_memory_id="",   # 空 root：见 batch_commit docstring ③
                        precomputed_embeddings=precomputed,
                    )
                )
            else:
                recon_meta["stored_via"] = "direct"
                stored_ids, recon_error, ops_counts, recon_tokens = run(
                    writer._direct_store(
                        new_memories_meta=all_metas,
                        request=base_wr,
                        vector_store=vs,
                        req_id=req_id,
                        source_raw_memory_id="",
                        precomputed_embeddings=precomputed,
                    )
                )
            if recon_error is not None:
                raise CommitFailed(
                    f"RECONCILE_FAILED(batch): {recon_error}; "
                    f"l1_memory_ids={','.join(l1_ids)}"
                )
        if direct_texts:
            d_ids, d_err, d_counts, d_tokens = run(
                writer._direct_store(
                    new_memories_meta=direct_metas,
                    request=base_wr,
                    vector_store=vs,
                    req_id=req_id,
                    source_raw_memory_id="",
                    precomputed_embeddings={
                        t: precomputed[t] for t in direct_texts if t in precomputed
                    },
                )
            )
            if d_err is not None:
                raise CommitFailed(
                    f"DIRECT_STORE_FAILED(batch): {d_err}; "
                    f"l1_memory_ids={','.join(l1_ids)}"
                )
            stored_ids.extend(d_ids)
            direct_ids = list(d_ids)
            if ops_counts:
                ops_counts["add"] = int(ops_counts.get("add") or 0) + len(d_ids)
                ops_counts["total"] = int(ops_counts.get("total") or 0) + len(d_ids)

        stored_ids, recon_meta["rejected_ids"] = _verify_memory_ids(
            run, vs, stored_ids
        )

        # 与单条路径同一套兜底：reconcile 空操作/幻觉 id 时回退直存，
        # 保证本批事实至少有一条可检索节点。
        if not stored_ids:
            fb_ids, fb_err, _, fb_tokens = run(
                writer._direct_store(
                    # 直存组已有可检索节点时只回退 reconcile 组，避免重复写一份
                    new_memories_meta=recon_metas if direct_ids else origin_metas,
                    request=base_wr,
                    vector_store=vs,
                    req_id=req_id,
                    source_raw_memory_id="",
                    precomputed_embeddings=precomputed,
                )
            )
            if fb_err is not None:
                raise CommitFailed(
                    f"DIRECT_FALLBACK_FAILED(batch): {fb_err}; "
                    f"l1_memory_ids={','.join(l1_ids)}"
                )
            stored_ids = list(fb_ids)
            recon_meta["stored_via"] = "direct_fallback"
            if ops_counts:
                ops_counts["add"] = int(ops_counts.get("add") or 0) + len(stored_ids)
                ops_counts["total"] = int(ops_counts.get("total") or 0) + len(stored_ids)
            recon_tokens = {
                k: int(recon_tokens.get(k) or 0) + int(fb_tokens.get(k) or 0)
                for k in set(recon_tokens) | set(fb_tokens)
            }
        recon_meta["merged_existing"] = bool(
            int(ops_counts.get("update") or 0) + int(ops_counts.get("supersede") or 0)
        )
    return stored_ids, ops_counts, recon_tokens, recon_meta


def _batch_lineage_fix(run, vs, stored_ids: list[str], job_of_text: dict[str, int],
                       l1_ids: list[str]) -> tuple[dict, str]:
    """④ lineage 修复：ADD 节点按 content→job 精确回填正确的根。

    返回 (id_to_node, lineage_note)；修复段失败不判失败（诚实降级，见文档）。
    """
    lineage_note = ""
    id_to_node: dict = {}
    if stored_ids:
        by_id = run(vs.get_by_ids(stored_ids)) if hasattr(vs, "get_by_ids") else []
        id_to_node = {n.node_id: n for n in (by_id or [])}
        fixed = 0
        for nid in stored_ids:
            node = id_to_node.get(nid)
            content = getattr(node, "content", "") if node else ""
            ji = job_of_text.get(content)
            if ji is None:
                continue
            try:
                run(vs.update_payload(nid, {"source_raw_memory_id": l1_ids[ji]}))
                fixed += 1
            except Exception as e:  # noqa: BLE001
                lineage_note = f"lineage_partial: {fixed} 已回填，{e}"
        if fixed < len(stored_ids) and not lineage_note:
            lineage_note = f"lineage_partial: {fixed}/{len(stored_ids)} 映射命中"
    return id_to_node, lineage_note


def batch_commit(client: object, jobs: list[dict]) -> list[dict]:
    """多条 job 的锁内合并提交：一次判定 LLM，逐 job 保 lineage。

    jobs：[{text, prepared, add_kw...}, ...]（同 user+agent 已由
    claim_next_batch 保证；prepared 为 prepare_commit 的结果）。
    调用方必须已持有 WriteLock。返回与 jobs 等长的 result dict 列表
    （与 commit_extracted 同形态，另带 batch 字段）。

    流水线（spec §1 批次流水线）：
      ① 批级 dedup：合并各 job 的 facts 互相判重（_dedup_extracted
         原生批量入口，内容唯一化；content→job 映射随之收紧）
      ② 逐 job：L1 根 upsert（各自原文/向量，SDK 规范不变）
      ③ 一次 _reconcile_and_store(全部 facts, source_raw_memory_id="")
         —— 空 root：SDK 批量入口只收单一根 id，传任何一个 job 的根
         都会把整批挂错；UPDATE 路径空串不写（writer.py:1417 守卫），
         历史挂链零扰动
      ④ lineage 修复：按 content→job 精确映射（对齐 SDK
         _inherit_exact_input_structure 的 no-fuzzy 原则），
         update_payload 逐 ADD 节点回填正确的 source_raw_memory_id
      ⑤ 逐 job：basic_info（各判各的）、L1→SHADOW、结果 dict

    失败语义：reconcile 整批失败抛 CommitFailed（调用方对批内全部
    mark_failed）；lineage 修复段失败不判失败——L2 已正确落库可检索，
    note 落 batch 结果（lineage_partial），诚实降级不假绿（P4/P6）。
    """
    from hy_memory.models.memory import MemoryStatus

    writer = _writer_of(client)
    run = lambda coro: _run(client, coro)  # noqa: E731
    vs = run(writer._get_vector_store())
    if not jobs:
        return []
    t0 = time.monotonic()

    # ① 批级 dedup（跨 job 的重复 facts 收敛为一条；gmt 按序保首个）
    all_texts, all_metas, job_of_text, precomputed = _batch_dedup(writer, run, jobs)

    # ② 逐 job L1 存档（各自原文/向量；SDK 规范：原始内容始终存 L1_RAW）
    l1_ids, l1_nodes = _batch_l1_archive(writer, run, vs, jobs)

    # ③ 一次判定（空 root；RECONCILE_ENABLED=false 走 _direct_store 同样空 root）
    stored_ids, ops_counts, recon_tokens, recon_meta = _batch_reconcile(
        writer, run, vs, jobs, all_texts, all_metas, precomputed, l1_ids
    )

    # ④ lineage 修复：ADD 节点按 content→job 精确回填正确的根
    id_to_node, lineage_note = _batch_lineage_fix(
        run, vs, stored_ids, job_of_text, l1_ids
    )

    # ⑤ 逐 job 结果（L1→SHADOW、basic_info、shadow_only 判定）
    # 批处理是「一次判定 + 一次 reconcile」，批次内的 job 共享同一段锁内
    # 耗时；给每个 job 报批次总时长比报 0 更诚实（--json 里能看出成本）。
    batch_elapsed_ms = int((time.monotonic() - t0) * 1000)
    results: list[dict] = []
    for ji, j in enumerate(jobs):
        p = j["prepared"]
        # basic_info 各判各的（best-effort；job 间的画像字段演化互不干扰，
        # SDK upsert_basic_profile 本身是幂等演化链）
        bi = p["basic_info"]
        if isinstance(bi, dict) and bi:
            try:
                from hy_memory.agent.tools.basic_profile import upsert_basic_profile

                run(
                    upsert_basic_profile(
                        user_id=p["wr"].user_id,
                        kv=bi,
                        vector_store=vs,
                        embed_service=writer.embed_service,
                        allowed_fields=list(
                            writer.config.basic_profile.effective_fields().keys()
                        ),
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        # L1 → SHADOW（成功路径守卫同 commit_extracted）
        try:
            l1_nodes[ji].status = MemoryStatus.SHADOW
            run(vs.upsert(l1_nodes[ji]))
        except Exception:  # noqa: BLE001
            pass
        mine_ids = [nid for nid in stored_ids
                    if job_of_text.get(getattr(id_to_node.get(nid), "content", "")) == ji]
        ar = j.get("agent_result")
        try:
            extract_tok = {
                "prompt": getattr(ar, "extract_prompt_tokens", 0) or 0,
                "completion": getattr(ar, "extract_completion_tokens", 0) or 0,
                "total": getattr(ar, "extract_tokens_used", 0) or 0,
            }
        except Exception:  # noqa: BLE001
            extract_tok = {"prompt": 0, "completion": 0, "total": 0}
        # 批处理是「一次判定 + 一次 reconcile」，批次内的 job 共享同一段锁内
        # 耗时；给每个 job 报批次总时长比报 0 更诚实（--json 里能看出成本）。
        results.append({
            "success": True,
            "scene": "chat",
            "memory_id": l1_ids[ji],
            "memory_ids": mine_ids,
            "request_id": p["request_id"],
            "elapsed_ms": batch_elapsed_ms,
            "error_code": None,
            "error_message": None,
            "extract_tokens": extract_tok,
            "reconcile_ops": ops_counts if ji == 0 else {},
            "reconcile_tokens": recon_tokens if ji == 0 else {},
            "stored_via": recon_meta.get("stored_via"),
            "reconcile_rejected_ids": recon_meta.get("rejected_ids") or []
            if ji == 0 else [],
            "reconcile_gate": {
                "min_sim": recon_meta.get("gate_min_sim"),
                "bypassed": recon_meta.get("gate_bypassed"),
            } if ji == 0 else {},
            "merged_existing": bool(recon_meta.get("merged_existing")) if ji == 0 else False,
            "two_phase": True,
            "batch": {"size": len(jobs), "lineage_note": lineage_note},
        })
    return results
