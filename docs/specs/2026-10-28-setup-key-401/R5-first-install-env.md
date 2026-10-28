# R5 · 首次安装时密钥判据仍失效

> 父级：`../00-contract.md`
> 契约条款：`C1`（唯一事实源在父级，本文件只做落地，**不改约定**）
> 状态：`✓ 完成`　｜　commit：见父级 `§4`

## 1. 这一片做什么

R1–R4 修完后，「第二次配置选当前项」在源码上已通过。**打包前用 exe 复测首次安装**时又暴露一处：
空沙箱（无 `config.json`）跑 `setup --step llm`，先提示「将继承当前密钥（sk-fw…，来自 config.json）」，
紧接着报 `✗ 密钥为空（配置里的 apiKey 与 keyEnv 指向的变量都取不到）` —— 自相矛盾。

## 2. 根因

`config.load()` 在**没有配置文件**时调 `_fill_from_env()`，把 `$MEMORY_LLM_API_KEY` /
`$MEMORY_EMBEDDER_API_KEY` 的值**复制进内存里的 `apiKey`**。于是：

| 位置 | 错在哪 |
|---|---|
| `step_llm` | 判断"要不要写回 keyEnv"用的是 `not llm.get("apiKey")`。env 副本让它为假 → 写回被跳过 → `new["keyEnv"]` 仍是预设声明的 `$YSJX_API_KEY`（本机为空）→ 探针拿空串 |
| `step_llm` | 提示语把 env 副本说成「来自 config.json」，误导排查方向 |
| `step_embedding`「选当前项」分支 | `new = {**emb, ...}` 把 env 副本 `apiKey` 一起复制 → **明文被烤进新配置**，违反密钥纪律 |
| `step_embedding` 预设分支 | 条件写成 `inherited and not key_in_file`，env 副本让 `key_in_file` 为真 → 退化成追问用户 |

## 3. 怎么改

| 文件 | 改动 |
|---|---|
| `cli/setup_wizard.py:step_llm` | 判据改为**只问环境变量**：按 `[预设 key_env, 配置 keyEnv, MEMORY_LLM_API_KEY]` 顺序找第一个有值的变量名，写回 `new["keyEnv"]`、`new["apiKey"] = ""`。仅当环境变量全空、且配置里有真明文时才沿用明文 |
| `cli/setup_wizard.py:step_embedding` | ① 预设分支改成「环境变量优先」，不再看 `key_in_file`；② 选当前项分支若 `keyEnv` 指向的变量有值，清空 `apiKey`，不烤明文 |
| `scripts/test_setup.py` | 新增 Q6/Q7：复刻 `_fill_from_env` 的结果（apiKey 有 env 副本、keyEnv 是预设默认名），断言不追问、探针拿到 env key、落盘 `apiKey` 为空、`keyEnv` 指向真实变量名 |

判据（红 → 绿）：

```
修前（exe，空沙箱）：i 将继承当前密钥（sk-fw741…，来自 config.json）
                    ✗ 密钥为空（配置里的 apiKey 与 keyEnv 指向的变量都取不到）
修后（exe，空沙箱）：i 将继承当前密钥（sk-fw741…，来自 $MEMORY_LLM_API_KEY），不落盘
                    ✓ deepseek-v4-flash 可用，375ms，返回 '可用'
```

## 4. 风险与回滚

- 影响面：向导的向量模型 / LLM 两步，所有首次安装与重配路径。
- 回滚：恢复 `step_llm` / `step_embedding` 的密钥分支；已落盘配置无需迁移。
- 注意：`DEFAULTS.llm.keyEnv` 仍是预设声明的 `YSJX_API_KEY`，本轮不动它（改了会影响把 key 放在该变量里的人）；
  新逻辑通过"找第一个有值的变量"覆盖本机 `$MEMORY_LLM_API_KEY` 的情形。

## 5. 结果

- `test_setup` 125 → **133**；九套 443 → **451** 全绿；`check_secrets` 0 ERROR。
- exe 复测：空沙箱首次配 LLM ✓、连续两次配向量模型 ✓（都不追问 key、都不出现 ✗）。
- 产物：`dist/hy-mem-0.9.12-win64.zip`（178MB，解压冒烟通过）。
