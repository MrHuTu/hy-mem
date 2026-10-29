# 模型预设收敛 + 交互精简 · 契约

> 父级文件：本文即父级（唯一事实源）。
> 类型：功能调整 + 缺陷修复（用户现场反馈 5 条 + 交互重构）　｜　状态：**已实现（待打包手测）**
> 创建：2026-10-28　｜　分支：test　｜　前置：`docs/specs/2026-10-28-cli-loop-and-model-switch/`（默认续用那轮）

## 0. 现象（用户原话）

1. 「模型设置（包括向量模型）提供几种主流的模型选择（GPT, GLM, DeepSeek），和自定义模型设置就行，
   其他花里胡哨的设置方式都删掉。」
2. 「向量模型和 LLM 模型重新设置还是有问题，我按 N，结果提示 401。」
3. 「写入和查询，执行完成一次之后另起一行，新行的标题头太傻了，不要计数的文本提示。」
4. 「写入的时候，还提示要不要唤醒 worker，这有必要吗？默认就唤醒了消费者，这个确认还多余。」
5. 「你写一个 UI 测试脚本吧，要保证 CLI 每个菜单都正常。」

## 1. 契约区 —— 实现与测试都只认这里

| # | 条款 | 约定 | 落地位置 |
| --- | --- | --- | --- |
| C1 | 预设只留主流三家 + 自定义 | 向量模型列 3 项：`GPT` / `GLM` / `百炼 qwen`；LLM 列 3 项：`GPT` / `GLM` / `DeepSeek`。两者末尾各一项 `自定义`。**不再出现**区分度 / 延迟 / 多维度的指标副标题，每项只留一句话说明。历史预设（text-embedding-v4、qwen 标准版、硅基 bge-m3、本地 ONNX、ysjx 两条、qwen-maas）全部移除 | `cli/presets.py` |
| C2 | DeepSeek 不进向量预设 | DeepSeek 官方只提供 chat 接口，没有 `/embeddings`。向量选择页必须显式说明这一点，避免用户去找一个不存在的入口 | `cli/setup_wizard.py::step_embedding` |
| C3 | 跨端点不继承旧密钥 | 只有**配置端点与所选预设端点相同**时才自动继承环境变量里的 key；换了端点一律不复用旧 key，直接要求提供该端点自己的 key，并把旧变量名与旧端点写进提示 | `cli/setup_wizard.py::step_embedding` / `step_llm` |
| C4 | 继承即不落盘、不再追问 | 走环境变量继承时不得再问「改为在 config.json 里单独保存一个 key？」—— 按 N 之后旧代码仍拿旧 key 去请求新端点，这正是 401 的来源 | 同上 |
| C5 | 401 要说清是谁的 key | 探针拿到 401/403 且密钥来自环境变量时，错误信息必须带「$VAR 里的 key 可能属于旧端点（X）」 | `cli/setup_wizard.py` |
| C6 | 轮次不带计数标题 | 任何一次写记忆 / 检索**都不得**打印「写记忆（第 N 条）」「检索（第 N 次）」之类的计数标题。控制台里两条结果之间只隔一个空行 —— 结果本身就在滚动区，接着往上滚就能对照 | `cli/menu.py` |
| C7 | 不确认已自动发生的事 | `add --async` 已经自动拉起消费进程，交互层**不得**再问「现在就跑一次 worker 落库？」，改为直接告知「后台会自动落库，此刻检索不到是正常的」 | `cli/menu.py::cmd_add` |
| C8 | 菜单全覆盖 | 新增 `scripts/test_menu.py`：旧菜单 18 个功能项（主菜单 10 + 设置中心 8）逐项映射到命令并**真跑**，缺一条即失败；`storage`/`lang`/`delete`/`get` 另有一条覆盖断言；另有 1 条真实 `run_menu` 管道循环与 1 条 `setup` 路由校验 | `scripts/test_menu.py` |
| C9 | 命令名即子命令名 | 交互层不再自造一套菜单动作名。每条命令 `Command.name` 与 `hy-mem <name>` 子命令同名（`add` / `search` / `setup` …），别名只做输入便利（`a`、`s`、`ls`）。旧菜单 18 个功能项必须有明确承接，见 `S6-menu-sweep.md` 映射表 | `cli/menu.py::COMMANDS` |
| C10 | 结果留在滚动区 | 一次操作结束即回到提示符，**不清屏、不「按任意键继续」、不重画表头**。只有 `clear` 命令自己清屏 | `cli/menu.py` |
| C11 | 非 TTY 可管道驱动 | `hy-mem menu` 在 stdin 非 TTY 时逐行读命令、逐条执行，不降级成编号菜单；stdin EOF 等价 `Ctrl+D` 退出并打印「退出。」，**不得**在 EOF 上原地打转 | `cli/menu.py::run_menu`、`cli/tui.py::line(eof_is_exit=True)` |
| C13 | 设置流程四段式（用户规范） | 设置模型（embedding / llm）必须按序：①等用户把 baseUrl / 模型 / key 补充完整——继承来的 key 也要明示来源（`_confirm_credentials` 三件套 + **三选一**：就用这把（默认）/ 换一把 key / 取消，换 key 是显式入口而非藏在 N 后面；信息不齐不发探针）；②信息完整才测连通性；③不可用可重设（retry/save/abort 菜单）；④连通性通过后，**与旧配置有差异时**先摆出旧→新对比再问「覆盖旧配置？」（`_credential_diff`），无差异不问 | `cli/setup_wizard.py` |
| C12 | 行编辑对齐 bash | ↑/↓ 与 `Ctrl+P/N` 翻历史（落盘 `<dataDir>/shell-history`，上限 300 条、单条截 200 字符、连续重复不记）、`Tab` 补全命令名与已知参数词、`Ctrl+R` 反查、`Ctrl+A/E` 行首行尾、`Ctrl+U/W/K` 删、`Ctrl+L` 清屏、`Ctrl+D` 空行退出、`Ctrl+C`/`Esc` 只放弃本行不退出会话 | `cli/tui.py::_line_edit` |

