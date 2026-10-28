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

文案统一走 i18n.tr()：键是中文原文，默认语言下原样返回，所以中文界面与改造前
逐字一致；只有显式切到 en 才会查词典。这里不在 import 期就把 MENU 翻好——
设置页切语言后要能立刻重画，翻译必须发生在渲染那一刻。
"""

from __future__ import annotations

import json
import os
import subprocess

import config as cfgmod
import presets
import storage_ops
import tui
from i18n import label, lang, set_lang, tr
from tui import c

# 横幅的 ASCII 骨架与右侧标签分开：英文标签宽度和中文不同，拼在一起才好换语言
_BANNER_ART = [
    r"   _  _  _    __  __  ___ ___",
    r"  | || || |  |  \/  |/ __| __|",
    r"  | __ || |__| |\/| | (__| _|",
    r"  |_||_||____|_|  |_|\___|___|",
]

_MODE_DESC = {"lite": "纯向量，最快", "pro": "向量+事实抽取", "ultra": "再+Schema 归纳"}


def _banner_text() -> str:
    art = _BANNER_ART
    return "\n".join(
        [
            art[0],
            art[1] + "   " + tr("记忆库命令行"),
            art[2],
            art[3] + "   lite / pro / ultra",
        ]
    )


def banner(cfg: dict) -> str:
    emb = cfg.get("embedding") or {}
    mode = str(cfg.get("mode") or "pro")
    data_dir = str((cfg.get("storage") or {}).get("dataDir") or "?")
    try:
        uid = cfgmod.identity_label(cfgmod.identity_defaults(cfg))
    except Exception:  # noqa: BLE001
        uid = "?"
    rows = [
        c.cyan(_banner_text()),
        "",
        "  "
        + c.gray(tr("模式") + " ")
        + c.bold(mode)
        + c.dim(tr("（{}）", tr(_MODE_DESC.get(mode, mode))))
        + c.gray("   " + tr("向量") + " ")
        + c.bold(str(emb.get("model") or "?"))
        + c.dim(" " + tr("{} 维", emb.get("dims")))
        + c.gray("   " + tr("身份") + " ")
        + c.bold(uid),
        "  "
        + c.gray(tr("库") + " ")
        + c.dim(_short_path(data_dir))
        + c.gray("   " + tr("帮助") + " ")
        + c.dim(tr("按 i 或运行 hy-mem help --json")),
    ]
    missing = _cfg_missing(cfg)
    if missing:
        # 配置不全时人一进来就该知道，而不是写完记忆才看到报错
        rows.append("  " + tui.icon("warn") + " " + c.yellow(tr("配置未完成：缺 {}", ", ".join(missing[:3])))
                    + c.dim("   " + tr("→ 按 s 进设置，或运行 hy-mem setup")))
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
        tui.err(tr("失败（exit {}）", code))
        return
    # 这里比对的是子进程的原始输出，保持中文匹配：它的文案不随界面语言变
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
        return 127, "", tr("无法启动子进程：{}", e)
    except subprocess.TimeoutExpired:
        return 124, "", tr("子命令 30 分钟未结束，已放弃等待")
    except OSError as e:
        return 126, "", tr("子进程启动失败：{}: {}", type(e).__name__, e)
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
def _add_round_title(n: int) -> str:
    # 第 1 条不带轮次：首次进入时「写记忆（第 1 条）」像是流程强加的编号，
    # 从第 2 条起才需要提示「还在同一个会话里」
    return tr("写记忆") if n <= 1 else tr("写记忆（第 {} 条）", n)


def _read_add_body() -> tuple[str, bool]:
    """读一段多行正文。返回 (正文, 是否被取消)。空行结束，与改造前一致。"""
    lines: list[str] = []
    while True:
        try:
            line = tui.line(c.gray("  │ "))
        except (EOFError, KeyboardInterrupt, tui.Aborted):
            print()
            return "\n".join(lines).strip(), True
        if line.strip() == "":
            return "\n".join(lines).strip(), False
        lines.append(line)


def _ask_write_again() -> bool:
    """问「再写一条？」。EOF / Esc / Ctrl+C 一律当作结束，别让循环卡死。"""
    try:
        return tui.confirm(tr("再写一条？"), default=True)
    except (EOFError, KeyboardInterrupt, tui.Aborted):
        print()
        return False


def page_add(dispatch, cfg: dict) -> None:
    """写记忆 —— 支持在一个会话里连着写多条。

    为什么不写完一条就回主界面：写记忆几乎从不只写一条（一次复盘往往要拆成
    四五条事实），每写一条都退出去、再进菜单、再等一次首屏，成本远超收益。
    循环之间**不清屏**：上一条的「已入队 / 已落库」反馈要留在屏幕上，否则用户
    看不到自己刚写的东西到底进没进库（这是 0.9.x 反复被反馈的疑点）。
    """
    round_no = 0
    while True:
        round_no += 1
        if round_no > 1:
            print()
            print(tui.hr())
        print(tui.title(_add_round_title(round_no)))
        print(c.dim(tr("  一行一条最好（混在一起会稀释向量）。空行结束；Ctrl+C 取消。")))
        print()

        # 取消（Esc/Ctrl+C/EOF）与「敲了空行」要区别对待：前者是想走，后者是
        # 手滑；只有后者才值得再问一句「再写一条？」
        text, cancelled = _read_add_body()
        if not text:
            tui.warn(tr("没有输入内容"))
            if cancelled or not _ask_write_again():
                return
            continue

        lines = text.splitlines()
        split = len(lines) > 1 and tui.confirm(
            tr("检测到 {} 行，拆成 {} 条独立记忆？", len(lines), len(lines)), default=True
        )

        tag_raw = tui.ask(
            tr("标签（逗号分隔，可留空）"),
            hint=tr("标签用于按类归档与过滤；pro 模式还会由 LLM 自动打 tag"),
        )
        tags = [t.strip() for t in tag_raw.replace("，", ",").split(",") if t.strip()]

        mode = str(cfg.get("mode") or "pro")
        hint = tr("pro 模式同步写入每条 8~40s，建议入队") if mode in ("pro", "ultra") else ""
        use_async = tui.confirm(
            tr("入队异步写入？") + (tr("（{}）", hint) if hint else tr("（推荐）")), default=True
        )

        argv = ["add"]
        if use_async:
            argv.append("--async")
        if split:
            argv.append("--split")
        for t in tags:
            argv += ["--tag", t]

        # 内容走 stdin，避免 cmd.exe 把多行参数压平
        print()
        tui.spinner_line(tr("写入中…"))
        code, out, err = dispatch(argv, stdin_text=text)
        raw = (out or err or "").strip()
        if code == 0:
            tui.ok(raw.splitlines()[-1] if raw else tr("已提交"))
            if use_async:
                # 异步写入只是入队，库里还没有它 —— 这一点必须说破。
                # 之前只说「已入队，用队列页跑 worker」，用户写完直接去检索，
                # 得到 0 条就以为记忆丢了。
                tui.note(tr("已入队，但还没进库 —— 此刻检索不到它是正常的。"))
                if tui.confirm(tr("现在就跑一次 worker 落库？（pro 模式每条约 10~50s）"), default=True):
                    tui.spinner_line(tr("落库中…"))
                    code2, out2, err2 = dispatch(["worker", "--once"])
                    text2 = (out2 or err2 or "").strip()
                    if code2 == 0:
                        tui.ok(text2.splitlines()[-1] if text2 else tr("已落库"))
                    else:
                        _show_failure(text2, code2)
        else:
            # 失败也留在会话里继续问：一条写坏了不该把「刚想好的另外三条」冲掉
            _show_failure(raw, code)

        # 输入阶段已经按过 Ctrl+C 的，写完这条就回主界面，不再追问
        if cancelled or not _ask_write_again():
            return


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
    print(tui.title(tr("检索")))
    q = tui.ask(tr("查询内容"))
    if not q:
        return
    tag_raw = tui.ask(tr("限定标签（逗号分隔，可留空）"))
    tags = [t.strip() for t in tag_raw.replace("，", ",").split(",") if t.strip()]
    n_raw = tui.ask(tr("返回条数"), default="5")
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
        tui.warn(tr("无命中"))
        tui.note(tr("可试着放宽：降低阈值（设置 → 检索阈值）或换更接近原话的问法"))
        return
    print(tui.table([tr("#"), tr("分数"), tr("内容"), tr("标签")], search_rows(results)))
    tui.note(tr(
        "阈值 {}，命中 {} 条；看全文：hy-mem get {}",
        (data or {}).get("min_score"),
        len(results),
        results[0].get("memory_id") or "<id>",
    ))


def page_list(dispatch, cfg: dict) -> None:
    print(tui.title(tr("最近记忆")))
    code, data, raw = _capture(dispatch, ["list", "-n", "15"])
    if code != 0:
        _show_failure(raw, code)
        return
    items = (data or {}).get("memories") or []
    if not items:
        tui.warn(tr("（空）"))
        return
    rows = []
    idw = 12
    content_w = max(24, tui.term_width() - idw - 2)
    for it in items:
        mid = str(it.get("memory_id") or it.get("id") or "?")
        body = it.get("content") or it.get("new_memory") or it.get("text") or ""
        rows.append([mid[:idw], "\n".join(tui.wrap(body, content_w))])
    print(tui.table(["id", tr("内容")], rows, footer=tr("共 {} 条；删除用 hy-mem delete <id>", len(items))))


def page_tags(dispatch, cfg: dict) -> None:
    print(tui.title(tr("标签")))
    code, data, raw = _capture(dispatch, ["tags"])
    if code != 0:
        _show_failure(raw, code)
        return
    inv = (data or {}).get("tags") or (data or {}).get("inventory") or []
    if not inv:
        tui.warn(tr("（还没有标签）"))
        return
    rows = [[t.get("tag", "?"), str(t.get("n", 0)), str(t.get("user_n", 0)), str(t.get("auto_n", 0))] for t in inv]
    print(tui.table([tr("标签"), tr("合计"), tr("手工"), tr("自动")], rows))

    print()
    action = tui.select(
        tr("要做什么？"),
        [
            (tr("从记忆同步自动标签"), tr("把 SDK 侧 LLM 生成的 tag 拉进本地索引"), "s"),
            (tr("删除某个标签"), tr("只解绑，不删记忆"), "d"),
            (tr("返回"), "", "x"),
        ],
        default=2,
    )
    if action == 0:
        code, _d, raw = _capture(dispatch, ["tags", "--refresh"])
        (tui.ok if code == 0 else tui.err)(raw.strip().splitlines()[-1] if raw.strip() else tr("完成"))
    elif action == 1:
        tag = tui.ask(tr("要删除的标签名"))
        if tag and tui.confirm(tr("解绑所有 {} 标签？", tag), default=False):
            code, _d, raw = _capture(dispatch, ["untag", "--tag", tag, "--yes"])
            (tui.ok if code == 0 else tui.err)(raw.strip().splitlines()[-1] if raw.strip() else tr("完成"))


def page_queue(dispatch, cfg: dict) -> None:
    print(tui.title(tr("异步队列")))
    code, data, raw = _capture(dispatch, ["queue"])
    if code != 0:
        _show_failure(raw, code)
        return
    if isinstance(data, dict):
        pairs = [
            (tr("待处理"), str(data.get("pending", 0))),
            (tr("处理中"), str(data.get("running", 0))),
            (tr("已完成"), str(data.get("done", 0))),
            (tr("死信"), str(data.get("dead", 0))),
        ]
        if data.get("oldest_pending_age_s"):
            pairs.append((tr("最老待处理"), f"{data['oldest_pending_age_s']:.0f}s"))
        print(tui.kv(pairs))

    print()
    action = tui.select(
        tr("操作"),
        [
            (tr("处理队列直到清空"), tr("跑 worker，逐条落库（pro 每条 8~40s）"), "a"),
            (tr("只处理一条"), tr("处理 1 条后返回"), "o"),
            (tr("重投死信任务"), tr("把 dead 任务放回待处理"), "r"),
            (tr("返回"), "", "x"),
        ],
        default=3,
    )
    if action == 0:
        tui.note(tr("开始消费；Ctrl+C 可中断（已入队的任务不会丢）"))
        tui.spinner_line(tr("消费中…"))
        _run_and_show(dispatch, ["worker", "--idle-exit", "5"])
    elif action == 1:
        tui.spinner_line(tr("处理一条…"))
        _run_and_show(dispatch, ["worker", "--once", "--max", "1"])
    elif action == 2:
        if tui.confirm(tr("把状态为 dead 的任务全部重投？"), default=False):
            _run_and_show(dispatch, ["retry", "dead"])


_LEVEL_STYLE = {"OK": ("ok", "green"), "WARN": ("warn", "yellow"), "CRIT": ("err", "red")}


def page_health(dispatch, cfg: dict) -> None:
    print(tui.title(tr("健康检查")))
    tui.spinner_line(tr("扫描库体积与条数…"))
    code, data, raw = _capture(dispatch, ["stats", "--health"])
    if code != 0 or not isinstance(data, dict):
        tui.err((raw or "").strip()[:400] or tr("失败"))
        return
    verdict = data.get("verdict", "?")
    ico, col = _LEVEL_STYLE.get(verdict, ("dot", "gray"))
    print(tr("  结论 {}   ", getattr(c, col)(tui.icon(ico) + " " + verdict))
          + c.dim(tr("{} 条 / {} MB / 单条预期 {}s",
                     data.get("memory_count"),
                     data.get("size_mb"),
                     data.get("estimated_write_latency_s"))))
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
    print(tui.table(["", tr("项"), tr("值"), tr("说明")], rows))
    advice = data.get("advice") or []
    if advice:
        print()
        print(tui.section(tr("建议")))
        for i, t in enumerate(advice, 1):
            print(f"  {i}. {t}")


# --------------------------------------------------------------------------
# 设置中心
# --------------------------------------------------------------------------
def _emb_line(cfg: dict) -> str:
    emb = cfg.get("embedding") or {}
    return tr("{} · {} 维 · {}", emb.get("model"), emb.get("dims"), emb.get("provider"))


def _llm_line(cfg: dict) -> str:
    llm = cfg.get("llm") or {}
    if cfg.get("mode") == "lite":
        return tr("lite 模式不需要")
    return f"{llm.get('model')}"


def _storage_line(cfg: dict) -> str:
    p = str((cfg.get("storage") or {}).get("dataDir") or "")
    size, files = storage_ops.dir_usage(p)
    if files:
        return tr("{}  ({}/{} 文件)", _short_path(p, 40), storage_ops.human_size(size), files)
    return tr("{}  （空）", _short_path(p, 40))


def page_storage(dispatch, cfg: dict) -> None:
    while True:
        cfg = cfgmod.load()
        data_dir = str((cfg.get("storage") or {}).get("dataDir") or "")
        tui.clear()
        print(tui.title(tr("数据存储目录")))
        print(tui.kv(storage_ops_usage_rows(data_dir)))
        print()
        action = tui.select(
            tr("要做什么？"),
            [
                (tr("换目录 / 迁移数据"), tr("复制现有数据到新位置并切换"), "m"),
                (tr("恢复到默认目录"), str(cfgmod.default_data_dir()), "d"),
                (tr("备份一份"), tr("复制成 data.bak-时间戳，改模型前用"), "b"),
                (tr("清空重建"), tr("先自动备份，再清空（重建索引/换维度后使用）"), "w"),
                (tr("清理队列库"), tr("删除已完成任务记录并 VACUUM，只影响队列不影响记忆"), "q"),
                (tr("返回"), "", "x"),
            ],
            default=5,
        )
        if action == 5:
            return
        if action == 4:
            if tui.confirm(tr("清理 jobs 表里已完成的记录并 VACUUM？（不动记忆）"), default=False):
                code, _d, raw = _capture(dispatch, ["stats", "--health", "--vacuum"])
                (tui.ok if code == 0 else tui.err)(tr("队列记录已清理") if code == 0 else raw.strip()[:300])
            tui.pause(tr("按任意键继续…"))
            continue
        if action == 2:
            ok, msg = storage_ops.backup(data_dir)
            (tui.ok if ok else tui.err)(msg)
            tui.pause(tr("按任意键继续…"))
            continue
        if action == 3:
            if not tui.confirm(tr("先备份再清空数据目录？清空后记忆需要重新写入"), default=False):
                continue
            ok, msg = storage_ops.wipe(data_dir)
            (tui.ok if ok else tui.err)(msg)
            tui.pause(tr("按任意键继续…"))
            continue

        target = ""
        if action == 0:
            raw = tui.ask(tr("新目录"), hint=tr("留空取消；支持 %USERPROFILE% 与 ~"))
            if not raw:
                continue
            target = storage_ops.normalize_path(raw)
            good, msg = storage_ops.check_target(target)
            if not good:
                tui.err(msg)
                tui.pause(tr("按任意键继续…"))
                continue
        else:
            target = str(cfgmod.default_data_dir())
        if target == data_dir:
            tui.info(tr("与当前目录相同，无需切换"))
            tui.pause(tr("按任意键继续…"))
            continue

        size, files = storage_ops.dir_usage(data_dir)
        if files and tui.confirm(
            tr("把现有数据（{} / {} 文件）迁移过去？", storage_ops.human_size(size), files),
            default=True,
        ):
            tui.spinner_line(tr("复制中…"))
            ok, msg = storage_ops.migrate(data_dir, target)
            (tui.ok if ok else tui.err)(msg)
            if not ok and not tui.confirm(tr("迁移失败，仍然切换？"), default=False):
                tui.pause(tr("按任意键继续…"))
                continue
        cfg["storage"]["dataDir"] = target
        cfgmod.save(cfg)
        tui.ok(tr("存储目录已切到 {}", target))
        tui.pause(tr("按任意键继续…"))


def storage_ops_usage_rows(data_dir: str) -> list[tuple[str, str]]:
    size, files = storage_ops.dir_usage(data_dir)
    free = storage_ops.free_space(data_dir)
    rows = [(tr("当前目录"), data_dir)]
    rows.append((tr("已占用"), tr("{} / {} 个文件", storage_ops.human_size(size), files) if files
                 else tr("空目录")))
    if free:
        rows.append((tr("所在盘可用"), storage_ops.human_size(free)))
    rows.append((tr("默认目录"), str(cfgmod.default_data_dir())))
    return rows


def page_identity(dispatch, cfg: dict) -> None:
    """身份页：多智能体共享还是隔离，只在这一处决定。"""
    while True:
        cfg = cfgmod.load()
        cur = cfgmod.identity_defaults(cfg)
        src = cfgmod.identity_source(cfg)
        tui.clear()
        print(tui.title(tr("身份 user_id")))
        print(tui.kv(
            [
                (tr("当前身份"), cfgmod.identity_label(cur)),
                (tr("生效来源"), {
                    "env": tr("环境变量 HY_MEM_USER_ID"),
                    "config": "config.json",
                    "general": tr("未设置 → 通用"),
                }.get(src, src)),
                (tr("配置文件"), str(cfgmod.config_path())),
            ]
        ))
        print()
        general = tr("通用（default）")
        kw = tui.vwidth(general) + 1
        print(tui.panel([
            c.cyan(tui.lpad(general, kw)) + tr("所有智能体共享同一份记忆 —— 不设 user_id 的默认行为"),
            c.cyan(tui.lpad(tr("其它名字"), kw)) + tr("自成一套记忆，与别处互不可见，用于隔离"),
            "",
            c.dim(tr("切换只改身份：模型配置、数据目录、已有记忆都不动；")),
            c.dim(tr("原身份的记忆不会删，只是现在看不到，切回去就能看到。")),
        ]))
        print()
        action = tui.select(
            tr("要做什么？"),
            [
                (tr("改成另一个 user_id"), tr("为这个智能体单独隔离一份记忆"), "s"),
                (tr("回到通用（default）"), tr("与其它智能体共享"), "r"),
                (tr("查看见过哪些身份"), tr("只读"), "l"),
                (tr("返回"), "", "x"),
            ],
            default=3,
        )
        if action == 3:
            return
        if action == 2:
            known = [str(x) for x in ((cfg.get("identity") or {}).get("known") or []) if str(x).strip()]
            if not known:
                tui.note(tr("（还没有记录到别的身份）"))
            else:
                print(tui.table(["user_id", tr("当前")], [[u, "←" if u == cur else ""] for u in known]))
            tui.pause(tr("按任意键继续…"))
            continue
        if action == 1:
            if not tui.confirm(tr("回到通用（{}）？", cfgmod.GENERAL_USER_ID), default=True):
                continue
            _path, eff = cfgmod.set_user_id("")
            tui.ok(tr("已切到 {}", cfgmod.identity_label(eff)))
            tui.pause(tr("按任意键继续…"))
            continue
        uid = tui.ask(tr("新的 user_id"), hint=tr("英文/数字/-/_ 最稳；留空取消"))
        if not uid.strip():
            continue
        _path, eff = cfgmod.set_user_id(uid.strip())
        tui.ok(tr("已切到 {}", cfgmod.identity_label(eff)))
        tui.note(tr("{} 的记忆没有删，切回去仍能看到", cfgmod.identity_label(cur)))
        tui.pause(tr("按任意键继续…"))


def page_log(dispatch, cfg: dict) -> None:
    """操作日志页：只读回放最近 50 条，唯一动作是清空（需二次确认）。

    刻意不在这里跑任何子命令：日志页出问题时最不该依赖「再起一个子进程」——
    用户点进来往往正是因为别的操作刚刚失败。
    """
    import oplog

    while True:
        tui.clear()
        print(tui.title(tr("操作日志")))
        try:
            p = oplog.path()
            lines = oplog.recent(oplog.MAX_KEEP)
        except Exception as e:  # noqa: BLE001
            tui.err(tr("读取失败：{}", f"{type(e).__name__}: {e}"))
            return
        print(c.dim("  " + str(p)))
        print()
        if not lines:
            tui.note(tr("（还没有操作记录）"))
        else:
            # 摘要列吃掉剩余宽度后折行：错误提示常常很长，截断就看不出原因
            fixed = 8 + 10 + 5 + 6 + 3 * 2
            sum_w = max(20, tui.term_width() - fixed - 4)
            rows = []
            for ln in reversed(lines):  # 最新的在最上面
                e = oplog.parse(ln)
                rows.append([
                    (e["time"][11:] or "-"), e["cmd"], e["status"], e["duration"],
                    "\n".join(tui.wrap(e["summary"], sum_w)),
                ])
            print(tui.table(
                [tr("时间"), tr("命令"), tr("状态"), tr("耗时"), tr("摘要")],
                rows,
                footer=tr("共 {} 条（只保留最近 {} 条）；文件：{}", len(lines), oplog.MAX_KEEP, p),
            ))
        print()
        action = tui.select(
            tr("要做什么？"),
            [
                (tr("清空日志"), tr("删除全部记录，不可恢复"), "c"),
                (tr("返回"), "", "x"),
            ],
            default=1,
        )
        if action != 0:
            return
        if not tui.confirm(tr("清空全部 {} 条操作日志？", len(lines)), default=False):
            continue
        try:
            oplog.clear()
            tui.ok(tr("日志已清空"))
        except Exception as e:  # noqa: BLE001
            tui.err(tr("清空失败：{}", f"{type(e).__name__}: {e}"))
        tui.pause(tr("按任意键继续…"))


def page_settings(dispatch, cfg: dict) -> None:
    import setup_wizard

    while True:
        cfg = cfgmod.load()
        tui.clear()
        print(tui.title(tr("设置")))
        print(tui.kv(
            [
                (tr("身份 user_id"), cfgmod.identity_label(cfgmod.identity_defaults(cfg))),
                (tr("向量模型"), _emb_line(cfg)),
                (tr("运行模式"), f"{cfg.get('mode')}  ({tr(_MODE_DESC.get(str(cfg.get('mode')), ''))})"),
                (tr("LLM 模型"), _llm_line(cfg)),
                (tr("存储目录"), _storage_line(cfg)),
                (tr("检索阈值"), str((cfg.get("search") or {}).get("minScore"))),
                (tr("界面语言"), label()),
            ]
        ))
        print()
        action = tui.select(
            tr("改哪一项？"),
            [
                (tr("向量模型（embedding）"), tr("改后会实测维度；换模型需要重建数据目录"), "e"),
                (tr("运行模式 MEMORY_MODE"), tr("lite 纯向量 / pro 加 LLM 抽取 / ultra 再加 Schema"), "m"),
                (tr("LLM 模型"), tr("pro 及以上用于抽取事实与自动打标签"), "l"),
                (tr("数据存储目录（storage）"), tr("查看用量、迁移、备份、清空重建"), "d"),
                (tr("检索阈值 minScore"), tr("当前 {}", cfg.get("search", {}).get("minScore")), "t"),
                (tr("队列参数"), tr("重试次数与锁超时，一般不用动"), "q"),
                (tr("身份 user_id"), tr("多智能体共享（通用）还是隔离（独立名字）"), "u"),
                (tr("完整向导"), tr("从向量模型开始重走一遍四步"), "f"),
                (tr("查看当前配置"), tr("只读：所有生效值与环境变量映射"), "v"),
                (tr("环境自检 doctor"), tr("端到端连通性与依赖检查"), "c"),
                (tr("操作日志"), tr("最近 50 条执行记录，可清空"), "o"),
                (tr("界面语言 / Language"), tr("当前 {}；切换后立即重绘菜单", label()), "n"),
                (tr("返回"), "", "x"),
            ],
            default=12,
        )
        if action == 12:
            return
        if action == 11:
            # 只写 config.json 的 lang 字段，立刻重画：切完还得再退出去重进才能生效，
            # 用户会以为没切成功
            new = "en" if lang() == "zh" else "zh"
            try:
                set_lang(new)
            except Exception as e:  # noqa: BLE001
                tui.err(tr("切换失败：{}", f"{type(e).__name__}: {e}"))
            else:
                # 环境变量优先级高于配置：HY_MEM_LANG 在外部固定时，写配置也看不出变化，
                # 必须说清楚，否则用户会反复点这一项
                if lang() != new:
                    tui.warn(tr("环境变量 HY_MEM_LANG 覆盖了配置，界面语言仍是 {}；"
                                "取消该变量后再切换", label()))
                else:
                    tui.ok(tr("界面语言已切换为 {}，菜单文案立即生效。", label()))
            tui.pause(tr("按任意键继续…"))
            continue
        if action == 10:
            page_log(dispatch, cfg)
            continue
        if action == 9:
            page_doctor(dispatch, cfg)
            tui.pause(tr("按任意键继续…"))
            continue
        if action == 8:
            page_config(dispatch, cfg)
            tui.pause(tr("按任意键继续…"))
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
            tui.warn(tr("已取消"))
        tui.pause(tr("按任意键继续…"))


def page_doctor(dispatch, cfg: dict) -> None:
    print(tui.title(tr("环境自检")))
    _run_and_show(dispatch, ["doctor"])


def page_config(dispatch, cfg: dict) -> None:
    print(tui.title(tr("当前配置")))
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    pairs = [
        (tr("配置文件"), str(cfgmod.config_path())),
        (tr("首次设置"), tr("已完成") if cfg.get("setupComplete") else tr("未完成")),
        (tr("身份 user_id"), cfgmod.identity_label(cfgmod.identity_defaults(cfg))
         + tr("（来源：{}）", cfgmod.identity_source(cfg))),
        (tr("运行模式"), str(cfg.get("mode"))),
        (tr("向量模型"), f"{emb.get('model')} ({tr('{} 维', emb.get('dims'))}, {emb.get('provider')})"),
        (tr("嵌入入口"), str(emb.get("baseUrl") or tr("（本地 ONNX）"))),
        (tr("嵌入密钥"), tr("已设置") if emb.get("apiKey") else tr("未设置")),
        (tr("LLM 模型"), f"{llm.get('model')} @ {llm.get('baseUrl')}"),
        (tr("LLM 密钥"), (tr("已设置") + (tr("（来自 ${}）", llm.get("keyEnv")) if not llm.get("apiKey") else ""))
         if cfgmod.resolve_llm_key(cfg) else tr("未设置")),
        (tr("存储目录"), str((cfg.get("storage") or {}).get("dataDir"))),
        (tr("检索阈值"), str((cfg.get("search") or {}).get("minScore"))),
        (tr("队列"), tr("重试 {} 次 / 锁超时 {}s",
                       cfg.get("queue", {}).get("maxAttempts"),
                       cfg.get("queue", {}).get("lockTimeout"))),
        (tr("界面语言"), label()),
    ]
    print(tui.kv(pairs))
    print()
    tui.note(tr("改这些走「设置」；直接改 config.json 也行，但下次运行才生效。"))


def page_help(dispatch, cfg: dict) -> None:
    """把命令清单直接摆在界面上 —— 不然没人知道有 help，也不知道 agent 该读哪份规格。"""
    print(tui.title(tr("帮助 / 命令清单")))
    _run_and_show(dispatch, ["help"])
    print()
    tui.note(tr("机器可读规格：hy-mem help --json （参数类型 / 默认值 / 返回字段 / 示例）"))
    tui.note(tr("身份切换：hy-mem user set <名字> —— 不设就是「通用」，多个智能体共享同一份记忆"))
    tui.note(tr("接入新宿主：hy-mem install —— 把启动器写到固定目录，让 hy-mem 在 PATH 上真实存在"))
    tui.note(tr("按键自检：hy-mem tui-test —— 方向键没反应时先跑它"))


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
    ("操作日志", "最近 50 条执行记录，可清空", page_log, "o"),
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
        # 每次渲染才翻译：设置页切语言后回到这里必须立刻变英文
        options = [(tr(name), tr(detail), key) for name, detail, _fn, key in MENU]
        options.append((tr("退出"), tr("Esc / q 也可以退出"), "x"))
        try:
            idx = tui.select(tr("选择操作"), options, default=0)
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
            tui.warn(tr("已取消"))
        except KeyboardInterrupt:
            print()
            tui.warn(tr("已中断"))
        except SystemExit:
            # 子命令里 argparse 的用法错误会抛 SystemExit；在交互界面里它只应
            # 表现为「这次没执行成功」，不能把整个主界面带走。
            tui.warn(tr("该操作未执行（参数不合法）"))
        except Exception as e:  # noqa: BLE001
            tui.err(tr("{} 出错：{}: {}", tr(name), type(e).__name__, e))
        tui.pause(tr("按任意键继续…"))
