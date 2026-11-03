"""维护与认知层命令：doctor / evolve / evolve-status / stats / log。

S2 拆分（docs/specs/2026-11-02-cli-split-ports/）：本模块从 hymem/cli.py 按域拆入，
逻辑零变化——全局状态存于 cmds.base，跨模块经 _base 属性读写。
"""
from __future__ import annotations

from hymem.cmds.base import (  # noqa: F401
    EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, Path, asyncio,
    config_defaults_user, data_dir, embedding_provider, emit,
    fail, identity, json, make_client, netprobe,
    ok, os, require_ready, store, sys,
    time,
)
from hymem.backends.hymemory.clientops import sdk_info


def cmd_doctor(args) -> int:
    report = {"ok": True, "checks": []}

    def check(name, passed, detail, fatal=True):
        report["checks"].append({"name": name, "pass": bool(passed), "detail": detail})
        if not passed and fatal:
            report["ok"] = False

    # 1. SDK
    _sdk_ok, _sdk_detail = sdk_info()  # vendor import 收敛在 backends（守护断言 G1）
    check("sdk_import", _sdk_ok, _sdk_detail)
    if not _sdk_ok:
        emit(report, args.json, "SDK 导入失败，先 pip install hy-memory")
        return EXIT_USAGE

    # 1.5 运行环境：系统版本 / 位数 / 包完整性 / 安装目录可写 / 运行库 / 磁盘
    #     把「双击闪退」的成因搬进自检，不必等人报障再猜。
    try:
        from hymem.core import runtime_check

        for rc_item in runtime_check.checks(full=True):
            detail = rc_item["detail"]
            if not rc_item["ok"] and rc_item.get("fix"):
                detail += f"（怎么修：{rc_item['fix']}）"
            check(rc_item["item"], rc_item["ok"], detail, fatal=bool(rc_item.get("fatal", True)))
    except Exception as e:  # noqa: BLE001
        check("runtime", False, f"{type(e).__name__}: {e}", fatal=False)

    # 2. 数据目录
    dd = Path(args.data_dir) if args.data_dir else data_dir()
    try:
        from hymem.core import config as _cfgdir

        _src = _cfgdir.data_dir_source()  # env / config / default（PRD F6 的可观测性）
    except Exception:  # noqa: BLE001
        _src = "?"
    try:
        dd.mkdir(parents=True, exist_ok=True)
        probe = dd / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("data_dir", True, f"{dd}（来源 {_src}）")
    except Exception as e:  # noqa: BLE001
        check("data_dir", False, f"{dd}: {e}")

    # 2.5 身份与启动器：agent 不传 -u 时用的是哪个 user_id，命令在不在 PATH 上
    try:
        from hymem.core import config as _cfg

        _uid = _cfg.identity_defaults()
        _src_map = {
            "env": "环境变量 HY_MEM_USER_ID",
            "config": "config.identity.userId",
            "general": "未设置 → 通用",
        }
        _src = _src_map.get(_cfg.identity_source(), "config.identity")
        _scope = "所有智能体共享" if _uid == _cfg.GENERAL_USER_ID else "仅该身份可见（隔离）"
        check(
            "identity",
            bool(_uid),
            f"user_id={_uid}（{_scope}；来自 {_src}，-u 可覆盖，hy-mem user 可切换）",
            fatal=False,
        )
    except Exception as e:  # noqa: BLE001
        check("identity", False, f"解析失败：{type(e).__name__}: {e}", fatal=False)
    try:
        from hymem.core import install_ops

        # 先看 PATH 上到底能不能解析到 hy-mem —— 用户可能把启动器装在别的目录
        # （~/.local/bin 之类），只盯默认落点会误报 FAIL。
        found = install_ops.which()
        if found:
            check("launcher", True, f"PATH 上可用：{found}", fatal=False)
        else:
            _lp = install_ops.launcher_path()
            _on = install_ops.on_path(_lp.parent)
            check(
                "launcher",
                False,
                f"PATH 上找不到 hy-mem（默认落点 {_lp}"
                + ("" if _on else "，其目录也不在 PATH")
                + "；跑 hy-mem install --dir <已在 PATH 的目录>）",
                fatal=False,
            )
    except Exception as e:  # noqa: BLE001
        check("launcher", False, f"{type(e).__name__}: {e}", fatal=False)

    # 3. Embedding（local=进程内 ONNX；remote=OpenAI 兼容端点）
    provider = embedding_provider()
    want_dims = int(os.getenv("MEMORY_EMBEDDING_DIMS", "0") or 0)
    base = os.getenv("MEMORY_EMBEDDER_BASE_URL", "").rstrip("/")
    model = os.getenv("MEMORY_EMBEDDER_MODEL", "")
    key = os.getenv("MEMORY_EMBEDDER_API_KEY", "")
    if provider == "local":
        # 模型没缓存时不要「顺手下载」：doctor 是诊断命令，不该让用户毫无预期
        # 地等 40s、占 92MB。只如实报告，并说清首次使用会发生什么。
        try:
            from hymem.core.local_embedder import _default_cache_dir

            _cache = Path(_default_cache_dir())
            _cached = any(_cache.glob("**/*.onnx"))
        except Exception:  # noqa: BLE001
            _cache, _cached = Path("?"), False
        if not _cached:
            check(
                "embedding_local",
                True,
                f"本地模型尚未缓存（{_cache}）：首次使用会下载约 92MB，已默认走 hf-mirror.com 国内镜像",
                fatal=False,
            )
            want_dims = want_dims or 0
            provider = "local-pending"
    if provider == "local":
        try:
            import fastembed  # noqa: F401

            from hymem.core.local_embedder import LocalEmbeddingService

            svc = LocalEmbeddingService()
            t0 = time.time()
            vec = asyncio.run(svc.embed("健康检查", use_cache=False))
            ms = int((time.time() - t0) * 1000)
            dims = len(vec)
            cached = any(Path(svc._cache_dir).glob("**/*.onnx"))
            good = dims == (want_dims or dims)
            detail = f"{svc._model_name} {dims}维 {ms}ms ({'已缓存' if cached else '首次需下载'})"
            if not good:
                detail += f" 与 MEMORY_EMBEDDING_DIMS={want_dims} 不一致！"
            check("embedding_local", good, detail)
        except Exception as e:  # noqa: BLE001
            check("embedding_local", False, f"本地嵌入失败: {type(e).__name__}: {e}")
    elif not base or not model:
        check("embedding_config", False, "MEMORY_EMBEDDER_BASE_URL / _MODEL 未配置")
    else:
        check("embedding_config", True, f"{model} @ {base}")
        if not key:
            check("embedding_live", False, "MEMORY_EMBEDDER_API_KEY 为空", fatal=False)
        else:
            res = netprobe.post_json(
                f"{base}/embeddings",
                {"model": model, "input": "健康检查"},
                key,
                timeout=30,
                key_label="MEMORY_EMBEDDER_API_KEY",
            )
            if isinstance(res, netprobe.NotSent):
                # 请求没离开本机（密钥/地址含非 ASCII、DNS、连接被拒、超时）。
                # 它和「服务器返回了错误码」是两回事，绝不能写成 HTTP 0。
                check("embedding_live", False, res.reason)
            elif res.status != 200:
                check("embedding_live", False, netprobe.explain(res))
            else:
                ms = int(res.seconds * 1000)
                vec = (res.payload.get("data") or [{}])[0].get("embedding") or []
                dims = len(vec)
                good = dims > 0 and (want_dims == 0 or dims == want_dims)
                detail = f"{dims} 维 / {ms}ms"
                if want_dims and dims != want_dims:
                    detail += f"（与 MEMORY_EMBEDDING_DIMS={want_dims} 不一致！会导致写入失败）"
                check("embedding_live", good, detail)

    # 4. 模式与 LLM（lite 不需要 LLM）
    mode = args.mode or os.getenv("MEMORY_MODE", "pro")
    check("mode", mode in ("lite", "pro", "ultra"), f"MEMORY_MODE={mode}")
    if mode in ("pro", "ultra"):
        lm = os.getenv("MEMORY_LLM_BASE_URL", "").rstrip("/")
        lk = os.getenv("MEMORY_LLM_API_KEY", "")
        lmod = os.getenv("MEMORY_LLM_MODEL", "")
        if not lm or not lk or not lmod:
            check("llm_config", False, f"{mode} 模式必须配 LLM（缺 base_url/key/model）")
        else:
            check("llm_config", True, f"{lmod} @ {lm}")
            res = netprobe.post_json(
                f"{lm}/chat/completions",
                {
                    "model": lmod,
                    "messages": [{"role": "user", "content": "回复ok"}],
                    # 不能给小值。推理模型（deepseek-v4 / kimi-k2.5 / claude 系）
                    # 会先把输出配额花在 thinking 上，8 个 token 连思考都写不完，
                    # 于是 content 永远是空串。这个坑曾让 doctor 对一个完全可用
                    # 的模型报「环境不可用」，而真实抽取流程（agent_max_tokens
                    # 默认 2000）一直是好的 —— 纯粹是探针自身太抠。
                    "max_tokens": 512,
                },
                lk,
                timeout=45,
                key_label="MEMORY_LLM_API_KEY",
            )
            if isinstance(res, netprobe.NotSent):
                check("llm_live", False, res.reason)
            elif res.status != 200:
                check("llm_live", False, netprobe.explain(res))
            else:
                ms = int(res.seconds * 1000)
                _ch = (res.payload.get("choices") or [{}])[0]
                _msg = _ch.get("message") or {}
                reply = str(_msg.get("content") or "").strip()
                think = str(_msg.get("reasoning_content") or "").strip()
                fin = str(_ch.get("finish_reason") or "")
                if reply:
                    check("llm_live", True, f"{ms}ms reply={reply[:20]!r}")
                elif think:
                    # 推理模型：content 为空、思维链有内容。鉴权、路由、连通全都正常，
                    # 这是这类模型的正常形态，不能算失败。
                    _mt = os.getenv("MEMORY_AGENT_MAX_TOKENS", "2000")
                    check("llm_live", True, f"{ms}ms 推理模型（思考 {len(think)} 字后作答，抽取配额 {_mt}）")
                else:
                    check(
                        "llm_live",
                        False,
                        f"{ms}ms 空响应 finish_reason={fin!r}：端点通但模型没给内容",
                    )

    # 5. 异步队列与写锁（worker 依赖这两个，坏了会静默丢任务）
    try:
        qconn = store.connect()
        try:
            stats = store.queue_stats(qconn)
            check(
                "queue_db",
                True,
                f"{store.db_path()} | 待处理 {stats['pending']} / 死信 {stats['dead']}",
            )
        finally:
            qconn.close()
    except Exception as e:  # noqa: BLE001
        check("queue_db", False, f"队列库不可用: {type(e).__name__}: {e}")

    try:
        t0 = time.time()
        with store.WriteLock(timeout=5) as lk:
            held = lk.acquired
        ms = int((time.time() - t0) * 1000)
        check("write_lock", held, f"{ms}ms" if held else "5s 内未拿到写锁（有 worker 正在写？）", fatal=False)
    except Exception as e:  # noqa: BLE001
        check("write_lock", False, f"{type(e).__name__}: {e}", fatal=False)

    human = "\n".join(
        f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['name']}: {c['detail']}"
        for c in report["checks"]
    )
    from hymem.ui import i18n
    from hymem.core import errors as _ec
    _ec.register_tr(i18n.tr)  # S1 分层：错误文案翻译经钩子注入

    human = (i18n.tr("环境可用") if report["ok"] else i18n.tr("环境不可用")) + "\n" + human
    emit(report, args.json, human)
    return EXIT_OK if report["ok"] else EXIT_USAGE


