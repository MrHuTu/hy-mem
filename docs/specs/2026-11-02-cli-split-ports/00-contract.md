# 契约：CLI 按域拆包 + MemoryBackend 协议层（JDBC 式）

日期：2026-11-02　·　分道：L3 结构重构　·　版本：1.0.0-rc.8（拟）

## 用户拍板原文

> 按你推荐去拆分文件（按域分包），以后 AI 读功能相关的代码不需要加载大量无关代码。
> 注意：现在我们使用 hy-memory 去实现，后续可能会换其他开源实现（如 memvid），
> 所以设计时应该抽象出功能函数或接口，具体实现由核心 SDK 去做——
> 类似 JDBC：JDBC 定协议，实现由不同厂家去做。

## 设计原则（JDBC 类比）

| JDBC 概念 | 本项目对应 |
| --- | --- |
| JDBC 协议（接口） | `hymem/ports/memory.py` 的 `MemoryBackend` Protocol——**不 import vendor**，纯协议 |
| 驱动（厂家实现） | `hymem/backends/hymemory.py` 适配器——包装 hy_memory client，唯一 import vendor 的地方 |
| DriverManager | `hymem/ports/registry.py`——按 `config.backend.memory`（默认 `"hy-memory"`）选实现；未来注册 `"memvid"` 只加文件不改调用方 |
| 应用代码（CLI/MCP） | 只认 Protocol，`make_backend()` 拿到的对象只暴露协议方法 |

**关键约束**：vendor 的 import 全部收敛到 `hymem/backends/`（现状散在 cli/core 十几个文件里，
含 `asyncio.run` 调 SDK 异步方法、`_get_chain_store` 私有访问——这些脏活全部进适配器）。

## 实测依据（2026-11-02，全仓口径——用户追问「协议是否含 MCP 能力」后修订）

- **公开方法面 = 16 个**（CLI 15 + MCP 6 全是子集 + UI 零直连，grep 三层实测 union）：
  add/get/update/delete/delete_all/search/list_memories/history/get_recent_history/
  finalize_session/get_write_outcome/get_user_basic_info/get_metrics/evolve/close/
  _get_chain_store。
- **私有触点旁路（必须协议化，否则换实现全是暗桩）**：
  - `autoevops.py:135` → `client._graph_store`（数 L2 事实）
  - `evolveops.py:296,373-375` → `client._config.llm`（临时换归纳模型）
  - `searchops.py:334` → `client._get_chain_store`（coding diff 链补丁）
  - `two_phase.py:88,93` → `client._registry / _loop_thread`（写锁内提交段）
- vendor import 分布（收敛目标，共 6 文件）：cli.py、core/{autoevops,clientops,
  evolveops,searchops,two_phase}.py。
- vendor 自身单文件最大 7733 行（client.py）——**拆分目的不是行数达标，是「按域定位 +
  为换实现留缝」**。

## 协议定义（全量能力面，不分层取舍）

`MemoryBackend` Protocol 三段：

1. **基础记忆操作**（CLI/MCP 共用）：add/get/update/delete/delete_all/search/
   list_memories/history/get_recent_history/finalize_session/get_write_outcome/
   get_user_basic_info/get_metrics/close
2. **沉淀能力**：evolve
3. **内部能力协议化**（vendor 私有触点收编为语义化方法，memvid 未来按各自机制实现）：
   - `count_facts(user_id, layer, status) -> int`（替代 `_graph_store` 直摸）
   - `llm_model_swap(model, max_tokens)` 上下文管理器（替代 `_config.llm` 临时改写）
   - `chain_diff_blocks(...) -> list`（替代 `_get_chain_store` + decorate 补丁）
   - `submit_in_writer_lock(fn)`（替代 `_registry/_loop_thread` 锁内提交段）

## 分片

### S1 ports 协议层 + 适配器归位（零行为）

- `src/hymem/ports/__init__.py`：`make_backend(cfg)`（按 config.backend.memory 选实现，默认 hymemory）。
- `src/hymem/ports/memory.py`：`MemoryBackend` Protocol（上述三段全量签名 + 语义 docstring）。
- `src/hymem/ports/registry.py`：实现注册表 `{name: factory}`，未知 backend 名明确报错
  （提 memvid 时只改这一处注册）。
- `src/hymem/backends/hymemory/`：**适配器包**——现有 core 的 vendor 触点整编归位：
  - `clientops.py` / `searchops.py` / `evolveops.py` / `two_phase.py` / `autoevops.py`
    **搬家不改逻辑**，私有触点改为调用适配器的协议方法实现；
  - `hymem/core/searchops.py` 等旧路径变 re-export 门面（tests/其他引用零感知）。

### S2 cli.py 按域拆包

