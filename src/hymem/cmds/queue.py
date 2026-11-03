"""异步队列命令：serve / worker / queue / retry / drain（消费线程体在此）。

S2 拆分（docs/specs/2026-11-02-cli-split-ports/）：本模块从 hymem/cli.py 按域拆入，
逻辑零变化——全局状态存于 cmds.base，跨模块经 _base 属性读写。
"""
from __future__ import annotations

from hymem.cmds.base import (  # noqa: F401
    EXIT_RUNTIME, EXIT_USAGE, _bind_tags_to_new, _ids_of, _snapshot_ids,
    _worker_cap, commit_extracted_with_retry, datetime, emit, extract_phase,
    fail, json, make_client, ok, os,
    prepare_commit, require_ready, socket, store, sys,
    time, vdb_endpoint,
)


def _wanted_serve_port() -> int:
    """servePort 配置 → 固定端口；0 = 随机端口（历史默认）。

    配置读坏/值非法时**不阻断起库**，但必须往 stderr 说明原因——静默回退会让
    "端口怎么又变了" 变成无迹可查的现场。`servePort` 非法（如 "abc"）走同一条
    告警路径，回退随机端口。
    """
    try:
        from hymem.core import config as _cfgmod

        return int(_cfgmod.load().get("servePort") or 0)
    except Exception as e:  # noqa: BLE001  配置坏了不该拦住 serve 起库
        print(
            f"hy-mem: 读取 servePort 配置失败（{type(e).__name__}: {e}），"
            "serve 将以随机端口启动（不阻断起库）",
            file=sys.stderr,
        )
        return 0


def cmd_serve(args) -> int:
    """向量库服务：数据唯一所有者（长驻、单例、独占向量库目录）。

    chroma embedded 不支持跨进程共享（0.9.8 的召回池空窗就是这么来的），
    所以向量库必须由一个进程独占，其余进程经 HttpClient 访问。这个进程就是
    serve——它不做消费、不做业务，只把数据端开起来并登记端口。

    幂等：已有活着的 serve 时本进程立即退出（`already: true`），方便脚本
    "确保有服务"。空闲退出：--idle-exit 秒内没有任何 HTTP 请求就自行收摊，
    退出时只删自己登记的端口文件（owner 校验，见 vdb_endpoint）。
    """
    info = vdb_endpoint.read_port_info()
    if info and vdb_endpoint.pid_alive(info["pid"]) and vdb_endpoint.heartbeat_ok(info["port"]):
        payload = {"ok": True, "already": True, "port": info["port"], "pid": info["pid"]}
        return ok(payload, args.json,
                  f"向量库服务已在运行（pid {info['pid']}, :{info['port']}），本进程无需启动")

    persist = vdb_endpoint.sdk_persist_dir()
    # U3（docs/specs/2026-11-02-endpoint-refresh/）：servePort 配置的固定端口，
    # 0/缺省 = 随机（历史行为）；被占用时 start_server_thread 自行回退并告警。
    _want_port = _wanted_serve_port()
    t0 = time.time()
    port, handle = vdb_endpoint.start_server_thread(persist, port=_want_port or None)

    # 登记要在锁里做：两个 serve 同时启动时，只让先登记的活下来，另一个
    # 立即收摊——同一数据目录绝不允许两个所有者。
    with store.WriteLock():
        other = vdb_endpoint.read_port_info()
        if (other and other["pid"] != os.getpid()
                and vdb_endpoint.pid_alive(other["pid"])
                and vdb_endpoint.heartbeat_ok(other["port"])):
            handle.stop(2.0)
            payload = {"ok": True, "already": True, "port": other["port"], "pid": other["pid"]}
            return ok(payload, args.json,
                      f"向量库服务已在运行（pid {other['pid']}, :{other['port']}），本进程退出")
        vdb_endpoint.write_port_file(port)

    if not args.json:
        print(f"hy-mem: 向量库服务就绪 :{port}（数据 {persist}）", file=sys.stderr)

    idle = float(getattr(args, "idle_exit", 0) or 0)
    timeout = float(getattr(args, "timeout", 0) or 0)
    reason = "stopped"
    try:
        while True:
            if not handle.alive:
                reason = "crashed"
                break
            now = time.time()
            if timeout and now - t0 >= timeout:
                reason = "timeout"
                break
            if idle and now - vdb_endpoint.last_activity() >= idle:
                reason = "idle"
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        vdb_endpoint.stop_server_thread(handle, port)

    payload = {"ok": True, "already": False, "port": port, "pid": os.getpid(),
               "reason": reason, "uptime_s": round(time.time() - t0, 1)}
    return ok(payload, args.json,
              f"向量库服务退出（{reason}，运行 {time.time() - t0:.0f}s）")


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _batch_compat(row) -> bool:
    """批次约束（spec §1 合批约束）：显式字段/request 级参数不入批。

    tags 也在此列：批路径没有逐 job 的 before/after 快照，无法只给本条新
    节点打标签（见 _execute_job 的 _bind_tags_to_new）。带标签的 add 必须
    回退单条路径，否则标签会静默丢失。
    """
    import json as _json

    try:
        payload = _json.loads(row["payload"])
    except Exception:  # noqa: BLE001
        return False
    if str(row["kind"] or "") != "add":
        return False
    if str(row["mode"] or "").strip().lower() == "lite":
        return False
    for k in ("memory_at", "metadata", "request_id", "tags"):
        if payload.get(k):
            return False
    return True


