# -*- coding: utf-8 -*-
"""backend 注册表与选择入口（JDBC 的 DriverManager 角色）。

用法（cmds / mcp 统一走这里，绝不直接 import backends）：
    from hymem.ports.registry import make_backend
    backend = make_backend(mode)          # 按配置选实现

换实现（如 memvid）：写 ``hymem/backends/memvid/backend.py`` 满足
``ports.memory.MemoryBackend`` 协议，在 ``_REGISTRY`` 加一行注册即可，
调用方零改动。未知 backend 名当场报错并列出可用项——绝不静默回落。
"""
from __future__ import annotations

from hymem.ports.memory import MemoryBackend

# name -> 工厂(mode) -> MemoryBackend。工厂内延迟 import：ports 保持轻量，
# doctor / --help 等不碰记忆的路径不为 backend 付 import 成本。
_REGISTRY: dict[str, callable] = {
    "hy-memory": lambda mode: _hy_memory_backend(mode),
}


def _hy_memory_backend(mode: str | None):
    from hymem.backends.hymemory.backend import HyMemBackend

    return HyMemBackend(mode)


def available_backends() -> list[str]:
    return sorted(_REGISTRY)


def make_backend(mode: str | None = None, cfg: dict | None = None) -> MemoryBackend:
    """按 config.backend.memory（默认 hy-memory）构造记忆后端。"""
    if cfg is None:
        from hymem.core import config as cfgmod

        cfg = cfgmod.load()
    name = str(((cfg or {}).get("backend") or {}).get("memory") or "hy-memory").strip().lower()
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(
            f"未知记忆后端：{name!r}（可用：{', '.join(available_backends())}）；"
            "修：hy-mem config set backend.memory=<名称>"
        )
    return factory(mode)
