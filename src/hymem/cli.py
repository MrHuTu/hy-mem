#!/usr/bin/env python
"""hy-mem — Hy-Memory SDK 的命令行外壳.

设计约束（照搬 CLI-Anything HARNESS.md 的三条硬规则，但代码手写）：
  1. 每个子命令都支持 --json，输出机器可解析的单行 JSON
  2. exit code 有语义：0=成功 1=用法/环境错 2=运行时错
  3. 必须有 doctor 子命令，能一条命令判定环境是否可用

S2 门面（docs/specs/2026-11-02-cli-split-ports/）：实现已按域拆入 hymem/cmds/
（base/write/read/maint/queue/config/parser/entry）。本文件只做 re-export，
 hymem.cli 的既有引用（pyproject 入口 hymem.cli:main、__main__、_pyi_entry、
 tests 的 hy_mem_cli._xxx）零感知。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# S1 分层（2026-10-29）：本文件可被三种方式加载——
#   1) python src/hymem/cli.py      （直接执行，sys.path[0]=src/hymem/）
#   2) python -m hymem.cli      （包模式，sys.path[0]=仓库根）
#   3) PyInstaller 冻结（_pyi_entry） —— 模块已收集，path 无关
# 包导入（hymem.core / hymem.ui / hymem.mcp）需要仓库根在 path 上，这里统一兜底。
# ---------------------------------------------------------------------------
import sys as _sys
from pathlib import Path as _Path  # bootstrap 自用，提前于业务 import
if __package__ in (None, "") and not getattr(_sys, "frozen", False):
    # S2 src 布局：包根是 src/（hymem 所在目录），不是仓库根
    _PKG_ROOT = str(_Path(__file__).resolve().parents[1])
    if _PKG_ROOT not in _sys.path:
        _sys.path.insert(0, _PKG_ROOT)

from hymem.cmds.base import (  # noqa: F401
    CommitFailed, DEFAULT_ENV, EXIT_INTERNAL, EXIT_OK, EXIT_RUNTIME,
    EXIT_USAGE, ExtractFailed, Path, SKILL_ROOT, _EXIT_CODES,
    _FIELDS, _GLOBAL_FLAGS, _GLOBAL_VALUED, _JSON, _LAST_FAIL,
    _Palette, _QUIET, _SURROGATE_RE, _add_kwargs, _bind_tags,
    _bind_tags_to_new, _decode_stdin, _effective_tags, _embedding_unhealthy, _filter_by_tags,
    _flatten_search, _ids_of, _in_range, _item_ts, _new_ids_only,
    _parse_memory_at, _parse_when, _pending_jobs, _project, _read_text,
    _remember_auto, _report_missing, _resolve_home, _snapshot_ids, _spawn_detached_serve,
    _spawn_detached_worker, _split_lines, _strip_surrogates, _worker_cap, add_sync_with_retry,
    argparse, asyncio, build_embedding_service, c, classify_error,
    commit_extracted_with_retry, config_defaults_user, data_dir, datetime, embedding_provider,
    emit, error_detail, extract_phase, fail, find_derived_nodes,
    hoist_globals, identity, identity_lists, json, load_env,
    make_client, netprobe, ok, os, prepare_commit,
    readiness, require_ready, socket, store, subprocess,
    sweep_derived_nodes, sys, time, vdb_endpoint,
)
from hymem.cmds.write import (  # noqa: F401
    _cmd_add_scene, cmd_add, cmd_archive, cmd_delete, cmd_finalize,
    cmd_forget, cmd_tags, cmd_untag, cmd_update,
)
from hymem.cmds.read import (  # noqa: F401
    _cmd_search_scene, cmd_get, cmd_history, cmd_list, cmd_outcome,
    cmd_profile, cmd_search,
)
from hymem.cmds.maint import (  # noqa: F401
    _LAT_A, _LAT_B, _dir_size, _estimate_write_latency, _health_thresholds,
    _scan_all_memories, _stats_health, _status_icon, cmd_backup, cmd_doctor,
    cmd_evolve, cmd_evolve_status, cmd_graph_build, cmd_log, cmd_restore,
    cmd_stats, cmd_vdb_drop, cmd_vdb_plan, cmd_vdb_reembed,
    cmd_vdb_reembed_graph, cmd_vdb_scan,
)
from hymem.cmds.queue import (  # noqa: F401
    _LaneState, _batch_compat, _execute_batch, _execute_job, _worker_id,
    cmd_drain, cmd_queue, cmd_retry, cmd_serve, cmd_worker,
)
from hymem.cmds.config import (  # noqa: F401
    _check_chain_repo, cmd_config, cmd_install, cmd_menu, cmd_migrate,
    cmd_setup, cmd_skill, cmd_tui_test, cmd_user, cmd_version,
)
from hymem.cmds.parser import (  # noqa: F401
    _Parser, _print_short_help, build_parser, cmd_help,
)
from hymem.cmds.entry import (  # noqa: F401
    _force_utf8, _record_operation, _sub_argv, cmd_mcp, main,
)

# 显式导出清单：pyflakes 不识别 `# noqa`，re-export 只能靠 __all__ 标记为「已使用」。
# 这里列的是本模块对外暴露的全部名字（不含 __dunder），添加/删除导出时必须同步改这里。
__all__ = [
    "CommitFailed",
    "DEFAULT_ENV",
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_RUNTIME",
    "EXIT_USAGE",
    "ExtractFailed",
    "Path",
    "SKILL_ROOT",
    "_EXIT_CODES",
    "_FIELDS",
    "_GLOBAL_FLAGS",
    "_GLOBAL_VALUED",
    "_JSON",
    "_LAST_FAIL",
    "_LAT_A",
    "_LAT_B",
    "_LaneState",
    "_Palette",
    "_Parser",
    "_QUIET",
    "_SURROGATE_RE",
    "_add_kwargs",
    "_batch_compat",
    "_bind_tags",
    "_bind_tags_to_new",
    "_check_chain_repo",
    "_cmd_add_scene",
    "_cmd_search_scene",
    "_decode_stdin",
    "_dir_size",
    "_effective_tags",
    "_embedding_unhealthy",
    "_estimate_write_latency",
    "_execute_batch",
    "_execute_job",
    "_filter_by_tags",
    "_flatten_search",
    "_force_utf8",
    "_health_thresholds",
    "_ids_of",
    "_in_range",
    "_item_ts",
    "_new_ids_only",
    "_parse_memory_at",
    "_parse_when",
    "_pending_jobs",
    "_print_short_help",
    "_project",
    "_read_text",
    "_record_operation",
    "_remember_auto",
    "_report_missing",
    "_resolve_home",
    "_scan_all_memories",
    "_snapshot_ids",
    "_spawn_detached_serve",
    "_spawn_detached_worker",
    "_split_lines",
    "_stats_health",
    "_status_icon",
    "_strip_surrogates",
    "_sub_argv",
    "_worker_cap",
    "_worker_id",
    "add_sync_with_retry",
    "annotations",
    "argparse",
    "asyncio",
    "build_embedding_service",
    "build_parser",
    "c",
    "classify_error",
    "cmd_add",
    "cmd_archive",
    "cmd_backup",
    "cmd_config",
    "cmd_delete",
    "cmd_doctor",
    "cmd_drain",
    "cmd_evolve",
    "cmd_evolve_status",
    "cmd_finalize",
    "cmd_forget",
    "cmd_get",
    "cmd_graph_build",
    "cmd_help",
    "cmd_history",
    "cmd_install",
    "cmd_list",
    "cmd_log",
    "cmd_mcp",
    "cmd_menu",
    "cmd_migrate",
    "cmd_outcome",
    "cmd_profile",
    "cmd_queue",
    "cmd_restore",
    "cmd_retry",
    "cmd_search",
    "cmd_serve",
    "cmd_setup",
    "cmd_skill",
    "cmd_stats",
    "cmd_tags",
    "cmd_tui_test",
    "cmd_untag",
    "cmd_update",
    "cmd_user",
    "cmd_vdb_drop",
    "cmd_vdb_plan",
    "cmd_vdb_reembed",
    "cmd_vdb_reembed_graph",
    "cmd_vdb_scan",
    "cmd_version",
    "cmd_worker",
    "commit_extracted_with_retry",
    "config_defaults_user",
    "data_dir",
    "datetime",
    "embedding_provider",
    "emit",
    "error_detail",
    "extract_phase",
    "fail",
    "find_derived_nodes",
    "hoist_globals",
    "identity",
    "identity_lists",
    "json",
    "load_env",
    "main",
    "make_client",
    "netprobe",
    "ok",
    "os",
    "prepare_commit",
    "readiness",
    "require_ready",
    "socket",
    "store",
    "subprocess",
    "sweep_derived_nodes",
    "sys",
    "time",
    "vdb_endpoint",
]

if __name__ == "__main__":
    sys.exit(main())
