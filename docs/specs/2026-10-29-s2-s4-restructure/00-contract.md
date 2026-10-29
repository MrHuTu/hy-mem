# S2–S4 工程化重构契约 · 2026-10-29

> 承接 S1（已完成：cli/ 三层分层，commit 37b7ebe/c381da1/058c8e4）。串行执行，每片 12 套全绿 + 分层守卫过 = 可提交。

## §1 需求

### S2：正规包化（src 布局 + pyproject + 包名 hymem）
- `cli/` → `src/hymem/`；`hy_mem_cli.py` → `hymem/cli.py`；新增 `__main__.py`（`python -m hymem`）
- 全部 import `cli.*` → `hymem.*`
- pyproject.toml（setuptools；版本动态取 `hymem.core.version.VERSION`；console script `hy-mem = hymem.cli:main`）
- **app_root 统一**：新建 `core/paths.py` 唯一实现（frozen→exe 目录；源码→向上找 pyproject.toml/.git 标记；兜底 parents[3]），config/install_ops/runtime_check/skill_ops/local_embedder 5 处本地实现全部改为引用它（S1 暗雷的根治）
- `pip install -e .` 进 venv；源码启动器 `hy-mem.cmd` 切 `-m hymem`；`install_ops.target_command()` 源码分支切 `[py, -m, hymem]`
- 测试脚本：import 重写 + 子进程调用改 `[PY, -m, hymem]` + env 带 `PYTHONPATH=<repo>/src`（不依赖安装也可跑）
- build_exe：入口/hidden-imports/路径表包名化

### S3：拆 3508 行单体 + 消除 13 处反向依赖
- 入口 `hymem/cli.py` 按命令域拆 `hymem/commands/`（memories/queue/system/...），入口只留参数解析与分发
- 共享助手下沉：`make_client`/`build_embedding_service`/`readiness`/`require_ready`/`_spawn_detached_worker`/`_spawn_detached_serve`/`_flatten_search`/`_ids_of` 等 → core（clientops.py / procs.py / searchops.py）
- mcp/server 12 处 + vdb_endpoint 1 处 `from hymem.cli import` 全部改引 core → **清空 KNOWN_EXCEPTIONS**
- 行为零变化（纯搬家，函数体不改逻辑）

### S4：MCP HTTP transport
- `hymem/mcp/` 拆 `tools.py`（8 工具处理，协议无关）+ `transport/stdio.py`（现 JSON-RPC 循环）+ `transport/http.py`（新增）
- HTTP 形态：`hy-mem mcp --transport http --port 18088`，POST /mcp 收 JSON-RPC（单请求/批），响应 JSON；initialize 握手与 stdio 同语义；默认只绑 127.0.0.1
- stdio 默认行为完全不变（不传参数 = stdio）

### 验收（三阶段共用）
- A1：12 套测试全绿（每片都跑）
- A2：分层守卫 L1–L5 过；S3 完成后 KNOWN_EXCEPTIONS 清空
- A3：S2/S3 后 exe 打包冒烟三项（--version/mcp 握手/doctor）；S4 后 http 冒烟（curl POST initialize + tools/list + add/search 闭环）
- A4：用户零感知：config.json/数据目录/启动器语义/mcp.json 不变（`hy-mem` 命令照常）
- A5：`pip install -e .` 后任意 cwd `hy-mem --version` 可跑（S2 起）

### 不做
- 不上 PyPI 发布（pyproject 只为本地安装与未来桌面/服务复用）
- 不改任何用户可见文案/交互
- 不动 SDK 调用逻辑

## §2 执行记录（只由执行改）

| 分片 | 内容 | commit | 状态 |
| --- | --- | --- | --- |
| S2-1 | src 布局迁移 + import 重写 + __main__ + bootstrap | 本轮单 commit | 完成 |
| S2-2 | paths.py 统一 app_root（5 处收敛） | 本轮单 commit | 完成 |
| S2-3 | pyproject + editable install + 启动器/target_command 切 -m | 本轮单 commit | 完成 |
| S2-4 | build_exe 适配 + 打包冒烟 | 本轮单 commit | 完成 |
| S2-5 | 测试脚本对齐 + 全量回归 + 文档同步 | 本轮单 commit | 完成 |
| S3-1 | 共享助手下沉 core（clientops/procs/searchops） | | 待做 |
| S3-2 | mcp/vdb 反向依赖改引 core + 清空例外表 | | 待做 |
| S3-3 | 入口拆 commands/ + 分发瘦身 | | 待做 |
| S4-1 | mcp 拆 tools + transport/stdio | | 待做 |
| S4-2 | transport/http + 冒烟 | | 待做 |

## §3 追溯
| 条款 | 分片 | commit | 产物 | 验收证据 |
| --- | --- | --- | --- | --- |
| （执行时逐行回填） | | | | |
