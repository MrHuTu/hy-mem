"""SKILL.md 的定位与分发。

为什么单独一个模块：智能体能不能用上这个 CLI，取决于**两个**条件同时成立 ——
命令在 PATH 上（install 解决），且宿主能读到 SKILL.md（本模块解决）。
以前 SKILL.md 只躺在源码目录里，打包产物里根本没有，宿主自然「没看到帮助」。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

NAME = "hy-mem"


def app_root() -> Path:
    """安装根：打包后是 exe 所在目录，源码模式是 cli/ 的上一级。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # S1 分层（2026-10-29）：本文件在 cli/<层>/ 下，安装根要多上一层
    return Path(__file__).resolve().parent.parent.parent


def candidates() -> list[Path]:
    """SKILL.md 可能的位置，按优先级。"""
    root = app_root()
    return [root / "skill" / NAME / "SKILL.md", root / "skill" / "SKILL.md"]


def skill_path() -> Path | None:
    for p in candidates():
        if p.exists():
            return p
    return None


def digest(p: Path) -> str:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def host_dirs() -> list[tuple[str, Path]]:
    """常见宿主的 skill 目录。只返回父目录存在的项，避免报一堆无关路径。"""
    home = Path.home()
    table = [
        ("workbuddy", home / ".workbuddy" / "skills"),
        ("qoder", home / ".qoder" / "skills"),
        ("trae-cn", home / ".trae-cn" / "skills"),
        ("skills-manager", home / ".skills-manager" / "skills"),
        ("claude", home / ".claude" / "skills"),
    ]
    return [(name, d) for name, d in table if d.exists()]


def status() -> dict:
    """SKILL.md 本体位置 + 各宿主副本是否一致。"""
    src = skill_path()
    src_md5 = digest(src) if src else ""
    hosts = []
    for name, base in host_dirs():
        copy = base / NAME / "SKILL.md"
        hosts.append(
            {
                "host": name,
                "dir": str(base / NAME),
                "exists": copy.exists(),
                "inSync": bool(copy.exists() and src_md5 and digest(copy) == src_md5),
            }
        )
    return {
        "ok": src is not None,
        "source": str(src) if src else None,
        "bytes": src.stat().st_size if src else 0,
        "md5": src_md5,
        "hosts": hosts,
    }


def install_to(target: Path, overwrite: bool = True) -> tuple[bool, str]:
    src = skill_path()
    if src is None:
        return False, "找不到 SKILL.md（产物应带 skill/hy-mem/SKILL.md）"
    dst = target / "SKILL.md"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and not overwrite:
            return True, "已存在，跳过"
        if dst.exists() and digest(dst) == digest(src):
            return True, "已是最新"
        shutil.copy2(src, dst)
        return True, f"已写入 {dst}"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def install_all() -> list[tuple[str, bool, str]]:
    """把 SKILL.md 铺到所有检测到的宿主。返回 [(宿主, 成功, 说明)]。"""
    out: list[tuple[str, bool, str]] = []
    for name, base in host_dirs():
        ok, msg = install_to(base / NAME)
        out.append((name, ok, msg))
    return out
