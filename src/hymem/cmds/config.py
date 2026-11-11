"""配置与安装命令：config / user / migrate / install / setup / menu / version / skill。

S2 拆分（docs/specs/2026-11-02-cli-split-ports/）：本模块从 hymem/cli.py 按域拆入，
逻辑零变化——全局状态存于 cmds.base，跨模块经 _base 属性读写。
"""
from __future__ import annotations

from hymem.cmds.base import (  # noqa: F401
    EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, Path, fail,
    json, ok, os, store, sys,
)


def cmd_install(args) -> int:
    from hymem.core import install_ops

    if getattr(args, "print_only", False):
        body = install_ops.render_launcher()
        where = install_ops.launcher_path()
        # print() 在 Windows 上会把 \n 再翻成 \r\n，而 body 里已经是 \r\n，
        # 直接打印会得到 \r\r\n —— cmd 里表现为多出空行，用先归一再打。
        human = (
            f"将写入：{where}\n"
            f"实际执行：{' '.join(install_ops.target_command())}\n"
            f"在 PATH 上：{'是' if install_ops.on_path(where.parent) else '否 —— 见下方提示'}\n\n"
            "启动器内容：\n" + body.replace("\r\n", "\n").rstrip()
        )
        return ok({"launcher": body, "path": str(where), "target": install_ops.target_command()}, args.json, human)

    directory = Path(args.dir).expanduser() if args.dir else None
    good, msg, paths = install_ops.install(directory)
    path = paths[0] if paths else install_ops.launcher_path(directory)
    in_path = install_ops.on_path(path.parent)
    payload = {
        "ok": good,
        "launcher": [str(p) for p in paths],
        "dir": str(path.parent),
        "onPath": in_path,
        "target": install_ops.target_command(),
        "pathHint": None if in_path else install_ops.path_hint(path.parent),
    }
    if not good:
        return fail(f"写入启动器失败：{msg}", EXIT_RUNTIME)
    lines = [
        f"目录    {path.parent}",
        f"入口    {' / '.join(p.name for p in paths)}",
        f"指向    {' '.join(install_ops.target_command())}",
        "",
    ]
    if in_path:
        lines.append("PATH 已包含该目录，现在可以直接运行 hy-mem")
    else:
        lines.append("PATH 里还没有这个目录，加一下（然后把下面这条命令写进 SKILL.md 也行）：")
        lines.append("  " + str(install_ops.path_hint(path.parent)))
    # 先接管旧安装的配置与数据，再回收旧启动器 —— 顺序不能反：
    # adopt_stale 把启动器改指当前安装后，旧安装就从 PATH 上「消失」了，
    # 之后就再也找不到它的 config.json / data 在哪。
    adopted_install, install_notes = install_ops.adopt_previous_install()
    # 回收 PATH 上残留的旧 hy-mem 启动器（比如源码模式留在 ~/.local/bin 的壳），
    # 否则它们排在 PATH 前面时会把调用引到失效入口——见 install_ops.adopt_stale 注释。
    adopted, adopt_notes = install_ops.adopt_stale()
    payload["adoptedInstall"] = adopted_install
    payload["adoptNotes"] = install_notes + adopt_notes
    if adopted_install:
        lines.append("")
        lines.append("已沿用旧安装的配置与记忆数据：")
        lines.extend("  " + n for n in install_notes)
    if adopted:
        lines.append("")
        lines.append(f"接管了 {len(adopted)} 个旧启动器：")
        lines.extend("  " + n for n in adopt_notes)
    elif adopt_notes:
        lines.append("")
        lines.extend("  " + n for n in adopt_notes)
    return ok(payload, args.json, "\n".join(lines))