## 2. 分片表（根因表）

| # | 根因 / 动机 | 分片 | 改动 | 状态 | commit |
| --- | --- | --- | --- | --- | --- |
| S1 | 预设 5+4 条，带区分度/延迟/维度三列指标，选择页 6~7 屏；用户只想要"选个主流模型" | `S1-presets.md` | `cli/presets.py`、`cli/i18n.py` | 已实现 | e50f141 |
| S2 | 继承候选链兜底写死 `MEMORY_EMBEDDER_API_KEY`，跨端点照用 → 401；且继承分支还多问一句"是否单独保存" | `S2-cross-endpoint-key.md` | `cli/setup_wizard.py`、`cli/config.py` | 已实现 | e50f141 |
| S3 | 第 2 次起打「写记忆（第 2 条）」标题；入队后又问「现在就跑一次 worker？」 | `S3-round-title.md` | `cli/menu.py`、`cli/i18n.py` | 已实现（随 S4 重构） | e50f141 |
| S4 | 箭头菜单一次操作要按 4~6 次键，「按任意键继续」打断流；`tui.clear()` 擦掉上一次结果 | `S4-shell.md` | `cli/menu.py`（整体重写）、`cli/hy_mem_cli.py`、`cli/i18n.py` | 已实现 | e50f141 |
| S5 | 控制台没有历史 / 补全 / 反查，按错一个字只能整行重打 | `S5-line-editing.md` | `cli/tui.py` | 已实现 | e50f141 |
| S7 | 函数级桩测复现不了真实按键流（401 事故发生在「设置→选当前项→按 N」的 UI 流里）；且 C13 落地后需要 UI 级验证 | `S7-setup-ui-test.md` | `scripts/test_setup_ui.py` | 已实现 | 216545e |
| S6 | 没有一条测试保证"每个菜单都能进去再出来" | `S6-menu-sweep.md` | `scripts/test_menu.py` | 已实现 | e50f141 |

