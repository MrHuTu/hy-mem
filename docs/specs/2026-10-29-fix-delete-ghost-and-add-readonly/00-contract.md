# 契约：delete 幽灵节点（B1）与并发 add readonly（B2）

> 立契约：2026-10-29。分道：L2 修复。
> 状态：**待澄清确认后执行**（本文件 §1 为唯一事实源，改 §1 走变更记录）。

## 0. 背景与实测证据

两个 bug 均来自 2026-10-29 下午的并行实验（详见 .workbuddy/memory/2026-10-29.md）。

### B1：delete 后 l2_fact 向量节点残留（幽灵节点）【数据完整性 · 高优】

实测链：
1. `hy-mem add "并行实验条目乙…"` → memory_id=`c418c57b`（真实库）
2. `hy-mem delete c418c57b…` → 输出「已删除」
3. `hy-mem get c418c57b…` → 「未找到」（L1 已删）
4. `hy-mem search "并行实验条目乙"` → **仍命中**，`memory_id=c418c57b`，`layer=l2_fact`，score 0.649
5. 第二次 delete 仍输出「已删除」

源码定位：
- SDK `async_delete`（.venv/.../hy_memory/client.py:4443-4530）设计上"同时删除向量库节点"，
  实测未清除 l2_fact 层节点（向量层删除的范围待 R1 取证）
- CLI `cmd_delete`（src/hymem/cli.py:1340-1354）**不检查返回的 success/found**，
  无条件打印「已删除」——found=False / deleted_count=0 也报成功

### B2：同库多进程并发 sync add，第二个进程报 readonly database【并发健壮性 · 中优】

实测链：
1. 同一沙箱数据目录，两进程同时 `hy-mem add`（各带独立环境变量前缀）
2. 甲成功（memory_id=`f8d6923e`），乙报 `OperationalError: attempt to write a readonly database`
   （hint 还误导为「嵌入密钥无效」，实际无关）
3. 乙随后单独重跑 → 成功。失败是瞬态的，但对用户表现为硬失败

源码定位（机制链，最终确认靠 R2 复现取证）：
- hy-mem 自建队列库已有防护：`connect()` / `transient_write()`
  （src/hymem/core/store.py:100-145）——注释原话：**WAL 在「最后一个连接关闭 → 删
  -wal/-shm → 下个连接重建」窗口里，已打开连接写入会报 readonly（Windows 并发实测约 5%）**
- serve 模式只保护 chroma 向量库（数据所有者唯一）；但 SDK 在客户端进程内仍有自己的
  本地 sqlite 写入（history/graph/shadow 状态），**这些写入没有 transient retry 保护**
- 错误分类（src/hymem/core/errors.py:95-98）把 sqlite3.OperationalError 归入通用
  RUNTIME，hint 指向嵌入密钥——误导
- 锁模型（src/hymem/cli.py:1826 注释原话）：「SDK 的 add 全程需要 WriteLock 串行
  （reconcile 读改写，锁必须覆盖整个 client.add 含 LLM 抽取），多进程只是排队，吞吐恒为 1」

## 1. 需求（澄清后冻结）

### R1 · delete 幽灵节点修复
- R1.1 SDK 向量层删除必须覆盖 add 产物产生的全部可检索节点（含 l2_fact 派生节点）
- R1.2 CLI `cmd_delete` 透传 SDK 结果：found=False / success=False 时输出明确失败
  或警告（不得报「已删除」）；--json 输出包含 found/deleted_count
- R1.3 修复后重放本契约 §0.B1 的实测链：delete → get 未找到 → search 0 命中
- 遗留数据清理：真实库幽灵节点 `c418c57b`（l2_fact）在修复后删除

### R2 · 并发 add readonly 瞬态修复
- R2.1 复现取证：双进程并发 sync add 压测（沙箱），抓取乙的完整失败路径与
  涉事 sqlite 文件（是 SDK 本地库还是 chroma 端），落 .local-test/repro_parallel_add.py
- R2.2 对涉事写入套瞬态重试（readonly/locked 换新连接，参照 store.py:123-145 的
  transient_write 模式）；重试上限与退避对齐现有实现（4 次 / 0.3s 递增）
