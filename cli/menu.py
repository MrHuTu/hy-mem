"""交互式主界面：无参数运行 hy-mem 时进入.

定位不是「把子命令再包一层菜单」，而是补 CLI 不方便做的事：
多行输入（一条命令写多段记忆）、写前的标签与拆条选择、结果的表格化展示、
以及模型/存储这类需要「改一项就验证一项」的设置。

实现上刻意不 import hy_mem_cli（会循环依赖），改用 dispatch 回调：
菜单只负责收参数，执行走**子进程**调用同一套命令，保证菜单与命令行的行为
完全一致。为什么必须是子进程（0.9.10）：0.9.9 在本进程内跑 worker，worker
起的 chroma server 端口会写回本进程 env，退出后 env 指向死端口，整个会话
所有命令都报 "Could not connect to a Chroma server"（PRD F1）。子进程哪怕
把 env 改坏也只影响它自己。

导航约定（与 tui.select 一致）：↑↓ 移动、Enter 选中、Esc 回上一层；
在主界面按 Esc 直接退出。子页里 Esc 只退出当前页，不退出程序。
"""

from __future__ import annotations

import json
import os
import subprocess

import config as cfgmod
import presets
import storage_ops
import tui
from tui import c

BANNER = r"""
   _  _  _    __  __  ___ ___
  | || || |  |  \/  |/ __| __|   记忆库命令行
  | __ || |__| |\/| | (__| _|
  |_||_||____|_|  |_|\___|___|   lite / pro / ultra
"""

_MODE_DESC = {"lite": "纯向量，最快", "pro": "向量+事实抽取", "ultra": "再+Schema 归纳"}


def banner(cfg: dict) -> str:
    emb = cfg.get("embedding") or {}
    mode = str(cfg.get("mode") or "pro")
    data_dir = str((cfg.get("storage") or {}).get("dataDir") or "?")
    try:
        uid = cfgmod.identity_label(cfgmod.identity_defaults(cfg))
    except Exception:  # noqa: BLE001
        uid = "?"
    rows = [
        c.cyan(BANNER.strip("\n")),
        "",
        "  "
        + c.gray("模式 ")
        + c.bold(mode)
        + c.dim(f"（{_MODE_DESC.get(mode, mode)}）")
        + c.gray("   向量 ")
        + c.bold(str(emb.get("model") or "?"))
        + c.dim(f" {emb.get('dims')} 维")
        + c.gray("   身份 ")
        + c.bold(uid),
        "  "
        + c.gray("库 ")
        + c.dim(_short_path(data_dir))
        + c.gray("   帮助 ")
        + c.dim("按 i 或运行 hy-mem help --json"),
    ]
    missing = _cfg_missing(cfg)
    if missing:
        # 配置不全时人一进来就该知道，而不是写完记忆才看到报错
        rows.append("  " + tui.icon("warn") + " " + c.yellow(f"配置未完成：缺 {', '.join(missing[:3])}")
                    + c.dim("   → 按 s 进设置，或运行 hy-mem setup"))
    return "\n".join(rows)


def _cfg_missing(cfg: dict) -> list[str]:
    try:
        return [name for name, passed, _d in cfgmod.validate(cfg) if not passed]
    except Exception:  # noqa: BLE001
        return []



def _short_path(p: str, width: int = 34) -> str:
    if len(p) <= width:
        return p
    return "…" + p[-width:]


def _parse_json(out: str):
    out = out.strip()
    if not out:
        return None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _show_failure(raw: str, code: int) -> None:
    """把子命令的失败原样摆出来。

    为什么不再截成 400 字：配置不全时子命令打的是「缺哪几项 + 怎么补」的多行
    提示，截断后恰好把修复命令切掉 —— 用户只看到「失败了」，等于没提示。
    """
    text = (raw or "").strip()
    if not text:
        tui.err(f"失败（exit {code}）")
        return
    if "怎么补" in text or "配置还没" in text or len(text) > 200:
        print(text)
    else:
        tui.err(text)


