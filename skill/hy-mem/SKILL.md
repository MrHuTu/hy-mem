---
name: hy-mem
description: Local long-term memory for agents, backed by Tencent Hy-Memory SDK 2.0.5 with Qwen3.7 cloud embeddings (1024 dims). Use when the user asks you to remember something across sessions, recall past decisions or preferences, record project facts or conventions, or search what you already know about a project/person. Supports async write queue, tag-based archiving, an interactive setup wizard and a health check for library growth. Replaces Hindsight. Commands - help / skill / install / menu / setup / config / doctor / add / search / list / get / delete / forget / finalize / stats / worker / queue / drain / retry / tags / untag / archive. Read `hy-mem help --json` first for the machine-readable command spec.
---

# hy-mem

跨会话长期记忆。默认 `pro` 模式：写入时由 LLM 抽取成事实并**自动打标签**，检索走语义 + 标签过滤。

## 第一步：确认命令真的能跑

智能体「能用上」这个 CLI，需要**两件事同时成立**，缺一件就表现成「技能没生效」：

| 条件 | 症状 | 一条命令解决 |
|---|---|---|
| `hy-mem` 在 PATH 上真实存在 | 每条命令都 command not found | `hy-mem install` |
| 宿主读得到 SKILL.md | 命令能跑，但智能体不知道有这些命令 | `hy-mem skill --install` |

接一个新宿主时按顺序做一次：

```bash
hy-mem install            # 生成启动器（默认落 %LOCALAPPDATA%\hy-mem\bin）
hy-mem install --dir ~/.local/bin   # 或落一个已在 PATH 上的目录，省掉改环境变量
hy-mem --version          # 能打印版本就说明命令通了
hy-mem skill --install    # 把 SKILL.md 铺到检测到的宿主 skill 目录
hy-mem skill              # 复核：每个宿主「已同步 / 有旧版 / 未安装」
hy-mem doctor             # 端到端自检：SDK / 数据目录 / 嵌入端点 / LLM / 队列
```

`install` 一次写两个同名入口：`hy-mem.cmd`（cmd / 原生 shell）与 `hy-mem`（Git Bash / MSYS，无扩展名）。只写 .cmd 的话，bash 里的 `which hy-mem` 是查不到的——这正是「技能没生效」的一个常见来源。

`install` 不会改你的 PATH，只把该加的那条命令打出来；想一步到位就 `--dir` 指到已经在 PATH 上的用户目录（如 `~/.local/bin`）。不想动 PATH 就直接用绝对路径：产物模式是 `<安装目录>\hy-mem.exe`，源码模式是 `<安装目录>\hy-mem.cmd`。

### 升级：解压即用，配置与记忆自动继承

分发包（zip）**不含** `config.json` 与 `data/`——这是有意设计，解压覆盖旧目录永远不会碰用户配置和数据。升级两种姿势：

| 场景 | 行为 |
|---|---|
| **原地升级**（新 zip 解压覆盖旧目录） | `config.json`、`data/` 根本不在包里，原样保留，什么都不用做 |
| **换目录升级**（解压到新目录） | 首次运行任意命令时自动探测：PATH 上若还指向旧安装（有 `config.json`），就沿用其配置，`storage.dataDir` 改指旧数据目录——**记忆原位复用，不拷贝** |

换目录升级时 stderr 会打三行说明（检测到旧安装 / 配置写到哪 / 数据目录在哪）。接管只在「当前无 config.json」时发生一次，之后不再探测。两个退出开关：`HY_MEM_NO_ADOPT=1`（禁用自动接管，走全新向导）、`HY_MEM_CONFIG=<文件>`（本就自管配置，接管自动让位）。接管完成后旧安装目录可整个删掉——数据已通过 `dataDir` 引用，但删除前先把旧 `data/` 挪到新位置并改一次 `hy-mem config set storage.dataDir=<新路径>`。

命令跑不起来时按这个顺序查：`hy-mem --version`（找不到 → 走 install）→ `hy-mem skill`（宿主没看到帮助 → 走 skill --install）→ `hy-mem doctor`（配置问题）→ `hy-mem tui-test`（只影响人在终端里的按键）。

## 何时用

| 场景 | 命令 |
|---|---|
| 用户说"记一下"、表达了偏好/决策/项目事实 | `add` |
| 连续记多条、不想等 LLM（pro 单条可到 34s） | `add --async` + `worker` |
| 回答涉及用户历史偏好、项目上下文，且上下文里没有 | 先 `search` 再答 |
| 只想在某一类知识里找（如全部 infra 事实） | `search --tag <tag>` |
| 看知识库有哪些分类、各自多少条 | `tags` / `archive` |
| 不确定环境是否正常、报错排查 | `doctor` |
| 用户要查/删已记的内容 | `list` / `get` / `delete` |
| 会话收尾、需要沉淀演化 | `finalize` |

**不要**记：临时路径、typo 修复、中间态日志、大段原文摘抄。

## 先读规格，不要猜参数

```bash
hy-mem help --json            # 等价于 help --json：全部命令、参数类型、默认值、返回字段、示例
hy-mem --json help search     # 只看一条命令
hy-mem help                   # 人类可读总览
```

规格里 `commands[].args` / `options` 是**从 argparse 反射生成**的（类型、默认值、choices、是否可重复都与实现一致，不会漂移）；`when` / `examples` / `output` / `notes` 是手写语义，不确定怎么调时**优先照抄 `examples`**。

规格顶层还有 `hard_rules`（SDK 硬约束摘要）、`workflows`（记住一件事 / 回忆 / 归档 / 排障四条完整链路）、`env`（全部环境变量）、`exit_codes`。

