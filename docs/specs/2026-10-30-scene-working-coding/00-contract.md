# Working/Coding 场景接入 + ultra 能力评估（scene 契约）

- slug：`2026-10-30-scene-working-coding`
- 状态：**§1 设计待用户拍板（未拍板不开工）**
- 参考：vendor/hy-memory（pin v1.0.5）`docs/scene_coding_usage.md`、`docs/usage.md`、`docs/hy_memory_search_skill.md`、`tests/coding/`
- 上游 SDK：pip `hy-memory==2.0.5`（**coding 管线默认开启：MEMORY_CODING_ENABLED=true，缺的只是入口**）

## §0 SDK 能力 vs hy-mem CLI 现状（全景对比）

### 三场景设计（SDK 官方语义）

| | chat | working | coding |
|---|---|---|---|
| 定位 | 闲聊/个人事实/一般问答 | 文档/表格/研究/财经/电脑操作等生产力 | 编码/调试/测试/SQL/API 等软件工程 |
| 输入形态 | 纯文本或 messages | **messages 必须含 tool_calls / role=tool 轨迹** | 同左 |
| 记忆形态 | L0–L7 分层 | 双层：WorkingFact（每次恰 1 条）+ Experience（0–N 条，价值门槛） | 同 working + **版本演进链（diff 链）** |
| 独立存储 | 主 vector_db | coding_memory.db + coding_memory_trace.db + 专用向量集合 | coding_memory_cv.db + hy_code_chain/hy_code.db + _cv 集合（与 working 完全隔离） |
| 检索返回 | normal/schema/intention 三路 | facts + experiences 分路 | 同左 + chain_block（opt-in，默认关） |
| 模式要求 | lite/pro/ultra | **仅 pro/ultra**（lite 忽略 scene 一律 chat） | 同左 |

### 场景路由（SDK 行为，接入时必须遵守）

| 输入 | 行为 |
|---|---|
| `scene="chat"`（或纯文本无 scene） | 跳过 judge；tool 轨迹移除后进 chat 管线，**零 LLM 路由开销** |
| `scene="working"/"coding"` 显式 + 有 tool 轨迹 | 跳过 judge 直达对应管线 |
| **显式 working/coding 但无 tool 轨迹** | **SDK 回退 chat 管线**（CLI 必须检测并警告，否则用户以为写了 coding 库实际进了 chat） |
| `scene=None` + 含 tool 轨迹 | 一次 LLM 三分类（chat/working/coding）；失败回退 chat |
| 非法 scene | ValueError |

### 能力矩阵（✅已接 / ⚠️部分 / ❌未接）

| SDK 能力 | 依赖 | hy-mem CLI 现状 | 本 spec 覆盖 |
|---|---|---|---|
| chat 写入/检索/L0-L7 | pro | ✅ 全链路（含两段式+合批优化） | 不动 |
| **working 写入（事实+经验双层）** | pro | ❌ 无入口 | **S1/S2** |
| **coding 写入（+diff 链）** | pro + MEMORY_CODING_CHAIN_REPO_ROOT | ❌ 无入口 | **S1/S2/S4** |
| workspace_id/branch 作用域 | — | ❌ 无参数 | S1/S2 |
| scene 自动路由（三分类） | pro + 1 次 LLM | ❌（add 只收纯文本，恒走 chat） | S1 可选档 |
| working/coding 检索分路 | — | ❌ | S2 |
| `scene="all"` 跨场景 search/list | — | ❌ | S2 |
| include/exclude_sources 过滤 | — | ⚠️ add 有 source 字段，search 未暴露过滤 | S2 顺带 |
| search `queries` 多路 + `search_mode="agentic"`（judge/rewrite/时间解析） | LLM | ❌ | 不做（宿主 agent 自己就是 agentic，重复） |
| reader="hybrid"（BM25） | 后端支持 | ❌ | **不做**——官方文档明说 Chroma 上退化为纯向量，我们就是 Chroma |
| schema_limit/intention_limit（L6/L7 检索配额） | — | ❌ | 不做（chat 检索现况够用） |
| **ultra：evolve()（System 2 图式归纳 L6）** | ultra + Kuzu Graph | ❌ | S5 评估档 |
| **ultra：sweep()（跨域 sweeper）** | ultra | ❌ | S5 评估档 |
| build_memory_graph/entity_store 回填 | graph | ❌ | 不做 |
| get_user_basic_info（L0 读取） | — | ⚠️ 写入有（add 提取 basic_info），读取命令无 | S6 小项 |

