"""交互层自测 —— 不需要真终端，用脚本化按键驱动 tui.select.

为什么需要它：方向键/鼠标这条链路无法在 CI 或管道里手动验证，而它恰恰是
最容易在「某个终端下方向键失灵」时静默坏掉的部分。这里把 read() 换成
一个按键队列，直接断言「按了什么 → 选中了第几项」，覆盖：

  - 方向键上下移动 + 边界循环
  - Home/End/PgUp/PgDn
  - 数字直达
  - Esc / q / Ctrl+C 三种取消
  - 鼠标滚轮与左键点击（SGR 坐标 → 序号的反算）
  - 非 TTY 时降级为编号菜单（喂数字仍可用）

用法：.venv/Scripts/python.exe scripts/test_ui.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 控制台相关的用例会构造 menu.Shell（它要读配置）。指到沙箱，
# 免得跑测试时把真实安装目录的 config.json / 历史文件当工作区。
_SANDBOX = Path(tempfile.mkdtemp(prefix="hymem-ui-"))
os.environ.setdefault("HY_MEM_CONFIG", str(_SANDBOX / "config.json"))
os.environ.setdefault("MEMORY_DATA_DIR", str(_SANDBOX / "data"))

from hymem.ui import tui  # noqa: E402


class FakeReader(tui.KeyReader):
    """把按键序列喂给 select()，并把 DSR 应答当作第一个 token。"""

    def __init__(self, keys: list[str], cursor_row: int = 11) -> None:
        self.backend = "fake"
        self._pending: list[str] = []
        self._keys = list(keys)
        self._cursor_row = cursor_row
        self.wrote: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read(self, timeout=None) -> str:
        if self._cursor_row is not None:
            row, self._cursor_row = self._cursor_row, None
            return f"csi:{row};1R"
        return self._keys.pop(0) if self._keys else "ctrlc"


def run_select(
    keys: list[str],
    options: list[tuple[str, str]],
    default: int = 0,
    extra_hint=None,
    cursor_row=11,
    mouse=None,
):
    """返回 (返回值, 是否 Aborted, 输出文本)。"""
    captured = io.StringIO()
    tui._READER = FakeReader(keys, cursor_row=cursor_row)
    tui.interactive = lambda: True  # type: ignore[assignment]
    with contextlib.redirect_stdout(captured):
        try:
            rc = tui.select("选择操作", options, default=default, extra_hint=extra_hint, mouse=mouse)
            return rc, False, captured.getvalue()
        except tui.Aborted:
            return None, True, captured.getvalue()


OPTS = [(f"项{i}", f"说明{i}") for i in range(5)]


def case(name: str, keys: list[str], expect, default: int = 0, extra_hint=None, cursor_row=11, mouse=None) -> bool:
    got, aborted, out = run_select(
        keys, OPTS, default=default, extra_hint=extra_hint, cursor_row=cursor_row, mouse=mouse
    )
    want_abort = expect == "abort"
    ok = (aborted and want_abort) or (not aborted and got == expect)
    print(("  PASS  " if ok else "  FAIL  ") + f"{name:<34} keys={keys} → {got}{'(aborted)' if aborted else ''}")
    if not ok:
        print("        输出片段：", repr(out[-300:]))
    return ok


def main() -> int:
    print("tui.select 按键行为")
    results = [
        case("回车选中默认", ["enter"], 0),
        case("↓ ↓ 回车 → 第 3 项", ["down", "down", "enter"], 2),
        case("j j 回车 → 第 3 项", ["char:j", "char:j", "enter"], 2),
        case("↑ 从首项回绕到末项", ["up", "enter"], 4),
        case("End 到末项", ["end", "enter"], 4),
        case("Home 到头", ["end", "home", "enter"], 0),
        case("PgDn 步进 5 但夹紧", ["pgdn", "enter"], 4),
        case("数字 3 直达第 3 项", ["char:3"], 2),
        case("数字越界被忽略", ["char:9", "enter"], 0),
        case("空格也算选中", ["down", "space"], 1),
        case("→ 选中", ["down", "right"], 1),
        case("Esc 取消", ["esc"], "abort"),
        case("← 当作返回", ["left"], "abort"),
        case("q 取消", ["char:q"], "abort"),
        case("Ctrl+C 中断", ["ctrlc"], "abort"),
        # 鼠标坐标换算：块首行 top = cursor_row - total。
        # 无 extra_hint 时 total = 5 项 + 5 行装饰 = 10，DSR 报 11 → top = 1；
        # 选项 0 落在 top+3，所以 rel = y - 4。
        # 注意：鼠标上报默认关闭（把原生拖选/复制还给终端），所以这几条必须
        # 显式 mouse=True 才走点击分支。
        case("滚轮下移一格", ["mouse:65:1:1:press", "enter"], 1, mouse=True),
        case("滚轮上移回绕", ["mouse:64:1:1:press", "enter"], 4, mouse=True),
        case("左键点第 4 项", ["mouse:0:5:7:press"], 3, mouse=True),
        case("右键被忽略", ["mouse:2:5:7:press", "enter"], 0, mouse=True),
        case("带 extra_hint 的点击偏移", ["mouse:0:5:8:press"], 3, extra_hint="提示行占一行", cursor_row=12, mouse=True),
        case("默认不开鼠标：点击被忽略", ["mouse:0:5:7:press", "enter"], 0),
    ]

    # 非 TTY 降级：编号模式（模拟管道输入）
    print("\n非 TTY 降级（编号菜单）")
    tui.interactive = lambda: False  # type: ignore[assignment]
    buf = io.StringIO()
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO("3\n")
        with contextlib.redirect_stdout(buf):
            got = tui.select("选择操作", OPTS, default=0)
    finally:
        sys.stdin = old_stdin
    ok = got == 2
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + f"喂数字 3 → {got}")
    if not ok:
        print("        输出片段：", repr(buf.getvalue()[-300:]))

    # Esc 在编号模式下用 q
    buf2 = io.StringIO()
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO("q\n")
        with contextlib.redirect_stdout(buf2):
            try:
                tui.select("选择操作", OPTS, default=0)
                ok = False
            except tui.Aborted:
                ok = True
    finally:
        sys.stdin = old_stdin
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + "编号模式 q 取消")

    # 渲染宽度：任何一行都不得折行，否则重绘与鼠标坐标会整体错位
    print("\n渲染宽度（不得折行）")
    for w in (40, 44, 60, 78, 110):
        tui.term_width = lambda d=78, w=w: w  # type: ignore[assignment]
        tui._READER = FakeReader(["ctrlc"], cursor_row=11)
        tui.interactive = lambda: True  # type: ignore[assignment]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                tui.select("选择操作", [(f"项{i}", "很长很长的说明文字" * 4) for i in range(5)], extra_hint="提示" * 40)
            except tui.Aborted:
                pass
        frame = buf.getvalue().split("\x1b[6n")[0]
        rows = [r for r in frame.split("\x1b[2K") if r.strip()]
        widest = max(tui.vwidth(r.rstrip("\n")) for r in rows)
        ok = widest <= w - 1
        results.append(ok)
        print(("  PASS  " if ok else "  FAIL  ") + f"宽度 {w:<4}最宽行 {widest}")
    tui.term_width = lambda d=78: 78  # type: ignore[assignment]

    # 按键解码：终端把方向键发成什么字节，决定了「方向键是否失灵」
    print("\n按键解码（平台码表）")
    class CharReader(tui.KeyReader):
        def __init__(self, chars: str):
            self.backend = "fake"
            self._pending: list[str] = []
            self._buf = list(chars)
        def _getw(self) -> str:
            return self._buf.pop(0) if self._buf else ""
        def _ready(self, timeout: float) -> bool:
            return bool(self._buf)

    def decode(chars: str) -> str:
        return CharReader(chars).read()

    cases = [
        ("Windows 扫描码 ↑", "\x00H", "up"),
        ("Windows 扫描码 ↓", "\x00P", "down"),
        ("Windows 扫描码 →", "\x00M", "right"),
        ("Windows 扫描码 PgDn", "\x00Q", "pgdn"),
        ("VT 方向键 ↑", "\x1b[A", "up"),
        ("VT 方向键 ↓", "\x1b[B", "down"),
        ("VT Home/End", "\x1b[H", "home"),
        ("VT PgUp/PgDn", "\x1b[5~", "pgup"),
        ("VT Shift+Tab", "\x1b[Z", "shift-tab"),
        ("单个 Esc", "\x1b", "esc"),
        ("鼠标左键按下", "\x1b[<0;12;7M", "mouse:0:12:7:press"),
        ("鼠标滚轮下", "\x1b[<65;12;7M", "mouse:65:12:7:press"),
        ("光标位置应答", "\x1b[12;1R", "csi:12;1R"),
        ("回车", "\r", "enter"),
        ("退格", "\x7f", "bs"),
        ("Ctrl+C", "\x03", "ctrlc"),
        ("空格", " ", "space"),
        ("普通字符", "j", "char:j"),
        ("EOF", "", "ctrlc"),
        # Alt+键：当 Esc，但字符必须还回队列，不能被吞
        ("Alt+键先出 Esc", "\x1ba", "esc"),
    ]
    for name, chars, want in cases:
        got = decode(chars)
        ok = got == want
        results.append(ok)
        print(("  PASS  " if ok else "  FAIL  ") + f"{name:<22}{chars!r} → {got!r}" + ("" if ok else f"（期望 {want!r}）"))

    rd_alt = CharReader("\x1ba")
    first = rd_alt.read()
    second = rd_alt.read()
    ok = first == "esc" and second == "char:a"
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + f"{'Alt+键 字符不丢':<22}→ {first!r}, {second!r}")

    # 英文热键：按下即选中，且 q 在显式声明时不再是「取消」
    print("\n英文热键")
    keyed = [("写记忆", "", "w"), ("检索", "", "r"), ("队列", "", "q"), ("退出", "", "x")]

    def keycase(name: str, keys: list[str], expect) -> bool:
        got, aborted, out = run_select(keys, keyed)
        ok = (aborted and expect == "abort") or (not aborted and got == expect)
        print(("  PASS  " if ok else "  FAIL  ") + f"{name:<34} keys={keys} → {got}{'(aborted)' if aborted else ''}")
        return ok

    results += [
        keycase("按 r 直接选中检索", ["char:r"], 1),
        keycase("按 q 选中队列（不是退出）", ["char:q"], 2),
        keycase("按 x 选中退出", ["char:x"], 3),
        keycase("未声明的字母被忽略", ["char:z", "enter"], 0),
        keycase("大写字母同样有效", ["char:R"], 1),
    ]

    # 行编辑：打字 / 粘贴 / 退格 / 移动
    print("\n行编辑（打字与粘贴）")

    class LineReader(tui.KeyReader):
        """timeout 有值即「没有待处理输入」—— 与真终端一致，drain 才不会被误吃键。"""

        def __init__(self, keys: list[str]) -> None:
            self.backend = "fake"
            self._pending: list[str] = []
            self._keys = list(keys)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def read(self, timeout=None) -> str:
            if timeout is not None:
                return ""
            return self._keys.pop(0) if self._keys else "ctrlc"

    def line_edit(keys: list[str], secret: bool = False, history=None, completer=None) -> str:
        tui._READER = LineReader(keys)
        tui.interactive = lambda: True  # type: ignore[assignment]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                return tui._line_edit("› ", secret=secret, history=history, completer=completer)
            except tui.Aborted:
                return "<aborted>"
            except EOFError:
                return "<eof>"

    def linecase(name: str, keys: list[str], want: str, secret: bool = False) -> bool:
        got = line_edit(keys, secret)
        ok = got == want
        results.append(ok)
        print(("  PASS  " if ok else "  FAIL  ") + f"{name:<34} → {got!r}" + ("" if ok else f"（期望 {want!r}）"))
        return ok

    results += [
        linecase("逐字符输入", ["char:a", "char:b", "char:c", "enter"], "abc"),
        linecase("粘贴是一串连续按键", ["char:" + ch for ch in "sk-abc123"] + ["enter"], "sk-abc123"),
        linecase("退格删掉最后一个", ["char:a", "char:b", "bs", "enter"], "a"),
        linecase("退格不会越界", ["bs", "bs", "char:x", "enter"], "x"),
        linecase("Ctrl+U 清空整行", ["char:a", "ctrl-u", "char:z", "enter"], "z"),
        linecase("← 后插入", ["char:a", "char:c", "left", "char:b", "enter"], "abc"),
        linecase("中文输入", ["char:中", "char:文", "enter"], "中文"),
        linecase("空格", ["char:a", "space", "char:b", "enter"], "a b"),
        linecase("密钥走同一套读键", ["char:s", "char:k", "enter"], "sk", secret=True),
        linecase("Esc 取消", ["char:a", "esc"], "<aborted>"),
        linecase("Ctrl+C 取消", ["char:a", "ctrlc"], "<aborted>"),
    ]

    # bash 习惯：历史 / 补全 / 反查 / Ctrl+D / Ctrl+K
    print("\n行编辑：历史、补全、反查（bash 习惯）")
    hist = ["search 部署流程", "list"]

    def hcase(name: str, keys, want, **kw) -> bool:
        got = line_edit(keys, **kw)
        ok = got == want
        results.append(ok)
        print(("  PASS  " if ok else "  FAIL  ") + f"{name:<30} → {got!r}" + ("" if ok else f"（期望 {want!r}）"))
        return ok

    hcase("↑ 取上一条命令", ["up", "enter"], "list", history=hist)
    hcase("↑↑ 取更早一条", ["up", "up", "enter"], "search 部署流程", history=hist)
    hcase("↑ 后 ↓ 回到草稿", ["char:x", "up", "down", "enter"], "x", history=hist)
    hcase("Ctrl+P / Ctrl+N 等价", ["ctrl-p", "ctrl-n", "enter"], "", history=hist)
    hcase("密码输入不翻历史", ["up", "enter"], "", history=hist, secret=True)
    hcase("无历史时 ↑ 无副作用", ["char:a", "up", "enter"], "a", history=[])

    _cands = {"se": ["search", "setup"], "sear": ["search-a", "search-b"]}
    hcase("Tab 唯一候选直接补上", ["char:s", "char:e", "char:a", "char:r", "char:c", "char:h", "tab", "enter"],
          "search", completer=lambda t: ["search"] if t.startswith("sear") else [])
    hcase("Tab 补公共前缀", ["char:s", "char:e", "char:a", "char:r", "tab", "enter"], "search-",
          completer=lambda t: _cands["sear"] if t.startswith("sear") else [])
    hcase("Tab 无候选不改变输入", ["char:z", "tab", "enter"], "z", completer=lambda t: [])
    hcase("补全抛异常也不打断输入", ["char:a", "tab", "enter"], "a",
          completer=lambda t: (_ for _ in ()).throw(RuntimeError("boom")))

    hcase("Ctrl+R 反查到历史并采用", ["ctrl-r", "char:部", "enter", "enter"], "search 部署流程", history=hist)
    hcase("Ctrl+R 后 Esc 放弃", ["char:x", "ctrl-r", "char:部", "esc", "enter"], "x", history=hist)
    hcase("Ctrl+D 空行 = 退出（EOF）", ["ctrl-d"], "<eof>")
    hcase("Ctrl+D 非空 = 删光标处字符", ["char:a", "char:b", "left", "ctrl-d", "enter"], "a")
    hcase("Ctrl+K 删到行尾", ["char:a", "char:b", "char:c", "left", "left", "ctrl-k", "enter"], "a")

    # 输入行不得折行：长密钥粘贴时每次重绘都必须还在同一行
    print("\n输入行单行渲染（长密钥）")
    for w in (40, 60, 80, 110):
        tui.term_width = lambda d=78, w=w: w  # type: ignore[assignment]
        keys = ["char:" + ch for ch in ("sk-" + "x" * 90)] + ["enter"]
        tui._READER = LineReader(keys)
        tui.interactive = lambda: True  # type: ignore[assignment]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            got = tui._line_edit("  › API Key: ", secret=True)
        segs = out.getvalue().split("\r\x1b[2K")
        widest = max(tui.vwidth(s.rstrip("\n")) for s in segs)
        ok = widest <= w - 1 and got.startswith("sk-") and len(got) == 93
        results.append(ok)
        print(("  PASS  " if ok else "  FAIL  ") + f"{'宽度 ' + str(w):<22}最宽 {widest} 行数 {len(segs)} 读回 {len(got)} 字符")
    tui.term_width = lambda d=78: 78  # type: ignore[assignment]

    # clip_input / _fit 的纯函数契约
    lead, body, cur = tui.clip_input(10, "a" * 100, 100, 40)
    ok = lead == "…" and 10 + tui.vwidth(lead + body) <= 39 and cur == tui.vwidth(lead + body)
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + f"{'clip_input 裁到一行':<22}正文 {len(body)} 列，光标 {cur}")

    lead, body, _c = tui.clip_input(10, "中文abc", 5, 60)
    ok = lead == "" and body == "中文abc"
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + f"{'clip_input 放得下就原样':<22}{body!r}")

    fitted = tui._fit("\x1b[31m" + "长" * 100 + "\x1b[0m", 41)
    ok = tui.vwidth(fitted) <= 41 - 1
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + f"{'_fit 不折行':<22}宽 {tui.vwidth(fitted)}")

    # 截断与宽度工具
    assert tui.vwidth("中文abc") == 7
    assert tui.trunc("中文中文中文", 6) == "中文…"
    assert tui.dwidth("a中") == 3
    print("  PASS  dwidth/vwidth/trunc")

    # 密钥字符集校验：中文/全角/零宽/空白都要当场拦下，否则会在发请求时炸成
    # UnicodeEncodeError（'latin-1' codec can't encode ...），界面上只显示 HTTP 0。
    print("\n密钥字符集校验")
    cases_ok = [
        ("sk-ws-abc123XYZ_-", True),
        ("a" * 64, True),
        ("", True),  # 空串由调用方决定沿用默认值，不算问题
    ]
    for s, want in cases_ok:
        got = tui._secret_problem(s) is None
        results.append(got == want)
        print(("  PASS  " if got == want else "  FAIL  ") + f"合法密钥放行 {s[:16]!r}")

    cases_bad = [
        ("没有啊", "非 ASCII"),
        ("sk-中文", "非 ASCII"),
        ("sk-abc def", "空白"),
        ("\ufeffsk-abc", "BOM"),
        ("sk\u200b-abc", "零宽空格"),
        ("sk\u3000abc", "全角空格"),
    ]
    for s, needle in cases_bad:
        msg = tui._secret_problem(s) or ""
        ok = bool(msg) and (needle in msg or "非 ASCII" in msg)
        results.append(ok)
        print(("  PASS  " if ok else "  FAIL  ") + f"拦下 {s[:10]!r:<14}{msg[:34]}")

    # 连续写入：两条 add 之间不插菜单、不问「再写一条」、不等「按任意键」
    print("\n控制台：写记忆 / 检索 连续操作")
    from hymem.ui import menu

    # 只在这一段强制行输入：前面的用例靠 key_mode() 走 FakeReader，
    # 提前全局关掉会让它们直接退化成编号菜单、卡在 stdin 上等输入。
    _prev_tui = os.environ.get("HY_MEM_TUI")
    os.environ["HY_MEM_TUI"] = "0"

    calls: list[tuple[list[str], str | None]] = []

    def fake_dispatch(argv, stdin_text=None):
        calls.append((list(argv), stdin_text))
        return 0, "已写入 1 条", ""

    sh = menu.Shell(fake_dispatch)
    out = io.StringIO()
    old_stdin = sys.stdin
    try:
        with contextlib.redirect_stdout(out):
            for body in ("第一条\n\n", "第二条\n\n"):
                sys.stdin = io.StringIO(body)
                menu.cmd_add(sh, [])
    finally:
        sys.stdin = old_stdin
    text = out.getvalue()
    ok = (
        len(calls) == 2
        and calls[0][1] == "第一条"
        and calls[1][1] == "第二条"
        and text.count("已写入 1 条") == 2
        # 契约：一次操作结束后不得再插「继续 / 回主界面」的二选一，也不得带轮次标题
        and "再写一条" not in text
        and "第 2 条" not in text
        and "按任意键" not in text
    )
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ")
          + f"连写两条 → 两次 dispatch（{len(calls)} 次），无二选一菜单 / 无轮次标题")

    # 失败只影响这一条，不能把控制台或后续写入带崩
    def failing_dispatch(argv, stdin_text=None):
        calls.append((list(argv), stdin_text))
        return 2, "", "写入失败"

    sh_fail = menu.Shell(failing_dispatch)
    out2 = io.StringIO()
    raised = ""
    try:
        with contextlib.redirect_stdout(out2):
            menu.cmd_add(sh_fail, ["坏的一条"])
            menu.cmd_add(sh_fail, ["好的第二条"])
    except Exception as e:  # noqa: BLE001
        raised = f"{type(e).__name__}: {e}"
    ok2 = not raised and out2.getvalue().count("写入失败") == 2
    results.append(ok2)
    print(("  PASS  " if ok2 else "  FAIL  ")
          + f"写入失败不抛异常，下一条照常执行（{raised or 'ok'}）")

    # 连续检索：两次查询各自拿到结果，不追问「再查一次」
    print("\n控制台：检索连续查询")
    asked: list[str] = []

    def search_dispatch(argv, stdin_text=None):
        asked.append(argv[argv.index("search") + 1])
        payload = {
            "results": [{"memory_id": "m1", "score": 0.91, "content": "命中内容", "tags": ["t"]}],
            "min_score": 0.35,
        }
        return 0, json.dumps(payload, ensure_ascii=False), ""

    sh_s = menu.Shell(search_dispatch)
    t3 = ""
    out3 = io.StringIO()
    try:
        with contextlib.redirect_stdout(out3):
            menu.cmd_search(sh_s, ["第一问"])
            menu.cmd_search(sh_s, ["第二问"])
    finally:
        pass
    t3 = out3.getvalue()
    ok3 = asked == ["第一问", "第二问"] and t3.count("命中内容") == 2 and "再查一次" not in t3
    results.append(ok3)
    print(("  PASS  " if ok3 else "  FAIL  ")
          + f"两次查询 → {asked}，结果各打一次，无「再查一次」")

    if _prev_tui is None:
        os.environ.pop("HY_MEM_TUI", None)
    else:
        os.environ["HY_MEM_TUI"] = _prev_tui

    bad = results.count(False)
    print(f"\n{'全部通过' if not bad else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