## 调用方式

```bash
hy-mem <子命令> [参数]        # 已跑过 hy-mem install 且在 PATH 上
D:/tools_open/agent-bridge/dist/hy-mem/hy-mem.exe <子命令>   # 或用产物绝对路径
```

无参数且非 TTY 时只打印帮助，不会卡住等输入；`menu` / `setup` 在非 TTY 下同样不会阻塞。

多行文本**一律走 stdin**，不要当参数传——`hy-mem.cmd` 启动器经 cmd.exe，会把多行参数压平成一行。

```bash
cat facts.txt | hy-mem add --split
```

## 通用参数（写在子命令前）

| 参数 | 含义 | 默认 |
|---|---|---|
| `--json` | 输出单行 JSON，便于解析 | 关 |
| `--mode lite\|pro\|ultra` | 覆盖 MEMORY_MODE | pro |
| `-u/--user` | user_id（记忆隔离键） | **不用传**：`-u` > `HY_MEM_USER_ID` > `config.identity.userId` > **通用（default）** |
| `-a/--agent` | agent_id | 不设（由 SDK 用 `default_agent`，另设会换掉隔离键，旧记忆读不到） |
| `-s/--session` | session_id | 不设 |
| `--verbose` | 显示 SDK 日志 | 关（默认静默） |
| `--quiet` | 成功时不打印任何内容，只看退出码 | 关 |
| `--fields a,b` | 只保留这些顶层字段（省 token） | 不裁剪 |
| `--no-color` | 关彩色（等价 `NO_COLOR=1`） | 关 |
| `--config <path>` | 指定配置文件（等价 `HY_MEM_CONFIG`） | 安装根下的 config.json |

## 身份与多智能体（user_id）

`user_id` 不是「用户账号」，而是**记忆的隔离键**。它决定「谁和谁看到同一份记忆」：

| 情况 | 内部值 | 效果 |
|---|---|---|
| 没设（默认） | `default` | **通用**：所有智能体共享同一份记忆，互相能读到对方写的东西 |
| 设了某个名字 | 例如 `writer` | **隔离**：只有带这个名字的调用能看到，与其它名字互不可见 |

```bash
hy-mem --json user                 # 当前身份 + 来源（env / config / general）
hy-mem user set writer             # 只改身份：不动模型、不动数据目录、不动已有记忆
hy-mem user reset                  # 回到通用，和别的智能体共享
hy-mem --json user list            # 见过哪些身份（含当前）
hy-mem -u writer search "..."      # 临时只影响这一条命令
```

要点：

- **切换不删数据**。`writer` 下写过的东西切走后只是看不到，切回来还在。
- **只改一个字段**。`user set` 只写 `config.json → identity.userId`，模型配置、数据目录、已有索引全不动 —— 所以「换个身份试试」是零成本操作。
- **优先级**：`-u` > `HY_MEM_USER_ID`（进程级临时覆盖，适合一个 shell 里专门服务某个智能体）> 配置文件 > 通用。
- 智能体**默认不该传 `-u`**：传一个随机的 user_id 等于另开一份空记忆，会让人以为「记忆丢了」。要隔离时由人来 `user set`，而不是每条命令自己编一个。

## 配置不全时会怎样

配置缺项时命令**不会去调 SDK**，而是当场拦下并说清缺什么、怎么补：

```
✗ 配置还没配好，暂不执行：检索记忆

  缺  embedding.apiKey  向量模型密钥未设置
  缺  llm.apiKey        LLM 密钥未设置（配置文件与环境变量都没有）

怎么补（任选其一，改完立即生效）：
  1. hy-mem setup --step embedding   或   hy-mem config set embedding.apiKey=sk-xxx
  2. hy-mem setup --step llm   或   set YSJX_API_KEY=<key>
```

- 退出码 **1**，`--json` 时 `error.code = "E_CONFIG"`，`error.missing[]` 给出每项的 `{item, what, fix}`。
- 这一条是为 agent 写的：看到 `E_CONFIG` 就**停止重试**，把修复命令转告用户，不要反复调用把配额打光。
- 拦住而不是「先跑跑看」：配置错时报的是 SDK 内部的 401 堆栈，或者更糟 —— 检索静默返回空列表，被误读成「没有相关记忆」。

### 密钥只能是 ASCII，且「没发出」与「HTTP 错误」严格分开

API Key 最终进 HTTP 头 `Authorization: Bearer <key>`，而 `http.client` 用 **latin-1** 编码头。2026-10-27 的真实故障：用户把中文粘进密钥框（回显 `已读取 5 个字符`），发请求那一刻抛 `UnicodeEncodeError: 'latin-1' codec can't encode characters in position 7-8`（`Bearer ` 正好占 7 位），界面显示成 `HTTP 0` —— 一个「输入内容不对」的问题，长得像网络故障，用户只能反复查网络、换模型。

**0.9.5 起只有两种结果，没有第三种**（全部走 `cli/netprobe.py`）：

| 结果 | 含义 | 文案形态 | agent 该做什么 |
|---|---|---|---|
| `Sent(status)` | 请求**发出去了**，服务器给了码（必然 ≥100） | `HTTP 401: Invalid API-key …｜密钥被拒绝：无效 / 过期 / 填错了别家的 key` | 按码处理：401 换 key、404 改路径、429 退避后重试 |
| `NotSent(cause)` | **请求没离开本机** | `请求没有发出：向量模型密钥 第 1 位是「没」（非 ASCII 字符）…` | 改配置或查网络，**不要盲目重试** |

