"""交互式控制台：无参数运行 hy-mem 时进入.

形态按 **bash / 现代 agent CLI** 的习惯来，不是「一层层箭头菜单」：

  · 一个常驻提示符 `hy-mem ›`，敲命令干活：`add`、`search 部署流程`、`queue run`；
  · ↑↓ 翻历史（存在 `<dataDir>/shell-history`）、Tab 补全命令与子命令、
    Ctrl+R 反查、Ctrl+L 清屏、Ctrl+D 退出 —— 全部在 `tui._line_edit` 里实现；
  · 结果直接打在滚动区：**不清屏、不「按任意键继续」**。上一条结果往上滚就能看，
    不再需要为了多看一眼而重进页面；
  · 一行一条命令，参数用旗标（`-n 10`、`-t 标签`）表达，与 `hy-mem` 子命令一一对应。

为什么推翻旧版（0.9.13 及以前）：
  旧版每做一件事都要「进页面 → 选一项 → 做完 → 按任意键 → 回主菜单 → 再选」，
  一次会话里回车键有一半花在导航上；而且 `tui.clear()` 会把上一次的结果擦掉，
  想对照两条检索结果只能重查。用户的原话是「很多地方反人类设计」。

三个硬约定：
  1. **每个动作都能用一条命令完成**，且命令名 = 子命令名（`add` / `search` / …），
     不会出现「菜单里做的事命令行做不了」。
  2. **非 TTY 自动降级**（管道 / 测试 / agent 调用）：同一套命令、同一套输出，
     逐行读 stdin。所以 `scripts/test_menu.py` 能把每个命令都跑一遍。
  3. 执行仍走**子进程** dispatch（见 `subprocess_dispatch`）：后台角色改 env
     只影响它自己，控制台进程永远干净。

文案统一走 i18n.tr()：键是中文原文，默认语言下原样返回；切到 en 才查词典。
翻译发生在渲染那一刻，所以 `lang en` 之后下一行提示就是英文。
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import config as cfgmod
import storage_ops
import tui
from i18n import label, lang, set_lang, tr
from tui import c

HISTORY_NAME = "shell-history"
HISTORY_MAX = 300


# --------------------------------------------------------------------------
# 与子命令打交道的底座
# --------------------------------------------------------------------------
def _short_path(p: str, width: int = 40) -> str:
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

    控制台要靠结构化结果做表格；直接让子命令往终端打印会打乱版面。
    """
    # --json 必须前置：argparse 的全局选项放在子命令之后会解析失败
    code, out, err = dispatch(["--json"] + argv)
    return code, _parse_json(out), (out or err)


def _run_and_show(dispatch, argv: list[str]) -> int:
    """跑子命令并把结果摆到屏幕上（子进程的 stdout 不再自动可见）。"""
    code, out, err = dispatch(argv)
    text = (out or err or "").strip()
    if code == 0:
        if text:
            print(text)
    else:
        _show_failure(text, code)
    return code


# --------------------------------------------------------------------------
# 历史：存在数据目录里，跟着身份/库走
# --------------------------------------------------------------------------
def _data_dir() -> Path:
    raw = str((cfgmod.load().get("storage") or {}).get("dataDir") or "").strip()
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else cfgmod.default_data_dir()


def history_path() -> Path:
    return _data_dir() / HISTORY_NAME


def load_history() -> list[str]:
    """读历史；读不到就当没有 —— 历史文件坏掉不能让人进不了控制台。"""
    try:
        raw = history_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [x for x in raw.splitlines() if x.strip()][-HISTORY_MAX:]


def append_history(line: str) -> None:
    """追加一行。只存命令行本身，正文（add 的多行内容）不进来。

    连续重复的命令不重复记：翻历史时最烦的就是连按几次 ↑ 都是同一条。
    """
    text = line.strip().replace("\n", " ")[:200]
    if not text:
        return
    try:
        old = load_history()
        if old and old[-1] == text:
            return
        p = history_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
        if len(old) + 1 > HISTORY_MAX * 2:  # 偶尔瘦身，别让文件无限长
            keep = (old + [text])[-HISTORY_MAX:]
            p.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        pass  # 历史写不进去不影响干活


# --------------------------------------------------------------------------
# 参数切分 / 旗标解析
# --------------------------------------------------------------------------
def split_args(s: str) -> list[str]:
    """按 shell 习惯切词：空格分隔，支持 "…" / '…'，反斜杠原样保留。

    不用 shlex：posix 模式会吃掉 Windows 路径里的反斜杠，非 posix 模式又不认
    引号，两种都会让 `storage move C:\\data\\mem` 变成乱码路径。
    """
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in s:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def parse_flags(
    items: list[str],
    value_opts: dict[str, str] | None = None,
    bool_opts: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str], str]:
    """解析 `-n 10` / `--tag=a,b` / `-s` 这类旗标，返回 (选项, 位置参数, 错误)。

    手写而不用 argparse：这里要支持「参数顺序随意 + 未知选项给出人话提示」，
    而 argparse 的报错会直接 `sys.exit(2)` —— 在控制台里那等于把会话踢掉。
    """
    value_opts = value_opts or {}
    bool_opts = bool_opts or {}
    opts: dict[str, str] = {}
    pos: list[str] = []
    i = 0
    while i < len(items):
        tok = items[i]
        if tok == "--":
            pos.extend(items[i + 1:])
            break
        if tok in bool_opts:
            opts[bool_opts[tok]] = "1"
            i += 1
            continue
        name, sep, val = tok.partition("=")
        if tok in value_opts:
            if i + 1 >= len(items):
                return opts, pos, tr("{} 后面要跟一个值", tok)
            opts[value_opts[tok]] = items[i + 1]
            i += 2
            continue
        if sep and name in value_opts:
            opts[value_opts[name]] = val
            i += 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            return opts, pos, tr("不认识的选项 {}", tok)
        pos.append(tok)
        i += 1
    return opts, pos, ""