def subprocess_dispatch(argv: list[str], stdin_text: str | None = None) -> tuple[int, str, str]:
    """跑一条子命令（**子进程**），返回 (退出码, stdout, stderr)。

    为什么不用 `main(argv)` 同进程调用：子命令可能拉起 worker / serve 这类
    后台角色，它们会改本进程的 env（MEMORY_VECTOR_PORT）。同进程跑过一次之后，
    本进程 env 就永久指向一个可能已经死掉的端口，之后所有命令都连不上
    （0.9.9 的 F1）。子进程把 env 改动留在自己肚子里，界面进程永远干净。
    """
    import install_ops

    cmd = install_ops.target_command() + list(argv)
    try:
        p = subprocess.run(
            cmd, input=stdin_text, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=dict(os.environ), timeout=1800,
        )
    except FileNotFoundError as e:
        return 127, "", f"无法启动子进程：{e}"
    except subprocess.TimeoutExpired:
        return 124, "", "子命令 30 分钟未结束，已放弃等待"
    except OSError as e:
        return 126, "", f"子进程启动失败：{type(e).__name__}: {e}"
    return p.returncode or 0, p.stdout or "", p.stderr or ""


def _capture(dispatch, argv: list[str]) -> tuple[int, object, str]:
    """跑一条子命令，把 stdout 抓回来解析 JSON。

    菜单要靠结构化结果做表格；直接让子命令往终端打印会打乱菜单版面。
    """
    # --json 必须前置：argparse 的全局选项放在子命令之后会解析失败
    code, out, err = dispatch(["--json"] + argv)
    return code, _parse_json(out), (out or err)


def _run_and_show(dispatch, argv: list[str]) -> None:
    """跑子命令并把结果摆到屏幕上（子进程的 stdout 不再自动可见）。"""
    code, out, err = dispatch(argv)
    text = (out or err or "").strip()
    if code == 0:
        if text:
            print(text)
    else:
        _show_failure(text, code)


# --------------------------------------------------------------------------
# 功能页
# --------------------------------------------------------------------------
def page_add(dispatch, cfg: dict) -> None:
    print(tui.title("写记忆"))
    print(c.dim("  一行一条最好（混在一起会稀释向量）。空行结束；Ctrl+C 取消。"))
    print()
    lines: list[str] = []
    while True:
        try:
            line = tui.line(c.gray("  │ "))
        except (EOFError, KeyboardInterrupt, tui.Aborted):
            print()
            break
        if line.strip() == "":
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        tui.warn("没有输入内容")
        return
    split = len(lines) > 1 and tui.confirm(f"检测到 {len(lines)} 行，拆成 {len(lines)} 条独立记忆？", default=True)

    tag_raw = tui.ask("标签（逗号分隔，可留空）", hint="标签用于按类归档与过滤；pro 模式还会由 LLM 自动打 tag")
    tags = [t.strip() for t in tag_raw.replace("，", ",").split(",") if t.strip()]

    mode = str(cfg.get("mode") or "pro")
    hint = "pro 模式同步写入每条 8~40s，建议入队" if mode in ("pro", "ultra") else ""
    use_async = tui.confirm("入队异步写入？" + (f"（{hint}）" if hint else "（推荐）"), default=True)

    argv = ["add"]
    if use_async:
        argv.append("--async")
    if split:
        argv.append("--split")
    for t in tags:
        argv += ["--tag", t]

    # 内容走 stdin，避免 cmd.exe 把多行参数压平
    print()
    tui.spinner_line("写入中…")
    code, out, err = dispatch(argv, stdin_text=text)
    raw = (out or err or "").strip()
    if code == 0:
        tui.ok(raw.splitlines()[-1] if raw else "已提交")
        if use_async:
            # 异步写入只是入队，库里还没有它 —— 这一点必须说破。
            # 之前只说「已入队，用队列页跑 worker」，用户写完直接去检索，
            # 得到 0 条就以为记忆丢了。
            tui.note("已入队，但还没进库 —— 此刻检索不到它是正常的。")
            if tui.confirm("现在就跑一次 worker 落库？（pro 模式每条约 10~50s）", default=True):
                tui.spinner_line("落库中…")
                code2, out2, err2 = dispatch(["worker", "--once"])
                text2 = (out2 or err2 or "").strip()
                if code2 == 0:
                    tui.ok(text2.splitlines()[-1] if text2 else "已落库")
                else:
                    _show_failure(text2, code2)
    else:
        _show_failure(raw, code)


