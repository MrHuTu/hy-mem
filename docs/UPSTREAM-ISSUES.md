# 上游 Bug 追踪（hy-memory / Tencent-Hunyuan）

> 用途：记录上游 SDK 的已知 bug、我们下游的暂时对策、修复关联文档。上游修复后同步更新本表并退役对应下游补丁。
> 上游仓库：<https://github.com/Tencent-Hunyuan/Hy-Memory>（vendor 快照：`vendor/hy-memory/`，git clone）
> 同步纪律：**上游发新版后，先跑本表「验证方法」列，再决定是否退役下游补丁；退役后必须在「同步记录」留痕。**

## 总览

| # | 上游 Bug | 影响我们的能力 | 下游对策 | 对策落点 | 上游状态 | 我方状态 |
|---|---|---|---|---|---|---|
| U1 | GraphStore 锁冲突降级被 client 层否决 → ultra 多进程第二个 client 构造必炸 | 异步 add（worker 死信/影子写入）、CLI add/stats、多进程并发 | 方案 A：vendor 补丁接通降级（`_initialize_impl` 的 raise → warning） | `scripts/apply_upstream_patches.py`（幂等重放）+ `docs/specs/2026-11-02-ultra-graph-degrade/` | **未修**（main@3511153 仍在：client.py L613-617 raise / L528-529 ultra 硬编码开图） | **补丁已实施并验收**（2026-11-02，commit fa78cb5；red→green + 全量 14 套全绿 + 实战死信复活） |
| U2 | S2 归纳 auto 语言检测 `zh_ratio≥0.7` 对技术型中文误判为 en → 画像/模式归纳成英文 | evolve / L6 schema 语言 | 显式配置 `evolve.language=zh`（SDK 2.0.11 起原生支持，显式时换归纳提示词本体 + 11 语言 OUTPUT_LANGUAGE_DIRECTIVE 硬约束），**不用 auto** | `config.json` evolve.language=zh；运行时补丁已在 rc.9 退役 | **部分改善未根治**：2.0.12 补齐 11 语言提示词（PR #261/262）+ memhub 显式传 zh-Hans（PR #270），但 auto 检测阈值逐字未变（system2_agent.py L2336-2337，2026-11-02 实测） | 显式 zh 常态化；上游若修 auto 再评估回归 |
| U3 | client 构造期冻结 `MEMORY_VECTOR_HOST/PORT`，serve 重启换端口后长驻 client 全部 ConnectError 10061 | MCP server 长驻 client 的 add sync（search 因动态发现幸存） | 暂无代码级对策；运营层保证 serve 常驻 + 出问题重启 MCP 会话 | —（见 `docs/specs/2026-11-02-ultra-graph-degrade/issue-draft.md` 附带发现段） | **未修** | 待定对策（候选：client 连接失败自动重建） |
| U4 | scene 能力声明与实际生效条件不一致：`get_version.capabilities.scene` 只看 memoryMode，SDK 还需 `mode != lite` 且 `coding.enable` | MCP `add_memory` / `search_memory` 的 `scene=working/coding` | 能力声明对齐 SDK 真实条件；add/search 入口前同一检查，不可用即明确失败（零写入） | `src/hymem/mcp/server.py`（capabilities / `_add_productivity` / `_search_scene`）、`src/hymem/core/config.py`（`scene_status`） | SDK 行为本身合理（降级日志在 info 级），属我方声明口径问题 | 已修（2026-11-03，P2 后批次） |
| U5 | chat 检索未钉死 scene：`searchcore` 不传 scene，`queries` / `search_mode=agentic` 时 SDK 三分类可能路由到生产力库，返回 facts/experiences 被 chat 拍平丢弃 | CLI/MCP chat 检索（表现为"库里有、返回 0"） | 检测到 productivity 响应后以 `scene="chat"` 用同一目标 query 重取一次；判 chat 时沿用首次结果（保留 rewrite/agentic） | `src/hymem/backends/hymemory/searchops.py`（`searchcore`） | SDK 设计（自动三分类），下游按产品约定只认 chat 库 | 已修（2026-11-03，P2 后批次） |
| U6 | smart 侧场景可辨识性不足：`scene` 无 JSON Schema `default`、working/coding 边界未写明；`add_memory` 只回请求值不回实际落库场景；`search_memory` 只回笼统 `productivity`，无法区分 working/coding | MCP 调用方（智能体/客户端）按 schema 与返回值决定写哪库、怎么二次检索 | schema 补 `default: chat` + 判定规则与跨库提示；add 增加 `requested_scene`/`stored_scene`/`scene_degraded`，search 增加 `scene_requested`/`scene_resolved`（均可选，旧字段不动） | `src/hymem/mcp/server.py`（TOOLS 的 inputSchema + `_add_productivity` / `_search_scene` 返回体） | 返回体是"请求值"而非"落库值"时，智能体无法发现写错库 | 已修（2026-11-03，P2 后批次） |
| U7 | `shadow_only` 把两种成因混成同一标记与同一句 error：「抽取真失败（LLM/embedding 异常、解析出错）」与「内容与既有记忆重复、抽取正常完成但产出 0 条可检索事实」 | 运维判读（`shadow_only` 告警、是否值得 `retry --shadow` 重投） | 不自动重试重复内容；登记观察（重投前先人工确认是否与既有记忆重复） | 无代码改动（观察项） | **未修**（SDK 2.0.13 实测：重跑正常完成仍同一句 error） | 观察（2026-11-03 登记） |