### 关键技术约束（决定实现方式）

1. **coding/working 是完全独立管线**：与 chat 的 `writer._reconcile_and_store`（我们 two_phase/batch_commit 调的东西）零共享——scene 写入只能走 `client.add(messages, scene=...)` 整段，**不能**复用两段式/合批优化（那是 chat 专属）
2. **数据域隔离**：coding 写独立 SQLite + 独立向量集合，不碰 chat 的 vector_db——与 chat WriteLock 理论无冲突，但首版保守起见 coding sync 写仍持锁（慢一点，安全第一；后续实测无竞争再出锁）
3. **lite 不生效**：scene 写入需 pro（我们默认 pro ✓）
4. **trace 历史**：SDK 自带 `coding_memory_trace.db` 存历史消息（LAST 5 USER TURNS 指代消解用），多次 add 同一 user 自动衔接

## §1 设计（待拍板）

### 分片总览

| 分片 | 内容 | 规模 |
|---|---|---|
| **S0** | episode 调研：读 MEMORY_SESSION_EPISODE_* 默认值 + 一次召回对比实验，产出「接/不接」结论 | 小（半小时） |
| **S1** | CLI 写入入口：`add --scene coding\|working` + messages 轨迹文件 | 中 |
| **S2** | CLI 检索入口：`search/list --scene` 分路 + workspace 过滤 + sources 过滤 + `outcome` 命令 | 中 |
| **S3** | MCP 工具增强：add_memory/search_memory 加 scene 三件套 | 小 |
| **S4** | diff 链：MEMORY_CODING_CHAIN_REPO_ROOT 配置 + chain_block 检索开关 | 小 |
| **S5** | ultra 评估档：evolve/sweep + Kuzu（**只评估不实现**，产出决策记录） | 小 |
| **S6** | L0 读取：`hy-mem profile`（get_user_basic_info 排版） | 小 |

### S0 episode 调研（已完成，2026-10-30 11:30）

**结论：缓做（不接消费侧开关，维持 SDK 默认全关）**。

- 默认值核对：episode 消费侧全默认关（RECALL_ENABLED/SUMMARY_ENABLED/SESSION_EPISODE_CONTEXT_ENABLED 均 false）；**写入侧 episode_id 切分默认在打**（_resolve_episode_id 按 30min gap，现库 27/151 节点带 episode_id，数据基础已在）
- A/B 实验（probe_episode_ab.py，真库 5 查询开/关 RECALL 加权）：仅 1/5 查询有差异且只是尾部微调（top1 分差 <0.005、排序位移 0）——库小 episode 多为单节点，加权无势可借
- 增益预判：等库到千条级、多轮同主题写入后再开才有意义；届时 config 加一行 env 即可，无技术债
- 显式不设 env（维持 SDK 默认关），不写入 hy-mem config

### S1 CLI 写入设计

```
hy-mem add --scene coding \
  --workspace "github.com/MrHuTu/hy-mem" \    # 可选，仓库/项目隔离
  --branch "master" \                          # 可选，需同时有 --workspace
  --trace trace.json                           # messages JSON 文件（或 stdin）
```

- `--trace` 文件格式：OpenAI messages 数组（含 tool_calls/role=tool）；`-` 表 stdin
- **无 tool 轨迹即拒**：CLI 预检（比 SDK 静默回退 chat 更诚实——SDK 的回退是陷阱，我们要在门口拦住并明确报错 `E_NO_TRACE`）
- `--scene` 缺省 = 现有 chat 行为，零变化
- 可选档（待拍板）：`--scene auto` 走 SDK 三分类（+1 次 LLM）？——**默认不做**，显式优于自动
- `--async`：payload 带 scene/messages/workspace/branch 入队；worker 分流——scene=chat 走现有两段式+合批；**working/coding 走 client.add 整段且不合批**（不同管线，§0 约束 1）
- helptext 同步：coding 记「文件坐标+决策，不贴代码」的官方抽取规范提示

