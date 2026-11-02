# -*- coding: utf-8 -*-
"""HyMemBackend —— ports.MemoryBackend 的 hy-memory 实现（当前唯一驱动）。

规则：**vendor（hy_memory）的 import 只准出现在 backends/ 目录**。调用方
（cmds / mcp / tests）只认 ``hymem.ports.registry.make_backend`` 返回的对象。

``__getattr__`` 透传：未在本类显式列出的属性原样转发给底层 SDK client——
迁移期 ops 助手仍会摸 SDK 私有成员（``_registry``/``_loop_thread``/``_config``），
透传保证它们零改动工作；换新 backend 时未实现的面会当场 AttributeError，
缺什么补什么，不会静默走错。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from hymem.backends.hymemory.clientops import make_client as _make_sdk_client


class HyMemBackend:
    """hy-memory 驱动。显式方法 = 协议面；其余透传。"""

    def __init__(self, mode: str | None = None):
        self.__dict__["_client"] = _make_sdk_client(mode)

    # ------------------------------------------------------------------
    # 1. 基础记忆操作（签名对齐 ports/memory.py）
    # ------------------------------------------------------------------
    def add(self, text: Optional[str] = None, **kw) -> dict:
        return self._client.add(text, **kw)

    def get(self, memory_id: str, **kw) -> dict:
        return self._client.get(memory_id, **kw)

    def update(self, memory_id: str, content: str, **kw) -> dict:
        return self._client.update(memory_id, content, **kw)

    def delete(self, memory_id: str, **kw) -> dict:
        return self._client.delete(memory_id, **kw)

    def delete_all(self, **kw) -> dict:
        return self._client.delete_all(**kw)

    def search(self, query: str, **kw) -> dict:
        return self._client.search(query, **kw)

    def list_memories(self, **kw) -> dict:
        return self._client.list_memories(**kw)

    def history(self, memory_id: str, **kw) -> dict:
        return self._client.history(memory_id, **kw)

    def get_recent_history(self, **kw) -> dict:
        return self._client.get_recent_history(**kw)

    def finalize_session(self, **kw) -> dict:
        return self._client.finalize_session(**kw)

    def get_write_outcome(self, request_id: str, **kw) -> dict:
        return self._client.get_write_outcome(request_id, **kw)

    def get_user_basic_info(self, **kw) -> dict:
        return self._client.get_user_basic_info(**kw)

    def get_metrics(self, **kw) -> dict:
        return self._client.get_metrics(**kw)

    def close(self) -> None:
        return self._client.close()

    # ------------------------------------------------------------------
    # 2. 沉淀能力
    # ------------------------------------------------------------------
    def evolve(self, **kw) -> dict:
        return self._client.evolve(**kw)

    # ------------------------------------------------------------------
    # 3. 内部能力协议化
    # ------------------------------------------------------------------
    def count_facts(self, user_id: str, layer: Any = None, status: Any = None) -> int:
        from hymem.backends.hymemory.autoevops import _fact_count

        return _fact_count(self._client, user_id)

    @contextmanager
    def llm_model_swap(self, model: str = "", max_tokens: Any = None) -> Iterator[None]:
        """临时换 LLM 模型，退出必还原（原值先存，finally 恢复）。"""
        llm = self._client._config.llm
        old_model = getattr(llm, "model", "")
        old_mt = getattr(llm, "agent_max_tokens", None)
        try:
            if model:
                llm.model = model
            if max_tokens is not None:
                llm.agent_max_tokens = max_tokens
            yield
        finally:
            llm.model = old_model
            if old_mt is not None:
                llm.agent_max_tokens = old_mt

    def chain_diff_blocks(self, resp: dict, query: str, *, user_id: str, **kw) -> list:
        from hymem.backends.hymemory.searchops import fill_chain_block

        return fill_chain_block(self._client, resp, query, user_id=user_id, **kw)

    def submit_in_writer_lock(self, fn: Callable) -> Any:
        from hymem.backends.hymemory.two_phase import _writer_of

        return fn(_writer_of(self._client))

    # ------------------------------------------------------------------
    # 透传（迁移期兼容）：显式面之外的一切（含 SDK 私有成员）原样转发
    # ------------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        client = self.__dict__.get("_client")
        if client is None or name == "_client":
            raise AttributeError(name)
        return getattr(client, name)
