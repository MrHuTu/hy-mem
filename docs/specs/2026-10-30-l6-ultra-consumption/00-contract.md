# L6 认知层接入（ultra 专属）+ 模式感知（l6 契约）

> 提出人：huchao　｜　执行：AI　｜　日期：2026-10-30
> 类型：功能　｜　状态：执行中
> 落地目录：`docs/specs/2026-10-30-l6-ultra-consumption/`
> 前置决策：同目录上级 `2026-10-30-scene-working-coding/S5-ultra-eval.md`、`L6-retrieval-decision.md`

> **本文件是唯一事实源。** 实现、测试、验收都只认这里。
> `§1` 只由「澄清」修改，`§2` 只由「执行」修改；任何改动写进 `§3`。

---

## 1. 契约区

### 1.1 目标（一句话）

把 L6 认知层接成**可配置、可触发、可消费、且 pro 模式下完全不可见**的能力：能 `evolve`、能按语义检索、MCP 客户端清楚自己在什么模式。

### 1.2 核心定位（后续所有条款的前提）

| 事实 | 含义 |
|---|---|
| **L6 = ultra 专属** | `evolve` 仅 ultra 可调（SDK `client.py:2118` 硬拦 `RuntimeError`） |
| **pro 下 L6 通道恒关** | `reader_base.py:1674` `schema_search_on = has_graph and schema_limit != 0`；pro 下 `graph_store=None`（实测 `NoneType`） |
| **模式是安装级配置** | `config.json` 的 `mode`；现网安装 = `pro` |
| **MCP 客户端只能看到启动时的模式** | `tools/list` 在 initialize 时快照，切模式需重连客户端 |

### 1.3 条款