def search_rows(results: list[dict]) -> list[list[str]]:
    """检索结果 → 表格行。内容列按终端宽度折行，绝不截断。

    演化链的 content 本身就是多行的（Current / When / History / 各旧版本），
    以前压平成一行再 trunc(90)，用户只看到 `用户认…`，会以为记忆内容就
    这么短（2026-10-28 反馈）。固定列 #/分数/标签 让出宽度后交给 tui.wrap。
    """
    tw = tui.term_width()
    nw = max(2, len(str(max(1, len(results)))))
    tagw = min(22, max([tui.vwidth(",".join(str(t) for t in (it.get("tags") or [])))
                        for it in results] + [4]))
    content_w = max(24, tw - nw - 5 - tagw - 3 * 2)
    rows = []
    for i, it in enumerate(results, 1):
        score = it.get("score")
        s = f"{score:.3f}" if isinstance(score, (int, float)) else "-"
        body = it.get("content") or it.get("text") or ""
        rows.append([
            str(i), s,
            "\n".join(tui.wrap(body, content_w)),
            ",".join(str(t) for t in (it.get("tags") or []))[:tagw],
        ])
    return rows


def page_search(dispatch, cfg: dict) -> None:
    print(tui.title("检索"))
    q = tui.ask("查询内容")
    if not q:
        return
    tag_raw = tui.ask("限定标签（逗号分隔，可留空）")
    tags = [t.strip() for t in tag_raw.replace("，", ",").split(",") if t.strip()]
    n_raw = tui.ask("返回条数", default="5")
    try:
        n = max(1, int(n_raw))
    except ValueError:
        n = 5

    argv = ["search", q, "-n", str(n)]
    for t in tags:
        argv += ["--tag", t]

    print()
    code, data, raw = _capture(dispatch, argv)
    if code != 0:
        _show_failure(raw, code)
        return
    results = (data or {}).get("results") or []
    if not results:
        tui.warn("无命中")
        tui.note("可试着放宽：降低阈值（设置 → 检索阈值）或换更接近原话的问法")
        return
    print(tui.table(["#", "分数", "内容", "标签"], search_rows(results)))
    tui.note(f"阈值 {(data or {}).get('min_score')}，命中 {len(results)} 条；"
             f"看全文：hy-mem get {results[0].get('memory_id') or '<id>'}")


def page_list(dispatch, cfg: dict) -> None:
    print(tui.title("最近记忆"))
    code, data, raw = _capture(dispatch, ["list", "-n", "15"])
    if code != 0:
        _show_failure(raw, code)
        return
    items = (data or {}).get("memories") or []
    if not items:
        tui.warn("（空）")
        return
    rows = []
    idw = 12
    content_w = max(24, tui.term_width() - idw - 2)
    for it in items:
        mid = str(it.get("memory_id") or it.get("id") or "?")
        body = it.get("content") or it.get("new_memory") or it.get("text") or ""
        rows.append([mid[:idw], "\n".join(tui.wrap(body, content_w))])
    print(tui.table(["id", "内容"], rows, footer=f"共 {len(items)} 条；删除用 hy-mem delete <id>"))


def page_tags(dispatch, cfg: dict) -> None:
    print(tui.title("标签"))
    code, data, raw = _capture(dispatch, ["tags"])
    if code != 0:
        _show_failure(raw, code)
        return
    inv = (data or {}).get("tags") or (data or {}).get("inventory") or []
    if not inv:
        tui.warn("（还没有标签）")
        return
    rows = [[t.get("tag", "?"), str(t.get("n", 0)), str(t.get("user_n", 0)), str(t.get("auto_n", 0))] for t in inv]
    print(tui.table(["标签", "合计", "手工", "自动"], rows))

    print()
    action = tui.select(
        "要做什么？",
        [
            ("从记忆同步自动标签", "把 SDK 侧 LLM 生成的 tag 拉进本地索引", "s"),
            ("删除某个标签", "只解绑，不删记忆", "d"),
            ("返回", "", "x"),
        ],
        default=2,
    )
    if action == 0:
        code, _d, raw = _capture(dispatch, ["tags", "--refresh"])
        (tui.ok if code == 0 else tui.err)(raw.strip().splitlines()[-1] if raw.strip() else "完成")
    elif action == 1:
        tag = tui.ask("要删除的标签名")
        if tag and tui.confirm(f"解绑所有 {tag} 标签？", default=False):
            code, _d, raw = _capture(dispatch, ["untag", "--tag", tag, "--yes"])
            (tui.ok if code == 0 else tui.err)(raw.strip().splitlines()[-1] if raw.strip() else "完成")


