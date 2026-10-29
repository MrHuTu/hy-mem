# 多条 job 合并消费（batch commit）契约

- slug：`2026-10-30-batch-commit`
- 日期：2026-10-30 00:00（2026-10-29 深夜立项）
- 状态：**§1 设计待用户拍板**（§2 未动）
- 上游契约：`2026-10-29-fix-delete-ghost-and-add-readonly`（R1–R4.9 已全部完成）

## §0 问题与动机

### 现状（R4.9 后的耗时结构）

worker 异步消费 4 条 job 实测 **103.2s**（test_concurrency 基线）。每条 job
独立走两段式：extract LLM（13s，3 lane 并行 ✓）→ prepare（0.5s，锁外 ✓）→
锁内 commit（1.5s，含**每条一次** reconcile 判定 LLM ~8s——等等，判定在锁内
且每条各判各的：4 条 = 4 次判定 LLM ≈ 32s 串行排队（per-user 锁 + WriteLock）。

**剩余瓶颈**：reconcile 判定调用次数 = job 条数。SDK 的 RECONCILE_PROMPT
本身就是**批量格式**（一次判定多条 facts，writer.py 单次 write 多 fact 是
原生场景）——我们每条 job 一次调用，浪费了 SDK 的批量能力。

### 目标

同批多条 job 的 facts 合并成**一次** `_reconcile_and_store` 调用：
判定 LLM 次数 N→1，批量场景总耗时从 `init + extract + N×判定` 降到
`init + extract + 1×判定(K 条)`。

### 设计原则（用户定调 + 执行补充）

| # | 原则 | 来源 |
|---|---|---|
| P1 | **对齐主流 RAG 批量摄入做法**：LangChain `batch`/LlamaIndex ingestion pipeline/vector DB 批量 upsert 都是「攒批 → 一次嵌入 → 一次写」，判定/去重合批是同类范式 | 用户 |
| P2 | **贴齐 SDK、不改 SDK 源码**：只用 SDK 原生批量入口（`_reconcile_and_store` 批量签名 / `_dedup_extracted` / `update_payload`），耦合点集中并在本 spec 登记回归义务 | 用户（"最气SDK"理解为贴齐 SDK） |
| P3 | **lineage 不降级**：合批后每条 job 的 L1 根→L2 挂链（source_raw_memory_id）必须逐条正确，验收含拓扑核对 | 执行补充 |
| P4 | **失败隔离**：批内单条失败不连坐；整批失败回退逐条重试语义（复用现有 mark_failed 退避） | 执行补充 |
| P5 | **保守合批**：默认小批量（4）、env 可调可关（0=关）、异构 job 不合批；宁可不合也不改语义 | 执行补充 |
| P6 | **可观测**：判定 LLM 次数、批大小、lineage 修复计数全部落日志/任务行 note | 执行补充 |

## §1 设计（待拍板）

### 合批约束（哪些 job 能进同一批）

| 维度 | 规则 | 理由 |
|---|---|---|
| user_id + agent_id | **必须相同** | reconcile 候选检索按 user 圈定；per-user asyncio 锁一把 |
| session_id | 不要求（合并请求 session 置空） | 避免 episode 归属污染跨会话记忆 |
| 显式 memory_at | **有则不入批**（回退单条） | memory_at 是 request 级、落在 ADD 节点上，合批会串时间 |
| metadata / request_id / client_meta | **有则不入批** | request 级字段无法逐条透传；幂等回放不在两段式路径上 |
| mode | lite 不入批（无抽取，单条路径已够快） | R4.6 已定 |
| kind | 只合 `add`；finalize 永不 | finalize 是会话级操作 |

### 批次流水线（在 R4.7 三段式上叠加）

```
每 lane（不变，锁外）：extract_phase → prepare_commit   ← 逐 job，3 lane 并行
                              │
合批点（新增）：lane 领任务时一次 claim 多条（同 user+agent、约束过滤后）
                              │
batch_commit（新增，一次 WriteLock 内）：
  ① 批级 _dedup_extracted（合并各 job 的 facts 互相判重，内容唯一化）
  ② 逐 job：L1 根 upsert（各 job 各自的原文/向量，SDK 规范不变）
  ③ 一次 _reconcile_and_store(全部 facts, source_raw_memory_id="")
  ④ lineage 修复段：按 content→job 精确映射，update_payload 逐 ADD 节点
     回填正确的 source_raw_memory_id（P3；UPDATE 节点天然不受影响——
     writer.py:1417 `if source_raw_memory_id:` 空串不写，历史挂链保留）
  ⑤ 逐 job：L1→SHADOW、tags 绑定、结果 dict、shadow_only note
```

**为什么传空 `source_raw_memory_id` 再修复**：SDK 批量入口只收单一根 id，
传任何一个 job 的根都会把整批 ADD 挂错；传空 + 修复段是唯一既走 SDK 原生
批量路径、又保 per-job lineage 的做法。UPDATE 路径实测确认：空串时
`updates["source_raw_memory_id"]` 不写入（writer.py:1417 守卫），旧节点
演化链零扰动。

### worker 改动

- `store.claim_batch(conn, limit, ident_filter)`：一个事务领 N 条同 user+agent
  的 pending add（现有 claim_next 单行逻辑扩展；无 schema 变更，批上下文
  只在内存 + 任务行 note 留痕）