- `status = 0` 这个约定被**彻底删除**：`Sent.__post_init__` 拒绝 `< 100` 的值，想构造一个「状态码 0 的成功结果」会直接抛 `ValueError`。
- 发送前 `precheck()` 先查 key 与 URL 的字符集（复用 `config.secret_charset_problem`，只一处判定），**不等异常发生**。`NotSent.cause` 取值：`precheck / encoding / dns / proxy / refused / tls / timeout / network / other`。
- 看到 `请求没有发出` **先去查 `cause`**：`precheck` / `encoding` 是输入内容问题（key、baseUrl 里的中文/全角/零宽），`dns` / `proxy` / `refused` / `tls` / `timeout` 才是网络。
- `scripts/test_setup.py` 有源码守卫：除 `netprobe.py` 外，`cli/` 下不许出现 `urlopen(` / `import urllib.request` / `requests` / `httpx`，也不许再写 `status == 0`。新加调用点只能复用 `netprobe.post_json()`。

## 写入

```bash
# 单条（一条只放一个主题，见"粒度"）
hy-mem add "招商活动报名审批流用自研 FSM，fireAndPersist 落库并写 system_flow_log"

# 多主题拆条（推荐）
hy-mem add --split < facts.txt

# 带标签写入（tag 可重复；标签会绑到"本次真正新建"的节点上）
hy-mem add --tag infra --tag k8s "生产集群 12 个节点，etcd 独立部署"

# 异步：只入队立即返回（~0.45s），不阻塞对话
hy-mem add --async --tag infra "消息队列用 RocketMQ，4.9.4"
```

## 异步队列

**为什么要它**：pro 模式单条 `add` 实测 3.4~34s（LLM 抽取 + reconcile），对话里连续记几条会把交互卡死。队列把写入和等待解耦。

```
add --async  →  写 SQLite 队列（立即返回 job_id）
                ↳ 队列无人消费时**自动拉起后台 worker**（空闲 10 分钟自退出，无黑窗）
worker       →  单写者消费，串行落库，失败重试，超次数进死信
```

**0.9.6 起 `add --async` 不再需要手动跑 worker**：入队时检测心跳，没有活着的 worker 就自动拉起一个临时的。`--json` 输出里 `workerSpawned: true` 表示已拉起、`workerLive: true` 表示已有 worker 在跑。手动跑 worker 仍然可用（调试/补漏）。

```bash
# 消费队列：清空即退出
hy-mem worker --once

# 常驻：空闲 60s 自动退出（适合桌面端按需拉起）
hy-mem worker --idle-exit 60

# 只处理 3 个任务后退出（限流）
hy-mem worker --max 3

# 查看队列 / 等清空 / 重投死信
hy-mem queue
hy-mem queue --status dead
hy-mem drain --timeout 300
hy-mem retry 12          # 单个 job 重投
hy-mem retry dead        # 全部死信重投
```

规则：

- **同一时刻只应有一个 worker**。多 worker 不会重复消费（原子 `UPDATE ... WHERE status='pending'` 抢占），但会让写入排队无意义地变长。
- worker 崩溃留下的 `running` 任务，下次启动自动回收（`HY_MEM_STALE_SEC`，默认 900s）。
- 队列库 = `MEMORY_DATA_DIR/hy_mem_queue.db`，与记忆库同一个 data 目录，备份一起拷。

### 入队 ≠ 入库（最容易被误解的一条）

`add --async` 只是把任务写进队列就返回，**这一刻库里还没有这条记忆**，`list` / `search` 当然查不到。没有 worker 在跑，它就一直躺在 `pending`。

判断方法：

```bash
hy-mem queue            # 待处理 N > 0 就是还没落库
hy-mem worker --once    # 消费掉（pro + 推理模型每条约 10~50s）
```

现在 `search` 空手时、`list` 结尾，都会主动提示「队列里还有 N 条没落库」——`search --json` 也会带上 `pending_jobs` 与 `hint` 字段。看到这两个字段，别再回答「没有相关记忆」，那是错的。

进程被 Ctrl+C 打断时（任务 `error=interrupted`、`attempts` +1），任务会回到 `pending` 等重试，但**必须有 worker 再跑一次**才会继续。菜单里写完异步条目后会直接问「现在就跑一次 worker 落库吗」，默认是。

**给 agent 的建议**：批量沉淀用 `--async` 即可（0.9.6 起自动拉起 worker 落库，无需手动消费）。要求「写完立刻能查到」的场景，就别用 `--async`，直接同步 `add`。

## MCP 接入（原生工具，优先于命令行）

宿主支持 MCP 时（WorkBuddy / CodeBuddy 的 `mcp.json`），优先配 stdio 服务，智能体获得结构化工具而非文本约定：

```json
{
  "mcpServers": {
    "hy-mem": {
      "command": "hy-mem",
      "args": ["mcp"]
    }
  }
}
```

- 协议：JSON-RPC 2.0 换行分隔（MCP stdio 标准），宿主按会话拉起、随会话退出，不常驻。
- 工具：`add_memory`（默认异步毫秒返回）/ `search_memory` / `list_memories` / `get_memory` / `delete_memory`（须 `confirm: true`）。
- 身份：MCP 会话固定用默认身份（config 的 identity.userId），与 CLI 不传 `-u` 时同口径。
- 命令行与 MCP 可共存：同一套 queue.db + WriteLock + 向量库。

## 标签与归档

`pro` 模式写入时 LLM 会**自动打标签**（实测 `['work','iisw_be','branch-convention']`、`['git','windows','tools']`）。手工 `--tag` 叠加在同一套索引上，两者在检索结果里合并显示。