def page_queue(dispatch, cfg: dict) -> None:
    print(tui.title("异步队列"))
    code, data, raw = _capture(dispatch, ["queue"])
    if code != 0:
        _show_failure(raw, code)
        return
    if isinstance(data, dict):
        pairs = [
            ("待处理", str(data.get("pending", 0))),
            ("处理中", str(data.get("running", 0))),
            ("已完成", str(data.get("done", 0))),
            ("死信", str(data.get("dead", 0))),
        ]
        if data.get("oldest_pending_age_s"):
            pairs.append(("最老待处理", f"{data['oldest_pending_age_s']:.0f}s"))
        print(tui.kv(pairs))

    print()
    action = tui.select(
        "操作",
        [
            ("处理队列直到清空", "跑 worker，逐条落库（pro 每条 8~40s）", "a"),
            ("只处理一条", "处理 1 条后返回", "o"),
            ("重投死信任务", "把 dead 任务放回待处理", "r"),
            ("返回", "", "x"),
        ],
        default=3,
    )
    if action == 0:
        tui.note("开始消费；Ctrl+C 可中断（已入队的任务不会丢）")
        tui.spinner_line("消费中…")
        _run_and_show(dispatch, ["worker", "--idle-exit", "5"])
    elif action == 1:
        tui.spinner_line("处理一条…")
        _run_and_show(dispatch, ["worker", "--once", "--max", "1"])
    elif action == 2:
        if tui.confirm("把状态为 dead 的任务全部重投？", default=False):
            _run_and_show(dispatch, ["retry", "dead"])


_LEVEL_STYLE = {"OK": ("ok", "green"), "WARN": ("warn", "yellow"), "CRIT": ("err", "red")}


def page_health(dispatch, cfg: dict) -> None:
    print(tui.title("健康检查"))
    tui.spinner_line("扫描库体积与条数…")
    code, data, raw = _capture(dispatch, ["stats", "--health"])
    if code != 0 or not isinstance(data, dict):
        tui.err((raw or "").strip()[:400] or "失败")
        return
    verdict = data.get("verdict", "?")
    ico, col = _LEVEL_STYLE.get(verdict, ("dot", "gray"))
    print(f"  结论 {getattr(c, col)(tui.icon(ico) + ' ' + verdict)}   "
          + c.dim(f"{data.get('memory_count')} 条 / {data.get('size_mb')} MB / 单条预期 "
                  f"{data.get('estimated_write_latency_s')}s"))
    print()
    rows = []
    # 说明列吃掉剩余宽度后折行：健康检查的 detail 常有「怎么修」这类长句，
    # 截到 60 列等于把处置建议砍掉一半。
    for ck in data.get("checks") or []:
        i2, col2 = _LEVEL_STYLE.get(ck.get("level", "OK"), ("dot", "gray"))
        rows.append([tui.icon(i2), ck.get("name", ""), getattr(c, col2)(str(ck.get("value", ""))), ""])
    name_w = max([tui.vwidth(r[1]) for r in rows] + [4])
    val_w = max([tui.vwidth(r[2]) for r in rows] + [4])
    detail_w = max(24, tui.term_width() - 2 - name_w - val_w - 3 * 2)
    for r, ck in zip(rows, data.get("checks") or []):
        r[3] = "\n".join(tui.wrap(ck.get("detail", ""), detail_w))
    print(tui.table(["", "项", "值", "说明"], rows))
    advice = data.get("advice") or []
    if advice:
        print()
        print(tui.section("建议"))
        for i, t in enumerate(advice, 1):
            print(f"  {i}. {t}")


# --------------------------------------------------------------------------
# 设置中心
# --------------------------------------------------------------------------
def _emb_line(cfg: dict) -> str:
    emb = cfg.get("embedding") or {}
    return f"{emb.get('model')} · {emb.get('dims')} 维 · {emb.get('provider')}"


def _llm_line(cfg: dict) -> str:
    llm = cfg.get("llm") or {}
    if cfg.get("mode") == "lite":
        return "lite 模式不需要"
    return f"{llm.get('model')}"


def _storage_line(cfg: dict) -> str:
    p = str((cfg.get("storage") or {}).get("dataDir") or "")
    size, files = storage_ops.dir_usage(p)
    if files:
        return f"{_short_path(p, 40)}  ({storage_ops.human_size(size)}/{files} 文件)"
    return f"{_short_path(p, 40)}  （空）"


