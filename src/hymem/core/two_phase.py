# -*- coding: utf-8 -*-
"""R4（2026-10-29）：同步 add 两段式 —— 遵守 SDK 设计规范的锁粒度收窄。

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
    if new_texts:
        recon = writer_mod._RECONCILE_ENABLED
        if recon:
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

    elapsed_ms = 0.0
    try:
        extract_tok = {
            "prompt": getattr(agent_result, "extract_prompt_tokens", 0) or 0,
            "completion": getattr(agent_result, "extract_completion_tokens", 0) or 0,
            "total": getattr(agent_result, "extract_tokens_used", 0) or 0,
        }
    except Exception:  # noqa: BLE001
        extract_tok = {"prompt": 0, "completion": 0, "total": 0}

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
