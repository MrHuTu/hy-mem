# L6 schema 检索恒空修复（阈值口径 + 状态可见性）

> 提出人：huchao　｜　执行：AI　｜　日期：2026-10-30
> 类型：bug 修复　｜　状态：执行中
> 落地目录：`docs/specs/2026-10-30-l6-schema-empty/`
> 前置：`docs/specs/2026-10-30-l6-ultra-consumption/`（L6 契约，C7 的阈值口径由本契约修订）

> **本文件是唯一事实源。** 实现、测试、验收都只认这里。
> `§1` 只由「澄清」修改，`§2` 只由「执行」修改；任何改动写进 `§3`。

---

## 0. 现象与根因（已实测钉死）

**现象**（用户 iisw-fullstack 会话，2026-10-30 17:11–17:19）：`evolve` 报 `schemas_added=4`
（0→4，get_all_nodes 数得到、写入轨迹 created=true），此后同一 MCP 进程内 6 次
`search_memory` 带 `schema=true` 全部 `schemas=[]` 且 `schema_available=true`，无任何报错。

**根因（三层，均已实测验证）**：

1. **主因：阈值与语言错配。** evolve 模型（deepseek-v4.1-flash）把中文事实归纳成
   **英文** schema（SDK 提示词决定，hy-mem 不改上游）。真实库 4 条 schema 的
   retrieval_text × 用户 4 个中文 query，用同一 embedding 端点（qwen3.7-text-embedding-flash,
   1024 维）实测余弦：最高 0.2572–0.3445，**全部低于 `schemaMinScore=0.4`** →
   静默过滤成空。同语言精准 query（"hy-mem 打包发版怎么验证"）可达 0.5855——
   检索通道本身完好（0.4 是按英文 query × 英文 schema 实测定的口径）。
2. **次因：图库未就绪时谎报可用。** 多会话并发时抢不到 kuzu 独占锁的 MCP 进程，
   `_graph_store._available=False`，`vector_search` 静默返回 `[]`，而
   `fetch_l6_schemas` 只判 `gs is None` → 照样回 `schema_available=true`（本次
   排障时在本机三个 hy-mem 进程并发现场直接复现）。
3. **可观测性缺口：** 「有 schema 但被阈值滤空」与「图库里真的没有」返回完全一致，
   调用方无从分辨，更无从自救。

## 1. 契约区

### 1.1 目标（一句话）

让 L6 schema 检索在「中文 query × 英文 schema」的真实场景下**能命中**，并在
「图库未就绪」「被阈值滤空」两种空结果下**说真话、给出路**。

### 1.2 条款

| 编号 | 约定 | 落地位置 | 判据（怎么算做到） |
|---|---|---|---|
| R1 | `evolve.schemaMinScore` 默认 **0.4 → 0.25**。依据：中文 query 对英文 schema 实测分 0.17–0.34（相关的在 0.26–0.34），0.4 恒空；0.25 恰在相关（≥0.26）与无关（≤0.20）的空档。同步更新：config DEFAULTS、cli/mcp 的 fallback 值、`config validate` 文案、README×2、SKILL.md 的实测口径段、test_setup 断言 | `core/config.py`、`cli.py`、`mcp/server.py`、`README.md`、`README.en.md`、`skill/hy-mem/SKILL.md`、`tests/test_setup.py` | 默认配置输出 0.25；`test_setup` R 节断言 0.25；README/SKILL 无 0.4 残留 |
| R2 | `fetch_l6_schemas` 在 `_graph_store._available == False` 时返回 state **`"graph_unavailable"`**（不再走 vector_search 静默空）；两个调用点（cli/mcp）映射为 `schema_available=false` + note「L6 图库未就绪（kuzu 独占锁被其它 hy-mem 进程占用或初始化失败）」 | `core/searchops.py`、`cli.py cmd_search`、`mcp/server.py _tool_search_memory` | 桩 client（`_available=False`）→ state=graph_unavailable；MCP 返回体含该 note 且 available=false |
| R3 | 命中为空但**库里确有 schema** 时返回 state **`"empty:<n>"`**（n=该 user 的 ACTIVE L6 条数，用 evolveops._schema_count 同口径）；`schema_available` 保持 `true`（通道是好的），note 说明「库里有 n 条、阈值 x 下无命中」+ 两条出路（传 `schema_min_score` 降阈值 / `config set evolve.schemaMinScore=0.25`） | 同上 | 桩 client（vector_search 返回 []、get_all_nodes 返回 2 条）→ state="empty:2"；MCP note 含 n 与阈值 |
| R4 | MCP `search_memory` 新增可选参数 **`schema_min_score`**（0–1，缺省走 config）：调用方可临时降阈值排障，不改配置。工具 inputSchema 同步声明。CLI 不加参数（人可以 `config set`，最小改动） | `mcp/server.py` | `tools/list` 的 search_memory properties 含 `schema_min_score`；传 0 时低分 schema 可返回 |
| R5 | 已装环境（rc.3 安装根 config.json）的 `schemaMinScore` 随升级更新为 0.25；版本号 bump **1.0.0-rc.4** | `core/version.py`、安装流程 | 升级后 `hy-mem config` 显示 0.25；真实库中文 query 能命中 schema |

### 1.3 不改的边界

- **不改 SDK**（schema 英文归纳是上游提示词行为，已在上级契约 1.4 排除「schema 中文重写」）
- **不改 normal hits 的 minScore 口径**（search.minScore 0.35 与本阈值无关）
- **CLI 不加 `--schema-min-score` 参数**（人侧用 `config set` 即可）
- 不做跨语言 query 改写、不做 schema 双语存储

---

## 2. 分片表（唯一进度表）

| # | 分片 | 改动文件 | 依赖 | 状态 | commit |
|---|---|---|---|---|---|
| S1 | 状态可见性（R2+R3） | `core/searchops.py`、`cli.py`、`mcp/server.py`、`tests/test_evolve.py` | — | ✓ | 见 §4 |
| S2 | 阈值口径（R1+R4+R5 版本号） | `core/config.py`、`cli.py`、`mcp/server.py`、`README*.md`、`skill/hy-mem/SKILL.md`、`core/version.py`、`tests/test_setup.py`、`tests/test_evolve.py` | S1 | ✓ | 见 §4 |
| S3 | 打包安装 + 真实库验证 | 安装产物、已装 config | S2 | ✓ | 见 §4 |

---

## 3. 变更记录（只追加，不改旧行）

| 日期 | 改了什么 | 为什么 | 影响分片 | 是否重走澄清 |
|---|---|---|---|---|
| 2026-10-30 | 初版 | 用户报「evolve 出了 schema 但 search 恒空」，实测钉死三层根因 | 全部 | — |

---

## 4. 追溯表（条款 → 分片 → commit → 产物 → 证据）

| 条款 | 分片 | commit | 产物 | 验收证据 |
|---|---|---|---|---|
| R2+R3 | S1 | （回填） | 1.0.0-rc.4（待打包） | （回填） |
| R1+R4 | S2 | （回填） | 同上 | （回填） |
| R5 | S3 | （回填） | 1.0.0-rc.4 已装 | （回填） |
