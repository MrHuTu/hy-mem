# -*- coding: utf-8 -*-
"""

S3-1（2026-10-29）：本模块从入口 cli.py 平移而来（纯搬家，逻辑零变化）。
依赖方向：core 内部互引 + stdlib/SDK；不依赖 ui/mcp/入口。

"""
from __future__ import annotations

import os
import subprocess  # 2026-10-29 修：S3-1 平移时漏带，_spawn_detached_worker 的 Popen 路径才触发
import time
import weakref
from collections import OrderedDict
from datetime import datetime

from hymem.core import store
from hymem.core import timefilter


def _parse_memory_at(v) -> "datetime | None":
    """--memory-at 的值 → datetime。支持 2026-10-27 / 2026-10-27 14:30:00 / ISO T 分隔。"""
    if v is None or not str(v).strip():
        return None
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"--memory-at 时间格式不认识：{s!r}（支持 2026-10-27、2026-10-27 14:30:00 这类写法）"
    )


def vdb_memories(resp) -> list[dict]:
    """解析 `list_memories` 的返回包装：`{"vdb": {"memories": [...]}}`。

    CLI `cmd_list` 与 MCP `list_memories` 都吃这个结构：直接当列表迭代会拿到
    key 字符串（"vdb"），再被 isinstance 过滤成恒 0 条——这个坑吃过一次就够了。
    """
    if not isinstance(resp, dict):
        return []
    vdb = resp.get("vdb")
    if isinstance(vdb, dict):
        return [m for m in (vdb.get("memories") or []) if isinstance(m, dict)]
    return [m for m in (resp.get("memories") or []) if isinstance(m, dict)]


def _snapshot_ids(client, ident: dict, cap: int = 2000) -> tuple[set[str], bool]:
    """取节点 id 快照，返回 (ids, complete)。

    complete=False 表示**无法确定快照是否覆盖了全部节点**：分页触到 cap 上限，
    或读取中途抛错。调用方严禁在 complete=False 时打标签——那会把标签污染到
    老节点，属于不可逆的数据错误。

    实测：一次 add 的 memory_ids 并不等于"新建节点"，而是"本次影响到的节点"。
    写一句 CI 流水线的事实，会连带返回 8 个被合并/更新的旧节点 id。
    直接全部打标签 = 把标签污染到无关记忆上，所以必须做前后差分。
    """
    ids: set[str] = set()
    offset = 0
    total: int | None = None
    # 只按 user 取全量快照：reconcile 会跨 session 合并节点，
    # 若同时按 session/agent 过滤，已知集合会偏小，导致把老节点误判为新节点。
    kwargs: dict = {}
    if (ident or {}).get("user_id"):
        kwargs["user_id"] = ident["user_id"]
    try:
        while True:
            if offset >= cap:
                # 到上限还没读到空页：后面可能还有节点，快照不完整。
                return ids, False
            r = client.list_memories(limit=200, offset=offset, **kwargs)
            if not isinstance(r, dict) or "vdb" not in r:
                return ids, False
            vdb = r.get("vdb") or {}
            batch = vdb.get("memories") or []
            if not isinstance(batch, list):
                return ids, False
            if total is None:
                t = vdb.get("total")
                if isinstance(t, int) and t >= 0:
                    total = t  # 真 SDK 会带 total；缺失时退化为「探到空页」
            if not batch:
                # 空页 = 到底；但 total 说还有 → 数据不一致，不能算完整
                if total is not None and offset < total:
                    return ids, False
                break
            for it in batch:
                mid = it.get("memory_id") if isinstance(it, dict) else None
                if mid:
                    ids.add(str(mid))
            offset += len(batch)
            if total is not None:
                if offset >= total:
                    break
            # 无 total 的兼容路径：短页未必是末页，继续读到空页才判定完整
    except Exception:  # noqa: BLE001
        # 不静默清空：保留已读到的部分供诊断，但明确告知不完整。
        return ids, False
    return ids, True


def _ids_of(result: dict) -> list[str]:
    """pro 模式下 memory_id 是 l1_raw 事件 id，memory_ids 才是抽取后的最终节点 id。

    标签必须绑最终节点，否则在 search/list 里永远对不上。
    """
    ids = result.get("memory_ids") or []
    ids = [str(i) for i in ids if i]
    if not ids:
        mid = result.get("memory_id") or result.get("id")
        if mid:
            ids = [str(mid)]
    return ids


