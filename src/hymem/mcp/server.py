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
import sys
import time

from hymem.core import errors  # CLI/MCP 共用的错误分类与格式化
from hymem.core.timefilter import ParamError, _in_range, _item_ts, _parse_time  # noqa: F401
from hymem.core.version import VERSION

SERVER_NAME = "hy-mem"
# serve() 用的热切换检测：上次发通知时的 mode。None = 尚未记录（连接后第一条
# 消息只记录不发，避免对刚连上的宿主多推一次无意义通知）。
_LAST_MODE: str | None = None
SERVER_VERSION = VERSION  # 单一来源：CLI --version / help / MCP initialize 三处共用

# JSON-RPC 错误码（常用三个，够用）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


# --------------------------------------------------------------------------
# 工具实现：进程内直调 SDK，不走子进程（省 ~1s 冷启动/次）
# --------------------------------------------------------------------------
_PRODUCTIVITY_SCENES = ("working", "coding")


def _identity() -> dict:
    """MCP 会话统一用默认身份（与 CLI 不传 -u 时同口径）。"""
    try:
        from hymem.core import config as cfgmod

        u = cfgmod.identity_defaults() or "default"
    except Exception:  # noqa: BLE001
        u = "default"
    return {"user_id": u}


_CLIENT = None


def _endpoint_stale() -> bool:
    """检测注入端点是否失效（U3，docs/specs/2026-11-02-endpoint-refresh/）。

    serve 空闲自退后重拉必换端口（bind :0 随机分配），而 SDK client 构造期
    一次性冻结 MEMORY_VECTOR_HOST/PORT——长驻进程不重建就永远连旧死端口
    （ConnectError 10061，实测 add sync 挂而 search 侥幸的分裂现场）。
    判定（全部只读、回环拒连即时返回，正常热路径 ~1ms）：
      1. 只对「自己注入的 env」生效（SRC 标记）；外部显式配置不干预，
         嵌入式（无标记）恒 False；
      2. env 端口心跳死了 → 失效（重建后 make_client→ensure_endpoint 会
         重发现/重拉 serve 并重注入 env）；
      3. 端口文件登记了另一个活着且可达的端口 → 失效（换端口场景）。
    """
    from hymem.core import vdb_endpoint

    if os.getenv(vdb_endpoint.SRC_ENV) != vdb_endpoint.SRC_VALUE:
        return False
    try:
        port_env = int(os.getenv("MEMORY_VECTOR_PORT") or 0)
    except ValueError:
        return False
    if port_env > 0 and not vdb_endpoint.heartbeat_ok(port_env, timeout=0.5):
        return True
    info = vdb_endpoint.read_port_info()
    if (info and info["port"] != port_env
            and vdb_endpoint.pid_alive(info["pid"])
            and vdb_endpoint.heartbeat_ok(info["port"], timeout=0.5)):
        return True
    return False


def _client():
    """进程内复用同一个 client。

    ultra 模式下 graph_store 会独占 kuzu 库文件（第二个实例报
    `IO exception: Could not set lock on file ... kuzu_db`）。早期每次 tool call
    都新建 client 且不关闭：pro 下无害（没有 graph_store），ultra 下第二次调用
    就必然失败。MCP 是长驻进程，本来就该持有一个。

    代价：ultra 下本进程会一直占着图库，CLI 的 evolve 会撞锁——所以 evolve
    必须同时暴露成本进程里的工具（C12），而不是只留 CLI。

    U3 自愈（docs/specs/2026-11-02-endpoint-refresh/）：单例冻结的向量端点
    会因 serve 重启换端口而死（serve 空闲自退是设计行为）。每次取单例前
    廉价复验，失效则放掉重建——make_backend 内部重跑 ensure_endpoint，
    SDK 构造期读 env，重建即拿到新端口。
    """
    global _CLIENT
    if _CLIENT is not None and _endpoint_stale():
        _close_client()
    if _CLIENT is None:
        # S2.5（docs/specs/2026-11-02-cli-split-ports/）：MCP 走协议层工厂，
        # 不再直连 vendor。HyMemBackend 对未显式列出的属性（含 SDK 私有成员）
        # 原样透传，调用点零改动；换 backend 时这里就是唯一改动点。
        from hymem.ports.registry import make_backend

        _CLIENT = make_backend(None)
    return _CLIENT


def _close_client() -> None:
    """放掉单例（仅供测试/排障；正常静默退出时进程结束自然释放）。"""
    global _CLIENT
    if _CLIENT is None:
        return
    try:
        _CLIENT.close()
    except Exception:  # noqa: BLE001
        pass
    _CLIENT = None


def _scene_status() -> tuple[bool, str]:
    """scene 在当前配置下是否真实可用（与 SDK 生效条件同口径）。

    配置读坏时不误伤：返回可用，让 SDK 的真实行为决定结果。
    """
    try:
        from hymem.core import config as cfgmod

        return cfgmod.scene_status(_memory_mode())
    except Exception:  # noqa: BLE001
        return True, ""


def _memory_mode() -> str:
    """当前记忆模式（lite/pro/ultra）。只读配置，不构造 client——要快。"""
    try:
        from hymem.core import config as cfgmod

        return str(cfgmod.load().get("mode") or "pro")
    except Exception:  # noqa: BLE001
        return "pro"


def _str_list(v, name: str) -> list[str] | None:
    """筛选参数归一：单字符串 -> [s]；空列表 -> None（等同不筛）。"""
    if v is None:
        return None
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        raise ParamError(f"{name} 必须是字符串或字符串数组")
    out = [str(x).strip() for x in v if str(x).strip()]
    return out or None


def _parse_scene_scope(args: dict) -> tuple[str | None, str | None, str | None]:
    """解析 scene 三件套并校验（与 CLI --scene/--workspace/--branch 同口径）。

    省略 scene = chat（现有行为零变化）；显式 working/coding 才走生产力管线。
    """
    raw = args.get("scene")
    scene = None
    if raw is not None and str(raw).strip():
        scene = str(raw).strip().lower()
        if scene not in ("chat",) + _PRODUCTIVITY_SCENES:
            raise ParamError("scene 只能是 chat / working / coding（省略即 chat）")
    ws = str(args.get("workspace_id") or "").strip() or None
    branch = str(args.get("branch") or "").strip() or None
    if branch and not ws:
        raise ParamError("branch 必须和 workspace_id 一起传（SDK 作用域契约）")
    return scene, ws, branch