### S2 CLI 检索设计

```
hy-mem search "超时怎么修的" --scene coding --workspace <ws> [--branch <b>] [-n 5]
hy-mem list --scene all          # 跨场景列出（chat 记忆 + experiences 分桶）
hy-mem search "..." --source mcp --exclude-source cli    # sources 过滤
```

- `--scene coding/working` 输出排版：**Facts** / **Experiences** 两段（官方返回结构 facts+experiences 分路，limit 各自独立）
- `--scene all`：SDK list_memories 返回 chat 桶 + productivity 桶，排版分组
- `--chain-block` 开关（S4 联动）：facts 下挂 diff 演进版本
- **`hy-mem outcome <request_id>`**：SDK `get_write_outcome(request_id)` 排版——`--async` 写入的结果级查询（比 queue_status 的 job 状态更进一步：直接看 SDK 写成了什么，含 scene 写入的结果），补齐异步闭环

### S3 MCP 增强

- `add_memory` 新增可选参数：`messages`（数组，与 text 二选一）、`scene`、`workspace_id`、`branch`——宿主智能体（WorkBuddy/CodeBuddy）可直接投喂工具轨迹
- `search_memory` 新增：`scene`、`workspace_id`、`branch`、`include_sources`/`exclude_sources`
- 工具描述补场景指引（沿 rc.2 的描述风格：什么任务走什么 scene）
- **MCP add 默认仍 chat**；scene 显式传才走生产力管线

### S4 diff 链

- `hy-mem config set coding.chain_repo <路径>` → 写 env/配置 `MEMORY_CODING_CHAIN_REPO_ROOT`
- `MEMORY_CODING_CHAIN_BLOCK_ENABLED=true` 由 `search --chain-block` 按次注入（还是全局开？**待拍板**，倾向按次——官方默认关是省 token）
- 验收：同一文件两轮修改 → 检索 fact 挂两版 diff

### S5 ultra 评估（只评估不实现）

- 产出：一页决策记录——evolve() 成本（读全量 VDB + LLM 归纳，适合夜间批跑）、Kuzu 引入成本、L6 Schema 对我们 chat 记忆的实际增益
- 倾向：**缓做**。L6 schema 对个人记忆库增益未验证，Kuzu 是新依赖；等 working/coding 数据积累后再评估

### S6 profile 命令

- `hy-mem profile` → get_user_basic_info() 排版（姓名/职业等 L0 画像）
- 30 分钟小活，顺手

### 验收口径（全分片）

0. S0：episode 调研结论落 spec（接/不接 + 理由 + 若接的改动面）
1. S1 E2E：真实 coding trace（含 apply_patch 轨迹）写入 → 事实层恰 1 条 + 经验层 ≥0 条 + trace.db 有历史
2. S2 E2E：`search --scene coding` 返回 facts/experiences 两路且语义命中；`--scene all` 分桶正确；`outcome` 能按 request_id 查到 async 写入结果
3. 无轨迹拦截：`add --scene coding` 纯文本 → E_NO_TRACE 明确报错（不静默进 chat）
4. 回归：chat 全链路（12 套 tests/）零回归；scene 缺省行为与现状逐字节等价
5. MCP：tools/list 新参数 schema 正确；真实轨迹经 MCP 写入 coding 库成功
6. SDK 耦合登记：client.add(scene/messages/workspace_id/branch)、search(scene/workspace_id/branch/include_sources)、get_write_outcome、coding 返回结构（facts/experiences/chain_block/chain_used）——SDK 升级须回归

### 边界（不做，含查漏登记）

**明确不做**：
- reader="hybrid"（Chroma 无效）、queries 多路 + agentic search_mode（宿主 agent 已是 agentic，SDK 内再 judge 是双重路由浪费）
- scene 自动路由默认开（显式 > 自动；要 auto 未来加 `--scene auto`）
- ultra 实现（S5 只评估）
- coding 链路的两段式/合批优化（独立管线没有我们的锁问题，SDK 内部自管）