## U1：GraphStore 降级被否决（详细）

- **上游现象**：同数据目录第二个 ultra client 构造 `RuntimeError: GraphStore (kuzu) init failed`（kuzu_db 文件独占锁，Windows Error 5）。
- **根因**：`graph_store_kuzu.py initialize()` 捕获锁冲突已降级 no-op（`_available=False`，全部运行期方法有 `return None` 守卫，日志自称 *"expected in multi-worker environments"*），但 `client.py _initialize_impl()` 检查 `_available` 后主动 raise——降级被否决。
- **下游对策（方案 A，2026-11-02 拍板）**：vendor 补丁把 raise 改为 warning，接通 SDK 自己设计的降级。补丁以文本替换方式幂等应用（`scripts/apply_upstream_patches.py`，pip 重装 venv 后由 build_exe.py 自动重放）。
- **影响语义**：锁冲突进程的图能力 no-op（L4 关系边不写），事实/向量/检索完全正常；关系边缝隙用 `graph-build` / `vdb-reembed-graph` 回填。
- **关联文档**：`docs/specs/2026-11-02-ultra-graph-degrade/00-contract.md`（契约与追溯链）、`issue-draft.md`（上游 issue 稿）。
- **验证方法（上游新版到达时）**：
  1. `pip install hy-memory==<新版>`；
  2. 跑 `.local-test/repro_graph_lock.py`（进程 A 持锁 + 进程 B 构造）；
  3. B 构造成功且日志为 warning 降级（而非 RuntimeError）→ 上游已修 → **退役补丁**：从 `scripts/apply_upstream_patches.py` 移除对应 patch 条目、重装 SDK、复跑复现脚本确认；
  4. 更新本行状态与同步记录。
- **同步记录**：
  - 2026-11-02：发现（rc.10 打包验收实测死信/影子写入现场）；issue 稿完成待提交。
  - 2026-11-02：方案 A 补丁实施并验收（commit fa78cb5）：red（双进程 B 构造 RuntimeError）→ green（B 构造成功 + warning 降级）；test_cli 122/122（含 G2×2 补丁守护）；全量 14 套全绿；重打包覆盖安装后实战验证——先前死信 job 98/99 经 worker --once 全部落库可检索，队列清零。

## U2：S2 归纳语言 auto 误判（详细）