def _scene_receipt(r: dict, requested: str) -> dict:
    """U6：从 SDK 回执取真实落库场景，与请求值对比（可选字段，不改旧字段）。

    SDK coding 回执形如 {"scene": "productivity", "variant": "working|coding"}，
    chat 回执是 {"scene": "chat"}。因此以 variant 优先，其次认 chat/working/coding；
    都没有（含 productivity 无 variant）时退回请求值，避免凭空猜测。
    """
    sdk_variant = str((r or {}).get("variant") or "").strip().lower()
    sdk_scene = str((r or {}).get("scene") or "").strip().lower()
    if sdk_variant in ("working", "coding"):
        stored = sdk_variant
    elif sdk_scene in ("chat", "working", "coding"):
        stored = sdk_scene
    else:
        stored = requested
    out = {
        "requested_scene": requested,
        "stored_scene": stored,
        "scene_degraded": stored != requested,
    }
    if stored != requested:
        out["warning"] = (
            f"请求 scene={requested}，SDK 实际落库 {stored}（已降级）。"
            f"以 stored_scene 为准：检索要用 scene={stored} 才找得到。"
        )
    return out


def _add_productivity(args: dict, scene: str, ws: str | None, branch: str | None) -> str:
    """生产力场景写入：SDK client.add(messages, scene=...) 整段，同步落库。

    与 chat 完全不同的管线（独立 SQLite + 独立向量集合），复用不了两段式/合批；
    也不进异步队列——队列 payload 是按 text 单条设计的，worker 无 scene 分流。
    """
    import os as _os

    from hymem.core import store

    # U4：SDK 在 lite / coding 关闭时会把这行 scene 写入静默降级到 chat。
    # 门口拦死（零写入），绝不返回 ok=true + mode=working 的假成功。
    _ok_scene, _why = _scene_status()
    if not _ok_scene:
        return json.dumps(
            {
                "mode": scene,
                "ok": False,
                "error": "scene_unavailable",
                "applied_scene": None,
                "guidance": (
                    f"{_why}；本条未写入（零写入）。要写记忆请省略 scene 走 chat（参数 text），"
                    "或按提示修正配置后重试。"
                ),
            },
            ensure_ascii=False,
        )

    raw = args.get("messages")
    if raw is None:
        raise ParamError(
            f"scene={scene} 需要 messages（OpenAI messages 数组，含 assistant.tool_calls "
            "或 role=tool 的工具轨迹）；纯文本记忆请省略 scene 走 chat"
        )
    if isinstance(raw, str):
        raise ParamError("messages 必须是数组，不是字符串：请传 OpenAI messages 对象数组")
    if not isinstance(raw, list) or not raw:
        raise ParamError("messages 必须是非空数组")
    if any(not isinstance(m, dict) for m in raw):
        raise ParamError("messages 每个元素都必须是对象（role/content 等字段）")
    if str(args.get("text") or "").strip():
        raise ParamError(
            "text 与 messages 二选一：scene 写入的内容就是这份工具轨迹，别再叠一段 text"
        )
    # SDK 对「显式 scene 但无工具轨迹」静默回退 chat 管线——那是陷阱
    # （调用方以为写了 coding 库，实际进了 chat）。门口拦住，零写入。
    if not any((m.get("tool_calls") or m.get("role") == "tool") for m in raw):
        raise ParamError(
            "messages 里没有 tool_calls / role=tool：SDK 会静默把这次写入回退到 chat 管线，"
            "已拦截（零写入）。生产力记忆的价值在工具轨迹——确认轨迹含 assistant.tool_calls "
            "或 role=tool 的消息，或省略 scene 改贴纯文本"
        )
    request_id = str(args.get("request_id") or "").strip() or None
    lock_timeout = float(_os.getenv("HY_MEM_ADD_LOCK_TIMEOUT") or 30)
    client = _client()
    add_kw = {"request_id": request_id} if request_id else {}
    try:
        # 首版保守持锁（独立管线与 chat WriteLock 理论无冲突，实测无竞争再出锁）
        with store.WriteLock(timeout=lock_timeout):
            r = client.add(
                raw,
                scene=scene,
                workspace_id=ws,
                branch=branch,
                source="mcp",
                **add_kw,
                **_identity(),
            )
    except TimeoutError:
        return json.dumps(
            {
                "mode": scene,
                "ok": False,
                "error": "lock_timeout",
                "guidance": (
                    f"另一进程正在写入，{lock_timeout:g}s 内未拿到写锁，本条未入库（零写入），可直接重试。"
                ),
            },
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {
                "mode": scene,
                "ok": False,
                "error": "scene_write_failed",
                "message": f"{type(e).__name__}: {e}",
                "guidance": (
                    f"SDK {scene} 管线报错（未入库）。常见原因：轨迹格式不合规、"
                    "LLM 抽取失败、向量端点不可用；排查用 hy-mem doctor。"
                ),
            },
            ensure_ascii=False,
        )
    if r.get("success") is not True:
        return json.dumps(
            {
                "mode": scene,
                "ok": False,
                "error": "scene_write_failed",
                "sdk_result": r,
                "guidance": "SDK 返回 success!=true，本条大概率未落库；换措辞或检查 LLM 配置后重试。",
            },
            ensure_ascii=False,
            default=str,
        )
    _receipt = _scene_receipt(r, scene)
    _note = (
        "生产力管线是同步写入（与 chat 的异步队列不同）：返回时已落库，"
        f"可直接用 search_memory(query, scene={_receipt['stored_scene']}) 验证。"
    )
    if _receipt["scene_degraded"]:
        _note += f"（mode 是请求值，真实落库为 {_receipt['stored_scene']}，以 stored_scene 为准）"
    return json.dumps(
        {
            "mode": scene,
            "ok": True,
            "request_id": r.get("request_id") or request_id,
            "fact_ids": r.get("fact_ids") or [],
            "experience_ids": r.get("experience_ids") or [],
            "chain_rounds": r.get("chain_rounds"),
            "workspace_id": ws,
            "branch": branch,
            **_receipt,
            "note": _note,
        },
        ensure_ascii=False,
        default=str,
    )


