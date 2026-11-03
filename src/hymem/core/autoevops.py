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

# 显式导出清单：pyflakes 不识别 `# noqa`，re-export 只能靠 __all__ 标记为「已使用」。
# 这里列的是本模块对外暴露的全部名字（不含 __dunder），添加/删除导出时必须同步改这里。
__all__ = [
    "_BACKOFF_CAP_EXP",
    "_BACKOFF_CAP_HOURS",
    "_MAX_RECORDS",
    "_RUN_LOCK",
    "_auto_cfg",
    "_backoff_hours",
    "_fact_count",
    "_record",
    "_run_auto",
    "_save_state",
    "_state_path",
    "load_state",
    "maybe_auto_evolve",
    "should_attempt",
    "status_summary",
]
