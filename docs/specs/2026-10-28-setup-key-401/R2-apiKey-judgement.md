# R2 · 判据用 apiKey 字段

> 父级：`../00-contract.md`
> 契约条款：`C1`、`C3`（唯一事实源在父级，本文件只做落地，**不改约定**）
> 状态：`✓ 完成`　｜　commit：`a1a7617`

## 1. 这一片做什么

`step_embedding()` 用 `emb.get("apiKey")` 判断"当前配置里有没有 key"。该字段恒空 → 判断恒假 →
每次进设置都追问 `API Key`，且默认值为空，用户一回车就把空串交给请求。

## 2. 怎么改

| 文件 | 改动 |
|---|---|
| `cli/setup_wizard.py:step_embedding` | 「有没有 key」改看解析结果；有 key 时继承、不落盘（与 `step_llm` 同一策略）；只有换端点才要求重填 |

改前的错法：

```python
if chosen is not None and cur == idx and emb.get("apiKey"):   # 恒假
```

## 3. 验收

| 步 | 命令 | 期望 |
|---|---|---|
| 红 | `… drive_setup.py … embedding "1\n"` | 出现 `API Key:` 提问 |
| 绿 | 同上 | **不提问**，直接 `✓ … 1024 维` |
| 回归 | `scripts/test_setup.py` | Q 节「二次配置不追问」通过 |

> 铁律 1 的落地：这条 bug 的首次配置路径完全正常（首次 `apiKey` 有值），
> 只有"再配一次"才暴露，所以用例必须跑第二遍。

## 4. 风险与回滚

- 影响面：设置向导向量模型步骤的所有分支（含 custom）。
- 回滚：恢复条件表达式即可。

## 5. 结果

commit `a1a7617`；父级 `§2`、`§4` 已回填。