def _tool_add_memory(args: dict) -> str:
    """写入记忆。默认走异步队列（毫秒返回，worker 自动拉起落库）。"""
    from hymem.core.procs import _spawn_detached_worker
    from hymem.core.searchops import _parse_memory_at

    scene, ws, branch = _parse_scene_scope(args)
    if scene in _PRODUCTIVITY_SCENES:
        return _add_productivity(args, scene, ws, branch)
    if args.get("messages"):
        raise ParamError(
            "messages 只在 scene=working/coding 时有效：chat 记忆请用 text；"
            "要写工具轨迹请同时传 scene"
        )
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
        from hymem.core.searchops import _bind_tags_to_new, _ids_of, _snapshot_ids
        from hymem.core import store

        before, before_complete = (
            _snapshot_ids(client, _identity()) if tags else (set(), True)
        )
        add_kw = (
            {"metadata": metadata, "memory_at": memory_at, "request_id": request_id}
        )
        add_kw = {k: v for k, v in add_kw.items() if v is not None}
        # R4 两段式（2026-10-29）：抽取（LLM 网络调用）在锁外，可多进程并行；
        # WriteLock 只包 SDK 标准提交段（L1 存档 → L2 reconcile → SHADOW）。
        # R3 fail-fast：抢不到锁立即返回结构化失败（默认 5s）；
        # 多条/并发的正路是 async=true（默认）。
        import os as _os

        lock_timeout = float(_os.getenv("HY_MEM_ADD_LOCK_TIMEOUT") or 5)
        from hymem.core.two_phase import (
            CommitFailed,
            ExtractFailed,
            commit_extracted_with_retry,
            extract_phase,
            prepare_commit,
        )

        # R4 锁探测前置：锁被占时不白烧 LLM 抽取，立即 lock_timeout。
        try:
            with store.WriteLock(timeout=lock_timeout):
                pass
        except TimeoutError:
            return json.dumps(
                {
                    "mode": "sync",
                    "ok": False,
                    "error": "lock_timeout",
                    "lock_timeout": lock_timeout,
                    "guidance": (
                        f"另一进程正在写入，{lock_timeout:g}s 内未拿到写锁，本条未入库（零写入）。"
                        "单条可稍后重试；多条/并发请保持 async=true（默认，入队即返回，3 车道消费）。"
                        "超时可用环境变量 HY_MEM_ADD_LOCK_TIMEOUT（秒）调整。"
                    ),
                },
                ensure_ascii=False,
            )
        try:
            agent_result = extract_phase(client, text, source="mcp", **add_kw, **_identity())
        except ExtractFailed as e:
            return json.dumps(
                {
                    "mode": "sync",
                    "ok": False,
                    "error": "extract_failed",
                    "sdk_code": str(e.sdk_code or "UNKNOWN"),
                    "guidance": (
                        f"LLM 抽取失败，本条未入库（零写入）：{e.message}。"
                        "多为 LLM/网络瞬时故障，单条可直接重试；反复失败请检查 LLM 配置。"
                    ),
                },
                ensure_ascii=False,
            )
        # R4.7 预计算出锁：L1 向量 + 本批 dedup 在抢锁前算好
        prepared = prepare_commit(client, text, agent_result, source="mcp", **add_kw, **_identity())
        try:
            with store.WriteLock(timeout=lock_timeout):
                last = commit_extracted_with_retry(
                    client, text, agent_result, prepared=prepared,
                    source="mcp", **add_kw, **_identity(),
                )
        except TimeoutError:
            return json.dumps(
                {
                    "mode": "sync",
                    "ok": False,
                    "error": "lock_timeout",
                    "lock_timeout": lock_timeout,
                    "guidance": (
                        f"另一进程正在写入，{lock_timeout:g}s 内未拿到写锁，本条未入库（零写入）。"
                        "单条可稍后重试；多条/并发请保持 async=true（默认，入队即返回，3 车道消费）。"
                        "超时可用环境变量 HY_MEM_ADD_LOCK_TIMEOUT（秒）调整。"
                    ),
                },
                ensure_ascii=False,
            )
        except CommitFailed as e:
            return json.dumps(
                {
                    "mode": "sync",
                    "ok": False,
                    "error": "commit_failed",
                    "message": str(e),
                    "guidance": (
                        "落库提交失败：原始对话已存档（L1），可检索的结论可能缺失；"
                        "重试本条即可，reconcile 会吸收重复；反复失败跑 hy-mem doctor。"
                    ),
                },
                ensure_ascii=False,
            )
        ids = _ids_of(last)
        tagged: list[str] = []
        tag_note = ""
        if tags:
            tagged, tag_note = _bind_tags_to_new(
                client, _identity(), before, ids, tags, before_complete=before_complete,
            )
        out: dict = {"mode": "sync", "memory_ids": ids, "tags": tags}
        if tag_note:
            out["note"] = tag_note
        return json.dumps(out, ensure_ascii=False)
    # 异步：入队 + 无人消费时自动拉起临时 worker（与 CLI add --async 同一套）
    from hymem.core import store

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
            # 耗时承诺按实测口径（2026-10-29：pro + 推理型 LLM 单条 took_s=124.1），
            # 「约 6~9 秒」是 lite/快模型的旧数字，按它去等必扑空
            "note": (
                "已入队，后台落库：lite 秒级，pro 模式单条实测可达 2 分钟左右。"
                "要确认落库（而不是猜）就用 queue_status(job_ids=[...], wait_seconds=60)"
            ),
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
    from hymem.core import store

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
    # shadow_only = done 但只落了 l1_raw 影子、检索不到（2026-10-29 实测：job 87
    # 标 shadow_only，hint 却说「可以 search 验证」——智能体顺着走必撞墙）。
    # 它的优先级压过一切「已落库」话术：done ≠ 可检索。
    shadow_ids = [
        j["job_id"] for j in out_jobs
        if j["status"] == "done" and (j.get("error") or "").startswith("shadow_only")
    ]
    if dead:
        hint = f"有死信任务 {dead}：看 error 字段；重投用 hy-mem retry <job_id>（或 retry dead 全部）"
    elif shadow_ids:
        hint = (
            f"job {shadow_ids} 已落库但检索不到（只落 l1_raw 影子，done ≠ 可检索）："
            "内容确有可抽取的事实时，用 `hy-mem retry --shadow` 重做抽取"
        )
    elif not drained and not live:
        hint = "队列有积压但没有 worker 在跑：先跑 hy-mem worker --once（pro 模式单条实测可达 2 分钟左右）"
    elif not drained:
        hint = "仍在落库：pro 模式单条实测可达 2 分钟左右；可再调 queue_status(wait_seconds=60) 等它"
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