def _result_failure(result: dict) -> str | None:
    """单条/批路径共用的静默失败防线（0.9.8）。

    L1 原始层写成功 ≠ 记忆可用。SDK 在 L2 事实抽取失败时仍可能返回
    success=True + 非空 ids（含 L1 id），诊断字段 agent_error_code 会带
    EMPTY_RESPONSE / RECONCILE_FAILED 等。只判 ids 会把「抽取失败」标成 done，
    记忆永远停在未抽取的 L1 状态 —— 这里统一判定为失败进重试。

    注意区分：extractor 判定「无可提取事实」是正常结论（agent_status=success、
    agent_error_code 为空、L1 退居 shadow），不误判、不重试。
    """
    if result.get("success") is False:
        return f"SDK 写入失败：{result.get('error_message') or 'unknown'}"
    agent_code = result.get("agent_error_code") or ""
    if agent_code:
        return f"agent 抽取失败（{agent_code}）：{result.get('agent_error') or ''}"
    if not _ids_of(result):
        # SDK 偶发把上游失败吞成「成功 + 空 ids」：实测 LLM 抽取 EMPTY_RESPONSE
        # 时会回滚 L1 并返回 ok+[]。必须视为失败，否则静默丢数据。
        return f"SDK add 返回空 memory_ids（疑似抽取失败被吞）：{str(result)[:200]}"
    return None


def _execute_batch(rows, get_client) -> list[tuple[int, dict, list[str], str | None, Exception | None]]:
    """S3 批次执行：逐 job 锁外 extract+prepare → 一次 batch_commit（锁内）。

    返回逐 job 五元组 (job_id, result, ids, note, exc)：
    exc 为 None 即成功（ids/note 取自 result）；extract/prepare 单 job
    失败只标记该 job（exc 非 None，落账走 mark_failed），其余照常进批；
    batch_commit 抛 CommitFailed 时整批全部标失败（exc 带上下文）。
    """
    from hymem.core.two_phase import CommitFailed as _CF
    from hymem.core.two_phase import batch_commit, extract_phase, prepare_commit

    if not rows:
        return []
    client = get_client(rows[0]["mode"])
    jobs: list[dict] = []
    per: dict[int, dict] = {}
    for row in rows:
        payload = json.loads(row["payload"])
        ident = payload.get("identity") or {}
        try:
            ar = extract_phase(client, payload["text"],
                               source=payload.get("source") or "cli", **ident)
            prepared = prepare_commit(client, payload["text"], ar,
                                      source=payload.get("source") or "cli", **ident)
        except Exception as e:  # noqa: BLE001  单 job 失败不连坐（P4）
            per[row["id"]] = {"exc": e}
            continue
        job = {"text": payload["text"], "agent_result": ar, "prepared": prepared}
        jobs.append(job)
        per[row["id"]] = {"job": job}
    results: list[dict] = []
    if jobs:
        try:
            with store.WriteLock():
                results = batch_commit(client, jobs)
        except _CF as e:
            err = RuntimeError(f"批次提交失败：{e}")
            return [(r["id"], {}, [], None, err) for r in rows]
        except Exception as e:  # noqa: BLE001
            return [(r["id"], {}, [], None, e) for r in rows]
    # 对齐回逐 job（jobs 顺序 == rows 中成功 extract 的顺序）
    out: list[tuple[int, dict, list[str], str | None, Exception | None]] = []
    ri = 0
    for row in rows:
        p = per[row["id"]]
        if "exc" in p:
            out.append((row["id"], {}, [], None, p["exc"]))
            continue
        r = results[ri]
        ri += 1
        fail = _result_failure(r)
        if fail:
            out.append((row["id"], r, [], None, RuntimeError(fail)))
            continue
        ids = _ids_of(r)
        note = None
        ln = (r.get("batch") or {}).get("lineage_note") or ""
        if ln:
            note = f"{ln}（挂链缺失但记忆可检索）"
        out.append((row["id"], r, ids, note, None))
    return out