def _tags_of(raw: str) -> list[str]:
    return [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]


# --------------------------------------------------------------------------
# 命令表
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    summary: str
    run: Callable[["Shell", list[str]], int]
    aliases: tuple[str, ...] = ()
    words: tuple[str, ...] = ()          # 参数补全词（子命令 / 常用选项）
    group: str = "常用"


class ExitShell(Exception):
    """`exit` / `quit`：正常退出，不是错误。"""


class Shell:
    """控制台状态：只持有 dispatch、配置快照和历史，不碰任何业务逻辑。"""

    def __init__(self, dispatch, cfg: dict | None = None) -> None:
        self.dispatch = dispatch
        self.cfg = cfg if cfg is not None else cfgmod.load()
        self.history = load_history()

    def reload(self) -> dict:
        """每条命令后重读配置：`setup` / `lang` / `identity set` 都会改它。"""
        try:
            self.cfg = cfgmod.load()
        except Exception:  # noqa: BLE001  配置读坏了也要能继续用控制台
            pass
        return self.cfg

    def capture(self, argv: list[str]) -> tuple[int, object, str]:
        return _capture(self.dispatch, argv)

    def run(self, argv: list[str]) -> int:
        return _run_and_show(self.dispatch, argv)

    def remember(self, line: str) -> None:
        self.history.append(line)
        append_history(line)

    def complete(self, text: str) -> list[str]:
        """Tab 补全：命令名 / 子命令 / 常用选项。

        只补行尾（bash 默认行为）。返回的是**整行替换文本**，行编辑器直接换上，
        省得它还要理解 token 边界。
        """
        stripped = text.lstrip()
        if not stripped:
            return []
        parts = stripped.split()
        if text.endswith(" ") or (text and text[-1] in "\t"):
            head, prefix = parts, ""
        else:
            head, prefix = parts[:-1], parts[-1]
        if not head:
            pool = []
            for cmd in COMMANDS:
                for name in (cmd.name, *cmd.aliases):
                    if name.startswith(prefix):
                        pool.append(name)
            return pool
        cmd = find_command(head[0])
        if cmd is None or not cmd.words:
            return []
        return [" ".join(head + [w]) for w in cmd.words if w.startswith(prefix)]

    def execute(self, line: str) -> int:
        items = split_args(line)
        if not items:
            return 0
        cmd = find_command(items[0])
        if cmd is None:
            self._unknown(items[0])
            return 2
        try:
            return int(cmd.run(self, items[1:]) or 0)
        except (tui.Aborted, KeyboardInterrupt):
            # 命令里的确认框被 Esc / Ctrl+C / EOF 取消：只取消这一次操作，
            # 会话继续。放在这里统一收口，省得每个命令各写一遍 try。
            print()
            tui.warn(tr("已取消"))
            return 0

    def _unknown(self, word: str) -> None:
        pool = [n for cmd in COMMANDS for n in (cmd.name, *cmd.aliases)]
        near = difflib.get_close_matches(word, pool, n=3, cutoff=0.55)
        tui.err(tr("未知命令 {}", word))
        if near:
            tui.note(tr("是不是要 {}？", " / ".join(near)))
        tui.note(tr("输入 help 看全部命令；Tab 可以补全"))


# --------------------------------------------------------------------------
# 命令实现：写 / 读
# --------------------------------------------------------------------------
def _read_body() -> tuple[str, bool]:
    """读一段多行正文。返回 (正文, 是否被取消)。空行结束。"""
    print(c.dim(tr("  多行输入，空行结束；Ctrl+C 取消这条")))
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