def cmd_version(args) -> int:
    """版本与安装信息。自动升级就靠它：比对版本 + 定位要替换的安装根。"""
    from hymem.core import config as cfgmod
    from hymem.ui import helptext
    from hymem.ui import i18n
    from hymem.core import errors as _ec
    _ec.register_tr(i18n.tr)  # S1 分层：错误文案翻译经钩子注入
    from hymem.core import install_ops

    frozen = bool(getattr(sys, "frozen", False))
    root = install_ops._current_root()
    cfg_env = os.getenv("HY_MEM_CONFIG", "").strip()
    cfg_path = Path(cfg_env) if cfg_env else root / "config.json"
    configured = cfg_path.is_file()
    data_dir = ""
    if configured:
        try:
            cfg = cfgmod.load()
            data_dir = str((cfg.get("storage") or {}).get("dataDir") or root / "data")
        except Exception:  # noqa: BLE001
            configured = False
    payload = {
        "ok": True,
        "version": helptext.VERSION,
        "mode": "exe" if frozen else "source",
        "installRoot": str(root),
        "configPath": str(cfg_path),
        "configured": configured,
        "dataDir": data_dir or str(root / "data"),
    }
    human = (
        i18n.tr("hy-mem {}（{}模式）", payload["version"], i18n.tr("exe") if frozen else i18n.tr("源码"))
        + "\n  " + i18n.tr("安装根 {}", payload["installRoot"])
        + "\n  " + i18n.tr("数据目录 {}", payload["dataDir"])
    )
    return ok(payload, args.json, human)


def cmd_migrate(args) -> int:
    """把旧安装的数据目录迁入当前安装根（升级接管后的收尾）。

    为什么是复制不是搬：原目录保留作备份，用户确认无误后自己删——
    数据安全的事，宁可多占一份磁盘，不做不可逆动作。
    """
    import shutil

    from hymem.core import config as cfgmod
    from hymem.core import install_ops

    cur = install_ops._current_root()
    target = cur / "data"
    src = store.data_dir().resolve()  # 引导已重放 env，这里就是当前生效的数据目录

    def fail(msg: str) -> int:
        return ok({"ok": False, "error": msg}, args.json, msg)

    if not (cur / "config.json").is_file():
        return fail("当前安装还没有 config.json（尚未配置/接管），无处可迁")
    if src == target.resolve():
        return ok({"ok": True, "migrated": False, "dataDir": str(src)},
                  args.json, f"数据目录已在当前安装内，无需迁移：{src}")
    if not src.is_dir():
        return fail(f"数据目录不存在：{src}")
    if target.exists() and any(target.iterdir()):
        return fail(f"目标目录非空，拒绝覆盖：{target}")
    # worker 活着时不能迁：它还握着旧目录的写权，迁完新旧两处都会写，数据分家
    conn = store.connect()
    try:
        hb = store.worker_age(conn)
    finally:
        conn.close()
    if hb is not None and hb < 15.0:
        return fail(
            f"后台 worker 正在运行（心跳 {hb:.0f}s 前），等它空闲自退后再迁移"
            "（队列清空后最多 600s 自退出，或直接结束该 python 进程）"
        )
    with store.WriteLock():  # 复制期间挡住一切写入，保证 SQLite 拷贝一致
        # write.lock 排除：它正被本进程字节锁锁着，Windows 上连自己都读不了
        # （WinError 33），而且它只是锁的载体，不是数据，不迁移。
        shutil.copytree(src, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("write.lock"))
    cfg = cfgmod.load()
    storage = cfg.get("storage") or {}
    storage["dataDir"] = str(target)
    cfg["storage"] = storage
    cfg_env = os.getenv("HY_MEM_CONFIG", "").strip()
    cfg_path = Path(cfg_env) if cfg_env else cur / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    cfgmod.apply_env(cfg)  # 本进程后续操作立即走新目录
    nbytes = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    human = (
        f"已迁移 {nbytes / 1e6:.1f} MB → {target}\n"
        f"  原目录保留作备份，确认无误后可删除：{src}"
    )
    return ok(
        {"ok": True, "migrated": True, "from": str(src), "to": str(target), "bytes": nbytes},
        args.json,
        human,
    )