def _execute_job(conn, row, get_client) -> tuple[dict, list[str], str | None]:
    payload = json.loads(row["payload"])
    client = get_client(row["mode"])
    kind = row["kind"]
    ident = payload.get("identity") or {}

    if kind == "finalize":
        with store.WriteLock():
            result = client.finalize_session(
                user_id=payload.get("user_id") or "",
                agent_id=payload.get("agent_id") or "default_agent",
                session_id=payload.get("session_id") or "default_session",
            )
        return result, [], None

    tags = [t for t in (payload.get("tags") or []) if str(t).strip()]
    # add 透传参数：metadata 原样；memory_at 是 ISO 字符串要转回 datetime；
    # request_id 原样（幂等写，同一 id 重复消费不会写重——重试场景的关键护栏）
    add_kw: dict = {}
    if payload.get("metadata"):
        add_kw["metadata"] = payload["metadata"]
    if payload.get("memory_at"):
        try:
            add_kw["memory_at"] = datetime.fromisoformat(str(payload["memory_at"]))
        except ValueError:
            pass  # 坏时间戳不入参（解析在入队前已拦截，这里是防御）
    if payload.get("request_id"):
        add_kw["request_id"] = str(payload["request_id"])
    before, before_complete = _snapshot_ids(client, ident) if tags else (set(), True)
    # R4 两段式（worker 侧，2026-10-29）：3 lane 线程共享一把跨进程 WriteLock，
    # 旧写法「锁内整段 client.add（含 LLM）」把 3 车道退化成串行 × 全程耗时。
    # 改为 extract 锁外（3 lane 真·并行过 LLM）+ 锁内 SDK 标准提交段。
    # lite 模式无 LLM 抽取（agent_mode=disabled），保持旧整段路径。
    mode_str = str(row["mode"] or "").strip().lower()
    if mode_str == "lite" or getattr(client, "_mode", "") == "lite":
        with store.WriteLock():
            result = client.add(
                payload["text"],
                source=payload.get("source") or "cli",
                **add_kw,
                **ident,
            )
    else:
        agent_result = extract_phase(
            client, payload["text"], source=payload.get("source") or "cli",
            **add_kw, **ident,
        )
        # R4.7 预计算出锁：3 lane 在锁外并行算 L1 向量 + 本批 dedup
        prepared = prepare_commit(
            client, payload["text"], agent_result,
            source=payload.get("source") or "cli", **add_kw, **ident,
        )
        with store.WriteLock():
            result = commit_extracted_with_retry(
                client, payload["text"], agent_result, prepared=prepared,
                source=payload.get("source") or "cli", **add_kw, **ident,
            )
        # 两段式的失败都走异常（ExtractFailed/CommitFailed → 上层 mark_failed
        # 重试，与旧路径 success=False / agent_error_code 语义对齐）；
        # extract 成功但 0 facts 是正常结论 → stored_ids 空 → shadow_only note。
    fail = _result_failure(result)
    if fail:
        raise RuntimeError(fail)
    ids = _ids_of(result)

    # 只落了 l1_raw 影子节点（PRD F9）：抽取没报错，但没产出可检索节点，
    # 记忆实际"写进去了却永远搜不到"。不做静默成功——打 stderr 告警，并把
    # shadow_only 写进任务行，queue --status done 可见、retry --shadow 可一键重做。
    note = None
    if not result.get("memory_ids"):
        note = ("shadow_only: 抽取未产出可检索节点（只落 l1_raw 影子），本条检索不到；"
                "内容确有可抽取的事实时可 `hy-mem retry --shadow` 重做")
        print(f"hy-mem: job {row['id']} 只落影子节点，检索不到（见 retry --shadow）",
              file=sys.stderr)

    tagged: list[str] = []
    if tags:
        tagged, tag_note = _bind_tags_to_new(
            client, ident, before, ids, [str(t) for t in tags],
            before_complete=before_complete,
        )
        if tag_note:
            note = f"{note}；{tag_note}" if note else tag_note
    return result, (tagged or ids), note