def page_storage(dispatch, cfg: dict) -> None:
    while True:
        cfg = cfgmod.load()
        data_dir = str((cfg.get("storage") or {}).get("dataDir") or "")
        tui.clear()
        print(tui.title("数据存储目录"))
        print(tui.kv(storage_ops_usage_rows(data_dir)))
        print()
        action = tui.select(
            "要做什么？",
            [
                ("换目录 / 迁移数据", "复制现有数据到新位置并切换", "m"),
                ("恢复到默认目录", str(cfgmod.default_data_dir()), "d"),
                ("备份一份", "复制成 data.bak-时间戳，改模型前用", "b"),
                ("清空重建", "先自动备份，再清空（重建索引/换维度后使用）", "w"),
                ("清理队列库", "删除已完成任务记录并 VACUUM，只影响队列不影响记忆", "q"),
                ("返回", "", "x"),
            ],
            default=5,
        )
        if action == 5:
            return
        if action == 4:
            if tui.confirm("清理 jobs 表里已完成的记录并 VACUUM？（不动记忆）", default=False):
                code, _d, raw = _capture(dispatch, ["stats", "--health", "--vacuum"])
                (tui.ok if code == 0 else tui.err)("队列记录已清理" if code == 0 else raw.strip()[:300])
            tui.pause()
            continue
        if action == 2:
            ok, msg = storage_ops.backup(data_dir)
            (tui.ok if ok else tui.err)(msg)
            tui.pause()
            continue
        if action == 3:
            if not tui.confirm("先备份再清空数据目录？清空后记忆需要重新写入", default=False):
                continue
            ok, msg = storage_ops.wipe(data_dir)
            (tui.ok if ok else tui.err)(msg)
            tui.pause()
            continue

        target = ""
        if action == 0:
            raw = tui.ask("新目录", hint="留空取消；支持 %USERPROFILE% 与 ~")
            if not raw:
                continue
            target = storage_ops.normalize_path(raw)
            good, msg = storage_ops.check_target(target)
            if not good:
                tui.err(msg)
                tui.pause()
                continue
        else:
            target = str(cfgmod.default_data_dir())
        if target == data_dir:
            tui.info("与当前目录相同，无需切换")
            tui.pause()
            continue

        size, files = storage_ops.dir_usage(data_dir)
        if files and tui.confirm(
            f"把现有数据（{storage_ops.human_size(size)} / {files} 文件）迁移过去？",
            default=True,
        ):
            tui.spinner_line("复制中…")
            ok, msg = storage_ops.migrate(data_dir, target)
            (tui.ok if ok else tui.err)(msg)
            if not ok and not tui.confirm("迁移失败，仍然切换？", default=False):
                tui.pause()
                continue
        cfg["storage"]["dataDir"] = target
        cfgmod.save(cfg)
        tui.ok(f"存储目录已切到 {target}")
        tui.pause()


def storage_ops_usage_rows(data_dir: str) -> list[tuple[str, str]]:
    size, files = storage_ops.dir_usage(data_dir)
    free = storage_ops.free_space(data_dir)
    rows = [("当前目录", data_dir)]
    rows.append(("已占用", f"{storage_ops.human_size(size)} / {files} 个文件" if files else "空目录"))
    if free:
        rows.append(("所在盘可用", storage_ops.human_size(free)))
    rows.append(("默认目录", str(cfgmod.default_data_dir())))
    return rows


