# S7 · 零产出要给解释 + MCP 描述补全

> 对应条款：C17、C18。前置：S2（evolve 命令）、S4（MCP 模式感知）。
> 本片不是新功能，是**切到 ultra 后跑真实库才暴露的两个可观测性缺口**。

## 1. 为什么单独成片

S1–S6 都在沙箱里验过（沙箱素材是刻意构造的 30 条同主题事实，必成簇）。
切真实库（`mode=ultra`、`llm.model=tongyi-xiaomi-analysis-flash`、`evolve.model=deepseek-v4.1-flash`）
跑第一次 `hy-mem evolve`，输出是这样的：

```
批次  0 批完成（计划 0）
归纳  处理 1 个簇            ← 其实一个簇都没成
schema  +0 条（现有 0 条）
tokens  输入 0 / 输出 0　耗时 2.0s
```

三处会误导人：

1. `处理 1 个簇` —— `tasks_processed` 是**任务数**不是簇数，而且这里根本没成簇；
2. `+0 条` 之后**没有任何解释** —— 用户分不清「素材不够」还是「功能坏了」还是「模型不行」；
3. 明明库里还有 **32 条未消费事实**，界面上完全看不见这个数字。

根因在 `evolveops.evolve()` 的 note 分支条件写成了
`ran == 0 and not tasks_processed` —— SDK 在「扫描了、判了没簇」时会把
`tasks_processed` 置 1，于是这条分支**永远不触发**，等于最该解释的情况反而静默。

## 2. 改了什么

| # | 改动 | 文件 |
|---|---|---|
| 1 | note 条件放宽为 `batches_completed == 0`，并按 `continuation_reason` + `fresh_facts_remaining` 三支给话 | `core/evolveops.py` |
| 2 | CLI 在 0 批时不再打「处理 N 个簇」，改「未成簇（素材太散或已被判重合并）　no_clusters，剩 32 条未消费」 | `cli.py cmd_evolve` |
| 3 | MCP `evolve` 描述从 300 字扩到 720 字，补四件事：默认用哪个模型 / 耗时 / MCP 是唯一可行路径 / 返回看 `schemas_added` | `mcp/server.py` |
| 4 | SKILL 补「MCP 场景只能走工具，不能走 CLI」段 | `skill/hy-mem/SKILL.md` |
| 5 | `test_mcp` 沙箱固定 `mode=pro`（见下） | `tests/test_mcp.py` |

第 3 条里最关键的是「**唯一可行路径**」这句：ultra 下 MCP 进程长驻持有图库
（kuzu 独占锁），此时 agent 若按直觉建议用户「去终端跑 hy-mem evolve」，
必然撞 `Could not set lock on file`。这句话是给 agent 的，不是给人的。

## 3. 证据

### R1 真实库零产出有解释（C17）

```
$ hy-mem evolve            # 真实库，32 条 fact 未成簇
批次  0 批完成（计划 0）
归纳  未成簇（素材太散或已被判重合并）　no_clusters，剩 32 条未消费
schema  +0 条（现有 0 条）
tokens  输入 0 / 输出 0　耗时 1.2s
提示  有 32 条未消费事实，但一条簇都没成：L6 只归纳「同主题、细节各异、≥4 条且未被判重合并」
      的事实簇。当前素材主题太散（或太像、已被判重合并），所以 S2 连 LLM 都没调（tokens=0）。
      这不是 evolve 坏了、也不是模型不行——继续 add 同一主题的具体细节再跑。
```

### R2 走配置的分模型链路（C1/C5 的回归）

`.local-test/verify_evolve_config.py`：沙箱 `llm.model=弱模型` + `evolve.model=deepseek-v4.1-flash`，
**不传 `--model`**，纯靠 config：

```
{"ok": true, "idle": false, "model": "deepseek-v4.1-flash", "batches_completed": 1,
 "schemas_before": 0, "schemas_after": 3, "schemas_added": 3,
 "completion_tokens": 3214, "continuation_reason": "complete"}
PASS V1 实际用的是 evolve.model（非弱模型）
PASS V2 强模型产出 schema   [+3，现有 3]
PASS V3 跑完 llm.model 未被改写   [tongyi-xiaomi-analysis-flash]
```

### R3 pro/ultra 异步链路对照（排除「ultra 下写入会死」的怀疑）

同一脚本跑两个沙箱，`add --async` → 等 12s → `queue`：

```
[pro]   async queued=1 spawned=True → 完成 1 / 死信 0
[ultra] async queued=1 spawned=True → 完成 1 / 死信 0
```

结论：ultra 不影响写入链路。此前 `test_mcp` 出现的 `dead` 是**并发跑测试 + 后台
worker 被回收**导致的，与 mode 无关。

### R4 test_mcp 的 8 工具断言（顺带修的测试缺陷）

`test_mcp` 从真实 `config.json` 复制配置、只替换 `dataDir`，于是继承了用户真实的
`mode`。切到 ultra 后 `tools/list` 多出 `evolve`，「8 工具齐全」必然失败，并连带
后续 10 项（add 拿不到 memory_id → search/list/get 全挂）。

修法：沙箱显式 `cfg["mode"] = "pro"`。理由——本套只验 MCP 协议全链路，ultra 的
工具集差异由 `test_evolve` C 面覆盖。修完 31/31。

### R5 模式热切换通知（C19，rc.3 打包后追加）

用户问「evolve 能不能动态——只有 ultra 时才把工具交给 MCP」。核查结论：S4 的
`_visible_tools()` 本就每次实时读 config（`cfgmod.load()` 无缓存），缺的是**主动
通知**——模式变了宿主不知道，只能重连。

补齐（`mcp/server.py`）：

1. initialize 的 capabilities 声明 `tools.listChanged=true`
2. serve 主循环每条消息处理前比对当前 mode 与 `_LAST_MODE`，变化则先写
   `notifications/tools/list_changed` 再处理请求

设计取舍：stdio 服务是被动循环，没有文件监听线程，外部 CLI 改 config 后要到
**下一条消息**才被发现——这是「请求驱动的准实时」，不是事件级实时。宿主每轮
对话都会发请求，够用；`tools/call` 的实时校验继续兜底「列表已刷新但还没重拉」
的窗口期。

端到端证据（沙箱，pro 起步）：

```
初始 tools: 8
ping 后收到的通知: ['notifications/tools/list_changed']
切换后 tools: 9 含 evolve: True
切回后 tools: 8 | 通知累计: [...list_changed, ...list_changed]
```

转正：`tests/test_evolve.py` C4 五项（35 → 40）；`McpClient.call` 顺带补了
通知收集（无 id 消息入 `self.notifications`，向后兼容）。
