# S5 决策记录：ultra（System 2 / L6 Schema）现在接不接

- 日期：2026-10-30
- 结论：**不接，缓做**。成本不是问题，产出是问题——30 条同主题事实都归不出 L6 schema。
- 本档只评估不实现（spec §1 S5 原定），未改动任何生产代码。

## 一、结论先行

| 问题 | 实测答案 |
|---|---|
| ultra 起得来吗 | 起得来。kuzu 0.11.3 已随 SDK 装好，`mode="ultra"` 初始化 4.2~4.4s（pro 4.4s，无差异） |
| evolve 贵吗 | 不贵。30 条同主题库：一次 evolve **4.0s / 4209 prompt tokens**，只跑 1 批 LLM |
| 能产出 L6 schema 吗 | **不能**。跑完 `schemas=0`、`created=None`，layer 分布仍只有 `l2_fact` |
| 为什么产不出 | 二阶段细分把 20 条簇判成「0 子簇 + 20 噪声」，S2 agent 拿不到可归纳的证据组；sweeper 又因 `no L6 basics` 直接跳过 |

一句话：**现在的库形态（个人技术事实）不满足 L6 归纳的前提，接进来只是每次多跑一遍空转。**

## 二、三次探针的实测数据

| 探针 | 库规模 | evolve 耗时 | clusters | 产出 |
|---|---|---|---|---|
| `probe_ultra_evolve.py` | 6 条**分散**技术事实 | 1.8s（首次）/ 0.1s（二次） | 0（`no_clusters`） | 无 L6，sweeper `no L6 basics` |
| `probe_ultra_real.py` | 真实库副本 | 1.2~1.3s | 0（`no_facts`，副本向量库未读通） | 探测无效，作废 |
| `probe_ultra_cluster.py` | 30 条**同主题**（hy-mem 自身架构） | **4.0s** | 1 个 stage1 簇（20 条） | **仍无 L6**，sweeper 跳过 |

第三次探针的关键日志（原文）：

```
[S2-preprocess] Facts triage: fresh=25 evidenced_excluded=0 (pool=25)
[S2-preprocess] Refined cluster (size=20) → 0 sub-clusters + 20 noise
[S2-preprocess] Clustering: 25 facts → 1 stage1 → 1 final clusters
[S2-preprocess] clusters=1 clustered_facts=20 source_groups=0 unclustered_supplement=5
[S2-preprocess] Facts triage: fresh=5 evidenced_excluded=20 (pool=5)
[S2-preprocess] Clustering: 5 facts → 0 stage1 → 0 final clusters
[S2-preprocess] no clusters → skip S2 agent
[S2] Cross-domain sweeper: {'skipped': True, 'reason': 'no L6 basics'}
```

附带观察：30 条同主题写入后库里只剩 25 条 —— reconcile 合并了 5 条（约 17%）。事实层在自我收敛，本身就没给 L6 留出「多版本证据」的空间。

## 三、成本结构（来自 SDK 源码，供将来重估）

- 单批 = **1 个 cluster 走 1 次 agent 式 LLM 调用**（`system2_writer.py` Phase 2，`max_batches` 默认 `MEMORY_EVOLING_MAX_BATCHES=128`）
- 每批会重新 `prepare_materials`（重扫 VDB + 重聚类），批数多时扫描开销线性叠加
- 聚类门槛：`MEMORY_S2_CLUSTER_THRESHOLD=0.55`（即余弦距离 0.45）、`MEMORY_S2_MIN_CLUSTER_SIZE=4`、`_MAX_CLUSTER_SIZE=12` 触发二阶段细分
- 图依赖：`GraphStoreConfig`（Kuzu/Neo4j/Memgraph），默认不启；`MEMORY_GRAPH_WRITE` 默认 True 但仅在图开时生效
- sweeper（`sweep()`）独立于 evolve，只补 schema 的 behav abstraction + 碰撞归纳，**没有 L6 basics 就永远是 skipped**

按 30 条 4.2k prompt tokens 外推 149 条分散事实：即便凑出 3~5 个簇，也就是 1.5~2 万 tokens、十几秒一次。**成本从来不是拦路的那一项。**

## 四、为什么不接

1. **产出为零**：唯一跑出 cluster 的用例（30 条同主题，20 条聚成一簇）依然 `schemas=0`。接进来等于给用户一个「ultra 已开」的错觉。
2. **前提不成立**：L6 schema 要的是「同一行为在多个场景反复出现」的证据组；我们的库是个人技术决策记录，reconcile 还在持续合并同类事实，天然缺多版本证据。
3. **改动面不小，收益不可验证**：需要新增 `hy-mem evolve` / `sweep` 命令、ultra 模式切换引导、L6 层检索展示（schema_limit / intention_limit 目前我们完全没暴露）。做完也说不清对 chat 记忆的实际增益。
4. **测试面**：ultra 会让 add 走更重的 System 1，现有 12 套件里有真模型套件（mcp/concurrency）要重跑基线，成本与收益不成比例。

## 五、什么条件下重新评估

满足任一条再回来读这份记录：

- 库规模到 **千条级** 且主题集中度明显提升（比如单一项目占 60% 以上）
- 出现真实需求：「把散落的同类决策归纳成一条通则」——这是 L6 唯一真正解决的问题
- SDK 侧打开 schema evidence gate（`MEMORY_S2_SCHEMA_EVIDENCE_GATE_ENABLED`）或调整聚类阈值后，重跑 `probe_ultra_cluster.py` 能看到 `schemas>0`

## 六、若将来要接，改动面清单（预估）

| 项 | 内容 |
|---|---|
| CLI | `hy-mem evolve [--user] [--max-batches]`、`hy-mem sweep [--user...]`；`config set mode=ultra` 的确认与回滚提示 |
| 检索 | `search --layer schema` 或 `--schema-limit`（我们目前不暴露 schema_limit / intention_limit） |
| MCP | `evolve_memory` 工具（长任务，需说明「同步阻塞、分钟级」） |
| 闸门 | 真模型套件加 ultra 基线；`probe_ultra_cluster.py` 转正为 `tests/test_ultra.py` |

## 七、探针位置

`.local-test/probe_ultra_evolve.py`、`probe_ultra_cluster.py`（`probe_ultra_real.py` 因副本向量库未读通，结论作废，留档不改）。
