# -*- coding: utf-8 -*-
"""

S3-1（2026-10-29）：本模块从入口 cli.py 平移而来（纯搬家，逻辑零变化）。
依赖方向：core 内部互引 + stdlib/SDK；不依赖 ui/mcp/入口。

"""
from __future__ import annotations

import os
import re
from datetime import datetime

from hymem.core import config as cfgmod
from hymem.core import store


def _parse_memory_at(v) -> "datetime | None":
    """--memory-at 的值 → datetime。支持 2026-10-27 / 2026-10-27 14:30:00 / ISO T 分隔。"""
    if v is None or not str(v).strip():
        return None
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"--memory-at 时间格式不认识：{s!r}（支持 2026-10-27、2026-10-27 14:30:00 这类写法）"
    )
def _snapshot_ids(client, ident: dict, cap: int = 2000) -> set[str]:
    """取写入前已存在的节点 id 集合，用于从 memory_ids 里剔除被 reconcile 合并到的老节点。

    实测：一次 add 的 memory_ids 并不等于"新建节点"，而是"本次影响到的节点"。
    写一句 CI 流水线的事实，会连带返回 8 个被合并/更新的旧节点 id。
    直接全部打标签 = 把标签污染到无关记忆上，所以必须做前后差分。
    """
    ids: set[str] = set()
    offset = 0
    # 只按 user 取全量快照：reconcile 会跨 session 合并节点，
    # 若同时按 session/agent 过滤，已知集合会偏小，导致把老节点误判为新节点。
    kwargs: dict = {}
    if (ident or {}).get("user_id"):
        kwargs["user_id"] = ident["user_id"]
    try:
        while offset < cap:
            r = client.list_memories(limit=200, offset=offset, **kwargs)
            batch = (r.get("vdb") or {}).get("memories") or []
            if not batch:
                break
            for it in batch:
                mid = it.get("memory_id")
                if mid:
                    ids.add(str(mid))
            offset += len(batch)
            if len(batch) < 200:
                break
    except Exception:  # noqa: BLE001
        pass
    return ids


def _ids_of(result: dict) -> list[str]:
    """pro 模式下 memory_id 是 l1_raw 事件 id，memory_ids 才是抽取后的最终节点 id。

    标签必须绑最终节点，否则在 search/list 里永远对不上。
    """
    ids = result.get("memory_ids") or []
    ids = [str(i) for i in ids if i]
    if not ids:
        mid = result.get("memory_id") or result.get("id")
        if mid:
            ids = [str(mid)]
    return ids


def _snapshot_ids(client, ident: dict, cap: int = 2000) -> set[str]:
    """取写入前已存在的节点 id 集合，用于从 memory_ids 里剔除被 reconcile 合并到的老节点。

    实测：一次 add 的 memory_ids 并不等于"新建节点"，而是"本次影响到的节点"。
    写一句 CI 流水线的事实，会连带返回 8 个被合并/更新的旧节点 id。
    直接全部打标签 = 把标签污染到无关记忆上，所以必须做前后差分。
    """
    ids: set[str] = set()
    offset = 0
    # 只按 user 取全量快照：reconcile 会跨 session 合并节点，
    # 若同时按 session/agent 过滤，已知集合会偏小，导致把老节点误判为新节点。
    kwargs: dict = {}
    if (ident or {}).get("user_id"):
        kwargs["user_id"] = ident["user_id"]
    try:
        while offset < cap:
            r = client.list_memories(limit=200, offset=offset, **kwargs)
            batch = (r.get("vdb") or {}).get("memories") or []
            if not batch:
                break
            for it in batch:
                mid = it.get("memory_id")
                if mid:
                    ids.add(str(mid))
            offset += len(batch)
            if len(batch) < 200:
                break
    except Exception:  # noqa: BLE001
        pass
    return ids
def _new_ids_only(returned: list[str], before: set[str] | None) -> list[str]:
    if not returned:
        return []
    if not before:
        return returned
    fresh = [i for i in returned if i not in before]
    # 全都被识别为老节点时（内容被 reconcile 判为"已存在"），退回第一个，
    # 避免标签无处可挂；这比把标签铺到 N 个老节点上安全。
    return fresh or returned[:1]