## 3. 变更记录（只追加）

| 日期 | 改了什么 | 为什么 | 影响分片 | 是否重走澄清 |
| --- | --- | --- | --- | --- |
| 2026-10-28 | 初版，S1-S5 | 用户现场反馈 5 条 | — | — |
| 2026-10-28 | C1 定为「向量 GPT/GLM/百炼qwen + 自定义」 | 用户选择：保留 qwen 作为独立预设，避免当前配置重选时落到自定义要手填端点 | S1 | 是（已问） |
| 2026-10-28 | LLM 默认档位定为轻量档 | 用户选择：gpt-4o-mini / glm-4-flash / deepseek-chat | S1 | 是（已问） |
| 2026-10-28 | 新增 C9~C12，分片表扩到 S1~S6 | 用户补充"ui 界面交互很多地方反人类，仿照 pi / bash shell 重做，别偷懒" —— 原 S4（worker 确认）/ S5（菜单扫描）被合并进 S3 / S6，空出的编号给控制台与行编辑 | S3 / S4 / S5 / S6 | 否（用户已给方向） |
| 2026-10-29 | ①验证失败菜单按场景分流：单步/菜单路径只有 retry/abort（测试不过不放行），首次向导保留 save（离线出口）；②`hy-mem setup` 子命令在 needs_setup 为真时等价首次向导（菜单化改造曾把首次安装漏进菜单）；③冻结进程 stdin 管道中文解码修复（_ask_line 裸 input() 按 cp936 切 UTF-8 字节，改按字节读+探测链，与 add 子命令同源）；④同端点不弹「端点已从 X 换到 X」荒谬警告；setup_ui 36→40（+I 失败菜单分流） | 用户口径：url/key/model 齐全→测连通→通过放行否则只能重配→用户确认后才覆盖；及「PS 传中文是否真有问题」的质疑（取证后坐实 stdin 管道路径） | S2/S4/S7 | 是（用户已给口径） |
| 2026-10-29 | setup 无参从顺序向导改为**设置菜单**（每项带当前值、选一项改一项、改完回菜单；首次安装保留顺序向导）；mode 切 pro/ultra 且 LLM 不可用时同流强制引导 LLM（判据 `_mode_llm_ok`=model+可用 key，防 `_fill_from_env` 预填误判）；**key 存储语义反转：默认固化进 config.json**（apiKey 有值、keyEnv 清空；config.json 恒被 gitignore、check_secrets 拦截误提交，不构成泄密面）——环境变量降级为兼容读取来源；setup_ui 29→36（+G 菜单/H 联动），test_setup 7 组断言反转 | 用户反馈三点：①各配置项应可单独设置而非嵌在引导流程；②pro 必须有 LLM；③key 放环境变量无实际意义（换模型即作废），当初用环境变量只为源码仓不泄密，config.json 本就不进仓 | S2/S4/S7 | 是（用户已给口径） |
| 2026-10-29 | MCP 异步反馈四缝修复：queue_status 的 shadow_only hint 压过「可检索」话术、search 返回体带 pending_writes、add note 按实测口径（pro 单条可达 2 分钟）、get_version 带 queue_health；mcp 22→25、queue 18→21 | 用户问「add 走异步静默返回是否不友好」；实测 0.9.14 抓到 job 87 done+shadow_only 但 hint 说「可以 search 验证」的自相矛盾、落库前 search 无队列提示、note 承诺 6~9s 实测 124s | （本轮为 MCP 交互修复，挂在同一契约下追溯） | 是（实测驱动） |
| 2026-10-29 | C13 的 key 决定从「confirm 二态」改为三选一菜单（就用这把/换一把 key/取消），新增 F 场景验证同端点显式换 key；setup_ui 25→29 | 用户反馈：设置/重置模型时没有要求输入 key 的机会——换全新模型的用户被「一路回车继承」挡住 | S7 | 是（用户已给口径） |
| 2026-10-28 | 新增 C13（设置四段式）与 S7（UI 级测试）；同时修掉 3 个「第二次配置」残留 bug：自定义分支 same_endpoint 写死 True、自定义配置默认光标落第 1 预设、ask_secret 空回车放行 | 用户给出明确交互规范（信息补齐→测连通→可重设→覆盖确认）；UI 级测试首跑红测抓到 | S2 / S7 | 是（用户已给规范） |
| 2026-10-28 | C6 措辞从"第 2 次起不打标题"改为"任何一次都不打计数标题" | 控制台里没有"轮次"这个概念了，结果直接写进滚动区 | S3 / S4 | 否 |

