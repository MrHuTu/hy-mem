# SDK 2.0.11 升级：evolve 语言走原生通道，退役 rc.5 运行时补丁

日期：2026-11-02 ｜ 父级契约：无 ｜ 状态：待确认

## 背景

rc.5 时 SDK（2.0.5）的 System2 归纳语言靠 `_detect_language` 自动检测
（zh_ratio≥0.7），对技术型中文记忆结构性失灵（实测 0.318 判 en → 英文
schema → 中文 query 检索 0.17~0.34 被阈值滤空）。当时已提上游 issue，
hy-mem 侧打运行时补丁：包装 `_build_single_call_system_prompt`，尾部追加
自造的输出语言指令（`evolve.language` 配置 / CLI `--language` / MCP language）。

现 SDK 升到 2.0.11，用户确认原生已解决。

## 源码核查结论（2.0.11 实测）

| 点 | 2.0.11 现状 | 对我们的影响 |
| --- | --- | --- |
| 显式语言 | `client.evolve(language=...)` + `MemoryConfig.language`（config `language` / env `MEMORY_LANGUAGE`）原生支持；`normalize_language` 认 11 种语言 + 别名（**`zh`→zh-Hans、`en` 合法**） | 我们 config 的 `evolve.language=zh/en` 可直接透传 |
| 强制输出语言 | 显式时 `with_output_language(...)` 追加 `OUTPUT_LANGUAGE_DIRECTIVE`：「所有自然语言内容一律用 <语言> 书写，无论对话或已有记忆使用什么语言…此规则优先于任何『输出与输入一致』要求」 | **比我们的补丁更强**：SDK 同时把提示词本体也换成该语言（我们只追加指令，提示词本体仍可能被检测成 en）；且指令覆盖记忆正文/摘要/理由/查询全部字段 |
| auto 检测 | `pipelines/system2_agent._detect_language` **仍是旧 0.7 阈值**（L2330-2341 逐字未改）；新的 CJK 启发式（`utils/lang_detect.py`）只用于 add 抽取路径 | **上游 issue 只修了一半**：显式模式已解决，auto 模式技术型中文依然判 en → hy-mem 的 `evolve.language` 配置（显式指定）仍是推荐用法，README 口径不变 |
| 抽取路径 | `agent/extractor.py` 等也接了 `resolve_language` + directive | hy-mem 的 `evolve.language` 只管 evolve，不动 add 抽取（语义不变） |

## 分片

### S1 evolveops 退役补丁，语言走原生通道

- 删除 `apply_language_directive` / `_LANG_DIRECTIVES` / `_LANG_STATE`（运行时
  monkey-patch 整段退役）。
- `evolve()` 改为：
  - `lang_eff` 解析不变（显式参数 > config `evolve.language`）。
  - 显式 zh/en：先经 `hy_memory.prompts.normalize_language` 预校验——
    ValueError → `language_note` 带警告、不传 language（软降级，不炸 evolve）；
    合法 → `client.evolve(language=lang_eff)` 原生透传。
  - 软降级兜底：`client.evolve(language=...)` 抛 TypeError（老版本 SDK 无此参）
    → 去掉 language 重试一次，`language_note` 注明。
  - auto（空串）：不传 language，完全跟随 SDK 检测（行为与现在一致）。
- `language` / `language_note` 返回字段语义不变（CLI/MCP 消费方零改动）。

### S2 配套收尾

- `requirements.txt`：`hy-memory==2.0.5` → `2.0.11`。
- `cmds/maint.py` cmd_evolve 输出行「已注入输出语言指令」→「SDK 原生输出语言约束」。
- README×2 / SKILL.md / AGENTS.md 中「运行时补丁」表述改为「SDK 原生 language 通道」，
  保留「auto 检测对技术型中文仍失灵、建议显式配 evolve.language=zh」的口径。

### S3 测试改写（F 面）

- F1 auto → evolve 收到**无 language**（原生透传路径不触发）。
- F2 zh / F3 en → client.evolve 收到 `language="zh"/"en"`。
- F4 非法值（如 fr 后传 fr？fr 本身合法——改为非法值 "xx"）→ warning + 不传。
- F5 老版本软降级：evolve 抛 TypeError → 去参重试 + note。
- F6 配置默认、F7 evolve_status schema 保持不动。
- D 面（真模型）不动，顺带验证 2.0.11 返回结构兼容（`results.system2_agent`）。

## 不做什么

- 不动 MCP 工具面（evolve 已在 rc.6 移除，evolve_status 无语言参数）。
- 不动 add 抽取路径的语言行为（`evolve.language` 语义保持「只管 S2 归纳」）。
- vendor/ 目录快照不同步（仅参考用，运行时走 site-packages）。
- 不给上游 auto 检测再打补丁——显式配置已足够，等上游修 0.7 再回归 auto。

## 验收口径

red→green closure（test_evolve F 面改写）+ 14 套件全绿 + check_secrets 零 ERROR。

## 追溯链

| 条款 | 分片 | commit | 验收证据 |
| --- | --- | --- | --- |
| 原生语言通道 | S1 | d7829cb | test_evolve F1–F5 PASS + 冒烟四路（透传/非法值/降级/auto） |
| 配套收尾 | S2 | d7829cb | requirements 2.0.11 + 文档三处同步 |
| 测试改写 | S3 | d7829cb | test_evolve 80/80（含真模型 D 面确认 2.0.11 返回结构兼容）+ 14 套件全绿 + check_secrets 0 ERROR |

## 执行记录（2026-11-02）

- 桩测四路冒烟全对：zh 透传 / xx 非法值警告 / 老版本 TypeError 去参重试 / auto 不传；
  own=False 路径的 llm 覆盖跑完正确还原。
- 真模型 D 面（tongyi-xiaomi-analysis-flash）在 2.0.11 下跑通，返回键
  `results.system2_agent` 等结构与 2.0.5 兼容，summarize 零改动。