def _flatten_search(resp: dict) -> list:
    """search 返回 {memories:{schema:[],proactive:[],normal:[]}}，拍平后按 score 降序。"""
    mem = resp.get("memories") or {}
    items = []
    if isinstance(mem, dict):
        for bucket in ("normal", "proactive", "schema"):
            v = mem.get(bucket)
            if isinstance(v, list):
                items.extend(x for x in v if isinstance(x, dict))
        # 兜底：未知桶
        for k, v in mem.items():
            if k not in ("normal", "proactive", "schema") and isinstance(v, list):
                items.extend(x for x in v if isinstance(x, dict))
    elif isinstance(mem, list):
        items = [x for x in mem if isinstance(x, dict)]
    items.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return items


def _pending_jobs() -> int:
    """队列里还有多少条没落库。

    用途：解释了「刚写完却查不到」这种最常见困惑 —— 写是异步的，任务还在
    队列里等着 worker 消费，库自然没有它。查队列本身失败时返回 0，绝不
    因为一句提示而把检索搞挂。
    """
    try:
        conn = store.connect()
        try:
            stats = store.queue_stats(conn) or {}
            return int(stats.get("pending") or 0) + int(stats.get("running") or 0)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0
def _embedding_unhealthy() -> str:
    """空结果时的自证：嵌入端点到底通不通。通就返回 ""（真没命中）。

    只在「搜索结果为空」这条路上调用，正常命中不付这份时间（约 300ms）。
    """
    if os.getenv("HY_MEM_NO_PROBE", "").strip().lower() in ("1", "true", "yes"):
        return ""
    try:
        from hymem.core import config as cfgmod
        from hymem.ui import setup_wizard

        emb = (cfgmod.load().get("embedding") or {})
        good, msg, _dims = setup_wizard.probe_embedding(emb, timeout=20)
        return "" if good else msg
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"
def _bind_tags(ids: list[str], tags: list[str], source: str = "user") -> int:
    if not ids or not tags:
        return 0
    conn = store.connect()
    try:
        return store.bind_tags(conn, ids, tags, source)
    finally:
        conn.close()


def _bind_tags_to_new(client, ident: dict, before: set[str], returned: list[str],
                      tags: list[str]) -> tuple[list[str], set[str]]:
    """把用户标签绑到「本次真正新建的可检索节点」上，返回 (绑定的 id, 新快照)。

    为什么不直接用 add 返回的 ids（0.9.9 / PRD F7）：抽取没产出节点时，
    add 只回 l1_raw（status=shadow）的 id，标签就挂在一个永远搜不到的影子
    节点上——用户看到的现象是「标签丢了」，而且 list/search --tag 都返回空。
    改用「写入前后 list_memories 差分」，拿到的是向量库里真实存在的节点。

    差分对 reconcile 也成立：把内容合并进老节点时 fresh 为空，退回
    `_new_ids_only` 的老逻辑（至少挂一个，别让标签彻底没有去处）。
    """
    after = _snapshot_ids(client, ident)
    fresh = sorted(after - before)
    target = fresh or _new_ids_only(returned, before)
    _bind_tags(target, tags, "user")
    return target, after


def _remember_auto(items: list[dict]) -> None:
    """把检索/列表顺带看到的 LLM 标签缓存进本地索引，供 tags 统计与过滤复用。"""
    if os.getenv("HY_MEM_TAG_AUTOSYNC") == "0":
        return
    pairs = [(str(it.get("memory_id")), it.get("tags") or []) for it in items if isinstance(it, dict)]
    pairs = [(mid, tags) for mid, tags in pairs if mid and mid != "None" and tags]
    if not pairs:
        return
    try:
        conn = store.connect()
        try:
            for mid, tags in pairs:
                store.bind_tags(conn, [mid], [str(t) for t in tags], "auto")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass  # 只读路径不能因为标签缓存失败而整体报错


