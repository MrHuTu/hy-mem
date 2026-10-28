# setup-key-401 · 契约（唯一事实源）

> 提出人：胡超　｜　执行：阿衡　｜　日期：2026-10-28
> 类型：缺陷修复　｜　状态：**完成**（产物 0.9.12 已打包）
> 落地目录：`docs/specs/2026-10-28-setup-key-401/`
> 备注：本目录为**事后补录**，按 `skill/hy-dev` 规范反向整理，作为模板的活样例。

> **本文件是唯一事实源。** `§1` 只由「澄清」修改，`§2` 只由「执行」修改。

---

## 1. 契约区

### 1.1 现象

设置 → `[1/4] 向量模型` → 选「当前项」→ 向导追问 `API Key:` → 用户直接回车 →
`✗ HTTP 401: No API-key provided.`，而环境变量 `$MEMORY_EMBEDDER_API_KEY` 里有一把有效 key。

### 1.2 复现（真 CLI 子进程，非 TTY 自动降级编号菜单）

```bash
.venv/Scripts/python.exe scripts/drive_setup.py /d/tools_open/tmp-401 embedding "1\n\n"
# 修前：› API Key:  →  ✗ HTTP 401: No API-key provided.
```

### 1.3 条款（正确行为）

| 编号 | 约定 | 落地位置 | 判据 |
|---|---|---|---|
| C1 | **密钥只有一个来源**：`config.resolve_embed_key()` / `resolve_llm_key()`。任何"有没有 key"的判断、任何请求头构造，都必须走它，不许直接读 `emb["apiKey"]` | `cli/config.py` | 全仓 `grep 'apiKey'` 无判据类用法 |
| C2 | 取不到 key 时**不发请求**，直接报「密钥为空（config.json 与 $变量名 都取不到）」 | `cli/setup_wizard.py:probe_embedding` | 无 key 环境下 0 次出站请求 |
| C3 | 选「当前项」或同端点换模型时**不追问 key**，沿用环境变量、不落盘 | `cli/setup_wizard.py:step_embedding` | `drive_setup.py … embedding "1\n"` 不出现 `API Key:` 提问 |
| C4 | 继承到 key 时，`keyEnv` 必须回写成**实际取到 key 的那个变量名**（预设声明的变量名可能与本机不同：预设 `$YSJX_API_KEY`，本机 `$MEMORY_LLM_API_KEY`） | `cli/presets.py` | 保存后 `resolve_*_key()` 仍返回该 key |
| C5 | 展示层「已设置/未设置」也看解析结果，不看 `apiKey` 字段 | `config show` / `menu` 设置页 / 向导摘要 | 显示「已设置（来自 $MEMORY_EMBEDDER_API_KEY）」 |

### 1.4 不改的边界

- 不动 `netprobe` 的 `Sent` / `NotSent` 语义与退出码。
- 不恢复"明文 key 落盘"的旧行为（0.9.11 起密钥只留变量名，这是有意的）。
- 不改 LLM 步骤的交互流程，只对齐它的密钥判断方式。

---

## 2. 分片表（根因即分片）

| # | 分片 | 文档 | 改动文件 | 依赖 | 状态 | commit |
|---|---|---|---|---|---|---|
| R1 | 探针绕过 keyEnv 解析 | `R1-probe-bypass.md` | `cli/setup_wizard.py` | — | ✓ | `a1a7617` |
| R2 | 判据用 apiKey 字段 | `R2-apiKey-judgement.md` | `cli/setup_wizard.py` | — | ✓ | `a1a7617` |
| R3 | to_cfg 丢失/冲掉 keyEnv | `R3-to-cfg-keyenv.md` | `cli/presets.py` | — | ✓ | `a1a7617` |
| R4 | 三处展示误报「未设置」 | `R4-display-mask.md` | `cli/hy_mem_cli.py`、`cli/menu.py`、`cli/setup_wizard.py` | R1 | ✓ | `a1a7617` |
| R5 | 首次安装（无 config.json）密钥判据仍失效 | `R5-first-install-env.md` | `cli/setup_wizard.py`、`scripts/test_setup.py` | R1 | ✓ | 见 §4 |

**共同根因**：密钥这一事实没有单一真源 —— 三处各自判断，三处都判断错。这正是要立 C1 的原因。

---

## 3. 变更记录

| 日期 | 改了什么 | 为什么 | 影响分片 | 是否重走澄清 |
|---|---|---|---|---|
| 2026-10-28 | 新增 R4（展示层误报） | 修完请求链路后，用户仍看到「嵌入密钥 未设置」，与事实不符 | R4 | 否 |
| 2026-10-28 | 新增 R5（首次安装路径） | 打包前用 exe 在空沙箱复测，发现 `setup --step llm` 提示「将继承当前密钥」却报「✗ 密钥为空」。根因是 `_fill_from_env()` 把环境变量值复制进内存 `apiKey`，使 `not llm.get("apiKey")` 判据失效。**C1-C5 未变**，属执行期补漏 | R5 | 否 |

---

## 4. 追溯表

| 条款 | 分片 | commit | 产物 | 验收证据 |
|---|---|---|---|---|
| C1 | R1+R2+R3 | `a1a7617` | 0.9.12 | `drive_setup.py … embedding "1\n"` → `✓ qwen3.7-text-embedding-flash 可用，1024 维` |
| C1 | R5 | 见 `git log --oneline -- cli/setup_wizard.py` | 0.9.12 | exe 空沙箱 `setup --step llm` → `✓ deepseek-v4-flash 可用，375ms`，无「密钥为空」 |
| C2 | R1 | `a1a7617` | 0.9.12 | `test_setup` Q 节「无 key 时不发请求」 |
| C3 | R2 | `a1a7617` | 0.9.12 | `test_setup` Q 节「二次配置不追问」 |
| C4 | R3 | `a1a7617` | 0.9.12 | `test_setup` Q 节「换预设继承且 keyEnv 不丢」 |
| C5 | R4 | `a1a7617` | 0.9.12 | `hy-mem config show` → `已设置（来自 $MEMORY_EMBEDDER_API_KEY）` |

---

## 5. 结论

- 测试：`test_setup` 103 → **133**（Q 节「第二次配置」+ Q6/Q7 首次安装路径）；九套 **451** 全绿；`check_secrets` 0 ERROR。
- 黑盒复跑（**打包后的 exe**，0.9.12）：
  - 空沙箱首次配 LLM：不追问 key，`✓ deepseek-v4-flash 可用，375ms`；
  - 连续两次配向量模型（首次 + 第二次）：都不追问，`✓ 1024 维 / 345ms`、`✓ 1024 维 / 310ms`；
  - 落盘结构：`embedding.apiKey` / `llm.apiKey` 均为空，`keyEnv` 指向真实变量名。
- 实际产物：`dist/hy-mem-0.9.12-win64.zip`（178MB），干净包解压冒烟 `hy-mem 0.9.12` 通过；
  `HY_MEM_TEST_EXE` 下 `test_cli` 84/84。
- 沉淀：`AGENTS.md §8.1` 四条铁律 + `skill/hy-dev` 流水线；`scripts/drive_setup.py`（黑盒驱动，已转正入库）。
- 遗留：`DEFAULTS.llm.keyEnv = "YSJX_API_KEY"` 保持原样（见 `R5-first-install-env.md` §4）。
