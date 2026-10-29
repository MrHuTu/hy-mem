# -*- coding: utf-8 -*-
"""分层守卫（S1 契约 A4）—— 扫 cli/ 三层包结构的依赖违例。

判据（docs/specs/2026-10-29-s1-layering/00-contract.md）：
  L1  core 不得 import ui / mcp / hy_mem_cli
  L2  ui 与 mcp 互不 import；两者可依赖 core
  L3  除 hy_mem_cli.py 外，任何模块不得 import 入口 hy_mem_cli
      —— 例外存量：mcp/server.py 的 12 处 from hymem.cli import（S3 消除）
  L4  __init__.py 必须为空（防 import 副作用串包）
  L5  禁止裸模块名 import（import store / from config import ...）：
      分层后必须用包全名（from hymem.core import store），裸名在包模式下
      只有 cli/ 目录在 path 时才能碰巧工作，掩盖结构破坏

用法：.venv/Scripts/python.exe scripts/check_layering.py
退出码：0 = 无违例；1 = 有违例（发版门禁用）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "src" / "hymem"

# S3 待消除的例外（契约记录在案）：mcp/server.py 反向依赖入口的符号
KNOWN_EXCEPTIONS = set()  # S3-2（2026-10-29）：13 处反向依赖已全部消除，例外表清空

LAYERS = {
    "core": {"hymem.core", "hymem.core.*"},
    "ui": {"hymem.ui", "hymem.ui.*"},
    "mcp": {"hymem.mcp", "hymem.mcp.*"},
}

# 内部模块裸名（分层前的模块名）
BARE_NAMES = [
    "config", "errors", "netprobe", "oplog", "presets", "runtime_check",
    "storage_ops", "store", "vdb_endpoint", "local_embedder", "install_ops",
    "helptext", "i18n", "menu", "setup_wizard", "skill_ops", "tui",
    "mcp_server", "hy_mem_cli",
]

IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_.]*)", re.M
)


def rel(p: Path) -> str:
    return p.relative_to(CLI).as_posix()


def check() -> int:
    violations: list[str] = []

    # L4：__init__ 必须为空
    for init in CLI.rglob("__init__.py"):
        if init.read_text(encoding="utf-8").strip():
            violations.append(f"L4 {rel(init)} 非空（import 副作用风险）")

    for p in sorted(CLI.rglob("*.py")):
        if p.name == "__init__.py" or p.name == "_pyi_entry.py":
            continue  # _pyi_entry 是 PyInstaller 入口 shim，天然引用入口
        src = p.read_text(encoding="utf-8")
        where = rel(p)
        layer = p.parent.name  # core / ui / mcp（cli 根文件则是 'cli'）

        for m in IMPORT_RE.finditer(src):
            mod = m.group(1)
            # 只管内部依赖
            if not mod.startswith("cli.") and mod != "cli":
                # L5：裸内部模块名
                root = mod.split(".")[0]
                if root in BARE_NAMES:
                    violations.append(
                        f"L5 {where} 裸名 import：{mod}（应用包全名 cli.<层>.<模块>）"
                    )
                continue
            # L1：core 不许碰 ui/mcp/入口
            if layer == "core":
                if mod.startswith("hymem.ui") or mod.startswith("hymem.mcp") or mod == "hymem.cli" or mod.startswith("hymem.cli"):
                    if (where, "hymem.cli") not in KNOWN_EXCEPTIONS:
                        violations.append(f"L1 {where} 反向依赖上层：{mod}")
            # L2：ui 与 mcp 互不 import
            if layer == "ui" and mod.startswith("hymem.mcp"):
                violations.append(f"L2 {where} import mcp：{mod}")
            if layer == "mcp" and mod.startswith("hymem.ui"):
                violations.append(f"L2 {where} import ui：{mod}")
            # L3：不得 import 入口（例外：mcp/server.py 存量 + 入口自身）
            if mod == "hymem.cli" or mod.startswith("hymem.cli"):
                if where != "hy_mem_cli.py" and (where, "hymem.cli") not in KNOWN_EXCEPTIONS:
                    violations.append(f"L3 {where} import 入口：{mod}")

    if violations:
        print(f"分层违例 {len(violations)} 处：")
        for v in violations:
            print("  " + v)
        return 1
    print("分层守卫通过：L1–L5 无违例")
    return 0


if __name__ == "__main__":
    sys.exit(check())