def _new_ids_only(returned: list[str], before: set[str] | None) -> list[str]:
    if not returned:
        return []
    if not before:
        return returned
    fresh = [i for i in returned if i not in before]
    # 全都被识别为老节点时（内容被 reconcile 判为"已存在"），退回第一个，
    # 避免标签无处可挂；这比把标签铺到 N 个老节点上安全。
    return fresh or returned[:1]


def _flatten_search(resp: dict) -> list:
    """search 返回 {memories:{schema:[],proactive:[],normal:[]}}，拍平后按 score 降序。"""
    mem = resp.get("memories") or {}
    items = []
    if isinstance(mem, dict):
        for bucket in ("normal", "proactive", "schema"):
            v = mem.get(bucket)
            if isinstance(v, list):
                items.extend(x for x in v if isinstance(x, dict))
        # 兜底：未知桶
        for k, v in mem.items():
            if k not in ("normal", "proactive", "schema") and isinstance(v, list):
                items.extend(x for x in v if isinstance(x, dict))
    elif isinstance(mem, list):
        items = [x for x in mem if isinstance(x, dict)]
    items.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return items


# P1-5：L6 通道的两处放大
#   1. query 向量：`client.search` 内部已经 embed 过一次，这里再 embed 一次。
#      不碰 SDK 私有（query_embedding 只能传入「原始 query」的向量，会绕过 SDK
#      的 retrieval 改写，属于语义漂移），改为在**本层**按 query 缓存向量：
#      同一进程内同一 query 的第二次 L6 检索不再产生额外 embed。
#   2. `_l6_schema_stats` 空结果时 `get_all_nodes(limit=1000)` 全量拉取：
#      改为短 TTL 缓存，避免「空结果 = 最慢路径」在 MCP 长驻进程里反复放大。
# 缓存只影响「省一次调用」，不参与打分/排序，命中与否都不改变检索结果。


_L6_STATS_TTL = 10.0   # 秒；schema 统计变化不频繁，短 TTL 足够挡住连续检索
_L6_VEC_TTL = 300.0    # 秒；query→向量只与模型/文本有关，可以缓存更久
_L6_VEC_MAX = 64       # 单进程最多缓存多少个 query 向量（防长驻进程无界增长）
_L6_STATS_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_L6_VEC_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _reset_l6_caches() -> None:
    """清空 L6 缓存（测试用；也便于排障时手工失效）。"""
    _L6_STATS_CACHE.clear()
    _L6_VEC_CACHE.clear()


def _l6_vec_get(es, query: str):
    try:
        bucket = _L6_VEC_CACHE.get(es)
    except TypeError:  # provider 对象不可弱引用
        return None
    hit = (bucket or {}).get(query)
    if not hit:
        return None
    ts, vec = hit
    if time.monotonic() - ts > _L6_VEC_TTL:
        bucket.pop(query, None)
        return None
    return vec


def _l6_vec_put(es, query: str, vec) -> None:
    try:
        bucket = _L6_VEC_CACHE.get(es)
        if bucket is None:
            bucket = OrderedDict()
            _L6_VEC_CACHE[es] = bucket
    except TypeError:
        return
    bucket[query] = (time.monotonic(), vec)
    bucket.move_to_end(query)
    while len(bucket) > _L6_VEC_MAX:
        bucket.popitem(last=False)


def _l6_stats_get(gs, user_id: str):
    try:
        bucket = _L6_STATS_CACHE.get(gs)
    except TypeError:
        return None
    hit = (bucket or {}).get(user_id)
    if not hit:
        return None
    ts, total, with_emb = hit
    if time.monotonic() - ts > _L6_STATS_TTL:
        return None
    return total, with_emb


def _l6_stats_put(gs, user_id: str, total: int, with_emb: int) -> None:
    if total < 0:  # 查询失败（-1,-1）不能缓存，否则会把故障固化一个 TTL
        return
    try:
        bucket = _L6_STATS_CACHE.get(gs)
        if bucket is None:
            bucket = {}
            _L6_STATS_CACHE[gs] = bucket
    except TypeError:
        return
    bucket[user_id] = (time.monotonic(), total, with_emb)


