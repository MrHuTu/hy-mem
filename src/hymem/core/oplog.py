"""操作日志 —— 最近 50 次子命令执行记录，落在数据目录下的 ops.log.

为什么要有它：hy-mem 的失败经常发生在「子进程里」（交互界面的每次操作都是一次
子进程调用，异步写入还要等 worker），用户回头想「我刚才到底跑了什么、为什么
挂了」时，终端里那点滚动缓冲早没了。一份只保留最近 50 条、人能直接读的纯文本
日志，比让用户复现一次故障便宜得多。

三条取舍：

1. **纯文本、一行一条**。出故障时用户会直接 `type ops.log` 或发给别人看，
   不该要求他先装个解析器。结构化字段用固定列宽对齐，需要 JSON 时由
   `hy-mem log --json` 现场解析（见 parse）。

2. **正文与查询词不落盘**。add/update 的位置参数是记忆正文、search 的是查询
   词，写进日志等于在磁盘上多存一份明文副本。日志只记「做了什么」，不记
   「内容是什么」—— id、标签、条数这些定位信息照记。

3. **日志永远不能影响命令本身**。写不进去（目录只读、磁盘满、并发替换失败）
   就静静放弃；`--verbose` 时才在 stderr 上说一句，仍然不改退出码。
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

MAX_KEEP = 50
LOG_NAME = "ops.log"

# 位置参数是自由文本（正文 / 查询词）的命令：这些值不进日志，见模块 docstring
_FREETEXT_CMDS = {"add", "update", "search"}

# 键名含这些词就一律打码。按「去掉分隔符后包含」匹配，所以 --api-key / apiKey
# / API_KEY 都能命中；--tag 不含这些词，不受影响。
_SECRET_HINTS = ("key", "token", "secret", "password", "apikey", "passwd")

# 单行摘要的字符预算：再长就截断，保证一条日志不会被挤成好几屏
_SUMMARY_MAX = 78

# 列宽。固定宽度是为了 log --json 能反解，也让肉眼扫读时各列对齐。
_TS_W = 19
_CMD_W = 10
_ST_W = 5
_DUR_W = 7


def path() -> Path:
    """日志文件路径：<storage.dataDir>/ops.log（目录不存在就建）。"""
    from hymem.core import config as cfgmod

    raw = str((cfgmod.load().get("storage") or {}).get("dataDir") or "").strip()
    base = Path(os.path.expandvars(os.path.expanduser(raw))) if raw else cfgmod.default_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / LOG_NAME


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------
def _is_secret(flag: str) -> bool:
    name = flag.lstrip("-").lower().replace("-", "").replace("_", "")
    return any(h in name for h in _SECRET_HINTS)


def _sanitize(argv: list[str]) -> str:
    """argv（含子命令）→ 一行摘要。密钥类参数打码，正文/查询词整个丢掉。"""
    if not argv:
        return ""
    head = str(argv[0])
    drop_positional = head in _FREETEXT_CMDS
    out = [head]
    i = 1
    while i < len(argv):
        tok = str(argv[i])
        if tok == "--":
            # `--` 之后全是字面量：对 add/update/search 而言就是正文，整段去掉；
            # 别的命令照抄，但仍做一次键名打码（可能写着 apiKey=xxx）
            if not drop_positional:
                for lit in argv[i + 1:]:
                    name, sep, _value = str(lit).partition("=")
                    out.append(f"{name}=***" if (sep and _is_secret(name)) else str(lit))
            break
        if "=" in tok and not tok.startswith("-"):
            # 形如 config set embedding.apiKey=xxx：键名敏感就整段打码
            name, _value = tok.split("=", 1)
            out.append(f"{name}=***" if _is_secret(name) else tok)
            i += 1
            continue
        if not tok.startswith("-"):
            if not drop_positional:
                out.append(tok)
            i += 1
            continue
        # 选项：`--key=value` 与 `--key value` 两种形态都按「名字是否敏感」处理
        name, sep, _value = tok.partition("=")
        secret = _is_secret(name)
        if sep:
            out.append(f"{name}=***" if secret else tok)
            i += 1
            continue
        out.append(tok)
        nxt = str(argv[i + 1]) if i + 1 < len(argv) else None
        if nxt is not None and not nxt.startswith("-"):
            out.append("***" if secret else nxt)
            i += 2
            continue
        i += 1
    text = " ".join(p for p in out if p)
    return text if len(text) <= _SUMMARY_MAX else text[:_SUMMARY_MAX - 1] + "…"


# --------------------------------------------------------------------------
# 行格式
# --------------------------------------------------------------------------
def _fmt_duration(seconds: float) -> str:
    """耗时的可读形式：秒级保留一位小数，长了换分钟/小时，避免宽窄跳变。"""
    try:
        s = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "-"
    if s < 60:
        return f"{s:.1f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    return f"{s / 3600:.1f}h"


def _row(ts: str, cmd: str, status: str, dur: str, summary: str) -> str:
    cols = [ts.ljust(_TS_W), cmd.ljust(_CMD_W), status.ljust(_ST_W), dur.rjust(_DUR_W)]
    return "  ".join(cols) + ("  " + summary if summary else "")


def _error_text(error) -> str:
    """error 可以是异常，也可以是调用方拼好的字符串。"""
    if error is None:
        return ""
    if isinstance(error, BaseException):
        try:
            from hymem.core import errors

            code, hint = errors.classify_error(error)[1:]
            return f"{code} {hint}".strip() if hint else code
        except Exception:  # noqa: BLE001
            return type(error).__name__
    return str(error).strip()


def _clip(text: str, n: int = _SUMMARY_MAX) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def make_line(argv: list[str], rc: int, seconds: float, error=None, extra: str | None = None) -> str:
    cmd = str(argv[0]) if argv else "-"
    status = "OK" if int(rc) == 0 else "FAIL"
    summary = (extra or "").strip() or _sanitize(list(argv))
    err = _error_text(error)
    if err:
        # 错误码与提示在前、命令在后：先给错误留够位置，剩下多少给命令，
        # 否则长提示会把「到底哪条命令失败了」整个挤掉
        summary = f"{err}  {_clip(summary, max(12, _SUMMARY_MAX - len(err) - 2))}".strip() if summary else err
        summary = _clip(summary)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return _row(ts, cmd, status, _fmt_duration(seconds), summary)


def record(argv, rc, seconds, error=None, extra=None, verbose: bool = False) -> None:
    """追加一条。任何异常都吞掉——日志坏了不能连累命令本身。"""
    try:
        _append(make_line(list(argv or []), rc, seconds, error, extra))
    except Exception as e:  # noqa: BLE001
        if verbose:
            # 只有 --verbose 才吭声，且只走 stderr：stdout 属于命令结果
            import sys

            print(f"hy-mem: 操作日志写入失败（不影响本次结果）：{type(e).__name__}: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------
def _read_lines(p: Path) -> list[str]:
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8", errors="replace")
    return [ln for ln in text.splitlines() if ln.strip()]


def _write(p: Path, lines: list[str]) -> None:
    # 临时文件 + replace：读方（TUI 日志页）不会读到写了一半的文件
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    tmp.replace(p)


def _append(line: str) -> None:
    p = path()
    lines = _read_lines(p)
    lines.append(line)
    if len(lines) > MAX_KEEP:
        lines = lines[-MAX_KEEP:]  # 只留最近的，旧的自然淘汰
    _write(p, lines)


def recent(n: int = MAX_KEEP) -> list[str]:
    """最近 n 条，最旧在前（与文件顺序一致）。"""
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = MAX_KEEP
    return _read_lines(path())[-n:]


def parse(line: str) -> dict:
    """一行 → 结构体。列宽反解，解不动就把整行塞进 summary（绝不抛）。"""
    raw = (line or "").rstrip("\n")
    fallback = {"time": "", "cmd": "", "status": "", "duration": "", "summary": raw}
    # 时间列之后必须是两个空格；不满足说明这行不是我们写的（手改过 / 旧格式），
    # 硬按列宽切只会得到一串空字段，界面上就是「有一条记录什么都不显示」
    if len(raw) < _TS_W + 2 or raw[_TS_W:_TS_W + 2] != "  ":
        return fallback
    try:
        ts = raw[:_TS_W]
        rest = raw[_TS_W + 2:]
        cmd = rest[:_CMD_W].strip()
        rest = rest[_CMD_W + 2:]
        status = rest[:_ST_W].strip()
        rest = rest[_ST_W + 2:]
        dur = rest[:_DUR_W].strip()
        rest = rest[_DUR_W + 2:]
        return {
            "time": ts.strip(),
            "cmd": cmd,
            "status": status,
            "duration": dur,
            "summary": rest.strip(),
        }
    except Exception:  # noqa: BLE001
        return fallback


def clear() -> None:
    """清空日志文件（保留文件本身，路径不变）。"""
    _write(path(), [])
