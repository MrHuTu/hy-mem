"""hy-mem 的 MCP stdio 服务器。

为什么：智能体经 SKILL.md 约定调 CLI 是「文本约定」，经 MCP 是「原生工具」——
宿主自动发现、结构化参数、无需猜命令。stdio 传输由宿主按会话拉起本进程，
随会话退出，不常驻；常驻消费由 add --async 的自动拉起 worker 负责（见
hy_mem_cli._spawn_detached_worker）。

协议：JSON-RPC 2.0，换行分隔（MCP stdio 规范）。只实现必需面：
initialize / notifications/initialized / ping / tools/list / tools/call。

铁律：**stdout 只准出现协议响应**。任何诊断输出一律走 stderr（日志文件会
污染只有 stderr 的宿主界面，所以直接丢弃细节、只留最少量提示）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime

import errors  # CLI/MCP 共用的错误分类与格式化
from helptext import VERSION

SERVER_NAME = "hy-mem"
SERVER_VERSION = VERSION  # 单一来源：CLI --version / help / MCP initialize 三处共用

# JSON-RPC 错误码（常用三个，够用）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class ParamError(ValueError):
    """调用方参数不对（缺参/空参/类型不符）。

    单独建类而不是裸 raise ValueError：参数错和运行时错的建议完全不同——
    参数错只需直说哪里错了，不该套上「先跑 doctor」这类运行时建议误导宿主。
    继承 ValueError 保持向后兼容。
    """


# --------------------------------------------------------------------------
# 时间参数 —— since/until 支持相对值与 ISO 日期两种形态
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# 工具实现：进程内直调 SDK，不走子进程（省 ~1s 冷启动/次）
# --------------------------------------------------------------------------
def _identity() -> dict:
    """MCP 会话统一用默认身份（与 CLI 不传 -u 时同口径）。"""
    try:
        import config as cfgmod

        u = cfgmod.identity_defaults() or "default"
    except Exception:  # noqa: BLE001
        u = "default"
    return {"user_id": u}


def _client():
    from hy_mem_cli import make_client

    return make_client(None)


def _tool_add_memory(args: dict) -> str:
    """写入记忆。默认走异步队列（毫秒返回，worker 自动拉起落库）。"""
    from hy_mem_cli import _spawn_detached_worker, _parse_memory_at

    text = str(args.get("text") or "").strip()
    if not text:
        raise ParamError("text 不能为空：写入记忆必须提供要记的内容")
    tags = [t for t in (args.get("tags") or []) if str(t).strip()]
    use_async = args.get("async", True) is not False
    # SDK 透传参数：metadata（对象）/ memory_at（补录时间）/ request_id（幂等）
    metadata = args.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise ParamError("metadata 必须是对象（object），如 {\"project\": \"iisw\"}")
    request_id = str(args.get("request_id") or "").strip() or None
    try:
        memory_at = _parse_memory_at(args.get("memory_at"))
    except ValueError as e:
        raise ParamError(str(e))
    client = _client()
    if not use_async:
        from hy_mem_cli import _ids_of, _new_ids_only, _bind_tags
        from hy_mem_cli import store

        before = set()
        if tags:
            try:
                before = {
                    m.get("memory_id")
                    for m in client.list_memories(limit=200, **_identity()) or []
                    if isinstance(m, dict) and m.get("memory_id")
                }
            except Exception:  # noqa: BLE001
                before = set()
        add_kw = (
            {"metadata": metadata, "memory_at": memory_at, "request_id": request_id}
        )
        add_kw = {k: v for k, v in add_kw.items() if v is not None}
        with store.WriteLock():
            last = client.add(text, source="mcp", **add_kw, **_identity())
        ids = _ids_of(last)
        if tags:
            _bind_tags(_new_ids_only(ids, before), tags, "user")
        return json.dumps(
            {"mode": "sync", "memory_ids": ids, "tags": tags}, ensure_ascii=False
        )
    # 异步：入队 + 无人消费时自动拉起临时 worker（与 CLI add --async 同一套）
    from hy_mem_cli import store

    queued_extra = {}
    if metadata:
        queued_extra["metadata"] = metadata
    if memory_at is not None:
        queued_extra["memory_at"] = memory_at.isoformat()  # 队列 payload 必须 JSON 可序列化
    if request_id:
        queued_extra["request_id"] = request_id

    def _enq(conn):
        return [
            store.enqueue(
                conn,
                {
                    "text": text,
                    "source": "mcp",
                    "identity": _identity(),
                    "tags": tags,
                    **queued_extra,
                },
            )
        ]

    job_ids = store.transient_write(_enq)
    conn = store.connect()
    try:
        stats = store.queue_stats(conn)
        hb = store.worker_age(conn)
    finally:
        conn.close()
    live = hb is not None and hb < 15.0
    spawned = False
    if stats["pending"] > 0 and not live:
        spawned = _spawn_detached_worker()
    return json.dumps(
        {
            "mode": "async",
            "job_ids": job_ids,
            "pending": stats["pending"],
            "worker_spawned": spawned,
            # 已有 worker 在跑（心跳新鲜）时 spawned 必为 False——这同样是
            # 「落库有人管」，别让调用方把它读成失败
            "worker_live": live,
            # 进度怎么查：调度方拿到 job_ids 就能用 queue_status 跟踪，
            # 不必用「search 空结果」反推落库与否
            "progress_tool": "queue_status",
            "note": "已入队，后台 worker 约 6~9 秒落库；查进度用 queue_status(job_ids=[...])",
        },
        ensure_ascii=False,
    )


_TERMINAL_JOB = ("done", "dead")


def _tool_queue_status(args: dict) -> str:
    """异步写入进度：自己刚提交的 job 到底落库了没有。

    为什么需要：add_memory 默认异步（毫秒返回），智能体只能「过一会儿再
    search」。search 空手时它很容易得出「没有相关记忆」——真相常常是
    「还在队列里」。这里给可轮询的事实：每条 job 的状态/耗时/错误、worker
    是否活着、队列是否已清空。wait_seconds 支持短阻塞，省得宿主自己 sleep。
    """
    from hy_mem_cli import store

    raw_ids = args.get("job_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = [raw_ids]
    job_ids: list[int] = []
    for x in raw_ids:
        try:
            job_ids.append(int(x))
        except (TypeError, ValueError):
            raise ParamError(f"job_ids 只能是整数（add_memory 返回的 job_ids），收到 {x!r}")
    try:
        wait = float(args.get("wait_seconds") or 0)
    except (TypeError, ValueError):
        raise ParamError("wait_seconds 必须是秒数（数字，上限 60）")
    wait = max(0.0, min(wait, 60.0))
    try:
        recent = max(1, min(int(args.get("limit") or 10), 50))
    except (TypeError, ValueError):
        raise ParamError("limit 必须是整数（1~50）")

    deadline = time.time() + wait
    while True:
        conn = store.connect()
        try:
            stats = store.queue_stats(conn)
            age = store.worker_age(conn)
            workers = store.count_live_workers(conn)
            jobs = store.get_jobs(conn, job_ids) if job_ids else store.list_jobs(conn, None, recent)
        finally:
            conn.close()
        if job_ids:
            busy = any(j.get("status") not in _TERMINAL_JOB for j in jobs)
        else:
            busy = stats["pending"] > 0 or stats["running"] > 0
        if not busy or time.time() >= deadline:
            break
        time.sleep(min(1.0, max(0.05, deadline - time.time())))

    now = time.time()
    live = age is not None and age < 15.0
    out_jobs = []
    for j in jobs:
        started, finished = j.get("started_at"), j.get("finished_at")
        out_jobs.append({
            "job_id": j.get("id"),
            "status": j.get("status"),
            "attempts": f"{j.get('attempts')}/{j.get('max_attempts')}",
            "text": str((j.get("payload") or {}).get("text") or "")[:60],
            "memory_ids": j.get("memory_ids") or [],
            "queued_s": round(now - float(j["created_at"]), 1) if j.get("created_at") else None,
            "took_s": round(float(finished) - float(started), 1) if started and finished else None,
            "error": (str(j.get("error") or "")[:300] or None),
        })

    drained = stats["pending"] == 0 and stats["running"] == 0
    dead = [j["job_id"] for j in out_jobs if j["status"] == "dead"]
    if dead:
        hint = f"有死信任务 {dead}：看 error 字段；重投用 hy-mem retry <job_id>（或 retry dead 全部）"
    elif not drained and not live:
        hint = "队列有积压但没有 worker 在跑：先跑 hy-mem worker --once（pro 模式每条约 10~50s）"
    elif not drained:
        hint = "仍在落库：pro 模式每条约 10~50s；可再调 queue_status(wait_seconds=30) 等它"
    elif out_jobs and all(j["status"] == "done" for j in out_jobs):
        hint = "已全部落库，可以 search_memory 验证内容"
    else:
        hint = "队列已清空"

    return json.dumps({
        "drained": drained,
        "stats": stats,
        "worker": {
            "live": live,
            "last_beat_s": round(age, 1) if age is not None else None,
            "workers": workers,
        },
        "jobs": out_jobs,
        "hint": hint,
    }, ensure_ascii=False)


def _tool_search_memory(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ParamError("query 不能为空：检索必须提供查询词")
    limit = max(1, min(int(args.get("limit") or 5), 20))
    min_score = args.get("min_score")
    offset = max(0, int(args.get("offset") or 0))
    since = _parse_time(args.get("since"))
    until = _parse_time(args.get("until"))
    from hy_mem_cli import _flatten_search

    client = _client()
    # search 的身份参数是复数形参（user_ids），与 CLI identity_lists 同口径；
    # 单数 user_id 会被 SDK 拒收（TypeError: unexpected keyword argument）
    u = _identity()["user_id"]
    # 翻页 = 多取前 offset 条再切片（SDK search 按 score 排序，无原生 offset）；
    # 拉取上限 200 防深翻页拖慢——语义检索翻到 200 名开外本来就没有意义
    fetch = min(offset + limit, 200)
    kw = {"created_after": since} if since is not None else {}  # SDK 原生时间下限
    r = client.search(query, limit=fetch, min_score=min_score, user_ids=[u], **kw)
    items = _flatten_search(r)
    if not items:
        # SDK 吞掉嵌入端异常静默返回空列表（见 cmd_search 同款防护）——
        # 空手时补一次真实探测，端点挂了把真实原因当错误报出去，
        # 别让宿主把「密钥失效」误读成「没有相关记忆」。探测通过（why 空）
        # 则是窗口内真没命中，照常返回空 hits。
        from hy_mem_cli import _embedding_unhealthy

        why = _embedding_unhealthy()
        if why:
            raise RuntimeError(f"检索失败：嵌入端点不可用（{why}）—— 这不代表没有相关记忆")
    if since is not None or until is not None:
        items = [x for x in items if _in_range(_item_ts(x), since, until)]
    page = items[offset : offset + limit]
    has_more = len(items) > offset + limit
    out = [
        {
            "memory_id": it.get("memory_id"),
            "content": it.get("content") or it.get("text"),
            "score": it.get("score"),
            "when": it.get("when") or it.get("created_at"),
        }
        for it in page
    ]
    resp = {"query": query, "count": len(out), "hits": out, "offset": offset}
    if has_more:
        resp["has_more"] = True
        resp["next_offset"] = offset + limit
    return json.dumps(resp, ensure_ascii=False)


def _tool_list_memories(args: dict) -> str:
    limit = max(1, min(int(args.get("limit") or 10), 50))
    offset = max(0, int(args.get("offset") or 0))
    order = str(args.get("order") or "desc").lower()
    if order not in ("desc", "asc"):
        raise ParamError("order 只能是 desc（最新在前，默认）或 asc（最旧在前）")
    since = _parse_time(args.get("since"))
    until = _parse_time(args.get("until"))
    client = _client()
    # SDK 返回 dict（{"vdb": {"memories": [...]}}），与 CLI cmd_list 同解析；
    # 直接当列表迭代会拿到 key 字符串、被 isinstance 过滤成恒 0 条
    u = _identity()["user_id"]
    if since is None and until is None:
        # 无时间过滤：SDK 原生 offset 分页，多取 1 条判 has_more，精确且省
        r = client.list_memories(limit=limit + 1, offset=offset, order=order, user_id=u) or {}
        rows = [m for m in ((r.get("vdb") or {}).get("memories") or []) if isinstance(m, dict)]
        has_more = len(rows) > limit
        rows = rows[:limit]
    else:
        # SDK 无时间过滤参数：取窗口内全部（个人库量级，上限 500，可调
        # HY_MEM_LIST_CAP），按时间过滤后本地切片——翻页按「过滤后的列表」算
        cap = int(os.getenv("HY_MEM_LIST_CAP") or 500)
        r = client.list_memories(limit=cap, offset=0, order=order, user_id=u) or {}
        rows = [m for m in ((r.get("vdb") or {}).get("memories") or []) if isinstance(m, dict)]
        rows = [m for m in rows if _in_range(_item_ts(m), since, until)]
        has_more = len(rows) > offset + limit
        rows = rows[offset : offset + limit]
    out = [
        {
            "memory_id": m.get("memory_id"),
            "content": (m.get("content") or "")[:200],
            "when": m.get("when") or m.get("created_at"),
        }
        for m in rows
    ]
    resp = {"count": len(out), "memories": out, "offset": offset, "order": order}
    if has_more:
        resp["has_more"] = True
        resp["next_offset"] = offset + limit
    return json.dumps(resp, ensure_ascii=False)


def _tool_get_memory(args: dict) -> str:
    mid = str(args.get("memory_id") or "").strip()
    if not mid:
        raise ParamError("memory_id 不能为空：请传入 list 或 search 返回的 memory_id")
    client = _client()
    r = client.get(mid)
    if r is None:
        return json.dumps({"found": False, "memory_id": mid}, ensure_ascii=False)
    return json.dumps({"found": True, "memory": r}, ensure_ascii=False, default=str)


def _tool_delete_memory(args: dict) -> str:
    mid = str(args.get("memory_id") or "").strip()
    if not mid:
        raise ParamError("memory_id 不能为空：请传入 list 或 search 返回的 memory_id")
    if args.get("confirm") is not True:
        raise ParamError("删除是破坏性操作：必须显式传 confirm=true")
    from hy_mem_cli import store

    client = _client()
    with store.WriteLock():
        r = client.delete(mid)
    return json.dumps({"deleted": mid, "result": r}, ensure_ascii=False, default=str)


def _tool_update_memory(args: dict) -> str:
    """更新一条记忆的内容。走 SDK 演化链：旧版本进 SHADOW 保留，可追溯。"""
    mid = str(args.get("memory_id") or "").strip()
    content = str(args.get("content") or "").strip()
    if not mid:
        raise ParamError("memory_id 不能为空：请传入 list 或 search 返回的 memory_id")
    if not content:
        raise ParamError("content 不能为空：请提供修改后的记忆正文")
    if args.get("confirm") is not True:
        raise ParamError("更新会改变检索结果：必须显式传 confirm=true")
    from hy_mem_cli import store

    client = _client()
    if client.get(mid) is None:
        return json.dumps({"found": False, "memory_id": mid}, ensure_ascii=False)
    with store.WriteLock():
        r = client.update(mid, content)
    return json.dumps(
        {"updated": mid, "note": "旧版本保留在演化链（get_recent_history/history 可查）", "result": r},
        ensure_ascii=False,
        default=str,
    )


def _tool_get_version(_args: dict) -> str:
    """版本与安装信息。自动升级链路的第一步：比对版本、定位要替换的安装根。"""
    import os
    from pathlib import Path

    import helptext
    import install_ops

    root = install_ops._current_root()
    cfg_env = os.getenv("HY_MEM_CONFIG", "").strip()
    cfg_path = Path(cfg_env) if cfg_env else root / "config.json"
    configured = cfg_path.is_file()
    data_dir = ""
    if configured:
        try:
            import config as cfgmod

            cfg = cfgmod.load()
            data_dir = str((cfg.get("storage") or {}).get("dataDir") or root / "data")
        except Exception:  # noqa: BLE001
            configured = False
    return json.dumps(
        {
            "version": helptext.VERSION,
            "mode": "exe" if getattr(sys, "frozen", False) else "source",
            "installRoot": str(root),
            "configPath": str(cfg_path),
            "configured": configured,
            "dataDir": data_dir or str(root / "data"),
        },
        ensure_ascii=False,
    )


TOOLS = [
    {
        "name": "add_memory",
        "description": (
            "写入一条长期记忆。用户说「记一下」、表达偏好/决策/项目事实时调用。"
            "默认异步：立即返回，后台落库（约 6~9 秒后可检索）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "记忆正文，一条一个主题"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签"},
                "async": {"type": "boolean", "description": "默认 true；false 时同步等待落库（8~40s）"},
                "metadata": {"type": "object", "description": "附加元数据对象，如 {\"project\": \"iisw\"}"},
                "memory_at": {
                    "type": "string",
                    "description": "记忆发生时间（补录历史用）：2026-10-27 或 2026-10-27T14:30:00",
                },
                "request_id": {"type": "string", "description": "幂等写 id：同一 id 重复提交只落一条"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "queue_status",
        "description": (
            "查异步写入进度：add_memory 返回的 job_ids 现在到哪一步了（待处理/运行中/完成/死信）、"
            "worker 是否还活着、队列是否清空。add_memory 之后想确认「落库了没」就调它，"
            "别用 search 空结果反推。wait_seconds>0 时短阻塞等待（上限 60 秒）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "要跟踪的 job_id（add_memory 返回的）；省略则看最近 10 条队列",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "最长等待秒数（0~60，默认 0 立即返回）：等这些 job 变成完成/死信",
                },
                "limit": {"type": "integer", "description": "未指定 job_ids 时返回最近几条，默认 10，上限 50"},
            },
        },
    },
    {
        "name": "search_memory",
        "description": (
            "语义检索长期记忆。回答涉及用户偏好/历史决策/项目上下文且当前上下文没有时，先查再答。"
            "支持时间范围（since/until）与翻页（offset/has_more）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词（语义匹配，不是关键词精确匹配）"},
                "limit": {"type": "integer", "description": "返回条数，默认 5，上限 20"},
                "min_score": {"type": "number", "description": "相似度阈值，默认用配置值（0.35）"},
                "since": {
                    "type": "string",
                    "description": "只要这段时间之后：相对值（30m/12h/7d/30d/1y）或 ISO 日期（2026-01-01）",
                },
                "until": {"type": "string", "description": "只要这段时间之前，格式同 since"},
                "offset": {"type": "integer", "description": "翻页偏移；上页返回 has_more 时用 next_offset 继续"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_memories",
        "description": (
            "按时间顺序列出记忆（默认最新在前）。支持时间范围（since/until）、"
            "排序（order）与翻页（offset/has_more）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "默认 10，上限 50"},
                "offset": {"type": "integer", "description": "翻页偏移；上页返回 has_more 时用 next_offset 继续"},
                "order": {"type": "string", "description": "desc（最新在前，默认）/ asc（最旧在前）"},
                "since": {
                    "type": "string",
                    "description": "只要这段时间之后：相对值（30m/12h/7d/30d/1y）或 ISO 日期（2026-01-01）",
                },
                "until": {"type": "string", "description": "只要这段时间之前，格式同 since"},
            },
        },
    },
    {
        "name": "get_memory",
        "description": "按 memory_id 读取单条记忆全文。",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
    {
        "name": "get_version",
        "description": (
            "返回 hy-mem 版本与安装信息（版本号 / exe或源码 / 安装根 / 配置与数据目录）。"
            "自动升级前先调它：比对版本决定要不要升级，installRoot 就是要替换的目录。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_memory",
        "description": "删除一条记忆。破坏性操作，必须 confirm=true。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "confirm": {"type": "boolean", "description": "必须显式传 true"},
            },
            "required": ["memory_id", "confirm"],
        },
    },
    {
        "name": "update_memory",
        "description": (
            "更新一条记忆的内容（走演化链，旧版本保留可追溯）。会改变检索结果，必须 confirm=true。"
            "记忆内容有误或过时、用户纠正了某个事实时用；新增信息请用 add_memory。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "要改的记忆 id（list/search 返回的）"},
                "content": {"type": "string", "description": "修改后的完整正文（整条替换，不是追加）"},
                "confirm": {"type": "boolean", "description": "必须显式传 true"},
            },
            "required": ["memory_id", "content", "confirm"],
        },
    },
]

_HANDLERS = {
    "add_memory": _tool_add_memory,
    "queue_status": _tool_queue_status,
    "search_memory": _tool_search_memory,
    "list_memories": _tool_list_memories,
    "get_memory": _tool_get_memory,
    "delete_memory": _tool_delete_memory,
    "update_memory": _tool_update_memory,
    "get_version": _tool_get_version,
}


# --------------------------------------------------------------------------
# JSON-RPC 派发
# --------------------------------------------------------------------------
def _resp(id_, result) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _tool_result(text: str, is_error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


def handle(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 消息。通知（无 id）返回 None。"""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _err(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "不是合法的 JSON-RPC 2.0 消息")
    method, id_ = msg["method"], msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        return None if is_notification else _resp(id_, {
            "protocolVersion": params.get("protocolVersion") or "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return None if is_notification else _resp(id_, {})
    if method == "tools/list":
        return None if is_notification else _resp(id_, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        handler = _HANDLERS.get(name)
        if handler is None:
            return _err(id_, INVALID_PARAMS, f"未知工具：{name}")
        try:
            return _resp(id_, _tool_result(handler(params.get("arguments") or {})))
        except ParamError as e:  # 参数错直说哪里错，不套运行时建议
            return _resp(id_, _tool_result(f"参数错误：{e}", is_error=True))
        except Exception as e:  # noqa: BLE001  工具失败以 isError 报给宿主，不断连接
            print(f"hy-mem mcp: {name} 失败: {type(e).__name__}: {e}", file=sys.stderr)
            # 错误码 + 异常原文 + 修复建议，宿主拿到就能转述给用户
            return _resp(id_, _tool_result(errors.format_tool_error(e), is_error=True))
    if method == "prompts/list":
        return None if is_notification else _resp(id_, {"prompts": []})
    if method == "resources/list":
        return None if is_notification else _resp(id_, {"resources": []})
    if is_notification:
        return None
    return _err(id_, METHOD_NOT_FOUND, f"未知方法：{method}")


def serve() -> int:
    """主循环：逐行读 stdin → 派发 → 写 stdout（一行一个响应）。"""
    # MCP stdio 规范：字节流是 UTF-8。不显式声明的话 exe 会按控制台代码页
    # （cp936）解 stdin——宿主发来的 UTF-8 中文部分字节恰好是合法 GBK 序列、
    # 部分不是，产生孤立代理字符后在 sqlite 落库/编码时炸（时好时坏，纯看
    # 字节运气）。stdout 同时固定 LF，去掉 Windows 文本模式的 \r。
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:  # noqa: BLE001  非 tty/被重定向的极端场景保底
        pass
    print(f"hy-mem mcp server {SERVER_VERSION} ready (stdio)", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        t0 = time.time()
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            resp = _err(None, PARSE_ERROR, f"JSON 解析失败：{e}")
        else:
            try:
                resp = handle(msg)
            except Exception as e:  # noqa: BLE001  派发层兜底，绝不让进程死
                print(f"hy-mem mcp: 派发异常 {type(e).__name__}: {e}", file=sys.stderr)
                resp = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, f"内部错误：{errors.error_detail(e)}")
        if resp is not None:
            # ensure_ascii=True：协议输出纯 ASCII（中文转 \uXXXX），不受宿主
            # 控制台代码页影响——exe 的 stdout 编码随环境漂移，False 会产生
            # 孤代理字符打断 JSON-RPC 行。解码后内容不变，只是线上更冗长。
            sys.stdout.write(json.dumps(resp, ensure_ascii=True, default=str) + "\n")
            sys.stdout.flush()
        else:
            print(f"hy-mem mcp: {msg.get('method')} 处理 {int((time.time()-t0)*1000)}ms (notification)", file=sys.stderr)
    return 0