- R2.3 错误分类：sqlite readonly/locked 单列一条 hint（指向重跑命令），不得指向嵌入密钥
- R2.4 回归：并发压测无裸崩/未分类失败（允许形态：E_LOCK 锁快失败、E_DB_BUSY
  重试耗尽后的结构化失败）；转正为 scripts/test_concurrency.py 风暴用例
  **【变更 2026-10-29 执行期】**：原文「0 硬失败、瞬态重试全部吞掉」口径过严——
  WAL 重建窗口实测可长于重试预算，重试耗尽后按 R2.3 归为 E_DB_BUSY（干净失败、
  hint 正确）属诚实暴露而非硬失败。8 轮风暴实测：8 ok + 7 e_lock + 1 e_db_busy、零裸崩

### R3 · sync add 抢锁 fail-fast + 工具口径修正【智能体体验 · 与 R2 同期】

用户拍板（2026-10-29）：sync add 保持单写者设计，但抢不到锁要**立即友好失败**，
不许让智能体干等 180s 后撞 TimeoutError。

现状证据：
- WriteLock strict 路径（src/hymem/core/store.py:200-205）：默认等 180s 后抛
  TimeoutError；非 strict 更危险——超时后**无锁放行**（reconcile 丢更新风险）
- MCP add_memory 默认 async=true（mcp/server.py:139），但 async=false 的同步分支
  走同一条 180s 等锁路径；描述写「同步等待落库（8~40s）」，实测 18~100s，口径失真

条款：
- R3.1 CLI sync add 抢锁改为短超时 fail-fast（默认 5s，`HY_MEM_ADD_LOCK_TIMEOUT`
  可调）：超时返回结构化失败（E_LOCK），文案三要素——①本条未入库（零写入）
  ②单条可稍后重试 ③多条/并发务必 `hy-mem add --async`（入队即返回，3 车道消费）。
  不再复用 180s 的 HY_MEM_LOCK_TIMEOUT 语义
- R3.2 MCP `add_memory` 同步分支（async=false）同样 fail-fast，结果里带
  guidance 字段（同上三要素）
- R3.3 口径修正（helptext.py 单一真源 + mcp/server.py 工具描述）：
  同步 add 标注「单写者设计；实测单条 20~100s（LLM 抽取主导）；并发/多条用
  --async」；MCP 描述的 8~40s 改为实测口径
- R3.4 回归：test_cli / test_mcp 新增 fail-fast 用例（模拟锁被占 → 断言退出码、
  文案三要素、零写入），计入套件总数

## 2. 执行记录（执行时回写）

| 分片 | 状态 | commit | 产物 | 验收证据 |
|---|---|---|---|---|
| R1 | ✓ 完成 | 5a5840f | clientops 级联清扫 + cmd_delete 透传 + helptext + 单测×4 | 集成红测试 .local-test/repro_delete_ghost.py 红 3→绿 4/4；test_cli 88/88；真实库幽灵 c418c57b…bcdf 已清除、复验 0 命中 |
| R2 | ✓ 完成 | 6cc35ec | clientops 瞬态重试 + errors E_DB_BUSY + 风暴用例 | .local-test/repro_parallel_add.py 8 轮：8 ok + 7 e_lock + 1 e_db_busy、零裸崩；test_concurrency 全绿（异步段 + 风暴段） |
| R3 | ✓ 完成 | eb49959 | CLI/MCP fail-fast + 口径修正 | test_cli 96/96（锁占用全链路 + 零写入 + help 口径）；test_mcp 31/31（lock_timeout + guidance） |
| R4 | ✓ 完成 | 见下 | two_phase.py + CLI/MCP 接入 + 锁探测前置 + 单测×6 | repro_two_phase 6/6；test_cli 97/97；test_mcp 31/31；并发实测 60.1s 双成功；E2E 拓扑 L1 shadow/L2 挂根 |

## R4 · 锁粒度细化：网络调用出锁，提交段串行【2026-10-29 用户拍板加入】

用户定调：向量模型与 LLM 支持并发，把它们关进锁里严重拖慢 sync add；
**只保证 SDK 的同步写入（提交段）串行即可**。

### 现状证据
- 锁模型：WriteLock 一把覆盖整个 client.add 含 LLM 抽取（cli.py:1100/1737，
  注释原话"多进程只是排队，吞吐恒为 1"）
- SDK 已有半套两段式 API：`async_add_extracted`（client.py:2984，"跳过 extract，
  直接 reconcile"）——**缺的是 extract-only 公开方法**（client.py 无 async_extract）
- 实测耗时结构：LLM 抽取 18~100s（主导）；嵌入 ~0.3s；reconcile+写 <2s