- **历史**：rc.5 时代 service2 归纳用户画像把中文归纳成英文；当时我们内置提示词/运行时补丁修过。
- **上游 2.0.11**：原生支持显式语言（`client.evolve(language=)` / `MemoryConfig.language` / env `MEMORY_LANGUAGE`），显式时换归纳提示词本体 + 11 语言 OUTPUT_LANGUAGE_DIRECTIVE 硬约束。我们 rc.9 退役运行时补丁改原生透传。
- **残留问题**：auto 检测（`zh_ratio >= 0.7 → zh else en`）对技术型中文（大量英文术语）误判为 en。2.0.12（= main@3511153 快照，2026-11-02 核对）该阈值逐字未变。
- **下游对策**：`config.json` 恒显式 `evolve.language=zh`；**不回归 auto**。
- **关联文档**：`docs/specs/2026-11-02-sdk-2011-language/`（rc.9 契约）。
- **验证方法（上游新版到达时）**：grep 新版 `system2_agent.py` 的 `_detect_language`/`zh_ratio`；若改为可配置阈值或更稳的语言判定 → 沙箱跑一次 auto evolve 用技术型中文样本验证 → 通过则评估是否回归 auto（仍建议显式，语义最稳）。
- **同步记录**：
  - 2026-10-30：rc.9 显式 zh 落地（退役 rc.5 运行时补丁）。
  - 2026-11-02：核对 2.0.12，auto 检测未修；用户提示"2.0.12 可能修复 service2 多语言"→ 实证为提示词补全（PR #261/262），auto 误判仍在。

## U3：向量端点构造期冻结（详细）

- **现象**：SDK `config.py` `__post_init__` 在 client 构造时读 `MEMORY_VECTOR_HOST/PORT` 冻结；serve 空闲自退后重拉换端口，长驻 client（MCP server）的 HttpClient 连旧死端口 → ConnectError 10061。search 因某层动态发现幸存，add sync 稳定失败。
- **下游对策**：暂无代码级修复；运营层：保持 serve 常驻（`serve --idle-exit 1800` 被请求续命），出现 ConnectError 时重启 MCP 会话（WorkBuddy 重连）。
- **候选方案**（待排期）：hy-mem 侧 client 连接失败自动重建（捕获 ConnectError → 重跑 `ensure_endpoint` → 换新 client；ultra 下需先 close 旧 client 释放进程内资源）。
- **关联文档**：`docs/specs/2026-11-02-ultra-graph-degrade/issue-draft.md` 附带发现段。
- **同步记录**：
  - 2026-11-02：发现于 MCP 工具实测（serve 重启窗口）。

- **下游对策（2026-11-02 落地，commit d575778，rc.11）**：
  - S1 自愈：MCP `_client()` 取缓存前 `_endpoint_stale()` 复验（env 端口心跳死 / 登记新活端口 → 放掉重建，重跑 ensure_endpoint）；CLI 短进程天然免疫。
  - S2 加固：config 新增 `servePort`（0=随机默认），serve 绑定固定端口（被占回退随机+告警）——端口不变则冻结端点恒有效。
  - 定性修正：主因是 hy-mem 侧「随机端口 + 单例不失效」两处设计；SDK 构造期读 env 属设计限制（enhancement），提 issue 时按低优先级建议附带。
  - 验收：test_vdb_endpoint 44/44（U3 四态 + S2 两态），全量 14 套全绿。

## U4：scene 能力声明与实际生效条件不一致（详细）

