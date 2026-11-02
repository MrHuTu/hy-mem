# -*- coding: utf-8 -*-
# 门面（docs/specs/2026-11-02-cli-split-ports/）：实现已归位 backends/hymemory/evolveops.py。
# 旧路径 hymem.core.evolveops 保持可用（cmds/server/tests 引用零感知）；vendor import 只准出现在 backends/。
from hymem.backends.hymemory.evolveops import (  # noqa: F401
    IDLE_COMPLETION_TOKENS,
    KUZU_LOCK_MARKERS,
    ModeDisabled,
    _LANG_DIRECTIVES,
    _LANG_STATE,
    _agent_results,
    _aggregate,
    _effective_model,
    _error_hint,
    _schema_count,
    apply_language_directive,
    build_evolve_client,
    evolve,
    kuzu_lock_error,
    summarize,
)