**已知不接**（2026-10-30 查漏对照 SDK 公开 API + env_reference 全集后显式登记，避免后人再查）：
- `get_user_profile()`（balanced catalog/画像补充体系）——依赖 ultra 档，S6 用 basic_info 已够；未来开 ultra 时一并评估
- `clone_user()`——单用户场景无需求
- `add_extracted()`——R4 时已拍板弃用（两段式自己实现了等价物）
- MySQL 缓存族（MEMORY_MYSQL_*）、Qdrant/FAISS/Neo4j 后端——锁 Chroma + 本地 SQLite
- `MEMORY_MEMORY_OPERATIONS_ENABLED`（SDK 内部操作记忆，实验性）、`MEMORY_LOG_LLM_CONTENT`（隐私，默认关是对的）、topic_summary/summary_in_sys（L3 注入相关，现况够用）
- evolve/sweep/build_memory_graph/build_entity_store 实现（S5 评估档覆盖）

## §2 执行记录

### S1 写入入口（2026-10-30，commit `a85ea13`）

- 实现：`_cmd_add_scene()`——`--scene working|coding` + `--trace <messages.json|->` + `--workspace` + `--branch`；`client.add(messages, scene=...)` 整段，**不走两段式/合批**（独立管线）
- 门口预检：无 `tool_calls`/`role=tool` → `E_NO_TRACE` 零写入（SDK 静默回退 chat 是陷阱，不往外放）
- 验收：`repro_scene_add` 7/7（E_NO_TRACE 拦截、fact 恰 1 条、`coding_memory_cv.db` + facts 表生成、chat 缺省同构）

### S2 检索 + outcome（2026-10-30，commit `5b8c6e3`）

- 实现：`_cmd_search_scene()`——facts/experiences 双路返回、`--workspace/--branch` scope、`--source-filter`、`--chain-block`（按次注入 `MEMORY_CODING_CHAIN_BLOCK_ENABLED`，用后即焚）
- 实现：`hy-mem outcome <request_id>` → `get_write_outcome()` 排版
- 验收：`repro_scene_add` 8/8（facts 命中刚写入的 `fact_id` 闭环）；回归 `test_cli` 97/97、`test_mcp` 31/31

### S3 MCP 增强（2026-10-30）

- 新增 `_PRODUCTIVITY_SCENES` / `_parse_scene_scope()` / `_str_list()` / `_add_productivity()` / `_search_scene()`
- `add_memory`：`messages` / `scene` / `workspace_id` / `branch`；`text` 与 `messages` 二选一；无轨迹直接 `ParamError`（与 CLI 同一拦截口径，不让 SDK 静默回退 chat）；**scene 写入恒同步**（队列 payload 是 text 单条设计，worker 无 scene 分流），返回带 `note` 说明；`required: ["text"]` 移除（改由实现校验二选一）
- `search_memory`：`scene` / `workspace_id` / `branch` / `include_sources` / `exclude_sources`；scene 分路返回 facts+experiences 两段（零命中给排障 hint：chat/生产力库互不可见、scope 要对齐）；include/exclude 互斥
- 边界守住：chat 路径传 `messages` 报错、chat 路径传 `workspace_id/branch` 报错（chat 记忆没有仓库作用域）
- 描述沿 rc.2 口径扩写：三场景分工（省略=chat / working=生产力 / coding=编码），并点明「库隔离 → 要跨库得搜两次」
- 验收 `repro_scene_mcp` **15/15**（M1 schema 齐全、M2-M5 四类参数拦截、M6 真轨迹写入 fact 恰 1 条、M7 facts 命中刚写入 fact_id、M8 scope 不匹配零命中带 hint、M9 chat 缺省仍异步入队）；回归 `test_cli` 97/97、`test_mcp` 31/31
- 测试环境坑（登记备查）：进程内直调 `hymem.mcp.server` 必须先跑 `cli.load_env()`，否则 `config.json` 没映射成 `MEMORY_*` 环境变量 → SDK 退回默认 openai 端点、embedding 连不上、facts 落不了库（第一轮 12/15 的失败全源于此，非产品缺陷）

### 待办

- S4 diff 链 / S5 ultra 评估 / S6 profile 命令
- 三个 scene repro（S1/S2/S3）待 S6 收尾后统一转正进 `tests/`，计入套件总数
