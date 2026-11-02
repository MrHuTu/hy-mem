# -*- coding: utf-8 -*-
# 门面（docs/specs/2026-11-02-cli-split-ports/）：实现已归位 backends/hymemory/clientops.py。
# 旧路径 hymem.core.clientops 保持可用（cmds/server/tests 引用零感知）；vendor import 只准出现在 backends/。
from hymem.backends.hymemory.clientops import (  # noqa: F401
    _vector_collection,
    add_sync_with_retry,
    build_embedding_service,
    embedding_provider,
    find_derived_nodes,
    is_transient_db_error,
    sdk_info,
    make_client,
    sweep_derived_nodes,
)
