# -*- coding: utf-8 -*-
# 门面（docs/specs/2026-11-02-cli-split-ports/）：实现已归位 backends/hymemory/autoevops.py。
# 旧路径 hymem.core.autoevops 保持可用（cmds/server/tests 引用零感知）；vendor import 只准出现在 backends/。
from hymem.backends.hymemory.autoevops import (  # noqa: F401
    _BACKOFF_CAP_EXP,
    _BACKOFF_CAP_HOURS,
    _MAX_RECORDS,
    _RUN_LOCK,
    _auto_cfg,
    _backoff_hours,
    _fact_count,
    _record,
    _run_auto,
    _save_state,
    _state_path,
    load_state,
    maybe_auto_evolve,
    should_attempt,
    status_summary,
)
