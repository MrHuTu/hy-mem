# S1 分层重构契约 · 2026-10-29

## §1 需求（只由澄清改）

### 背景
用户判断：目录结构不符工程化，未来可能拓展桌面软件、MCP HTTP 协议、对外团队服务。
方案已确认：只做 S1（分层），S2（pyproject/src）、S3（拆单体）、S4（HTTP transport）按需另立项。

### 目标结构（cli/ 内部三层子包）
```
cli/
├── hy_mem_cli.py      # 入口不变（3486 行拆解是 S3，不在本期）
├── _pyi_entry.py      # PyInstaller 入口不变
├── __init__.py        # 新增（包化）
├── core/              # 核心域：无 UI 依赖，桌面/服务复用层
│   ├── __init__.py
│   ├── config.py      ← config
│   ├── errors.py      ← errors
│   ├── netprobe.py    ← netprobe
│   ├── oplog.py       ← oplog
│   ├── presets.py     ← presets
│   ├── runtime_check.py ← runtime_check
│   ├── storage_ops.py ← storage_ops
│   ├── store.py       ← store
│   ├── vdb_endpoint.py ← vdb_endpoint
│   └── local_embedder.py ← local_embedder
├── ui/                # 终端交互层
│   ├── __init__.py
│   ├── helptext.py    ← helptext
│   ├── i18n.py        ← i18n
│   ├── menu.py        ← menu
│   ├── setup_wizard.py ← setup_wizard
│   ├── skill_ops.py   ← skill_ops
│   └── tui.py         ← tui
└── mcp/
    ├── __init__.py
    └── server.py      ← mcp_server
```

### 分层铁律（CI/评审判据）
- **L1**：`core` 不 import `ui`/`mcp`/`hy_mem_cli`（含函数内延迟 import）
- **L2**：`ui` 可 import `core`；`mcp` 可 import `core`；`ui` ↔ `mcp` 互不 import
- **L3**：`hy_mem_cli.py` 可 import 全部；其余文件不得 import `hy_mem_cli`
    - 例外存量：`mcp/server.py` 与 `store.py` 有 `from hy_mem_cli import ...`（S3 拆解时消除），本期保留并记录
- **L4**：`cli/__init__.py` 必须为空（防 import 副作用串包）

### 验收（§2 执行时逐条落证据）
- A1：633 项测试全绿（11 套不变）
- A2：`python -m cli.hy_mem_cli version` / `menu` / `setup` / `mcp` 四入口冒烟通过
- A3：打包 build_exe 成功且 exe 冒烟（--version、doctor 三项）
- A4：分层扫描脚本全过（新增 `scripts/check_layering.py`，判 L1–L4，转正入发版门禁）
- A5：对既有用户零感知：启动器、mcp.json、快捷方式、数据目录均不变

### 例外与不做
- `mcp/server.py` 内 `from hy_mem_cli import _parse_time` 等三符号（S3 消除，本期记录）
- `store.py` 若有对入口的反向 import（S3 消除）
- 不动 `scripts/` 结构、不引入 pyproject（S2）、不拆入口（S3）

## §2 执行记录（只由执行改）

| 分片 | 内容 | commit | 状态 |
| --- | --- | --- | --- |
| S1-1 | 建三层子包骨架 + 迁 12 个 core 文件 + 修全量 import（含入口 bootstrap） | （本轮单 commit） | 完成 |
| S1-2 | 迁 ui 6 文件 + mcp/server.py + 修依赖（VERSION 挪 core/version，errors 翻译钩子） | （本轮单 commit） | 完成 |
| S1-3 | check_layering.py 扫描器（L1–L5）+ 转正入 scripts | （本轮单 commit） | 完成 |
| S1-4 | build_exe 适配（--paths/hidden-imports 15 项包名化）+ exe 冒烟三项通过 | （本轮单 commit） | 完成 |
| S1-5 | 633 全量回归（12 套全绿）+ 文档同步 | （本轮单 commit） | 完成 |

## §3 追溯
| 条款 | 分片 | commit | 产物 | 验收证据 |
| --- | --- | --- | --- | --- |
| A1 633 全绿 | S1-5 | 本 commit | 12 套 | cli 84 / ui 110 / menu 112 / setup 139 / setup_ui 40 / vdb 36 / mcp 25 / queue 21 / oplog 25 / i18n 31 / encoding OK / concurrency PASS；check_layering L1–L5 无违例 |
| A2 入口冒烟 | S1-1 | 本 commit | 源码 | version / menu / setup / mcp 四入口直跑与 -m 模式均通过 |
| A3 打包冒烟 | S1-4 | 本 commit | exe | --version=1.0.0-rc.1；mcp 握手 ready；doctor llm_live 1319ms PASS |
| A4 分层守卫 | S1-3 | 本 commit | scripts/check_layering.py | L1–L5 全过（mcp→入口 12 处 + vdb_endpoint 1 处为契约记录的 S3 存量例外） |
| A5 用户零感知 | S1-1 | 本 commit | 启动器/mcp.json/数据目录 | 均未动 |