- `src/hymem/cli/` 包（原 cli.py 4078 行拆入，**原 `hymem/cli.py` 变成兼容门面**：
  re-export 全部公开名，`python -m hymem`、tests 的 `import hymem.cli`、
  `hymem.cli:main` 入口引用零变化）：
  - `base.py`：ok/fail/EXIT_*/require_ready/load_env/readiness/make_backend 等助手
  - `cmd_write.py`：add/update/delete/forget/tags/archive（写路径）
  - `cmd_read.py`：search/list/get/history/profile/outcome（读路径）
  - `cmd_maint.py`：evolve/evolve-status/doctor/stats/log（维护诊断）
  - `cmd_queue.py`：serve/worker/queue/drain/retry（异步队列）
  - `cmd_config.py`：config/setup/user/storage/migrate/install（配置向导）
  - `parser.py`：argparse 组装（_Parser + build_parser）
  - `__init__.py`：门面 + main()
- 测试文件**不动**（它们只 import hymem.cli 顶层名）。

### S2.5 MCP 层接入协议（消灭工厂旁路）

- `mcp/server.py` 的 `make_client()` 调用点替换为 `make_backend()`（类型注解同步）。
- MCP 业务逻辑不动——它调用的 6 个方法本就是协议子集，换工厂即接入。

### S3 收尾验证

- 14 套件全绿（行为零变化的硬标准）+ check_secrets 0 ERROR。
- AGENTS.md / SKILL.md 结构图同步；契约追溯链回填。
- **守护断言进 test_cli**：全仓除 `ports/` 与 `backends/` 外——
  ① 不得 `import hy_memory`；② 不得访问 `client._<私有>`；③ 不得 `asyncio.run` SDK 调用。
  三条断言钉死「只抽一半」的回潮。

## 不做什么（防过重）

- **不引入抽象基类/运行时校验**：用 `typing.Protocol`（结构化鸭子类型），
  适配器不需要继承任何东西，memvid 未来只要长出同名方法就能接上。
- **不搬业务逻辑**：core ops 的搜索调参/两段式写/语言补丁逻辑原样保留，
  只把 vendor 触点换成协议方法调用（搬家归位，不改算法）。
- **不改任何行为**：拆完 `--help` 输出、命令行为、返回码应逐字节一致（test_cli 97 项守着）。
- **queue/db 层不进协议**：队列是 hymem 自有资产（sqlite），与记忆实现无关；
  get_version/queue_status 不依赖 backend，无需协议化。

## 风险与对策

| 风险 | 对策 |
| --- | --- |
| 循环 import（commands↔base） | base 不 import 任何 cmd 模块；parser 单向 import cmd |
| 遗漏私有引用（tests 直挖 cli._xxx / core.searchops 旧路径） | 门面 re-export 全量公开名；core ops 旧路径变 re-export 门面 |
| 私有触点协议化后行为漂移 | 适配器实现体 = 原 core ops 代码原样搬入，diff 只在 import 与签名包装 |
| PyInstaller 打包漏新包 | hy-mem.spec 按包路径自动收集，打包后 doctor + exe test_cli 验证 |
| vendor 触点漏网 | test_cli 三条守护断言（import / 私有访问 / asyncio.run） |

## 追溯链

| 条款 | 分片 | commit | 验收证据 |
| --- | --- | --- | --- |
| 协议层（全量能力面） | S1 | 6e819d7 | test_cli G1 四项断言 PASS |
| CLI 拆包 | S2 | 98981ed | 14 套件绿（819 项）+ help/--version 冒烟一致 |
| MCP 接入协议 | S2.5 | 98981ed | test_mcp 31/31 + test_scene 43/43 绿 |
| 收尾 | S3 | 98981ed（守护断言随 test_cli） | check_layering L1–L5 通过 + check_secrets 0 ERROR |

## 执行偏差记录（2026-11-02 实际执行）

1. **拆分目标包名 `hymem/cmds/`（契约原写 `hymem/cli/`）**：分层守卫 L4 强制所有
   `__init__.py` 为空，而 `hymem.cli` 必须继续是可 import 的门面模块
   （pyproject 入口 `hymem.cli:main`、`__main__.py`、`_pyi_entry`、tests 依赖），
   因此包名让位：`cli.py` 保留为门面（79 行），实现进 `hymem/cmds/` 八模块。
2. **`__getattr__` 透传**：HyMemBackend 对未显式列出的属性（含 SDK 私有成员）
   原样转发底层 client——迁移期 ops 助手与调用点零改动；换 backend 时未实现的
   面会当场 AttributeError，不会静默走错。
3. **能力方法消费方**：count_facts 等 4 个内部能力当前只在适配器内部被消费
   （ops 模块整体归位 backends/ 后外部无人再摸私有成员），Protocol 里保留它们
   是为 memvid 等未来实现定义契约面。
4. **doctor 的 `import hy_memory` 收敛为 `clientops.sdk_info()`**：守护断言 G1
   首跑抓到 cmds/maint.py 的 vendor import，按规则收进适配器（这正是断言的用途）。
5. **一次返工**：entry 的 `global _QUIET,...` 语句被全局名替换误改写
   （`_base.global ...` 语法错）——拆分脚本对 `global` 语句要整行删除而非替换。
6. **AGENTS 目录地图**：原地图仍是 cli\hy_mem_cli.py 旧布局，本次重画为 src/hymem 真实结构。

## 验收口径

14 套件 817 项全绿（计数不变——不加测试逻辑，只加 import 约束断言）+
check_secrets 0 ERROR + 打包后 exe 冒烟（test_cli + doctor）。