class _LaneState:
    """多 lane 共享的计数器（一把小锁串行化增减与读取）。"""

    def __init__(self):
        import threading

        self._lock = threading.Lock()
        self.ok = 0
        self.failed = 0
        self.active = 0  # 正在执行任务的 lane 数
        self.claimed_any = False
        self.last_active = time.time()
        # lane 因领取/统计/入账异常而退出：必须可诊断，且不能装作正常收工
        self.dead = 0
        self.errors: list[str] = []

    def claim_started(self) -> None:
        with self._lock:
            self.active += 1
            self.claimed_any = True
            self.last_active = time.time()

    def claim_ended(self) -> None:
        with self._lock:
            self.active -= 1
            self.last_active = time.time()

    def done_one(self, ok: bool) -> tuple[int, int]:
        with self._lock:
            if ok:
                self.ok += 1
            else:
                self.failed += 1
            return self.ok + self.failed

    def processed(self) -> int:
        with self._lock:
            return self.ok + self.failed

    def lane_died(self, lane_id: int, where: str, exc: Exception) -> None:
        """记录一条 lane 死亡（限流错误条数），供 cmd_worker 汇总诊断。"""
        with self._lock:
            self.dead += 1
            if len(self.errors) < 5:
                self.errors.append(
                    f"lane{lane_id} died at {where}: {type(exc).__name__}: {exc}"
                )

    def deaths(self) -> tuple[int, list[str]]:
        with self._lock:
            return self.dead, list(self.errors)