def _search_scene(args: dict, scene: str, ws: str | None, branch: str | None,
                  inc: list[str] | None, exc: list[str] | None) -> str:
    """生产力场景检索：facts / experiences 分路返回（与 chat 单路 hit 列表不同）。"""
    # U4：lite / coding 关闭时 SDK 会把 scene 检索降级成 chat 单路，
    # 再按 facts/experiences 取值只会得到 0——明确失败比"看起来成功"诚实。
    _ok_scene, _why = _scene_status()
    if not _ok_scene:
        return json.dumps(
            {
                "scene": scene,
                "ok": False,
                "error": "scene_unavailable",
                "guidance": (
                    f"{_why}；scene 检索未执行（零结果）。改用省略 scene 的 chat 检索，"
                    "或按提示修正配置后重试。"
                ),
            },
            ensure_ascii=False,
        )
    query = str(args.get("query") or "").strip()
    if not query:
        raise ParamError("query 不能为空：检索必须提供查询词")
    try:
        limit = max(1, min(int(args.get("limit") or 5), 20))
    except (TypeError, ValueError):
        raise ParamError("limit 必须是整数（1~20）")
    min_score = args.get("min_score")
    if min_score is not None:
        try:
            min_score = float(min_score)
        except (TypeError, ValueError):
            raise ParamError("min_score 必须是数字（0~1）")
    # S4：diff 块按次开。MCP 是长驻进程——env 必须用后即焚（finally 还原），
    # 否则一次 chain_block=true 会污染后续所有检索（多烧 token 且结果变样）。
    chain_block = args.get("chain_block") is True
    if chain_block and scene != "coding":
        raise ParamError(
            f"chain_block 只对 scene=coding 生效（{scene} 没有版本演进链）：去掉它或改 scene=coding"
        )
    _prev_cb = os.environ.get("MEMORY_CODING_CHAIN_BLOCK_ENABLED")
    if chain_block:
        os.environ["MEMORY_CODING_CHAIN_BLOCK_ENABLED"] = "true"
    try:
        # client 必须在开关设好之后构造：CodingConfig 在 client 初始化时读 env，
        # 先造 client 再设 env 等于开关没生效。
        from hymem.core.searchops import scenecore

        client = _client()
        uid = _identity()["user_id"]
        r = scenecore(
            client,
            query,
            scene=scene,
            workspace_id=ws,
            branch=branch,
            limit=limit,
            user_ids=[uid],
            min_score=min_score,
            include_sources=inc,
            exclude_sources=exc,
            chain_block=chain_block,
            chain_user_id=uid,
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps(
            {
                "scene": scene,
                "ok": False,
                "error": "scene_search_failed",
                "message": f"{type(e).__name__}: {e}",
                "guidance": (
                    f"{scene} 库检索报错。scope 不匹配（workspace/branch 传错）会查不到，"
                    "workspace/branch 只在写入时用过才传。"
                ),
            },
            ensure_ascii=False,
        )
    finally:
        if chain_block:
            if _prev_cb is None:
                os.environ.pop("MEMORY_CODING_CHAIN_BLOCK_ENABLED", None)
            else:
                os.environ["MEMORY_CODING_CHAIN_BLOCK_ENABLED"] = _prev_cb
    facts = r.get("facts") or []
    experiences = r.get("experiences") or []
    _sdk_scene = str(r.get("scene") or "").strip().lower()
    out = {
        # 旧字段保持原语义（SDK 回执值，可能是笼统的 productivity）
        "scene": r.get("scene") or scene,
        # U6：请求值 / 真实库（working|coding），供智能体区分两个生产力库
        "scene_requested": scene,
        "scene_resolved": (_sdk_scene if _sdk_scene in ("working", "coding") else scene),
        "query": query,
        "count": len(facts) + len(experiences),
        "facts": facts,
        "experiences": experiences,
        "chain_used": r.get("chain_used") or [],
        "chain_block": (r.get("chain_block") or "")[:2000] or None,
        "chain_note": r.get("chain_note"),
        "elapsed_ms": r.get("elapsed_ms"),
    }
    if ws:
        out["workspace_id"] = ws
        out["branch"] = branch
    if not facts and not experiences:
        out["hint"] = (
            f"{scene} 库没命中。两点常见原因：①这个主题根本没写过 {scene} 记忆"
            "（chat 记忆不在这里——去掉 scene 再搜）；②写入时带了 workspace_id/branch，"
            "检索要用同样的 scope 才看得见。"
        )
    return json.dumps(out, ensure_ascii=False, default=str)


def _tool_search_memory(args: dict) -> str:
    scene, ws, branch = _parse_scene_scope(args)
    inc = _str_list(args.get("include_sources"), "include_sources")
    exc = _str_list(args.get("exclude_sources"), "exclude_sources")
    if inc and exc:
        raise ParamError("include_sources 与 exclude_sources 不能同时用（选一个）")
    # S1 新参数只进 chat 路径：scene 双路有自己的融合口径，传了就明说，别静默忽略
    if scene in _PRODUCTIVITY_SCENES and (
        args.get("queries") or (args.get("search_mode") or "default") != "default"
        or args.get("rerank") is False
    ):
        raise ParamError("queries / search_mode / rerank 仅 chat 检索支持（scene 检索有自己的双路口径）")
    if scene in _PRODUCTIVITY_SCENES:
        return _search_scene(args, scene, ws, branch, inc, exc)
    if ws or branch:
        raise ParamError(
            "workspace_id / branch 只对 scene=working/coding 生效：chat 记忆没有仓库作用域"
        )
    if args.get("chain_block"):
        raise ParamError("chain_block 只对 scene=coding 生效：chat 记忆没有 diff 演进链")
    query = str(args.get("query") or "").strip()
    # S1（docs/specs/2026-11-02-sdk-alignment-gaps/）：多路检索 / agentic / rerank。
    # 只进 chat 路径——scene 双路检索有自己的融合口径，不混。
    queries = _str_list(args.get("queries"), "queries")
    if queries and len(queries) > 5:
        raise ParamError("queries 最多 5 路（融合收益在 3~5 路，多了纯烧钱）")
    if queries and query:
        raise ParamError("queries 与 query 二选一，不要都传")
    if not queries and not query:
        raise ParamError("query 不能为空：检索必须提供查询词")
    search_mode = str(args.get("search_mode") or "default").strip()
    if search_mode not in ("default", "agentic"):
        raise ParamError(f"search_mode 只认 default/agentic（收到 {search_mode!r}）")
    rerank = args.get("rerank")
    if rerank is not None and not isinstance(rerank, bool):
        raise ParamError("rerank 必须是布尔值（缺省 true）")
    limit = max(1, min(int(args.get("limit") or 5), 20))
    min_score = args.get("min_score")
    offset = max(0, int(args.get("offset") or 0))
    since = _parse_time(args.get("since"))
    until = _parse_time(args.get("until"))
    from hymem.core.searchops import searchcore

    client = _client()
    # search 的身份参数是复数形参（user_ids），与 CLI identity_lists 同口径；
    # 单数 user_id 会被 SDK 拒收（TypeError: unexpected keyword argument）
    u = _identity()["user_id"]
    # 翻页 = 多取前 offset 条再切片（SDK search 按 score 排序，无原生 offset）；
    # 拉取上限 200 防深翻页拖慢——语义检索翻到 200 名开外本来就没有意义
    fetch = min(offset + limit, 200)

    # L6 行为模式：独立字段，绝不混进 hits——混进去会在命中不足时补位冒充检索
    # 结果，宿主会把一条无关归纳当事实用。仅 ultra 有；pro 下明说「没有这条路」。
    want_schema = args.get("schema") is not False
    schema_min = 0.0
    schema_limit = 0
    if want_schema:
        from hymem.core import config as _cfgmod

        _ev = _cfgmod.load().get("evolve") or {}
        # R4：调用方可临时降阈值排障（不改配置）；缺省走 config.evolve.schemaMinScore
        _sms = args.get("schema_min_score")
        schema_min = float(_sms) if _sms is not None else float(_ev.get("schemaMinScore") or 0.25)
        schema_limit = max(0, min(int(args.get("schema_limit") or _ev.get("schemaLimit") or 3), 10))

    # 召回/拍平/时间过滤/空结果探针/L6/队列积压全部走共享实现（与 CLI 同一份）。
    res = searchcore(
        client,
        query or None,
        queries=queries or None,
        search_mode=search_mode,
        rerank=True if rerank is None else rerank,
        fetch_limit=fetch,
        offset=offset,
        limit=limit,
        min_score=min_score,
        since=since,
        until=until,
        ident={"user_ids": [u]},
        include_sources=inc or None,
        exclude_sources=exc or None,
        schema=want_schema,
        schema_query=query,
        schema_limit=schema_limit,
        schema_min=schema_min,
        schema_user_id=u,
        remember_auto=False,
        probe_empty=True,
        pending_always=True,
    )
    if res["probe_error"]:
        # SDK 吞掉嵌入端异常静默返回空列表（见 cmd_search 同款防护）——
        # 空手时补一次真实探测，端点挂了把真实原因当错误报出去，
        # 别让宿主把「密钥失效」误读成「没有相关记忆」。探测通过（why 空）
        # 则是窗口内真没命中，照常返回空 hits。
        raise RuntimeError(
            f"检索失败：嵌入端点不可用（{res['probe_error']}）—— 这不代表没有相关记忆"
        )
    page = res["items"]
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
    resp["schemas"] = [
        {"content": s.get("content"), "score": s.get("score"), "layer": "l6_schema"}
        for s in res["schemas"]
    ]
    if want_schema:
        from hymem.core.searchops import schema_state_meta

        resp["schema_available"], _note = schema_state_meta(res["schema_state"], schema_min)
        if _note:
            resp["schema_note"] = _note
    else:
        resp["schema_available"] = False
    if res["has_more"]:
        resp["has_more"] = True
        resp["next_offset"] = offset + limit
    # 2026-10-29 实测缝隙：add 异步返回后立刻 search，返回旧结果/空结果且无任何
    # 队列提示——智能体会把「还在落库」误读成「没有相关记忆」或拿旧内容冒充。
    # 这里把队列未完成数带上：有积压时明说「此刻检索不到是正常的」。
    _busy = int(res["pending"] or 0)
    if _busy > 0:
        resp["pending_writes"] = _busy
        resp["pending_note"] = (
            f"另有 {_busy} 条写入还在队列落库中，此刻检索不到它们是正常的；"
            "确认进度用 queue_status"
        )
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
    # SDK 返回 dict（{"vdb": {"memories": [...]}}），与 CLI cmd_list 同解析
    # （vdb_memories，见 core.searchops）；直接当列表迭代会拿到 key 字符串、
    # 被 isinstance 过滤成恒 0 条
    from hymem.core.searchops import vdb_memories

    u = _identity()["user_id"]
    if since is None and until is None:
        # 无时间过滤：SDK 原生 offset 分页，多取 1 条判 has_more，精确且省
        r = client.list_memories(limit=limit + 1, offset=offset, order=order, user_id=u) or {}
        rows = vdb_memories(r)
        has_more = len(rows) > limit
        rows = rows[:limit]
    else:
        # SDK 无时间过滤参数：取窗口内全部（个人库量级，上限 500，可调
        # HY_MEM_LIST_CAP），按时间过滤后本地切片——翻页按「过滤后的列表」算
        cap = int(os.getenv("HY_MEM_LIST_CAP") or 500)
        r = client.list_memories(limit=cap, offset=0, order=order, user_id=u) or {}
        rows = vdb_memories(r)
        rows = [m for m in rows if _in_range(_item_ts(m), since, until)]
        has_more = len(rows) > offset + limit
        rows = rows[offset : offset + limit]
    out = [
        {
            "memory_id": m.get("memory_id"),
            "content": (m.get("content") or "")[:200],
            "when": m.get("when") or m.get("created_at"),
            # 标签分列（2026-10-29）：tags=自动标签（LLM 抽取）、manual_tags=
            # 手工标签（add 时传入，SDK 存 _tags）。只看 tags 会误判手工标签丢失。
            "tags": m.get("tags") or [],
            "manual_tags": m.get("_tags") or [],
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
    # 标签字段澄清（2026-10-29 实测误报）：SDK 返回里 tags 只含**自动标签**
    # （LLM 抽取），add 时传入的手工标签（含 proj:/domain: 三件套）存在
    # `_tags`。只看 tags 会误判「手工标签被 update 冲掉」。这里显式分列，
    # 原字段不动（兼容既有消费方）。
    if isinstance(r, dict):
        auto_tags = r.get("tags") or []
        manual_tags = r.get("_tags") or []
        r["auto_tags"] = auto_tags
        r["manual_tags"] = manual_tags
        r["tags_note"] = (
            "tags 字段只含自动标签；手工标签（add 时传入的）在 manual_tags/_tags。"
            "检索用 list --tag <名> 可精确命中两者。"
        )
    return json.dumps({"found": True, "memory": r}, ensure_ascii=False, default=str)


def _tool_delete_memory(args: dict) -> str:
    mid = str(args.get("memory_id") or "").strip()
    if not mid:
        raise ParamError("memory_id 不能为空：请传入 list 或 search 返回的 memory_id")
    if args.get("confirm") is not True:
        raise ParamError("删除是破坏性操作：必须显式传 confirm=true")
    from hymem.core.clientops import delete_memory_with_cascade

    client = _client()
    r = delete_memory_with_cascade(client, mid)
    # 与 CLI 同口径：success=False 不许报成功（上游 vdb_deleted 恒 True 不可信）。
    # partial/error_code 区分「部分存储没删净」与「本来就没这条」。
    if not r.get("success"):
        if r.get("partial") or r.get("error_code"):
            raise RuntimeError(
                "删除未完成（部分存储未删净）："
                f"error_code={r.get('error_code')}, deleted_count={r.get('deleted_count', 0)}"
            )
        raise RuntimeError(
            f"未找到 {mid}（deleted_count={r.get('deleted_count', 0)}）；"
            "可先 list_memories / search_memory 定位"
        )
    return json.dumps(
        {
            "deleted": mid,
            "swept": r.get("derived_purged") or [],
            "cascade_note": r.get("cascade_note") or None,
            "result": r,
        },
        ensure_ascii=False,
        default=str,
    )


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
    from hymem.core.clientops import update_memory

    client = _client()
    r = update_memory(client, mid, content)
    if r is None:
        return json.dumps({"found": False, "memory_id": mid}, ensure_ascii=False)
    if isinstance(r, dict) and r.get("success") is False:
        raise RuntimeError(f"更新失败：{r.get('error_message') or r.get('error') or 'unknown'}")
    return json.dumps(
        {"updated": mid, "note": "旧版本保留在演化链（get_recent_history/history 可查）", "result": r},
        ensure_ascii=False,
        default=str,
    )


def _tool_evolve_status(args: dict) -> str:
    """L6 自动沉淀状态查询（ultra 专属）。

    沉淀已自动化（add 后事件驱动触发，docs/specs/2026-10-30-evolve-auto/），
    这里只读 evolve_jobs.json 回报：最近记录 + 汇总（连续失败 / 下次可尝试
    时间 / 是否可立即尝试）。纯读状态文件，不碰图库——图库忙不忙都不影响。
    """
    mode = _memory_mode()
    if mode != "ultra":
        # 工具列表本来就不该在 pro 下出现（C11），但客户端可能缓存了旧列表，
        # 所以这里再实时校验一次——这是唯一可信的真源。
        raise ParamError(
            f"evolve_status 仅 ultra 模式可用（当前 mode={mode}）：L6 自动沉淀"
            "只在 ultra 下启用。切换：hy-mem config set mode=ultra。"
        )
    from hymem.core import autoevops

    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    return json.dumps(autoevops.status_summary(limit=limit), ensure_ascii=False)


def _tool_get_version(_args: dict) -> str:
    """版本与安装信息。自动升级链路的第一步：比对版本、定位要替换的安装根。"""
    import os
    from pathlib import Path

    from hymem.core import version as helptext
    from hymem.core import install_ops

    root = install_ops._current_root()
    cfg_env = os.getenv("HY_MEM_CONFIG", "").strip()
    cfg_path = Path(cfg_env) if cfg_env else root / "config.json"
    configured = cfg_path.is_file()
    data_dir = ""
    if configured:
        try:
            from hymem.core import config as cfgmod

            cfg = cfgmod.load()
            data_dir = str((cfg.get("storage") or {}).get("dataDir") or root / "data")
        except Exception:  # noqa: BLE001
            configured = False
    # 库健康摘要（2026-10-29）：shadow_only / dead 是「写了却检索不到/没写成」的
    # 静默坑，不主动调 queue_status 就永远看不见。给智能体一个零成本入口：
    # 每次 get_version 顺带带出，非零时直接给出处理命令。
    queue_health: dict = {}
    try:
        from hymem.core import store as _store

        _conn = _store.connect()
        try:
            _stats = _store.queue_stats(_conn)
        finally:
            _conn.close()
        _shadow = int(_stats.get("shadow_only") or 0)
        _dead = int(_stats.get("dead") or 0)
        _busy = int(_stats.get("pending") or 0) + int(_stats.get("running") or 0)
        if _shadow or _dead or _busy:
            queue_health = {"shadow_only": _shadow, "dead": _dead, "in_flight": _busy}
            notes = []
            if _shadow:
                notes.append(f"shadow_only={_shadow}（已落库但检索不到，`hy-mem retry --shadow` 重做）")
            if _dead:
                notes.append(f"dead={_dead}（死信，`hy-mem retry dead` 重投）")
            if _busy:
                notes.append(f"in_flight={_busy}（队列还有未完成任务）")
            queue_health["note"] = "；".join(notes)
    except Exception:  # noqa: BLE001
        pass  # 队列库读不到不影响版本信息本身
    mem_mode = _memory_mode()
    dist_mode = "exe" if getattr(sys, "frozen", False) else "source"
    payload = {
        "version": helptext.VERSION,
        # 记忆模式（决定有没有 L6 / scene）与打包形态（exe/source）是两件事，
        # 早先共用 "mode" 这个名字，宿主极容易把 exe 当成「模式」来理解。
        "memoryMode": mem_mode,
        "capabilities": {
            "evolve": mem_mode == "ultra",
            "l6Search": mem_mode == "ultra",
            # U4：不能只看 memoryMode——SDK 还要 mode != lite 且 coding 链路开启
            "scene": _scene_status()[0],
        },
        "distMode": dist_mode,
        # 兼容旧调用方，语义仍是打包形态（已 deprecated，用 distMode）
        "mode": dist_mode,
        "installRoot": str(root),
        "configPath": str(cfg_path),
        "configured": configured,
        "dataDir": data_dir or str(root / "data"),
    }
    if queue_health:
        payload["queue_health"] = queue_health
    # evolve 异常信号（与 queue_health 同款设计）：正常时不出现该键（零噪声）。
    # L6 沉淀已全自动，坏了没人看得见——get_version 是智能体日常必调的工具，
    # 连续失败 >0 时在这里顺带喊一嗓子，别让异常只有主动查 evolve_status 才发现。
    if mem_mode == "ultra":
        try:
            from hymem.core import autoevops as _ae

            _ev_fails = int(_ae.load_state().get("consecutive_failures") or 0)
            if _ev_fails > 0:
                payload["evolve_health"] = {
                    "consecutive_failures": _ev_fails,
                    "note": (
                        f"L6 自动沉淀已连续失败 {_ev_fails} 次（冷却按 2^n 自动退避）；"
                        "详情与历史用 evolve_status 查"
                    ),
                }
        except Exception:  # noqa: BLE001
            pass  # 状态文件读不到不影响版本信息本身
    return json.dumps(payload, ensure_ascii=False)


TOOLS = [
    {
        "name": "add_memory",
        "description": (
            "写入一条长期记忆（用户说「记一下」、表达偏好/决策/项目事实时）。"
            "写事实型内容（含主体+具体值），别写时间性流水（改投日志类工具）。"
            "默认异步：入队即返回，pro 单条落库可达 2 分钟，确认落库用 queue_status。"
            "scene 三选一：省略=chat；working/coding 记生产力/编码轨迹（必须传 messages，"
            "同步写入，检索须带同样 scene）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "记忆正文，一条一个主题（chat 用；与 messages 二选一）"},
                "messages": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "工具轨迹：OpenAI messages 数组，须含 assistant.tool_calls 或 role=tool 的消息。"
                        "仅 scene=working/coding 用（与 text 二选一）；没有轨迹会被明确拒绝"
                        "（SDK 对无轨迹的显式 scene 会静默回退 chat，hy-mem 在门口拦住）"
                    ),
                },
                "scene": {
                    "type": "string",
                    "enum": ["chat", "working", "coding"],
                    "default": "chat",
                    "description": (
                        "省略或 chat=普通对话记忆（默认）。判定规则：内容含 diff/patch/源码改动 → coding；"
                        "其它带工具轨迹的生产力操作 → working；纯文本 → 省略（走 chat）。"
                        "注意：scene 传错不会报错，只会写进另一个库，导致对应检索 0 命中；"
                        "working / coding / chat 三个库互不可见，跨库检索要分别调用。"
                        "capabilities.scene=false（lite 模式或 MEMORY_CODING_ENABLED=false）时不要传 scene，"
                        "会被明确拒绝（零写入）。working/coding 必须同时传 messages。"
                    ),
                },
                "workspace_id": {"type": "string", "description": "仓库/项目隔离（建议 git remote URL）；仅 scene=working/coding"},
                "branch": {"type": "string", "description": "分支隔离，必须同时传 workspace_id"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签（仅 chat 路径生效）"},
                "async": {"type": "boolean", "description": "默认 true（入队即返回）。false 同步落库（约 3~15s；抢不到写锁 5s 返回 lock_timeout，本条零写入可重试）；多条/并发务必用默认 true。working/coding 恒同步，此参数忽略"},
                "metadata": {"type": "object", "description": "附加元数据对象，如 {\"project\": \"iisw\"}"},
                "memory_at": {
                    "type": "string",
                    "description": "记忆发生时间（补录历史用）：2026-10-27 或 2026-10-27T14:30:00",
                },
                "request_id": {"type": "string", "description": "幂等写 id：同一 id 重复提交只落一条"},
            },
        },
    },
    {
        "name": "queue_status",
        "description": (
            "查异步写入进度：add_memory 返回的 job 走到哪了（待处理/运行中/完成/死信）、"
            "worker 是否存活。add 后想确认「落库了没」就调它，别用 search 空结果反推。"
            "wait_seconds>0 时短阻塞等待（上限 60 秒）。"
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
            "语义检索长期记忆，适合主题类问题（偏好/历史决策/项目上下文/技术事实）。"
            "时间类问题（「我昨天做了什么」）别用它——语义匹配对时间问法必空手，"
            "改用 list_memories 带 since/until。默认只搜 chat 记忆：working/coding "
            "在独立库，必须带 scene 才搜得到。查不到不代表没有，可换措辞再试。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词（语义匹配，不是关键词精确匹配）；与 queries 二选一"},
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "多路并行检索（≤5 条）：一次传多个措辞变体，SDK 融合去重，省得换措辞反复试；与 query 二选一",
                },
                "search_mode": {
                    "type": "string",
                    "enum": ["default", "agentic"],
                    "description": "agentic=智能体重写扩展后检索（更慢，命中率上限更高），第一轮 default 空手时可试；仅 chat 路径",
                },
                "rerank": {"type": "boolean", "description": "关闭重排可省时省 token（默认 true，排序质量优先就别传）"},
                "limit": {"type": "integer", "description": "返回条数，默认 5，上限 20"},
                "min_score": {"type": "number", "description": "相似度阈值，默认用配置值（0.35）"},
                "scene": {
                    "type": "string",
                    "enum": ["chat", "working", "coding"],
                    "default": "chat",
                    "description": (
                        "省略或 chat=只搜 chat 记忆（默认）。working=生产力记忆库；coding=编码记忆库"
                        "（带 diff 演进链，与 working 也不互见），两者返回 facts/experiences 两路。"
                        "注意：scene 传错不会报错，只会搜到空结果，看起来像「库里没有」；"
                        "chat / working / coding 互不可见，跨库检索要分别调用。"
                        "capabilities.scene=false（lite 模式或 MEMORY_CODING_ENABLED=false）时不要传 "
                        "working/coding，会被明确拒绝。"
                    ),
                },
                "workspace_id": {"type": "string", "description": "按仓库/项目过滤（须与写入时一致）；仅 scene=working/coding"},
                "branch": {"type": "string", "description": "按分支过滤（须与写入时一致），必须同时传 workspace_id"},
                "chain_block": {
                    "type": "boolean",
                    "description": (
                        "仅 scene=coding：额外返回该文件的 diff 演进块。默认 false（体量大），"
                        "只在问「这段代码改过几版/怎么演进」时开"
                    ),
                },
                "include_sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只保留这些来源的记忆（如 [\"mcp\"]）；与 exclude_sources 二选一",
                },
                "exclude_sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "排除这些来源的记忆（如 [\"cli\"]）；与 include_sources 二选一",
                },
                "since": {
                    "type": "string",
                    "description": "只要这段时间之后：相对值（30m/12h/7d/30d/1y）或 ISO 日期（2026-01-01）",
                },
                "until": {"type": "string", "description": "只要这段时间之前，格式同 since"},
                "offset": {"type": "integer", "description": "翻页偏移；上页返回 has_more 时用 next_offset 继续"},
                "schema": {
                    "type": "boolean",
                    "description": "是否附带 L6 行为模式（默认 true）。仅 ultra 且沉淀产出过才有；pro 下恒空并返回 schema_available=false，不报错",
                },
                "schema_limit": {
                    "type": "integer",
                    "description": "L6 条数，默认取 config.evolve.schemaLimit（3），上限 10",
                },
                "schema_min_score": {
                    "type": "number",
                    "description": (
                        "L6 语义分阈值（0~1），默认 0.25。schema 语言跟随 config.evolve.language"
                        "（显式 zh 时为中文归纳）；schemas 为空且 schema_note 提示阈值下无命中时，"
                        "传 0.2 或 0 重试"
                    ),
                },
            },
        },
    },
    {
        "name": "list_memories",
        "description": (
            "按时间顺序列出记忆（默认最新在前）。时间类问题（「我昨天/上周做了什么」）"
            "的正解：用 since/until 圈时间窗浏览。也适合回顾流水、验收刚写入的内容。"
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
            "返回 hy-mem 版本与安装信息（版本号 / exe或源码 / 安装根 / 配置与数据目录），"
            "可能附带 queue_health / evolve_health 异常信号。自动升级前先调它比对版本。"
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
            "更新一条记忆的正文（整条替换，旧版本保留可追溯）。内容有误/过时、"
            "用户纠正了某个事实时用；新增信息用 add_memory。必须 confirm=true。"
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

# ultra 专属工具：只在 mode=ultra 时出现在 tools/list 里（C11）。
# 为什么不干脆常驻+调用时拦截：宿主是照着工具列表决定要不要用的，pro 下看到
# evolve 会去试，白花一轮往返。代价是切模式后工具列表要重连才刷新——切模式
# 本来就是低频操作，而且 tools/call 还有实时二次校验兜底（C12）。
ULTRA_TOOLS = [
    {
        "name": "evolve_status",
        "description": (
            "查看 L6 自动沉淀的状态与历史（仅 ultra）。沉淀已全自动：add 素材攒够就"
            "后台跑，无需手动触发。想确认「跑没跑、归纳出几条 schema、为什么没跑」时用。"
            "records：skipped_small=素材不够（正常）；skipped_lock=图库被别的进程占着；"
            "failed=LLM 出错（冷却自动翻倍退避）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回最近几条记录，默认 20，上限 100",
                },
            },
        },
    },
]


