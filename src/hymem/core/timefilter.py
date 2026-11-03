"""时间参数解析与过滤（CLI / MCP 共用的纯函数）。

原本住在 mcp/server.py，导致 cmds/base.py 反向 import MCP 层（P1-8）。
这里只依赖 stdlib，不碰任何业务对象，两边按同一口径解析 since/until。
"""
from __future__ import annotations

import re
import time
from datetime import datetime


class ParamError(ValueError):
    """调用方参数不对（缺参/空参/类型不符）。

    单独建类而不是裸 raise ValueError：参数错和运行时错的建议完全不同——
    参数错只需直说哪里错了，不该套上「先跑 doctor」这类运行时建议误导宿主。
    继承 ValueError 保持向后兼容。
    """


_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}


def _parse_time(v) -> float | None:
    """since/until 参数 → unix 秒。

    两种形态都收：相对值（30m/12h/7d/30d/1y——LLM 问「近一周」最顺手）和
    ISO 日期（2026-01-01 / 2026-01-01T09:30:00）。空/缺省 = None = 不过滤。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*([smhdwy])", s, re.IGNORECASE)
    if m:
        return time.time() - int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    raise ParamError(f"时间格式不认识：{s!r}（支持 30m/12h/7d/1y 相对值，或 2026-01-01 这类日期）")


def _item_ts(m: dict) -> float | None:
    """条目 → unix 秒。优先 memory_at（补录的「记忆发生时间」，add --memory-at
    写入，语义上时间过滤就该按它）；没有补录才退回 gmt_created（实测 list/get
    行是字符串 unix 秒）；其余候选是防御性兼容。"""
    for k in ("memory_at", "gmt_created", "created_at", "createdTime", "when", "updated_at"):
        v = m.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            ts = float(v)
            return ts / 1000.0 if ts > 1e12 else ts  # 毫秒时间戳兜底
        s = str(v).strip()
        try:
            ts = float(s)
            return ts / 1000.0 if ts > 1e12 else ts
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:19], fmt).timestamp()
            except ValueError:
                continue
    return None


def _in_range(ts: float | None, since: float | None, until: float | None) -> bool:
    """无时间戳的条目放行（宁可多给不错杀），有时间戳才按窗过滤。"""
    if ts is None:
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True