### 锁粒度真相（2026-10-29 晚源码实读，修正此前认知）

- **SDK 内部本来就是细粒度**：LLM 抽取（mem_agent.process_add，18~100s）在
  锁外；只有 reconcile+store 段持 per-user 进程内 asyncio 锁
  （pipelines/writer.py:1212 `_reconcile_user_lock`）。SDK 设计即"抽取并发、
  提交串行"
- **粗的是 hy-mem 的跨进程 WriteLock**（cli.py:1826）：SDK 进程内锁管不到
  多进程，当年一刀切包了整个 client.add——这是 hy-mem 侧的历史决策，非 SDK 要求
- **extract-only 出口已存在**：`mem_agent.process_add(mode=ProcessMode.EXTRACT_ONLY)`
  （agent/mem_agent.py:180）纯抽取零落库。client 级公开 API 2.0.8 仍没有，
  但 R4 走内部入口（耦合一个对象属性）即可，**不再阻塞于上游**

### 设计（两段式 v3 定稿 · 遵守 SDK 设计规范 · 2026-10-29 20:51 用户拍板）

用户定调：「按照 SDK 的源码注释来，L1 存档。我们要遵守 SDK 设计规范。」
v2 的 add_extracted 路线作废——SDK 注释（writer.py 模块头 + write_extracted docstring）
明确标准提交段结构：**原始内容始终存为 L1_RAW → reconcile 落 L2（带
source_raw_memory_id 挂根）→ 提取成功后 L1_RAW 降级 SHADOW**。v2 经
add_extracted 落 L2 断掉挂根链接，不合规。

- Phase 1 `extract_phase`（无 WriteLock，可多进程并行）：
  `_collect_existing_tags`（只读）+ `mem_agent.process_add(mode=EXTRACT_ONLY)` →
  AgentResult；失败抛 ExtractFailed（零写入）
- Phase 2 `commit_extracted`（持 WriteLock，SDK 标准提交段，逐行对齐 writer.write()）：
  ① embed → `MemoryNode(L1_RAW, ACTIVE)` → upsert → memory_id（**L1 存档**）
  ② basic_info → upsert_basic_profile（L0 演化链，best-effort）
  ③ `_collect_new_memories` → [≥2 条] `_dedup_extracted`
  ④ `_RECONCILE_ENABLED ? _reconcile_and_store : _direct_store`，
     **两路都透传 source_raw_memory_id=L1 根**（v3 修复 v2 断链）
  ⑤ intentions → _store_intentions（L7，best-effort）
  ⑥ L1 → SHADOW（成功路径；reconcile 失败留 ACTIVE，同 SDK「数据可救」语义）
- **锁探测前置**（2026-10-29 test_cli R3 用例暴露）：extract 前先抢一次
  WriteLock(timeout=5)，抢不到立即 E_LOCK——否则锁被占时 extract 白烧一次 LLM
  还误报 E_EXTRACT。探测与正式抢锁间存在竞态窗口（extract 期间被抢），属少数
  情况，正式抢锁段兜底 E_LOCK，可重试
- 实现位置：`src/hymem/core/two_phase.py`（单文件封装，内部耦合全部集中于此；
  SDK 若日后开放「extracted + L1 存档」公开入口应整体替换）
- 与 SDK 标准 write() 的显式差异（有意为之）：顺序颠倒（extract 先行，失败零
  写入 fail-fast）；观测面（tracer/埋点/WRITE_SUMMARY）不复刻
- 内部耦合点（SDK 升级须回归 two_phase.py）：~15 处，全部集中在模块头注释清单
- 与 R3 衔接：锁内提交段实测 3~15s（reconcile 判重主导）；fail-fast 默认 5s 维持
- 预期收益：N 条并发 sync add 墙钟 ≈ max(LLM) + N×commit，而非 Σ(LLM)+Σ(commit)

### 依赖（2026-10-29 晚间更新：SDK 身份与上游核实）

**SDK 身份纠正**：hy-memory 不是自研 SDK，是**腾讯混元开源项目**
（GitHub `Tencent-Hunyuan/Hy-Memory`，Apache-2.0，PyPI 分发）。此前契约里
"SDK 源码仓库待用户提供"的表述作废。

