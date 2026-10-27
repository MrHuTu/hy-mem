"""本地离线嵌入服务 —— 实现 Hy-Memory 的 EmbeddingService 协议.

动机：Hy-Memory 2.0.5 的 embedding.py 只定义了 Protocol（53 行），实际嵌入必须打
OpenAI 兼容端点。本机网关无 embedding 通道、Ollama 未部署，故用 fastembed
(ONNX Runtime，已是 hy-memory 的既有依赖) 在进程内直接推理：

  - 零 API key、零网络（首次下载模型后）
  - 无常驻服务进程（对比：包成 MCP 就得常驻，还要处理 db 写锁）
  - bge-small-zh-v1.5 对中文检索优于同尺寸 MiniLM

代价：维度固定 512，必须与 MEMORY_EMBEDDING_DIMS 一致，否则 SDK 抛
EmbeddingDimensionMismatchError。
"""

from __future__ import annotations

import asyncio
import os
import warnings
from pathlib import Path
from typing import Any, Dict, List, Tuple

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_DIMS = 512
_ROOT = Path(__file__).resolve().parent.parent


def _default_cache_dir() -> Path:
    """ONNX 模型缓存位置：优先显式配置，其次数据目录（随备份/迁移一起走），
    最后回落到安装根下的 .models。放数据目录的好处是换包不重下 92MB。"""
    env = os.getenv("MEMORY_LOCAL_EMBED_CACHE")
    if env:
        return Path(env)
    dd = os.getenv("MEMORY_DATA_DIR")
    if dd:
        return Path(dd) / "models"
    return _ROOT / ".models"


class LocalEmbeddingService:
    """满足 hy_memory.EmbeddingService 协议的本地实现。

    模型懒加载：只读命令（doctor/help）不会触发 75s 的冷启动。
    """

    def __init__(
        self,
        model_name: str | None = None,
        cache_dir: str | os.PathLike | None = None,
        dims: int | None = None,
    ) -> None:
        self._model_name = model_name or os.getenv("MEMORY_LOCAL_EMBED_MODEL", DEFAULT_MODEL)
        self._cache_dir = str(cache_dir or _default_cache_dir())
        self._dims = int(dims or os.getenv("MEMORY_EMBEDDING_DIMS") or DEFAULT_DIMS)
        self._model = None
        self._cache: Dict[str, List[float]] = {}

    # -- 懒加载 ------------------------------------------------------------
    def _ensure(self):
        if self._model is None:
            # 国内直连 huggingface 基本会挂，默认走镜像；离线环境可设 HF_HUB_OFFLINE=1
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            # 我们已经用 HF_HUB_DISABLE_PROGRESS_BARS=1 关掉进度条，但 fastembed /
            # transformers 内部还会调 huggingface_hub 的 enable_progress_bars()，
            # 于是每次都在控制台吐一条 UserWarning（"…has priority"）。它既不是
            # 错误也不是用户能处理的事，向导里看着像报错，直接压掉。
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=".*[Cc]annot enable progress bars.*")
                from fastembed import TextEmbedding

                self._model = TextEmbedding(model_name=self._model_name, cache_dir=self._cache_dir)
        return self._model

    def _embed_sync(self, text: str) -> List[float]:
        m = self._ensure()
        vec = next(iter(m.embed([text])))
        out = [float(x) for x in vec]
        if len(out) != self._dims:
            raise ValueError(
                f"嵌入维度 {len(out)} 与 MEMORY_EMBEDDING_DIMS={self._dims} 不一致，"
                f"请同步修改 .env 后清空数据目录重建"
            )
        return out

    def _embed_batch_sync(self, texts: List[str]) -> List[List[float]]:
        m = self._ensure()
        return [[float(x) for x in v] for v in m.embed(list(texts))]

    # -- 协议实现 ----------------------------------------------------------
    async def embed(self, text: str, use_cache: bool = True) -> List[float]:
        if use_cache and text in self._cache:
            return self._cache[text]
        vec = await asyncio.to_thread(self._embed_sync, text)
        if use_cache:
            self._cache[text] = vec
        return vec

    async def embed_with_meta(self, text: str, use_cache: bool = True) -> Tuple[List[float], Dict[str, Any]]:
        vec = await self.embed(text, use_cache=use_cache)
        return vec, {"model": self._model_name, "dims": self._dims, "provider": "local"}

    async def embed_batch(self, texts: List[str], use_cache: bool = True) -> List[List[float]]:
        return await asyncio.to_thread(self._embed_batch_sync, list(texts))

    async def embed_queued(self, text: str, use_cache: bool = True) -> List[float]:
        return await self.embed(text, use_cache=use_cache)

    async def embed_queued_with_meta(self, text: str, use_cache: bool = True) -> Tuple[List[float], Dict[str, Any]]:
        return await self.embed_with_meta(text, use_cache=use_cache)

    def get_embedding_dims(self) -> int:
        return self._dims
