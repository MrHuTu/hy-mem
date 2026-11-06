# -*- coding: utf-8 -*-
"""add 拆条规则（CLI `--split` 与 MCP `add_memory.split` 共用，只维护一处）。

为什么要共享：两边各写一份「按行拆 + 上限 + 告警」迟早漂移，
`test_mcp_split` 与 `test_cli` 一旦口径不同，就会出现「MCP 拆 50 条、
CLI 拆 55 条」这种没人能解释的行为差。
"""
from __future__ import annotations

# 单次 add 最多拆这么多条：超出的分片**不入库**，但必须回报，不静默丢。
SPLIT_MAX = 50


def split_lines(text: str) -> list[str]:
    """按行拆条。一次 add 的多个事实会被压成一条 l2_fact，稀释向量拉低召回。

    单行文本原样返回；空行丢弃、行首尾空白裁掉。只按换行拆，不按标点/长度切。
    """
    parts = [p.strip() for p in text.replace("\r", "").split("\n") if p.strip()]
    return parts if len(parts) > 1 else [text]


def split_limited(text: str, enabled: bool) -> tuple[list[str], int]:
    """按需拆条并施加 SPLIT_MAX；返回 (实际写入的 parts, 被截断的条数)。"""
    if not enabled:
        return [text], 0
    parts = split_lines(text)
    if len(parts) > SPLIT_MAX:
        dropped = len(parts) - SPLIT_MAX
        return parts[:SPLIT_MAX], dropped
    return parts, 0


def truncation_warning(kept: int, dropped: int) -> str:
    """截断告警文案；未截断时返回空串。"""
    if dropped <= 0:
        return ""
    return (f"按行拆出 {kept + dropped} 条，超过单次上限 {SPLIT_MAX}，"
            f"还有 {dropped} 条未写入")


def derive_request_id(request_id: str, index: int) -> str:
    """拆条时的分片幂等 id：`<rid>#<i>`（1-based）。

    规则只依赖入参（原文 request_id + 分片序号），因此同一输入重试会得到
    同样的分片 id——幂等键稳定，不会因重试重复写；不同分片之间又互不重复，
    SDK 不会把第 2 片当成第 1 片的重复。
    """
    return f"{request_id}#{index}"


def part_request_id(request_id: str | None, index: int, total: int) -> str | None:
    """分片幂等 id：未传 / 只有一片时原样返回，多片时 `<rid>#<i>`。"""
    if not request_id or total <= 1:
        return request_id
    return derive_request_id(request_id, index)
