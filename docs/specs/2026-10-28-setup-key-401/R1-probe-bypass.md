# R1 · 探针绕过 keyEnv 解析

> 父级：`../00-contract.md`
> 契约条款：`C1`、`C2`（唯一事实源在父级，本文件只做落地，**不改约定**）
> 状态：`✓ 完成`　｜　commit：`a1a7617`

## 1. 这一片做什么

`probe_embedding()` 构造请求头时直接取 `emb["apiKey"]`。0.9.11 起配置里只留 `keyEnv`，
字段恒空 → 请求头是空串 → 服务端 `401 No API-key provided`。改为走统一解析入口。

## 2. 怎么改

| 文件 | 改动 |
|---|---|
| `cli/setup_wizard.py:probe_embedding` | 改调 `cfgmod.resolve_embed_key(cfg)`；取不到时提前返回「密钥为空（…$变量名）」，**不发请求** |

改前的错法：

```python
key = str(emb.get("apiKey") or "")      # 恒空，环境变量那把 key 从未被读
```

## 3. 验收

| 步 | 命令 | 期望 |
|---|---|---|
| 红 | `.venv/Scripts/python.exe scripts/drive_setup.py /d/tools_open/tmp-401 embedding "1\n\n"` | `✗ HTTP 401: No API-key provided` |
| 绿 | 同上 | `✓ qwen3.7-text-embedding-flash 可用，1024 维` |

## 4. 风险与回滚

- 影响面：向导探测、`doctor` 的 `embedding_live` 项。
- 回滚：恢复该函数内两行即可，无数据结构变化。

## 5. 结果

commit `a1a7617`；父级 `§2`、`§4` 已回填。
