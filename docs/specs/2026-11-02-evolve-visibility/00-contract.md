# 契约：evolve 可见性补全 + MCP 工具描述精简

日期：2026-11-02　·　分道：L2 功能（S1/S2）+ L1 修复（S3 文案）　·　版本：1.0.0-rc.7

## 背景

rc.6 把 L6 沉淀改成全自动并移除了 MCP evolve 工具，留下两个缺口：

1. **异常发现不了**：MCP 侧只有 `evolve_status`（ultra 专属），CLI 没有对应命令；
   连续失败、冷却卡死这类 L6 异常没有任何日常可见的出口。
2. **工具描述太繁琐**：MCP tools/list 的 description 动辄数百字，占宿主上下文，
   核心信息被淹没。

## 唯一事实源

本文件。代码注释、README、SKILL.md 与此对齐。

## 分片

### S1 CLI `evolve-status` 命令（commit: feat(cli)）

- `hy-mem evolve-status [-n N] [--json]`：读 `autoevops.status_summary()`。
- **所有模式可看**（不拦 pro）：pro 下输出注明「自动沉淀仅 ultra 启用」——
  观察窗口不该要求先切模式。
- 人类可读输出：模式 / 阈值 / 连续失败 / 基线事实 / 最近成功 / 下次可尝试 /
  最近 ≤10 条记录（含 skipped/failed 的 error 摘要）。
- 测试：test_evolve.py 新增 H 面（pro/ultra JSON 结构、human 提示、故障注入）。

### S2 MCP `get_version` 附带 evolve 异常信号（commit: feat(mcp)）

- 仅 ultra 且 `consecutive_failures > 0` 时，payload 带 `evolve_health`：
  `{consecutive_failures, note}`，note 指路 `evolve_status`。
- 正常时**不出现**该键（零噪声）；读的只是小状态文件，微秒级。
- 先例：queue_health（shadow_only/dead 顺带带出，同款设计）。
- 测试：注入失败状态 → get_version 出现 evolve_health；清空 → 不出现。

### S3 MCP 工具描述精简（commit: feat(mcp)，随 S2 同 commit）

- 全部 9 个工具（TOOLS + ULTRA_TOOLS）的主 description 压到 ≤200 字：
  保留「做什么 + 什么时候调 + 关键红线」，背景故事/实测数据/设计理由
  下沉到 inputSchema 属性描述或代码注释。
- inputSchema 属性描述同步去冗（最长的 async/messages/chain_block 三处）。
- 行为零变化：工具名、参数、返回结构全部不动；测试钉名称与 schema 键，
  另加一条回归钉「主描述 ≤200 字」。

## 追溯链

| 条款 | 分片 | commit | 验收证据 |
| --- | --- | --- | --- |
| CLI 状态命令 | S1 | 0cf1c9c | test_evolve H1–H4 PASS |
| get_version 信号 | S2 | 0cf1c9c | test_evolve H5–H6 PASS |
| 描述精简 | S3 | 0cf1c9c | test_evolve H7 PASS + 全套件绿 |

## 验收口径

red→green closure（test_evolve 69→80 项，H 面含故障注入与零噪声双向断言）
+ 14 套件全绿（cli 97 / ui 110 / menu 113 / setup 148 / setup_ui 41 / vdb 36 /
mcp 31 / queue 21 / oplog 25 / i18n 31 / encoding 10 / scene 43 / evolve 80
= **817**）+ check_secrets 0 ERROR。执行记录：一次返工（H5 首跑失败——
evolve_health 块误插在 payload 构造前，NameError 被 except 吞掉，移到
payload 之后即绿），终态 80/80。
