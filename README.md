# hy-mem

本地长期记忆服务，为 AI Agent 提供跨会话的记忆存储与语义检索能力。基于 [Tencent Hy-Memory SDK](https://github.com/Tencent-Hunyuan/Hy-Memory) 2.0.15 构建，内置多厂商模型预设（OpenAI 兼容 / 智谱 / 百炼 / DeepSeek，含自定义网关）。

> A local long-term memory CLI for AI agents — write, fact-extract via LLM, semantic search, and tag archiving, with a built-in MCP stdio server (9 tools).

## 下载安装

| 平台 | 下载 |
|------|------|
| Windows x64 | [hy-mem-win64.zip](https://github.com/MrHuTu/hy-mem/releases/download/v1.0.0-rc.11/hy-mem-win64.zip)（免 Python，解压即用；zip 恒名不带版本号，解压覆盖即升级） |

其他版本与历史发布见 [Releases](https://github.com/MrHuTu/hy-mem/releases)。

**安装三步**：解压到任意目录 → 双击「安装到 PATH.cmd」（或直接运行 `hy-mem.exe`）→ 首次运行进设置向导，配好向量模型与密钥即可使用。

## 功能特性

- **MCP 服务**：内置 stdio MCP server（9 工具：add/search/get/update/delete/list + queue_status + get_version + **evolve_status**），任何支持 MCP 的客户端（WorkBuddy / Claude Desktop / Cursor 等）即插即用。**pro 模式下 evolve_status 不出现在 `tools/list` 里**，客户端拿不到也就调不动
- **L6 认知层（仅 ultra）**：行为模式归纳**全自动**——add 素材攒够（默认 ≥8 条新事实）且距上次尝试 ≥24h 就后台自动跑，无需手动触发；`evolve_status` 工具或 CLI `hy-mem evolve-status`（所有模式可看）查进度与历史，`get_version` 在连续失败时会顺带带出 `evolve_health` 异常信号。归纳模型与抽取/判重分开配（`config set evolve.model=…`）——**抽取（`llm.model`）配分析类小模型**（如 `tongyi-xiaomi-analysis-flash`，封闭判定够用且快），**归纳（`evolve.model`）配更强的通用大模型**（如 `deepseek-v4.1-flash`）：弱模型能抽取但做不了开放归纳。手动逃生门：CLI `hy-mem evolve`（直连模式下可用）
- ⚠️ **ultra 单写者语义**：L6 图库用 kuzu（独占锁，无 `read_only`），同一数据目录**图库只允许一个进程持有**。本版本起内置上游补丁：持锁冲突时自动降级为图 no-op（事实/向量/检索完全正常，仅该进程写入的记忆暂缺 L4 关系边，可用 `graph-build` 批量回填），**不再抛错死信**。`servePort` 可为 serve 固定端口（默认 0=随机）；serve 重启换端口后，MCP 长驻 client 会自动检测并重建端点（U3 自愈），无需重启客户端
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
python -m hymem setup
```

或启动交互式菜单：

```bash
python -m hymem menu
```

### 基本命令

| 命令 | 说明 |
|------|------|
| `add` | 添加记忆（支持单条/多条/带标签） |
| `search` | 语义检索记忆（支持 `queries` 多路并行检索） |
| `list` / `get` / `update` / `delete` | 列出 / 详情 / 修改 / 逻辑删除 |
| `forget` / `finalize` | 彻底删除 / 清理逻辑删除残留（`--language` 指定归纳语言） |
| `tags` / `untag` / `archive` | 标签与归档管理 |
| `stats` / `doctor` | 统计 / 19 项健康自检 |
| `queue` / `worker` / `drain` / `retry` | 异步队列查看 / 消费 / 清空 / 重投（含 shadow 影子重投） |
| `backup` / `restore` | 数据面快照备份 / 恢复 |
| `vdb-scan` / `vdb-plan` / `vdb-reembed` / `vdb-reembed-graph` / `vdb-drop` | 向量空间巡检 / 重嵌计划 / 执行重嵌 / 图重嵌 / 删除空间（换嵌入模型后迁移用） |
| `graph-build` | 存量记忆批量回填 L4 关系图（图库重建/降级补边用） |
| `profile` | 用户画像查看（`--full` 含全部演化细节） |
| `mcp` / `serve` | 启动 MCP stdio 服务 / HTTP 服务 |
| `setup` / `config` / `user` / `install` | 设置菜单 / 配置管理 / 身份切换 / 注册到 PATH |
| `history` / `log` / `skill` / `migrate` / `version` | 操作历史 / 运行日志 / Skill 安装 / 数据迁移 / 版本 |
| `evolve` | 手动归纳 L6 行为模式（**逃生门**：直连模式可用；正常情况不需要，沉淀已自动化） |
| `evolve-status` | L6 沉淀进度与健康度查看（所有模式可用） |

完整 43 个子命令说明见 `hy-mem help`。

## 配置说明

配置文件位于 `config.json`，支持以下配置项：

- **embedding**: 嵌入模型配置（provider、model、apiKey）
- **llm**: 大语言模型配置。**S1 抽取模型，推荐分析类小模型**（信息抽取、场景分类等封闭判定需求），如 `tongyi-xiaomi-analysis-flash` —— 高频调用，快和便宜优先，不要求开放生成能力
- **evolve**: L6 归纳配置（model / maxTokens 8000 / schemaLimit 3 / schemaMinScore 0.25 / language）。**model 是 S2 归纳模型，推荐更强的通用大模型**（如 `deepseek-v4.1-flash`）—— 开放归纳是成败关键，分析类小模型做不了（实测只回 7 个 token 还自报 success）；留空则回落 `llm.model`（等于没分）。schemaMinScore 默认 0.25 而非 SDK 的 0.4：evolve 归纳出的 schema 是英文，中文 query 对它的语义分实测 0.17~0.34，0.4 会全部滤空。**language** 控制归纳输出语言：默认 auto 跟随 SDK 检测（对技术型中文记忆失灵，实测 32 条中文事实因 ASCII 密度被判 en）；`config set evolve.language=zh` 强制中文 schema（CLI 有 `--language`）——SDK ≥2.0.11 原生支持（显式语言会换归纳提示词本体 + 追加硬性输出语言约束），配 zh 后中文 query 检索分可回正常档；auto 检测上游仍未修，显式配置是推荐用法。**auto** 控制自动沉淀（`evolve.auto`）：enabled=true（默认，仅 ultra）/ minNewFacts=8（自上次沉淀以来新增 L2 事实达到才跑）/ cooldownHours=24（两次尝试最小间隔，连续失败按 2^n 退避、7 天封顶，成功清零）。配置入口：`config set evolve.model=…` 或 `setup` 面板「沉淀模型」（仅 ultra 生效）
- **storage**: 存储路径配置
- **user_id**: 当前身份标识
- **search**: 检索参数（min_score 等）
- **servePort**: serve 固定端口（0=随机默认；设为固定值后 serve 重启不再换端口，长驻 MCP client 端点恒有效）
- **backend.memory**: 记忆后端选择（默认 `hy-memory`；JDBC 式协议层，实现在 `backends/`，未来可注册其他实现）
- **graph**: ultra 图库资源（`graph.bufferPoolMb` / `graph.maxThreads`）。留空沿用 SDK 2.0.15 默认（512MB / 4 线程），`0` 表示交回 Kuzu 默认（≈80% 物理内存 / 全部核）。等价环境变量 `MEMORY_GRAPH_BUFFER_POOL_MB` / `MEMORY_GRAPH_MAX_THREADS`；两处都设时 config 覆盖 env（与其它字段一致）。`doctor` 会回显生效值。SDK 2.0.15 起同进程多个 client 共享一个 `kuzu.Database`，跨进程仍受文件锁约束

**密钥存储**：默认直接固化在 `config.json` 的 `apiKey` 字段（`config.json` 恒被 `.gitignore` 保护，不会入库）；也兼容从环境变量读取（`MEMORY_EMBEDDER_API_KEY` / `MEMORY_LLM_API_KEY`，老配置无需改动）。

## 项目结构

```
hy-mem/
├── src/hymem/              # 包根（pip install -e . 可装；python -m hymem 可跑）
│   ├── cli.py             # 门面入口（cmds/ 拆包后仅 re-export，79 行）
│   ├── cmds/              # CLI 按域拆包（base / write / read / maint / queue / config / parser / entry）
│   ├── ports/             # JDBC 式协议层：MemoryBackend Protocol + make_backend 工厂（不 import vendor）
│   ├── backends/          # 后端实现（hymemory/ 适配器：vendor import 唯一允许地）
│   ├── core/              # 核心域（无 UI 依赖，桌面/服务可复用）
│   │   ├── store.py       #   SQLite 队列与记忆库操作
│   │   ├── config.py      #   配置加载与密钥解析
│   │   ├── netprobe.py    #   出站请求唯一出口
│   │   ├── vdb_endpoint.py#   serve 按需拉起 / 端点发现与自愈
│   │   └── ...            #   presets / oplog / version 等
│   ├── ui/                # 终端交互层
│   │   ├── tui.py         #   终端组件（行编辑/菜单/确认）
│   │   ├── menu.py        #   交互式控制台
│   │   ├── setup_wizard.py#   设置向导与设置菜单
│   │   └── ...            #   helptext / i18n / skill_ops
│   └── mcp/
│       ├── server.py      # MCP 服务（9 工具，pro 下 8 个；evolve_status 仅 ultra 可见；端点失效自愈）
│       └── transport_http.py  # HTTP transport（--transport http，默认 stdio 不变）
├── skill/                 # Skill 定义
├── docs/                  # 契约文档（docs/specs/）+ 上游 bug 追踪（UPSTREAM-ISSUES.md）
├── scripts/               # 业务脚本（构建 / 上游补丁重放 / 闸门 / check_secrets / check_layering）
├── vendor/hy-memory/      # 上游 SDK 快照（gitlink，拉最新源码用）
├── tests/                 # 14 套测试（cli / ui / menu / setup / setup_ui / mcp / queue / vdb / oplog / i18n / encoding / concurrency / scene / evolve）
├── .local-test/           # 本地临时脚本（探针 / 实验 / bug 复现），整目录不入库
├── data/                  # 运行时数据（不入库，程序自建）
└── AGENTS.md             # 项目交接文档
```

分层铁律：core 不 import ui/mcp；ui 与 mcp 互不依赖；vendor import 只准出现在 backends/；版本号真源在 core/version.py。

## 开发说明

### 源码模式运行

```bash
python -m hymem <command> [options]
```

### 打包为可执行文件

```bash
python scripts/build_exe.py
```

打包产物为目录或压缩包形式分发，不建议使用 onefile 模式。

### 测试

```bash
python tests/test_cli.py        # CLI（162 项，含分层守护 G1/G2）
python tests/test_setup_ui.py   # 设置向导 UI 级（41 项）
# 共 14 套 921 项：cli / ui / menu / setup / setup_ui / mcp / queue / vdb / oplog /
# i18n / encoding / concurrency / scene / evolve。其中 mcp、concurrency、scene、evolve
# 会真调模型（缺密钥时显式跳过，不产出假绿）
```

## 注意事项

- **exe 与源码是两套完全独立的环境**，修改代码后需重新打包
- 写入记忆时建议**一条只放一个主题**，保持粒度清晰；事实型表述（而非寒暄/闲聊）才会被抽取成可检索记忆
- 异步写入会自动拉起后台 worker，入队后可通过 `queue` 查看进度；**入队 ≠ 立即可检索**，pro 模式单条落库可达 2 分钟。`retry` / `retry --shadow` 重投后同样会按需拉起消费者（回执含 `worker_spawned` / `worker_live`）；设 `HY_MEM_AUTO_WORKER=0` 可统一关闭自动拉起（add 与 retry 都生效，默认开启，非法值按开启并告警）
- 密钥仅支持 ASCII 字符

## 版本策略

CLI 独立 semver，不与底层 hy-memory SDK 版本耦合（SDK 版本见 `hy-mem doctor`）。当前为 **1.0.0-rc.11**（Release Candidate）：功能面完整、测试矩阵全绿，进入发布候选；自用验证稳定后转正 1.0.0。

对上游 SDK 的已知问题（图库锁降级被否决 / auto 语言误判 / 端点构造期冻结）建有追踪文档 [docs/UPSTREAM-ISSUES.md](docs/UPSTREAM-ISSUES.md)：本仓库以补丁/自愈机制临时规避，上游修复后按文档流程退役。

## 声明

- **本项目完全由 AI 生成**（GLM + 人类需求方驱动开发模式），代码、测试与文档均由 AI 产出，人类负责需求定义、方案评审与验收。
- 面向**个人使用**设计：单机单用户场景（SQLite + 本地向量库），未做多租户、鉴权与并发治理，不适合生产环境多用户部署。
- 底层能力来自 Tencent [Hy-Memory SDK](https://github.com/Tencent-Hunyuan/Hy-Memory)（Apache 2.0），本项目仅在其上封装 CLI / MCP / TUI 交互层。

## 许可证

Apache License 2.0，见 [LICENSE](LICENSE)。