```bash
# 标签清单：合计 / 手工 / 自动 三列
hy-mem tags

# 从已有记忆补齐 LLM 自动标签，并清理指向已删记忆的孤儿绑定
hy-mem tags --refresh

# 按标签检索（AND：全部命中；--tag-mode any 为 OR）
hy-mem search "监控怎么配" --tag infra
hy-mem search "配置" --tag infra --tag apollo --tag-mode any

# 按标签列记忆
hy-mem list --tag apollo -n 20

# 归档分布
hy-mem archive --by tag      # 标签分布（带条形图）
hy-mem archive --by layer    # 记忆层分布，如 l2_fact / l6_schema

# 解绑
hy-mem untag <memory_id> --tag infra
hy-mem untag --tag infra --yes    # 全局解绑某标签（需 --yes）
```

**标签过滤是先召回后筛**：SDK 的 `search()` 没有 tag 参数，CLI 会按 `limit × 10`（上限 200）多取候选再筛，因此 `-n` 给多少就还你多少条。标签特别稀疏时可能筛空——此时放宽 `--min-score 0` 或调大 `-n`。

## 设置与配置（给人用，不是给 agent 用）

`config.json`（默认在安装根，`D:\tools_open\agent-bridge\config.json`）是设置的**单一真源**，优先级 **命令行 > config.json > .env**。向导里改过的值会覆盖 .env 同名项。

```bash
hy-mem                 # 无参数：首次进设置向导，之后进交互主界面
hy-mem setup           # 完整四步向导（向量模型 / 运行模式 / LLM / 存储目录）
hy-mem setup --step llm        # 只改 LLM
hy-mem setup --step storage    # 只改存储目录
hy-mem setup --step embedding  # 只改向量模型（会实测维度并提示重建）
hy-mem menu            # 交互主界面：写/搜/列/标签/队列/健康/设置/自检

hy-mem config show     # 当前配置（密钥打码）
hy-mem config path     # 配置文件位置
hy-mem config validate # 静态校验，不发网络请求
hy-mem config set mode=lite              # 点路径修改
hy-mem config set search.minScore=0.3
hy-mem config set --yes embedding.model=text-embedding-v4   # 改嵌入模型必须 --yes
hy-mem config reset --yes                # 删除配置（含密钥）
```

首次运行的判定不看「文件是否存在」，而看配置是否完整（`setupComplete` 且关键项齐全）。所以即使产物里残留一份空配置，也会正常进引导（引导页可选「先跳过」，跳过会记 `setupSkipped`，之后不再反复劝）。顺序固定：

| 步骤 | 内容 | 说明 |
|---|---|---|
| 1 | 向量模型 | 真发一次 embedding 请求，维度以实测为准 |
| 2 | 运行模式 MEMORY_MODE | lite / pro / ultra |
| 3 | LLM 模型 | **仅 pro / ultra 才问**；lite 自动跳过 |
| 4 | 存储目录 | 显示体积与剩余空间；换目录可选迁移 |

### 交互按键（menu / setup 通用）

| 按键 | 行为 |
|---|---|
| ↑ / ↓ 或 k / j | 上下移动 |
| Home / End 或 g / G | 跳到首 / 尾项 |
| PgUp / PgDn | 翻 5 项 |
| Enter / Space / → | 选中 |
| Esc / ← / q | 返回上一层（主界面即退出） |
| 1~9 | 直达第 n 项 |
| **`[字母]` 热键** | 每项前面方括号里的字母，按下即选中该项（如 `w` 写记忆、`r` 检索、`s` 设置）；标签同时给出英文名，便于记忆与脚本对照 |
| Ctrl+C | 中断，不写入任何改动 |
| 输入框：← → / Home / End | 移动光标 |
| 输入框：退格 / Delete | 删字符 |
| 输入框：Ctrl+U / Ctrl+W | 清空整行 / 删一个词 |
| 鼠标拖选 / 右键复制 | 用终端自己的选择与复制（**默认可用**）；复制请用右键或 `Ctrl+Shift+C`，`Ctrl+C` 是中断 |
| 鼠标点选 | 仅 `HY_MEM_MOUSE=1` 时启用（会抢占终端原生选择，不建议常开） |

输入（含粘贴密钥、中文）与选择器走同一套按键读取，不混用控制台行缓冲——这一点很关键：混用会让 DSR 应答、鼠标事件串进输入框，表现成「打了字却没生效」。密钥输入行做**水平单行裁剪**（只显示光标附近一段），长 key 不会把提示行撑成两行，因此重绘不会留下残影。

**粘贴/复制的约定**：粘贴直接 `Ctrl+V` 或右键；复制用鼠标拖选后右键（Windows Terminal 也可 `Ctrl+Shift+C`）。默认不开鼠标上报，就是为了让这两个动作保持终端原生行为 —— 开着上报时拖选会被当成点击，选中项乱跳、面板反复重绘。

按键不可用（老终端 / stdin 不是真控制台 / `HY_MEM_TUI=0`）自动降级为编号菜单，仍可喂数字或英文热键。

方向键没反应时，先在同一个终端里跑 `hy-mem tui-test`：它会打印「按键后端 / 是否 TTY」并逐键显示解码结果（应看到 `up` / `down`，而不是 `char:x`），据此判断是终端问题还是程序问题。


### 设置中心（主界面「设置」）

