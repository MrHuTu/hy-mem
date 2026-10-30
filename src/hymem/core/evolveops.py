# -*- coding: utf-8 -*-
"""L6 认知层（ultra 专属）：evolve 用的独立 client + 空转检测。

为什么要有这个模块
------------------
SDK 只有一个 `config.llm`，S1（每次 add 的抽取/判重）和 S2（evolve 的 schema
归纳）共用它。但两者对模型的要求正好相反：

* S1 是封闭候选里的判定，意图分类模型又快又稳（实测 0.7s/条）；
* S2 是开放生成 + 多工具决策，意图分类模型直接放弃（实测 4209 token 输入只
  回 7 个 completion token），换通用模型稳定产出 2~3 个 schema。

所以不能全局换模型，只能在 evolve 这一次调用里换：本模块造一个**只用于
evolve 的 client**，在内存 config 对象上覆盖 `llm.model`，跑完即丢。
`config.json` 与环境变量都不动 —— 注入 `MEMORY_LLM_MODEL` 会波及全局，
`add` 也一起变慢（0.7s → 1.7s/条）。

kuzu 独占（重要）
----------------
`graph_store_kuzu.py:340` 用的是 `kuzu.Database(path)`，没有 read_only 选项，
因此**同一数据目录同一时刻只能有一个进程持有图库**。实测第二个 client 构造时
报 `IO exception: Could not set lock on file ... kuzu_db`。

推论：MCP 长驻进程若已持有图库，CLI 的 evolve 必然失败。这不是我们能绕过的
（要绕过得给上游提 PR 加 read_only），所以错误信息必须说清楚「谁占着、怎么办」，
而不是甩一句 RuntimeError。
"""
from __future__ import annotations

import os

# 空转判据（C6）：模型被调用过、但几乎没输出。实测意图分类模型 7 个 token，
# 强模型 2700~5000。150 是两者之间的安全分界，留足余量避免误杀短归纳。
IDLE_COMPLETION_TOKENS = 150

KUZU_LOCK_MARKERS = ("Could not set lock on file", "GraphStore (kuzu) init failed")


class ModeDisabled(RuntimeError):
    """当前 mode 不提供 L6（非 ultra）。"""


def kuzu_lock_error(text: str) -> bool:
    """识别图库被别的进程占着（而不是配置错、依赖缺）。"""
    t = str(text or "")
    return any(m in t for m in KUZU_LOCK_MARKERS)


def build_evolve_client(model_override: str = "", max_tokens: int = 0):
    """造一个专供 evolve 的 ultra client。

    `MemoryConfig.from_env()` 拿的是当前进程已注入的 MEMORY_* 环境变量（含
    llm.model），随后**只在内存对象上**覆盖：
      * llm.model        —— evolve.model 或 --model 给了才覆盖
      * llm.agent_max_tokens —— 兜底再设一次（SDK 默认 2000 会截断 schema JSON）
    """
    from hy_memory import HyMemoryClient, MemoryConfig

    from hymem.core import config as cfgmod
    from hymem.core import vdb_endpoint
    from hymem.core.clientops import build_embedding_service

    cfg = cfgmod.load()
    mode = str(cfg.get("mode") or "pro")
    if mode != "ultra":
        raise ModeDisabled(mode)

    ev = cfg.get("evolve") or {}
    sdk_cfg = MemoryConfig.from_env()
    model = str(model_override or ev.get("model") or "").strip()
    if model:
        sdk_cfg.llm.model = model
    sdk_cfg.llm.agent_max_tokens = int(max_tokens or ev.get("maxTokens") or 8000)

    vdb_endpoint.ensure_endpoint()
    svc = build_embedding_service()
    return HyMemoryClient(config=sdk_cfg, mode="ultra", embedding_service=svc)


def _agent_results(out: dict) -> list[dict]:
    """从 evolve 返回里挖出「每个任务的 agent_result」。

    结构（`pipelines/system2_writer.py:1214`，踩了三轮才对）：顶层 `results` 是
    **dict 而不是 list**，形如 `{"system2_agent": {...}, "should_continue": ...}`，
    批次与 token 在那个 `system2_agent` 里。当成 list 遍历会一个元素都拿不到，
    于是 batches/tokens 全为 0、idle 判据永远不触发。

    这里对三种形态都兜住：dict（当前）、list of dict（旧版/多任务）、detail（更旧）。
    """
    raw = out.get("results")
    if isinstance(raw, dict):
        s2 = raw.get("system2_agent")
        if isinstance(s2, dict):
            return [s2]
        return [
            v for v in raw.values()
            if isinstance(v, dict) and "batches_completed" in v
        ]
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    det = out.get("detail")
    return [det] if isinstance(det, dict) else []


