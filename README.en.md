# hy-mem

Local long-term memory service providing cross-session memory storage and semantic retrieval capabilities for AI Agents. Built upon Tencent Hy-Memory SDK 2.0.5, supporting Qwen3.7 cloud embedding (1024-dimensional vectors).

## Features

- **Semantic Retrieval**: Memory search based on vector similarity
- **Async Writing**: Non-blocking queuing mechanism, supports queue consumption
- **Tag Management**: Flexible tag system supporting archiving and classification
- **Interactive Configuration**: Visual setup wizard and health check
- **Multi-Identity Support**: Distinguish different Agent memory spaces via `user_id`

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
- **llm**: Large Language Model configuration
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
├── scripts/               # Utility Scripts
│   ├── build_exe.py      # Packaging script
│   ├── test_cli.py       # CLI Testing
│   └── ...
├── data/                  # Data Directory
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
python scripts/test_cli.py
python scripts/test_ui.py
```

## Notes

- **exe and source code are two completely independent environments**; modifications to code require repackaging
- When writing memories, it is recommended to include **only one topic per entry** to maintain clear granularity
- Async writing only queues; **queuing ≠ storing**, the queue must be consumed by the worker
- Keys support ASCII characters only

## License

Please refer to the license file in the project root directory.