def cmd_add(sh: Shell, items: list[str]) -> int:
    """写记忆。默认入队异步写（`add --async` 已自动拉起消费者，不再多问一句）。

    为什么不问「拆条吗 / 打标签吗 / 要异步吗」：
      · 拆条：命令行的默认就是「一段话 = 一条记忆」，要多条用 `-s`；
      · 标签：`-t` 直接给；
      · 异步：pro 模式同步写一条 8~40s，没人想等；改同步用 `--sync`。
    三个问题问下来要按三次回车，而九成的写入用的是同一组答案。
    """
    opts, rest, bad = parse_flags(
        items,
        {"-t": "tags", "--tag": "tags"},
        {"-s": "split", "--split": "split", "--sync": "sync"},
    )
    if bad:
        tui.err(bad)
        return 2
    text = " ".join(rest).strip()
    if not text:
        text, cancelled = _read_body()
        if not text:
            if not cancelled:
                tui.note(tr("（没有输入内容，什么都没写）"))
            return 0
    tags = _tags_of(str(opts.get("tags") or ""))
    argv = ["add"]
    if not opts.get("sync"):
        argv.append("--async")
    if opts.get("split"):
        argv.append("--split")
    for t in tags:
        argv += ["--tag", t]

    print()
    tui.spinner_line(tr("写入中…"))
    code, out, err = sh.dispatch(argv, stdin_text=text)
    raw = (out or err or "").strip()
    if code == 0:
        tui.ok(raw.splitlines()[-1] if raw else tr("已提交"))
        if not opts.get("sync"):
            tui.note(tr("已入队，后台消费者会自动落库；此刻 search 还查不到是正常的（pro 每条约 10~50s，`queue` 看进度）"))
    else:
        _show_failure(raw, code)
    return code


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


def cmd_search(sh: Shell, items: list[str]) -> int:
    opts, rest, bad = parse_flags(items, {"-n": "n", "--num": "n", "-t": "tags", "--tag": "tags"})
    if bad:
        tui.err(bad)
        return 2
    q = " ".join(rest).strip()
    if not q:
        tui.warn(tr("用法：search <查询> [-n 条数] [-t 标签]"))
        tui.note(tr("例：search 部署流程 -n 10"))
        return 2
    try:
        n = max(1, int(str(opts.get("n") or 5)))
    except ValueError:
        n = 5
    argv = ["search", q, "-n", str(n)]
    for t in _tags_of(str(opts.get("tags") or "")):
        argv += ["--tag", t]

    print()
    code, data, raw = sh.capture(argv)
    if code != 0:
        _show_failure(raw, code)
        return code
    results = (data or {}).get("results") or []
    if not results:
        tui.warn(tr("无命中"))
        tui.note(tr("可试着放宽：降低阈值（setup search）或换更接近原话的问法"))
        return 0
    print(tui.table([tr("#"), tr("分数"), tr("内容"), tr("标签")], search_rows(results)))
    tui.note(tr(
        "阈值 {}，命中 {} 条；看全文：get {}",
        (data or {}).get("min_score"),
        len(results),
        results[0].get("memory_id") or "<id>",
    ))
    return 0


def cmd_list(sh: Shell, items: list[str]) -> int:
    opts, rest, bad = parse_flags(items, {"-n": "n", "--num": "n"})
    if bad:
        tui.err(bad)
        return 2
    try:
        n = max(1, int(str(opts.get("n") or (rest[0] if rest else 15))))
    except ValueError:
        n = 15
    code, data, raw = sh.capture(["list", "-n", str(n)])
    if code != 0:
        _show_failure(raw, code)
        return code
    mem = (data or {}).get("memories") or []
    if not mem:
        tui.warn(tr("（空）"))
        return 0
    rows = []
    idw = 12
    content_w = max(24, tui.term_width() - idw - 2)
    for it in mem:
        mid = str(it.get("memory_id") or it.get("id") or "?")
        body = it.get("content") or it.get("new_memory") or it.get("text") or ""
        rows.append([mid[:idw], "\n".join(tui.wrap(body, content_w))])
    print(tui.table(["id", tr("内容")], rows, footer=tr("共 {} 条；删除用 delete <id>", len(mem))))
    return 0


def cmd_get(sh: Shell, items: list[str]) -> int:
    if not items:
        tui.warn(tr("用法：get <id>（id 从 list / search 的结果里取）"))
        return 2
    return sh.run(["get", items[0]])


def cmd_delete(sh: Shell, items: list[str]) -> int:
    if not items:
        tui.warn(tr("用法：delete <id>；批量删用 forget"))
        return 2
    if not tui.confirm(tr("删除 {}？旧版本会一并消失", items[0]), default=False):
        tui.note(tr("已取消"))
        return 0
    return sh.run(["delete", items[0], "--yes"])


def cmd_tags(sh: Shell, items: list[str]) -> int:
    sub = items[0] if items else ""
    if sub == "refresh":
        code, _d, raw = sh.capture(["tags", "--refresh"])
        (tui.ok if code == 0 else tui.err)(raw.strip().splitlines()[-1] if raw.strip() else tr("完成"))
        return code
    if sub in ("rm", "del", "remove"):
        tag = items[1] if len(items) > 1 else ""
        if not tag:
            tui.warn(tr("用法：tags rm <标签>"))
            return 2
        if not tui.confirm(tr("解绑所有 {} 标签？（不删记忆）", tag), default=False):
            tui.note(tr("已取消"))
            return 0
        code, _d, raw = sh.capture(["untag", "--tag", tag, "--yes"])
        (tui.ok if code == 0 else tui.err)(raw.strip().splitlines()[-1] if raw.strip() else tr("完成"))
        return code

    code, data, raw = sh.capture(["tags"])
    if code != 0:
        _show_failure(raw, code)
        return code
    inv = (data or {}).get("tags") or (data or {}).get("inventory") or []
    if not inv:
        tui.warn(tr("（还没有标签）"))
        return 0
    rows = [[t.get("tag", "?"), str(t.get("n", 0)), str(t.get("user_n", 0)), str(t.get("auto_n", 0))] for t in inv]
    print(tui.table([tr("标签"), tr("合计"), tr("手工"), tr("自动")], rows))
    tui.note(tr("同步自动标签：tags refresh ｜ 解绑：tags rm <标签>"))
    return 0