| 项 | 说明 |
|---|---|
| 身份 user_id | 通用（共享）/ 独立名字（隔离）；切换只改身份，不动模型与数据 |
| 向量模型（嵌入） | 换模型会实测维度；维度或语义空间变化时提示「备份并重建数据目录」 |
| 运行模式 MEMORY_MODE | 改后已有记忆不受影响 |
| LLM 模型 | pro 及以上用于抽取事实与自动打标签 |
| 数据存储目录 | 用量/剩余空间 → 迁移 / 恢复默认 / 备份 / 清空重建 / 清理队列库 |
| 检索阈值 minScore | 0.30 宽松 / 0.35 平衡 / 0.45 严格 / 手动 |
| 队列参数 | 失败重试次数、单任务锁超时 |
| 完整向导 / 当前配置 / doctor | 重走四步、只读查看、端到端自检 |

**存储目录的取舍**：迁移成功后**不删源目录**（长期记忆的删除不可逆，多留一份副本只是空间代价）；「清空重建」会先自动备份成 `data.bak-时间戳` 再清空。队列库（`hy_mem_queue.db`）与记忆库同在 data 目录，备份一起拷。

**改嵌入模型是危险操作**：维度不同 → 旧向量完全不可读；维度相同但模型不同 → 语义空间不同，混用会让检索失真。`config set` 会拦一道要求 `--yes`；`setup` 会主动问是否「备份并重建数据目录」。重建即把 `data` 拷成 `data.bak-<时间戳>` 后清空。

向导的两个设计要点（重跑时别意外覆盖）：

- **维度不手填，实测得出**：向导会真发一次 embedding 请求，用返回向量长度作为 `dims`，与预设不一致时以实测为准。
- **首次运行从 .env 回填**：如果只配了 .env 没配 config.json，向导会把现有值读成默认项（显示「← 当前」），不用重填。

agent 调用时**不要**用 `menu` / `setup`——它们会等待输入（非 TTY 下退化为编号模式，仍然读 stdin）。脚本里一律用具体子命令。

## 库体积策略

pro 的 reconcile 是 O(存量)：写一条新事实会把十几条旧节点标记 `update` 并重写。所以「库多大」直接决定「写一条要多久」，必须定期看：

```bash
hy-mem stats --health        # 条数 / 体积 / 延迟预期 / 归档建议
hy-mem --json stats --health  # 同上，机器可解析
hy-mem stats --health --vacuum          # 顺带清理已完成 job 记录并 VACUUM
```

输出四项指标 + 建议，结论分 `OK / WARN / CRIT`：

| 指标 | 默认阈值 | 超了意味着 |
|---|---|---|
| 记忆条数 | 200 警告 / 500 危险 | reconcile 池变大，写入持续变慢 |
| 数据体积 | 1024MB 警告 / 2048MB 危险 | 需要归档或清理 |
| 单条写入预期 | 30s 警告 / 60s 危险 | 必须改 `--async` |
| 异步队列 | 有死信即危险 | 查 `queue --status dead` |

延迟预期是**经验模型**（`max(2.5, -10.9 + 1.95×条数)`，两点标定，误差约 ±20%），只用来判断量级，不要当 SLA。阈值可用 `HY_MEM_HEALTH_*` 覆盖。

到达 WARN 后的处理顺序：`archive --by tag` 看分布 → `untag` 或 `delete` 收缩低频标签 → 必要时 `stats --health --vacuum`。

## 检索 / 读取

```bash
# 语义检索（--min-score 默认 0.35；0 表示不过滤，用于排查召回）
hy-mem search "审批流怎么落库" -n 5      # 不用传 -u，身份取 config.identity.userId

# 已抽取的记忆（l2/l3 层，lite 模式下为空）
hy-mem list -n 20

hy-mem get <memory_id>
hy-mem delete <memory_id> --dry-run   # 先看一眼要删什么
hy-mem delete <memory_id>
hy-mem forget --dry-run              # 批量删之前先看影响条数
hy-mem -s s1 forget --yes            # 按范围清空（必须 --user + --yes）
hy-mem -s s1 finalize
hy-mem doctor
hy-mem --json stats
```

## 输出契约

- `--json`：单行 JSON。检索返回 `{"count":n,"min_score":x,"tags_filter":[...],"results":[...]}`，条目含 `memory_id` / `content` / `score` / `layer` / `tags`（含合并后的 `_tags`）。
- `add --json` 返回 `memory_id`（l1_raw 事件 id）、`memory_ids`（本次影响到的节点）、`tagged_ids`（真正打了标签的节点）。
- exit code：`0` 成功，`1` 用法或环境错，`2` 运行时错（可修可重试），`3` 内部错误（程序缺陷，别重试），`130` 被 Ctrl+C 中断。
- **全局选项位置无关**：`hy-mem search "x" --json` 与 `hy-mem --json search "x"` 等价（入口会先把 `--json/--quiet/--config/--fields/--mode/-u` 搬到前面），命令行怎么排都行。要传字面量 `--json` 当文本，用 `--` 隔开。
- 失败时 stderr 输出 `hy-mem: <原因>`；`--json` 时 stdout 还会给一份 `{"ok":false,"error":{"code","message","hint"}}`，**优先读 stdout 那份**。
- `--quiet` 只看退出码；`--fields a,b` 裁剪顶层字段省 token。

### 检索返回 0 条，怎么判断是「没记过」还是「坏了」

`count: 0` 有两种完全不同的原因，别混：

| 现象 | 含义 | 怎么办 |
|---|---|---|
| `count: 0`，exit 0 | 真的没有相似记忆 | 换个更接近原话的问法，或 `--min-score 0.2` 放宽 |
| 非 0 退出 + `嵌入端点在报错` | 嵌入密钥失效/端点不通（**SDK 会静默返回空列表**，所以 CLI 空手时会自动补一次真实探测） | `hy-mem setup --step embedding` 换 key；再用 `doctor` 复核 |
| 有数据但 `list` 也空 | 身份不对（写到了别的 user_id） | `hy-mem config` 看 `identity`，或临时 `-u <那个 id>` |

