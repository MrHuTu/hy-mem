# -*- coding: utf-8 -*-
"""产品版本 —— 唯一真源（S1 分层：版本是产品事实，归 core）。

消费者：ui/helptext（帮助文案）、mcp/server（serverInfo 与 get_version）、
打包脚本、check_secrets 之外的一切需要报版本号的地方。
"""
from __future__ import annotations

VERSION = "1.0.0-rc.2"