- lane 循环：队列深度 >1 且首条可入批 → claim_batch（`HY_MEM_BATCH_MAX`
  默认 4，0=关闭合批）；否则走现单条路径（完全保留）
- 判定耗时对比落 stderr 日志（`[batch] jobs=K judge_llm=1 vs K`）

### 失败语义

| 场景 | 行为 |
|---|---|
| 批内某 job 的 extract/prepare 失败 | 该 job 单独 mark_failed 退避；其余照常合批 |
| 整批 reconcile 失败（CommitFailed） | 批内全部 mark_failed（错误信息带 batch 上下文）；重试时重新评估合批 |
| reconcile 部分成功后异常 | 同上全部退避重试；重复条目由判重吸收（与 R2 同取舍，契约已有先例） |
| lineage 修复段失败 | 不判失败：L2 已正确落库可检索，仅挂链缺失——note 落任务行 `lineage_partial`，可日后修复（诚实降级，不假绿） |

### 分片

| 分片 | 内容 | 预估 |
|---|---|---|
| S1 | `store.claim_batch` + 单测（同 user 过滤、约束排除、事务性） | 小 |
| S2 | `two_phase.batch_commit` 核心（合批/空根/修复段/逐 job 结果）+ Fake 红测试（lineage 逐根核对、dedup 跨 job、UPDATE 不受扰） | 中 |
| S3 | worker lane 接线 + env 开关 + helptext/queue 口径 | 小 |
| S4 | 验收：test_concurrency 异步段计时（目标 ≤80s，基线 103.2s）、判定 LLM 次数日志取证（4→1）、E2E 拓扑（批内每 job L1↔L2 挂链）、全套回归 | 中 |

### 验收口径

1. test_concurrency 异步段 4 条：**≤80s**（基线 103.2s；LLM 抖动下看两轮取优）
2. 判定 LLM 次数：日志取证批内 **1 次**（现 4 次）
3. E2E 拓扑：批内每个 job 的 L2 节点 `source_raw_memory_id` == 该 job 的 L1 根（chroma where 逐条核对）
4. 回归：test_queue / test_cli / test_mcp / repro_two_phase 全绿；`HY_MEM_BATCH_MAX=0` 时行为与现状逐字节等价
5. SDK 耦合点登记：`_reconcile_and_store(source_raw_memory_id="")` 空串守卫（writer.py:1417）、`_dedup_extracted`、`update_payload`、content 精确映射（对齐 `_inherit_exact_input_structure` 的 no-fuzzy 原则）——SDK 升级须回归

### 边界（不做）

- 不改 SDK 源码、不提 fork；上游若开放 per-fact root 的批量 API 则整体替换修复段
- 不做跨 user 合批（reconcile 语义不允许）
- 不做定时窗口攒批（`--poll` 间隔天然是窗口；队列有货就合，没货立即走单条，不引入等待延迟）
- sync add（CLI/MCP 直连路径）不合批——单条语义，合并只发生在 worker 队列侧

## §2 执行记录

- **S1 完成（commit 677103b）**：`store.claim_next_batch`——同 (user_id, agent_id)
  批量原子领取、异 user 止批、compat_check 约束注入、首条恒领。
  验收：repro_batch_claim 6/6（含双 worker 并发零重复）；test_queue 21/21。
- **S2 完成（commit 9e9c6ac）**：`two_phase.batch_commit`——批级 dedup →
  逐 job L1 存档 → 一次判定（空 root）→ lineage 修复段 → 逐 job 结果。
  验收：repro_batch_commit 7/7。修 UnboundLocalError（all_texts 空时
  ops_counts/id_to_node 未初始化——"无可提取事实"是正常路径必须扛住）。
- **S3 完成（本次 commit）**：worker lane 接线——`_batch_compat` 约束 +
  `_execute_batch`（单 job extract 失败不连坐）+ lane 批分支
  （peek>1 才批领、1 条退单条路径、批兜底绝不留 running 僵尸）。
  `HY_MEM_BATCH_MAX`（默认 4，0=关闭）。调试教训：dbg 脚本先误判
  "worker 2.2s 卡死"——真相是 add --async 自动拉起的 detached worker
  先领走了任务（running），前台 --once 正常空跑；E2E 改为轮询等
  detached worker 完成。
- **S4 完成（本次 commit）**：
  - test_concurrency 异步段 4 条：**103.2s → 66.7s**（目标 ≤80s 达标，
    基线 103.2s；判定 LLM 4→1）
  - E2E 拓扑（e2e_batch_lineage.py）：L1 根 3 个全 shadow、L2 3 条
    **挂链 3/3 正确**、检索命中
  - 回归：test_queue 21/21、test_cli 97/97、repro_two_phase 7/7、
    repro_batch_claim 6/6、repro_batch_commit 7/7
  - HY_MEM_BATCH_MAX=0 关闭开关走原单条路径（test_queue/test_cli 全绿佐证）

### 遗留（后续 spec）

- 判定 LLM 空响应（EMPTY_RESPONSE / finish_reason=length）在批路径同样
  存在：_execute_batch 的单 job extract 失败走 mark_failed 退避，重试
  后重新评估合批——行为正确但可观测性待加（统计 batch_success 计数）
- 上游若开放 per-fact root 的批量 API，删除 lineage 修复段整体替换
