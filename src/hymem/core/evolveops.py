# -*- coding: utf-8 -*-
# 门面（docs/specs/2026-11-02-cli-split-ports/）：实现已归位 backends/hymemory/evolveops.py。
# 旧路径 hymem.core.evolveops 保持可用（cmds/server/tests 引用零感知）；vendor import 只准出现在 backends/。
from hymem.backends.hymemory.evolveops import (  # noqa: F401
    IDLE_COMPLETION_TOKENS,
    KUZU_LOCK_MARKERS,
    ModeDisabled,
    _agent_results,
    _aggregate,
    _call_evolve,
    _effective_model,
    _error_hint,
    _native_language,
    _schema_count,
    build_evolve_client,
    evolve,
    kuzu_lock_error,
    summarize,
)

# 显式导出清单：pyflakes 不识别 `# noqa`，re-export 只能靠 __all__ 标记为「已使用」。
# 这里列的是本模块对外暴露的全部名字（不含 __dunder），添加/删除导出时必须同步改这里。
__all__ = [
    "IDLE_COMPLETION_TOKENS",
    "KUZU_LOCK_MARKERS",
    "ModeDisabled",
    "_agent_results",
    "_aggregate",
    "_call_evolve",
    "_effective_model",
    "_error_hint",
    "_native_language",
    "_schema_count",
    "build_evolve_client",
    "evolve",
    "kuzu_lock_error",
    "summarize",
]
