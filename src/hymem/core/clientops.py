# -*- coding: utf-8 -*-
"""

S3-1（2026-10-29）：本模块从入口 cli.py 平移而来（纯搬家，逻辑零变化）。
依赖方向：core 内部互引 + stdlib/SDK；不依赖 ui/mcp/入口。

"""
from __future__ import annotations

import os
from hymem.core import config as cfgmod
from hymem.core import vdb_endpoint


def embedding_provider() -> str:
    """当前生效的嵌入来源：环境变量 > config.json > openai。

    不能简单把缺省当 local：全新安装时 env 里什么都没有，落成 local 就会在
    第一次 doctor 时去下载 92MB 的 ONNX 模型（国内还要等 40s+），而用户
    真正选的可能就是云端向量模型。config 的默认值本来就是 openai，保持一致。
    """
    p = (os.getenv("MEMORY_EMBEDDER_PROVIDER") or "").strip().lower()
    if p:
        return p
    try:
        from hymem.core import config as cfgmod

        return str((cfgmod.load().get("embedding") or {}).get("provider") or "openai").strip().lower()
    except Exception:  # noqa: BLE001
        return "openai"


def build_embedding_service():
    """按嵌入来源决定是否注入进程内实例；local 时返回该实例。"""
    provider = embedding_provider()
    if provider != "local":
        return None
    from hymem.core.local_embedder import LocalEmbeddingService

    svc = LocalEmbeddingService()
    # 先把维度对齐到环境变量，SDK 初始化时会校验二者一致
    os.environ["MEMORY_EMBEDDING_DIMS"] = str(svc.get_embedding_dims())
    return svc


def make_client(mode: str | None):
    from hy_memory import HyMemoryClient  # 延迟导入，doctor 失败时也能跑

    # 端点注入（2026-10-28 事故收敛）：有 worker 承载的 chroma server 就走
    # HttpClient（SDK 按 MEMORY_VECTOR_HOST/PORT 自动切换），没有则拉起
    # worker 等就绪。客户端进程从此不再自开嵌入式实例——chroma embedded
    # 不支持多进程共享，长驻进程会读到过期/空的向量索引。
    vdb_endpoint.ensure_endpoint()
    svc = build_embedding_service()
    kwargs = {}
    if mode:
        kwargs["mode"] = mode
    if svc is not None:
        kwargs["embedding_service"] = svc
    # SDK 在 __init__ 里初始化 hy_memory.log（TimedRotatingFileHandler）和
    # chroma 向量库连接。多进程同秒并发初始化时 Windows 上会互踩：
    #   - 日志打开/轮转 → PermissionError (Errno 13)
    #   - chroma WAL -shm 重建窗口 → code 8 readonly database
    # 实测并发风暴命中率约 1/4——指数退避重试，让先到者完成初始化。
    last: Exception | None = None
    import time as _time  # 2026-10-29 修：此处 time 未定义（并发 init 竞态重试路径才触发，NameError 曾被风暴暴露）

    for i in range(5):
        try:
            return HyMemoryClient(**kwargs)
        except Exception as e:  # noqa: BLE001
            s = str(e).lower()
            if not (isinstance(e, PermissionError)
                    or "readonly" in s or "locked" in s or "busy" in s):
                raise
            last = e
            _time.sleep(0.2 * (i + 1))
    raise last  # type: ignore[misc]


# ---------------------------------------------------------------------------
# R1（2026-10-29）：delete 级联清扫派生节点
#
# 上游 SDK 缺陷（docs/specs/2026-10-29-fix-delete-ghost-and-add-readonly §0.B1）：
# client.delete(memory_id) 只删根节点本身；抽取产生的 l2_fact 派生节点带
# source_raw_memory_id 指向根，delete 后残留且仍可被检索（幽灵节点）。
# 且 ChromaVectorStore.delete 恒 return True，CLI 侧若不校验 found/success
# 会双重假成功。本模块提供确定性的残留发现与级联删除（走公开 client.delete，
# 保留 SDK 的演化链/标签索引簿记），SDK 升级修复级联后本助手自动变空转。
# ---------------------------------------------------------------------------

def _vector_collection(client: object):
    """拿 SDK 内部 chroma collection；内部结构变化时返回 None（调用方降级）。"""
    vs = getattr(client, "_vector_store", None)
    return getattr(vs, "_collection", None)


def find_derived_nodes(client: object, root_id: str) -> list[str]:
    """发现 source_raw_memory_id 指向 root_id 的全部派生节点 id。

    只读不删。内部结构不可达或查询失败时返回 []（调用方无法区分
    「无残留」与「查不了」，需要区分时用 sweep_derived_nodes 的 note）。
    """
    col = _vector_collection(client)
    if col is None:
        return []
    try:
        res = col.get(where={"source_raw_memory_id": {"$eq": root_id}}, include=[])
        return list((res or {}).get("ids") or [])
    except Exception:  # noqa: BLE001
        return []


def sweep_derived_nodes(client: object, root_id: str) -> tuple[list[str], str]:
    """级联删除 root_id 的残留派生节点。返回 (purged_ids, note)。

    note 非空表示「无法确认是否残留」（内部结构变化/查询失败），调用方
    必须把它透传给用户——静默当成功会重新制造假绿。
    """
    col = _vector_collection(client)
    if col is None:
        return [], "SDK 内部结构变化，无法定位派生节点（可能残留且可检索）"
    try:
        res = col.get(where={"source_raw_memory_id": {"$eq": root_id}}, include=[])
        ids = list((res or {}).get("ids") or [])
    except Exception as e:  # noqa: BLE001
        return [], f"派生节点查询失败（可能残留且可检索）：{e}"
    purged: list[str] = []
    for nid in ids:
        try:
            r = client.delete(nid)
            if r and r.get("success"):
                purged.append(nid)
        except Exception:  # noqa: BLE001
            pass
    return purged, ""


def is_transient_db_error(e: BaseException) -> bool:
    """SQLite WAL 重建窗口的瞬态占用（R2）：与 make_client 的 init 重试同口径。"""
    if isinstance(e, PermissionError):
        return True
    s = str(e).lower()
    return "database" in s and ("readonly" in s or "locked" in s or "busy" in s)


def add_sync_with_retry(client: object, text: str, *, attempts: int = 4, **kw):
    """同步 add 包一层瞬态重试（readonly/locked 换次重跑，非瞬态直接抛）。

    已知代价：若失败点在部分写入之后，重试可能产生重复条目——reconcile 的
    判重/合并会在后续写入时吸收重复；对比硬失败（整条丢失且报错误导），
    取舍为重试。队列消费侧（_execute_job）自带退避重试，不走这里。
    重试耗尽仍瞬态占用 → 抛出，由 errors.classify_error 归为 E_DB_BUSY
    （干净失败：明确 hint「稍等重跑」，绝不指向密钥）。
    """
    import time as _time

    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return client.add(text, **kw)
        except Exception as e:  # noqa: BLE001
            if not is_transient_db_error(e) or i == attempts - 1:
                raise
            last = e
            _time.sleep(0.3 * (i + 1))
    raise last  # type: ignore[misc]