## 4. 追溯表

| 条款 | 分片 | commit | 产物 | 验收证据 |
| --- | --- | --- | --- | --- |
| C1 / C2 | S1 | e50f141 | 待打包 | `test_setup.py`：向量预设恰 3 条 + 自定义、LLM 预设恰 3 条 + 自定义、向量不含 DeepSeek |
| C3 / C4 / C5 | S2 | e50f141 | 待打包 | `test_setup.py` Q4/Q8：换端点 → 不继承、`keyEnv` 指向新端点变量名、`resolve_embed_key()` 解析为空；同端点改模型 → 继承且不再追问 |
| C6 / C7 | S3 | e50f141 | 待打包 | 真 CLI 连写两条：第二次不出现「第 N 条」字样；入队后无「现在就跑一次 worker」 |
| C8 | S6 | e50f141 | 待打包 | `test_menu.py` 112 项全绿 |
| C9 / C10 / C11 | S4 | e50f141 | 待打包 | `printf 'help\nconfig\nqueue\nlog\nexit\n' \| hy-mem menu` 逐条执行并正常退出；stdout 无「按任意键继续」 |
| C13 | S7 | 216545e | 待打包 | `test_setup_ui.py` 25 项全绿（含三件套确认、跨端点不继承、覆盖提示、EOF 中止不落盘） |
| C12 | S5 | e50f141 | 待打包 | `test_ui.py` 15 项行编辑用例（↑历史 / Tab / Ctrl+R / Ctrl+D / Ctrl+K）全绿 |

## 5. 结论

六片全部落地，回归 **565 项全绿**（cli 84 / ui 110 / setup 139 / vdb 36 / queue 18 / oplog 25 / i18n 31 / encoding 10 / menu 112），含 MCP 的 22 项为 587。

三条用户现场问题的根因与最终修法：

1. **401 不是"key 填错了"，是继承链回落到旧变量名。** `resolve_embed_key()` 在 `keyEnv` 显式写了名字但值为空时，又退回去读 `MEMORY_EMBEDDER_API_KEY` —— 于是新端点拿着旧端点的 key 发请求。改成"`keyEnv` 非空就只认它"，同时跨端点填完 key 后 `keyEnv` 指向**新端点自己的变量名**（不是清空，清空会重新触发回落）。
2. **"标题头太傻"其实是"菜单式交互"的症状。** 只要还在"进页面 → 读标题 → 作答 → 按任意键 → 回菜单"这套里，就必须给每次循环一个标题来分隔。换成常驻提示符后，结果直接续在滚动区，C6 自然满足，不需要单独做补丁。
3. **UI 级测试抓到了函数级测不到的三个 bug。** 自定义分支 same_endpoint 写死 True（换 baseUrl 静默继承旧 key）、自定义配置默认光标落第 1 预设（回车秒变跨端点）、ask_secret 空回车放行（空 key 落盘后 resolve 回落拿旧 key）——全部在「第二次配置」家族，全部由 `test_setup_ui.py`（真子进程 + 管道喂键 + 假端点只认自家 key）首跑红测抓到。
4. **测试缺口在中间层。** `test_cli` 只管子命令、`test_ui` 只管按键解码，中间"界面把命令接错"没人管。`test_menu.py` 把旧菜单 18 个功能项逐一映射到命令并真跑，补上了这一层。