def _visible_tools() -> list[dict]:
    """按当前记忆模式给出工具列表。"""
    if _memory_mode() == "ultra":
        return list(TOOLS) + list(ULTRA_TOOLS)
    return list(TOOLS)


_HANDLERS = {
    "add_memory": _tool_add_memory,
    "queue_status": _tool_queue_status,
    "search_memory": _tool_search_memory,
    "list_memories": _tool_list_memories,
    "get_memory": _tool_get_memory,
    "delete_memory": _tool_delete_memory,
    "update_memory": _tool_update_memory,
    "get_version": _tool_get_version,
    "evolve_status": _tool_evolve_status,
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
            # listChanged：工具集随 mode 变（evolve 仅 ultra），模式切换时
            # 服务端会主动发 notifications/tools/list_changed，宿主收到后
            # 重拉 tools/list 即可，无需重连。
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return None if is_notification else _resp(id_, {})
    if method == "tools/list":
        return None if is_notification else _resp(id_, {"tools": _visible_tools()})
    if method == "tools/call":
        name = params.get("name")
        handler = _HANDLERS.get(name)
        if handler is None:
            return _err(id_, INVALID_PARAMS, f"未知工具：{name}")
        try:
            _result = handler(params.get("arguments") or {})
            if name == "add_memory":
                # 事件驱动的 L6 自动沉淀检查（docs/specs/2026-10-30-evolve-auto/
                # R1，用户拍板）：add 完成后顺带看一眼素材够不够，满足就起
                # 一次性后台线程。检查是微秒级（读状态文件+时间戳），线程与
                # 本响应零交互，内部全兜底——绝不影响 add 的返回。
                try:
                    from hymem.core.autoevops import maybe_auto_evolve

                    _auto = maybe_auto_evolve()
                    if _auto.get("triggered"):
                        print(
                            "hy-mem mcp: auto-evolve triggered（素材攒够，后台沉淀中，"
                            "用 evolve_status 查进度）",
                            file=sys.stderr,
                        )
                except Exception:  # noqa: BLE001
                    pass
            return _resp(id_, _tool_result(_result))
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
    global _LAST_MODE
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        # 模式热切换检测（准实时）：stdio 服务是被动循环，没有文件监听线程，
        # config 被外部 CLI 改了只有下一条消息进来时才能发现。宿主每轮都会
        # 发请求（ping / tools/call…），所以下一轮就能刷新工具集——发出
        # list_changed 后客户端自动重拉 tools/list，无需重连。
        cur_mode = _memory_mode()
        if _LAST_MODE is None:
            _LAST_MODE = cur_mode
        elif cur_mode != _LAST_MODE:
            _LAST_MODE = cur_mode
            note = json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
                              ensure_ascii=True)
            sys.stdout.write(note + "\n")
            sys.stdout.flush()
            print(f"hy-mem mcp: mode -> {cur_mode}，已发 tools/list_changed", file=sys.stderr)
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
