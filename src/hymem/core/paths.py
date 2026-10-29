# -*- coding: utf-8 -*-
"""路径判定唯一真源（S2-2）：安装根 app_root() 与相关路径。

S1 的暗雷教训：app_root 曾在 5 个文件里各自用 __file__.parent.parent 推算，
目录层级一变全部偏移（worker 拉起指向不存在的路径、配置落到错误位置）。
这里统一为**一个实现**，其他模块只许引用：

    from hymem.core.paths import app_root

规则：
- 冻结（PyInstaller）：exe 所在目录
- 源码：src/hymem/core/paths.py → parents[3] = 仓库根（含 src 布局）
- editable 安装（pip install -e .）：site-packages 里有 __editable__ 指针，
  本文件真实路径仍在仓库 src/ 下，parents[3] 依然正确
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """安装根：exe 模式=exe 目录；源码模式=仓库根（src 布局）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]
