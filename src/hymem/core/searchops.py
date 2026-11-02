# -*- coding: utf-8 -*-
# 门面（docs/specs/2026-11-02-cli-split-ports/）：实现已归位 backends/hymemory/searchops.py。
# 旧路径 hymem.core.searchops 保持可用（cmds/server/tests 引用零感知）；vendor import 只准出现在 backends/。
from hymem.backends.hymemory.searchops import (  # noqa: F401
    _bind_tags,
    _bind_tags_to_new,
    _effective_tags,
    _embedding_unhealthy,
    _filter_by_tags,
    _flatten_search,
    _ids_of,
    _l6_schema_stats,
    _new_ids_only,
    _parse_memory_at,
    _pending_jobs,
    _remember_auto,
    _snapshot_ids,
    _spawn_detached_serve,
    _spawn_detached_worker,
    _worker_cap,
    fetch_l6_schemas,
    fill_chain_block,
    schema_state_meta,
)