def fetch_l6_schemas(
    client,
    query: str,
    *,
    user_id: str = "default",
    limit: int = 3,
    min_score: float = 0.4,
) -> tuple[list[dict], str]:
    """L6 正路：把 query 向量化，去 l6_schema 层做语义检索。

    返回 `(schemas, state)`，state 为空串表示正常，其余是原因：
    `"disabled"`（非 ultra / 没有图库）、`"error: ..."`（调用失败）。

    为什么不让 SDK 带 schema：SDK 的 schema 通道一票控两路——
      * 正路：query 向量 vs l6_schema，有阈值（schema_min_score）
      * 反路：拿 normal 命中的 fact id 反查「哪些 schema 引用了它」，**不看 query**、
        不卡阈值
    而且注入时 score 被 RRF 融合分覆盖（0.01~0.03 量级），和语义分不是一个
    尺度，调用方既过滤不了也解释不了。实测「问向量库用什么」返回的 6 条 schema
    全是「凭证不入库」「发布前跑测试」这类无关归纳，就是这么来的。

    所以：给 SDK 传 schema_limit=0 把两路一起关掉，正路由这里单独跑，
    阈值/条数/排序全在我们手里。反路（证据反查）首版不做（契约 C9）。
    """
    if getattr(client, "_mode", "") != "ultra":
        return [], "disabled"
    gs = getattr(client, "_graph_store", None)
    es = getattr(client, "_embed_service", None)
    if gs is None or es is None:
        return [], "disabled"
    # R2（2026-10-30）：图库未就绪时 vector_search 会静默返回 []——多会话并发下
    # 抢不到 kuzu 独占锁的 MCP 进程 _available=False，调用方拿到的是
    # 「schema_available=true 但恒空」的假象（实测复现）。必须在这里显式拦下。
    if getattr(gs, "_available", True) is False:
        return [], "graph_unavailable"

    async def _run():
        from hy_memory.models.memory import MemoryLayer

        qe = _l6_vec_get(es, query)
        if qe is None:
            qe = await es.embed(query)
            # embed 的返回形态在不同 provider 下不一致：可能是 (vec, meta)、
            # [[vec]] 或 vec 本身，逐层剥到一维 float 列表为止。
            if isinstance(qe, tuple):
                qe = qe[0]
            if qe and isinstance(qe[0], (list, tuple)):
                qe = qe[0]
            _l6_vec_put(es, query, qe)
        return await gs.vector_search(
            query_embedding=qe,
            isolation_key="",
            layers=[MemoryLayer.L6_SCHEMA],
            limit=int(limit),
            score_threshold=float(min_score),
            user_id=user_id,
        )

    try:
        import asyncio

        rows = asyncio.run(_run()) or []
    except Exception as e:  # noqa: BLE001
        return [], f"error: {type(e).__name__}: {e}"

    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        content = str(r.get("content") or "").strip()
        if not content:
            continue
        out.append(
            {
                "content": content,
                "score": float(r.get("score") or 0),
                "layer": "l6_schema",
                "node_id": r.get("node_id"),
            }
        )
    out.sort(key=lambda x: x["score"], reverse=True)
    if not out:
        # R3（2026-10-30）：空结果必须区分「库里真的没有」和「有但被阈值滤空 /
        # 根本没写进 embedding」。实测根因：evolve 归纳的 schema 是英文（SDK 提示
        # 词决定），中文 query 对它的语义分 0.17~0.34，被 schemaMinScore=0.4 全部
        # 滤掉后调用方只看到 schemas=[]，无从自救。
        total, with_emb = _l6_schema_stats(client, user_id)
        if total > 0 and with_emb == 0:
            return [], f"no_embedding:{total}"
        if total > 0:
            return [], f"empty:{total}"
    return out[:limit], ""