def _worker_batch_round(conn, rows_batch, args, st, lane_id, get_client, lane_dead) -> bool:
    """一次合批执行（≥2 条）+ 逐 job 入账。返回 False 表示 lane 必须退出。

    等价提取自 cmd_worker::_lane_loop 的批处理段：语句顺序、日志文案、
    异常边界（KeyboardInterrupt 上抛 / 其它异常兜底标失败）逐字保持原样。
    """
    st.claim_started()
    t0 = time.time()
    job_ids = [r["id"] for r in rows_batch]
    try:
        outcomes = _execute_batch(rows_batch, get_client)
        for job_id, result, ids, note, exc in outcomes:
            if exc is None:
                # 入账段单独窄捕获：写库异常不能静默杀死 lane
                try:
                    store.mark_done(conn, job_id, ids,
                                    extra={"note": note} if note else None)
                except Exception as me:  # noqa: BLE001
                    lane_dead(lane_id, "mark_done", me)
                    return False
                st.done_one(True)
                ln = (result.get("batch") or {}).get("lineage_note") or ""
                print(f"[lane{lane_id}][batch][{job_id}] add ok "
                      f"ids={len(ids)}" + (" shadow_only" if note and not ln else ""),
                      file=sys.stderr)
                if args.json:
                    emit({"ok": True, "job": job_id, "kind": "add",
                          "batch": result.get("batch"),
                          "memory_ids": ids, "note": note}, True)
            else:
                st.done_one(False)
                try:
                    state = store.mark_failed(
                        conn, job_id, f"{type(exc).__name__}: {exc}", retry=True)
                except Exception as me:  # noqa: BLE001
                    lane_dead(lane_id, "mark_failed", me)
                    return False
                print(f"[lane{lane_id}][batch][{job_id}] FAILED -> {state}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                if args.json:
                    emit({"ok": False, "job": job_id, "state": state,
                          "error": str(exc)}, True)
        ms = int((time.time() - t0) * 1000)
        print(f"[lane{lane_id}][batch] {len(rows_batch)} jobs "
              f"judge_llm=1 in {ms}ms", file=sys.stderr)
    except KeyboardInterrupt:
        for jid in job_ids:
            store.mark_failed(conn, jid, "interrupted", retry=True)
        raise
    except Exception as e:  # noqa: BLE001  批路径兜底：绝不留 running 僵尸
        import traceback

        traceback.print_exc()
        for jid in job_ids:
            st.done_one(False)
            try:
                state = store.mark_failed(
                    conn, jid, f"batch aborted: {type(e).__name__}: {e}",
                    retry=True)
            except Exception as me:  # noqa: BLE001
                lane_dead(lane_id, "mark_failed", me)
                return False
            if args.json:
                emit({"ok": False, "job": jid, "state": state,
                      "error": str(e)}, True)
    finally:
        st.claim_ended()
    return True


def _worker_should_stop(args, st, started, stats) -> bool:
    """无可领任务时的四类收工判定（once / 快退 / 空闲 / 超时），顺序不变。"""
    if args.once and st.active == 0:
        # 一次性模式：无可领任务且本进程无在途 → 收工。
        # 延迟重试的任务（not_before 未到）留待下次消费。
        return True
    if (
        not getattr(args, "no_fast_exit", False)
        and not st.claimed_any
        and time.time() - started >= 30
        and stats["pending"] == 0
    ):
        # 空闲快速释放：拉起后 30s 从没抢到过任务且队列空 → 尽早退出
        return True
    if (
        args.idle_exit
        and time.time() - st.last_active >= args.idle_exit
        and stats["pending"] == 0
        and st.active == 0
    ):
        # 空闲释放：只在队列真正清空时退——延迟任务到期前
        # pending>0，不会误退，supervisor 会守到它可领。
        return True
    if args.timeout and time.time() - started >= args.timeout:
        return True
    return False


def _worker_single_round(conn, row, args, st, lane_id, get_client, lane_dead) -> bool:
    """单条任务执行 + 入账。返回 False 表示 lane 必须退出（入账段失败）。"""
    job_id, kind = row["id"], row["kind"]
    st.claim_started()
    t0 = time.time()
    try:
        # 任务执行仍是 per-job 捕获（不扩大到一个大 except 里）
        try:
            result, ids, note = _execute_job(conn, row, get_client)
        except KeyboardInterrupt:
            try:
                store.mark_failed(conn, job_id, "interrupted", retry=True)
            except Exception:  # noqa: BLE001  中断收尾失败不掩盖原异常
                pass
            raise
        except Exception as e:  # noqa: BLE001
            st.done_one(False)
            try:
                state = store.mark_failed(
                    conn, job_id, f"{type(e).__name__}: {e}", retry=True)
            except Exception as me:  # noqa: BLE001  入账失败：lane 记账后退出
                lane_dead(lane_id, "mark_failed", me)
                return False
            print(
                f"[lane{lane_id}][{job_id}] {kind} FAILED -> {state}: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )
            if args.json:
                emit({"ok": False, "job": job_id, "state": state,
                      "error": str(e)}, True)
            # 退避写在任务行（not_before），这里不 sleep，立刻领下一个
        else:
            # 入账段单独窄捕获：mark_done 失败不能静默杀死 lane
            try:
                store.mark_done(conn, job_id, ids,
                                extra={"note": note} if note else None)
            except Exception as me:  # noqa: BLE001
                lane_dead(lane_id, "mark_done", me)
                return False
            st.done_one(True)
            ms = int((time.time() - t0) * 1000)
            print(f"[lane{lane_id}][{job_id}] {kind} ok {ms}ms ids={len(ids)}"
                  + (" shadow_only" if note else ""), file=sys.stderr)
            if args.json:
                emit({"ok": True, "job": job_id, "kind": kind, "ms": ms,
                      "memory_ids": ids, "note": note}, True)
    finally:
        st.claim_ended()
    return True


def _worker_finish(args, lanes, wid, st, interrupted, clients, hb_conn) -> int:
    """收尾：注销注册行、关客户端、汇总统计并输出（退出码由 lane 死亡决定）。"""
    try:
        store.worker_leave(hb_conn, wid)  # 注销注册行，立即腾出拉起名额
    except Exception:  # noqa: BLE001
        pass
    for c in clients.values():
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        hb_conn.close()
    except Exception:  # noqa: BLE001
        pass

    stats = None
    conn = store.connect()
    try:
        stats = store.queue_stats(conn)
    finally:
        conn.close()
    processed = st.processed()
    deaths, lane_errors = st.deaths()
    payload = {
        "ok": not deaths, "lanes": lanes, "processed": processed,
        "succeeded": st.ok, "failed": st.failed, "queue": stats,
    }
    if deaths:
        # 「全部 lane 死掉但退出码 0」是旧缺陷：这里显式标记并汇总可诊断信息
        payload["lane_deaths"] = deaths
        payload["lane_errors"] = lane_errors
    if interrupted:
        payload["interrupted"] = True
    tail = f"，延迟重试 {stats['delayed']} 条" if stats.get("delayed") else ""
    human = (f"worker 退出：{lanes} 线程，成功 {st.ok} / 失败 {st.failed} / "
             f"剩余待处理 {stats['pending']}{tail}")
    if deaths:
        human += f"；{deaths} 条 lane 异常退出（详见 lane_errors 与 stderr）"
    rc = ok(payload, args.json, human)
    return rc if not deaths else EXIT_RUNTIME


def cmd_worker(args) -> int:
    """队列消费者：单进程 supervisor + N 个消费线程（lane）。

    为什么是线程不是多进程：SDK 的 add 全程需要 WriteLock 串行（reconcile
    读改写，锁必须覆盖整个 client.add 含 LLM 抽取），多进程只是排队，吞吐
    恒为 1，还各付一份 SDK 初始化与 chroma 竞态。线程模型：lane 在锁外的
    部分（snapshot 读、入账、退避调度）可与在途写入重叠；client 按 mode
    共享一个实例，SDK 内部单事件循环保证读操作安全。

    失败退避不在循环里 sleep（旧实现单消费者时一个失败任务会把整个队列
    卡住最长 30s）：mark_failed 把 not_before 写进任务行，claim_next 只领
    到期任务，lane 立刻去领下一个。
    """
    import threading

    rc = require_ready("消费写入队列")
    if rc is not None:
        return rc
    wid = _worker_id()
    lanes = max(1, int(getattr(args, "lanes", 0) or 0) or _worker_cap())
    if lanes > 16:
        lanes = 16  # 防御：线程再多也只会挤在 WriteLock 上，没有意义

    # 启动期回收僵死任务套瞬态重试：新建库/风暴期 WAL -shm 重建窗口会让
    # 这条 UPDATE 撞 readonly（code 8），曾实测导致任务卡 pending
    if args.no_recover:
        recovered = 0
    else:
        recovered = store.transient_write(lambda c: store.requeue_stale(c))
    if recovered:
        print(f"hy-mem: 回收 {recovered} 个僵死任务", file=sys.stderr)

    hb_conn = store.connect()
    store.worker_purge_stale(hb_conn)  # 清理硬杀残留的注册行

    # 本进程只是消费者，**永不起 chroma server**（0.9.10 角色分离）。
    # 需要端点时 make_client → ensure_endpoint 会去拉起/复用 serve；端点可用
    # 时它注入 env，SDK 自动走 HttpClient。0.9.9 让 worker 顺手当宿主，短命
    # 消费者退出就把数据所有者带走，端口文件被误删（PRD F2/F5）。

    clients: dict[str, object] = {}
    clients_lock = threading.Lock()

    def get_client(mode: str | None):
        key = mode or "default"
        with clients_lock:
            if key not in clients:
                clients[key] = make_client(mode)  # 创建串行化，避免进程内再撞初始化
            return clients[key]

    stop = threading.Event()
    st = _LaneState()
    started = time.time()

    def _lane_dead(lane_id: int, where: str, exc: Exception) -> None:
        """lane 因领取/统计/入账异常退出：记账 + 打一条可诊断日志。"""
        st.lane_died(lane_id, where, exc)
        print(f"hy-mem: lane died: lane{lane_id} at {where}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)

    def _lane_loop(lane_id: int) -> None:
        try:
            conn = store.connect()
        except Exception as e:  # noqa: BLE001  连不上库，这条 lane 无法工作
            _lane_dead(lane_id, "connect", e)
            return
        try:
            while not stop.is_set():
                if args.max and st.processed() >= args.max:
                    stop.set()
                    break
                # S3 合批消费（2026-10-30 batch-commit）：队列有货且批次约束
                # 满足时一次领多条同 user+agent 的 add；HY_MEM_BATCH_MAX=0 关闭。
                # 判定 LLM 次数 N→1；单条路径完全保留（首条约束不满足即走原路）。
                batch_max = 0
                try:
                    batch_max = max(0, int(os.getenv("HY_MEM_BATCH_MAX") or 4))
                except ValueError:
                    batch_max = 4
                rows_batch: list = []
                stats: dict | None = None
                if batch_max > 1:
                    # 领取/统计段：窄捕获，异常即 lane 无法继续，记录后退出线程。
                    # stats 一轮只查一次，传给 claim_next_batch 并在空队列分支复用，
                    # 不再每轮重复 4 条统计 SQL（P1-4）。
                    try:
                        stats = store.queue_stats(conn)
                        if (stats.get("pending") or 0) > 1:
                            rows_batch = store.claim_next_batch(
                                conn, wid, limit=batch_max, compat_check=_batch_compat,
                                stats=stats,
                            )
                    except Exception as e:  # noqa: BLE001
                        _lane_dead(lane_id, "claim_batch", e)
                        return
                if len(rows_batch) >= 2:
                    if not _worker_batch_round(conn, rows_batch, args, st,
                                               lane_id, get_client, _lane_dead):
                        return
                    continue
                if rows_batch:
                    # 只领到 1 条（约束/异 user 止批）：退回单条路径语义
                    row = rows_batch[0]
                else:
                    try:
                        row = store.claim_next(conn, wid)
                    except Exception as e:  # noqa: BLE001  领取段异常，lane 无法继续
                        _lane_dead(lane_id, "claim_next", e)
                        return
                if row is None:
                    if stats is None:
                        try:
                            stats = store.queue_stats(conn)
                        except Exception as e:  # noqa: BLE001  统计段异常，lane 无法继续
                            _lane_dead(lane_id, "queue_stats", e)
                            return
                    if _worker_should_stop(args, st, started, stats):
                        stop.set()
                        break
                    time.sleep(args.poll)
                    continue

                if not _worker_single_round(conn, row, args, st, lane_id,
                                            get_client, _lane_dead):
                    return
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def lane(lane_id: int) -> None:
        """线程入口：最后一道兜底只记录死亡，不改变 per-job 执行捕获语义。"""
        try:
            _lane_loop(lane_id)
        except Exception as e:  # noqa: BLE001
            _lane_dead(lane_id, "lane", e)

    threads = [
        threading.Thread(target=lane, args=(i,), daemon=True, name=f"lane-{i}")
        for i in range(lanes)
    ]
    for t in threads:
        t.start()

    interrupted = False
    try:
        # 主线程只做心跳与看门狗：注册 supervisor 身份，供 add --async 判活
        while any(t.is_alive() for t in threads):
            try:
                store.heartbeat(hb_conn, wid)
            except Exception:  # noqa: BLE001  心跳失败不影响消费主流程
                pass
            time.sleep(min(args.poll, 1.0))
    except KeyboardInterrupt:
        interrupted = True
        print("hy-mem: 中断，等待在途任务收尾（再按一次 Ctrl+C 强退）…", file=sys.stderr)
        stop.set()
        try:
            for t in threads:
                t.join(timeout=10)
        except KeyboardInterrupt:
            pass  # 第二次 Ctrl+C：立即强退，在途任务由 requeue_stale 兜底
    if not interrupted:
        for t in threads:
            t.join(timeout=240)  # 挂着的 LLM 调用留足收尾时间

    return _worker_finish(args, lanes, wid, st, interrupted, clients, hb_conn)


def cmd_queue(args) -> int:
    conn = store.connect()
    try:
        recovered = 0
        if args.recover:
            recovered = store.requeue_stale(conn)
        stats = store.queue_stats(conn)
        jobs = store.list_jobs(conn, args.status, args.limit)
    finally:
        conn.close()

    if args.json:
        return ok({"ok": True, "stats": stats, "recovered": recovered, "jobs": jobs}, True)

    head = (
        f"待处理 {stats['pending']} | 运行中 {stats['running']} | 完成 {stats['done']} | 死信 {stats['dead']}"
        f" | 总计 {stats['total']}"
    )
    if stats.get("shadow_only"):
        head += (f"\n其中 {stats['shadow_only']} 个完成任务只落了影子节点（检索不到）："
                 "hy-mem retry --shadow 可重做")
    if stats.get("oldest_pending_age_s"):
        head += f"\n最老待处理已等待 {stats['oldest_pending_age_s']}s"
    if recovered:
        head += f"\n已回收僵死任务 {recovered} 个"
    if not jobs:
        return ok({"stats": stats}, False, head + "\n（无任务）")
    lines = [head, ""]
    for j in jobs:
        body = str((j.get("payload") or {}).get("text") or "")[:60].replace("\n", " ")
        tags = (j.get("payload") or {}).get("tags") or []
        tail = f"  #{','.join(tags)}" if tags else ""
        err = f"  ! {str(j.get('error'))[:50]}" if j.get("error") else ""
        lines.append(f"#{j['id']:<4} {j['status']:<8} try {j['attempts']}/{j['max_attempts']}  {body}{tail}{err}")
    return ok({"stats": stats, "jobs": jobs}, False, "\n".join(lines))


def cmd_retry(args) -> int:
    """把死信/失败任务放回待处理，重置重试计数。"""
    conn = store.connect()
    try:
        if getattr(args, "shadow", False):
            n = store.requeue_shadow(conn)
            stats = store.queue_stats(conn)
            return ok(
                {"ok": True, "requeued": n, "queue": stats}, args.json,
                f"已重投 {n} 个「只落影子节点」的任务，待处理 {stats['pending']} 个",
            )
        if args.job_id == "dead" or args.all_dead:
            cur = conn.execute(
                "UPDATE jobs SET status='pending', attempts=0, error=NULL, started_at=NULL, worker=NULL, "
                "not_before=NULL WHERE status='dead'"
            )
            n = cur.rowcount
        else:
            try:
                jid = int(args.job_id)
            except (TypeError, ValueError):
                return fail("job_id 必须是数字，或使用 dead 重投全部死信", EXIT_USAGE)
            cur = conn.execute(
                "UPDATE jobs SET status='pending', attempts=0, error=NULL, started_at=NULL, worker=NULL, "
                "not_before=NULL WHERE id=? AND status IN ('dead','done')",
                (jid,),
            )
            n = cur.rowcount
            if n == 0:
                return fail(f"job {jid} 不存在或状态不允许重投（只有 dead/done 可重投）", EXIT_USAGE)
        stats = store.queue_stats(conn)
    finally:
        conn.close()
    return ok({"ok": True, "requeued": n, "queue": stats}, args.json, f"已重投 {n} 个任务，待处理 {stats['pending']} 个")


def cmd_drain(args) -> int:
    deadline = time.time() + args.timeout
    conn = store.connect()
    try:
        while True:
            stats = store.queue_stats(conn)
            if stats["pending"] == 0 and stats["running"] == 0:
                return ok({"ok": True, "queue": stats}, args.json, f"队列已清空（完成 {stats['done']}，死信 {stats['dead']}）")
            if time.time() >= deadline:
                return fail(f"等待超时，仍有 {stats['pending']} 个待处理 / {stats['running']} 个运行中", EXIT_RUNTIME)
            time.sleep(args.poll)
    finally:
        conn.close()