def cmd_evolve(args) -> int:
    """L6 认知层：把全量事实归纳成行为模式（System 2，ultra 专属）。

    与 add 的关系：add 只跑 S1（抽事实/判重），S2 不自动触发。典型用法是
    「批量 add → evolve 一次」，evolve 每次扫的还是当前全量 VDB，因此非幂等、
    连续跑有意义，但不是「跑一次就一直有效」。

    为什么先拦 mode：SDK 在非 ultra 下抛的是英文 RuntimeError
    （client.py:2118），直接冒到宿主智能体那里既看不懂也找不到出路。
    这里提前拦，并把「怎么切」写在提示里。
    """
    rc = require_ready("运行 L6 认知归纳")
    if rc is not None:
        return rc
    from hymem.core import config as cfgmod
    from hymem.core import evolveops

    cfg = cfgmod.load()
    mode = str(cfg.get("mode") or "pro")
    if mode != "ultra":
        return fail(
            f"evolve 需要 mode=ultra，当前 mode={mode}。L6 认知层是 ultra 独占："
            "归纳出的 schema 要落 Kuzu 图库，pro/lite 里没有这条存储，"
            "检索侧也没有 L6 通道。",
            EXIT_USAGE,
            hint=(
                "切换：hy-mem config set mode=ultra"
                "（首次会在数据目录建 graph 库，启动约 +4s；切回 pro 用 config set mode=pro）"
            ),
        )

    uid = identity(args).get("user_id") or config_defaults_user()
    model = getattr(args, "model", "") or ""
    batches = getattr(args, "batches", None)
    if not getattr(args, "json", False):
        eff = model or (cfg.get("evolve") or {}).get("model") or (cfg.get("llm") or {}).get("model")
        print(
            f"hy-mem: 正在对 {uid} 的全量记忆做 schema 归纳（模型 {eff}），可能耗时数十秒…",
            file=sys.stderr,
        )

    res = evolveops.evolve(
        model_override=model, max_batches=batches, user_id=uid,
        language=getattr(args, "language", "") or "",
    )

    if not res.get("ok"):
        return fail(
            str(res.get("error") or "evolve 失败"),
            EXIT_RUNTIME,
            hint=res.get("error_hint") or "排查：hy-mem config validate；确认 mode=ultra 且图库未被占用",
        )

    if args.json:
        return ok(res, True)

    lines = []
    if res.get("idle"):
        lines.append(f"[!] {res.get('idle_note')}")
        lines.append(res.get("idle_hint") or "")
    planned = res.get("batches_planned")
    done = res.get("batches_completed")
    lines.append(
        f"批次  {done} 批完成" + (f"（计划 {planned}）" if planned is not None else "")
        + (f"，失败 {res.get('batches_failed')}" if res.get("batches_failed") else "")
    )
    # 「处理 N 个簇」在 0 批时是误导——实测 tasks_processed=1 而 batches_planned=0、
    # continuation_reason=no_clusters，一个簇都没成却显示「处理 1 个簇」。
    reason = str(res.get("continuation_reason") or "")
    fresh = res.get("fresh_facts_remaining")
    if done:
        lines.append(f"归纳  处理 {res.get('tasks_processed') or 0} 个簇")
    else:
        why = {
            "no_clusters": "未成簇（素材太散或已被判重合并）",
            "no_facts": "无新事实",
            "no_fresh_facts": "新事实已消费完",
        }.get(reason, "未开批")
        tail = f"，剩 {fresh} 条未消费" if fresh else ""
        lines.append(f"归纳  {why}　{reason or 'unknown'}{tail}")
    if res.get("schemas_added") is not None:
        sign = "+" if res["schemas_added"] >= 0 else ""
        lines.append(f"schema  {sign}{res['schemas_added']} 条（现有 {res.get('schemas_after')} 条）")
    lines.append(
        f"tokens  输入 {res.get('prompt_tokens')} / 输出 {res.get('completion_tokens')}"
        f"　耗时 {round((res.get('elapsed_ms') or 0) / 1000, 1)}s"
    )
    if str(res.get("language") or "") not in ("", "(auto)"):
        lines.append(f"语言  {res['language']}（SDK 原生输出语言约束）")
    if res.get("note"):
        lines.append(f"提示  {res['note']}")
    if res.get("language_note"):
        lines.append(f"提示  {res['language_note']}")
    return ok(res, False, "\n".join(x for x in lines if x))