def cmd_queue(sh: Shell, items: list[str]) -> int:
    sub = items[0] if items else ""
    if sub in ("run", "drain"):
        tui.note(tr("开始消费；Ctrl+C 可中断（已入队的任务不会丢）"))
        return sh.run(["worker", "--idle-exit", "5"])
    if sub in ("one", "step"):
        return sh.run(["worker", "--once", "--max", "1"])
    if sub in ("retry", "redeliver"):
        if not tui.confirm(tr("把状态为 dead 的任务全部重投？"), default=False):
            tui.note(tr("已取消"))
            return 0
        return sh.run(["retry", "dead"])

    code, data, raw = sh.capture(["queue"])
    if code != 0:
        _show_failure(raw, code)
        return code
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
    tui.note(tr("消费：queue run（直到清空）/ queue one（只处理一条）/ queue retry（重投死信）"))
    return 0


_LEVEL_STYLE = {"OK": ("ok", "green"), "WARN": ("warn", "yellow"), "CRIT": ("err", "red")}


def cmd_stats(sh: Shell, items: list[str]) -> int:
    tui.spinner_line(tr("扫描库体积与条数…"))
    code, data, raw = sh.capture(["stats", "--health"])
    if code != 0 or not isinstance(data, dict):
        tui.err((raw or "").strip()[:400] or tr("失败"))
        return code or 1
    verdict = data.get("verdict", "?")
    ico, col = _LEVEL_STYLE.get(verdict, ("dot", "gray"))
    print(tr("  结论 {}   ", getattr(c, col)(tui.icon(ico) + " " + verdict))
          + c.dim(tr("{} 条 / {} MB / 单条预期 {}s",
                     data.get("memory_count"),
                     data.get("size_mb"),
                     data.get("estimated_write_latency_s"))))
    print()
    checks = data.get("checks") or []
    rows = []
    for ck in checks:
        i2, col2 = _LEVEL_STYLE.get(ck.get("level", "OK"), ("dot", "gray"))
        rows.append([tui.icon(i2), ck.get("name", ""), getattr(c, col2)(str(ck.get("value", ""))), ""])
    name_w = max([tui.vwidth(r[1]) for r in rows] + [4])
    val_w = max([tui.vwidth(r[2]) for r in rows] + [4])
    detail_w = max(24, tui.term_width() - 2 - name_w - val_w - 3 * 2)
    for r, ck in zip(rows, checks):
        r[3] = "\n".join(tui.wrap(ck.get("detail", ""), detail_w))
    print(tui.table(["", tr("项"), tr("值"), tr("说明")], rows))
    advice = data.get("advice") or []
    if advice:
        print()
        print(tui.section(tr("建议")))
        for i, t in enumerate(advice, 1):
            print(f"  {i}. {t}")
    return 0


# --------------------------------------------------------------------------
# 命令实现：设置
# --------------------------------------------------------------------------
_SCOPES = ("embedding", "mode", "llm", "storage", "search", "queue")


def cmd_setup(sh: Shell, items: list[str]) -> int:
    """`setup` 走完整向导；`setup <项>` 只改一项。改完立刻重读配置。"""
    scope = (items[0] if items else "all") or "all"
    if scope in ("model", "embed"):
        scope = "embedding"
    import setup_wizard

    if scope != "all" and scope not in _SCOPES:
        tui.err(tr("未知的设置项 {}", scope))
        tui.note(tr("可用：all / {}", " / ".join(_SCOPES)))
        return 2
    try:
        rc = setup_wizard.run_setup(scope=scope)
    except tui.Aborted:
        tui.warn(tr("已取消，配置未改动。"))
        return 0
    finally:
        sh.reload()
    return rc


def _emb_line(cfg: dict) -> str:
    emb = cfg.get("embedding") or {}
    return tr("{} · {} 维 · {}", emb.get("model"), emb.get("dims"), emb.get("provider"))


def _llm_line(cfg: dict) -> str:
    llm = cfg.get("llm") or {}
    if cfg.get("mode") == "lite":
        return tr("lite 模式不需要")
    return f"{llm.get('model')}"


def _cfg_missing(cfg: dict) -> list[str]:
    try:
        # 带上 detail（如「MEMORY_EMBEDDER_API_KEY 为空」），
        # 引导用户去环境变量而不是把 key 写进 config.json
        return [name if not detail else f"{name}（{detail}）"
                for name, passed, detail in cfgmod.validate(cfg) if not passed]
    except Exception:  # noqa: BLE001
        return []


