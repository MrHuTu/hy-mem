# R3 · to_cfg 丢失 / 冲掉 keyEnv

> 父级：`../00-contract.md`
> 契约条款：`C4`（唯一事实源在父级，本文件只做落地，**不改约定**）
> 状态：`✓ 完成`　｜　commit：`a1a7617`

## 1. 这一片做什么

预设转配置时把 `keyEnv` 弄丢了：`EmbeddingPreset.to_cfg()` 根本不带该字段，
`LlmPreset.to_cfg()` 把它冲成**预设声明的**变量名。于是"继承成功、保存后取不到"。

## 2. 怎么改

| 文件 | 改动 |
|---|---|
| `cli/presets.py` | `to_cfg()` 带上 `keyEnv`；继承到 key 时把 `keyEnv` 写回成**实际取到 key 的那个变量名** |

典型现场：

```
预设声明：$YSJX_API_KEY
本机实际：$MEMORY_LLM_API_KEY
→ 保存后 resolve_llm_key() 返回空，配置闸门报"缺项"
```

## 3. 验收

| 步 | 命令 | 期望 |
|---|---|---|
| 绿 | `test_setup` Q 节「换同厂商预设：继承不追问，keyEnv 不丢」 | 通过 |
| 绿 | `test_setup` Q 节「LLM：预设变量名与本机不一致」 | 通过 |
| 结构断言 | 保存后的 `embedding` 段 | `apiKey == ""` 且 `keyEnv == "MEMORY_EMBEDDER_API_KEY"` |

> 铁律 3 的落地：只断言 `apiKey == ""` 会漏掉 `keyEnv` 被冲回预设默认值，
> 必须断言整个字段组合。

## 4. 风险与回滚

- 影响面：任何走预设的配置写入（源 wizard / lite / pro）。
- 回滚：恢复 `to_cfg()` 的返回字典字段即可；已落盘的配置需手工补 `keyEnv`。

## 5. 结果

commit `a1a7617`；父级 `§2`、`§4` 已回填。
