# S5 决策记录：ultra（System 2 / L6 Schema）现在接不接

- 日期：2026-10-30（当日复核，第一版结论有误已更正，见 §二）
- 结论：**暂不接，但拦住它的不是「素材不够」而是「默认模型做不了 + 前置门禁太窄」**。
  换成 deepseek-v4.1-flash 后同一份素材能稳定归纳出 1~3 个 schema。
- 本档只评估不实现（spec §1 S5 原定），未改动任何生产代码。

## 一、结论先行

| 问题 | 实测答案 |
|---|---|
| ultra 起得来吗 | 起得来。kuzu 0.11.3 随 SDK 装好，`mode="ultra"` 初始化 4.0~4.4s（与 pro 无差异） |
| evolve 贵吗 | 不贵。30 条同主题库一次 evolve 4~34s、4.2k~5.5k prompt tokens |
| **为什么归不出 schema** | **默认模型（tongyi-xiaomi-analysis-flash）直接放弃**：prompt 灌了 4209 token，只回 7 个 completion token（约等于一句 `{"operations": []}`），却报 `success=true` |
| 换模型能出吗 | **能**。同一份素材、同一份 prompt、同一套门槛，deepseek-v4.1-flash 两次分别产出 **2 个 / 3 个** schema，内容经人工核对是真实归纳 |
| 还有别的前置限制吗 | 有。L6 要「同主题但细节各异」的一簇（≥4 条）：太分散不聚类，太相似被 reconcile 合并成 1 条，可用夹缝很窄 |

一句话：**第一版结论「个人技术事实库不满足 L6 归纳前提」是错的。同一批事实换模型就归纳出来了 —— 拦路的是模型能力，其次是聚类与去重门禁，不是素材本身。**

## 二、第一版结论错在哪（显式更正）

第一版（同日 12:40 前后）写的是：

> 二阶段细分把 20 条簇判成「0 子簇 + 20 噪声」，S2 agent 拿不到可归纳的证据组；sweeper 又因 `no L6 basics` 直接跳过 → 现在的库形态不满足 L6 归纳的前提。

错在两处：

1. **事实错**：日志里 `Clustering: 25 facts → 1 stage1 → 1 final clusters` 之后，S2 agent **确实被调用了**（`total_prompt_tokens=4209`）。不是「拿不到证据组」，是拿到了却回了空。
2. **归因错**：把「LLM 返回空」读成「素材不支持」。判断依据只是 `schemas=0` 这一个外部现象，没有去看 completion token 数 —— 7 个 token 是这次复核查模型对比时才发现的。

当时的盲点：只换了「素材形态」（分散 6 条 → 同主题 30 条），**没有换模型**。单变量实验从头到尾只动了一个变量，却对另一个变量下了结论。

## 三、决定性证据：同素材换模型

素材与流程完全一致（`probe_ultra_cluster.py` 的 30 条 hy-mem 架构事实、独立沙箱、`mode=ultra`、
`MEMORY_EVOLING_MAX_BATCHES=3`），只改 `llm.model`：

| 模型 | completion tokens | batches | 产出 schema | 自报成功 |
|---|---|---|---|---|
| `tongyi-xiaomi-analysis-flash` | **7** | 1/1 | **0** | `success=true`（空转） |
| `deepseek-v4.1-flash-1017` #1 | 3532 | 1/2 | **2** | `success=false`（第 2 批 JSON 解析失败） |
| `deepseek-v4.1-flash-1017` #2 | 2772 | 2/2 | **3** | `success=true` |

deepseek 实际归纳出的两个 schema（原文，`graph actual count` 与 embed 请求体双重取证）：

```
When designing hy-mem's persistence and version-control layout, the user keeps runtime
data and release artifacts out of git and makes state changes non-destructive (data/
untracked and local-only, builds shipped via GitHub Releases, identity switch hides but
never deletes memories) — reflecting a preference for local data sovereignty and
reversible operations over anything that could silently lose or entangle state.

When structuring hy-mem's memory storage, the user partitions stores by scenario and
enforces explicit write preconditions (chat vs working/coding databases, collection
named by embedding dimension, multi-process access arbitrated by serve, --trace
mandatory in coding writes or E_NO_TRACE) — reflecting a belief that strict boundaries
and auditable write paths are needed to prevent silent state corruption.
```

它把「data/ 不入库 + 发布走 Releases + 身份切换不删记忆」合成「本地数据主权 / 可逆操作」，
把「chat 与 coding 分库 + collection 按维度命名 + serve 仲裁 + E_NO_TRACE」合成
「严格边界 + 可审计写路径」。这是归纳，不是拼接，也不是幻觉。

### 为什么 xiaomi 做不了

它是**意图分类模型**：在封闭候选里做判定（S1 抽事实、reconcile 判重）又快又准，0.7s/条正是它的强项。
但 L6 归纳是**开放生成** —— 5k token 上下文 + 多工具决策（`create_schema` / `add_evidence` /
`skip_fact` 还要覆盖每个候选）+ 结构化 JSON 输出。这两件事对模型的要求不是同一个量级。
实测表现就是：不做尝试，直接返回空，然后被框架当成「决策正确」记 success —— **空转被记成成功**
是这条链路上最危险的一点。