def cmd_config(sh: Shell, items: list[str]) -> int:
    cfg = sh.reload()
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    pairs = [
        (tr("配置文件"), str(cfgmod.config_path())),
        (tr("首次设置"), tr("已完成") if cfg.get("setupComplete") else tr("未完成")),
        (tr("身份 user_id"), cfgmod.identity_label(cfgmod.identity_defaults(cfg))
         + tr("（来源：{}）", cfgmod.identity_source(cfg))),
        (tr("运行模式"), str(cfg.get("mode"))),
        (tr("向量模型"), _emb_line(cfg)),
        (tr("嵌入入口"), str(emb.get("baseUrl") or tr("（本地 ONNX）"))),
        (tr("嵌入密钥"), (tr("已设置") + (tr("（来自 ${}）", emb.get("keyEnv")) if not emb.get("apiKey") else ""))
         if cfgmod.resolve_embed_key(cfg) else tr("未设置")),
        (tr("LLM 模型"), f"{llm.get('model')} @ {llm.get('baseUrl')}"),
        (tr("LLM 密钥"), (tr("已设置") + (tr("（来自 ${}）", llm.get("keyEnv")) if not llm.get("apiKey") else ""))
         if cfgmod.resolve_llm_key(cfg) else tr("未设置")),
        (tr("存储目录"), str((cfg.get("storage") or {}).get("dataDir") or cfgmod.default_data_dir())),
        (tr("检索阈值"), str((cfg.get("search") or {}).get("minScore"))),
        (tr("队列"), tr("重试 {} 次 / 锁超时 {}s",
                       cfg.get("queue", {}).get("maxAttempts"),
                       cfg.get("queue", {}).get("lockTimeout"))),
        (tr("界面语言"), label()),
    ]
    print(tui.kv(pairs))
    tui.note(tr("改这些用 setup <项>；直接改 config.json 也行，但下次运行才生效。"))
    return 0


def _storage_rows(data_dir: str) -> list[tuple[str, str]]:
    size, files = storage_ops.dir_usage(data_dir)
    free = storage_ops.free_space(data_dir)
    rows = [(tr("当前目录"), data_dir)]
    rows.append((tr("已占用"), tr("{} / {} 个文件", storage_ops.human_size(size), files) if files
                 else tr("空目录")))
    if free:
        rows.append((tr("所在盘可用"), storage_ops.human_size(free)))
    rows.append((tr("默认目录"), str(cfgmod.default_data_dir())))
    return rows


def cmd_storage(sh: Shell, items: list[str]) -> int:
    cfg = sh.reload()
    data_dir = str((cfg.get("storage") or {}).get("dataDir") or cfgmod.default_data_dir())
    sub = items[0] if items else ""

    if not sub:
        print(tui.kv(_storage_rows(data_dir)))
        tui.note(tr("备份：storage backup ｜ 迁目录：storage move <路径> ｜ 重建：storage wipe ｜ "
                    "清队列记录：storage vacuum ｜ 回默认：storage reset"))
        return 0

    if sub == "backup":
        ok, msg = storage_ops.backup(data_dir)
        (tui.ok if ok else tui.err)(msg)
        return 0 if ok else 1

    if sub == "vacuum":
        if not tui.confirm(tr("清理 jobs 表里已完成的记录并 VACUUM？（不动记忆）"), default=False):
            tui.note(tr("已取消"))
            return 0
        code, _d, raw = sh.capture(["stats", "--health", "--vacuum"])
        (tui.ok if code == 0 else tui.err)(tr("队列记录已清理") if code == 0 else raw.strip()[:300])
        return code

    if sub == "wipe":
        if not tui.confirm(tr("先备份再清空数据目录？清空后记忆需要重新写入"), default=False):
            tui.note(tr("已取消"))
            return 0
        ok, msg = storage_ops.wipe(data_dir)
        (tui.ok if ok else tui.err)(msg)
        return 0 if ok else 1

    target = ""
    if sub == "move":
        raw = " ".join(items[1:]).strip()
        if not raw:
            tui.warn(tr("用法：storage move <新目录>（支持 %USERPROFILE% 与 ~）"))
            return 2
        target = storage_ops.normalize_path(raw)
        good, msg = storage_ops.check_target(target)
        if not good:
            tui.err(msg)
            return 1
    elif sub == "reset":
        target = str(cfgmod.default_data_dir())
    else:
        tui.err(tr("未知的 storage 子命令 {}", sub))
        tui.note(tr("可用：backup / move <路径> / reset / wipe / vacuum"))
        return 2

    if target == data_dir:
        tui.info(tr("与当前目录相同，无需切换"))
        return 0
    size, files = storage_ops.dir_usage(data_dir)
    if files and tui.confirm(
        tr("把现有数据（{} / {} 文件）迁移过去？", storage_ops.human_size(size), files), default=True
    ):
        tui.spinner_line(tr("复制中…"))
        ok, msg = storage_ops.migrate(data_dir, target)
        (tui.ok if ok else tui.err)(msg)
        if not ok and not tui.confirm(tr("迁移失败，仍然切换？"), default=False):
            return 1
    cfg["storage"]["dataDir"] = target
    cfgmod.save(cfg)
    sh.reload()
    tui.ok(tr("存储目录已切到 {}", target))
    return 0