def _l6_schema_stats(client, user_id: str = "default") -> tuple[int, int]:
    """(该 user 的 ACTIVE L6 schema 总数, 其中带 embedding 的条数)。失败 (-1, -1)。

    isolation 口径与 evolveops._schema_count 一致：SDK evolve 写 schema 用
    `MemoryNode.build_isolation_key(user_id, "default_agent")`，传空串数出来恒 0
    （实测踩过）。只数不动库，失败绝不阻断检索主路径。
    """
    try:
        import asyncio

        from hy_memory.models.memory import MemoryLayer, MemoryNode, MemoryStatus

        gs = getattr(client, "_graph_store", None)
        if gs is None:
            return -1, -1
        cached = _l6_stats_get(gs, user_id)
        if cached is not None:
            return cached
        iso = MemoryNode.build_isolation_key(user_id, "default_agent")
        rows = asyncio.run(
            gs.get_all_nodes(
                isolation_key=iso,
                layer=MemoryLayer.L6_SCHEMA,
                status=MemoryStatus.ACTIVE,
                limit=1000,
            )
        ) or []
        total = len(rows)
        with_emb = sum(1 for r in rows if getattr(r, "embedding", None))
        _l6_stats_put(gs, user_id, total, with_emb)
        return total, with_emb
    except Exception:  # noqa: BLE001
        return -1, -1


def schema_state_meta(state: str, min_score: float) -> tuple[bool, str]:
    """schema 检索的 state → (schema_available, note)。

    两处调用点（cli cmd_search / mcp search_memory）共用这一份口径，禁止各写
    各的文案——B2/C3 的断言只锁「ultra」字样与 available 布尔，具体句子改这里。
    note 为空串表示无需解释（正常命中或调用方主动关掉）。
    """
    if not state or state == "off":
        return state == "", ""
    if state == "disabled":
        return False, (
            "L6 仅 ultra 模式提供（schema 存在 Kuzu 图库）——切 ultra 并跑过 evolve 才有模式可检索"
        )
    if state == "graph_unavailable":
        return False, (
            "L6 图库未就绪：kuzu 独占锁被其它 hy-mem 进程占用或初始化失败，"
            "本进程的 schema 检索不可用（关掉占锁的进程后重连 MCP）"
        )
    if state.startswith("no_embedding:"):
        n = state.split(":", 1)[1]
        return False, (
            f"库里有 {n} 条 L6 schema，但全部缺 embedding（evolve 写入时嵌入调用失败，"
            "向量检索永远查不到它们）——重跑 evolve（schema 被修订时会补写）或检查嵌入端点"
        )
    if state.startswith("empty:"):
        n = state.split(":", 1)[1]
        return True, (
            f"库里有 {n} 条 L6 schema，但阈值 {min_score} 下无命中——"
            "中文 query 对英文 schema（evolve 归纳默认英文）的语义分常在 0.2~0.35。"
            "出路：传 schema_min_score 降阈值试一次，或 hy-mem config set evolve.schemaMinScore=0.25"
        )
    return False, f"L6 检索失败：{state}"


def _pending_jobs() -> int:
    """队列里还有多少条没落库。

    用途：解释了「刚写完却查不到」这种最常见困惑 —— 写是异步的，任务还在
    队列里等着 worker 消费，库自然没有它。查队列本身失败时返回 0，绝不
    因为一句提示而把检索搞挂。
    """
    try:
        conn = store.connect()
        try:
            stats = store.queue_stats(conn) or {}
            return int(stats.get("pending") or 0) + int(stats.get("running") or 0)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0


