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
| R4 | pending（设计 v2 定稿：L1/L2 双层保留，只锁提交段 ~2-3s） | - | - | - |

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

### 设计（两段式 v2 · L1/L2 双层保留 · 2026-10-29 19:33 用户拍板）

用户定调：L1 和 L2 都要进库，不丢原文层；只锁提交段。
（v1 曾为省事用 add_extracted 单做 Phase2 而丢 L1_RAW，作废）

- Phase 1（无 WriteLock）：`mem_agent.process_add(mode=EXTRACT_ONLY)` →
  facts 列表（纯 LLM + 读已有 tags，零写入）；多进程天然并发
- Phase 2（持 WriteLock，~2-3s）：
  ① embed 原文 → `MemoryNode(layer=L1_RAW)` → `vector_store.upsert()` 落 L1 节点
     （仿 write() 首段，writer.py:2601-2646，组件全在 writer 实例上）
  ② `client.add_extracted(facts)` → reconcile + 落 L2_FACT
  锁持有从 20~100s 降至 ~2-3s（含原文件嵌入 0.3s）
- **已知差异（记录在案）**：L1↔L2 的 source_raw_memory_id 链接丢失——
  write_extracted/_reconcile_and_store 的该参数不经 add_extracted 透传；
  两层各自可检索、可 get，但派生关系不落 meta（上游 API 限制，提 issue 可解）
- 内部耦合点（SDK 升级需回归）：mem_agent 入口、embed_service/_get_vector_store、
  MemoryNode 构造——共 3 处，均为读用不修改
- 与 R3 衔接：提交段 ~2-3s，fail-fast 默认超时建议调至 8s（原 5s）
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
- R4.1 Phase1 抽取走 `mem_agent.process_add(mode=EXTRACT_ONLY)`（SDK 内部入口，
  升级 SDK 时本分片须回归测试；不复制抽取逻辑）
- R4.2 Phase2 锁内双层落库：先手写 L1_RAW 节点（embed+upsert，仿 write() 首段），
  再 add_extracted(facts) 落 L2；**L1/L2 都进库，召回面不缩**
- R4.3 并发验收：双进程并发 sync add 各 1 条，墙钟 < 两者串行耗时之和 × 0.7
  （LLM 段确实重叠）；且零失败、零丢更新（reconcile 一致性回归）
- R4.4 幂等：两段式全程透传 request_id（Phase2 的 add_extracted 已支持幂等）
- R4.5 回归验收：两段式与整段 add 的检索结果对齐——同文本写入后，原文措辞查询
  与事实主题查询均能命中（分别验证 L1/L2 召回）

## 边界（不做）

- 不改 SDK 源码（用户拍板）；SDK 内部入口（mem_agent）的耦合仅限 Phase1 一个调用点
- 不做嵌入出锁：0.3s 量级，出锁需 reconcile 支持 precomputed candidates，另立 spec
- 不做 sync add 的服务化改造（常驻进程队列）：两段式 + R3 fail-fast 已覆盖痛点
- 真实库幽灵节点的手工 chroma 清理不做（等 R1 修复后走正规 delete）