def cmd_identity(sh: Shell, items: list[str]) -> int:
    """身份：多智能体共享（通用）还是隔离（独立名字），只在命令里决定。"""
    cfg = sh.reload()
    cur = cfgmod.identity_defaults(cfg)
    sub = items[0] if items else ""

    if sub in ("set", "use"):
        uid = " ".join(items[1:]).strip()
        if not uid:
            tui.warn(tr("用法：identity set <名字>（英文/数字/-/_ 最稳）"))
            return 2
        _path, eff = cfgmod.set_user_id(uid)
        sh.reload()
        tui.ok(tr("已切到 {}", cfgmod.identity_label(eff)))
        tui.note(tr("{} 的记忆没有删，切回去仍能看到", cfgmod.identity_label(cur)))
        return 0

    if sub in ("reset", "default"):
        if not tui.confirm(tr("回到通用（{}）？", cfgmod.GENERAL_USER_ID), default=True):
            tui.note(tr("已取消"))
            return 0
        _path, eff = cfgmod.set_user_id("")
        sh.reload()
        tui.ok(tr("已切到 {}", cfgmod.identity_label(eff)))
        return 0

    if sub in ("list", "ls"):
        known = [str(x) for x in ((cfg.get("identity") or {}).get("known") or []) if str(x).strip()]
        if not known:
            tui.note(tr("（还没有记录到别的身份）"))
            return 0
        print(tui.table(["user_id", tr("当前")],
                        [[u, "←" if u == cfgmod.identity_defaults(cfg) else ""] for u in known]))
        return 0

    print(tui.kv([
        (tr("当前身份"), cfgmod.identity_label(cur)),
        (tr("生效来源"), {
            "env": tr("环境变量 HY_MEM_USER_ID"),
            "config": "config.json",
            "general": tr("未设置 → 通用"),
        }.get(cfgmod.identity_source(cfg), cfgmod.identity_source(cfg))),
    ]))
    tui.note(tr("切身份：identity set <名字> ｜ 回通用：identity reset ｜ 看过哪些：identity list"))
    tui.note(tr("切换只改身份：模型配置、数据目录、已有记忆都不动"))
    return 0


def cmd_log(sh: Shell, items: list[str]) -> int:
    """操作日志：只读回放最近 50 条。

    刻意不在这里跑任何子命令：日志页出问题时最不该依赖「再起一个子进程」——
    用户点进来往往正是因为别的操作刚刚失败。
    """
    import oplog

    sub = items[0] if items else ""
    if sub == "clear":
        try:
            lines = oplog.recent(oplog.MAX_KEEP)
        except Exception as e:  # noqa: BLE001
            tui.err(tr("读取失败：{}", f"{type(e).__name__}: {e}"))
            return 1
        if not tui.confirm(tr("清空全部 {} 条操作日志？", len(lines)), default=False):
            tui.note(tr("已取消"))
            return 0
        try:
            oplog.clear()
            tui.ok(tr("日志已清空"))
        except Exception as e:  # noqa: BLE001
            tui.err(tr("清空失败：{}", f"{type(e).__name__}: {e}"))
            return 1
        return 0

    try:
        p = oplog.path()
        lines = oplog.recent(oplog.MAX_KEEP)
    except Exception as e:  # noqa: BLE001
        tui.err(tr("读取失败：{}", f"{type(e).__name__}: {e}"))
        return 1
    print(c.dim("  " + str(p)))
    print()
    if not lines:
        tui.note(tr("（还没有操作记录）"))
        return 0
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
        footer=tr("共 {} 条（只保留最近 {} 条）", len(lines), oplog.MAX_KEEP),
    ))
    tui.note(tr("清空：log clear"))
    return 0


def cmd_doctor(sh: Shell, items: list[str]) -> int:
    return sh.run(["doctor"])


def cmd_lang(sh: Shell, items: list[str]) -> int:
    """切界面语言。只写 config.json 的 lang 字段，下一行提示立刻生效。"""
    want = (items[0] if items else "").strip().lower()
    if want in ("", "toggle"):
        want = "en" if lang() == "zh" else "zh"
    if want not in ("zh", "en"):
        tui.warn(tr("用法：lang zh|en|toggle"))
        return 2
    try:
        set_lang(want)
    except Exception as e:  # noqa: BLE001
        tui.err(tr("切换失败：{}", f"{type(e).__name__}: {e}"))
        return 1
    sh.reload()
    # 环境变量优先级高于配置：HY_MEM_LANG 在外部固定时，写配置也看不出变化，
    # 必须说清楚，否则用户会反复敲这一条
    if lang() != want:
        tui.warn(tr("环境变量 HY_MEM_LANG 覆盖了配置，界面语言仍是 {}；取消该变量后再切换", label()))
        return 1
    tui.ok(tr("界面语言已切换为 {}，下一行提示就生效。", label()))
    return 0


