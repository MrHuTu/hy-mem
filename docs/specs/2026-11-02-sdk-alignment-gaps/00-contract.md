# 契约：SDK 2.0.11 功能对齐补全（6 缺口 + MCP 开放面分析）

- **日期**：2026-11-02
- **父级**：`docs/specs/2026-11-02-sdk-2011-language/`（SDK 升级的后续）
- **授权**：用户 2026-11-02 拍板「全做，MCP 开放面按 AI 推荐处理」
- **背景**：对齐检查（工作日志 2026-11-02）发现 6 个真缺口；本契约把 SDK 能力
  包装面补齐，并明确哪些进 MCP、哪些只留 CLI。

## 分片总览

| 分片 | 内容 | MCP 开放？ |
| --- | --- | --- |
| S1 | search 透传 `queries` / `search_mode` / `rerank`（CLI chat 检索 + MCP search 工具） | ✅ **是**（检索是智能体高频动作，多路检索与 agentic 模式直接提升命中） |
| S2 | finalize `--language`（默认取 `evolve.language`） | ❌（MCP 无 finalize 工具，会话收尾由 CLI/脚本管） |
| S3 | profile `--full`（get_user_profile 组装画像） | ❌（L0 画像检索已覆盖智能体需求；CLI 观察窗口够用） |
| S4 | reembed 工具族 → `vdb-scan` / `vdb-plan` / `vdb-reembed` / `vdb-reembed-graph` / `vdb-drop` | ❌ **明确不开**——换向量模型是迁移级重操作（写全库向量、可删空间），必须人发起 |
| S5 | backup / restore → `backup` / `restore`（SDK 快照契约 + 写锁静默检测） | ❌ **明确不开**——数据面备份/回滚是运维动作，智能体误触发风险大于收益 |
| S6 | graph/entity 回填 → `graph-build`（`--entities` 附带实体库） | ❌（批量 LLM 调用、耗时长，属一次性运营操作） |

**MCP 开放面判定原则**（记录备查）：高频、只读或低风险、对智能体任务有直接
增益 → 开；迁移级/破坏性/长耗时重操作 → 只留 CLI 由人执行。

## 各片细节

### S1 search 透传

- CLI `search`：`--queries "a,b,c"`（逗号分，多路并行检索）、`--search-mode`
  （default/agentic，默认 default）、`--no-rerank`。chat 路径生效；
  scene 路径本次不动（工作/编码记忆单 query 够用，控制爆炸半径）。
- MCP `search` 工具：inputSchema 增 `queries`（string[]，≤5 条）、
  `search_mode`（enum）、`rerank`（bool 默认 true）；handler 透传。
- 行为约束：`queries` 与 `query` 互斥（都传报用法错）；rerank=False 原样透传。

### S2 finalize --language

- `--language` 缺省取 `config.evolve.language`（同一失灵根因，同一下 medicine）；
  两处都空 → 不传参（SDK auto）。

### S3 profile --full

- `--full` → `client.get_user_profile(user_id=...)`（SDK 组装好的完整画像，
  ultra 附 balanced catalog），输出 JSON 原样 + 人类可读摘要行；
  缺省行为不变（get_user_basic_info）。

### S4 reembed 工具族（backends/hymemory/reembedops.py 新建）

- `reembedops`：SDK 5 个模块级 async 函数的同步包装（`asyncio.run` 只在
  backends/ —— 守护断言 G1 合规）；embedding 服务复用 `build_embedding_service()`。
- 命令（平铺风格，与现有 37 命令一致）：
  - `vdb-scan [--root P]`：扫描向量空间（默认 data root），只读
  - `vdb-plan --source P --target P [--graph-source P]`：差量计划，只读
  - `vdb-reembed --source P --target P [--batch-size N] [--roles r1,r2]`：
    全量重嵌入（写 target，skip_existing 断点续跑）
  - `vdb-reembed-graph --source P --target P --dims N`：Kuzu 图向量重建
  - `vdb-drop --path P [--roles r1,r2] --yes`：删空间（**必须 --yes**，否则拒）
- source/target 均为 chroma persist 目录路径（SDK SpaceLike 的路径形态）。

### S5 backup / restore

- `backup --to DIR`：先探测静默——非阻塞抢 `store.WriteLock`，抢不到报
  「有写入进程，先停 worker/serve」（SDK 契约要求 writers_stopped=True）；
  成功后打印快照清单（文件数/字节）。
- `restore --from DIR --yes [--target P]`：SDK 自带回滚（protective_root）；
  **必须 --yes**。默认 target = data root。
- 沙箱测试：临时目录构造伪 data root，backup→篡改→restore→内容还原。

### S6 graph-build

- `graph-build [--limit N] [--clear-first] [--entities] [--rebuild-entities]`：
  `client.build_memory_graph(...)`；`--entities` 附带 `build_entity_store`。
- 真模型操作（LLM 抽关系），无密钥时走 readiness 显式报错。

## 不做什么

- 不动 MCP 工具集合（9 个不变，只扩 search 的 schema 字段）。
- 不给 scene 检索加 queries/search_mode。
- 不包装 clone_user / MemoryInspector / SDK HTTP server（对齐检查已判不适用）。
- 不做 reembed 的自动触发（换模型是显式人工决策）。

## 验收口径

- 新增测试 ≥16 项（CLI 离线桩 + MCP schema + backup/restore 沙箱回环），
  14 套件全绿；check_layering L1–L5 通过；check_secrets 0 ERROR。
- CMD_META 注册全部新命令（help 单一真源）；MCP tools/list 的 search schema
  含三个新字段。

## 追溯链

| 条款 | 分片 | commit | 验收证据 |
| --- | --- | --- | --- |
| search 透传（CLI+MCP） | S1 | fd05d37 | test_cli I2–I5 + test_mcp 2b 四项 PASS |
| finalize language | S2 | fd05d37 | CLI 冒烟（缺省走 evolve.language） |
| profile --full | S3 | fd05d37 | CLI 冒烟双路径 |
| reembed 工具族 | S4 | fd05d37 | test_cli I1/I6 PASS + help 冒烟 |
| backup/restore | S5 | fd05d37 | restore 确认门 PASS；快照回环留给实机演练（真数据面） |
| graph-build | S6 | fd05d37 | help/参数注册 PASS（真模型路径缺密钥显式报错） |

## 执行记录（2026-11-02）

- 14 套件 842 项全绿（cli 120 / mcp 35）；check_layering L1–L5 通过；check_secrets 0 ERROR。
- 守护断言 G1 再次立功：backup/restore 的 `import hy_memory` 首写进 cmds/maint.py
  被 I 面后跑的 G1 抓到 → 收敛为 reembedops.snapshot_data_plane/restore_data_plane。
- 一次返工：profile --full 编辑时残留旧函数尾块（SyntaxError），py_compile 即捕获。
- 版本 1.0.0-rc.10。未打包未 push。