| 编号 | 约定 | 落地位置 | 判据（怎么算做到） |
|---|---|---|---|
| C1 | config 新增 `evolve` 段：`model`（空 = 沿用 `llm.model`）、`maxTokens`（默认 **8000**）、`schemaLimit`（默认 **3**）、`schemaMinScore`（默认 **0.4**）。**`to_env` 不得注入 `evolve.model`** —— 只在 `core/evolveops.py` 里于内存 config 对象上覆盖，否则全局 `MEMORY_LLM_MODEL` 被改成强模型，S1 抽取也一起变慢 | `core/config.py` | `to_env()` 输出里 `MEMORY_LLM_MODEL` 恒等于 `llm.model`；`config` / `config validate` 能显示 evolve 段 |
| C2 | `MEMORY_AGENT_MAX_TOKENS` 默认值由 SDK 的 **2000** 提升到 **8000**。根因：SDK 写的是 `agent_max_tokens or 4000`，默认填了 2000 使 `or` 永不生效，单轮输出被 2000 截断 → JSON 不闭合 → `Failed to parse operations JSON` | `core/config.py` `to_env` | evolve 单批 completion > 2000 时不报解析失败 |
| C3 | `config set mode=ultra\|pro\|lite` 可用且校验三值；`config validate` 对非法值报错 | `cli.py cmd_config` | `config set mode=bad` → exit 2 |
| C4 | `hy-mem evolve [--model X] [--batches N] [--json]`：非 ultra **前置拦截**并给中文错误（含切 ultra 的命令），不抛英文 `RuntimeError` | `cli.py cmd_evolve` | pro 下 → exit **1**（`EXIT_USAGE`），输出含 `mode=pro` 与 `hy-mem config set mode=ultra` |
| C5 | evolve 用**独立 client**：`MemoryConfig.from_env()` → 按 C1 覆盖 `llm.model` / `agent_max_tokens` → `HyMemoryClient(config=..., mode="ultra")`；**不得**修改 `config.json` 与环境变量 | `core/evolveops.py` | 跑完 `config.json` 的 `llm.model` 不变；默认 client 的 model 不变 |
| C6 | **空转检测**：`batches_completed > 0` 且 `total_completion_tokens < 150` → 标 `idle=true` 并给提示「模型未产出内容，建议换 evolve.model」，**不得**报成功 | `core/evolveops.py` | 用 xiaomi 跑 → `idle=true` 且提示换模型 |
| C7 | 检索默认**只走正路**：给 SDK 传 `schema_limit=0`（关掉 SDK 双路注入），hy-mem 自行调 `graph_store.vector_search(layers=["l6_schema"], score_threshold=C1.schemaMinScore, limit=C1.schemaLimit)` 取正路 | `core/searchops.py` | ultra 下 `results` 里无 schema；`schemas` 独立字段 0–3 条 |
| C8 | schema 结果**独立呈现**：不进 `results`、不占 `limit`、按**语义分**降序（不用 RRF 分），每条带 `score` 与 `layer="l6_schema"` | `cli.py cmd_search`、`core/searchops.py` | `--json` 输出顶层 `schemas` 键 |
| C9 | **反路（证据反查）首版不做**：不调 `reverse_lookup_l6`，不暴露 `--schema-evidence` 开关 | — | 代码中无该参数 |
| C10 | pro 下 `search` 的 `schemas` 恒为 `[]`，且**不额外调** embedding（省一次网络请求） | `core/searchops.py` | pro 下 search 延迟与现状一致 |
| C11 | MCP `tools/list` **按当前 mode 动态生成**：pro/lite 下不含 `evolve` 工具 | `mcp/server.py` | pro 下 `tools/list` 无 `evolve`；ultra 下有 |
| C12 | MCP `evolve` 工具存在时，`tools/call` 仍**实时读 mode** 校验（防列表过时），非 ultra 返回结构化错误 + 切换命令 | `mcp/server.py` | pro 手工调 `tools/call evolve` → 结构化错误非崩溃 |
| C13 | `get_version` 新增 `memoryMode`（lite/pro/ultra）与 `capabilities`（`evolve` / `l6Search` 布尔）；原 `"mode"`（exe/source）改名 `distMode`，保留 `mode` 作 deprecated 别名一版 | `mcp/server.py` | 输出同时含 `distMode`、`memoryMode`、`capabilities` |
| C14 | `search_memory` 新增 `schema` 参数（默认 `true`）：ultra 下带 L6 结果，pro 下传 `true` 不报错但 `schemas=[]`，返回体带 `schema_available=false` 说明原因 | `mcp/server.py` | pro 下返回 `schema_available=false` + `note` |
| C15 | 全部工具描述与 helptext 声明「evolve / L6 仅 ultra」 | `mcp/server.py`、`ui/helptext.py` | `hy-mem help search`、`help evolve` 含 ultra 说明 |
| C16 | **沉淀模型必须能从「设置功能」里换**：`setup` 新增 `evolve` 项（面板一行 + `setup --step evolve` 单步）；只能填与 LLM 同一端点下的模型名（端点/密钥共用 llm），选完实测一次连通性，失败给「重填 / 仍保存 / 放弃」 | `ui/setup_wizard.py`、`ui/menu.py`、`ui/i18n.py` | `tests/test_setup.py` R 节 9 例（含两遍配置与落盘）；控制台 `setup evolve` 路由通 |

### 1.4 不改的边界

- **不改 SDK**（`hy-memory==2.0.5` 已 pin）；私有 API `_graph_store` / `_embed_service` 可用，版本升级时需回归 `tests/test_evolve.py`
- **不改 chat 写入链路**（两段式 / batch-commit / 锁粒度）——L6 只动读取侧与 evolve
- **不动 `mode` 默认值**：仍 `pro`，切 ultra 是部署动作，不做自动切换
- **不做**：反路证据检索、`sweep()` 独立命令、schema 中文重写、跨语言 query 改写
- **不动** `proactive` / `intention` 通道

---

## 2. 分片表（唯一进度表）