def cmd_help(sh: Shell, items: list[str]) -> int:
    import helptext

    if items:
        cmd = find_command(items[0])
        if cmd is None:
            tui.err(tr("没有这个命令：{}", items[0]))
            return 2
        print(tui.title(tr("{} 用法", cmd.name)))
        print("  " + c.cyan(cmd.usage or cmd.name))
        print()
        print("  " + cmd.summary)
        if cmd.aliases:
            print("  " + c.dim(tr("别名：{}", " / ".join(cmd.aliases))))
        if cmd.words:
            print("  " + c.dim(tr("可补全：{}", " ".join(cmd.words))))
        print()
        tui.note(tr("完整命令清单：help ｜ 机器可读规格：hy-mem help --json"))
        return 0

    print(tui.title(tr("hy-mem 命令")))
    # 列宽按**全部**命令算一次：分组各算各的会让每组的缩进都不一样
    uw = min(18, max([tui.vwidth(cmd.usage or cmd.name) if tui.vwidth(cmd.usage or cmd.name) <= 18
                      else tui.vwidth(cmd.name) for cmd in COMMANDS] + [6]))
    groups: dict[str, list[Command]] = {}
    for cmd in COMMANDS:
        groups.setdefault(cmd.group, []).append(cmd)
    for gname, cmds in groups.items():
        print(tui.section(tr(gname)))
        # 用法串放在第一列只在它够短时成立；长了就换行，别把整张表推歪
        shorts = [cmd.usage if tui.vwidth(cmd.usage or cmd.name) <= 18 else cmd.name for cmd in cmds]
        for cmd, short in zip(cmds, shorts):
            print("  " + c.cyan(tui.rpad(short, uw)) + "  " + c.dim(cmd.summary))
            # 参数太长就换到下一行：挤在一行里会把整张表推歪，而用法串不能截断
            if short != (cmd.usage or cmd.name):
                print("  " + " " * uw + "  " + c.dim(cmd.usage))
    print()
    print(tui.kv([
        (tr("按键"), tr("↑↓ 历史 · Tab 补全 · Ctrl+R 反查 · Ctrl+C 放弃本行 · Ctrl+D 退出")),
        (tr("子命令"), f"hy-mem {helptext.VERSION}；任何命令也能直接在命令行执行：hy-mem <命令>"),
        (tr("机器可读"), tr("hy-mem help --json（参数类型 / 默认值 / 返回字段 / 示例）")),
    ]))
    tui.note(tr("help <命令> 看单项用法，例如 help search"))
    return 0


def cmd_clear(sh: Shell, items: list[str]) -> int:
    tui.clear()
    print(header(sh.cfg))
    return 0


def cmd_exit(sh: Shell, items: list[str]) -> int:
    raise ExitShell()


# --------------------------------------------------------------------------
# 命令表：name = 子命令名，尽量一一对应，别让人在两套词表之间换算
# --------------------------------------------------------------------------
COMMANDS: list[Command] = [
    Command("add", "add [-t 标签] [-s] [文本]", "写记忆：给文本就直接写，不给就进多行输入", cmd_add,
            ("a", "w", "write"), ("-t", "-s", "--sync"), "写"),
    Command("search", "search <查询> [-n 条数] [-t 标签]", "语义检索（拿不准就换几种问法）", cmd_search,
            ("s", "find"), ("-n", "-t"), "读"),
    Command("list", "list [条数]", "最近记忆，拿 id 去 get / delete", cmd_list, ("ls",), ("-n",), "读"),
    Command("get", "get <id>", "按 id 读全文", cmd_get, ("show",), (), "读"),
    Command("delete", "delete <id>", "按 id 删一条（要确认）", cmd_delete, ("rm", "del"), (), "读"),
    Command("tags", "tags [refresh|rm <标签>]", "标签清单；refresh 同步自动标签，rm 解绑", cmd_tags,
            ("tag",), ("refresh", "rm"), "读"),
    Command("queue", "queue [run|one|retry]", "异步队列状态与消费", cmd_queue, ("jobs",),
            ("run", "one", "retry"), "运维"),
    Command("stats", "stats", "库体积 / 条数 / 写入延迟预期", cmd_stats, ("health",), (), "运维"),
    Command("doctor", "doctor", "环境自检：端到端连通性", cmd_doctor, ("doc",), (), "运维"),
    Command("log", "log [clear]", "最近 50 条操作记录", cmd_log, ("logs",), ("clear",), "运维"),
    Command("setup", "setup [all|embedding|mode|llm|storage|search|queue]", "改配置；不带参数走完整向导",
            cmd_setup, ("set", "model"), _SCOPES + ("all",), "设置"),
    Command("config", "config", "只读查看生效配置", cmd_config, ("cfg", "conf"), (), "设置"),
    Command("storage", "storage [backup|move <路径>|reset|wipe|vacuum]", "数据目录：用量 / 迁移 / 备份 / 重建",
            cmd_storage, ("store",), ("backup", "move", "reset", "wipe", "vacuum"), "设置"),
    Command("identity", "identity [set <名字>|reset|list]", "身份 user_id：共享还是隔离", cmd_identity,
            ("user",), ("set", "reset", "list"), "设置"),
    Command("lang", "lang [zh|en|toggle]", "界面语言", cmd_lang, ("language",), ("zh", "en", "toggle"), "设置"),
    Command("help", "help [命令]", "命令清单 / 单项用法", cmd_help, ("?", "h"), (), "其他"),
    Command("clear", "clear", "清屏（历史还在，↑ 能翻回来）", cmd_clear, ("cls",), (), "其他"),
    Command("exit", "exit", "退出控制台（也可以 Ctrl+D）", cmd_exit, ("quit", "q"), (), "其他"),
]

