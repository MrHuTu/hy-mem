# hy-mem

Local long-term memory service providing cross-session memory storage and semantic retrieval capabilities for AI Agents. Built upon Tencent Hy-Memory SDK 2.0.5, supporting Qwen3.7 cloud embedding (1024-dimensional vectors).

## Features

- **Semantic Retrieval**: Memory search based on vector similarity
- **Async Writing**: Non-blocking queuing mechanism, supports queue consumption
- **Tag Management**: Flexible tag system supporting archiving and classification
- **Interactive Configuration**: Visual setup wizard and health check
- **Multi-Identity Support**: Distinguish different Agent memory spaces via `user_id`
- **L6 Cognitive Layer (ultra only)**: `evolve` condenses a batch of facts into "behavior patterns" that are retrieved semantically alongside facts. Its model is configured separately (`config set evolve.model=…`) — **use a small analysis model for extraction** (`llm.model`, e.g. `tongyi-xiaomi-analysis-flash`: closed-set judgments, fast), **and a stronger general-purpose model for induction** (`evolve.model`, e.g. `deepseek-v4.1-flash`): small intent-classification models can extract but cannot do open-ended induction
- ⚠️ **ultra is single-writer**: the L6 graph store uses kuzu (exclusive lock, no `read_only`), so a **second process on the same data directory always fails** (measured: 2 concurrent `add` → 1 ok / 1 fail). Do not run CLI and MCP against the same store in ultra; stay on **pro** if you need multi-process / multi-subagent concurrent writes. While MCP holds the graph, `evolve` only works through the MCP tool — the CLI `hy-mem evolve` will hit the lock

## Quick Start

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Initialize Configuration

Run the setup wizard on first launch:

```bash
python -m cli.hy_mem_cli setup
```

Or start the interactive menu:

```bash
python -m cli.hy_mem_cli menu
```

### Basic Commands

| Command | Description |
|------|------|
| `add` | Add memory (supports single/batch/tags) |
| `search` | Semantic memory search |
| `list` | List memories |
| `get` | Get details of a single memory |
| `delete` | Delete memory (logical deletion) |
| `forget` | Permanently delete memory |
| `tags` | List all tags |
| `untag` | Unbind tags |
| `archive` | Archive management |
| `stats` | Statistics and health check |
| `queue` | View async queue |
| `worker` | Consume queue tasks |

A complete list of commands can be viewed via `python -m cli.hy_mem_cli help`.

## Configuration Description

The configuration file is located at `config.json`, supporting the following items:

- **embedding**: Embedding model configuration (provider, model, api_key)
- **llm**: Large Language Model configuration. This is the **S1 extraction model — prefer a small analysis model** (information extraction, scene classification and other closed-set tasks), e.g. `tongyi-xiaomi-analysis-flash`: called on every `add`, so speed and cost matter more than open-ended generation
- **evolve**: L6 induction settings (model / maxTokens 8000 / schemaLimit 3 / schemaMinScore 0.4). **model is the S2 induction model — use a stronger general-purpose model** (e.g. `deepseek-v4.1-flash`): open-ended induction is make-or-break, and small analysis models cannot do it (measured: 7 completion tokens while self-reporting success). Leave empty to fall back to `llm.model` (i.e. no split). Entry points: `config set evolve.model=…` or the "沉淀模型" item in the `setup` panel (effective in ultra only)
- **storage**: Storage path configuration
- **user_id**: Current identity identifier
- **search**: Retrieval parameters (min_score, etc.)

Configuration can also be overridden via environment variables; see `AGENTS.md` for details.

## Project Structure

```
hy-mem/
├── cli/                    # CLI Core Module
│   ├── hy_mem_cli.py      # Main entry point, command parsing and dispatch
│   ├── config.py          # Configuration loading and management
│   ├── store.py           # SQLite database operations
│   ├── local_embedder.py  # Local embedding service
│   ├── tui.py             # Terminal UI components
│   ├── menu.py            # Interactive menu
│   ├── setup_wizard.py    # Setup wizard
│   └── ...
├── skill/                 # Skill Definitions
├── scripts/               # Project scripts (build / secret gate / test suites)
│   ├── build_exe.py      # Packaging script
│   ├── check_secrets.py  # Pre-commit secret scan
│   ├── test_cli.py       # CLI Testing
│   └── ...
├── tests/                 # 14 suites (cli / ui / menu / setup / setup_ui / mcp / queue / vdb /
│                          #   oplog / i18n / encoding / concurrency / scene / evolve)
├── .local-test/           # Local scratch scripts (probes / experiments / repros), git-ignored
├── data/                  # Runtime data (git-ignored, auto-created)
│   ├── data/             # Memory data (SQLite + Vector DB)
│   ├── logs/             # Logs
│   └── hy_mem_queue.db   # Async task queue
└── AGENTS.md             # Project Handover Documentation
```

## Development Instructions

### Run in Source Mode

```bash
python -m cli.hy_mem_cli <command> [options]
```

### Package as Executable

```bash
python scripts/build_exe.py
```

Distribution is in directory or archive format; using 'onefile' mode is not recommended.

### Testing

```bash
python tests/test_cli.py          # CLI contract (97 cases)
python tests/test_setup_ui.py     # Setup wizard, UI level (40 cases)
# 14 suites / 777 cases total: cli / ui / menu / setup / setup_ui / mcp / queue / vdb /
# oplog / i18n / encoding / concurrency / scene / evolve. mcp, concurrency, scene and
# evolve call real models (they skip explicitly without keys instead of faking green).
```

## Notes

- **exe and source code are two completely independent environments**; modifications to code require repackaging
- When writing memories, it is recommended to include **only one topic per entry** to maintain clear granularity
- Async writing only queues; **queuing ≠ storing**, the queue must be consumed by the worker
- Keys support ASCII characters only

## License

Please refer to the license file in the project root directory.