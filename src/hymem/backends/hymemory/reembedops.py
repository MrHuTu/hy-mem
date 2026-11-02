# -*- coding: utf-8 -*-
"""reembed 工具族（docs/specs/2026-11-02-sdk-alignment-gaps/ S4）。

SDK 2.0.11 的向量空间迁移路径：scan → plan → reembed（向量/图）→ drop 旧空间。
换向量模型（改维度/换端点）时唯一官方迁移通道，此前 hy-mem 零包装。

设计约束：
- asyncio.run 只允许出现在 backends/（test_cli 守护断言 G1）——SDK 这组是
  模块级 async 函数，同步包装收在这里；
- embedding 服务复用 clientops.build_embedding_service（配置驱动的唯一出口）；
- CLI 只编排、不碰 asyncio。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "scan_spaces", "plan_reembed", "run_reembed", "run_reembed_graph",
    "drop_space",
]


def _persist_root(root: str | os.PathLike | None = None) -> Path:
    """向量空间根目录：缺省取配置数据目录（SDK 的 chroma 数据面在它下面）。"""
    if root:
        return Path(str(root)).expanduser()
    from hymem.cmds.base import data_dir  # 延迟导入防环（base 不依赖 backends）

    return data_dir()


def scan_spaces(root: str | os.PathLike | None = None, per_user: bool = True) -> list[dict]:
    """扫描向量空间（只读）。返回 ScannedSpace 的 dict 形态。"""
    import hy_memory

    return [s if isinstance(s, dict) else getattr(s, "__dict__", {"repr": repr(s)})
            for s in asyncio.run(hy_memory.scan_vector_spaces(str(_persist_root(root)), per_user=per_user))]


def plan_reembed(source: str | os.PathLike, target: str | os.PathLike,
                 source_graph_path: str | os.PathLike | None = None) -> dict:
    """差量计划（只读）：每个 role 要算多少条向量；带 graph 源时含 Kuzu 图。"""
    import hy_memory

    plan = asyncio.run(hy_memory.plan_reembed(
        str(Path(source).expanduser()), str(Path(target).expanduser()),
        source_graph_path=str(Path(source_graph_path).expanduser()) if source_graph_path else None,
    ))
    return plan if isinstance(plan, dict) else getattr(plan, "__dict__", {"repr": repr(plan)})


def run_reembed(source: str | os.PathLike, target: str | os.PathLike, *,
                batch_size: int = 64, max_inflight: int = 2,
                roles: Optional[list[str]] = None,
                skip_existing: bool = True,
                on_progress: Optional[Callable[[Any], None]] = None) -> dict:
    """全量重嵌入：source 全部记录用当前 embedding 配置写进 target。

    skip_existing=True（默认）：target 已持有的 id 不动——断点续跑、误操作不覆盖。
    返回 dict 形态的汇总（记录数/跳过数/耗时，字段随 SDK 版本）。
    """
    import hy_memory
    from hymem.backends.hymemory.clientops import build_embedding_service

    emb = build_embedding_service()
    kw: dict = {
        "embedding": emb, "batch_size": int(batch_size), "max_inflight": int(max_inflight),
        "roles": roles, "skip_existing": skip_existing,
    }
    if on_progress is not None:
        kw["on_progress"] = on_progress
    try:
        out = asyncio.run(hy_memory.reembed_vector_space(
            str(Path(source).expanduser()), str(Path(target).expanduser()), **kw))
    except TypeError:
        # 老版本 SDK 没有 on_progress/skip_existing 形参：剥掉可选参数重试一次
        kw.pop("on_progress", None)
        kw.pop("skip_existing", None)
        out = asyncio.run(hy_memory.reembed_vector_space(
            str(Path(source).expanduser()), str(Path(target).expanduser()), **kw))
    return out if isinstance(out, dict) else getattr(out, "__dict__", {"repr": repr(out)})


def run_reembed_graph(source_graph_path: str | os.PathLike, target_graph_path: str | os.PathLike,
                      target_dims: int, *, batch_size: int = 64) -> dict:
    """Kuzu 图向量重建：源图路径 → 目标图路径（新维度）。"""
    import hy_memory
    from hymem.backends.hymemory.clientops import build_embedding_service

    emb = build_embedding_service()
    out = asyncio.run(hy_memory.reembed_graph(
        str(Path(source_graph_path).expanduser()), str(Path(target_graph_path).expanduser()),
        embedding=emb, target_dims=int(target_dims), batch_size=int(batch_size),
    ))
    return out if isinstance(out, dict) else getattr(out, "__dict__", {"repr": repr(out)})


def drop_space(space: str | os.PathLike, roles: Optional[list[str]] = None) -> list[str]:
    """删除向量空间（破坏性，CLI 必须先 --yes）。返回被删的 collection 名。"""
    import hy_memory

    return list(asyncio.run(hy_memory.drop_vector_space(str(Path(space).expanduser()), roles=roles)))


def snapshot_data_plane(source_root: Path, target_root: Path) -> dict:
    """数据面快照（调用方保证静默）。vendor import 收敛点（守护断言 G1）。"""
    import hy_memory

    out = hy_memory.snapshot_local_data_plane(
        source_root=source_root, target_root=target_root, writers_stopped=True)
    return out if isinstance(out, dict) else getattr(out, "__dict__", {"repr": repr(out)})


def restore_data_plane(snapshot_root: Path, target_root: Path) -> dict:
    """从快照恢复数据面（SDK 自带回滚）。vendor import 收敛点。"""
    import hy_memory

    out = hy_memory.restore_local_data_plane(
        snapshot_root=snapshot_root, target_root=target_root,
        writers_stopped=True, protective_root=None)
    return out if isinstance(out, dict) else getattr(out, "__dict__", {"repr": repr(out)})
