# 契约：L6 自动沉淀（autoevops）——去掉手动 evolve 工具，改事件驱动

- 日期：2026-10-30
- 父级：无（独立需求，用户拍板设计）
- 性质：L2 功能（M 级）
- 状态：**澄清通过（用户 2026-10-30 20:14 设计指令即拍板），执行中**

## 0. 背景与动机

用户指出：MCP 暴露 evolve 工具让模型手动调，实际没人记得调；SDK 的增量
checkpoint 机制（`s2_reviewed_source_count`，见 2026-10-30 源码核查）天然支持
「素材攒够自动跑、重复跑不重算、无簇零成本退出」。因此把 evolve 从模型可见
工具改为**事件驱动的后台自动沉淀**：MCP 活跃时（add 等调用）顺带检查，满足
条件就触发，不需要定时线程（避免空转压力）。

SDK 三个免费特性支撑此设计（system2_agent.py 实测核查）：
1. 增量 checkpoint：已消费事实不再进素材池，自动跑频繁也不重算
2. `no_clusters` 零成本退出：tokens=0，不调 LLM → 阈值判断可以粗放
3. `max_batches` / continuation：单次跑不完自动留到下次

## 1. 用户设计指令（原文要点 → 条款）

| 用户指令 | 条款 |
|---|---|
| 不需要定时线程，每次 MCP 服务活跃（add 的时候）自动检查，满足条件就开始 L6 沉淀 | R1 |
| MCP 暴露的 evolve 去掉 | R2 |
| 提供查询 evolve 消费情况的工具 | R3（承接上轮讨论） |
| 发现正在有 L6 沉淀，应该立马结束（新的不排队） | R4 |
| kuzu 交给单线程统一入库 | R5（按现状论证简化，见 §2） |

## 2. R5 的论证与简化（重要设计说明）

「单线程统一入库」的完整 actor 化成本高且不必要：MCP 主循环**本来就是逐条
消息串行处理**（JSON-RPC serve 循环），同进程内唯一会与图库并发的只有自动
沉淀线程。沉淀线程使用独立 client（`build_evolve_client()`，同进程第二个
kuzu 连接——实测可行：2026-10-30 用户会话 MCP evolve 成功时主 client 图库
连接同时持有）。因此：

- 沉淀执行 = 一次性 daemon 线程（事件驱动创建，非常驻，无空转压力）
- 同进程并发防护 = `threading.Lock` 非阻塞 try-acquire，拿不到立即放弃（R4）
- 跨进程并发防护 = kuzu 文件锁天然互斥，撞锁异常捕获 → 记 skipped，不报错

## 3. 需求条款

### R1 自动触发
- 挂点：`_tool_add_memory` 处理**完成后**调用 `autoevops.maybe_auto_evolve()`
  （同步、非阻塞、微秒级返回——只读状态文件 + 比对时间戳）。
- 触发条件（全部满足才起线程）：
  1. `config.mode == "ultra"` 且 `evolve.auto.enabled`
  2. 距上次 evolve **尝试**（成功或失败都算）≥ `cooldownHours`（默认 24）×
     连续失败退避系数
  3. 同进程互斥锁 try-acquire 成功
- 线程内执行顺序：数 L2 fact 总数 → 增量（当前总数 − 上次记录总数）≥
  `minNewFacts`（默认 8）才真正跑 evolve → 否则记 skipped_small 并更新时间戳
  （冷却期从尝试时刻起算，防止每条 add 都做全库计数）。
- evolve 调用：`evolveops.evolve(client=build_evolve_client(), language=配置)`，
  归纳语言沿用 `evolve.language`（当前安装已配 zh）。

### R2 移除 MCP evolve 工具
- `ULTRA_TOOLS` 清空（evolve 条目删除），`_HANDLERS` 删 `evolve` 映射，
  `_tool_evolve` 函数删除。ultra 下 tools/list = 8 个工具。
- CLI `hy-mem evolve` 命令**保留**（逃生门：直连模式可用；ultra+MCP 运行时
  撞锁会得到精确错误提示——这是既有行为）。

### R3 evolve_status 工具
- 新增 MCP 工具 `evolve_status`（仅 ultra 可见），无必填参数。
- 返回：最近 20 条沉淀记录（时间 / 触发方式 auto / 结果
  success|skipped_small|skipped_concurrent|skipped_lock|failed / schemas_added /
  增量 / 错误摘要）+ 汇总（总数、最近成功时间、下次可尝试时间、连续失败数）。

### R4 并发防护（立即结束语义）
- 同进程：`threading.Lock(blocking=False)`，拿不到 → 记 `skipped_concurrent`
  → 立即返回（不打断正在跑的沉淀）。
- 跨进程：evolve 抛图库锁异常 → 记 `skipped_lock` → 退避计时照常推进。

### R5 状态落盘与失败退避
- 状态文件：`store.data_dir() / "evolve_jobs.json"`（MEMORY_DATA_DIR 下，
  不入 git）。结构：`{"records": [...], "last_total_facts": int, "consecutive_failures": int}`。
- 退避：连续失败 n 次 → 实际冷却 = cooldownHours × 2^n（上限 7 天）；成功清零。
- 文件损坏/缺失按首次运行处理（记录基线，下次触发再比较）。

## 4. 验收口径

- red→green closure：test_evolve 新增/改造用例先红后绿
- 14 套全绿 + AGENTS.md 矩阵更新
- 打包 rc.6 安装后：ultra tools/list 无 evolve、有 evolve_status；
  add 触发检查日志可见；手动 CLI evolve 仍可用

## 5. 追溯链

| 条款 | 分片 | commit | 产物 | 验收证据 |
|---|---|---|---|---|
| R1 | S1+S2 | 待回填 | autoevops + server 接线 | 待补 |
| R2 | S2 | 待回填 | ULTRA_TOOLS/HANDLERS 改动 | 待补 |
| R3 | S2 | 待回填 | evolve_status 工具 | 待补 |
| R4 | S1 | 待回填 | 互斥锁+撞锁捕获 | 待补 |
| R5 | S1 | 待回填 | evolve_jobs.json + 退避 | 待补 |

## 6. 变更记录

- 2026-10-30 初版（用户设计指令拍板，澄清即通过）。
- 2026-11-02 补强（用户追问跨进程并发）：R5 增加**状态文件原子写**——
  多个 MCP 进程共享同一 `evolve_jobs.json`，两进程同时触发时的
  read-modify-write 交错可能丢记录/JSON 撕裂；实现一律「写临时文件 +
  `os.replace` 原子替换」。跨进程 evolve 并发本身由 kuzu 文件锁兜底
  （第二个进程 build client 即失败 → `skipped_lock`，零副作用），双保险
  无需额外跨进程文件锁。