- **现象**：`get_version` 只按 `memoryMode` 判定 `capabilities.scene`（`src/hymem/mcp/server.py:1019-1023`：`mem_mode in ("pro", "ultra")`），但 SDK 真正启用 scene 写入还需 `mode != lite`、`content_type == "messages"` 且 `coding.enable`（`vendor/hy-memory/hy_memory/client.py:2967-2972`）。
- **触发条件**：`MEMORY_MODE=lite`，或 `MEMORY_CODING_ENABLED=false`（SDK `CodingConfig.enable` 的唯一来源，默认 `True`，见 `vendor/hy-memory/hy_memory/config.py:1167`），且调用 `add_memory` / `search_memory` 传 `scene=working|coding`。
- **后果**：SDK 以 info 级日志静默降级到 chat 管线；MCP 仍返回 `ok=true, mode=working`，调用方以为写进了生产力库，实际落在 chat（两库互不可见 → 数据"丢失"）。
- **修法**：`core/config.py` 新增 `scene_status()`，与 SDK 条件同口径（`mode != lite` 且 `MEMORY_CODING_ENABLED` 开启）；`get_version.capabilities.scene` 改用它；`_add_productivity` / `_search_scene` 在进入 SDK 前做同一检查，不可用即返回 `ok=false, error=scene_unavailable`（零写入，`mode` 保持请求值，附 `guidance` 说明原因与替代路径）；CLI `add/search --scene` 同闸门。
- **验证方式**：lite 与 `MEMORY_CODING_ENABLED=false` 两种配置下断言 add 不谎报场景（零写入）、search 返回可解释失败；pro + 默认 coding 开启时行为不变（`tests/test_cli.py` L 段）。
- **同步记录**：
  - 2026-11-03：登记并修复（红→绿用例 + 受影响套件；全量回归见同批次报告）。

## U5：chat 检索未钉死 scene（详细）

- **现象**：`searchcore` 调 `client.search` 未传 scene（`src/hymem/backends/hymemory/searchops.py:640`）。调用方传 `queries` 或 `search_mode=agentic` 时，SDK 会做 LLM 三分类（`vendor/hy-memory/hy_memory/client.py:4125-4170`）；一旦判为 working/coding，返回结构变为 `{"scene": "productivity", "facts": [...], "experiences": [...]}`（`client.py:1702-1708`），而 MCP/CLI 拍平只认 `memories` → 表现为"库里有、返回 0"。
- **触发条件**：chat 检索传 `queries`（多查询）或 `search_mode=agentic`，且分类器判为生产力场景。
- **后果**：chat 库明明有内容却返回空结果；`probe_empty` 还会误报 embedding 异常。
- **修法**：`searchcore` 检测到 productivity 响应后，用同一目标 query（`query` 或 `queries[-1]`）以 `scene="chat"` 重取一次，保证结果只来自 chat 库。首次分类判 chat 时沿用原结果，因此 `queries` 的指代改写与 agentic 能力在正常路径完整保留；仅在"本来就要被误路由"的场景下丢失该次 rewrite（原响应不含改写文本，无法复用）。
- **验证方式**：fake client 两态（判 chat → 不重试且 `queries` 透传；判 productivity → 重试 `scene="chat"` 并返回 chat 命中）；CLI 与 MCP 两入口各跑一遍（`tests/test_cli.py` L 段）。
- **同步记录**：
  - 2026-11-03：登记并修复（红→绿用例 + 受影响套件；全量回归见同批次报告）。

## U6：smart 侧场景可辨识性不足（详细）

- **现象**：三点合在一起让调用方无法可靠决定/校验"记忆进了哪个库"：
  1. `scene` 参数 schema 没有 `default`，working/coding 的边界只在工具描述里一句带过；智能体容易在纯文本场景误传 `working`，写入另一个库后对应检索必然 0 命中。
  2. `add_memory` 的成功回执只回请求值 `mode`，不回 SDK 的真实落库场景（SDK 内部可能改写/降级）。
  3. `search_memory` 的 `scene` 字段取自 SDK 回执，生产力检索恒为笼统的 `"productivity"`，无法区分 working 与 coding。