同理：`add` 之后 `search` 立刻查不到，先跑 `doctor`，别急着怀疑阈值。

## 模式与延迟（实测）

| 模式 | LLM | 每次 add | 写入层 |
|---|---|---|---|
| `lite` | 否 | ~0.3~1s | `l1_raw`（**不打标签**） |
| **`pro`（默认）** | 是 | **3.4~34s，波动大** | `l2_fact` + 自动 tags |
| `add --async` | — | **~0.45s** 入队即返回 | 由 worker 落库 |
| `ultra` | 是 | 更慢 | `l2` + `l6_schema` |

pro 慢是结构性的，且**会随库变大而恶化**：写一条新事实时 reconcile 会把十几条已有记忆标成 `update` 并重写。实测单写一句话 43.5s，其中 `extract` 4126 tokens、`reconcile` 7061 tokens、`ops_summary={"add":1,"update":18}`。

三个已知缓解手段：

1. **批量一律走 `--async`**（入队 0.45s），不要连续同步 `add`。
2. **控制库体积**：`archive` 看清分布，过期的用 `forget` 清掉；否则 reconcile 池只会越来越大。
3. 高频低价值笔记用 `--mode lite`（但 lite 不带标签、也不进 `list`）。

**已排除的捷径**：`lite` 快写 + `finalize` 批量整合 **不可行**——本配置下 `finalize` 是 no-op（实测 2s，l2_fact 前后都是 21 条，没有新增）。reconcile 也没有任何 `ENABLED` 开关可关。

## 粒度：这条最容易踩

**一条 add 只放一个主题。** 一次 add 的内容会被压成**一条** fact；偏好、项目约定、技术细节混在一条里，向量被拉平，单独问其中一点时召回骤降。

| 写法 | 得分 |
|---|---|
| 偏好 + FSM 细节 + git 规矩 混在一条 | 0.341（**低于阈值，查不到**） |
| 拆成三条独立写 | 0.472 / 0.641（正常命中） |

多主题用 `--split`，或分多次 `add`。

## 推理模型（reasoning）的两个坑

`ysjx` / `deepseek-v4` / `kimi-k2.5` / Claude 系在回答前先写思维链：正文在 `content`，思考过程单独放在 `reasoning_content`。由此有两个反直觉现象：

1. **`max_tokens` 给小了，`content` 恒为空串**。实测同一句话：`max_tokens=8` → `finish_reason=length`、`content=''`、`reasoning_content='The user has sent a very brief message'`；`max_tokens=300` → 正常返回 `ok`。所以任何探活都不能用 8/16 这种配额 —— 曾因此让 `doctor` 对一个完全可用的模型报「环境不可用」。现在 `doctor` 与设置向导统一用 512，并接受「content 为空但 `reasoning_content` 有内容」判为可用。
2. **抽取配额默认够用但不宽裕**。SDK 的 `agent_max_tokens` 默认 2000（`MEMORY_AGENT_MAX_TOKENS` 可调），思考会吃掉一部分。若写入报 `EMPTY_RESPONSE` 且日志里 `completion_tokens == max_tokens`，就是这个原因，调到 3000~4000。

代价是慢：一次 `add` 的 extract / reconcile 各要 4~6s。批量写一律走 `--async`。

## 嵌入后端

| `MEMORY_EMBEDDER_PROVIDER` | 后端 | 维度 | 延迟 |
|---|---|---|---|
| `openai`（当前） | 百炼 MaaS `/compatible-mode/v1` + `qwen3.7-text-embedding-flash` | 1024 | ~300ms |
| `local` | fastembed + `BAAI/bge-small-zh-v1.5` | 512 | 216ms |

MaaS 的 `/api/v1/embeddings` 返回 **404**，OpenAI 兼容入口必须写 `/compatible-mode/v1`；`/api/v1` 只用于 `/models` 列举。

`provider` 缺省时取 config.json 的值（默认 `openai`）—— **不要**把它当 `local`，否则全新安装的第一次 `doctor` 就会去下载 92MB 的 ONNX 模型。`local` 的模型只在真正用到时下载，缓存放在 `<数据目录>/models`（随备份/迁移一起走），下载默认走 `HF_ENDPOINT=https://hf-mirror.com` 国内镜像；`doctor` 发现模型未缓存时只报告、不下载。

## 硬约束（改代码前先读）

