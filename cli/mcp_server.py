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
import sys
import time

from helptext import VERSION

SERVER_NAME = "hy-mem"
SERVER_VERSION = VERSION  # 单一来源：CLI --version / help / MCP initialize 三处共用

# JSON-RPC 错误码（常用三个，够用）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


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
    from hy_mem_cli import _spawn_detached_worker

    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    tags = [t for t in (args.get("tags") or []) if str(t).strip()]
    use_async = args.get("async", True) is not False
    client = _client()
    try:
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
            with store.WriteLock():
                last = client.add(text, source="mcp", **_identity())
            ids = _ids_of(last)
            if tags:
                _bind_tags(_new_ids_only(ids, before), tags, "user")
            return json.dumps(
                {"mode": "sync", "memory_ids": ids, "tags": tags}, ensure_ascii=False
            )
        # 异步：入队 + 无人消费时自动拉起临时 worker（与 CLI add --async 同一套）
        from hy_mem_cli import store

        conn = store.connect()
        try:
            job_ids = [
                store.enqueue(conn, {"text": text, "source": "mcp", "identity": _identity(), "tags": tags})
            ]
            stats = store.queue_stats(conn)
            hb = store.worker_age(conn)
            live = hb is not None and hb < 15.0
            spawned = False
            if stats["pending"] > 0 and not live:
                spawned = _spawn_detached_worker()
        finally:
            conn.close()
        return json.dumps(
            {
                "mode": "async",
                "job_ids": job_ids,
                "pending": stats["pending"],
                "worker_spawned": spawned,
                "note": "已入队，后台 worker 约 6~9 秒落库，无需等待",
            },
            ensure_ascii=False,
        )
    finally:
        client.close()


def _tool_search_memory(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    limit = max(1, min(int(args.get("limit") or 5), 20))
    min_score = args.get("min_score")
    from hy_mem_cli import _flatten_search

    client = _client()
    try:
        # search 的身份参数是复数形参（user_ids），与 CLI identity_lists 同口径；
        # 单数 user_id 会被 SDK 拒收（TypeError: unexpected keyword argument）
        u = _identity()["user_id"]
        r = client.search(query, limit=limit, min_score=min_score, user_ids=[u])
        items = _flatten_search(r)[:limit]
        out = [
            {
                "memory_id": it.get("memory_id"),
                "content": it.get("content") or it.get("text"),
                "score": it.get("score"),
                "when": it.get("when") or it.get("created_at"),
            }
            for it in items
        ]
        return json.dumps({"query": query, "hits": out}, ensure_ascii=False)
    finally:
        client.close()


def _tool_list_memories(args: dict) -> str:
    limit = max(1, min(int(args.get("limit") or 10), 50))
    client = _client()
    try:
        # SDK 返回 dict（{"vdb": {"memories": [...]}}），与 CLI cmd_list 同解析；
        # 直接当列表迭代会拿到 key 字符串、被 isinstance 过滤成恒 0 条
        u = _identity()["user_id"]
        r = client.list_memories(limit=limit, user_id=u) or {}
        rows = (r.get("vdb") or {}).get("memories") or []
        out = [
            {
                "memory_id": m.get("memory_id"),
                "content": (m.get("content") or "")[:200],
                "when": m.get("when") or m.get("created_at"),
            }
            for m in rows
            if isinstance(m, dict)
        ]
        return json.dumps({"count": len(out), "memories": out}, ensure_ascii=False)
    finally:
        client.close()


def _tool_get_memory(args: dict) -> str:
    mid = str(args.get("memory_id") or "").strip()
    if not mid:
        raise ValueError("memory_id 不能为空")
    client = _client()
    try:
        r = client.get(mid)
        if r is None:
            return json.dumps({"found": False, "memory_id": mid}, ensure_ascii=False)
        return json.dumps({"found": True, "memory": r}, ensure_ascii=False, default=str)
    finally:
        client.close()


def _tool_delete_memory(args: dict) -> str:
    mid = str(args.get("memory_id") or "").strip()
    if not mid:
        raise ValueError("memory_id 不能为空")
    if args.get("confirm") is not True:
        raise ValueError("删除是破坏性操作：必须显式传 confirm=true")
    from hy_mem_cli import store

    client = _client()
    try:
        with store.WriteLock():
            r = client.delete(mid)
        return json.dumps({"deleted": mid, "result": r}, ensure_ascii=False, default=str)
    finally:
        client.close()


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
            },
            "required": ["text"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "语义检索长期记忆。回答涉及用户偏好/历史决策/项目上下文且当前上下文没有时，先查再答。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词（语义匹配，不是关键词精确匹配）"},
                "limit": {"type": "integer", "description": "返回条数，默认 5，上限 20"},
                "min_score": {"type": "number", "description": "相似度阈值，默认用配置值（0.35）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_memories",
        "description": "按时间倒序列出最近的记忆。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "默认 10，上限 50"}},
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
]

_HANDLERS = {
    "add_memory": _tool_add_memory,
    "search_memory": _tool_search_memory,
    "list_memories": _tool_list_memories,
    "get_memory": _tool_get_memory,
    "delete_memory": _tool_delete_memory,
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
        except Exception as e:  # noqa: BLE001  工具失败以 isError 报给宿主，不断连接
            print(f"hy-mem mcp: {name} 失败: {type(e).__name__}: {e}", file=sys.stderr)
            return _resp(id_, _tool_result(f"{type(e).__name__}: {e}", is_error=True))
    if method == "prompts/list":
        return None if is_notification else _resp(id_, {"prompts": []})
    if method == "resources/list":
        return None if is_notification else _resp(id_, {"resources": []})
    if is_notification:
        return None
    return _err(id_, METHOD_NOT_FOUND, f"未知方法：{method}")


def serve() -> int:
    """主循环：逐行读 stdin → 派发 → 写 stdout（一行一个响应）。"""
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
                resp = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, "内部错误")
        if resp is not None:
            # ensure_ascii=True：协议输出纯 ASCII（中文转 \uXXXX），不受宿主
            # 控制台代码页影响——exe 的 stdout 编码随环境漂移，False 会产生
            # 孤代理字符打断 JSON-RPC 行。解码后内容不变，只是线上更冗长。
            sys.stdout.write(json.dumps(resp, ensure_ascii=True, default=str) + "\n")
            sys.stdout.flush()
        else:
            print(f"hy-mem mcp: {msg.get('method')} 处理 {int((time.time()-t0)*1000)}ms (notification)", file=sys.stderr)
    return 0