上游核实（2026-10-29，PyPI + 2.0.8 wheel 实拆）：
- 我们用 **2.0.5**，上游最新 **2.0.8**（落后 3 个小版本）
- **2.0.8 仍无 extract-only 公开 API**（只有 add_extracted 家族）→ R4 两段式
  依旧缺 SDK 支持；extract 逻辑没有独立出口
- **2.0.8 delete 主流程与 2.0.5 相同**（evolution chain 修复 → 单点 delete →
  retrieval_clauses → graph）→ 幽灵节点大概率未修
- 2.0.8 新增 `delete_by_metadata`（仅 delete_all / 迁移场景使用），且签名只支持
  user_id/agent_id/session_id 组合，**不能按单条 memory_id 删**

### B1 幽灵节点根因（源码级定位 + 沙箱拓扑实测，2026-10-29）

实测拓扑（修正早前推断）：
- `memory_id`（add 返回）= **l1_raw 根节点的 node_id**；抽取完成后根节点常态
  status=shadow（原话被事实取代，不参与检索）
- l2_fact 派生节点持有独立 node_id，metadata 带 `source_raw_memory_id` 指向根
- `client.delete(memory_id)` 只删根节点本身，**不级联 source_raw 指向它的派生
  节点**；单 fact 的 add 无感，多 fact（memory_ids 复数）时兄弟 fact 残留可检索
- SDK 公开 delete 返回**没有 `found` 键**（found 只在内部 op-log dict）；
  `vdb_deleted` 因 ChromaVectorStore.delete 恒 True 不可信——CLI 不能拿它当判据

1. `ChromaVectorStore.delete()`（vector_store_chroma.py:364-374）：
   `collection.delete(ids=[point_id])` 后**无条件 return True**——删不到也报成功
2. client.delete 不级联派生节点（上）
3. CLI `cmd_delete` 不检查结果（见 R1.2），叠加"delete 恒 True"，双层假成功

### 修复路径约束（用户拍板：不修改 SDK 源码）

- 上游可行路径：向 GitHub 提 issue（附本契约 §0.B1 复现链），等上游修复后升级
- R1 短期缓解（不改 SDK）：CLI 侧 delete 后**回读校验**（client.search 按
  memory_id 过滤，命中则警告"上游 bug：向量节点残留"）+ cmd_delete 修正假成功
- R4 短期缓解（不改 SDK）：备选 B（hy-mem import SDK 内部抽取管道）——耦合
  私有结构、升级易碎；或维持 R3 fail-fast 缓解、等上游开放 extract API

### 条款
- R4.1 ✓ Phase1 抽取走 `mem_agent.process_add(mode=EXTRACT_ONLY)`（SDK 内部入口，
  升级 SDK 时本分片须回归测试；不复制抽取逻辑）
- R4.2 ✓（v3 修订）Phase2 锁内 SDK 标准提交段：L1 存档（embed+MemoryNode+upsert）
  → `_reconcile_and_store`/`_direct_store` 透传 source_raw_memory_id 落 L2；
  L1/L2 都进库；成功路径 L1→SHADOW（遵守 SDK 规范，原文退居备份层不参与召回——
  这本就是 SDK 的召回面定义，非缩水）
- R4.3 ✓ 并发验收：双进程并发 sync add 各 1 条，wall=60.1s 双成功（单条全程
  ~50-60s，串行预估 ~110s，60.1 < 110×0.7=77）；另轮 p1 撞锁得干净 E_LOCK
  （零裸崩、零丢更新）；test_concurrency 风暴 4 轮×2 进程 ok=5 e_lock=3 零裸崩
- R4.4 ✓（修订）request_id 全程透传至 WriteRequest（日志/ops 追溯单一真相源）；
  SDK 层幂等回放（_claim_write_outcome）不接——手写提交段绕过 client 入口，
  重复调用可能产生重复条目，reconcile 判重会吸收（与 add_sync_with_retry 同取舍）
- R4.5 ✓ 检索对齐：E2E 实测「agent-bridge 项目在哪」事实查询召回第 1 名
  （l2_fact, score 0.72）；L1 shadow 不进召回（SDK 规范原意：原文层本来就不在
  _VDB_RECALL_LAYERS）
- R4.6 ✓（21:57 追加，worker 侧）`_execute_job` 的 add 分支同构接两段式：
  extract 锁外（3 lane 线程真并行过 LLM）+ 锁内 commit_extracted_with_retry；
  lite 模式（无 LLM 抽取）保持旧整段路径。动机：风暴解剖（probe_storm_anatomy）
  发现 worker 旧写法「锁内整段 client.add」把 3 车道退化成串行 × 全程 ~45s/条
  ——test_concurrency 异步段 187s 的真因。验收：test_queue 21/21 +
  test_concurrency 异步段耗时对比（改造前 187s 基线）