def _aggregate(out: dict) -> dict:
    """把每个任务的批次/token 合并成一份。"""
    items = _agent_results(out)
    if not items:
        return {}
    keys = (
        "batches_planned", "batches_completed", "batches_failed",
        "total_prompt_tokens", "total_completion_tokens",
    )
    return {k: sum(int(r.get(k) or 0) for r in items) for k in keys}


def summarize(out: dict) -> dict:
    """把 evolve 原始返回整理成 CLI/MCP 都能用的形状，并判定是否空转。

    `idle` 是这套东西最关键的字段：意图分类模型跑完自报 `success=true`，
    但 completion 只有 7 个 token、schema 一个没建。不单独标出来，用户会以为
    L6 在积累，实际一直是空的。
    """
    out = out or {}
    det = _aggregate(out)
    batches_done = int(det.get("batches_completed") or 0)
    prompt_tokens = int(det.get("total_prompt_tokens") or 0)
    completion_tokens = int(det.get("total_completion_tokens") or 0)
    # 空转 = 真的开了批、模型也真被调了（有 prompt），但几乎没吐东西。
    idle = batches_done > 0 and prompt_tokens > 0 and completion_tokens < IDLE_COMPLETION_TOKENS
    res = {
        "ok": bool(out.get("success")),
        "idle": idle,
        "request_id": out.get("request_id"),
        "batches_planned": det.get("batches_planned"),
        "batches_completed": batches_done,
        "batches_failed": det.get("batches_failed"),
        "tasks_processed": out.get("tasks_processed"),
        "clusters_remaining": out.get("remaining_clusters"),
        "fresh_facts_remaining": out.get("remaining_fresh_facts"),
        "continuation_reason": out.get("continuation_reason"),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "elapsed_ms": out.get("elapsed_ms"),
        "error": out.get("error"),
        "raw_keys": sorted(k for k in out if not str(k).startswith("_")),
    }
    if idle:
        res["idle_note"] = (
            f"模型被调用了（{prompt_tokens} prompt tokens）但只输出 {completion_tokens} 个 token，"
            "没有归纳出任何 schema。多半是当前模型做不了开放生成——用意图分类/小模型时必然如此。"
        )
        res["idle_hint"] = (
            "注意：本批事实已被标记为「已参与过归纳」，换模型直接重跑通常仍无产出。"
            "正确做法是 add 新内容后再跑，或一开始就用 --model 指定强模型。"
            "长期生效：hy-mem config set evolve.model=<模型名>（如 deepseek-v4.1-flash-1017）"
        )
    if not res["ok"] and res.get("error"):
        res["error_hint"] = _error_hint(str(res["error"]))
    return res


def _error_hint(err: str) -> str:
    if kuzu_lock_error(err):
        return (
            "图库被另一个进程占着（kuzu 是独占锁，不支持多进程共享）。"
            "ultra 模式下 MCP 服务长驻会一直持有图库——先停掉 MCP 客户端再跑 evolve，"
            "或改用 MCP 里的 evolve 工具（它跑在持有进程内，不撞锁）。"
        )
    if "Failed to parse operations JSON" in err:
        return (
            "模型输出被 max_tokens 截断导致 JSON 不闭合。把上限调大："
            "hy-mem config set evolve.maxTokens=16000，或换输出更稳的模型。"
        )
    return ""


def _schema_count(client, user_id: str = "default") -> int:
    """数图里现有多少条 ACTIVE 的 L6 schema。失败返回 -1（不阻断主流程）。

    用它做「沉淀了几条」的显式回报：用户问的永远是「到底归纳出来没有」，
    光给 batches/tokens 等于答非所问。SDK 的 get_stats 只数 Memory/User 节点，
    不含 schema，所以这里直接按层查。

    isolation_key 必须和 SDK 归纳时用的完全一致，否则数出来恒 0（实测踩过：
    传空串 → 明明有 6 条 schema 却报 0）。SDK 是
    `MemoryNode.build_isolation_key(user_id, agent_id or "default")`，evolve 的
    agent_id 默认 "default_agent"。
    """
    try:
        import asyncio

        from hy_memory.models.memory import MemoryLayer, MemoryNode, MemoryStatus

        gs = getattr(client, "_graph_store", None)
        if gs is None:
            return -1
        iso = MemoryNode.build_isolation_key(user_id, "default_agent")
        rows = asyncio.run(
            gs.get_all_nodes(
                isolation_key=iso,
                layer=MemoryLayer.L6_SCHEMA,
                status=MemoryStatus.ACTIVE,
                limit=1000,
            )
        )
        return len(rows or [])
    except Exception:  # noqa: BLE001
        return -1


