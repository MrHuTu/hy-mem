"""把 hy-mem 装成一个稳定可调用的命令（launcher）。

为什么需要：SKILL.md 写的是 `hy-mem add ...`，但智能体执行时那条命令必须在
PATH 上真的存在。产物是个目录（dist\\hy-mem\\hy-mem.exe），没谁会把一个随机
目录加进 PATH —— 于是 skill 里的每条命令都落空，看起来就像「帮助/技能没生效」。
这里生成一个薄启动器放到固定位置，把路径问题一次性解决。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path


def app_root() -> Path:
    """委托 paths 唯一实现（S2-2）。"""
    from hymem.core.paths import app_root as _ar

    return _ar()


def target_command() -> list[str]:
    """真正要执行的命令（argv 前缀）。

    S2 起：源码模式统一走 python -m hymem（editable 安装后任意 cwd 可跑，
    worker/serve 拉起的子进程不再依赖 cli.py 的路径推导）。
    """
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    root = app_root()
    py = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        py = Path(sys.executable)
    return [str(py), "-m", "hymem"]


def bin_dir() -> Path:
    """启动器默认落点：Windows 用 LOCALAPPDATA，其它用 ~/.local/bin。"""
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "hy-mem" / "bin"
    return Path.home() / ".local" / "bin"


def launcher_path(directory: Path | None = None) -> Path:
    directory = directory or bin_dir()
    return directory / ("hy-mem.cmd" if os.name == "nt" else "hy-mem")


def shim_path(directory: Path | None = None) -> Path:
    """POSIX shell 用的无扩展名启动器。

    Windows 上光有 hy-mem.cmd 是不够的：Git Bash / MSYS 里的 `hy-mem` 不会去
    匹配 .cmd（PATHEXT 只对 cmd 生效）。两套壳各放一个同名入口，才做到
    「不管谁来调都能用」。
    """
    directory = directory or bin_dir()
    return directory / "hy-mem"


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or "\\" in arg) else arg


def _posix(path: str) -> str:
    """转成 bash 能执行的形式：正斜杠就够了，Git Bash / MSYS 都认。"""
    return path.replace("\\", "/")


def render_launcher(kind: str = "") -> str:
    """生成启动器内容。kind: cmd（批处理）| sh（POSIX）。"""
    kind = kind or ("cmd" if os.name == "nt" else "sh")
    argv = target_command()
    if kind == "cmd":
        cmd = " ".join(_quote(a) for a in argv)
        return f"@echo off\r\n{cmd} %*\r\n"
    cmd = " ".join(f'"{_posix(a)}"' for a in argv)
    return f'#!/bin/sh\nexec {cmd} "$@"\n'


def _read_raw(path: Path) -> str:
    """读文件原始内容，**不做换行翻译**。

    read_text() 默认 universal-newline 会把 \r\n 归一成 \n，导致 CRLF 启动器
    （.cmd）与渲染结果永远比对不等 → install 每次都「重写」、scan 每次都误判
    stale，幂等被破坏。必须按字节读。
    """
    return path.read_bytes().decode("utf-8", errors="replace")


def _write_one(path: Path, body: str) -> tuple[bool, str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and _read_raw(path) == body:
            return True, f"{path.name} 已是最新"
        path.write_text(body, encoding="utf-8", newline="")
        if os.name != "nt" or path.suffix == "":
            try:
                path.chmod(0o755)
            except OSError:
                pass
        return True, f"已写入 {path.name}"
    except OSError as e:
        return False, f"{path.name}: {type(e).__name__}: {e}"


def install(directory: Path | None = None) -> tuple[bool, str, list[Path]]:
    """写启动器。返回 (全部成功, 说明, 写出的路径)。

    Windows 上写两个同名入口：hy-mem.cmd（cmd 与原生 shell）与 hy-mem
    （Git Bash / MSYS）。少一个，就有一半的调用方式落空 —— 之前 `which hy-mem`
    在 bash 里查不到，正是只写了 .cmd 的后果。
    """
    directory = directory or bin_dir()
    jobs: list[tuple[Path, str]] = []
    if os.name == "nt":
        jobs.append((launcher_path(directory), render_launcher("cmd")))
    jobs.append((shim_path(directory), render_launcher("sh")))
    msgs: list[str] = []
    paths: list[Path] = []
    good = True
    for path, body in jobs:
        okk, msg = _write_one(path, body)
        good = good and okk
        msgs.append(msg)
        if okk:
            paths.append(path)
    return good, "；".join(msgs), paths


def on_path(directory: Path | None = None) -> bool:
    dirs = os.getenv("PATH", "").split(os.pathsep)
    target = str((directory or bin_dir()).resolve()).lower()
    for d in dirs:
        try:
            if str(Path(d).resolve()).lower() == target:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def path_hint(directory: Path | None = None) -> str:
    d = str((directory or bin_dir()).resolve())
    if os.name == "nt":
        # 不用 setx "%PATH%;..."：那条命令会把系统 PATH 展开写进用户 PATH，
        # 长度超过 1024 会被截断，是经典事故。
        return (
            "powershell -NoProfile -Command \""
            "[Environment]::SetEnvironmentVariable('Path',"
            "[Environment]::GetEnvironmentVariable('Path','User') + ';" + d + "', 'User')\""
        )
    return f'echo \'export PATH="{d}:$PATH"\' >> ~/.bashrc'


def which() -> str | None:
    """在 PATH 里找 hy-mem，**跳过当前目录**。

    直接用 shutil.which 在 Windows 上会把「空 PATH 项 = 当前目录」也算进去，
    于是在仓库里跑 doctor 会把仓库里的 hy-mem.cmd 当成已装好的命令 —— 一换
    目录就失效。这里只认 PATH 里显式列出的目录。
    """
    names = ["hy-mem.exe", "hy-mem.cmd", "hy-mem"]
    for raw in os.getenv("PATH", "").split(os.pathsep):
        if not raw.strip():
            continue  # 空项代表当前目录，不算「装好了」
        d = Path(raw.strip('"'))
        for name in names:
            f = d / name
            try:
                if f.is_file():
                    return str(f)
            except OSError:
                continue
    return None


# ---------------------------------------------------------------------------
# 旧启动器接管
#
# 真实事故（2026-10-27）：源码模式曾把启动器写进 ~/.local/bin，后来 venv 被清、
# exe 版另装到 %LOCALAPPDATA%\hy-mem\bin —— 但 ~/.local/bin 在 PATH 里排前面，
# `hy-mem` 一直打到已失效的旧壳上。install 只顾自己的一亩三分地、不回收旧壳，
# 用户就得手工排雷。这里把「发现旧壳 → 改指当前安装」收进 install 流程。
# ---------------------------------------------------------------------------

_LAUNCHER_NAMES = ("hy-mem.cmd", "hy-mem")  # hy-mem.exe 是二进制，不在接管范围


def _is_hymem_launcher_text(text: str) -> bool:
    """判断文件内容是不是 hy-mem 启动器（而非别人的同名程序）。"""
    return ("hy_mem_cli.py" in text) or ("hy-mem.exe" in text)


def _points_to_current(text: str, kind: str) -> bool:
    """启动器内容是否已指向当前安装（避免无谓改写）。

    kind 必须按文件自身类型给：.cmd 比对 cmd 渲染、无扩展名比对 sh 渲染，
    否则 Git Bash 用的 sh 壳会被永远误判为 stale，破坏幂等。
    """
    return text == render_launcher(kind)


def scan_launchers() -> list[tuple[Path, str]]:
    """扫 PATH 上所有 hy-mem 启动器（跳过默认落点，那是 install 自己的地盘）。

    返回 [(路径, 分类)]，分类：
      current —— 已指向当前安装，无需处理
      stale   —— 是 hy-mem 启动器但指向别处（多半已失效），可接管
      foreign —— 内容不像 hy-mem 启动器（同名异姓），绝不动
    """
    try:
        ours = str(bin_dir().resolve()).lower()
    except Exception:  # noqa: BLE001
        ours = ""
    found: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for raw in os.getenv("PATH", "").split(os.pathsep):
        raw = raw.strip().strip('"')
        if not raw:
            continue  # 空项 = 当前目录，与 which() 同口径
        try:
            d = Path(raw).resolve()
        except Exception:  # noqa: BLE001
            continue
        key = str(d).lower()
        if key in seen or key == ours:
            continue
        seen.add(key)
        for name in _LAUNCHER_NAMES:
            f = d / name
            if not f.is_file():
                continue
            try:
                text = _read_raw(f)
            except OSError:
                found.append((f, "foreign"))  # 读不了的不碰
                continue
            if not _is_hymem_launcher_text(text):
                found.append((f, "foreign"))
            elif _points_to_current(text, "cmd" if f.suffix == ".cmd" else "sh"):
                found.append((f, "current"))
            else:
                found.append((f, "stale"))
    return found


def adopt_stale() -> tuple[list[Path], list[str]]:
    """把 PATH 上残留的旧 hy-mem 启动器就地改写为指向当前安装。

    只改「确认是 hy-mem 启动器」的文本文件；同名外部程序与二进制一律不动。
    返回 (已接管的路径, 逐条说明)。
    """
    fixed: list[Path] = []
    notes: list[str] = []
    for f, kind in scan_launchers():
        if kind == "current":
            continue
        if kind != "stale":
            notes.append(f"跳过 {f}（不是 hy-mem 启动器，不动）")
            continue
        body = render_launcher("cmd" if f.suffix == ".cmd" else "sh")
        try:
            f.write_text(body, encoding="utf-8", newline="")
            if f.suffix == "":
                try:
                    f.chmod(0o755)
                except OSError:
                    pass
            fixed.append(f)
            notes.append(f"已接管 {f} → 指向当前安装")
        except OSError as e:
            notes.append(f"改写 {f} 失败：{type(e).__name__}: {e}")
    return fixed, notes


# ---------------------------------------------------------------------------
# 升级接管：新安装沿用旧安装的配置与记忆数据
#
# 场景（2026-10-27 用户需求）：zip 分发包不含 config.json / data（有意设计，
# 防止覆盖用户数据），代价是「解压到新目录升级」后首次运行一切从零 —— 向导
# 重走、记忆全空。实际上旧安装还好好地躺在 PATH 指向的老地方。这里在首次
# 运行时探测：PATH 上若指向另一个带 config.json 的 hy-mem 安装根，就把配置
# 复制过来、storage.dataDir 改指旧数据目录（**原位复用，不拷贝几百 MB 数据**）。
# ---------------------------------------------------------------------------

_LAUNCHER_NAMES_ALL = ("hy-mem.cmd", "hy-mem", "hy-mem.exe")


def _parse_launcher_target(text: str) -> Path | None:
    """从启动器文本解析出它指向的安装根；解析不出返回 None。"""
    tokens = re.findall(r'"([^"]+)"', text)
    for t in tokens:
        if t.replace("\\", "/").endswith(("cli/hy_mem_cli.py", "hymem/cli.py")):
            try:
                return Path(t).resolve().parent.parent
            except OSError:
                return None
    for t in tokens:
        if t.replace("/", "\\").lower().endswith("hy-mem.exe"):
            try:
                return Path(t).resolve().parent
            except OSError:
                return None
    return None


def _current_root(current_root: Path | None = None) -> Path:
    """当前安装根。与 config._root() 同口径：HY_MEM_HOME > app_root()。

    app_root() 只认 sys.frozen / 源码位置，测试与「指到别处跑」的场景靠
    HY_MEM_HOME 改根 —— 两处口径不一致时，接管会把当前安装误判成旧安装。
    """
    if current_root is not None:
        return current_root.resolve()
    env = os.getenv("HY_MEM_HOME", "").strip()
    if env:
        return Path(env).resolve()
    return app_root().resolve()


def find_previous_install(current_root: Path | None = None) -> tuple[Path | None, str]:
    """扫 PATH，找到「另一个」带 config.json 的 hy-mem 安装根。

    返回 (旧安装根, 发现途径说明)；找不到返回 (None, "")。
    hy-mem.exe 直接躺在 PATH 目录里也算（说明旧安装目录本身在 PATH 上）。
    """
    cur = _current_root(current_root)
    seen: set[str] = set()
    for raw in os.getenv("PATH", "").split(os.pathsep):
        raw = raw.strip().strip('"')
        if not raw:
            continue
        try:
            d = Path(raw).resolve()
        except Exception:  # noqa: BLE001
            continue
        key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        for name in _LAUNCHER_NAMES_ALL:
            f = d / name
            if not f.is_file():
                continue
            if name == "hy-mem.exe":
                roots = [d]
            else:
                try:
                    text = _read_raw(f)
                except OSError:
                    continue
                if not _is_hymem_launcher_text(text):
                    continue  # 同名异姓，不碰
                t = _parse_launcher_target(text)
                roots = [t] if t else []
            for r in roots:
                try:
                    if r.resolve() == cur:
                        continue  # 自己不算「旧安装」
                except OSError:
                    continue
                if (r / "config.json").is_file():
                    return r, f"PATH 上的 {f.name}"
    return None, ""


def adopt_previous_install(current_root: Path | None = None) -> tuple[dict | None, list[str]]:
    """新装（无 config.json）时接管旧安装的配置与记忆数据。

    只复制配置这一个 JSON；记忆数据不搬，把 storage.dataDir 写成旧数据目录
    的绝对路径原位复用。返回 (接管信息 dict 或 None, 逐条说明)。

    跳过条件（按序）：
      - 当前已有 config.json（原地升级 / 已配置）
      - HY_MEM_CONFIG 显式指定（用户自管配置，不该被我们动）
      - HY_MEM_NO_ADOPT=1（显式退出）
      - PATH 上找不到另一个带 config.json 的安装
    """
    cur = _current_root(current_root)
    if (cur / "config.json").is_file():
        return None, []
    if os.getenv("HY_MEM_CONFIG"):
        return None, ["HY_MEM_CONFIG 已显式指定，跳过旧安装接管"]
    if os.getenv("HY_MEM_NO_ADOPT", "").strip() == "1":
        return None, ["HY_MEM_NO_ADOPT=1，跳过旧安装接管"]
    old_root, src = find_previous_install(cur)
    if not old_root:
        return None, []
    try:
        cfg = json.loads((old_root / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, [f"旧安装 {old_root} 的 config.json 读不了（{type(e).__name__}），不接管"]
    storage = cfg.get("storage") or {}
    raw_dir = str(storage.get("dataDir") or "").strip()
    old_data = Path(raw_dir) if raw_dir else old_root / "data"
    if not old_data.is_dir():
        return None, [f"旧安装 {old_root} 的数据目录 {old_data} 不存在，不接管"]
    storage["dataDir"] = str(old_data)
    cfg["storage"] = storage
    target = cur / "config.json"
    try:
        target.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except OSError as e:
        return None, [f"写入 {target} 失败：{type(e).__name__}: {e}"]
    info = {"oldRoot": str(old_root), "via": src, "dataDir": str(old_data), "config": str(target)}
    notes = [
        f"检测到旧安装 {old_root}（经 {src}）",
        f"已沿用其配置 → {target}",
        f"记忆数据原位复用（不拷贝）：{old_data}",
        "提示：数据仍在旧目录；想收进本安装（之后才能删旧目录）就跑 hy-mem migrate",
    ]
    return info, notes