- R4.7 ✓（22:23 追加，Phase 1.5 预计算出锁）`prepare_commit` 新段：L1 原文
  embed + facts 收集 + 本批 dedup（含各 fact 向量）+ WriteRequest 构造——
  全部纯函数不读不写库状态（dedup 本批内判重，writer.py:452 实读确认），
  从锁内挪到抢锁前。锁内只剩 reconcile 判定 LLM + upsert + SHADOW。
  CLI/MCP/worker 三调用点同步接入；prepared 可跨瞬态重试复用。
  验收：真库实测锁内 commit 2~11s → **1.5s**；prepare 0.5s 锁外；
  repro_two_phase 7/7（T7 新增：prepare 锁外可执行 + commit 复用零 embed +
  语义回归）；test_queue 21/21、test_mcp 31/31、test_cli 97/97；
  风暴复测 4 轮×2 进程 ok=7 e_lock=1 零 BAD
- R4.8 ✓（22:40 顺带修复，R4.7 风暴暴露的存量 NameError）pyflakes 全扫
  发现两处平移期漏 import，均概率性路径（并发才触发，串行测试全绿掩盖）：
  ① clientops.py:75 make_client 重试循环 time.sleep（R2 时代引入）
  ② searchops.py _spawn_detached_worker 的 subprocess（S3-1 平移引入）。
  修复后风暴 4 轮×2 零 BAD。教训入记忆：平移/重构后必须跑 pyflakes 级
  静态扫，串行单测对概率路径零覆盖
- R4.9 ✓（23:05 追加，EMPTY_RESPONSE 瞬态重试）R4.7 后风暴新形态：
  并发负载下 LLM 网关偶发空响应（实测 1/8 概率）→ E_EXTRACT 干净失败。
  本身零写入可重试（设计如此），但属网关瞬态——按 R2 哲学加换次重跑：
  extract_phase 内 EMPTY_RESPONSE 最多重试 3 次（0.5s 递增退避），其余
  错误码（配额/鉴权）不重试直接抛。风暴分类器（repro_parallel_add +
  test_concurrency）同步收编 E_EXTRACT 为合法干净失败形态。
  验收：风暴 4 轮×2 零 BAD（7 ok + 1 E_LOCK，重试后 EMPTY_RESPONSE 消失）

### 执行记录（2026-10-29 20:51–21:40）

- 新增 `src/hymem/core/two_phase.py`：extract_phase / commit_extracted /
  commit_extracted_with_retry / ExtractFailed / CommitFailed
- `cli.py` cmd_add 同步分支：锁探测前置 → extract_phase（锁外）→
  WriteLock + commit_extracted_with_retry；E_EXTRACT / E_LOCK / E_COMMIT 三态
- `mcp/server.py` add_memory 同步分支同构改造 + 工具描述口径更新（两段式）
- `errors.py`：ExtractFailed→E_EXTRACT、CommitFailed→E_COMMIT 分类
- `ui/helptext.py` add 条目口径更新
- 红测试 `.local-test/repro_two_phase.py`：T1 锁外行为（真 WriteLock 占锁实测）
  T2 提交段语义（L1 存档→挂根→SHADOW）T3 reconcile 失败留 ACTIVE
  T4 extract 失败零写入 T5 多条 dedup+basic_info T6 MCP 锁探测前置——6/6 绿
- 套件回归：test_cli 97/97（R3 help 口径断言更新）、test_mcp 31/31、
  test_concurrency 全 PASS
- E2E（真库）：add 成功 → L1 根 status=shadow、L2 leaf l2_fact/active 且
  source_raw_memory_id 挂根（chroma where 查询实测）、事实查询召回第 1

## 边界（不做）

- 不改 SDK 源码（用户拍板）；SDK 内部入口（mem_agent）的耦合仅限 Phase1 一个调用点
- 不做嵌入出锁：0.3s 量级，出锁需 reconcile 支持 precomputed candidates，另立 spec
- 不做 sync add 的服务化改造（常驻进程队列）：两段式 + R3 fail-fast 已覆盖痛点
- 真实库幽灵节点的手工 chroma 清理不做（等 R1 修复后走正规 delete）
