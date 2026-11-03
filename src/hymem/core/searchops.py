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
    scenecore,
    searchcore,
    vdb_memories,
)

# 显式导出清单：pyflakes 不识别 `# noqa`，re-export 只能靠 __all__ 标记为「已使用」。
# 这里列的是本模块对外暴露的全部名字（不含 __dunder），添加/删除导出时必须同步改这里。
__all__ = [
    "_bind_tags",
    "_bind_tags_to_new",
    "_effective_tags",
    "_embedding_unhealthy",
    "_filter_by_tags",
    "_flatten_search",
    "_ids_of",
    "_l6_schema_stats",
    "_new_ids_only",
    "_parse_memory_at",
    "_pending_jobs",
    "_remember_auto",
    "_snapshot_ids",
    "_spawn_detached_serve",
    "_spawn_detached_worker",
    "_worker_cap",
    "fetch_l6_schemas",
    "fill_chain_block",
    "scenecore",
    "schema_state_meta",
    "searchcore",
    "vdb_memories",
]