_BY_NAME: dict[str, Command] = {}
for _cmd in COMMANDS:
    _BY_NAME[_cmd.name] = _cmd
    for _alias in _cmd.aliases:
        _BY_NAME.setdefault(_alias, _cmd)


def find_command(word: str) -> Command | None:
    return _BY_NAME.get(word)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _hint_line() -> str:
    return tr("help 看命令 · Tab 补全 · ↑↓ 历史 · Ctrl+R 反查 · Ctrl+D 退出")


def header(cfg: dict) -> str:
    """一次性表头：版本 / 模式 / 向量模型 / 数据目录，加一行按键提示。

    刻意不打印条数：那要起一次子进程查库，控制台启动就会慢半秒；
    真要看条数敲 `stats` 就行。
    """
    import helptext

    emb = cfg.get("embedding") or {}
    mode = str(cfg.get("mode") or "pro")
    data_dir = str((cfg.get("storage") or {}).get("dataDir") or cfgmod.default_data_dir())
    room = max(12, tui.term_width() - 6)
    rows = [
        c.cyan(c.bold(f"hy-mem {helptext.VERSION}"))
        + c.dim(" · ")
        + c.bold(mode)
        + c.dim(" · ")
        + str(emb.get("model") or "?")
        + c.dim(tr("（{} 维）", emb.get("dims") or "?")),
        c.dim("  " + tr("库") + " ") + c.dim(_short_path(data_dir, room)),
        c.dim("  " + _hint_line()),
    ]
    missing = _cfg_missing(cfg)
    if missing:
        # 配置不全时人一进来就该知道，而不是写完记忆才看到报错
        rows.append(
            "  " + tui.icon("warn") + " "
            + c.yellow(tr("配置未完成：缺 {}", ", ".join(missing[:3])))
            + c.dim("   " + tr("→ 跑 setup（或 setup embedding）"))
        )
    return "\n".join(rows)


def _banner_text() -> str:
    """表头里的提示行 —— i18n 测试用它核对中英切换是否真的换了语言。"""
    return _hint_line()


def prompt(sh: Shell) -> str:
    """提示符：`hy-mem ›`；身份不是「通用」时带上名字，提醒现在写进哪份库。"""
    try:
        ident = cfgmod.identity_label(cfgmod.identity_defaults(sh.cfg))
    except Exception:  # noqa: BLE001
        ident = ""
    tag = ""
    if ident and cfgmod.GENERAL_USER_ID not in ident:
        tag = c.yellow(f"[{ident}]")
    return c.cyan(c.bold("hy-mem")) + tag + " " + c.dim("›") + " "


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
def run_menu(dispatch) -> int:
    """进入控制台。EOF（Ctrl+D / 输入结束）与 `exit` 都正常退出，退出码 0。"""
    tui.enable_ansi()
    sh = Shell(dispatch)
    print(header(sh.cfg))
    print()
    while True:
        try:
            raw = tui.line(prompt(sh), history=sh.history, completer=sh.complete, eof_is_exit=True)
        except EOFError:
            print()
            print(c.dim(tr("退出。")))
            return 0
        except (tui.Aborted, KeyboardInterrupt):
            # Ctrl+C / Esc 只放弃当前行（bash 行为），不退出会话 ——
            # 写了一半的命令不该连会话一起丢
            print(c.dim(tr("（已放弃这一行；exit 或 Ctrl+D 退出）")))
            continue
        line = raw.strip()
        if not line:
            continue
        if not sys.stdin.isatty():
            # stdin 是管道时终端不会回显输入，补打一行，脚本/日志才看得出
            # 「这句是用户敲的」而不是程序自己冒出来的
            print(c.dim(raw))
        sh.remember(line)
        try:
            sh.execute(line)
        except ExitShell:
            print(c.dim(tr("退出。")))
            return 0
        except (tui.Aborted, KeyboardInterrupt):
            print()
            tui.warn(tr("已中断"))
        except SystemExit:
            # 子命令里 argparse 的用法错误会抛 SystemExit；在控制台里它只应
            # 表现为「这次没执行成功」，不能把整个会话带走。
            tui.warn(tr("该操作未执行（参数不合法）"))
        except Exception as e:  # noqa: BLE001
            tui.err(tr("{}: {}", type(e).__name__, e))
        sh.reload()
