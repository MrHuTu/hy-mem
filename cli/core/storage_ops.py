"""数据目录运维 —— 用量、剩余空间、路径归一化、迁移、备份、重建.

抽成独立模块的原因：交互界面（menu）和设置向导（setup_wizard）都要用，
放在任一边都会让另一边反向 import，形成环。

关于「迁移」的取舍：迁移成功后**不删源目录**。这个库是长期记忆，删源是不可逆
操作，而磁盘上多留一份旧副本的代价只是空间。想清理就让用户自己删。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

# 迁移时跳过的运行时垃圾：锁文件由新进程重建，日志没有搬迁价值
_SKIP_NAMES = {"write.lock", "hy_mem_queue.db-wal", "hy_mem_queue.db-shm"}


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def normalize_path(raw: str) -> str:
    """把用户输入变成可用的绝对路径。

    支持 %USERPROFILE% 展开、~、前后引号（从资源管理器复制路径常带引号）、
    正斜杠，最后统一去尾部分隔符。
    """
    s = str(raw or "").strip().strip('"').strip("'")
    if not s:
        return ""
    if os.name == "nt":
        s = re.sub(r"%([^%]+)%", lambda m: os.getenv(m.group(1), m.group(0)), s)
    s = os.path.expanduser(s)
    # 相对路径按当前工作目录补全，避免 config 里存一个随 cwd 漂移的路径
    p = Path(s)
    if not p.is_absolute():
        p = Path(os.getcwd()) / p
    return str(p.resolve()) if not p.exists() else str(p)


def is_pathlike(s: str) -> bool:
    """粗筛明显不是路径的输入。

    起因：向导用管道喂输入时曾错位一位，把确认答案 "n" 当成存储目录写了进去，
    而文本输入对一切非空值都是原样接受的。路径是少数「写错就立刻毁掉一切」的
    配置项（数据全落到意外位置），所以这里宁可判严。
    """
    if not s or len(s) < 2:
        return False
    if os.path.isabs(s):
        return True
    if s.startswith("\\\\") or s.startswith("//"):
        return True
    if s.startswith("%") or s.startswith("~"):
        return True
    return "/" in s or "\\" in s


def dir_usage(path: str) -> tuple[int, int]:
    """返回 (总字节, 文件数)。目录不存在返回 (0, 0)。"""
    p = Path(path)
    if not p.exists():
        return 0, 0
    total = 0
    files = 0
    for root, _dirs, names in os.walk(p):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass
    return total, files


def free_space(path: str) -> int:
    """所在卷的剩余字节。路径不存在时向上找第一个存在的祖先。"""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).free
    except Exception:  # noqa: BLE001
        return 0


def probe_writable(path: str) -> tuple[bool, str]:
    """真写一个探针文件，比 os.access 可靠（Windows 上 access 基本恒真）。"""
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return False, f"无法创建目录：{type(e).__name__}: {e}"
    probe = p / ".hy-mem-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, "可写"
    except Exception as e:  # noqa: BLE001
        return False, f"目录不可写：{type(e).__name__}: {e}"


def check_target(path: str, need_bytes: int = 0) -> tuple[bool, str]:
    """校验目标目录可用：像路径、能建、能写、空间够。"""
    if not is_pathlike(path):
        return False, f"这不像一个路径：{path!r}（要绝对路径，例如 D:\\hy-mem\\data）"
    ok, msg = probe_writable(path)
    if not ok:
        return False, msg
    if need_bytes:
        free = free_space(path)
        if free and free < need_bytes * 1.1:
            return False, f"空间不足：需要约 {human_size(need_bytes)}，可用 {human_size(free)}"
    return True, "可用"


def _copy_tree(src: Path, dst: Path) -> int:
    """逐文件复制，跳过锁文件。返回复制的文件数。"""
    n = 0
    for root, _dirs, names in os.walk(src):
        rel = Path(root).relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            if name in _SKIP_NAMES:
                continue
            s = Path(root) / name
            try:
                shutil.copy2(s, target_dir / name)
                n += 1
            except Exception:  # noqa: BLE001
                # 单个文件失败不该中断整次迁移：chroma 的 sqlite 有时被占用
                continue
    return n


def migrate(src: str, dst: str) -> tuple[bool, str]:
    """把数据目录整体复制到新位置。保留源目录作为回滚点。"""
    s, d = Path(src), Path(dst)
    if str(s.resolve()) == str(d.resolve()):
        return False, "源目录与目标目录相同"
    size, files = dir_usage(src)
    if files == 0:
        return True, "源目录为空，无需迁移"
    ok, msg = check_target(dst, need_bytes=size)
    if not ok:
        return False, msg
    try:
        n = _copy_tree(s, d)
    except Exception as e:  # noqa: BLE001
        return False, f"复制失败：{type(e).__name__}: {e}"
    if n == 0:
        return False, "没有复制到任何文件（源目录可能被占用）"
    return True, f"已复制 {n}/{files} 个文件（{human_size(size)}），源目录保留：{src}"


def backup(path: str) -> tuple[bool, str]:
    """就地复制一份 .bak-时间戳，用于「改模型前先留底」。"""
    src = Path(path)
    if not src.exists():
        return False, "数据目录不存在"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = src.with_name(f"{src.name}.bak-{stamp}")
    try:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*_SKIP_NAMES))
        size, files = dir_usage(str(dst))
        return True, f"已备份 {files} 个文件（{human_size(size)}）→ {dst}"
    except Exception as e:  # noqa: BLE001
        return False, f"备份失败：{type(e).__name__}: {e}"


def wipe(path: str) -> tuple[bool, str]:
    """先备份再清空内容（保留目录本身）。维度不兼容时用。"""
    ok, msg = backup(path)
    p = Path(path)
    try:
        if p.exists():
            for item in p.iterdir():
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    try:
                        item.unlink()
                    except OSError:
                        pass
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        return False, f"清空失败：{type(e).__name__}: {e}"
    return True, (msg if ok else "（旧数据未能备份，已直接清空）")
