# 契约：evolve 归纳语言可控（evolve.language）

- 父级：无（独立轻需求，S 级）
- 日期：2026-10-30
- 状态：**已确认（用户拍板方案 B，2026-10-30 19:22）**

## 背景与根因（引 docs/specs/2026-10-30-l6-schema-empty/）

SDK hy-memory 2.0.5 的 System2 归纳语言由 `_detect_language()` 自动检测：
`中文字符 ÷ 全部非空白字符 ≥ 0.7 → zh 提示词`。技术型中文记忆（路径/时间戳/
英文术语密集）实测 zh_ratio=0.318 → 判 en → deepseek-v4.1-flash 输出英文
schema → 中文 query 语义分 0.17~0.34，跨语言低分不可检索。且 zh 提示词本身
无「必须用中文输出」的显式指令（无兜底）。已向上游提 issue，等 SDK 修复前
在 hy-mem 侧自保（方案 B：运行时补丁，不改 vendor）。

## R1 配置项 `evolve.language`

- 三值：`""`（auto，跟随 SDK 检测，默认=现状行为）/ `"zh"` / `"en"`
- 校验：非法值报错；缺 key 按 `""` 处理（向后兼容旧 config）

## R2 语言指令运行时补丁（evolveops）

- 注入点：monkey-patch `hy_memory.pipelines.system2_agent._build_single_call_system_prompt`
  （SDK 单调用路径 L2125 唯一消费点），在其返回值尾部追加一句语言指令
- zh 指令：「所有 create_schema / revise_schema 的 content 与 retrieval_text
  必须用简体中文书写；专有名词与技术标识（路径/命令/术语）可保留英文」
- en 指令：对偶英文句
- **软降级**：SDK 升级若函数改名/缺失 → 返回 warning（进 `language_note`），
  evolve 照常跑（退回 SDK 默认行为），不抛异常
- 重复调用幂等：改配置/传参后重新生效，不叠加追加
- 强制语义：显式指定时**无视检测结果**（en 提示词选中也追加 zh 指令）——
  这正是它存在的意义（检测不可靠才需要强制）

## R3 两端接线

- CLI：`hy-mem evolve [--language zh|en]`，不传走 `config.evolve.language`
- MCP：`evolve` 工具新增可选参数 `language`，不传走配置；inputSchema 与
  工具描述同步（说明什么时候用：技术型中文记忆被归纳成英文 schema 时）
- 优先级：显式参数 > config > auto

## R4 结果可见性

- `evolve()` 返回新增 `language`（实际生效值）与 `language_note`（补丁失败
  warning，成功为空）；CLI 非 JSON 输出有 warning 时走「提示」行

## 追溯链

| 条款 | 分片 | commit | 产物/证据 |
|---|---|---|---|
| R1-R4 | S1 | 待填 | test_evolve F 节 6 项 + C1 补 1，14 套全绿 |
| 文档同步 | S2 | 待填 | README×2 / SKILL.md / helptext / AGENTS.md 矩阵 |

## 验收口径

red→green closure；不追求真模型端到端（语言指令是纯提示词注入，F 节离线
桩测补丁行为即可）；版本 bump 1.0.0-rc.5。

## 变更记录

- 2026-10-30 立。用户确认方案 B（等上游 SDK 修复期间的侧写自保）。