def cmd_evolve_status(args) -> int:
    """查看 L6 自动沉淀状态与历史（观察窗口，所有模式可看）。

    为什么不拦 pro：沉淀只在 ultra 跑，但异常（连续失败/冷却卡死）的发现
    不该要求先切模式——这里任何模式都回报状态，非 ultra 时注明「未启用」。
    数据源与 MCP evolve_status 同一份（autoevops.status_summary）。
    """
    rc = require_ready("查看 L6 沉淀状态")
    if rc is not None:
        return rc
    from hymem.core import autoevops

    try:
        limit = max(1, min(int(getattr(args, "limit", None) or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    payload = autoevops.status_summary(limit=limit)
    if args.json:
        return ok(payload, True)

    auto = payload.get("auto") or {}
    lines = [
        f"模式  {payload.get('mode')}" + ("（L6 自动沉淀仅 ultra 启用）" if payload.get("mode") != "ultra" else ""),
        f"阈值  新增事实 ≥ {auto.get('minNewFacts')} 条触发，冷却 {auto.get('cooldownHours'):g}h × 2^连续失败（7 天封顶）",
        f"状态  连续失败 {payload.get('consecutive_failures')} 次，基线事实 {payload.get('last_total_facts')} 条",
    ]
    if payload.get("last_success"):
        lines.append(f"最近成功  {payload['last_success']}")
    if payload.get("next_attempt_after"):
        tail = "（现在可尝试）" if payload.get("can_attempt_now") else "（冷却中）"
        lines.append(f"下次可尝试  {payload['next_attempt_after']}　{tail}")
    recs = payload.get("records") or []
    lines.append(f"最近记录  {len(recs)} 条")
    if not recs:
        lines.append("  （还没有记录：素材攒够且冷却过后，add 时会自动触发）")
    for r in recs[-10:]:
        extra = {k: v for k, v in r.items() if k not in ("ts", "status")}
        lines.append(f"  {r.get('ts')}  {r.get('status')}" + (f"  {extra}" if extra else ""))
    return ok(payload, False, "\n".join(lines))


def _dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _scan_all_memories(client, ident: dict, cap: int = 5000, page: int = 100) -> list[dict]:
    """分页扫全量抽取层记忆。cap 是防御上限，避免库异常大时把 CLI 拖死。"""
    items: list[dict] = []
    offset = 0
    while offset < cap:
        r = client.list_memories(limit=page, offset=offset, **ident)
        batch = (r.get("vdb") or {}).get("memories") or []
        if not batch:
            break
        items.extend([b for b in batch if isinstance(b, dict)])
        offset += len(batch)
        if len(batch) < page:
            break
    return items


_LAT_A = -10.9


_LAT_B = 1.95


def _estimate_write_latency(n: int) -> float:
    return max(2.5, _LAT_A + _LAT_B * max(n, 1))


def _health_thresholds() -> dict:
    def _f(name: str, dflt: float) -> float:
        try:
            return float(os.getenv(name) or dflt)
        except ValueError:
            return dflt

    return {
        "count_warn": _f("HY_MEM_HEALTH_COUNT_WARN", 200),
        "count_crit": _f("HY_MEM_HEALTH_COUNT_CRIT", 500),
        "lat_warn": _f("HY_MEM_HEALTH_LAT_WARN", 30),
        "lat_crit": _f("HY_MEM_HEALTH_LAT_CRIT", 60),
        "size_crit_mb": _f("HY_MEM_HEALTH_SIZE_CRIT_MB", 2048),
    }


def _status_icon(level: str) -> str:
    return {"OK": "[OK]  ", "WARN": "[WARN]", "CRIT": "[CRIT]"}.get(level, "[ -- ]")


def cmd_stats(args) -> int:
    if not args.health:
        client = make_client(args.mode)
        try:
            r = client.get_metrics(minutes=args.minutes)
            return ok(r, args.json, json.dumps(r, ensure_ascii=False, indent=2, default=str))
        finally:
            client.close()
    return _stats_health(args)


def _stats_health(args) -> int:
    """库体积健康度：条数 / 体积 / 队列 / 写入延迟预期 / 归档建议。

    立论：pro 写入的 reconcile 是 O(存量) —— 写一条新事实会把一批旧节点标记
    update 并重写。所以「库有多大」直接决定「写一条要多久」，这是必须能
    一条命令看出来的运营指标，而不是等写入慢到不可用才发现。
    """
    th = _health_thresholds()
    rc = require_ready("健康检查（要真实读写一次以测延迟）")
    if rc is not None:
        return rc
    checks: list[dict] = []
    notes: list[str] = []

    # ---- 体积 ----
    dd = data_dir()
    qdb = dd / "hy_mem_queue.db"
    inner = dd / "data"
    total_bytes = _dir_size(dd)
    inner_bytes = _dir_size(inner) if inner.exists() else 0
    q_bytes = qdb.stat().st_size if qdb.exists() else 0
    size_mb = total_bytes / 1048576
    size_level = "CRIT" if size_mb >= th["size_crit_mb"] else ("WARN" if size_mb >= th["size_crit_mb"] / 2 else "OK")
    checks.append(
        {
            "name": "数据体积",
            "value": f"{size_mb:.1f} MB",
            "level": size_level,
            "detail": f"记忆库 {inner_bytes/1048576:.1f}M / 队列库 {q_bytes/1024:.0f}K",
        }
    )

    # ---- 条数 + 层分布 ----
    items: list[dict] = []
    layer_count: dict[str, int] = {}
    client = make_client(args.mode)
    try:
        ident = {}
        if args.user:
            ident["user_id"] = args.user
        items = _scan_all_memories(client, ident, cap=args.max_scan)
        for it in items:
            layer = str(it.get("layer") or "unknown")
            layer_count[layer] = layer_count.get(layer, 0) + 1
    finally:
        client.close()

    n = len(items)
    if n >= th["count_crit"]:
        cnt_level = "CRIT"
    elif n >= th["count_warn"]:
        cnt_level = "WARN"
    else:
        cnt_level = "OK"
    checks.append(
        {
            "name": "记忆条数",
            "value": str(n),
            "level": cnt_level,
            "detail": " / ".join(f"{k} {v}" for k, v in sorted(layer_count.items(), key=lambda x: -x[1]))
            or "（空库）",
        }
    )

    # ---- 写入延迟预期 ----
    est = _estimate_write_latency(n)
    if est >= th["lat_crit"]:
        lat_level = "CRIT"
    elif est >= th["lat_warn"]:
        lat_level = "WARN"
    else:
        lat_level = "OK"
    checks.append(
        {
            "name": "单条写入预期",
            "value": f"{est:.0f}s",
            "level": lat_level,
            "detail": f"pro 模式同步 add；上限 {th['lat_crit']:.0f}s，超过必须改用 --async",
        }
    )

    # ---- 队列 ----
    conn = store.connect()
    try:
        qs = store.queue_stats(conn)
        inv = store.tag_inventory(conn)
        stale = sum(1 for _ in conn.execute("SELECT 1 FROM jobs WHERE status='done'").fetchall())
    finally:
        conn.close()
    q_level = "CRIT" if qs["dead"] else ("WARN" if qs["pending"] > 50 or qs["running"] else "OK")
    checks.append(
        {
            "name": "异步队列",
            "value": f"待 {qs['pending']} / 死信 {qs['dead']}",
            "level": q_level,
            "detail": f"已完成 {qs['done']} 条" + (f"，最老待处理 {qs['oldest_pending_age_s']:.0f}s" if qs["oldest_pending_age_s"] else ""),
        }
    )

    # ---- 建议 ----
    if n >= th["count_warn"]:
        notes.append(
            f"条数已达 {n}（阈值 {th['count_warn']:.0f}），reconcile 开销随之线性上升。"
            f"建议执行 `hy-mem archive --by tag` 盘点，再对低频标签用 `untag`/`delete` 收缩。"
        )
    if est >= th["lat_warn"]:
        notes.append(
            f"单条同步写入预期 {est:.0f}s，已超过交互可接受范围。批量写入一律走 "
            f"`add --async` + `worker`，或临时 `--mode lite`。"
        )
    if qs["dead"]:
        notes.append(f"存在 {qs['dead']} 条死信任务，跑 `hy-mem queue --status dead` 查原因，`hy-mem retry dead` 重投。")
    if stale > 5000:
        notes.append(f"jobs 表已积累 {stale} 条已完成记录，可执行 `hy-mem stats --health --vacuum` 清理。")

    # 低频标签：只报「有用户手工标签但仅 1 条」的，作为归档候选
    thin = [t for t in inv if t["n"] == 1 and t["user_n"] >= 1]
    if thin:
        notes.append(
            "单条标签（归档候选）：" + "、".join(f"{t['tag']}" for t in thin[:10])
            + ("…" if len(thin) > 10 else "")
        )

    # 疑似测试残留
    junk_kw = ("探针", "并发测试", "测试残留", "TODO", "临时")
    junk = [it for it in items if any(k in str(it.get("content") or "") for k in junk_kw)]
    if junk:
        notes.append(f"疑似测试残留 {len(junk)} 条，可用 `hy-mem list` 核对后 `hy-mem delete <id>` 清理。")

    if not notes:
        notes.append("各项指标正常，无需干预。")

    worst = "CRIT" if any(c["level"] == "CRIT" for c in checks) else ("WARN" if any(c["level"] == "WARN" for c in checks) else "OK")
    payload = {
        "ok": True,
        "verdict": worst,
        "memory_count": n,
        "layers": layer_count,
        "size_mb": round(size_mb, 2),
        "estimated_write_latency_s": round(est, 1),
        "queue": qs,
        "top_tags": [{"tag": t["tag"], "n": t["n"]} for t in inv[:15]],
        "checks": checks,
        "advice": notes,
        "thresholds": th,
    }

    if args.vacuum:
        conn = store.connect()
        try:
            # P1-4：只清超过保留期的 done；dead（失败）永久保留，留给排障。
            purged = store.purge_finished_jobs(conn)
            conn.execute("VACUUM")
            payload["vacuumed"] = True
            payload["purged_jobs"] = purged
        finally:
            conn.close()

    if args.json:
        return ok(payload, True)

    lines = [f"hy-mem 健康检查  结论 {worst}", ""]
    w = max(len(c["name"]) for c in checks) + 4
    for c in checks:
        lines.append(f"  {_status_icon(c['level'])} {c['name']:<{w}}{c['value']:<16}{c['detail']}")
    lines.append("")
    lines.append("建议：")
    for i, t in enumerate(notes, 1):
        lines.append(f"  {i}. {t}")
    if inv:
        lines.append("")
        lines.append("标签分布（前 10）：")
        for t in inv[:10]:
            lines.append(f"  {t['tag']:<24}{t['n']:>5}")
    return ok(payload, False, "\n".join(lines))


def cmd_log(args) -> int:
    """最近的操作日志（默认 50 条）。

    记录点在 main() 里统一做（见 _record_operation），这里只负责读和清。
    不做 --follow：日志是用来事后回看「刚才那条命令到底跑了什么」的，
    不是监控流；多一个长驻模式只会多一处要维护的退出路径。
    """
    from hymem.ui import i18n
    from hymem.core import errors as _ec
    _ec.register_tr(i18n.tr)  # S1 分层：错误文案翻译经钩子注入
    from hymem.core import oplog

    try:
        p = oplog.path()
    except Exception as e:  # noqa: BLE001
        return fail(i18n.tr("读取失败：{}", f"{type(e).__name__}: {e}"), EXIT_RUNTIME)

    if args.clear:
        try:
            oplog.clear()
        except Exception as e:  # noqa: BLE001
            return fail(i18n.tr("清空失败：{}", f"{type(e).__name__}: {e}"), EXIT_RUNTIME)
        return ok(
            {"ok": True, "path": str(p), "cleared": True},
            args.json,
            f"{i18n.tr('日志已清空')}  {p}",
        )

    lines = oplog.recent(args.limit)
    entries = [oplog.parse(ln) for ln in lines]
    payload = {
        "ok": True,
        "path": str(p),
        "keep": oplog.MAX_KEEP,
        "count": len(entries),
        "entries": entries,
    }
    if not lines:
        return ok(payload, args.json, f"{i18n.tr('（还没有操作记录）')}\n  {p}")
    head = f"{i18n.tr('操作日志')}  {len(entries)}/{oplog.MAX_KEEP}  {p}"
    return ok(payload, args.json, "\n".join([head, ""] + lines))


# =========================================================== S4/S5/S6（docs/specs/2026-11-02-sdk-alignment-gaps/）
# SDK 2.0.11 对齐补全：reembed 工具族 / backup-restore / graph-build。
# 全部只留 CLI（迁移级/破坏性/长耗时重操作不进 MCP，判定见契约）。

def _require_yes(args, what: str) -> int | None:
    if not getattr(args, "yes", False):
        return fail(f"{what}是破坏性/迁移级操作，必须加 --yes 确认", EXIT_USAGE,
                    hint="先跑只读命令看影响范围，确认后再加 --yes")
    return None


def _quiesce_or_fail(action: str) -> int | None:
    """备份/回滚前探测静默：短暂抢写锁，抢不到说明有写入进程在跑。"""
    lk = store.WriteLock(timeout=1.0, name="backup.lock")
    with lk:
        if not lk.acquired:
            return fail(
                f"{action}要求数据面静默（writers_stopped），但写锁被别的进程持有",
                hint="先停 worker / serve / 其它 hy-mem 进程，再重试",
            )
    return None


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"


def cmd_vdb_scan(args) -> int:
    """扫描向量空间（只读）：每个空间的 role/条数/宽度/模型。"""
    from hymem.backends.hymemory import reembedops

    try:
        spaces = reembedops.scan_spaces(getattr(args, "root", None), per_user=not args.whole)
    except Exception as e:  # noqa: BLE001
        return fail(f"扫描失败：{type(e).__name__}: {e}")
    payload = {"root": str(reembedops._persist_root(getattr(args, "root", None))), "count": len(spaces), "spaces": spaces}
    lines = [f"向量空间扫描：{payload['root']}（{len(spaces)} 个）", ""]
    for s in spaces:
        name = s.get("name") or s.get("base") or "?"
        width = s.get("width") or s.get("dims") or "?"
        cnt = s.get("count") or s.get("records") or "?"
        lines.append(f"  - {name}  宽度={width}  记录={cnt}")
    return ok(payload, args.json, "\n".join(lines) if spaces else "没有找到任何向量空间")


def cmd_vdb_plan(args) -> int:
    """reembed 差量计划（只读）：每个 role 要算多少条向量。"""
    from hymem.backends.hymemory import reembedops

    try:
        plan = reembedops.plan_reembed(args.source, args.target,
                                       getattr(args, "graph_source", None))
    except Exception as e:  # noqa: BLE001
        return fail(f"计划失败：{type(e).__name__}: {e}")
    return ok(plan, args.json, f"计划生成（源 {args.source} → 目标 {args.target}），JSON 里有逐 role 明细")


def cmd_vdb_reembed(args) -> int:
    """全量重嵌入：source → target（skip_existing 断点续跑）。"""
    rc = _require_yes(args, "重嵌入（会向 target 写全量向量）")
    if rc is not None:
        return rc
    from hymem.backends.hymemory import reembedops

    roles = [r.strip() for r in (args.roles or "").split(",") if r.strip()] or None
    try:
        report = reembedops.run_reembed(
            args.source, args.target, batch_size=args.batch_size,
            max_inflight=args.max_inflight, roles=roles,
        )
    except Exception as e:  # noqa: BLE001
        return fail(f"重嵌入失败：{type(e).__name__}: {e}",
                    hint="先跑 hy-mem vdb-plan 看差量；skip_existing 默认开，中断后重跑即续传")
    return ok(report, args.json, f"重嵌入完成（源 {args.source} → 目标 {args.target}）")


def cmd_vdb_reembed_graph(args) -> int:
    """Kuzu 图向量重建：源图 → 目标图（新维度）。"""
    rc = _require_yes(args, "图向量重建（会写目标图路径）")
    if rc is not None:
        return rc
    from hymem.backends.hymemory import reembedops

    try:
        report = reembedops.run_reembed_graph(args.source, args.target, args.dims,
                                              batch_size=args.batch_size)
    except Exception as e:  # noqa: BLE001
        return fail(f"图重建失败：{type(e).__name__}: {e}")
    return ok(report, args.json, f"图向量重建完成（{args.source} → {args.target}，dims={args.dims}）")


def cmd_vdb_drop(args) -> int:
    """删除向量空间（破坏性）。"""
    rc = _require_yes(args, "删除向量空间")
    if rc is not None:
        return rc
    from hymem.backends.hymemory import reembedops

    roles = [r.strip() for r in (args.roles or "").split(",") if r.strip()] or None
    try:
        dropped = reembedops.drop_space(args.path, roles=roles)
    except Exception as e:  # noqa: BLE001
        return fail(f"删除失败：{type(e).__name__}: {e}")
    return ok({"dropped": dropped, "count": len(dropped)}, args.json,
              f"已删除 {len(dropped)} 个 collection：{', '.join(dropped) or '（无）'}")


def cmd_backup(args) -> int:
    """数据面快照（SDK snapshot 契约：静默 + 校验清单）。"""
    rc = _quiesce_or_fail("备份")
    if rc is not None:
        return rc
    from hymem.backends.hymemory import reembedops

    src = data_dir()
    dst = Path(str(args.to)).expanduser()
    if dst.resolve() == src.resolve():
        return fail("备份目标不能是数据目录本身", EXIT_USAGE)
    try:
        out = reembedops.snapshot_data_plane(src, dst)
    except Exception as e:  # noqa: BLE001
        return fail(f"备份失败：{type(e).__name__}: {e}")
    return ok(out, args.json, f"备份完成：{src} → {dst}")


def cmd_restore(args) -> int:
    """从快照恢复数据面（SDK 自带回滚：protective_root 保底）。"""
    rc = _require_yes(args, "恢复（会覆盖当前数据面）")
    if rc is not None:
        return rc
    rc = _quiesce_or_fail("恢复")
    if rc is not None:
        return rc
    from hymem.backends.hymemory import reembedops

    target = Path(str(args.target).expanduser()) if args.target else data_dir()
    snap_root = Path(str(args.snapshot)).expanduser()
    try:
        res = reembedops.restore_data_plane(snap_root, target)
    except Exception as e:  # noqa: BLE001
        return fail(f"恢复失败：{type(e).__name__}: {e}",
                    hint="SDK 恢复自带回滚清单，看它的输出定位 protective 备份目录")
    return ok(res, args.json, f"恢复完成：{snap_root} → {target}")


def cmd_graph_build(args) -> int:
    """对存量记忆回填关系图（可选附带实体库）。真模型操作。"""
    rc = require_ready("构建关系图")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    uid = identity(args).get("user_id") or config_defaults_user()
    try:
        r = client.build_memory_graph(
            user_id=uid, limit=args.limit, clear_first=args.clear_first)
        if args.entities:
            r["entity_store"] = client.build_entity_store(
                user_id=uid, rebuild=args.rebuild_entities)
    except Exception as e:  # noqa: BLE001
        return fail(f"构建失败：{type(e).__name__}: {e}",
                    hint="这是真模型批量操作（逐条抽关系），确认 LLM 端点与密钥可用")
    finally:
        client.close()
    return ok(r, args.json, f"关系图构建完成（user={uid}，产出门面见 JSON；实体库={'已重建' if args.entities else '未动'}）")