1. **`memory_ids` ≠ 新建节点**。一次 `add` 的 `memory_ids` 是"本次影响到的节点"，写完一句 CI 配置实测返回 **8~15 个 id**（含被 reconcile 合并的老节点）。直接全部打标签会把标签污染到无关记忆上。CLI 用「写入前快照 → 求差集」取真正新建的节点，失败时退化为第一个 id。
2. **`list_memories` 没有 `session_id` 参数**（只有复数 `session_ids`）。传错会 `TypeError`，而这个异常在快照函数里被吞掉的话，差分就静默失效 → 标签又会被铺满。所以快照只按 `user_id` 取全量。
3. **SDK 内部没有任何文件锁**（`filelock`/`flock`/`fcntl` 全无引用），而 reconcile 是「读旧节点 → 比较 → 覆盖写」。多进程并发写会丢更新。所有写操作（`add`/`delete`/`forget`/`finalize`/worker 每个 job）都必须包在 `store.WriteLock()` 里。
4. **`store.py` 必须在 `load_env()` 之后调用**。它靠 `MEMORY_DATA_DIR` 定位队列库，没装载 .env 就会落到 `~/.hy_memory/`，与主库分家且不易察觉。
5. **维度必须与模型一致**：现为 1024（云端 flash）。改模型 = 改 `MEMORY_EMBEDDING_DIMS` + 清空 `data` 目录重建，否则抛 `EmbeddingDimensionMismatchError`。
6. **`min_score` 别用 SDK 默认的 0.4**：flash 的分数带更窄（噪声 0.28 / 近邻干扰 0.33 / 合法改写 0.355~0.372 / 直命中 0.5~0.66），0.4 会把改写问法全漏掉。CLI 已改为 0.35，可用 `HY_MEM_MIN_SCORE` 覆盖。
7. **`search` 用复数形参**（`user_ids`/`agent_ids`/`session_ids`），`add` 用单数。写错 `TypeError`。
8. **`search` 返回嵌套**：`memories.{normal,proactive,schema}`，需拍平再按 `score` 排序。
9. **`list_memories` 只列抽取层**（l2+）；`lite` 模式写入在 `l1_raw`，列表恒为空 → CLI 自动回退 `get_recent_history`。**这也是 lite 模式打标签不可靠的原因**（节点不在 vdb 列表里，快照取不到）。
10. **日志必须压到 ERROR 以下**：`hy_memory` logger 自带 handler 且 `propagate=False`，`logging.disable(logging.INFO)` 压不住——reconcile 会把 "BM25_ALL_ZERO" 这类正常统计打成 ERROR 混进输出。CLI 用 `logging.disable(logging.ERROR)`。
11. `MEMORY_PIPELINE_TRACE_ENABLED=false` 必须保留，否则进程退出时报 `Task was destroyed but it is pending`。
12. **`config.json` 存在时会覆盖 .env**。改了 .env 却不生效，先看 `hy-mem config show`——很可能 config.json 里有旧值压着。反之删掉 config.json 就能退回纯 .env 模式。
13. **深拷贝陷阱**：`dict(cfg)` 只复制顶层，改 `merged["embedding"]` 会连带改掉原对象，导致「变更检测」自己和自己比、永远判不出变化。所有配置比对前必须 `copy.deepcopy`。
14. **向导的输入校验不能省**：`ask()` 对一切非空输入原样返回。开发期用管道喂输入时曾错位一位，把确认答案 `n` 当成存储目录写进了配置。路径这类「写错就毁掉一切」的项必须 `_looks_like_path()` 校验 + 尝试 mkdir。

存储目录 `MEMORY_DATA_DIR`，默认 `D:\tools_open\agent-bridge\data`。备份就拷这个目录（含 `hy_mem_queue.db`）。

## 文件结构

```
D://tools_open//agent-bridge//
├── AGENTS.md             # 开发者交接说明（目录 / 架构 / 构建 SOP / 排障）
├── cli\hy_mem_cli.py     # 24 个子命令（含 help / skill / install / menu / setup / config / user）
├── cli\helptext.py       # 命令规格（argparse 反射 + 手写语义），help --json 的数据源
├── cli\config.py         # config.json 单一真源 + env 映射 + 校验 + 身份默认值
├── cli\netprobe.py       # 出站 HTTP 唯一出口：发送前预检字符集 + Sent/NotSent（无 status=0）
├── cli\setup_wizard.py   # 四步向导 + 单步入口（实测维度 / 真调 LLM / 重建数据目录）
├── cli\tui.py            # 终端 UI（方向键 + 鼠标 + 行编辑，零依赖）
├── cli\menu.py           # 交互主界面 + 设置中心
├── cli\runtime_check.py  # 运行环境自检（系统版本 / 位数 / 包完整性 / 可写 / 运行库 / 磁盘）
├── cli\storage_ops.py    # 数据目录运维：用量 / 可用空间 / 迁移 / 备份 / 清空
├── cli\install_ops.py    # 生成 PATH 上的启动器（hy-mem install）
├── cli\skill_ops.py      # SKILL.md 定位 / 一致性 / 分发（hy-mem skill）
├── cli\presets.py        # 向量模型 / LLM / 模式预设（数字均实测）
├── cli\store.py          # SQLite 队列 + 标签索引 + 跨进程写锁
├── cli\local_embedder.py # 本地 ONNX 嵌入（零 key 备选）
├── config.json           # 设置（首次运行向导生成；含密钥，权限已收紧）
├── skill\hy-mem\SKILL.md # 本文件（打包时会复制进产物 skill\hy-mem\）
├── hy-mem.cmd            # 启动器（源码模式）
├── scripts\build_exe.py  # 打包（消毒 + SKILL.md + 启动器/说明 + 压缩包 + 清历史）
├── scripts\test_cli.py   # 接口契约自测：退出码 / 输出分流 / 全局选项位置
├── scripts\test_ui.py    # 按键 / 鼠标 / 行编辑自测（无需真终端）
├── scripts\test_setup.py # 向导 / 存储运维 / 运行环境自检 自测（不发网络请求）
├── dist\hy-mem\          # 产物目录（351MB，含 _internal\，整个拷走）
├── dist\hy-mem-*-win64.zip # 对外分发物（152MB，解压即完整目录）
└── data\                 # 记忆库 + hy_mem_queue.db，整体备份
```

产物目录里带 `skill\hy-mem\SKILL.md`，所以 `hy-mem skill --install` 在别人的机器上也能把这套说明铺进宿主。

## 自包含分发（打包成 exe）

```bash
.venv/Scripts/python.exe scripts/build_exe.py     # 约 2 分钟，产物目录 351MB / 压缩包 152MB
dist\hy-mem\hy-mem.exe --version                  # 拷走即可用，不需要 Python
```

