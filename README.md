# hy-mem

本地长期记忆服务，为 AI Agent 提供跨会话的记忆存储与语义检索能力。基于 Tencent Hy-Memory SDK 2.0.5 构建，支持 Qwen3.7 云端嵌入（1024 维向量）。

## 功能特性

- **语义检索**：基于向量相似度的记忆搜索
- **异步写入**：非阻塞式入队机制，支持队列消费
- **标签管理**：灵活的标签系统，支持归档与分类
- **交互式配置**：可视化设置向导与健康检查
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
| `list` | 列出记忆 |
| `get` | 获取单条记忆详情 |
| `delete` | 删除记忆（逻辑删除） |
| `forget` | 彻底删除记忆 |
| `tags` | 列出所有标签 |
| `untag` | 解绑标签 |
| `archive` | 归档管理 |
| `stats` | 统计与健康检查 |
| `queue` | 查看异步队列 |
| `worker` | 消费队列任务 |

完整命令列表可通过 `python -m cli.hy_mem_cli help` 查看。

## 配置说明

配置文件位于 `config.json`，支持以下配置项：

- **embedding**: 嵌入模型配置（provider、model、api_key）
- **llm**: 大语言模型配置
- **storage**: 存储路径配置
- **user_id**: 当前身份标识
- **search**: 检索参数（min_score 等）

也可通过环境变量覆盖配置，详见 `AGENTS.md`。

## 项目结构

```
hy-mem/
├── cli/                    # CLI 核心模块
│   ├── hy_mem_cli.py      # 主入口，命令解析与分发
│   ├── config.py          # 配置加载与管理
│   ├── store.py           # SQLite 数据库操作
│   ├── local_embedder.py  # 本地嵌入服务
│   ├── tui.py             # 终端交互组件
│   ├── menu.py            # 交互式菜单
│   ├── setup_wizard.py    # 设置向导
│   └── ...
├── skill/                 # Skill 定义
├── scripts/               # 业务脚本（构建 / 提交闸门 / 测试套件）
│   ├── build_exe.py      # 打包脚本
│   ├── check_secrets.py  # 提交前密钥扫描
│   ├── test_cli.py       # CLI 测试
│   └── ...
├── .local-test/           # 本地临时脚本（探针 / 实验 / bug 复现），整目录不入库
├── data/                  # 运行时数据（不入库，程序自建）
│   ├── data/             # 记忆数据（SQLite + 向量库）
│   ├── logs/             # 日志
│   └── hy_mem_queue.db   # 异步任务队列
└── AGENTS.md             # 项目交接文档
```

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
python scripts/test_cli.py
python scripts/test_ui.py
```

## 注意事项

- **exe 与源码是两套完全独立的环境**，修改代码后需重新打包
- 写入记忆时建议**一条只放一个主题**，保持粒度清晰
- 异步写入仅入队，**入队 ≠ 入库**，需通过 worker 消费队列
- 密钥仅支持 ASCII 字符

## 版本策略

CLI 独立 semver，不与底层 hy-memory SDK 版本耦合（SDK 版本见 `hy-mem doctor`）。当前为 **1.0.0-rc.1**（Release Candidate）：功能面完整、测试矩阵全绿，进入发布候选；自用验证稳定后转正 1.0.0。

## 许可证

请查看项目根目录下的许可证文件。