## 四、第二重限制：前置门禁（这部分第一版没错）

换模型也不能无限制产 schema，前面还有两道门：

| 门 | 阈值 | 实测后果 |
|---|---|---|
| DBSCAN 聚类 | `MEMORY_S2_CLUSTER_THRESHOLD=0.55`（余弦距离 0.45）、`MEMORY_S2_MIN_CLUSTER_SIZE=4` | 12 条「用户做事习惯」分属两个主题 → `11 facts → 0 clusters`，LLM 根本没被调用 |
| reconcile 去重 | 语义近重复合并 | 30 条同一行为的细分表述 → **合并成 1 条 fact**，连 `min_samples=4` 都凑不满 |

合起来看，L6 真正需要的素材是「**同主题但细节各异、≥4 条不被合并**」那一小片区域：

| 素材形态 | 落库 fact | clusters | 结果 |
|---|---|---|---|
| 6 条分散技术事实 | 6 | 0 | `no_clusters` 空转 |
| 12 条两主题用户习惯 | 11 | 0 | `no_clusters`，LLM 未调用 |
| 30 条同一行为细分表述 | **1** | 0 | 被去重合并，凑不满簇 |
| 30 条同主题架构事实（细节各异） | 19~25 | 1 | LLM 被调用 → 模型差距暴露 |

## 五、成本结构（来自 SDK 源码）

- 单批 = 1 个 cluster 走 1 次 agent 式 LLM 调用（`system2_writer.py` Phase 2，`max_batches` 默认
  `MEMORY_EVOLING_MAX_BATCHES=128`）
- 每批重新 `prepare_materials`（重扫 VDB + 重聚类），批数多时扫描开销线性叠加
- 二阶段细分：`_MAX_CLUSTER_SIZE=12` 以上的簇用 `MEMORY_S2_REFINE_CLUSTER_THRESHOLD=0.65` 再切
- 图依赖：`GraphStoreConfig`（Kuzu 默认），`MEMORY_GRAPH_WRITE` 默认 True
- sweeper（`sweep()`）独立于 evolve，只补 schema 的行为抽象与碰撞归纳，**没有 L6 basics 永远 skipped**
- 证据门槛 `MEMORY_S2_SCHEMA_EVIDENCE_GATE_ENABLED` **默认 false**（不拦）；若打开，还要求证据
  内容以「用户/user」开头且来自 ≥2 个独立 session

## 六、为什么不接（更正后）

1. **默认模型出不了活，且失败静默**：xiaomi 返回空还报 success。接进来等于给用户一个
   「ultra 已开、L6 在积累」的错觉，比不开更糟。
2. **换模型要动配置分流**：SDK 只有一个 `config.llm`，S1 抽取与 S2 归纳共用同一模型；给 evolve
   单独配强模型需要 hy-mem 侧做配置分流（见 §七）。
3. **收益仍不可验证**：schema 内容看着对，但对 chat 检索的实际增益没有量化证据。
4. **改动面不小**：`evolve` / `sweep` 命令、ultra 模式引导、L6 层检索展示目前全没暴露。

## 七、若要接，最小路径

| 步 | 内容 | 理由 |
|---|---|---|
| 1 | `hy-mem evolve [--model <name>] [--max-batches N]` | 唯一入口都没有，先补。`--model` 让 S2 用强模型而 S1 仍用 flash |
| 2 | 空转检测：`completion_tokens` 过小（如 < 100）或 `schemas` 没增长时报「本批未产出」，不报成功 | 治「空转记 success」这个坑 |
| 3 | 阈值可调：`MEMORY_S2_CLUSTER_THRESHOLD` / `MIN_CLUSTER_SIZE` 暴露为 config | 给用户一个「我的库太分散」的自救手段 |
| 4 | `search --layer schema` | 归纳出来却检索不到，等于白跑 |
| 5 | `probe_ultra_cluster.py` 转正为 `tests/test_ultra.py`（真模型，缺密钥跳过） | 计入套件 |

模型分流怎么做（待验证）：`LLMProvider(config)` 每次调用新建、读 `config.llm.model`，
而 `HyMemoryClient(config=MemoryConfig)` 接受外部 config 对象 —— evolve 前替换 `config.llm.model`
再构造 client，理论上可行，需实测确认 S2 路径没有提前缓存 provider。

## 八、什么条件下重新评估

- 出现真实需求：「把散落的同类决策归纳成一条通则」——这是 L6 唯一真正解决的问题
- 库规模到千条级且主题集中度提升（单项目占 60%+），让聚类门禁容易满足
- SDK 侧开放按 task 分模型（不必 hy-mem 自己做分流）

## 九、探针位置

- `.local-test/probe_ultra_evolve.py`：6 条分散事实，验证「不聚类就不调 LLM」
- `.local-test/probe_ultra_cluster.py`：30 条同主题，**支持 `L6_MODEL` / `L6_TAG` 环境变量换模型**，
  本文 §三 的对比数据由此产出
- `.local-test/probe_l6_model.py`：验证「素材太分散不聚类」与「素材太像被去重合并」两个边界
- `.local-test/probe_ultra_real.py`：副本向量库未读通，结论作废，留档不改