def cmd_skill(args) -> int:
    """管 SKILL.md：定位 / 一致性 / 分发 / 打印。

    智能体「能用上」这个 CLI 需要两件事同时成立 —— 命令在 PATH 上、宿主能读到
    SKILL.md。install 解决前者，这里解决后者；两者都过一遍再宣称「接好了」。
    """
    from hymem.ui import skill_ops

    if getattr(args, "print_only", False):
        src = skill_ops.skill_path()
        if src is None:
            return fail("找不到 SKILL.md（产物里应有 skill/hy-mem/SKILL.md）")
        body = src.read_text(encoding="utf-8")
        return ok({"path": str(src), "content": body}, args.json, body)

    if getattr(args, "install", False) or getattr(args, "dir", None):
        if args.dir:
            okk, msg = skill_ops.install_to(Path(args.dir).expanduser() / skill_ops.NAME)
            return (ok({"ok": okk, "message": msg}, args.json, msg) if okk
                    else fail(msg, EXIT_RUNTIME))
        results = skill_ops.install_all()
        if not results:
            return fail("没有检测到任何宿主的 skill 目录（可用 --dir 指定）", EXIT_USAGE)
        good = all(r[1] for r in results)
        lines = [f"{'OK  ' if r[1] else 'FAIL'}  {r[0]:<16}{r[2]}" for r in results]
        payload = {"ok": good, "results": [{"host": h, "ok": o, "message": m} for h, o, m in results]}
        return ok(payload, args.json, "\n".join(lines)) if good else fail("\n".join(lines), EXIT_RUNTIME)

    st = skill_ops.status()
    if args.json:
        return ok(st, True)
    if not st["source"]:
        return fail("找不到 SKILL.md：产物里应带 skill/hy-mem/SKILL.md", EXIT_RUNTIME)
    lines = [f"SKILL.md  {st['source']}", f"体积      {st['bytes']} 字节", ""]
    if not st["hosts"]:
        lines.append("没有检测到宿主的 skill 目录")
    for h in st["hosts"]:
        mark = "已同步" if h["inSync"] else ("有旧版" if h["exists"] else "未安装")
        lines.append(f"  {h['host']:<16}{mark:<8}{h['dir']}")
    lines.append("")
    lines.append("同步到所有宿主：hy-mem skill --install")
    lines.append("打印全文：      hy-mem skill --print")
    return ok(st, False, "\n".join(lines))


def cmd_tui_test(args) -> int:
    from hymem.ui import tui

    tui.enable_ansi()
    return tui.keytest()


def cmd_setup(args) -> int:
    from hymem.ui import setup_wizard

    scope = getattr(args, "step", "all") or "all"
    # setup 子命令在「尚未完成设置」时等价首次向导（2026-10-29：菜单化改造后
    # 若只看 scope 会直接进设置菜单，把首次安装的四步依赖顺序跳掉）
    if scope == "all":
        need, _why = setup_wizard.needs_setup()
        return setup_wizard.run_setup(scope="all", first_run=need)
    return setup_wizard.run_setup(scope=scope)


def cmd_menu(args) -> int:
    from hymem.ui import menu

    # 子进程派发（0.9.10）：交互进程只画界面，不承载任何后台角色。
    # 0.9.9 用 `lambda argv: main(argv)` 在本进程内跑 worker，worker 起的
    # chroma server 端口被写回本进程 env，退出后 env 指向死端口 → 整个会话
    # 全命令报 "Could not connect to a Chroma server"（PRD F1）。
    return menu.run_menu(menu.subprocess_dispatch)


def _check_chain_repo(path: str) -> str | None:
    """校验 coding diff 链仓库根。返回 None 表示可用，否则给错误文案。

    只做本地文件系统判定（不跑 git 子进程）：目录要存在，且带 .git
    （普通仓库是目录，worktree/submodule 是文件，两者都算）。
    """
    from pathlib import Path as _P

    if not path.strip():
        return None  # 清空 = 关闭链读取，合法
    p = _P(path).expanduser()
    if not p.exists():
        return f"coding.chainRepo 路径不存在：{path}（diff 链会静默拿不到 diff，先确认真实路径）"
    if not p.is_dir():
        return f"coding.chainRepo 需要仓库根目录，不是文件：{path}"
    if not (p / ".git").exists():
        return f"coding.chainRepo 不是 Git 仓库（缺 .git）：{path}"
    return None


