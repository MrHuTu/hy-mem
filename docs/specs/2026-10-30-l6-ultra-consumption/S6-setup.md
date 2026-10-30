# S6 · 设置功能入口（换沉淀模型）

> 条款：C16　｜　依赖：S1（`evolve` 配置段）、S2（evolve 命令）　｜　状态：✓
> 用户原话（本轮起点）：「这个设置必须放到 CLI 的设置功能中去，不然用户无法更换模型」

## 1. 为什么单独成片

S1 只做了 `config set evolve.model=…`——这是「命令行能改」，不是「设置功能能换」。
设置面板（`hy-mem setup`）是用户唯一会主动进的配置入口，里面没有这一项，
等于**默认模型换不了**：`llm.model` 是抽取用的快模型，改它会把 `add` 一起拖慢（违背 C1/C5）。

## 2. 做什么

| 位置 | 改动 |
|---|---|
| `ui/setup_wizard.py` | 新增 `step_evolve()`；注册进 `STEPS`（插在 `llm` 之后）；设置菜单加一行「沉淀模型 · …」；`_apply_step` 切 ultra 时提示去哪配；`_summary_rows` 加一行；`_credential_diff` 纳入 `evolve.model`（改模型也要走「覆盖旧配置？」知情确认） |
| `ui/setup_wizard.py` | 设置菜单从「按 `idx` 取 `STEPS[idx]`」改成「按 key 查」——旧的 `idx == 7` 硬编码了退出项，插一行就错位 |
| `ui/menu.py` | `_SCOPES` 加 `evolve`（控制台 `setup evolve` 路由 + 补全）；`cmd_config` 打一行「沉淀模型」 |
| `ui/i18n.py` | `沉淀模型` / `沿用抽取模型（仅 ultra）` / `L6 归纳用哪个模型` 三条英文 |
| `ui/helptext.py` | `help setup` 补 `--step evolve` 例子与两条 notes |

## 3. 关键判断：只能填同端点的模型名

`evolve` 的实现是「取当前 config → **只覆盖 `llm.model`** → 造 client」（C5）。
`baseUrl` / `apiKey` 仍走 `llm` 那套，所以：

- ✅ 本机这种自建网关（`192.168.5.39:3000` newapi）换模型 = 只换名字，正合适
- ❌ 选「换到 DeepSeek 官方」这种跨厂商预设，必然 404/401

因此步骤里**不列跨厂商预设**，只给「沿用抽取模型 / 指定另一个模型」二选一 + 手填，
并把当前 `baseUrl` 打在表头，让「同源」这件事一眼可见。

## 4. 验证

| 用例 | 期望 | 实测 |
|---|---|---|
| R1–R3 首次指定 | `evolve.model` 落为强模型；探针用的是新名字；`maxTokens/schemaLimit/schemaMinScore` 不被冲掉 | PASS |
| R4–R5 第二次改回沿用 | `model` 清空；已配过时默认光标落在「指定另一个模型」 | PASS |
| R6 验证失败仍保存 | 记下名字、不阻断（真跑报错再说） | PASS |
| R7 验证失败放弃 | 抛 `Aborted`，不写配置 | PASS |
| R8 走 `run_setup(scope="evolve")` | 真落盘，`config validate` 显示 `evolve.model written-model` | PASS |
| R9 串配置检查 | `llm.model` 不被改（沉淀模型是独立字段） | PASS |
| G0（UI 级） | 设置菜单出现「沉淀模型 …（仅 ultra）」；退出项从 8 变 9 | PASS |

`tests/test_setup.py` 139 → **148**（+9）；`tests/test_setup_ui.py` 40 → **41**（+1）；
`tests/test_menu.py` 112 → **113**（`_SCOPES` 多一项，路由循环多跑一条）。

## 5. 没做

- **不动 `maxTokens` / `schemaLimit` / `schemaMinScore` 的交互式设置**：三个都是调参项，
  默认值来自实测（8000 / 3 / 0.4），日常不需要改，留在 `config set` 里
- **不做跨端点切换**：那等于配第二套 LLM，属于新能力，不在 C16 范围
