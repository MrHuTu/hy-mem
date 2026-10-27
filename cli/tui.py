"""终端 UI 基元 —— 零依赖，只用标准库.

为什么不用 rich / questionary / prompt_toolkit：
  1. 本工具最终要 PyInstaller 冻成单文件 exe，每多一个依赖就多几十 MB 和一类
     打包失败可能（rich 会拖 jinja2/markdown-it-py，questionary 拖 prompt_toolkit）。
  2. 要的形状很简单：彩色行、表格、原地重绘的选择器。标准库足够。

## 交互约定（v0.8 起）

选择器是**原地重绘**的列表，不是编号菜单：

| 按键 | 行为 |
|---|---|
| ↑ / ↓  或 k / j | 上下移动 |
| Home / End 或 g / G | 跳到首 / 尾 |
| PgUp / PgDn | 翻 5 项 |
| Enter / Space / → | 选中当前项 |
| Esc / ← / q | 返回上一层（不退出程序） |
| 1~9 | 直达第 n 项并选中 |
| Ctrl+C | 整体中断 |
| 鼠标拖选 / 右键复制 | 用终端自己的选择功能（**默认这样**，我们不抢鼠标） |

## 关于鼠标：默认不上报，把选择权还给终端

旧版默认打开 xterm 鼠标上报（`?1000h`+`?1006h`），本意是支持点选，代价却是
**把终端的原生选择/复制彻底抢走**：用户想拖选一段文本（比如复制 API Key），
拖拽产生的鼠标事件被当成「点击」，于是选中项乱跳、面板被反复重绘；
再加上拖动时事件连发，一次复制能叠出好几块面板。密钥本来就该被复制，
所以从 v0.9 起默认**不发**上报，拖选和右键复制就是终端自己的行为。
想恢复点选：HY_MEM_MOUSE=1（少数终端下才有意义）。

## 重绘的三条硬约束（踩过的坑都在这）

1. **行数必须与终端宽度一致**。任何一行超宽就会折行，实际占用行数比我们
   记录的 `total` 多，`ESC[{n}A` 上移不够 → 同样的面板在屏幕上一份份叠加。
   所以每一行都按 `term_width()-1` 裁剪，重绘前还重新取一次宽度（窗口被
   拖动后宽度会变）。
2. **重绘用「清到屏尾」而不是逐行擦**。`ESC[J` 一次清干净，即使上一次的
   行数算错了也不会留下残余。
3. **文本输入必须单行渲染**。长密钥（60+ 字符）加上提示语必然超过终端宽度
   而折行，`\r ESC[2K` 只清得掉最后一行，于是每按一个键就多留一行 ——
   这正是「粘贴 key 时面板上一下出现多条」的来路。现在输入行做水平窗口
   裁剪（只显示光标附近的一段），永远只占一行。

终端能力探测是分层的，任何一层不可用就自动降级，绝不卡住：
  1. 非 TTY（被 agent、管道、CI 调用）→ 退回编号菜单，仍可喂数字
  2. Windows 上 stdin 不是真控制台（mintty / 管道桥接：msvcrt.kbhit 永远
     返回 False，界面会变成「按什么都没反应」）→ 退回编号菜单
  3. 没有按键后端 → 退回编号菜单

想临时关掉按键交互：HY_MEM_TUI=0（回到编号菜单）。
"""

from __future__ import annotations

import getpass
import os
import shutil
import sys
import time
import unicodedata

# ANSI 控制序列
_MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
_MOUSE_OFF = "\x1b[?1000l\x1b[?1006l"
_CURSOR_HIDE = "\x1b[?25l"
_CURSOR_SHOW = "\x1b[?25h"

# --------------------------------------------------------------------------
# ANSI 启用
# --------------------------------------------------------------------------
_ANSI_OK = False


