# S1 · 模型预设收敛成「主流三家 + 自定义」

> 父级：`../00-contract.md`　｜　契约条款：C1 / C2　｜　状态：已实现（待打包手测）

## 现象

用户原话：「模型设置（包括向量模型）提供几种主流的模型选择（GPT、GLM、DeepSeek），
和自定义模型设置就行，其他花里胡哨的设置方式都删掉。」

旧预设：向量 5 条（qwen flash / text-embedding-v4 / qwen 标准版 / 硅基 bge-m3 / 本地 ONNX），
LLM 4 条（ysjx deepseek / ysjx glm / 百炼 qwen-maas / DeepSeek 官方）。每条副标题还挂着
「区分度 2.02x · 1024 维 · ~300ms」三列实测指标，选择页要滚两屏半。

## 结论

| 类别 | 保留 | 说明 |
| --- | --- | --- |
| 向量 | `GPT`（text-embedding-3-small, 1536）/ `GLM`（embedding-3, 2048）/ `百炼 qwen`（qwen3.7-text-embedding-flash, 1024） | qwen 是本机在用的那一条，保留它才不会让现网配置重选时掉进「自定义」 |
| LLM | `GPT`（gpt-4o-mini）/ `GLM`（glm-4-flash）/ `DeepSeek`（deepseek-chat） | 轻量档：抽取够用、便宜快 |
| 末尾 | `自定义（手填 baseUrl + model）` | 自建网关 / 其它厂商走这条 |

**DeepSeek 不进向量预设**：官方只有 `/chat/completions`，没有 `/embeddings`。
选择页顶部用一行 `extra_hint` 说明，避免用户去找一个不存在的入口（C2）。

副标题一律留空，指标数据退到数据类字段（`latency_ms` / `discrimination` / `note`），
只在对齐文档与排障时读。

## 验证

- `scripts/test_setup.py`：`预设收敛为「主流三家 + 自定义」`（3/3）、
  `向量预设不含 DeepSeek`、`LLM 预设含 DeepSeek`。
- 选择页实测：向量 4 项（3 + 自定义）、LLM 4 项，一屏放得下。

## 回滚

`git revert` 本分片 commit。已落盘配置里的 `baseUrl` / `model` 不受影响，
只是选择页再也匹配不到旧预设（`embedding_index` 返回 -1 → 落到「自定义」，字段值仍保留）。