`--onedir` 而不是 `--onefile`：onefile 每次启动要解包 290MB，冷启不可接受。

### 分发的是「目录 / 压缩包」，不是单个 exe

**`hy-mem.exe` 只是引导器，真正的 Python 在 `_internal\` 里。** 只把 exe 拷给别人，双击必然闪退——实测报错：

```
[PYI-21264:ERROR] Failed to load Python DLL '...\_internal\python312.dll'.
```

这条消息在双击场景下根本来不及看见（窗口立刻关闭），用户只会说「打不开」。所以：

| 产物 | 用途 |
|---|---|
| `dist\hy-mem\` | 目录版，拷走即用（自带 `_internal\`） |
| `dist\hy-mem-<版本>-win64.zip` | **对外分发的就是这个**，1417 个文件 / 152MB。解压出来就是完整目录，不存在漏文件 |
| `启动 hy-mem.cmd` | 双击入口。**启动前先查 `_internal\`**，缺了就用中文说明 + `pause` 停住窗口 |
| `安装到 PATH.cmd` | 一键 `hy-mem install` |
| `使用说明.txt` | 三步上手 + 常见问题（给完全不懂命令行的人） |
| `SHA256SUMS.txt` | exe 的 sha256，传输出错时校验 |

启动器内容**必须纯 ASCII**：cmd.exe 在 `chcp 65001` 下按块解码批处理，含中文的行会在块边界被截断，实测出现「一行 rem 的后半截被当成命令执行」。中文提示放在 `startup-error.txt` 里用 `type` 输出（`type` 只吐字节，不参与解析）。

### 启动前自检（两层）

| 层 | 位置 | 能查出什么 |
|---|---|---|
| 外层 | `启动 hy-mem.cmd` | 缺 `_internal\`（连 Python 都没起来，内层无能为力）、启动失败后停住窗口 |
| 内层 | `cli/runtime_check.py` → `_pyi_entry._preflight()` | Windows 版本（<8.1 跑不了 3.12）、位数、包完整性、数据目录可写、VC 运行库、控制台、磁盘余量 |

无参数运行（双击/首次使用）做完整检查；带参数只做廉价必检项，不拖慢高频调用。**只在有问题时输出**，并附「怎么修」（含国内镜像地址）；双击场景会等一次按键，不会一闪而过。同一套检查也进了 `hy-mem doctor`。

打包脚本里的参数不是随手写的，每一条都对应一次事故：

| 参数 | 不加会怎样 |
|---|---|
| `--collect-submodules chromadb --collect-data chromadb` | 构建全绿、`doctor` 全 PASS，但一写入就 `ModuleNotFoundError`（chromadb 按名字运行时取模块，静态分析看不到） |
| `--collect-submodules hy_memory` | 同上，SDK 内部子模块加载失败 |
| `--copy-metadata hy-memory --copy-metadata chromadb` | `--version` 显示 0.1.0（SDK 靠 dist-info 读版本，冻结后没有） |
| `--exclude-module tkinter/matplotlib/IPython/pytest` | 白多带 60~100MB |

打包后脚本会自动做三件事：

1. **消毒（发布纪律：包里不得内置任何模型 key）**：删掉产物里任意层级的 `config.json` / `.env` / `data\`，然后**再扫一遍**文本文件里的 `sk-…` / `Bearer …` / `AKIA…` 特征。扫到就判构建失败并列出文件 —— 带 key 的包发出去，比构建失败严重得多。所以拿到包的人必须自己跑一次 `hy-mem` 完成设置，否则任何写入/检索都会被配置闸门拦下并给出修复命令。
2. **带 SKILL.md**：把本文件复制进 `dist\hy-mem\skill\hy-mem\`，别人那边 `hy-mem skill --install` 才铺得出去。
3. **清历史产物与 build 缓存**：每次构建都是一个 354MB 目录，攒几次就把盘塞满。
4. **产物目录必须显式传给 PyInstaller**（`--distpath`）。只改脚本里的 `DIST` 变量不够 —— PyInstaller 默认仍写 `cwd/dist`，于是你指定了 `HY_MEM_DIST`，它却回头去删 `dist/hy-mem`，在旧 exe 正被占用时表现为「构建中途失败」。

### exe 与源码是两套完全独立的环境

这不是缺陷，是有意为之：打包产物要能整个目录拷给别人，就不能读你机器上的任何东西。

| | 源码（开发） | exe（分发） |
|---|---|---|
| 根目录 | 仓库根 | exe 所在目录（`…\hy-mem\`） |
| config / data / logs | 各一份，互相看不到 | 各一份，互相看不到 |
| 判定依据 | `sys.frozen` 为假 | `sys.frozen` 为真 |

后果：**在 exe 里配好的 key，`hy-mem` 命令读不到；反之亦然。** 排查「明明配好了却报配置不全」时，第一步先确认命令走的是哪一套：

```bash
hy-mem config show          # 看「配置文件」那行的路径
hy-mem.exe config show      # exe 自己那份
```

还有一条：`dist\hy-mem\` 是构建输出目录，**重新打包会按发布纪律删掉里面的 `config.json` 与 `data`**。自用配置请放在拷贝出去的目录里，别指望它留在 `dist` 下。


> 全 PASS ≠ 功能可用。验收必须在**打包后的 exe** 上跑一遍：`--version` → `help` → `help --json` → 无参非 TTY → `add` + `search`。`HY_MEM_TEST_EXE=dist/hy-mem/hy-mem.exe python scripts/test_cli.py` 可以一次跑完接口契约。