def page_identity(dispatch, cfg: dict) -> None:
    """身份页：多智能体共享还是隔离，只在这一处决定。"""
    while True:
        cfg = cfgmod.load()
        cur = cfgmod.identity_defaults(cfg)
        src = cfgmod.identity_source(cfg)
        tui.clear()
        print(tui.title("身份 user_id"))
        print(tui.kv(
            [
                ("当前身份", cfgmod.identity_label(cur)),
                ("生效来源", {"env": "环境变量 HY_MEM_USER_ID", "config": "config.json", "general": "未设置 → 通用"}.get(src, src)),
                ("配置文件", str(cfgmod.config_path())),
            ]
        ))
        print()
        kw = tui.vwidth("通用（default）") + 1
        print(tui.panel([
            c.cyan(tui.lpad("通用（default）", kw)) + "所有智能体共享同一份记忆 —— 不设 user_id 的默认行为",
            c.cyan(tui.lpad("其它名字", kw)) + "自成一套记忆，与别处互不可见，用于隔离",
            "",
            c.dim("切换只改身份：模型配置、数据目录、已有记忆都不动；"),
            c.dim("原身份的记忆不会删，只是现在看不到，切回去就能看到。"),
        ]))
        print()
        action = tui.select(
            "要做什么？",
            [
                ("改成另一个 user_id", "为这个智能体单独隔离一份记忆", "s"),
                ("回到通用（default）", "与其它智能体共享", "r"),
                ("查看见过哪些身份", "只读", "l"),
                ("返回", "", "x"),
            ],
            default=3,
        )
        if action == 3:
            return
        if action == 2:
            known = [str(x) for x in ((cfg.get("identity") or {}).get("known") or []) if str(x).strip()]
            if not known:
                tui.note("（还没有记录到别的身份）")
            else:
                print(tui.table(["user_id", "当前"], [[u, "←" if u == cur else ""] for u in known]))
            tui.pause()
            continue
        if action == 1:
            if not tui.confirm(f"回到通用（{cfgmod.GENERAL_USER_ID}）？", default=True):
                continue
            _path, eff = cfgmod.set_user_id("")
            tui.ok(f"已切到 {cfgmod.identity_label(eff)}")
            tui.pause()
            continue
        uid = tui.ask("新的 user_id", hint="英文/数字/-/_ 最稳；留空取消")
        if not uid.strip():
            continue
        _path, eff = cfgmod.set_user_id(uid.strip())
        tui.ok(f"已切到 {cfgmod.identity_label(eff)}")
        tui.note(f"{cfgmod.identity_label(cur)} 的记忆没有删，切回去仍能看到")
        tui.pause()


def page_settings(dispatch, cfg: dict) -> None:
    import setup_wizard

    while True:
        cfg = cfgmod.load()
        tui.clear()
        print(tui.title("设置"))
        print(tui.kv(
            [
                ("身份 user_id", cfgmod.identity_label(cfgmod.identity_defaults(cfg))),
                ("向量模型", _emb_line(cfg)),
                ("运行模式", f"{cfg.get('mode')}  ({_MODE_DESC.get(str(cfg.get('mode')), '')})"),
                ("LLM 模型", _llm_line(cfg)),
                ("存储目录", _storage_line(cfg)),
                ("检索阈值", str((cfg.get("search") or {}).get("minScore"))),
            ]
        ))
        print()
        action = tui.select(
            "改哪一项？",
            [
                ("向量模型（embedding）", "改后会实测维度；换模型需要重建数据目录", "e"),
                ("运行模式 MEMORY_MODE", "lite 纯向量 / pro 加 LLM 抽取 / ultra 再加 Schema", "m"),
                ("LLM 模型", "pro 及以上用于抽取事实与自动打标签", "l"),
                ("数据存储目录（storage）", "查看用量、迁移、备份、清空重建", "d"),
                ("检索阈值 minScore", f"当前 {cfg.get('search', {}).get('minScore')}", "t"),
                ("队列参数", "重试次数与锁超时，一般不用动", "q"),
                ("身份 user_id", "多智能体共享（通用）还是隔离（独立名字）", "u"),
                ("完整向导", "从向量模型开始重走一遍四步", "f"),
                ("查看当前配置", "只读：所有生效值与环境变量映射", "v"),
                ("环境自检 doctor", "端到端连通性与依赖检查", "c"),
                ("返回", "", "x"),
            ],
            default=10,
        )
        if action == 10:
            return
        if action == 9:
            page_doctor(dispatch, cfg)
            tui.pause()
            continue
        if action == 8:
            page_config(dispatch, cfg)
            tui.pause()
            continue
        if action == 6:
            page_identity(dispatch, cfg)
            continue
        if action == 3:
            page_storage(dispatch, cfg)
            continue
        try:
            if action == 7:
                setup_wizard.run_setup(scope="all")
            else:
                key = ["embedding", "mode", "llm", "storage", "search", "queue"][action]
                setup_wizard.run_setup(scope=key)
        except tui.Aborted:
            tui.warn("已取消")
        tui.pause()


def page_doctor(dispatch, cfg: dict) -> None:
    print(tui.title("环境自检"))
    _run_and_show(dispatch, ["doctor"])