- **触发条件**：任何按 schema 决定写库、按返回值做二次检索的 MCP 调用方（智能体、客户端集成）。
- **修法**：
  - `add_memory.scene`：`enum: [chat, working, coding]` + `default: chat`，描述写清判定规则（diff/patch/源码改动 → coding；其它带工具轨迹的生产力操作 → working；纯文本 → 省略）、"错 scene 不报错只会写进另一个库、对应检索 0 命中"、"三库互不可见、跨库要分别调用"、"`capabilities.scene=false` 时不要传 scene"。
  - `search_memory.scene`：`enum: [chat, working, coding]` + `default: chat`，同样写清三库互不可见与错 scene 的后果。
  - `add_memory` 新增可选字段 `requested_scene` / `stored_scene`（取 SDK 回执：`variant` 优先，其次 chat/working/coding，缺失退回请求值）与 `scene_degraded`；降级时附 `warning`，并在 `note` 中说明"`mode` 是请求值，以 `stored_scene` 为准"。
  - `search_memory` 新增可选字段 `scene_requested` / `scene_resolved`（working|coding）；原有 `scene` 字段语义与类型保持不变。
  - 所有新增字段均可选；`ok` / `mode` / `scene` 等既有字段不删、不改类型。
- **验证方式**：L 段用例覆盖 schema（default/enum/描述要点）、正常写入回执、模拟 SDK 降级回执（`scene_degraded=true` + warning）、scene 检索回执（`scene_resolved=working`）；真实模型 `test_scene` 回归。
- **同步记录**：
  - 2026-11-03：登记并修复（红→绿用例 + 受影响套件；schema 全文见同批次报告）。

## U7：shadow_only 混淆两种成因（详细）

- **现象**：`shadow_only` 用同一个标记、同一句 error 覆盖两种完全不同的成因——「抽取真失败（LLM/embedding 异常、解析出错）」与「内容与既有记忆重复、抽取正常完成但产出 0 条可检索事实」。运维侧无法区分，只能重投后看是否再次落到影子。
- **可复现证据（事实，2026-11-03，SDK 2.0.13，真实库 `D:/Agent_memory/hy-mem-data`）**：job 97（2026-10-30 创建）正文 `rc2验收A-1793324115: 用户的 hy-mem 已升级到 1.0.0-rc.2 并完成安装验证`；`retry --shadow` 重投（`requeued=1`，仅此一条）后以 `worker --max 1 --once` 真实重跑：耗时 7.6s、日志 `[lane0][97] add ok 7608ms ids=1 shadow_only`、无异常栈，仍产出 0 条可检索事实，只落一个新的 l1_raw 影子节点 `27ee0cbb-52c9-4890-9968-be3559a53e30`（首次为 `a37faaff`），`attempts=1`、`shadow_only=1`，error 与首次**逐字相同**：`shadow_only: 抽取未产出可检索节点（只落 l1_raw 影子），本条检索不到；内容确有可抽取的事实时可 hy-mem retry --shadow 重做`。而库里 job 94/95/96 是与它几乎相同的同一句（仅时间戳数字不同），均已正常落库且可检索。事后两次相关检索（含 `1793324115` 与 `rc.2 安装验证`）均未命中 `27ee0cbb`，命中的是 94/95/96 的重复记忆。
- **触发条件**：内容与既有记忆高度重复（无可抽取的新事实）时重投；或真实抽取链路故障（LLM/embedding 异常、解析出错）。两种情况下落库表现相同。
- **影响**：`shadow_only` 计数失去可解释性；告警无法判断"要不要重跑"；对重复内容重投纯属空转（本例白跑一次 7.6s 抽取，且影子节点 id 被替换，队列审计上看起来"做过处理"）。
- **建议方向（给上游的选项）**：在 error/状态里区分成因，例如 `duplicate_no_new_fact` 与 `extract_failed`；或保留 reconcile/planner 的判定输出（是否被判重复、候选事实数、丢弃原因）供排查；至少要让 `retry --shadow` 之后能区分"真失败"与"重复空转"。
- **我方处理口径（当前）**：不自动重试重复内容；此项登记观察，无代码改动。需要重投前先人工确认该条是否与既有记忆重复。
- **验证方式（上游新版到达时）**：在干净沙箱构造两条 add——一条"与既有记忆同义重复"、一条"强制抽取失败"（如指向不可达 LLM 端点）——比对两条 job 的 error/状态字段是否可分；能区分则评估据此过滤重复重投。
- **同步记录**：
  - 2026-11-03：登记（P2 后批次实测，job 97 重投空转；证据与检索复测见本段）。
