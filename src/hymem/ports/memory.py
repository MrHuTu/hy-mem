# -*- coding: utf-8 -*-
"""MemoryBackend —— 记忆后端协议（JDBC 式：接口在上，实现可换）。

设计（docs/specs/2026-11-02-cli-split-ports/，用户拍板 2026-11-02）
------------------------------------------------------------------
* 本文件**不 import vendor**（hy_memory）：这里是协议，不是实现。
* 实现方：``hymem.backends.hymemory``（当前）；未来可加 memvid 等开源实现，
  只在 ``ports/registry.py`` 注册，调用方（cmds / mcp）零改动。
* 用 ``typing.Protocol``（结构化鸭子类型）：实现方不需要继承任何东西，
  长出同名方法即视为满足协议——换实现时没有继承负担。

三段能力面（全仓实测 2026-11-02，CLI 15 处 + MCP 6 处 + core ops 全量 union）：
  1. 基础记忆操作（CLI/MCP 共用）
  2. 沉淀能力（L6 行为模式归纳）
  3. 内部能力协议化（vendor 私有触点收编为语义化方法）

注意：方法签名写的是「调用方实际怎么用」，具体参数语义见各实现的 docstring。
"""
from __future__ import annotations

from typing import Any, Callable, Iterator, Optional, Protocol


class MemoryBackend(Protocol):
    """记忆后端协议。所有方法返回值 = SDK 原样 dict（调用方自行解析）。"""

    # ------------------------------------------------------------------
    # 1. 基础记忆操作
    # ------------------------------------------------------------------
    def add(self, text: Optional[str] = None, **kw) -> dict:
        """写一条记忆。text=chat 正文；messages=工具轨迹（scene=working/coding）；
        kw 透传 scene/workspace_id/branch/tags/metadata/memory_at/request_id/async_。"""
        ...

    def get(self, memory_id: str, **kw) -> dict:
        """按 id 读一条。"""
        ...

    def update(self, memory_id: str, content: str, **kw) -> dict:
        """整条替换正文（旧版本进演化链）。"""
        ...

    def delete(self, memory_id: str, **kw) -> dict:
        """删一条（含 {success, found} 结构）。"""
        ...

    def delete_all(self, **kw) -> dict:
        """按范围批量删（forget）。"""
        ...

    def search(self, query: str, **kw) -> dict:
        """语义检索。kw: limit/min_score/user_ids/agent_ids/session_ids/created_after…"""
        ...

    def list_memories(self, **kw) -> dict:
        """按时间列出。kw: limit/offset/order/tags…"""
        ...

    def history(self, memory_id: str, **kw) -> dict:
        """一条记忆的演化链版本历史。"""
        ...

    def get_recent_history(self, **kw) -> dict:
        """最近的变更流水。"""
        ...

    def finalize_session(self, **kw) -> dict:
        """收尾 session（触发 SDK 演化）。"""
        ...

    def get_write_outcome(self, request_id: str, **kw) -> dict:
        """按幂等 id 查写入最终结果。"""
        ...

    def get_user_basic_info(self, **kw) -> dict:
        """L0 用户画像。"""
        ...

    def get_metrics(self, **kw) -> dict:
        """运行指标。"""
        ...

    def close(self) -> None:
        """释放底层资源（向量连接/事件循环线程）。"""
        ...

    # ------------------------------------------------------------------
    # 2. 沉淀能力（L6）
    # ------------------------------------------------------------------
    def evolve(self, **kw) -> dict:
        """L6 行为模式归纳（ultra）。kw: model_override/max_batches/user_id/language。"""
        ...

    # ------------------------------------------------------------------
    # 3. 内部能力协议化（vendor 私有触点收编；memvid 按各自机制实现）
    # ------------------------------------------------------------------
    def count_facts(self, user_id: str, layer: Any = None, status: Any = None) -> int:
        """数某身份下指定层/状态的 ACTIVE 事实数（自动沉淀阈值判断用）。
        hy-memory 实现 = 走图库 L2_FACT；memvid 可走自己的元数据索引。
        失败返回 -1（调用方跳过本轮阈值判断），绝不抛。"""
        ...

    def llm_model_swap(self, model: str = "", max_tokens: Any = None) -> Iterator[None]:
        """上下文管理器：临时换 LLM 模型（evolve 归纳用强模型，完事还原）。
        进出必须还原原值——MCP 里这个 backend 还要继续服务 add/search。"""
        ...

    def chain_diff_blocks(self, resp: dict, query: str, *, user_id: str, **kw) -> list:
        """scene=coding 检索的 diff 演进块补齐（SDK 在 workspace 作用域下滤空的上游缺陷绕过）。"""
        ...

    def submit_in_writer_lock(self, fn: Callable) -> Any:
        """在 SDK 的写者通道上执行 fn(writer)（两段式提交的锁内段走这里，与 client.add 同通道）。"""
        ...