def _effective_tags(items: list[dict]) -> dict[str, set[str]]:
    mids = [str(it.get("memory_id")) for it in items if isinstance(it, dict) and it.get("memory_id")]
    local: dict[str, list[str]] = {}
    if mids:
        try:
            conn = store.connect()
            try:
                local = store.tags_for(conn, mids)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            local = {}
    out: dict[str, set[str]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = str(it.get("memory_id") or "")
        s = {str(t).lower() for t in (it.get("tags") or [])}
        s |= {str(t).lower() for t in local.get(mid, [])}
        out[mid] = s
    return out


def _filter_by_tags(items: list[dict], tags: list[str] | None, tag_mode: str = "all") -> list[dict]:
    """标签过滤。SDK 的 search() 没有 tag 参数，只能在召回结果上后过滤，
    因此调用方必须先 over-fetch（多取 N 倍），否则过滤后可能剩不下东西。"""
    if not tags:
        return items
    want = {t.strip().lower() for t in tags if t and t.strip()}
    if not want:
        return items
    eff = _effective_tags(items)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        have = eff.get(str(it.get("memory_id") or ""), set())
        hit = want <= have if tag_mode == "all" else bool(want & have)
        if hit:
            it["_tags"] = sorted(have)
            out.append(it)
    return out


def _worker_cap() -> int:
    """消费并发线程数（lane 数）。HY_MEM_MAX_WORKERS 可覆盖，默认 3。

    0.9.8 起语义变化：以前是「detached worker 进程数上限」，现在是单个
    supervisor 进程内的消费线程数。SDK 的 reconcile 是读改写序列，跨进程/
    跨线程必须由 WriteLock 串行（锁覆盖整个 client.add 含 LLM 抽取），
    所以多个消费进程是伪并发——N 个进程只会在锁上排队，还各付一份 SDK
    初始化（hy_memory.log / chroma 的多进程初始化竞态正是历史并发风暴的
    根源）。线程池里锁语义不变，非临界区（snapshot 读、入账、退避调度）
    可与在途写入重叠。
    """
    try:
        return max(1, int(os.getenv("HY_MEM_MAX_WORKERS") or 3))
    except ValueError:
        return 3


def _spawn_detached_worker(idle_exit: float = 90.0,
                           extra_args: list[str] | None = None) -> bool:
    """拉起一个脱管的后台消费 supervisor（空闲自退出），成功返回 True。

    为什么不常驻：智能体场景下「临时拉起 + 空闲自杀」最干净 —— add --async
    入队后立即返回毫秒级，落库由后台进程完成，不留僵尸进程。

    消费并发模型（0.9.8）：detached supervisor 恒为 1 个进程，进程内跑
    N 个消费线程（--lanes，默认 HY_MEM_MAX_WORKERS=3）。进程数恒 1 是
    有意的——见 _worker_cap 的语义说明。WriteLock 与 SQLite 抢占兜底，
    即使拉重了也无害（闸门 cap=1 + 心跳判活）。
    """
    from hymem.core import install_ops

    # 上限闸门（WriteLock 内占位，串行化风暴竞态）：detached supervisor 恒 1 个。
    # 占位行 15s 过期，Popen 失败也不会永久卡死后续拉起。
    try:
        with store.WriteLock():
            conn = store.connect()
            try:
                slot = store.claim_worker_slot(conn, 1)
            finally:
                conn.close()
    except Exception:  # noqa: BLE001  闸门失败宁可多拉不多丢
        slot = True
    if not slot:
        return False

    argv = install_ops.target_command() + [
        "worker",
        "--idle-exit", str(idle_exit),
        "--poll", "1.0",
        "--lanes", str(_worker_cap()),
    ] + (extra_args or [])
    try:
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW：不闪黑框
            # stderr 落文件而非 DEVNULL：worker 若在启动期静默死亡（日志
            # 文件竞态/readonly 等），这里就是唯一现场
            try:
                err_f = open(store.data_dir() / "logs" / "worker.err", "ab")
            except OSError:
                err_f = subprocess.DEVNULL
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f, creationflags=flags, close_fds=True,
            )
        else:
            err_f = None
            try:
                (store.data_dir() / "logs").mkdir(parents=True, exist_ok=True)
                err_f = open(store.data_dir() / "logs" / "worker.err", "ab")
            except OSError:
                err_f = None
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f or subprocess.DEVNULL, start_new_session=True,
            )
        return True
    except OSError:
        return False


def _spawn_detached_serve(idle_exit: float = 1800.0) -> bool:
    """拉起 detached 的向量库服务（数据唯一所有者）。

    与 worker 分开拉：serve 必须长驻单例，worker 天然短命——0.9.9 把两者塞进
    同一个进程，消费者退出就顺手把数据所有者带走了（见 PRD F2/F5）。

    失败只 stderr 留痕、返回 False：调用方（ensure_endpoint）会继续轮询，
    也许已有别的进程把它拉起来了。
    """
    from hymem.core import install_ops

    argv = install_ops.target_command() + ["serve", "--idle-exit", str(idle_exit)]
    try:
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW：不闪黑框
            try:
                logs = store.data_dir() / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                err_f: object = open(logs / "serve.err", "ab")
            except OSError:
                err_f = subprocess.DEVNULL
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f, creationflags=flags, close_fds=True,
            )
        else:
            err_f = None
            try:
                logs = store.data_dir() / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                err_f = open(logs / "serve.err", "ab")
            except OSError:
                err_f = None
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=err_f or subprocess.DEVNULL, start_new_session=True,
            )
        return True
    except OSError:
        return False
