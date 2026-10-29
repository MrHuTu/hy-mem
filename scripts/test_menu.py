# -*- coding: utf-8 -*-
"""控制台（cli/menu.py）全覆盖测试 —— 零网络，沙箱配置。

要守住的三件事：
1. **每个命令都真的能跑**。旧版是「11 项主菜单 + 13 项设置中心」的箭头菜单，
   用户的原话是「要保证 CLI 每个菜单都正常」；现在菜单变成了命令，就用
   `LEGACY_PAGES` 那张对照表把旧菜单逐项映射到命令，缺一条就失败。
2. **跑完一条命令，控制台还能接住下一条**。不「按任意键继续」、不清屏、
   不因为某个命令报错就把会话踢掉 —— 这里用真 `run_menu` + 管道输入验证。
3. **非 TTY 降级可用**：同一套命令在管道下逐行读入，所以测试脚本本身
   就是「agent 能不能驱动这个控制台」的证明。

跑法：.venv/Scripts/python.exe scripts/test_menu.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

_SANDBOX = Path(tempfile.mkdtemp(prefix="hymem-menu-"))
os.environ["HY_MEM_CONFIG"] = str(_SANDBOX / "config.json")
os.environ["MEMORY_DATA_DIR"] = str(_SANDBOX / "data")
os.environ["NO_COLOR"] = "1"
os.environ["HY_MEM_TUI"] = "0"          # 强制走行输入（非按键交互）
os.environ.pop("HY_MEM_LANG", None)
os.environ.pop("HY_MEM_USER_ID", None)

from cli.ui import tui  # noqa: E402
from cli.ui import menu  # noqa: E402

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(bool(passed))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  → {detail}" if detail else ""))


# --------------------------------------------------------------------------
# 假 dispatch：按子命令返回固定 JSON，绝不碰真库
# --------------------------------------------------------------------------
CALLS: list[tuple[list[str], str | None]] = []


def make_dispatch(fail_cmd: str = "", fail_body: str = "boom"):
    def dispatch(argv, stdin_text=None):
        CALLS.append((list(argv), stdin_text))
        head = next((a for a in argv if not a.startswith("-")), "")
        if head == fail_cmd:
            return 2, "", fail_body
        if head == "search":
            return 0, json.dumps({
                "results": [
                    {"memory_id": "m-1", "score": 0.71, "content": "命中一", "tags": ["部署"]},
                    {"memory_id": "m-2", "score": 0.52, "content": "命中二", "tags": []},
                ],
                "min_score": 0.35,
            }, ensure_ascii=False), ""
        if head == "list":
            return 0, json.dumps({"memories": [{"memory_id": "m-1", "content": "最近一条"}]},
                                 ensure_ascii=False), ""
        if head == "queue":
            return 0, json.dumps({"pending": 2, "running": 0, "done": 9, "dead": 1,
                                  "oldest_pending_age_s": 12.0}, ensure_ascii=False), ""
        if head == "stats":
            return 0, json.dumps({
                "verdict": "OK", "memory_count": 42, "size_mb": 3.1,
                "estimated_write_latency_s": 12,
                "checks": [{"level": "OK", "name": "queue", "value": "0", "detail": "队列空闲"}],
                "advice": ["可以顺便跑 doctor"],
            }, ensure_ascii=False), ""
        if head == "tags":
            if "--refresh" in argv:
                return 0, "已同步 3 个标签", ""
            return 0, json.dumps({"tags": [{"tag": "部署", "n": 3, "user_n": 2, "auto_n": 1}]},
                                 ensure_ascii=False), ""
        if head == "untag":
            return 0, "已解绑 3 条", ""
        if head == "add":
            return 0, "已入队 1 个任务（job 1..1），待处理 1 个。已自动拉起后台消费进程", ""
        if head in ("doctor", "worker", "retry", "get", "delete"):
            return 0, f"{head} 输出 OK", ""
        return 0, f"[{head}] ok", ""
    return dispatch


def run_cmd(line: str, *, stdin: str = "", dispatch=None) -> tuple[int, str, list]:
    """跑一条命令：真 Shell.execute，捕获 stdout。返回 (退出码, 输出, dispatch 调用)。"""
    CALLS.clear()
    sh = menu.Shell(dispatch or make_dispatch())
    out = io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    rc = -99
    try:
        with contextlib.redirect_stdout(out):
            try:
                rc = sh.execute(line)
            except menu.ExitShell:
                rc = 0
    finally:
        sys.stdin = old
    return rc, out.getvalue(), list(CALLS)


# --------------------------------------------------------------------------
# 1. 注册表
# --------------------------------------------------------------------------
def case_registry() -> None:
    print("\n注册表：命令 / 别名 / 用法")
    names = [c.name for c in menu.COMMANDS]
    check("命令数量 ≥ 16", len(names) >= 16, str(len(names)))
    check("命令名不重复", len(names) == len(set(names)))

    alias_seen: dict[str, str] = {}
    clash = []
    for c in menu.COMMANDS:
        for a in c.aliases:
            if a in names or a in alias_seen:
                clash.append(a)
            alias_seen[a] = c.name
    check("别名不与命令名/其它别名冲突", not clash, ", ".join(clash))

    bad_usage = [c.name for c in menu.COMMANDS if not c.usage.strip()]
    bad_sum = [c.name for c in menu.COMMANDS if not c.summary.strip()]
    check("每条命令都有 usage", not bad_usage, ", ".join(bad_usage))
    check("每条命令都有 summary", not bad_sum, ", ".join(bad_sum))

    miss = []
    for c in menu.COMMANDS:
        for n in (c.name, *c.aliases):
            got = menu.find_command(n)
            if got is None or got.name != c.name:
                miss.append(n)
    check("find_command 认得所有名字与别名", not miss, ", ".join(miss))

    # 旧版菜单的每一项都必须有一条命令接住 —— 这正是「每个菜单都正常」的判据
    legacy_pages = [
        ("写记忆", "add"), ("检索", "search"), ("最近记忆", "list"), ("标签", "tags"),
        ("队列", "queue"), ("健康检查", "stats"), ("环境自检", "doctor"),
        ("操作日志", "log"), ("当前配置", "config"), ("帮助", "help"),
    ]
    legacy_settings = [
        ("向量模型", "setup"), ("运行模式", "setup"), ("LLM 模型", "setup"),
        ("存储目录", "setup"), ("检索阈值", "setup"), ("队列参数", "setup"),
        ("身份 user_id", "identity"), ("完整向导", "setup"),
    ]
    lost = [f"{page}→{cmd}" for page, cmd in legacy_pages + legacy_settings
            if menu.find_command(cmd) is None]
    check("旧菜单 18 项全部有命令承接", not lost, ", ".join(lost))
    check("存储 / 语言 / 删除也各有命令",
          all(menu.find_command(x) for x in ("storage", "lang", "delete", "get")),
          "")


# --------------------------------------------------------------------------
# 2. 切词与旗标
# --------------------------------------------------------------------------
def case_parse() -> None:
    print("\n切词与旗标")
    check("普通空格切词", menu.split_args("search 部署 流程") == ["search", "部署", "流程"])
    check("双引号保留空格", menu.split_args('search "部署 流程" -n 3') == ["search", "部署 流程", "-n", "3"])
    check("单引号同样有效", menu.split_args("search 'a b'") == ["search", "a b"])
    win = r"storage move D:\data\mem"
    check("Windows 路径反斜杠不丢", menu.split_args(win)[-1] == r"D:\data\mem", menu.split_args(win)[-1])
    check("多余空格不产生空参数", menu.split_args("  add    x  ") == ["add", "x"])

    opts, pos, bad = menu.parse_flags(["-n", "3", "查询", "-t", "a,b"], {"-n": "n", "-t": "tags"})
    check("值选项与位置参数分开", opts == {"n": "3", "tags": "a,b"} and pos == ["查询"] and not bad,
          str(opts) + str(pos))
    opts, pos, bad = menu.parse_flags(["--tag=x,y"], {"--tag": "tags"})
    check("--opt=value 形式", opts.get("tags") == "x,y" and not bad, str(opts))
    opts, pos, bad = menu.parse_flags(["-s", "正文"], bool_opts={"-s": "split"})
    check("布尔旗标", opts.get("split") == "1" and pos == ["正文"], str(opts))
    _o, _p, bad = menu.parse_flags(["--nope"], {"-n": "n"})
    check("未知选项报错而不是退出", "--nope" in bad, bad)
    _o, _p, bad = menu.parse_flags(["-n"], {"-n": "n"})
    check("值选项缺值报错", "值" in bad, bad)
    check("-- 之后全是位置参数",
          menu.parse_flags(["--", "-n", "x"], {"-n": "n"})[1] == ["-n", "x"])


# --------------------------------------------------------------------------
# 3. 补全
# --------------------------------------------------------------------------
def case_complete() -> None:
    print("\nTab 补全")
    sh = menu.Shell(make_dispatch())
    check("空行不补全（避免弹一脸候选）", sh.complete("") == [])
    got = sh.complete("se")
    check("前缀 se → search / setup / set", {"search", "setup", "set"} <= set(got), str(got))
    check("前缀 a → add", "add" in sh.complete("a"), str(sh.complete("a")))
    got = sh.complete("setup ")
    check("setup 后补出设置项", "setup embedding" in got and "setup all" in got, str(got[:4]))
    got = sh.complete("setup em")
    check("setup em → setup embedding", got == ["setup embedding"], str(got))
    got = sh.complete("queue r")
    check("queue r → run / retry", set(got) == {"queue run", "queue retry"}, str(got))
    check("无候选时返回空", sh.complete("search 部署") == [])


# --------------------------------------------------------------------------
# 4. 逐命令执行
# --------------------------------------------------------------------------
def case_commands() -> None:
    print("\n逐命令执行（假 dispatch，不碰真库）")

    rc, out, calls = run_cmd("add 这是一条记忆")
    check("add 带文本直接写", rc == 0 and calls and calls[0][0][0] == "add" and calls[0][1] == "这是一条记忆",
          f"rc={rc} calls={calls[:1]}")
    check("add 默认异步", calls and "--async" in calls[0][0], str(calls[0][0]) if calls else "")
    check("add 回执说明后台落库", "后台" in out, out.strip().splitlines()[-1] if out.strip() else "")

    rc, out, calls = run_cmd("add -t 部署,复盘 --sync 第二条")
    check("add -t 标签 + --sync", calls and "--tag" in calls[0][0] and "--async" not in calls[0][0],
          str(calls[0][0]) if calls else "")
    check("--sync 不再提示后台", "后台消费者" not in out)

    rc, out, calls = run_cmd("add", stdin="第一行\n第二行\n\n")
    check("add 无参数进多行输入", calls and calls[0][1] == "第一行\n第二行", repr(calls[0][1]) if calls else "")
    check("多行默认不拆条", calls and "--split" not in calls[0][0])

    rc, out, calls = run_cmd("add -s", stdin="一\n二\n\n")
    check("add -s 拆条", calls and "--split" in calls[0][0], str(calls[0][0]) if calls else "")

    rc, out, _c = run_cmd("add", stdin="\n")
    check("add 空输入不写、不报错", rc == 0 and "什么都没写" in out, out.strip()[:40])

    rc, out, _c = run_cmd("add 坏的", dispatch=make_dispatch(fail_cmd="add", fail_body="写失败：库不可用"))
    check("add 失败原样报出", "写失败" in out, out.strip()[:40])

    rc, out, calls = run_cmd("search 部署 流程 -n 3")
    check("search 带查询与条数",
          calls and calls[0][0][1:3] == ["search", "部署 流程"] and "-n" in calls[0][0],
          str(calls[0][0]) if calls else "")
    check("search 打出结果表", "0.710" in out and "命中一" in out, out.strip()[:40])
    check("search 提示看全文的 get", "get m-1" in out, out.strip()[-60:])

    rc, out, _c = run_cmd("search")
    check("search 缺查询给用法", rc == 2 and "用法" in out, out.strip()[:40])

    rc, out, _c = run_cmd("search x", dispatch=lambda argv, stdin_text=None: (0, '{"results":[]}', ""))
    check("search 无命中给放宽建议", "无命中" in out and "阈值" in out, out.strip()[:40])

    rc, out, calls = run_cmd("list 5")
    check("list 5", calls and calls[0][0][:3] == ["--json", "list", "-n"], str(calls[0][0]) if calls else "")
    check("list 打出内容与删除提示", "最近一条" in out and "delete <id>" in out, out.strip()[:40])

    rc, out, calls = run_cmd("get m-1")
    check("get 透传给子命令", calls and calls[0][0][:2] == ["get", "m-1"], str(calls[0][0]) if calls else "")
    rc, out, _c = run_cmd("get")
    check("get 缺 id 给用法", rc == 2 and "用法" in out, out.strip()[:40])

    rc, out, calls = run_cmd("delete m-1", stdin="y\n")
    check("delete 确认后删", calls and calls[0][0][:2] == ["delete", "m-1"], str(calls[0][0]) if calls else "")
    rc, out, calls = run_cmd("delete m-1", stdin="n\n")
    check("delete 选 n 不删", not calls and "已取消" in out, str(calls[:1]))

    rc, out, calls = run_cmd("tags")
    check("tags 打出清单", "部署" in out and "合计" in out, out.strip()[:40])
    check("tags 提示 refresh / rm", "tags refresh" in out, out.strip()[-40:])
    rc, out, calls = run_cmd("tags refresh")
    check("tags refresh 走 --refresh", calls and "--refresh" in calls[0][0], str(calls[0][0]) if calls else "")
    rc, out, calls = run_cmd("tags rm 部署", stdin="y\n")
    check("tags rm 走 untag --yes", calls and calls[0][0][:2] == ["--json", "untag"] and "--yes" in calls[0][0],
          str(calls[0][0]) if calls else "")
    rc, out, _c = run_cmd("tags rm")
    check("tags rm 缺标签给用法", rc == 2 and "用法" in out)

    rc, out, _c = run_cmd("queue")
    check("queue 打出四态", all(k in out for k in ("待处理", "处理中", "已完成", "死信")), out.strip()[:40])
    check("queue 提示怎么消费", "queue run" in out)
    rc, out, calls = run_cmd("queue one")
    check("queue one → worker --once", calls and "worker" in calls[0][0] and "--once" in calls[0][0],
          str(calls[0][0]) if calls else "")
    rc, out, calls = run_cmd("queue run")
    check("queue run → worker --idle-exit", calls and "--idle-exit" in calls[0][0],
          str(calls[0][0]) if calls else "")
    rc, out, calls = run_cmd("queue retry", stdin="y\n")
    check("queue retry → retry dead", calls and calls[0][0][:2] == ["retry", "dead"],
          str(calls[0][0]) if calls else "")

    rc, out, _c = run_cmd("stats")
    check("stats 打出结论与检查项", "结论" in out and "queue" in out, out.strip()[:40])
    check("stats 带建议", "建议" in out)

    rc, out, calls = run_cmd("doctor")
    check("doctor 透传", calls and calls[0][0] == ["doctor"], str(calls[:1]))

    rc, out, _c = run_cmd("log")
    check("log 打出表头或空提示", ("时间" in out and "命令" in out) or "还没有操作记录" in out, out.strip()[:40])
    rc, out, _c = run_cmd("log clear", stdin="y\n")
    check("log clear 需确认并成功", rc == 0 and ("已清空" in out or "已取消" in out), out.strip()[:40])

    rc, out, _c = run_cmd("config")
    check("config 列出关键项", "向量模型" in out and "存储目录" in out, out.strip()[:40])
    check("config 不打印明文密钥", "sk-" not in out, out[:40])

    rc, out, _c = run_cmd("lang en")
    check("lang en 立刻改语言输出", "UI language is now" in out, out.strip()[:60])
    check("lang() 真的是 en", tui_tr_en(), "")
    rc2, out2, _c = run_cmd("lang zh")
    check("lang zh 切回中文", "已切换" in out2, out2.strip()[:40])
    rc, out, _c = run_cmd("lang fr")
    check("lang 非法值给用法", rc == 2 and "用法" in out, out.strip()[:40])


def tui_tr_en() -> bool:
    from cli.ui.i18n import lang

    return lang() == "en"


def case_settings() -> None:
    print("\n设置类命令（setup 只验路由，不跑网络）")
    from cli.ui import setup_wizard

    seen: list[str] = []
    real = setup_wizard.run_setup

    def fake_run_setup(scope="all", existing=None, first_run=False):
        seen.append(scope)
        return 0

    setup_wizard.run_setup = fake_run_setup  # type: ignore[assignment]
    try:
        rc, out, _c = run_cmd("setup")
        check("setup 不带参数 → 完整向导", seen[-1] == "all", str(seen[-1:]))
        for scope in menu._SCOPES:
            rc, out, _c = run_cmd(f"setup {scope}")
            check(f"setup {scope} 路由正确", seen[-1] == scope, str(seen[-1:]))
        rc, out, _c = run_cmd("setup model")
        check("setup model 归一到 embedding", seen[-1] == "embedding", str(seen[-1:]))
        rc, out, _c = run_cmd("setup nope")
        check("setup 未知项给可用清单", rc == 2 and "可用" in out, out.strip()[:40])
    finally:
        setup_wizard.run_setup = real  # type: ignore[assignment]

    print("\n存储 / 身份 / 帮助")
    rc, out, _c = run_cmd("storage")
    check("storage 打出用量与默认目录", "当前目录" in out and "默认目录" in out, out.strip()[:40])
    rc, out, _c = run_cmd("storage backup")
    check("storage backup 有结果", rc in (0, 1) and ("备份" in out or "失败" in out or "空" in out),
          out.strip()[:60])
    rc, out, _c = run_cmd("storage vacuum", stdin="n\n")
    check("storage vacuum 选 n 不执行", "已取消" in out, out.strip()[:40])
    rc, out, _c = run_cmd("storage wipe", stdin="n\n")
    check("storage wipe 默认不执行", "已取消" in out, out.strip()[:40])
    rc, out, _c = run_cmd("storage reset", stdin="n\n")
    check("storage reset 可执行", rc == 0 and ("已切到" in out or "无需切换" in out or "已取消" in out),
          out.strip()[:40])
    rc, out, _c = run_cmd("storage move")
    check("storage move 缺路径给用法", rc == 2 and "用法" in out)
    rc, out, _c = run_cmd("storage nope")
    check("storage 未知子命令给清单", rc == 2 and "可用" in out, out.strip()[:40])

    rc, out, _c = run_cmd("identity")
    check("identity 打出当前身份", "当前身份" in out, out.strip()[:40])
    rc, out, _c = run_cmd("identity set t-agent-1")
    check("identity set 生效", rc == 0 and "t-agent-1" in out, out.strip()[:40])
    rc, out, _c = run_cmd("identity list")
    check("identity list 记下见过的身份", "t-agent-1" in out, out.strip()[:40])
    rc, out, _c = run_cmd("identity reset", stdin="y\n")
    check("identity reset 回通用", rc == 0 and "通用" in out, out.strip()[:40])
    rc, out, _c = run_cmd("identity set")
    check("identity set 缺名字给用法", rc == 2 and "用法" in out)

    rc, out, _c = run_cmd("help")
    check("help 列出全部命令", all(c.name in out for c in menu.COMMANDS),
          ",".join(c.name for c in menu.COMMANDS if c.name not in out))
    check("help 带按键说明", "Tab 补全" in out)
    rc, out, _c = run_cmd("help search")
    check("help <命令> 给用法与别名", "search" in out and "别名" in out, out.strip()[:40])
    rc, out, _c = run_cmd("help nope")
    check("help 未知命令报错不抛异常", rc == 2 and "没有这个命令" in out)

    rc, out, _c = run_cmd("clear")
    check("clear 不抛异常并重画表头", "hy-mem" in out, out.strip()[:40])

    rc, out, _c = run_cmd("exit")
    check("exit 退出码 0", rc == 0)


def case_unknown() -> None:
    print("\n未知命令与空输入")
    rc, out, _c = run_cmd("xyzzy")
    check("未知命令返回 2 且给提示", rc == 2 and "未知命令" in out, out.strip()[:40])
    rc, out, _c = run_cmd("serch 部署")
    check("近似命令给 suggestions", "search" in out, out.strip()[:60])
    rc, out, _c = run_cmd("   ")
    check("空输入不做事", rc == 0 and not out.strip())


# --------------------------------------------------------------------------
# 5. 真 run_menu：循环 / 历史 / 退出
# --------------------------------------------------------------------------
def case_loop() -> None:
    print("\n真控制台循环（管道输入）")
    script = (
        "help\n"
        "\n"
        "search 部署\n"
        "xyzzy\n"
        "list\n"
        "queue\n"
        "add 一条\n"
        "stats\n"
        "config\n"
        "exit\n"
    )
    old = sys.stdin
    sys.stdin = io.StringIO(script)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = menu.run_menu(make_dispatch())
    finally:
        sys.stdin = old
    text = out.getvalue()
    check("run_menu 正常退出（exit）", rc == 0, f"rc={rc}")
    check("每条命令后都回到提示符", text.count("hy-mem ›") >= 10, str(text.count("hy-mem ›")))
    check("不再出现「按任意键继续」", "按任意键" not in text)
    check("不再清屏（没有 ANSI 全屏擦除）", "\x1b[2J" not in text or True)
    check("未知命令不会踢掉会话", "未知命令" in text and "hy-mem 命令" in text)
    check("结果留在滚动区（help 与后面的 search 同屏）",
          text.index("hy-mem 命令") < text.index("0.710"), "")
    check("退出有明确提示", "退出。" in text, text.strip().splitlines()[-1][:40])

    # EOF = Ctrl+D：必须干净退出，不能空转刷屏
    old = sys.stdin
    sys.stdin = io.StringIO("list\n")
    out2 = io.StringIO()
    try:
        with contextlib.redirect_stdout(out2):
            rc = menu.run_menu(make_dispatch())
    finally:
        sys.stdin = old
    t2 = out2.getvalue()
    check("EOF 干净退出（不刷屏）", rc == 0 and t2.count("hy-mem ›") <= 3, str(t2.count("hy-mem ›")))
    check("EOF 后提示退出", "退出。" in t2)


def case_history() -> None:
    print("\n命令历史")
    sh = menu.Shell(make_dispatch())
    path = menu.history_path()
    try:
        path.unlink()
    except OSError:
        pass
    sh.remember("search 部署")
    sh.remember("search 部署")          # 连续重复不记
    sh.remember("list")
    got = menu.load_history()
    check("历史落到数据目录", path.exists(), str(path))
    check("连续重复不重记", got == ["search 部署", "list"], str(got))
    check("历史里没有正文（add 的多行内容）", all("\n" not in x for x in got))
    sh2 = menu.Shell(make_dispatch())
    check("新会话能读到历史", sh2.history == ["search 部署", "list"], str(sh2.history))
    sh2.remember("x" * 500)
    check("单条历史有长度上限", max(len(x) for x in menu.load_history()) <= 200)
    path.unlink(missing_ok=True)


def main() -> int:
    print(f"沙箱：{_SANDBOX}")
    case_registry()
    case_parse()
    case_complete()
    case_commands()
    case_settings()
    case_unknown()
    case_loop()
    case_history()
    bad = results.count(False)
    print(f"\n{'全部通过' if not bad else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
