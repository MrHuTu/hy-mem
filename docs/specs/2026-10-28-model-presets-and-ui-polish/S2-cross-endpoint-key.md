# S2 · 换端点时继承了旧端点的 key → 401

> 父级：`../00-contract.md`　｜　契约条款：C3 / C4 / C5　｜　状态：执行中

## 现象

用户重新设置向量模型 / LLM，在「改为在 config.json 里单独保存一个 key？」处按 N，随后报
`HTTP 401: Token is invalid.`

## 复现（2026-10-28 实跑，`scripts/drive_setup.py`）

```
$ hy-mem setup --step embedding  (stdin='4\nn\n')      # 4 = 硅基流动 bge-m3，与当前百炼不同端点

  [d] 4) 硅基流动 BAAI/bge-m3
  输入序号或英文键后回车（直接回车 = 1）:
  i 将从环境变量 $MEMORY_EMBEDDER_API_KEY 继承密钥（sk-ws-H.…S2Ef），不落盘；
    端点已变，若这把 key 不适用于新端点，请改为单独填写
  › 改为在 config.json 里单独保存一个 key？ [y/N]:      ← 按 N
  … 正在真实请求一次嵌入以确定维度…
  ✗ HTTP 401: Token is invalid.｜密钥被拒绝：无效 / 过期 / 填错了别家的 key，或前后多了空格
```

对照组：同端点重选（选第 1 项，`stdin='1\n'`）→ 不追问、直接 `✓ 可用，1024 维 / 344ms`。
说明 bug 只在**跨端点**时出现。

## 根因

`step_embedding` 的继承候选链末位兜底写死约定变量名：

```python
cands = [chosen.key_env, key_env, "MEMORY_EMBEDDER_API_KEY"]
for name in cands:            # 找到第一个"有值"的就继承
```

`MEMORY_EMBEDDER_API_KEY` 是**旧端点**的 key，但链上有值就返回。于是：

1. 端点已换（百炼 → 硅基流动），key 没换；
2. 提示语把判断责任推给用户（"若这把 key 不适用，请改为单独填写"），但**默认行为就是复用它**；
3. 用户按 N（"不单独保存"）的本意是"别把 key 写进配置"，程序却理解成"用这把"；
4. 请求带旧 key 打新端点 → 401。

`step_llm` 是同一套链（`cands` 末位 `MEMORY_LLM_API_KEY`），问题相同。

## 改法

判据从「候选链上有值就继承」改为「**同端点才继承**」：

| 场景 | 行为 |
| --- | --- |
| 配置端点 == 预设端点，且有可用 key | 自动继承、不落盘、**不追问**（删掉那个 confirm） |
| 配置端点 != 预设端点 | **不继承**。优先用该预设声明的 `key_env`（如 `$OPENAI_API_KEY`）；取不到就直接要求填 key，并说明「`$MEMORY_EMBEDDER_API_KEY` 是旧端点 X 的 key，新端点 Y 不通用」 |
| 配置为空（首次） | 按预设 `key_env` → 约定变量名找，有就用（此时不存在"旧端点"概念） |

同时：

- 删掉「改为在 config.json 里单独保存一个 key？」（C4）—— 有 key 就继承、没有才问，
  中间那个"要不要存明文"的岔路正是 401 的入口；
- 401 / 403 且密钥来自环境变量时，错误信息补一句「$VAR 里的 key 可能属于旧端点（{旧 baseUrl}）」（C5）。

## 红 → 绿

| 用例 | 期望 |
| --- | --- |
| 换端点（`'4\n'`） | 不出现"继承"字样；直接问 API Key（或提示 `$SILICONFLOW_API_KEY` 未设置） |
| 同端点（`'1\n'`） | 直接探针通过，无 key 追问、无 confirm |
| 换端点 + 环境变量里正好有新端点专用变量 | 自动用它，探针通过 |

## 回滚

`git revert` 本分片 commit。纯交互逻辑，回滚不影响已落盘配置与数据。