def cmd_config(args) -> int:
    from hymem.core import config as cfgmod

    cfg = cfgmod.load()
    action = args.action

    if action == "path":
        print(cfgmod.config_path())
        return EXIT_OK

    if action == "reset":
        p = cfgmod.config_path()
        if not p.exists():
            return ok({"ok": True, "removed": False}, args.json, "配置文件不存在，无需重置")
        if not args.yes:
            return fail(f"会删除 {p}（含密钥）。确认请加 --yes", EXIT_USAGE)
        cfgmod.reset()
        return ok({"ok": True, "removed": True, "path": str(p)}, args.json, f"已删除 {p}")

    if action == "validate":
        results = cfgmod.validate(cfg)
        bad = [r for r in results if not r[1]]
        # 有 FAIL 时返回非零（P1-6）：脚本/CI 才能拦住坏配置；--json 输出保持原契约。
        code = EXIT_USAGE if bad else EXIT_OK
        if args.json:
            ok(
                {
                    "ok": not bad,
                    "checks": [{"name": n, "pass": p, "detail": d} for n, p, d in results],
                },
                True,
            )
            return code
        lines = []
        for name, passed, detail in results:
            mark = "PASS" if passed else "FAIL"
            lines.append(f"[{mark}] {name:<22}{detail}")
        lines.append("")
        lines.append("结论：配置完整" if not bad else f"结论：{len(bad)} 项未通过")
        ok(results, False, "\n".join(lines))
        return code

    if action == "set":
        # P1-6：坏配置读不出来又没能备份时，默认值整体覆盖会把密钥冲掉——
        # 宁可拒绝，也不静默改写用户的 config.json。
        _le = cfgmod.load_error()
        if _le and not _le.get("backup"):
            return fail(
                f"配置文件读取失败且无法备份（{_le['reason']}），拒绝覆盖 {_le['path']}；"
                "请先修好或手工移走该文件",
                EXIT_RUNTIME,
            )
        if not args.pairs:
            return fail("用法：hy-mem config set key=value（点路径，如 embedding.model=xxx）", EXIT_USAGE)
        import copy

        # 必须深拷贝。浅拷贝 dict(cfg) 只复制顶层，merged["embedding"] 与
        # cfg["embedding"] 是同一个对象，改 merged 会连带改掉 old，导致
        # probe_dim_change 变成"自己和自己比"，永远判不出嵌入模型变更。
        before = copy.deepcopy(cfg)
        merged = copy.deepcopy(cfg)
        for pair in args.pairs:
            if "=" not in pair:
                return fail(f"格式错误：{pair!r}，应为 key=value", EXIT_USAGE)
            keypath, value = pair.split("=", 1)
            node = merged
            parts = keypath.strip().split(".")
            for seg in parts[:-1]:
                node = node.setdefault(seg, {})
                if not isinstance(node, dict):
                    return fail(f"路径冲突：{keypath}", EXIT_USAGE)
            old = node.get(parts[-1])
            if keypath.strip() in ("graph.bufferPoolMb", "graph.maxThreads"):
                # 默认值是 null，泛型分支会把它当字符串存进 JSON；这里显式转 int。
                try:
                    value = int(value)
                except ValueError:
                    return fail(
                        f"{keypath.strip()} 需要整数（0 = 交回 Kuzu 默认），得到 {value!r}",
                        EXIT_USAGE,
                    )
                if value < 0:
                    return fail(f"{keypath.strip()} 不能为负，得到 {value}", EXIT_USAGE)
            elif isinstance(old, bool):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int):
                try:
                    value = int(value)
                except ValueError:
                    return fail(f"{keypath} 需要整数，得到 {value!r}", EXIT_USAGE)
            elif isinstance(old, float):
                try:
                    value = float(value)
                except ValueError:
                    return fail(f"{keypath} 需要小数，得到 {value!r}", EXIT_USAGE)
            node[parts[-1]] = value
        changed, why = cfgmod.probe_dim_change(before, merged)
        if changed and not args.yes:
            return fail(f"嵌入模型变化（{why}），需要 --yes 确认；建议改用 hy-mem setup 走完整流程", EXIT_USAGE)
        # S4：diff 链仓库根先校验再落盘——写个不存在的路径，SDK 只会静默
        # 拿不到 diff（best-effort），用户却以为链开好了，这种假配置最难查。
        for pair in args.pairs:
            if pair.split("=", 1)[0].strip() == "coding.chainRepo":
                bad = _check_chain_repo(str((merged.get("coding") or {}).get("chainRepo") or ""))
                if bad:
                    return fail(bad, EXIT_USAGE)
        # L6：mode 只认三值。写错一个字母，SDK 会按 pro 处理却不报错，
        # 用户以为切了 ultra、evolve 却一直拒——这类静默降级必须当场拦。
        # 只在显式改 mode 时校验：否则历史配置里 mode 拼错会让所有 set 都失败。
        if any(p.split("=", 1)[0].strip() == "mode" for p in args.pairs):
            mode_val = str(merged.get("mode") or "")
            if mode_val not in ("lite", "pro", "ultra"):
                return fail(
                    f"mode 只能是 lite / pro / ultra，得到 {mode_val!r}；L6 认知层需要 ultra",
                    EXIT_USAGE,
                )
        p = cfgmod.save(merged)
        if changed:
            print(
                f"hy-mem: 注意 —— {why}。已有记忆不会自动迁移，需要时跑 hy-mem setup 重建数据目录。",
                file=sys.stderr,
            )
        return ok({"ok": True, "path": str(p), "changed": args.pairs}, args.json, f"已更新 {p}")

    # 默认 show
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    payload = {
        "path": str(cfgmod.config_path()),
        "exists": cfgmod.exists(),
        "mode": cfg.get("mode"),
        "embedding": {
            "model": emb.get("model"),
            "dims": emb.get("dims"),
            "provider": emb.get("provider"),
            "baseUrl": emb.get("baseUrl"),
            "apiKey": "***" if cfgmod.resolve_embed_key(cfg) else "",
        },
        "llm": {
            "model": llm.get("model"),
            "baseUrl": llm.get("baseUrl"),
            "keySet": bool(cfgmod.resolve_llm_key(cfg)),
            "keyEnv": llm.get("keyEnv"),
        },
        "dataDir": (cfg.get("storage") or {}).get("dataDir"),
        "minScore": (cfg.get("search") or {}).get("minScore"),
        "coding": {"chainRepo": (cfg.get("coding") or {}).get("chainRepo") or ""},
        # L6 认知层：model 空 = 沿用 llm.model。pro/lite 下这一段存在但不可用，
        # 显式给 enabled 让用户一眼看懂，不用去猜「为什么 evolve 报错」。
        "evolve": {
            "enabled": cfg.get("mode") == "ultra",
            "model": (cfg.get("evolve") or {}).get("model") or "",
            "effectiveModel": (cfg.get("evolve") or {}).get("model") or llm.get("model"),
            "maxTokens": (cfg.get("evolve") or {}).get("maxTokens"),
            "schemaLimit": (cfg.get("evolve") or {}).get("schemaLimit"),
            "schemaMinScore": (cfg.get("evolve") or {}).get("schemaMinScore"),
        },
        "identity": {
            "userId": cfgmod.identity_defaults(cfg),
            "label": cfgmod.identity_label(cfgmod.identity_defaults(cfg)),
            "source": cfgmod.identity_source(cfg),
        },
    }
    if args.json:
        return ok(payload, True)
    from hymem.ui import i18n
    from hymem.core import errors as _ec
    _ec.register_tr(i18n.tr)  # S1 分层：错误文案翻译经钩子注入

    t = i18n.tr
    lines = [
        t("配置文件  {}", payload["path"])
        + ("" if payload["exists"] else t("  （尚未创建，运行 hy-mem setup）")),
        "",
        t("身份      {}   （hy-mem user 可切换）", payload["identity"]["label"]),
        t("运行模式  {}", payload["mode"]),
        t("向量模型  {}  ({} 维, {})", emb.get("model"), emb.get("dims"), emb.get("provider")),
        t("嵌入入口  {}", emb.get("baseUrl") or t("（本地 ONNX）")),
        t("嵌入密钥  {}", t("已设置") if cfgmod.resolve_embed_key(cfg) else t("未设置"))
        + (t("（来自 ${}）", emb.get("keyEnv")) if emb.get("keyEnv") and not emb.get("apiKey") else ""),
        t("LLM 模型  {} @ {}", llm.get("model"), llm.get("baseUrl")),
        t("LLM 密钥  {}", t("已设置") if payload["llm"]["keySet"] else t("未设置"))
        + (t("（来自 ${}）", llm.get("keyEnv")) if llm.get("keyEnv") and not llm.get("apiKey") else ""),
        t("存储目录  {}", payload["dataDir"]),
        t("检索阈值  {}", payload["minScore"]),
        t("diff 链仓库  {}", payload["coding"]["chainRepo"] or t("（未设置，只认工具轨迹里的 path/old/new）")),
        t("L6 认知层  {}",
          (t("已启用（ultra）") + t("，归纳模型 {}，检索 {} 条/阈值 {}", payload["evolve"]["effectiveModel"],
                                   payload["evolve"]["schemaLimit"], payload["evolve"]["schemaMinScore"]))
          if payload["evolve"]["enabled"]
          else t("未启用（mode={}，L6 仅 ultra 可用）", payload["mode"])),
    ]
    return ok(payload, False, "\n".join(lines))


