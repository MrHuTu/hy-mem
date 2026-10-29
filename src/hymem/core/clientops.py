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
    for i in range(5):
        try:
            return HyMemoryClient(**kwargs)
        except Exception as e:  # noqa: BLE001
            s = str(e).lower()
            if not (isinstance(e, PermissionError)
                    or "readonly" in s or "locked" in s or "busy" in s):
                raise
            last = e
            time.sleep(0.2 * (i + 1))
    raise last  # type: ignore[misc]