def fill_chain_block(client, resp: dict, query: str, *, user_id: str,
                     workspace_id: str | None) -> dict:
    """补齐 scene=coding 检索的 diff 链块（SDK 在 workspace 作用域下会滤空）。

    上游缺陷（2026-10-30 S4 实测，hy-memory 2.0.5）：search(scene="coding",
    workspace_id=...) 内部把 `scope=workspace_id` 传给 chain_store.decorate，
    而 decorate 的 `_in_scope` 是拿 scope 去比**文件相对路径的段**
    （`src/client.py` → {src, client.py}）。编码场景的 workspace_id 通常是仓库
    URL（`github.com/org/repo`），一个段都对不上 → 全部 thread 被滤掉 →
    chain_block 恒空。链库本身写得没问题（thread/round 都在）。

    这里用同一份链库补一次 scope=None 的 decorate：只补块、不动 facts，
    并在 chain_note 里说明是兜底补的（不假装是 SDK 原样输出）。

    失败一律静默返回原 resp —— 链层是增量，拿不到就退回纯向量双层。
    """
    if resp.get("chain_block"):
        return resp
    try:
        owner = "%s|%s" % (str(user_id or "").strip(), str(workspace_id or "").strip())
        if owner == "|":
            owner = ""
        store = client._get_chain_store(owner)  # SDK 内部接口，见上（版本变更需回归）
        if store is None:
            return resp
        blk, used = store.decorate(resp.get("facts") or [], instruction=query) or ("", [])
        if blk:
            resp["chain_block"] = blk
            resp["chain_used"] = used
            resp["chain_note"] = (
                "diff 链块由 hy-mem 兜底补齐：SDK 在带 workspace_id 时会用仓库标识"
                "去匹配文件相对路径，匹配不上就把链全滤掉（上游行为）。"
            )
    except Exception:  # noqa: BLE001
        pass
    return resp


def _embedding_unhealthy() -> str:
    """空结果时的自证：嵌入端点到底通不通。通就返回 ""（真没命中）。

    只在「搜索结果为空」这条路上调用，正常命中不付这份时间（约 300ms）。
    """
    if os.getenv("HY_MEM_NO_PROBE", "").strip().lower() in ("1", "true", "yes"):
        return ""
    try:
        from hymem.core import config as cfgmod
        from hymem.ui import setup_wizard

        emb = (cfgmod.load().get("embedding") or {})
        good, msg, _dims = setup_wizard.probe_embedding(emb, timeout=20)
        return "" if good else msg
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def _bind_tags(ids: list[str], tags: list[str], source: str = "user") -> int:
    if not ids or not tags:
        return 0
    conn = store.connect()
    try:
        return store.bind_tags(conn, ids, tags, source)
    finally:
        conn.close()


def _bind_tags_to_new(client, ident: dict, before: set[str], returned: list[str],
                      tags: list[str], *, before_complete: bool = True) -> tuple[list[str], str]:
    """把用户标签绑到「本次真正新建的可检索节点」上，返回 (绑定的 id, note)。

    note 非空 = 跳过绑定，调用方必须透传原因。快照不完整（分页超上限/读取失败）
    时绝不打标签：不确定新老节点就下标签 = 数据污染。

    为什么不直接用 add 返回的 ids（0.9.9 / PRD F7）：抽取没产出节点时，
    add 只回 l1_raw（status=shadow）的 id，标签就挂在一个永远搜不到的影子
    节点上——用户看到的现象是「标签丢了」，而且 list/search --tag 都返回空。
    改用「写入前后 list_memories 差分」，拿到的是向量库里真实存在的节点。

    差分对 reconcile 也成立：把内容合并进老节点时 fresh 为空，退回
    `_new_ids_only` 的老逻辑（至少挂一个，别让标签彻底没有去处）。
    """
    if not tags:
        return [], ""
    if not before_complete:
        return [], "标签未绑定：写入前快照不完整（分页超上限或读取失败），无法判定新节点"
    after, after_complete = _snapshot_ids(client, ident)
    if not after_complete:
        return [], "标签未绑定：写入后快照不完整（分页超上限或读取失败），无法判定新节点"
    fresh = sorted(after - before)
    target = fresh or _new_ids_only(returned, before)
    _bind_tags(target, tags, "user")
    return target, ""


def _remember_auto(items: list[dict]) -> None:
    """把检索/列表顺带看到的 LLM 标签缓存进本地索引，供 tags 统计与过滤复用。"""
    if os.getenv("HY_MEM_TAG_AUTOSYNC") == "0":
        return
    pairs = [(str(it.get("memory_id")), it.get("tags") or []) for it in items if isinstance(it, dict)]
    pairs = [(mid, tags) for mid, tags in pairs if mid and mid != "None" and tags]
    if not pairs:
        return
    try:
        conn = store.connect()
        try:
            # 读路径上的目的一致：把 LLM 标签补进本地索引供统计/过滤复用。
            # 每轮检索都无条件重写一遍意味着「读一次写一次」——先把已有的
            # auto 标签取回来比对，只有确实新增的才落盘。
            mids = [mid for mid, _ in pairs]
            have = store.tags_for(conn, mids, source="auto")
            for mid, tags in pairs:
                want = {str(t).strip() for t in tags if str(t).strip()}
                missing = want - set(have.get(mid) or [])
                if missing:
                    store.bind_tags(conn, [mid], sorted(missing), "auto")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass  # 只读路径不能因为标签缓存失败而整体报错


