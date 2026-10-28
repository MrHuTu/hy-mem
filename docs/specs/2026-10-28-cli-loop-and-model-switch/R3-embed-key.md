# R3 · 配置没配好时，环境变量里的 key 用不上

> 父级：`../00-contract.md`　｜　契约条款：C3 / C4 / C6　｜　状态：✓ 已修

## 现象

用户要求检查「第一次运行没有配置好的情况」。实测（沙箱把 `embedding` 段清空，环境变量里有一把
有效的 `MEMORY_EMBEDDER_API_KEY`）：

```
通义 qwen3.7-embedding-flash（推荐） 需要 key；逐字粘贴，前后别带空格
› API Key:                       ← 明明环境变量里有，却逼用户手填
```

不填就没有后续，用户结论是「设置里没办法重新设置模型」。

## 根因

`step_embedding` 的继承分支写死了「只有同一个端点才继承」：

```python
same_host = str(emb.get("baseUrl") or "").rstrip("/") == chosen.base_url.rstrip("/")
inherited = os.getenv(key_env, "") if same_host else ""
```

配置为空时 `baseUrl` 是空串，`same_host` 恒假 → `inherited` 恒空 → 掉进 `else` 分支强制手填。
而 `step_llm` 上一轮已经改成「按 `[预设变量, 配置 keyEnv, MEMORY_LLM_API_KEY]` 找第一个有值的」，
两者行为不对称 —— 同一类问题只修了一半。

## 改法

与 `step_llm` 完全对称：

1. 候选变量名按 `[chosen.key_env, 配置 keyEnv, MEMORY_EMBEDDER_API_KEY]` 去重；
2. 取第一个**有值**的，继承、不落盘，并把 `keyEnv` 写回成那个真实变量名；
3. 跨端点继承时额外提示「端点已变，若该 key 不适用于新端点请改为单独填写」——把风险说破，
   而不是替用户挡住；
4. 只有当**所有候选都为空**且配置里也没有明文时，才要求手填。

## 红 → 绿

修前：`setup --step embedding`（embedding 段置空）→ 出现 `› API Key:`，最终「已取消，配置未改动」。
修后：同条件 → `i 将继承环境变量 $MEMORY_EMBEDDER_API_KEY 的密钥（sk-ws-…）`，
不出现 `› API Key:`，落盘 `model=qwen3.7-text-embedding-flash`、`apiKey` 为空、`keyEnv` 保留。

## 回滚

回滚到 `same_host` 判定即可，但会让「配置为空 / 换端点」两种场景退回手填。
