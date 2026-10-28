# setup-key-401 · 契约（唯一事实源）

> 提出人：胡超　｜　执行：阿衡　｜　日期：2026-10-28
> 类型：缺陷修复　｜　状态：**完成**（源码已修；产物待打包）
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

**共同根因**：密钥这一事实没有单一真源 —— 三处各自判断，三处都判断错。这正是要立 C1 的原因。

---

## 3. 变更记录

| 日期 | 改了什么 | 为什么 | 影响分片 | 是否重走澄清 |
|---|---|---|---|---|
| 2026-10-28 | 新增 R4（展示层误报） | 修完请求链路后，用户仍看到「嵌入密钥 未设置」，与事实不符 | R4 | 否 |

---

## 4. 追溯表

| 条款 | 分片 | commit | 产物 | 验收证据 |
|---|---|---|---|---|
| C1 | R1+R2+R3 | `a1a7617` | 待打包（0.9.12） | `drive_setup.py … embedding "1\n"` → `✓ qwen3.7-text-embedding-flash 可用，1024 维` |
| C2 | R1 | `a1a7617` | 待打包 | `test_setup` Q 节「无 key 时不发请求」 |
| C3 | R2 | `a1a7617` | 待打包 | `test_setup` Q 节「二次配置不追问」 |
| C4 | R3 | `a1a7617` | 待打包 | `test_setup` Q 节「换预设继承且 keyEnv 不丢」 |
| C5 | R4 | `a1a7617` | 待打包 | `hy-mem config show` → `已设置（来自 $MEMORY_EMBEDDER_API_KEY）` |

---

## 5. 结论

- 测试：`test_setup` 103 → 125（新增 Q 节「第二次配置」）；九套 **443** 全绿；`check_secrets` 0 ERROR。
- 黑盒复跑：完整二次向导 `all "1\n2\n1\n\n1\n\n"` 末尾「配置自检」10 项全 ✓，落盘 `apiKey` 仍为空、`keyEnv` 保留。
- 实际产物：**未重打包**（`dist/hy-mem` 仍是 0.9.11 修复前的构建）。要用上修复须走 `AGENTS.md §10`。
- 沉淀：`AGENTS.md §8.1` 四条铁律 + `skill/hy-dev` 流水线；新增 `scripts/drive_setup.py`（黑盒驱动，已转正入库）。