def _effective_tags(items: list[dict]) -> dict[str, set[str]]:
    mids = [str(it.get("memory_id")) for it in items if isinstance(it, dict) and it.get("memory_id")]
    local: dict[str, list[str]] = {}
    if mids:
        try:
            conn = store.connect()
            try:
                local = store.tags_for(conn, mids)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            local = {}
    out: dict[str, set[str]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = str(it.get("memory_id") or "")
        s = {str(t).lower() for t in (it.get("tags") or [])}
        s |= {str(t).lower() for t in local.get(mid, [])}
        out[mid] = s
    return out


def _filter_by_tags(items: list[dict], tags: list[str] | None, tag_mode: str = "all") -> list[dict]:
    """标签过滤。SDK 的 search() 没有 tag 参数，只能在召回结果上后过滤，
    因此调用方必须先 over-fetch（多取 N 倍），否则过滤后可能剩不下东西。"""
    if not tags:
        return items
    want = {t.strip().lower() for t in tags if t and t.strip()}
    if not want:
        return items
    eff = _effective_tags(items)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        have = eff.get(str(it.get("memory_id") or ""), set())
        hit = want <= have if tag_mode == "all" else bool(want & have)
        if hit:
            it["_tags"] = sorted(have)
            out.append(it)
    return out


def _routed_productivity(resp: dict) -> bool:
    """SDK 未显式 scene 时按 queries/agentic 做 LLM 三分类，可能判到生产力库。"""
    return str((resp or {}).get("scene") or "").strip().lower() == "productivity"


def _chat_pinned_search(client, query: str | None, kw: dict,
                        queries: list[str] | None) -> dict:
    """U5：chat 检索只认 chat 库——误路由后以 `scene="chat"` 重取一次。

    未显式 scene 时 SDK 会 LLM 三分类；判为 working/coding 就返回
    facts/experiences，而 CLI/MCP 拍平只认 memories → "库里有、返回 0"。
    这里钉死 chat 库重取，目标 query 与 SDK 自身口径一致（queries 取最后一路）。
    正常判 chat 的路径不会走到这里，因此 queries 改写与 agentic 能力不受影响；
    仅这次本来就该被误路由时，丢失该次 rewrite（原响应不含改写文本）。
    """
    retry_kw = dict(kw)
    retry_kw.pop("queries", None)   # SDK：query 与 queries 互斥，同时传会 ValueError
    retry_kw["scene"] = "chat"
    target = query or (list(queries)[-1] if queries else "")
    return client.search(target, **retry_kw)


def searchcore(
    client,
    query: str | None,
    *,
    queries: list[str] | None = None,
    search_mode: str = "default",
    rerank: bool = True,
    fetch_limit: int = 10,
    offset: int = 0,
    limit: int = 10,
    min_score: float | None = None,
    since: float | None = None,
    until: float | None = None,
    ident: dict | None = None,
    include_sources: list[str] | None = None,
    exclude_sources: list[str] | None = None,
    tags: list[str] | None = None,
    tag_mode: str = "all",
    schema: bool = True,
    schema_query: str | None = None,
    schema_limit: int = 3,
    schema_min: float = 0.25,
    schema_user_id: str = "default",
    remember_auto: bool = False,
    probe_empty: bool = True,
    pending_always: bool = False,
) -> dict:
    """检索的**唯一实现**（CLI `cmd_search` 与 MCP `search_memory` 共用）。

    调用方只负责参数解析与输出投影；召回、拍平、时间/标签过滤、空结果嵌入探针、
    L6 schema、队列积压提示都在这里，避免两边各写一份再慢慢漂移。

    开关只表达「调用方本来就有/本来就没有的能力」，不是在 MCP 里补功能：
      * `remember_auto`：CLI 的 tags 自动缓存（列表/检索顺手写入），MCP 不开；
      * `tags` / `tag_mode`：标签后过滤（SDK 无 tag 查询条件，必须 over-fetch）；
      * `pending_always`：MCP 无论有没有命中都要带队列提示，CLI 只在空手时带；
      * `probe_empty`：空结果时探测嵌入端点是否真的挂了。

    返回统一 dict（调用方只做投影，不再二次过滤）：
      items / filtered_total / has_more / elapsed_ms / min_score /
      schemas / schema_state / probe_error / pending
    """
    kw: dict = {
        "search_mode": search_mode,
        "rerank": rerank,
        "limit": int(fetch_limit),
        "min_score": min_score,
        # L6 只走正路（与历史口径一致）：SDK 的正路+反路一起关掉，
        # 正路由 fetch_l6_schemas 单独跑，阈值/条数/排序全在我们手里。
        "schema_limit": 0,
    }
    if since is not None:
        kw["created_after"] = since
    if include_sources:
        kw["include_sources"] = list(include_sources)
    if exclude_sources:
        kw["exclude_sources"] = list(exclude_sources)
    if queries:
        kw["queries"] = list(queries)
    if ident:
        kw.update(ident)

    resp = client.search(query, **kw)
    if _routed_productivity(resp):
        resp = _chat_pinned_search(client, query, kw, queries)
    items = _flatten_search(resp)
    probe_error = ""
    if not items and not tags and probe_empty:
        probe_error = _embedding_unhealthy()
    if remember_auto:
        _remember_auto(items)
    if since is not None or until is not None:
        items = [x for x in items if timefilter._in_range(timefilter._item_ts(x), since, until)]
    if tags:
        items = _filter_by_tags(items, tags, tag_mode)

    filtered_total = len(items)
    page = items[offset: offset + int(limit)]
    has_more = filtered_total > offset + int(limit)

    schemas: list[dict] = []
    schema_state = "off"
    if schema:
        schemas, schema_state = fetch_l6_schemas(
            client,
            schema_query if schema_query is not None else (query or ""),
            user_id=schema_user_id,
            limit=schema_limit,
            min_score=schema_min,
        )

    pending = 0
    if pending_always or not page:
        pending = _pending_jobs()

    return {
        "items": page,
        "filtered_total": filtered_total,
        "has_more": has_more,
        "elapsed_ms": resp.get("elapsed_ms"),
        "min_score": min_score,
        "schemas": schemas,
        "schema_state": schema_state,
        "probe_error": probe_error,
        "pending": pending,
    }


def scenecore(
    client,
    query: str,
    *,
    scene: str,
    workspace_id: str | None = None,
    branch: str | None = None,
    limit: int = 5,
    user_ids: list[str] | None = None,
    min_score: float | None = None,
    include_sources: list[str] | None = None,
    exclude_sources: list[str] | None = None,
    source_filter: str | None = None,
    chain_block: bool = False,
    chain_user_id: str = "default",
) -> dict:
    """场景检索（facts/experiences 双路）的唯一 SDK 调用点。

    双路的返回结构与 chat 单路不同，投影留给调用方；这里只统一参数拼装与
    chain_block 兜底，避免 CLI/MCP 各拼一份参数再漂移。
    """
    kw: dict = {"limit": limit, "user_ids": list(user_ids or [])}
    if min_score is not None:
        kw["min_score"] = min_score
    if include_sources:
        kw["include_sources"] = list(include_sources)
    if exclude_sources:
        kw["exclude_sources"] = list(exclude_sources)
    if source_filter:
        kw["include_sources"] = [source_filter]
    r = client.search(query, scene=scene, workspace_id=workspace_id, branch=branch, **kw)
    if chain_block:
        # SDK 在带 workspace_id 时会把链全滤掉（上游 _in_scope 拿仓名比文件相对
        # 路径），这里用同一份链库兜底补块；无 workspace 时 SDK 自己就出了块。
        r = fill_chain_block(
            client, r, query, user_id=chain_user_id, workspace_id=workspace_id
        )
    return r


def _worker_cap() -> int:
    """消费并发线程数（lane 数）。HY_MEM_MAX_WORKERS 可覆盖，默认 3。

    0.9.8 起语义变化：以前是「detached worker 进程数上限」，现在是单个
    supervisor 进程内的消费线程数。SDK 的 reconcile 是读改写序列，跨进程/
    跨线程必须由 WriteLock 串行（锁覆盖整个 client.add 含 LLM 抽取），
    所以多个消费进程是伪并发——N 个进程只会在锁上排队，还各付一份 SDK
    初始化（hy_memory.log / chroma 的多进程初始化竞态正是历史并发风暴的
    根源）。线程池里锁语义不变，非临界区（snapshot 读、入账、退避调度）
    可与在途写入重叠。
    """
    try:
        return max(1, int(os.getenv("HY_MEM_MAX_WORKERS") or 3))
    except ValueError:
        return 3


def _spawn_detached_worker(idle_exit: float = 90.0,
                           extra_args: list[str] | None = None) -> bool:
    """拉起一个脱管的后台消费 supervisor（空闲自退出），成功返回 True。

    为什么不常驻：智能体场景下「临时拉起 + 空闲自杀」最干净 —— add --async
    入队后立即返回毫秒级，落库由后台进程完成，不留僵尸进程。

    消费并发模型（0.9.8）：detached supervisor 恒为 1 个进程，进程内跑
    N 个消费线程（--lanes，默认 HY_MEM_MAX_WORKERS=3）。进程数恒 1 是
    有意的——见 _worker_cap 的语义说明。WriteLock 与 SQLite 抢占兜底，
    即使拉重了也无害（闸门 cap=1 + 心跳判活）。
    """
    from hymem.core import install_ops

    # 上限闸门（WriteLock 内占位，串行化风暴竞态）：detached supervisor 恒 1 个。
    # 占位行 15s 过期，Popen 失败也不会永久卡死后续拉起。
    try:
        with store.WriteLock():
            conn = store.connect()
            try:
                slot = store.claim_worker_slot(conn, 1)
            finally:
                conn.close()
    except Exception:  # noqa: BLE001  闸门失败宁可多拉不多丢
        slot = True
    if not slot:
        return False

    argv = install_ops.target_command() + [
        "worker",
        "--idle-exit", str(idle_exit),
        "--poll", "1.0",
        "--lanes", str(_worker_cap()),
    ] + (extra_args or [])
    try:
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW：不闪黑框
            # stderr 落文件而非 DEVNULL：worker 若在启动期静默死亡（日志
            # 文件竞态/readonly 等），这里就是唯一现场
            try:
                err_f = open(store.data_dir() / "logs" / "worker.err", "ab")
            except OSError:
                err_f = subprocess.DEVNULL
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f, creationflags=flags, close_fds=True,
            )
        else:
            err_f = None
            try:
                (store.data_dir() / "logs").mkdir(parents=True, exist_ok=True)
                err_f = open(store.data_dir() / "logs" / "worker.err", "ab")
            except OSError:
                err_f = None
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f or subprocess.DEVNULL, start_new_session=True,
            )
        return True
    except OSError:
        return False


def _spawn_detached_serve(idle_exit: float = 1800.0) -> bool:
    """拉起 detached 的向量库服务（数据唯一所有者）。

    与 worker 分开拉：serve 必须长驻单例，worker 天然短命——0.9.9 把两者塞进
    同一个进程，消费者退出就顺手把数据所有者带走了（见 PRD F2/F5）。

    失败只 stderr 留痕、返回 False：调用方（ensure_endpoint）会继续轮询，
    也许已有别的进程把它拉起来了。
    """
    from hymem.core import install_ops

    argv = install_ops.target_command() + ["serve", "--idle-exit", str(idle_exit)]
    try:
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW：不闪黑框
            try:
                logs = store.data_dir() / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                err_f: object = open(logs / "serve.err", "ab")
            except OSError:
                err_f = subprocess.DEVNULL
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f, creationflags=flags, close_fds=True,
            )
        else:
            err_f = None
            try:
                logs = store.data_dir() / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                err_f = open(logs / "serve.err", "ab")
            except OSError:
                err_f = None
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f or subprocess.DEVNULL, start_new_session=True,
            )
        return True
    except OSError:
        return False