| # | 分片 | 文档 | 改动文件 | 依赖 | 状态 | commit |
|---|---|---|---|---|---|---|
| S1 | 配置与模式入口 | `S1-config.md` | `core/config.py`、`cli.py`、`ui/helptext.py` | — | ✓ | `96b0738` |
| S2 | evolve 命令（含空转检测） | `S2-evolve.md` | `core/evolveops.py`(新)、`cli.py` | S1 | ✓ | `20055e2` |
| S3 | 检索隔离（只走正路） | `S3-search.md` | `core/searchops.py`、`cli.py` | S1 | ✓ | `0d8894e` |
| S4 | MCP 模式感知 | `S4-mcp.md` | `mcp/server.py` | S2、S3 | ✓ | `8d89c1e` |
| S5 | 测试转正 | `S5-test.md` | `tests/test_evolve.py`(新) | S1–S4 | ✓ | `3354f7a` |
| S6 | 设置功能入口（换沉淀模型） | `S6-setup.md` | `ui/setup_wizard.py`、`ui/menu.py`、`ui/i18n.py`、`ui/helptext.py`、`tests/test_setup.py` | S1、S2 | ✓ | `04e83a9` |

状态：`☐ 未开始` / `◐ 进行中` / `✓ 完成` / `✗ 废弃`

> S1–S5 执行期只维护本表 + 提交信息，未单独建片文档；条款级证据统一落在 §4 追溯表。
> S6 起恢复「一片一文档」（见 `S6-setup.md`）。

---

## 3. 变更记录（只追加，不改旧行）

| 日期 | 改了什么 | 为什么 | 影响分片 | 是否重走澄清 |
|---|---|---|---|---|
| 2026-10-30 | 初版 | 用户拍板「L6 仅 ultra 可用；MCP 动态过滤，pro 下不出现」 | 全部 | — |
| 2026-10-30 | 修正 C1：删除「to_env 注入 evolve.model」 | 执行前发现该做法会把 `MEMORY_LLM_MODEL` 全局改成强模型，S1 抽取（add）也一起变慢，违背 C5「不改环境变量」；改为只在 evolve 的 client config 对象上覆盖 | S1、S2 | 否（同向收紧，未改目标） |
| 2026-10-30 | 修正 C4 退出码 3 → 1 | `EXIT_INTERNAL(3)` 表示内部错误，而 pro 下调 evolve 是「当前配置下不支持」，与 `E_NO_TRACE` 同属用法类，应为 `EXIT_USAGE(1)` | S2 | 否 |
| 2026-10-30 | 补记 C16（新增 S6） | 用户最初就要求「这个设置必须放到 CLI 的设置功能中去，不然用户无法更换模型」，初版契约漏写成条款（S1 只做了 `config set evolve.model`）。S1–S5 完成后补齐 `setup evolve` | S6 | 否（补记既有要求） |

---

## 4. 追溯表（条款 → 分片 → commit → 产物 → 证据）

| 条款 | 分片 | commit | 产物 | 验收证据 |
|---|---|---|---|---|
| C1–C3 | S1 | `96b0738` | 1.0.0-rc.1（待打包） | `test_setup` 148 全绿（含 evolve 4 项校验）；`hy-mem config` 显示 evolve 段；`config set mode=bad` → exit 2 |
| C4–C6 | S2 | `20055e2` | 同上 | `test_evolve` B 面：pro 下 `evolve` → exit 1 且提示 `hy-mem config set mode=ultra`；D2 弱模型 → `idle=true`（completion 7 token、schema +0） |
| C7–C10 | S3 | `0d8894e` | 同上 | `test_evolve` D 面 `schemas` 独立字段 0–3 条、`results` 内无 l6_schema；13 条 query 实测 top1 均值 0.4705 vs 对照 0.2538（`L6-retrieval-decision.md`） |
| C11–C13 | S4 | `8d89c1e` | 同上 | `test_evolve` C 面：pro `tools/list` 无 `evolve`、`tools/call evolve` 结构化拒绝不崩；`get_version` 含 `memoryMode`/`capabilities`/`distMode` |
| C14–C15 | S4 | `8d89c1e` | 同上 | `search_memory` 收 `schema`/`schema_limit`，pro 返回 `schema_available=false` + note；`help evolve` / `help search` 含 ultra 说明 |
| 全部 | S5 | `3354f7a` | `tests/test_evolve.py` 35 项 78s | 14 套 **772** 项全绿（离线 632 + 真模型 140） |
| C16 | S6 | `04e83a9` | 同上 | `test_setup` R1–R9（首次指定 / 改回沿用 / 失败两分支 / `run_setup(scope=evolve)` 真落盘 / 不串 `llm.model`）；`test_setup_ui` G0 菜单含该行 |
