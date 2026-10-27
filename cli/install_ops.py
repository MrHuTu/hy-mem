"""把 hy-mem 装成一个稳定可调用的命令（launcher）。

为什么需要：SKILL.md 写的是 `hy-mem add ...`，但智能体执行时那条命令必须在
PATH 上真的存在。产物是个目录（dist\\hy-mem\\hy-mem.exe），没谁会把一个随机
目录加进 PATH —— 于是 skill 里的每条命令都落空，看起来就像「帮助/技能没生效」。
这里生成一个薄启动器放到固定位置，把路径问题一次性解决。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def app_root() -> Path:
    """安装根：打包后是 exe 所在目录，源码模式是 cli/ 的上一级。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def target_command() -> list[str]:
    """真正要执行的命令（argv 前缀）。"""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    root = app_root()
    py = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if py.exists():
        return [str(py), str(root / "cli" / "hy_mem_cli.py")]
    return [sys.executable, str(root / "cli" / "hy_mem_cli.py")]


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


def _write_one(path: Path, body: str) -> tuple[bool, str]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8", errors="replace") == body:
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