def evolve(
    model_override: str = "",
    max_batches: int | None = None,
    user_id: str = "default",
    client=None,
) -> dict:
    """跑一次 S2 归纳。返回 summarize() 的结构（含 idle 判定与 schema 增量）。

    `client` 为空时现造一个（CLI 用，短命进程）；传入时复用它（MCP 用）——
    MCP 长驻进程已经拿着图库，再开一个 client 必然撞 kuzu 独占锁
    （`Could not set lock on file`）。复用时不重建 provider，只在内存里临时改
    `llm.model`，跑完还原：LLMProvider 每次调用都读 config.llm.model，所以
    这一次 evolve 会真的用上强模型，而同一个进程里的 add/search 不受影响。
    """
    from hymem.core import config as cfgmod

    ev = cfgmod.load().get("evolve") or {}
    model = str(model_override or ev.get("model") or "").strip()
    own = client is None
    old_model = ""
    old_mt = None
    if own:
        client = build_evolve_client(model_override=model)
    else:
        llm = client._config.llm
        old_model = getattr(llm, "model", "")
        old_mt = getattr(llm, "agent_max_tokens", None)
        if model:
            llm.model = model
        llm.agent_max_tokens = int(ev.get("maxTokens") or 8000)

    before = _schema_count(client, user_id)
    try:
        try:
            out = client.evolve(user_id=user_id, max_batches=max_batches)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            return {
                "ok": False,
                "idle": False,
                "error": msg,
                "error_hint": _error_hint(msg),
                "schemas_before": before,
                "schemas_after": _schema_count(client, user_id),
            }
        after = _schema_count(client, user_id)
        res = summarize(out)
        res["model"] = _effective_model(model_override)
        res["schemas_before"] = before
        res["schemas_after"] = after
        if before >= 0 and after >= 0:
            res["schemas_added"] = after - before
            ran = int(res.get("batches_completed") or 0)
            reason = str(res.get("continuation_reason") or "")
            fresh = int(res.get("fresh_facts_remaining") or 0)
            if res.get("ok") and ran == 0:
                # 实测（2026-10-30 真实库）：tasks_processed=1、batches_planned=0、
                # continuation_reason=no_clusters、fresh_facts_remaining=32。
                # 旧条件要求 tasks_processed==0 才给 note，于是「有 32 条新事实却
                # 一条簇都没成」这种最需要解释的情况反而静默——CLI 只显示「+0 条」，
                # 用户会以为 evolve 坏了或模型不行。
                if reason == "no_clusters" and fresh > 0:
                    res["note"] = (
                        f"有 {fresh} 条未消费事实，但一条簇都没成：L6 只归纳「同主题、"
                        "细节各异、≥4 条且未被判重合并」的事实簇。当前素材主题太散"
                        "（或太像、已被判重合并），所以 S2 连 LLM 都没调（tokens=0）。"
                        "这不是 evolve 坏了、也不是模型不行——继续 add 同一主题的具体细节再跑。"
                    )
                elif fresh == 0:
                    res["note"] = (
                        f"没有未消费的新事实（库中现有 {after} 条 schema）。"
                        "参与过归纳的事实会被标记排除，要新结果就得 add 新内容。"
                        if after > 0
                        else "没有可归纳的素材：库里还没有可成簇的事实，先 add 一批同主题内容。"
                    )
                else:
                    res["note"] = (
                        f"本次没开出批次（continuation_reason={reason or 'unknown'}，"
                        f"剩 {fresh} 条未消费事实）。"
                    )
            # 模型真吐了一大堆 token、却一条 schema 没新增：不是空转，是归纳出的
            # 模式与已有 schema 重复被丢弃。这个区别要给用户讲清楚，否则会以为
            # 白跑一趟就想换模型。
            elif res.get("ok") and ran > 0 and after == before and not res.get("idle"):
                res["note"] = (
                    "本批没有新增 schema：归纳出的模式与已有 schema 重复（已被判重丢弃）。"
                )
        return res
    finally:
        if own:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        else:
            # 还原临时覆盖：MCP 里这个 client 还要继续服务 add/search，
            # 留着强模型会让之后的每次写入都变慢（S1 抽取 0.7s → 1.7s/条）。
            client._config.llm.model = old_model
            if old_mt is not None:
                client._config.llm.agent_max_tokens = old_mt


def _effective_model(override: str = "") -> str:
    """本次实际用的归纳模型：--model > evolve.model > llm.model。"""
    if override:
        return override
    from hymem.core import config as cfgmod

    cfg = cfgmod.load()
    ev = cfg.get("evolve") or {}
    return str(ev.get("model") or (cfg.get("llm") or {}).get("model") or "")