def page_config(dispatch, cfg: dict) -> None:
    print(tui.title("当前配置"))
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    pairs = [
        ("配置文件", str(cfgmod.config_path())),
        ("首次设置", "已完成" if cfg.get("setupComplete") else "未完成"),
        ("身份 user_id", cfgmod.identity_label(cfgmod.identity_defaults(cfg))
         + f"（来源：{cfgmod.identity_source(cfg)}）"),
        ("运行模式", str(cfg.get("mode"))),
        ("向量模型", f"{emb.get('model')} ({emb.get('dims')} 维, {emb.get('provider')})"),
        ("嵌入入口", str(emb.get("baseUrl") or "（本地 ONNX）")),
        ("嵌入密钥", "已设置" if emb.get("apiKey") else "未设置"),
        ("LLM 模型", f"{llm.get('model')} @ {llm.get('baseUrl')}"),
        ("LLM 密钥", ("已设置" + (f"（来自 ${llm.get('keyEnv')}）" if not llm.get("apiKey") else ""))
         if cfgmod.resolve_llm_key(cfg) else "未设置"),
        ("存储目录", str((cfg.get("storage") or {}).get("dataDir"))),
        ("检索阈值", str((cfg.get("search") or {}).get("minScore"))),
        ("队列", f"重试 {cfg.get('queue', {}).get('maxAttempts')} 次 / 锁超时 {cfg.get('queue', {}).get('lockTimeout')}s"),
    ]
    print(tui.kv(pairs))
    print()
    tui.note("改这些走「设置」；直接改 config.json 也行，但下次运行才生效。")


def page_help(dispatch, cfg: dict) -> None:
    """把命令清单直接摆在界面上 —— 不然没人知道有 help，也不知道 agent 该读哪份规格。"""
    print(tui.title("帮助 / 命令清单"))
    _run_and_show(dispatch, ["help"])
    print()
    tui.note("机器可读规格：hy-mem help --json （参数类型 / 默认值 / 返回字段 / 示例）")
    tui.note("身份切换：hy-mem user set <名字> —— 不设就是「通用」，多个智能体共享同一份记忆")
    tui.note("接入新宿主：hy-mem install —— 把启动器写到固定目录，让 hy-mem 在 PATH 上真实存在")
    tui.note("按键自检：hy-mem tui-test —— 方向键没反应时先跑它")


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
# (标题, 说明, 处理函数, 英文热键)
# 热键只用 ASCII 字母：中文标签的宽度与输入法状态在各终端不可控，而一个字母
# 既能让人少按几下，也能让脚本/智能体用确定的方式直接选中某一项。
# 避开 j/k/g/G（上下/首尾）与数字（直达序号）。
MENU = [
    ("写记忆", "多行输入，可拆条 + 打标签", page_add, "w"),
    ("检索", "语义检索，可按标签过滤", page_search, "r"),
    ("最近记忆", "最近 15 条，可复制 id 去删", page_list, "l"),
    ("标签", "分类清单与解绑", page_tags, "t"),
    ("队列", "异步写入的待处理与消费", page_queue, "q"),
    ("健康检查", "条数 / 体积 / 写入延迟预期", page_health, "h"),
    ("设置", "模型 / 模式 / 存储目录", page_settings, "s"),
    ("环境自检", "doctor：端到端连通性", page_doctor, "d"),
    ("当前配置", "只读查看生效配置", page_config, "c"),
    ("帮助", "命令清单 + 给智能体读的 JSON 规格入口", page_help, "i"),
]


def run_menu(dispatch) -> int:
    while True:
        cfg = cfgmod.load()
        tui.clear()
        print(banner(cfg))
        print()
        options = [(name, detail, key) for name, detail, _fn, key in MENU]
        options.append(("退出", "Esc / q 也可以退出", "x"))
        try:
            idx = tui.select("选择操作", options, default=0)
        except tui.Aborted:
            print()
            return 0
        if idx >= len(MENU):
            return 0
        name, _detail, fn, _key = MENU[idx]
        print()
        try:
            fn(dispatch, cfg)
        except tui.Aborted:
            print()
            tui.warn("已取消")
        except KeyboardInterrupt:
            print()
            tui.warn("已中断")
        except SystemExit:
            # 子命令里 argparse 的用法错误会抛 SystemExit；在交互界面里它只应
            # 表现为「这次没执行成功」，不能把整个主界面带走。
            tui.warn("该操作未执行（参数不合法）")
        except Exception as e:  # noqa: BLE001
            tui.err(f"{name} 出错：{type(e).__name__}: {e}")
        tui.pause()
