# hy-mem

本地长期记忆服务，为 AI Agent 提供跨会话的记忆存储与语义检索能力。基于 [Tencent Hy-Memory SDK](https://github.com/Tencent-Hunyuan/Hy-Memory) 2.0.5 构建，内置多厂商模型预设（OpenAI 兼容 / 智谱 / 百炼 / DeepSeek，含自定义网关）。

> A local long-term memory CLI for AI agents — write, fact-extract via LLM, semantic search, and tag archiving, with a built-in MCP stdio server (8 tools).

## 下载安装

| 平台 | 下载 |
|------|------|
| Windows x64 | [hy-mem-1.0.0-rc.1-win64.zip](https://github.com/MrHuTu/hy-mem/releases/download/v1.0.0-rc.1/hy-mem-1.0.0-rc.1-win64.zip)（免 Python，解压即用） |

其他版本与历史发布见 [Releases](https://github.com/MrHuTu/hy-mem/releases)。

**安装三步**：解压到任意目录 → 双击「安装到 PATH.cmd」（或直接运行 `hy-mem.exe`）→ 首次运行进设置向导，配好向量模型与密钥即可使用。

## 功能特性

- **MCP 服务**：内置 stdio MCP server（9 工具：add/search/get/update/delete/list + queue_status + get_version + **evolve**），任何支持 MCP 的客户端（WorkBuddy / Claude Desktop / Cursor 等）即插即用。**pro 模式下 evolve 不出现在 `tools/list` 里**，客户端拿不到也就调不动
- **L6 认知层（仅 ultra）**：`evolve` 把已有的一批事实归纳成「行为模式」，检索时与事实一起按语义返回；归纳用哪个模型与抽取/判重分开配（`config set evolve.model=…`），弱模型能抽取但做不了开放归纳
- **语义检索**：基于向量相似度的记忆搜索，写入时 LLM 自动抽取事实与标签
- **异步写入**：非阻塞式入队，自动拉起后台 worker 消费；队列状态全程可查（pending / dead / shadow）
- **标签管理**：灵活的标签系统，支持归档与分类
- **交互式配置**：设置菜单 + 首次安装向导，连通性实测通过才放行
- **多身份支持**：通过 `user_id` 区分不同 Agent 的记忆空间

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 初始化配置

首次运行需执行设置向导：

```bash
python -m cli.hy_mem_cli setup
```

或启动交互式菜单：

```bash
python -m cli.hy_mem_cli menu
```

### 基本命令

| 命令 | 说明 |
|------|------|
| `add` | 添加记忆（支持单条/多条/带标签） |
| `search` | 语义检索记忆 |
| `list` / `get` / `update` / `delete` | 列出 / 详情 / 修改 / 逻辑删除 |
| `forget` / `finalize` | 彻底删除 / 清理逻辑删除残留 |
| `tags` / `untag` / `archive` | 标签与归档管理 |
| `stats` / `doctor` | 统计 / 19 项健康自检 |
| `queue` / `worker` / `drain` / `retry` | 异步队列查看 / 消费 / 清空 / 重投 |
| `mcp` / `serve` | 启动 MCP stdio 服务 / HTTP 服务 |
| `setup` / `config` / `user` / `install` | 设置菜单 / 配置管理 / 身份切换 / 注册到 PATH |
| `history` / `log` / `skill` / `migrate` / `version` | 操作历史 / 运行日志 / Skill 安装 / 数据迁移 / 版本 |
| `evolve` | 归纳 L6 行为模式（**仅 ultra**；pro 下退 1 并给出切换提示） |

完整 34 个子命令说明见 `hy-mem help`。

## 配置说明

配置文件位于 `config.json`，支持以下配置项：

- **embedding**: 嵌入模型配置（provider、model、apiKey）
- **llm**: 大语言模型配置
- **storage**: 存储路径配置
- **user_id**: 当前身份标识
- **search**: 检索参数（min_score 等）

**密钥存储**：默认直接固化在 `config.json` 的 `apiKey` 字段（`config.json` 恒被 `.gitignore` 保护，不会入库）；也兼容从环境变量读取（`MEMORY_EMBEDDER_API_KEY` / `MEMORY_LLM_API_KEY`，老配置无需改动）。

## 项目结构

```
hy-mem/
├── src/hymem/              # 包根（pip install -e . 可装；python -m hymem 可跑）
│   ├── cli.py             # 入口（命令分发）
│   ├── core/              # 核心域（无 UI 依赖，桌面/服务可复用）
│   │   ├── store.py       #   SQLite 队列与记忆库操作
│   │   ├── config.py      #   配置加载与密钥解析
│   │   ├── netprobe.py    #   出站请求唯一出口
│   │   └── ...            #   presets / oplog / vdb_endpoint / version 等
│   ├── ui/                # 终端交互层
│   │   ├── tui.py         #   终端组件（行编辑/菜单/确认）
│   │   ├── menu.py        #   交互式控制台
│   │   ├── setup_wizard.py#   设置向导与设置菜单
│   │   └── ...            #   helptext / i18n / skill_ops
│   └── mcp/
│       ├── server.py      # MCP 服务（9 工具，pro 下 8 个；evolve 仅 ultra 可见）
│       └── transport_http.py  # HTTP transport（--transport http，默认 stdio 不变）
├── skill/                 # Skill 定义
├── scripts/               # 业务脚本（构建 / 闸门 / check_secrets / check_layering）
├── tests/                 # 14 套测试（cli / ui / menu / setup / setup_ui / mcp / queue / vdb / oplog / i18n / encoding / concurrency / scene / evolve）
├── .local-test/           # 本地临时脚本（探针 / 实验 / bug 复现），整目录不入库
├── data/                  # 运行时数据（不入库，程序自建）
└── AGENTS.md             # 项目交接文档
```

分层铁律：core 不 import ui/mcp；ui 与 mcp 互不依赖；版本号真源在 core/version.py。

## 开发说明

### 源码模式运行

```bash
python -m cli.hy_mem_cli <command> [options]
```

### 打包为可执行文件

```bash
python scripts/build_exe.py
```

打包产物为目录或压缩包形式分发，不建议使用 onefile 模式。

### 测试

```bash
python tests/test_cli.py        # CLI（97 项）
python tests/test_setup_ui.py   # 设置向导 UI 级（40 项）
# 共 14 套 772 项：cli / ui / menu / setup / setup_ui / mcp / queue / vdb / oplog /
# i18n / encoding / concurrency / scene / evolve。其中 mcp、concurrency、scene、evolve
# 会真调模型（缺密钥时显式跳过，不产出假绿）
```

## 注意事项

- **exe 与源码是两套完全独立的环境**，修改代码后需重新打包
- 写入记忆时建议**一条只放一个主题**，保持粒度清晰；事实型表述（而非寒暄/闲聊）才会被抽取成可检索记忆
- 异步写入会自动拉起后台 worker，入队后可通过 `queue` 查看进度；**入队 ≠ 立即可检索**，pro 模式单条落库可达 2 分钟
- 密钥仅支持 ASCII 字符

## 版本策略

CLI 独立 semver，不与底层 hy-memory SDK 版本耦合（SDK 版本见 `hy-mem doctor`）。当前为 **1.0.0-rc.1**（Release Candidate）：功能面完整、测试矩阵全绿，进入发布候选；自用验证稳定后转正 1.0.0。

## 声明

- **本项目完全由 AI 生成**（GLM + 人类需求方驱动开发模式），代码、测试与文档均由 AI 产出，人类负责需求定义、方案评审与验收。
- 面向**个人使用**设计：单机单用户场景（SQLite + 本地向量库），未做多租户、鉴权与并发治理，不适合生产环境多用户部署。
- 底层能力来自 Tencent [Hy-Memory SDK](https://github.com/Tencent-Hunyuan/Hy-Memory)（Apache 2.0），本项目仅在其上封装 CLI / MCP / TUI 交互层。

## 许可证

Apache License 2.0，见 [LICENSE](LICENSE)。