def cmd_user(args) -> int:
    """查看 / 切换 user_id —— 多智能体共享或隔离记忆的唯一开关。

    设计：user_id 是记忆的隔离键。不设 = 通用（default），所有智能体读写的
    是同一份记忆；设了 = 只属于这个名字，与其它名字互不可见。
    切换**只改这一个字段**，不碰模型配置、不碰数据目录、不动已有记忆。
    """
    from hymem.core import config as cfgmod

    cfg = cfgmod.load()
    action = getattr(args, "action", "show") or "show"
    if getattr(args, "reset", False) and action == "show":
        action = "reset"
    env_uid = os.getenv("HY_MEM_USER_ID", "").strip()

    if action == "set":
        uid = (getattr(args, "uid", "") or "").strip()
        if not uid:
            return fail(
                "用法：hy-mem user set <user_id>（回到通用用 hy-mem user reset）",
                EXIT_USAGE,
            )
        path, effective = cfgmod.set_user_id(uid)
        prev = cfgmod.identity_defaults(cfg)
        payload = {
            "ok": True,
            "userId": effective,
            "general": effective == cfgmod.GENERAL_USER_ID,
            "previous": prev,
            "path": str(path),
        }
        if env_uid and env_uid != effective:
            payload["envOverride"] = env_uid
        human = (
            f"身份已切到 {cfgmod.identity_label(effective)}"
            + (f"\n  注意：环境变量 HY_MEM_USER_ID={env_uid} 优先级更高，实际生效的是它"
               if env_uid and env_uid != effective else "")
            + f"\n  {cfgmod.identity_label(prev)} 下的记忆没有删，只是现在看不到；切回去就能看到。"
            + "\n  只改身份，模型配置与数据目录都没动。"
        )
        if args.json:
            return ok(payload, True)
        print(human)
        return EXIT_OK

    if action == "reset":
        path, effective = cfgmod.set_user_id("")
        payload = {"ok": True, "userId": effective, "general": True, "path": str(path)}
        if env_uid:
            payload["envOverride"] = env_uid
        return ok(
            payload,
            args.json,
            f"已回到{cfgmod.identity_label(effective)}：所有智能体共享同一份记忆"
            + (f"（但环境变量 HY_MEM_USER_ID={env_uid} 仍在覆盖）" if env_uid else ""),
        )

    effective = cfgmod.identity_defaults(cfg)
    configured = cfgmod.identity_configured(cfg)
    source = cfgmod.identity_source(cfg)
    known = [str(x) for x in ((cfg.get("identity") or {}).get("known") or []) if str(x).strip()]
    if effective not in known:
        known.append(effective)

    if action == "list":
        rows = [
            {
                "userId": u,
                "label": cfgmod.identity_label(u),
                "current": u == effective,
                "source": source if u == effective else "",
            }
            for u in known
        ]
        if args.json:
            return ok({"ok": True, "current": effective, "users": rows}, True)
        lines = []
        for r in rows:
            mark = "← 当前" if r["current"] else ""
            lines.append(f"  {r['userId']:<20}{r['label']:<14}{mark}")
        if not lines:
            lines = ["  （只见过通用身份）"]
        return ok({"ok": True, "current": effective, "users": rows}, False, "\n".join(lines))

    # show（默认）
    payload = {
        "ok": True,
        "userId": effective,
        "general": effective == cfgmod.GENERAL_USER_ID,
        "label": cfgmod.identity_label(effective),
        "source": source,
        "configured": configured or None,
        "envOverride": env_uid or None,
        "known": known,
        "configPath": str(cfgmod.config_path()),
    }
    if args.json:
        return ok(payload, True)
    src_text = {
        "env": "环境变量 HY_MEM_USER_ID（优先级最高，临时覆盖用）",
        "config": "config.json 的 identity.userId",
        "general": "未设置 → 通用（default）",
    }.get(source, source)
    lines = [
        f"当前身份  {cfgmod.identity_label(effective)}",
        f"生效来源  {src_text}",
        f"配置文件  {payload['configPath']}",
        "",
        "  通用（default）：所有智能体共享同一份记忆 —— 这是没设 user_id 时的默认行为",
        "  其它名字      ：自成一套记忆，与别处互不可见，用于多智能体隔离",
        "",
        "切换身份（只改这一个字段，不动模型配置与数据目录）：",
        "  hy-mem user set writer         # 之后读写都属于 writer",
        "  hy-mem user reset              # 回到通用，与其它智能体共享",
        "  hy-mem user list               # 见过哪些身份",
        "",
        "临时只影响一条命令：",
        '  hy-mem -u writer search "..."  # 或在环境里设 HY_MEM_USER_ID',
    ]
    return ok(payload, False, "\n".join(lines))