def enable_ansi() -> bool:
    """Windows 控制台默认不解析 ANSI，需要显式打开 VT 处理。"""
    global _ANSI_OK
    if _ANSI_OK:
        return True
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            for handle in (-11, -12):  # STDOUT, STDERR
                h = kernel32.GetStdHandle(handle)
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(h, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(h, mode.value | 0x0004)
            _ANSI_OK = True
        except Exception:  # noqa: BLE001
            _ANSI_OK = False
    else:
        _ANSI_OK = True
    return _ANSI_OK


def color_enabled() -> bool:
    """NO_COLOR 优先；输出被重定向时自动关闭（避免管给脚本时混入转义码）。"""
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("HY_MEM_COLOR") == "0":
        return False
    if not sys.stdout.isatty():
        return False
    if os.getenv("HY_MEM_COLOR") == "1":
        return True
    return True


class _Painter:
    CODES = {
        "reset": "\x1b[0m",
        "bold": "\x1b[1m",
        "dim": "\x1b[2m",
        "rev": "\x1b[7m",
        "red": "\x1b[31m",
        "green": "\x1b[32m",
        "yellow": "\x1b[33m",
        "blue": "\x1b[34m",
        "magenta": "\x1b[35m",
        "cyan": "\x1b[36m",
        "gray": "\x1b[90m",
    }

    def __getattr__(self, name: str):
        code = self.CODES.get(name)
        if code is None:
            raise AttributeError(name)

        def _wrap(text: object) -> str:
            s = str(text)
            if not color_enabled():
                return s
            return f"{code}{s}{self.CODES['reset']}"

        return _wrap


c = _Painter()

# --------------------------------------------------------------------------
# 宽度
# --------------------------------------------------------------------------
def dwidth(text: str) -> int:
    """显示宽度：东亚全角字符占 2 列。不做这个，表格在中文下必然错位。"""
    w = 0
    for ch in str(text):
        if unicodedata.combining(ch):
            continue
        ea = unicodedata.east_asian_width(ch)
        w += 2 if ea in ("W", "F") else 1
    return w


def _strip_ansi(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\x1b" and i + 1 < len(s) and s[i + 1] == "[":
            j = i + 2
            while j < len(s) and not ("@" <= s[j] <= "~"):
                j += 1
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def vwidth(text: str) -> int:
    return dwidth(_strip_ansi(str(text)))


def lpad(text: str, width: int) -> str:
    gap = width - vwidth(text)
    return str(text) + (" " * gap if gap > 0 else "")


def rpad(text: str, width: int) -> str:
    gap = width - vwidth(text)
    return (" " * gap if gap > 0 else "") + str(text)


def trunc(text: str, width: int, mark: str = "…") -> str:
    """按显示宽度截断，超长补省略号。"""
    s = str(text)
    if vwidth(s) <= width:
        return s
    if width <= 1:
        return mark
    out = ""
    w = 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + mark


def term_width(default: int = 78) -> int:
    try:
        return max(40, min(shutil.get_terminal_size((default, 24)).columns, 110))
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------
# 版面元素
# --------------------------------------------------------------------------
def title(text: str) -> str:
    line = "─" * min(term_width(), max(8, vwidth(text) + 4))
    return f"\n{c.cyan(line)}\n  {c.bold(text)}\n{c.cyan(line)}"


def section(text: str) -> str:
    return f"\n{c.bold(c.blue('▌ ' + text))}"


def hr(width: int | None = None) -> str:
    return c.gray("─" * (width or min(term_width(), 74)))


def bullet(text: str, level: int = 0) -> str:
    return "  " * (level + 1) + c.gray("• ") + text


def kv(pairs: list[tuple[str, str]], indent: int = 2) -> str:
    if not pairs:
        return ""
    w = max(vwidth(k) for k, _ in pairs) + 2
    lines = []
    for k, v in pairs:
        val = v if v != "" else c.dim("（未设置）")
        lines.append(" " * indent + c.gray(lpad(k, w)) + val)
    return "\n".join(lines)


def table(headers: list[str], rows: list[list[str]], footer: str | None = None) -> str:
    widths = [vwidth(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], vwidth(cell))
    sep = "  "
    out = [sep.join(c.bold(lpad(h, widths[i])) for i, h in enumerate(headers))]
    out.append(c.gray(sep.join("─" * w for w in widths)))
    for r in rows:
        out.append(sep.join(lpad(cell, widths[i]) for i, cell in enumerate(r) if i < len(widths)))
    if footer:
        out.append("")
        out.append(c.dim("  " + footer))
    return "\n".join(out)


def panel(lines: list[str], indent: int = 2) -> str:
    """带左侧竖线的信息块，比全框更省横向空间。"""
    bar = c.cyan("│ ")
    pad = " " * indent
    body = [pad + bar + ln for ln in lines]
    return "\n".join(body)


ICONS = {
    "ok": lambda: c.green("✓"),
    "warn": lambda: c.yellow("!"),
    "err": lambda: c.red("✗"),
    "info": lambda: c.cyan("i"),
    "dot": lambda: c.gray("·"),
    "arrow": lambda: c.cyan("›"),
    "star": lambda: c.yellow("★"),
    "bullet": lambda: c.gray("○"),
}


def icon(name: str) -> str:
    fn = ICONS.get(name)
    return fn() if fn else ""


def ok(msg: str) -> None:
    print(f"  {icon('ok')} {msg}")


def warn(msg: str) -> None:
    print(f"  {icon('warn')} {c.yellow(msg)}")


def err(msg: str) -> None:
    print(f"  {icon('err')} {c.red(msg)}")


def info(msg: str) -> None:
    print(f"  {icon('info')} {msg}")


def note(msg: str) -> None:
    print(c.dim("  " + msg))


# --------------------------------------------------------------------------
# 输入后端：raw 按键读取
# --------------------------------------------------------------------------
class Aborted(Exception):
    """用户主动取消（Esc / Ctrl+C / EOF）。调用方应干净退出，不打印堆栈。"""


def interactive() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


def key_mode() -> bool:
    """是否启用按键交互（方向键 / 鼠标）。"""
    if os.getenv("HY_MEM_TUI", "").strip().lower() in ("0", "off", "false", "no"):
        return False
    return interactive()


# Windows 扫描码 → 语义键
_WIN_SCAN = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
    "G": "home",
    "O": "end",
    "I": "pgup",
    "Q": "pgdn",
    "S": "del",
    "R": "ins",
}


def _win_stdin_is_console() -> bool:
    """Windows 上判断 stdin 是不是**真控制台**。

    为什么必须判：在 mintty / msys pty / 某些 IDE 终端里跑原生 exe 时，
    `sys.stdin.isatty()` 可能为 True，但 stdin 是管道桥接的，`msvcrt.kbhit()`
    永远返回 False、`getwch()` 阻塞在那里等一个永不到来的控制台事件 ——
    表现就是「上下键完全没反应」。这种环境必须降级到编号菜单（走行输入），
    宁可少些交互，也不能让界面假死。
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        return bool(kernel32.GetConsoleMode(h, ctypes.byref(mode)))
    except Exception:  # noqa: BLE001
        return False


class KeyReader:
    """跨平台单键读取。Windows 用 msvcrt，POSIX 用 termios，两者都不可用则退化。"""

    def __init__(self) -> None:
        self.backend = "none"
        self._fd = None
        self._saved = None
        # 被「看错但还给你」的键：DSR 探测、Alt+键 会顺带读到别的键，
        # 直接丢掉就成了「第一次按上下没反应」。统一塞回队列，下次 read 先吐它。
        self._pending: list[str] = []
        if os.name == "nt":
            try:
                import msvcrt  # noqa: F401

                if _win_stdin_is_console():
                    self.backend = "msvcrt"
            except ImportError:
                pass
        else:
            try:
                import termios
                import tty  # noqa: F401

                self._fd = sys.stdin.fileno()
                self._termios = termios
                self.backend = "termios"
            except Exception:  # noqa: BLE001
                pass

    # -- 回退队列 ----------------------------------------------------------
    def unread(self, tok: str) -> None:
        """把读到的键放回队首（顺序不乱）。"""
        if tok:
            self._pending.insert(0, tok)

    def unread_many(self, toks: list[str]) -> None:
        for t in reversed([t for t in toks if t]):
            self._pending.insert(0, t)

    def has_pending(self) -> bool:
        return bool(self._pending)


    # -- 上下文：进入时切 raw，退出时无论怎么退都还原 ----------------------
    def __enter__(self) -> "KeyReader":
        if self.backend == "termios":
            try:
                import termios

                self._saved = termios.tcgetattr(self._fd)
                new = termios.tcgetattr(self._fd)
                # 关掉行缓冲、回显与信号键：Ctrl+C 以 \x03 字节到达，由我们处理
                new[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
                # 保留 OPOST，否则 "\n" 不再是 CRLF，输出会阶梯状错行
                new[6][termios.VMIN] = 1
                new[6][termios.VTIME] = 0
                termios.tcsetattr(self._fd, termios.TCSADRAIN, new)
            except Exception:  # noqa: BLE001
                self._saved = None
        return self

    def __exit__(self, *_exc) -> None:
        if self.backend == "termios" and self._saved is not None:
            try:
                self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._saved)
            except Exception:  # noqa: BLE001
                pass
            self._saved = None
        return None

    # -- 底层 --------------------------------------------------------------
    def _getw(self) -> str:
        if self.backend == "msvcrt":
            import msvcrt

            ch = msvcrt.getwch()
            return ch if isinstance(ch, str) else ch.decode("latin-1")
        if self.backend == "termios":
            data = os.read(self._fd, 1)
            if not data:
                return ""
            first = data[0]
            if first < 0x80:
                return chr(first)
            need = 1 if first >= 0xC0 and first < 0xE0 else 2 if first < 0xF0 else 3
            buf = bytearray(data)
            for _ in range(need):
                buf += os.read(self._fd, 1)
            return buf.decode("utf-8", errors="replace")
        return ""

    def _ready(self, timeout: float) -> bool:
        if self.backend == "msvcrt":
            import msvcrt

            end = time.time() + timeout
            while time.time() < end:
                if msvcrt.kbhit():
                    return True
                time.sleep(0.005)
            return msvcrt.kbhit()
        if self.backend == "termios":
            import select

            r, _w_, _x = select.select([self._fd], [], [], max(0.0, timeout))
            return bool(r)
        return False

    def _next(self, timeout: float = 0.03) -> str:
        return self._getw() if self._ready(timeout) else ""

    # -- 解析 --------------------------------------------------------------
    def _csi(self, params: str, final: str) -> str:
        if final == "A":
            return "up"
        if final == "B":
            return "down"
        if final == "C":
            return "right"
        if final == "D":
            return "left"
        if final == "H":
            return "home"
        if final == "F":
            return "end"
        if final == "Z":
            return "shift-tab"
        if final == "~":
            return {
                "1": "home",
                "7": "home",
                "4": "end",
                "8": "end",
                "5": "pgup",
                "6": "pgdn",
                "2": "ins",
                "3": "del",
            }.get(params.split(";")[0], "")
        if final in ("M", "m") and params.startswith("<"):
            # SGR 鼠标：ESC [ < b ; x ; y M/m
            body = params[1:]
            try:
                b, x, y = (int(v) for v in body.split(";"))
            except ValueError:
                return ""
            return f"mouse:{b}:{x}:{y}:{'press' if final == 'M' else 'release'}"
        if final == "R" and params:
            return f"csi:{params}R"  # 光标位置应答（DSR）
        return ""

    def read(self, timeout: float | None = None) -> str:
        """读一个语义键。timeout 为 None 时阻塞。

        返回 up/down/left/right/home/end/pgup/pgdn/enter/tab/bs/esc/space/
        ctrlc/shift-tab/del/ins/mouse:.../char:<x>/""（超时或不可识别）
        """
        if self.backend == "none":
            return ""
        if self._pending:
            return self._pending.pop(0)
        if timeout is not None and not self._ready(timeout):
            return ""
        ch = self._getw()
        if ch == "":
            return "ctrlc"  # EOF：当作中断，绝不在这里死循环
        if ch == "\x03":
            return "ctrlc"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\t":
            return "tab"
        if ch in ("\x7f", "\x08"):
            return "bs"
        # 行编辑用的控制键：不映射的话会落进最后的 isprintable 分支之外被丢掉，
        # 行编辑里就成了「按了没反应」。
        if ch == "\x15":
            return "ctrl-u"  # 清空整行
        if ch == "\x17":
            return "ctrl-w"  # 删一个词
        if ch == "\x01":
            return "home"
        if ch == "\x05":
            return "end"
        if ch == " ":
            return "space"
        if ch == "\x1b":
            nxt = self._next(0.04)
            if not nxt:
                return "esc"
            if nxt in ("[", "O"):
                params = ""
                while True:
                    c2 = self._next(0.05)
                    if not c2:
                        return "esc"
                    if c2.isdigit() or c2 in ";<=>?":
                        params += c2
                        continue
                    return self._csi(params, c2)
            # Alt+键：语义上仍当 Esc，但把那个字符还回去 —— 直接吞掉的话，
            # 以 ESC 开头的残留序列会把紧跟其后的正常字符一并吃掉。
            if nxt.isprintable():
                self.unread("char:" + nxt)
            return "esc"
        if os.name == "nt" and ch in ("\x00", "\xe0"):
            scan = self._getw()
            key = _WIN_SCAN.get(scan, "")
            if not key and scan.isprintable():
                # 不认识的扫描码：把字符还给行编辑，别让按键凭空消失
                self.unread("char:" + scan)
            return key
        if ch.isprintable():
            return "char:" + ch
        return ""


_READER: KeyReader | None = None


def reader() -> KeyReader:
    global _READER
    if _READER is None:
        _READER = KeyReader()
    return _READER


# --------------------------------------------------------------------------
# 原地重绘
# --------------------------------------------------------------------------
def _fit(s: str, width: int) -> str:
    """把带 ANSI 的一行裁到显示宽度 ≤ width，**绝不折行**。

    折行是「面板叠出多份」的头号原因：只要有一行超过终端宽度，终端就会
    把它拆成两行，实际占用行数 > 我们记录的 total，下一次 `ESC[{n}A` 上移
    不够，新面板就画在旧面板下面。宁可裁掉几个字，也不能折行。
    """
    if width <= 0:
        return ""
    if vwidth(s) <= width:
        return s
    out: list[str] = []
    w = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\x1b" and i + 1 < n and s[i + 1] == "[":
            j = i + 2
            while j < n and not ("@" <= s[j] <= "~"):
                j += 1
            out.append(s[i : j + 1])
            i = j + 1
            continue
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            break
        out.append(ch)
        w += cw
        i += 1
    out.append("\x1b[0m")
    return "".join(out)


def _w(s: str) -> None:
    try:
        sys.stdout.write(s)
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def _draw(rows: list[str], drawn: int = 0) -> int:
    """整块重绘并返回这块实际占用的行数。

    `drawn` 是上一次绘制的行数：先上移它回到块首，再逐行「擦整行 + 写」，
    最后用 `ESC[J` 清到屏尾。收尾这一下是关键 —— 行数算错、宽度变化、
    意外折行留下的残余都会被清掉，屏幕上不会再叠出第二份面板。
    """
    if drawn:
        _w(f"\x1b[{drawn}A")
    for row in rows:
        _w("\x1b[2K" + row + "\n")
    _w("\x1b[J")
    return len(rows)


def _erase(drawn: int) -> None:
    """擦掉整块并把光标留在块首。一次 ESC[J 足够，不必逐行清。"""
    if drawn:
        _w(f"\x1b[{drawn}A")
    _w("\x1b[J")


def _cursor_row(rd: KeyReader, timeout: float = 0.3) -> int | None:
    """问终端「光标现在在第几行」（DSR）。失败返回 None，鼠标点击随之降级。

    只在真的要支持鼠标点击时调用 —— 否则这 0.3 秒里用户按的键会被读走
    再丢掉，正是「第一次按 ↑ 没反应」的来源。读到的非 DSR 键一律还回队列。
    """
    _w("\x1b[6n")
    consumed: list[str] = []
    end = time.time() + timeout
    try:
        while time.time() < end:
            tok = rd.read(timeout=max(0.02, end - time.time()))
            if not tok:
                continue
            if tok.startswith("csi:") and tok.endswith("R"):
                body = tok[4:-1].split(";")[0]
                if body.isdigit():
                    return int(body)
                break
            consumed.append(tok)
        return None
    finally:
        rd.unread_many(consumed)



# --------------------------------------------------------------------------
# 选择器
# --------------------------------------------------------------------------
_KEYPOOL = "abcdefhilmnopqrstuvwxyz"  # 去掉 g/j/k（导航键）与数字（序号直达）


def keyseq(n: int) -> list[str]:
    """生成 n 个可用的英文热键，按 e/f/a/b/... 顺序取。"""
    return [_KEYPOOL[i] if i < len(_KEYPOOL) else "" for i in range(n)]


def _norm_opts(options) -> tuple[list[tuple[str, str]], list[str]]:
    """接受 (标题, 说明) 或 (标题, 说明, '英文热键')，统一成二元组 + 热键表。

    热键用 ASCII 字母是刻意的：中文标签在各终端里的宽度与输入法状态都不可控，
    而一个字母既能让人类少按键，也能让智能体/脚本用 `--pick r` 这种确定性方式
    直接选中，不必依赖顺序。
    """
    opts: list[tuple[str, str]] = []
    keys: list[str] = []
    for o in options:
        label = o[0] if len(o) > 0 else ""
        detail = o[1] if len(o) > 1 else ""
        key = str(o[2]).lower() if len(o) > 2 and o[2] else ""
        opts.append((label, detail))
        keys.append(key[:1])
    return opts, keys


def _select_numbered(
    question: str,
    options: list[tuple[str, str]],
    default: int,
    allow_cancel: bool,
    extra_hint: str | None,
    keys: list[str] | None = None,
) -> int:
    """非 TTY / 无按键后端时的兜底：编号或英文键 + 回车。可被管道喂输入。"""
    keys = keys or [""] * len(options)
    print()
    print(f"  {c.bold(question)}")
    if extra_hint:
        print(c.dim("  " + extra_hint))
    print()
    w = max(vwidth(t) for t, _ in options)
    for i, (label, detail) in enumerate(options):
        num = c.cyan(f"{i + 1}") if i != default else c.cyan(c.bold(f"{i + 1}"))
        key = f"[{keys[i]}]" if keys[i] else "   "
        mark = c.green(" ← 当前") if i == default else ""
        print(f"    {key} {num}) {lpad(label, w)}{mark}")
        if detail:
            print(c.dim(f"           {detail}"))
    hint = "输入序号或英文键后回车" + (f"（直接回车 = {default + 1}）" if default >= 0 else "")
    if allow_cancel:
        hint += "，q 取消"
    while True:
        raw = _line_edit(c.gray(f"  {hint}: ")).strip()
        if raw == "" and default >= 0:
            return default
        if raw.lower() in ("q", "quit", "exit") and allow_cancel:
            raise Aborted()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        if raw.lower() in keys:
            return keys.index(raw.lower())
        err(f"无效输入：{raw!r}，请输入 1~{len(options)} 或英文键")


def _ask_line(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        print()
        raise Aborted() from exc


def drain(rd: "KeyReader | None" = None, ms: int = 60) -> int:
    """丢掉输入缓冲里已排队的字节，返回丢掉的个数。

    为什么必须有：select() 会发 DSR（ESC[6n）问光标行号，终端的应答若晚于
    等待窗口才到，就留在缓冲里；下一次文本输入会把它当成"你敲的字"读走 ——
    表现出来就是「我明明打了 key / 粘了文本，却没生效」。同理还有鼠标事件、
    以及上一屏误触的按键。
    """
    rd = rd or reader()
    if rd.backend == "none":
        return 0
    n = 0
    end = time.time() + ms / 1000.0
    while time.time() < end:
        tok = rd.read(timeout=0.01)
        if tok:
            n += 1
            if tok == "ctrlc":  # EOF：再读还是它，必须停
                break
            end = max(end, time.time() + 0.02)  # 还有键在来，继续排空
    return n


def _char_cells(text: str) -> list[int]:
    return [2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text]


def clip_input(prompt_w: int, text: str, pos: int, width: int) -> tuple[str, str, int]:
    """把输入内容裁成**一行放得下**的窗口，返回 (左省略标记, 可见正文, 光标前列宽)。

    密钥动辄 60+ 字符，加上提示语必然超过终端宽度。以前直接整行写出去，
    终端把它折成两三行，而重绘只用 `\\r ESC[2K` 清最后一行 —— 于是每按一个
    键就多留一行旧内容，屏幕上「一下出现多条」。这里做水平窗口裁剪：只显示
    光标附近的一段，永远只占一行，配合单行擦除就不会再堆。
    """
    pos = max(0, min(pos, len(text)))
    avail = max(4, width - 1 - max(0, prompt_w))
    widths = _char_cells(text)
    # 先给光标左侧尽量多的空间（预留 1 列给 "…"）
    budget = max(1, avail - 1)
    start = pos
    used = 0
    while start > 0:
        cw = widths[start - 1]
        if used + cw > budget:
            break
        used += cw
        start -= 1
    lead = "…" if start > 0 else ""
    # 剩余空间再往后填
    limit = avail - (1 if lead else 0)
    end = pos
    while end < len(text):
        cw = widths[end]
        if used + cw > limit:
            break
        used += cw
        end += 1
    cursor = (1 if lead else 0) + sum(widths[start:pos])
    return lead, text[start:end], cursor


def _line_edit(prompt: str, secret: bool = False) -> str:
    """自带行编辑的输入，和选择器走同一套按键后端。

    为什么不用 input() / getpass：
      * 两者都走控制台的行规范缓冲，与选择器的 getwch 读的是同一份输入，
        一旦有残留字节（DSR 应答、鼠标事件）就会串味，表现为「输入不生效」；
      * getpass 在 Windows 上对粘贴、以及非控制台 stdin 的处理并不一致。
    自己读键就没有这类混用问题，粘贴是一串连续按键，天然支持。

    渲染只有一条铁律：**永远只占一行**（见 clip_input）。长密钥、长路径、
    中文都不会把输入行撑成两行，因此「擦一行再写」永远是完整的。

    编辑：←→ 移动、Home/End、退格/Delete、Ctrl+U 清行、Ctrl+W 删词、
    Enter 提交、Esc / Ctrl+C 取消。粘贴 = 一串连续字符键，不需要特殊处理。
    """
    rd = reader()
    if rd.backend == "none" or not key_mode():
        return _ask_line(prompt)

    drain(rd, 60)
    buf: list[str] = []
    pos = 0
    prompt_w = vwidth(prompt)

    def shown() -> str:
        return "*" * len(buf) if secret else "".join(buf)

    def repaint() -> None:
        width = term_width()
        # 极窄终端（<50 列）里提示语本身就可能占满一行：先把它裁短，
        # 否则「提示语 + 输入内容」还是会折行，前面所有的单行努力就白做了。
        dpr, dpw = prompt, prompt_w
        if dpw > width - 8:
            dpr = trunc(_strip_ansi(prompt), max(4, width - 8))
            dpw = vwidth(dpr)
        lead, body, cursor = clip_input(dpw, shown(), pos, width)
        line = dpr + lead + body
        _w("\r\x1b[2K" + line)
        back = vwidth(lead + body) - cursor
        if back > 0:
            _w(f"\x1b[{back}D")

    with rd:
        repaint()
        while True:
            tok = rd.read()
            if tok == "":
                continue
            if tok == "enter":
                _w("\n")
                return "".join(buf)
            if tok in ("ctrlc", "esc"):
                _w("\n")
                raise Aborted()
            if tok == "bs":
                if pos > 0:
                    buf.pop(pos - 1)
                    pos -= 1
                    repaint()
                continue
            if tok == "del":
                if pos < len(buf):
                    buf.pop(pos)
                    repaint()
                continue
            if tok == "left":
                if pos > 0:
                    pos -= 1
                    repaint()
                continue
            if tok == "right":
                if pos < len(buf):
                    pos += 1
                    repaint()
                continue
            if tok == "home":
                pos = 0
                repaint()
                continue
            if tok == "end":
                pos = len(buf)
                repaint()
                continue
            if tok == "ctrl-u":
                buf.clear()
                pos = 0
                repaint()
                continue
            if tok == "ctrl-w":
                while buf and buf[-1].isspace():
                    buf.pop()
                while buf and not buf[-1].isspace():
                    buf.pop()
                pos = len(buf)
                repaint()
                continue
            if tok == "space":
                buf.insert(pos, " ")
                pos += 1
                repaint()
                continue
            if tok.startswith("char:"):
                ch = tok[5:]
                if ch.isprintable():
                    buf.insert(pos, ch)
                    pos += 1
                    repaint()
                continue
            # 其余（PgUp/PgDn/Tab/鼠标）在单行输入里没有语义，忽略



def select(
    question: str,
    options: list[tuple[str, str]],
    default: int = 0,
    *,
    allow_cancel: bool = True,
    extra_hint: str | None = None,
    mouse: bool | None = None,
) -> int:
    """列表选择，返回 0 基索引。

    options 是 [(标题, 一行说明)]；说明会截断显示在标题右侧。
    default 同时是「初始光标位置」和「当前值」（用 ● 标记）。
    Esc / ← / q 在 allow_cancel 时抛 Aborted，交给上层决定是返回还是退出。
    """
    options, keys = _norm_opts(options)
    if not options:
        return -1
    default = max(0, min(default, len(options) - 1))
    if not key_mode():
        return _select_numbered(question, options, default, allow_cancel, extra_hint, keys)

    rd = reader()
    if rd.backend == "none":
        return _select_numbered(question, options, default, allow_cancel, extra_hint, keys)

    # 鼠标上报**默认关**。开着它，终端的原生拖选/右键复制就没了 —— 用户想
    # 复制一段输出或一个 key，拖拽会被当成点击，选中项乱跳、面板反复重绘。
    # 需要点选再显式开：HY_MEM_MOUSE=1。
    raw_mouse = os.getenv("HY_MEM_MOUSE", "").strip().lower()
    use_mouse = (raw_mouse in ("1", "on", "true", "yes")) if mouse is None else bool(mouse)
    idx = default
    drawn = 0

    def render() -> list[str]:
        # 宽度每次重画都重新取：窗口被拖动后列数会变，沿用旧宽度会折行。
        # 末尾统一走 _fit，保证**没有任何一行超过终端宽度** —— 一旦折行，
        # 实际行数就多于 drawn，重绘上移不够，屏幕上会叠出第二份面板。
        width = term_width()
        label_w = min(max(vwidth(t) for t, _ in options), max(16, width - 26))
        rows = ["", "  " + c.bold(trunc(question, width - 4))]
        if extra_hint:
            rows.append("  " + c.dim(trunc(extra_hint, width - 4)))
        rows.append("")
        for i, (label, detail) in enumerate(options):
            sel = i == idx
            gutter = (c.cyan("›") if sel else " ") + (c.green("●") if i == default else " ")
            chip = c.cyan(f"[{keys[i]}]") if keys[i] else "   "
            lab = trunc(label, label_w)
            room = width - 11 - label_w - 3
            tail = c.dim(trunc(detail, room)) if detail and room > 8 else ""
            line = f"  {gutter} {chip} {lpad(lab, label_w)}  {tail}"
            if sel:
                line = c.rev(lpad(_strip_ansi(line), width - 3))
            rows.append(line)
        rows.append("")
        bits = ["↑↓/jk 移动", "Enter 选中", "1~9 直达"]
        if any(keys):
            bits.append("字母键直达")
        if use_mouse:
            bits.append("鼠标可点")
        if allow_cancel:
            bits.append("Esc 返回")
        hint = " · ".join(bits)
        if vwidth(hint) + 4 > width:  # 窄终端只保留必要提示
            bits = ["↑↓", "Enter 选中"] + (["Esc 返回"] if allow_cancel else [])
            hint = " · ".join(bits)
        rows.append("  " + c.dim(trunc(hint, width - 4)))
        return [_fit(r, width - 1) for r in rows]

    opt_row0 = 4 if extra_hint else 3  # rows 里第一个选项的下标
    _w(_CURSOR_HIDE)
    top_row: int | None = None
    try:
        with rd:
            if use_mouse:
                _w(_MOUSE_ON)
            drawn = _draw(render(), 0)
            if use_mouse:
                cur = _cursor_row(rd)
                if cur is not None:
                    # 画完光标停在块下一行：块首 = cur - drawn
                    top_row = cur - drawn

            def finish(i: int) -> int:
                _erase(drawn)
                _w(f"  {c.green('✓')} {question} → {c.bold(options[i][0])}\n")
                return i

            while True:
                tok = rd.read()
                if tok == "":
                    continue
                if tok == "ctrlc":
                    raise Aborted()
                # 英文热键优先于「q=取消」：菜单里如果显式声明了 q 这个键
                # （例如"队列"），那 q 就是选它，而不是退出。
                if tok.startswith("char:") and tok[5:].lower() in keys:
                    return finish(keys.index(tok[5:].lower()))
                if tok in ("esc", "left") or tok == "char:q":
                    if allow_cancel:
                        raise Aborted()
                    continue
                if tok in ("up",) or tok == "char:k":
                    idx = (idx - 1) % len(options)
                elif tok in ("down",) or tok == "char:j":
                    idx = (idx + 1) % len(options)
                elif tok in ("home",) or tok == "char:g":
                    idx = 0
                elif tok in ("end",) or tok == "char:G":
                    idx = len(options) - 1
                elif tok == "pgup":
                    idx = max(0, idx - 5)
                elif tok == "pgdn":
                    idx = min(len(options) - 1, idx + 5)
                elif tok.startswith("mouse:"):
                    parts = tok[6:].split(":")
                    if len(parts) != 4:
                        continue
                    _b, _x, _y, _act = parts
                    try:
                        b, y = int(_b), int(_y)
                    except ValueError:
                        continue
                    if b in (64, 65):  # 滚轮
                        idx = (idx - 1) % len(options) if b == 64 else (idx + 1) % len(options)
                    elif _act == "press" and (b & 3) == 0 and top_row is not None:
                        rel = y - top_row - opt_row0
                        if 0 <= rel < len(options):
                            return finish(rel)
                        continue
                    else:
                        continue
                elif tok in ("enter", "space", "right"):
                    return finish(idx)
                elif tok.startswith("char:") and tok[5:].isdigit():
                    n = int(tok[5:])
                    if 1 <= n <= min(9, len(options)):
                        return finish(n - 1)
                    continue
                else:
                    continue
                drawn = _draw(render(), drawn)
    finally:
        if use_mouse:
            _w(_MOUSE_OFF)
        _w(_CURSOR_SHOW)
        # 收尾排空：DSR 应答若在本函数返回后才到，会被下一次文本输入吃掉
        drain(rd, 30)



# --------------------------------------------------------------------------
# 文本输入
# --------------------------------------------------------------------------
def line(prompt: str) -> str:
    """公开的行输入（带上 tui 的编辑能力），供需要连续多行的界面复用。"""
    return _line_edit(prompt)


def drain_reader(ms: int = 60) -> int:
    return drain(ms=ms)


def ask(
    question: str,
    default: str = "",
    *,
    secret: bool = False,
    hint: str | None = None,
    validate=None,
) -> str:
    """行输入（保留控制台原生行编辑，中文输入法正常）。

    空回车取 default；validate 返回 (ok, msg) 时校验不过会重问。
    """
    if hint:
        print(c.dim("  " + hint))
    tail = f" {c.dim('[' + (default if not secret else '****') + ']')}" if default else ""
    while True:
        raw = _line_edit(f"  {c.gray('›')} {question}{tail}: ", secret=secret).strip()
        if raw == "":
            return default
        if validate is not None:
            good, msg = validate(raw)
            if not good:
                err(msg)
                continue
        return raw


def ask_secret(question: str, default: str = "", hint: str | None = None) -> str:
    """密钥输入。回显为 *，有 default 时直接回车保留原值。

    不用 getpass 的原因见 _line_edit：getpass 与选择器的按键读取共用控制台
    输入缓冲，残留字节会串进密钥里，而且粘贴行为在 Windows 控制台不一致。
    读完立刻回显长度，让「到底有没有读进去」一眼可见。

    复制粘贴的约定（贴合常见习惯）：
      * 粘贴 —— 直接 Ctrl+V / 右键粘贴，字符流会原样进缓冲；
      * 复制 —— 鼠标拖选后右键（Windows Terminal 也可 Ctrl+Shift+C）。
        我们默认**不抢鼠标**，所以终端自己的选择和复制一直可用。
    也正因为不抢鼠标，输入行不会因为拖拽事件被反复重绘。
    """
    print(c.dim("  粘贴：Ctrl+V 或右键直接贴；复制：鼠标拖选后右键。Ctrl+C 是中断，别用它复制。"))
    if hint:
        print(c.dim("  " + hint))
    if default:
        prompt = f"  {c.gray('›')} {question} {c.dim('[已设置，回车保留 / 输入新值覆盖]')}: "
    else:
        prompt = f"  {c.gray('›')} {question}: "
    raw = (_line_edit(prompt, secret=True) or "").strip()
    if raw:
        note(f"已读取 {len(raw)} 个字符" + (f"，{raw[:6]}…{raw[-4:]}" if len(raw) > 12 else ""))
        return raw
    if default:
        note("沿用原值")
    return default


def confirm(question: str, default: bool = False, *, allow_cancel: bool = False) -> bool:
    """单键确认（y / n / 回车）。Esc 在 allow_cancel 时取消。"""
    if key_mode():
        rd = reader()
        if rd.backend != "none":
            drain(rd, 40)
            tail = "Y/n" if default else "y/N"
            _w(f"  {c.gray('›')} {question} {c.dim('[' + tail + ']')} ")
            with rd:
                while True:
                    tok = rd.read()
                    if tok == "ctrlc":
                        _w("\n")
                        raise Aborted()
                    if tok in ("char:y", "char:Y"):
                        _w(c.green("是") + "\n")
                        return True
                    if tok in ("char:n", "char:N"):
                        _w(c.red("否") + "\n")
                        return False
                    if tok == "enter":
                        _w((c.green("是") if default else c.red("否")) + "\n")
                        return default
                    if tok == "esc":
                        _w(c.dim("（取消）") + "\n")
                        if allow_cancel:
                            raise Aborted()
                        return default
    tail = "Y/n" if default else "y/N"
    while True:
        raw = _ask_line(f"  {c.gray('›')} {question} {c.dim('[' + tail + ']')}: ").strip().lower()
        if raw == "":
            return default
        if raw in ("y", "yes", "是", "1"):
            return True
        if raw in ("n", "no", "否", "0"):
            return False
        err("请输入 y 或 n")


def pause(msg: str = "按任意键继续…") -> None:
    if key_mode():
        rd = reader()
        if rd.backend != "none":
            drain(rd, 40)
            _w(c.dim(f"  {msg}"))
            try:
                with rd:
                    while True:
                        tok = rd.read()
                        if tok in ("", None):
                            continue
                        break
            finally:
                _w("\n")
            return
    try:
        input(c.dim(f"  {msg}"))
    except (EOFError, KeyboardInterrupt):
        pass


def clear() -> None:
    if sys.stdout.isatty() and not os.getenv("HY_MEM_NO_CLEAR"):
        os.system("cls" if os.name == "nt" else "clear")


def keytest() -> int:
    """按键自检：在真实终端里逐个按下按键，看解码结果是否符合预期。

    存在的理由：方向键在不同终端下是三种不同的字节形态（Windows 扫描码
    `\x00H`、VT 的 `\x1b[A`、以及某些封装层的其它序列）。写代码时无法
    只靠单元测试确认真实终端走哪条路，出问题时又很难描述。这个命令把
    「按下 → 解码成什么」直接打出来，用于判定是终端问题还是程序问题。
    """
    print(title("按键自检"))
    rd = reader()
    print(kv([("按键后端", rd.backend), ("是否 TTY", str(interactive())), ("按键模式", str(key_mode()))]))
    if rd.backend == "none" or not interactive():
        warn("当前不是交互终端，无法读取按键。请在 cmd / Windows Terminal / iTerm 里直接运行。")
        return 1
    print()
    note("逐键按下，观察下方解码结果；Esc 或 Ctrl+C 退出。")
    note("方向键应显示 up / down / left / right；若显示 char: 说明终端没把它当功能键。")
    print()
    seen: list[str] = []
    with rd:
        while True:
            tok = rd.read()
            if tok in ("",):
                continue
            if tok == "ctrlc" or tok == "esc":
                break
            seen.append(tok)
            print(f"  {c.gray(f'{len(seen):>2}.')} {c.bold(tok)}")
    print()
    if seen:
        ok(f"共识别 {len(seen)} 个按键")
    else:
        warn("没有识别到任何按键 —— 终端可能把输入独占了")
    return 0


def spinner_line(msg: str) -> None:
    """耗时操作前的一行提示（不做真动画，避免非 TTY 下留垃圾字符）。"""
    print(c.dim(f"  … {msg}"))
