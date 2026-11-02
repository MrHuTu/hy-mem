# 契约：U3 端点失效自愈（MCP 长驻 client 重建 + serve 固定端口）

- 父级：无（独立 bug 修复）
- 日期：2026-11-02
- 状态：**已完成（2026-11-02，red→green closure + 全量 14 套全绿，commit d575778）**
- 关联：`docs/UPSTREAM-ISSUES.md` U3；前置事故链见 `2026-11-02-ultra-graph-degrade/`

## §1 问题（一句话）

serve 空闲自退后重拉必换端口（`vdb_endpoint.py` `bind((host,0))` 随机分配），
而 SDK client 构造期一次性冻结 `MEMORY_VECTOR_HOST/PORT`（SDK `config.py`
`__post_init__`），MCP 长驻进程的 `_client()` 单例又不失效重建——serve 重启后
MCP 工具层永远连旧死端口（ConnectError 10061）。CLI 短进程每次新构造所以无恙，
故障只打击长驻进程。

责任拆分（2026-11-02 已逐行核实）：主因在我们两处设计（随机端口 + 单例不失效）；
SDK 构造期读 env 属设计限制（enhancement 级，非恶性 bug，随 issue 附带建议）。

## §2 修复（两片，方案 2026-11-02 用户确认）

### S1 —— MCP `_client()` 失效重建（治本）

`mcp/server.py`：取单例前做廉价端点复验 `_endpoint_stale()`：

1. 只对**自己注入的 env** 生效（`SRC_ENV == SRC_VALUE`）；外部显式配置不干预，
   嵌入式（无 env）恒 False；
2. env 端口心跳死（0.5s 超时，回环拒连即时返回）→ 失效；
3. 端口文件登记了另一个活着且心跳可达的端口 → 失效（换端口场景）。

失效则 `_close_client()` → 重新 `make_backend()`（内部重跑 `ensure_endpoint()`，
重发现/重拉 serve 并重注入 env；SDK 构造期读 env，重建即拿到新端口）。

### S2 —— serve 固定端口（加固，双保险）

- `config.json` 顶层新增 `servePort`（0 = 随机，历史默认）。
- `vdb_endpoint.start_server_thread(persist_dir, host, port=None)`：port 有值先试
  绑定，`OSError`（占用）则 stderr 告警 + 回退随机。
- `cmds/queue.py cmd_serve` 读 `servePort` 传入。
- 端口固定后长驻 client 冻结端点恒有效，S1 兜底其余场景（占用回退、serve 重装）。

## §3 验收

- red：`_endpoint_stale()` 补丁前不存在/恒 False → 单例永不重建。
- green：test_vdb_endpoint 新增固定端口两态（占用回退/生效）；test_mcp 新增
  staleness 判定（无 env / 死端口 / 活端口 / 外部配置四态）+ 单例重建路径静态守护。
- 全量 14 套回归；重打包覆盖安装 + exe 冒烟。

## §4 追溯链

| 条款 | 分片 | commit | 产物 | 验收证据 |
|---|---|---|---|---|
| §2 S1 | S1 | d575778 | mcp/server.py `_endpoint_stale` + `_client` 重建 | test_vdb_endpoint U3 四态 + 重建守护全绿（44/44） |
| §2 S2 | S2 | d575778 | config `servePort` + start_server_thread 前缀绑定 + cmd_serve 传入 | test_vdb_endpoint S2 两态全绿（固定生效/占用回退） |
| §3 守护 | S3 | d575778 | test_vdb_endpoint 新断言 ×8 | 全量 14 套全绿（cli 122 / vdb 44 / mcp 35 等） |
| §1 red | R1 | d575778（判定函数可单测，测试直接入 tests/） | tests/test_vdb_endpoint.py U3 断言 | 补丁前无 _endpoint_stale（AttributeError）；补丁后四态全绿 |
