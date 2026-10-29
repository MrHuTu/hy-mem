"""hy-mem 设置向导 —— 交互式配置，首次运行自动进入.

三个设计决定，都是为了消灭「配错才知道」这类故障：

1. **维度不让人手填，实测得出**。embedding 请求一次，取返回向量长度。
   手填 dims 是这套系统最容易犯且最难自查的错：填错不会报配置错误，
   而是等到第一次写入时抛 EmbeddingDimensionMismatchError，且旧数据已废。

2. **验证发生在保存之前，且在同一个进程内**。这是选 Python 实现而不是
   Node 外壳的主要理由 —— 可以直接复用本地嵌入实现与 SDK，真发一次请求；
   Node 外壳只能再 spawn 一个 python 子进程去验，链路更长、错误更难定位。

3. **失败不阻塞保存**。离线、代理挂、端点临时 5xx 是常态，向导不能因此
   卡死。验证失败时给三个出口：重试 / 仍保存 / 放弃，把判断权交回用户。

流程顺序（v0.8 起固定为）：向量模型 → MEMORY_MODE → LLM（仅 pro/ultra）→ 存储目录。
先定嵌入是因为它决定 dims 与整个库的语义空间；模式决定要不要 LLM，所以 LLM
一步放在模式之后，lite 用户不会被问到无关的问题。

单步入口（设置页用）复用同一批 step_* 函数，保证「第一次的向导」和「以后改某一项」
走的是同一段代码，不会出现两条路径行为不一致。
"""

from __future__ import annotations

import os
import time

import config as cfgmod
import netprobe
import presets
import storage_ops
import tui
from tui import c

PROBE_TEXT = "招商活动报名审批流用自研 FSM 落库"


# --------------------------------------------------------------------------
# 远程探测
#
# 真实 HTTP 请求统一走 netprobe，只有两种结果：
#   Sent    —— 请求发出去了，带服务器给的真实状态码（一定 >= 100）；
#   NotSent —— 请求没离开本机，带可展示的原因。
# 这里**故意不再有** status=0 这种「既不是成功也不是 HTTP 错误」的中间态：
# 2026-10-27 的 HTTP 0 事故就是它造成的（中文粘进密钥 → latin-1 编码失败）。
# --------------------------------------------------------------------------
def probe_embedding(emb: dict, timeout: int = 45) -> tuple[bool, str, int]:
    """真实请求一次嵌入。返回 (是否可用, 说明, 实测维度)。

    维度以服务端返回为准；预设里的 dims 只是提示，冲突时**信实测**。
    """
    provider = str(emb.get("provider") or "openai").lower()
    if provider == "local":
        model = str(emb.get("model") or "")
        t0 = time.time()
        try:
            from local_embedder import LocalEmbeddingService

            svc = LocalEmbeddingService(model_name=model or None)
            vec = svc._embed_sync(PROBE_TEXT)  # 直调同步实现，避免 asyncio 包装
            ms = int((time.time() - t0) * 1000)
            warm = "冷启动" if ms > 5000 else "热启动"
            return True, f"{model} 本地 ONNX 可用，{len(vec)} 维 / {ms}ms（{warm}）", len(vec)
        except Exception as e:  # noqa: BLE001
            return False, f"本地嵌入失败：{type(e).__name__}: {str(e)[:200]}", 0

    base = str(emb.get("baseUrl") or "").rstrip("/")
    model = str(emb.get("model") or "")
    # 密钥来源必须走统一解析（config.json 明文 > keyEnv 变量 > 约定变量名）。
    # 直接读 emb["apiKey"] 是 0.9.11 的实测 bug：配置里只留 keyEnv 之后，
    # 这里读到空串，仍然发出 `Authorization: Bearer `，服务端回
    # 「401 No API-key provided」，而环境变量里的有效 key 根本没被用上。
    key = cfgmod.resolve_embed_key({"embedding": emb})
    if not base or not model:
        return False, "baseUrl / model 未填", 0
    if not key:
        env_name = emb.get("keyEnv") or "MEMORY_EMBEDDER_API_KEY"
        return False, f"密钥为空（config.json 的 apiKey 与 ${env_name} 都取不到）", 0
    res = netprobe.post_json(
        f"{base}/embeddings",
        {"model": model, "input": [PROBE_TEXT]},
        key,
        timeout,
        bool(emb.get("insecureTls")),
        key_label="向量模型密钥",
    )
    if isinstance(res, netprobe.NotSent):
        # 请求压根没离开本机（编码/DNS/连接/超时）。这里绝不能显示成 HTTP 错误。
        return False, res.reason, 0
    ms = int(res.seconds * 1000)
    if res.status != 200:
        return False, netprobe.explain(res), 0
    payload = res.payload
    data = payload.get("data") or []
    if not data:
        return False, "响应中没有 data 字段（这个地址可能不是 OpenAI 兼容的 /embeddings 接口）", 0
    vec = data[0].get("embedding") or []
    if not vec:
        return False, "响应中 embedding 为空", 0
    dims = len(vec)
    warn = ""
    if emb.get("dims") and int(emb["dims"]) != dims:
        warn = f"，预设写的是 {emb['dims']} 维，已按实测修正"
    return True, f"{model} 可用，{dims} 维 / {ms}ms{warn}", dims


def probe_llm(llm: dict, timeout: int = 45) -> tuple[bool, str]:
    base = str(llm.get("baseUrl") or "").rstrip("/")
    model = str(llm.get("model") or "")
    key = cfgmod.resolve_llm_key({"llm": llm})
    if not base or not model:
        return False, "baseUrl / model 未填"
    if not key:
        return False, "密钥为空（配置里的 apiKey 与 keyEnv 指向的变量都取不到）"
    res = netprobe.post_json(
        f"{base}/chat/completions",
        {
            "model": model,
            "messages": [{"role": "user", "content": "只回复两个字：可用"}],
            # 推理模型会先花配额思考，小 max_tokens 会让 content 恒为空串
            "max_tokens": 512,
        },
        key,
        timeout,
        bool(llm.get("insecureTls")),
        key_label="LLM 密钥",
    )
    if isinstance(res, netprobe.NotSent):
        return False, res.reason
    ms = int(res.seconds * 1000)
    if res.status != 200:
        return False, netprobe.explain(res)
    payload = res.payload
    choices = payload.get("choices") or []
    reply = think = fin = ""
    if choices:
        msg = choices[0].get("message") or {}
        reply = str(msg.get("content") or "").strip()
        think = str(msg.get("reasoning_content") or "").strip()
        fin = str(choices[0].get("finish_reason") or "")
    if reply:
        return True, f"{model} 可用，{ms}ms，返回 {reply[:20]!r}"
    if think:
        # content 空、思维链有内容 —— 推理模型的正常形态，可用。
        return True, f"{model} 可用，{ms}ms（推理模型，思考 {len(think)} 字后作答）"
    return False, f"{model} 端点通但返回空内容（finish_reason={fin!r}），检查模型名是否正确"


# --------------------------------------------------------------------------
# 步骤 1/4：向量模型
# --------------------------------------------------------------------------
def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 10:
        return secret[:2] + "***"
    return f"{secret[:8]}…{secret[-4:]}"


def _pick_key(
    prev: dict,
    preset_env: str,
    legacy_env: str,
    same_endpoint: bool,
    fallback_env: str = "MEMORY_EMBEDDER_API_KEY",
) -> tuple[str, str]:
    """决定这一步用哪把密钥、放在哪。返回 (明文, 变量名)，二者必有其一为空。

    规则只有一条主线 —— **同端点才允许继承**（C3/C4）：

    · 同端点：`[预设声明的变量, 配置里的 keyEnv, 约定变量名]` 取第一个有值的，
      明文留空、只记变量名（不落盘），也不再多问一句"要不要单独保存"；
    · 跨端点：**只认预设声明的变量**。旧端点的 key 属于别的服务，复用只会换来
      一次 401（2026-10-28 现场事故：百炼的 key 打到硅基流动），所以这里宁可返回空
      让用户现填，也不“借”一把看起来有值的 key。
    · 配置里真有明文（用户手填过）且同端点：沿用它。

    `fallback_env` 是本程序的约定变量名（$MEMORY_EMBEDDER_API_KEY /
    $MEMORY_LLM_API_KEY）。首次安装时配置是新建的，`keyEnv` 常被预设声明名占着
    （$OPENAI_API_KEY 之类，本机根本没设），只认它就会「明明有 key 却报密钥为空」——
    0.9.12 打包前就是这么翻车的，所以同端点时必须把它也算进候选。
    """
    cands = [preset_env]
    if same_endpoint:
        cands += [str(prev.get("keyEnv") or ""), legacy_env, fallback_env]
    seen: set[str] = set()
    for name in cands:
        if not name or name in seen:
            continue
        seen.add(name)
        if os.getenv(name, ""):
            return "", name
    if same_endpoint and prev.get("apiKey"):
        return str(prev["apiKey"]), str(prev.get("keyEnv") or "")
    return "", ""


def _confirm_credentials(new: dict, *, kind: str) -> dict:
    """把 baseUrl / 模型 / 密钥三件套摆给用户确认；不确认就换 key，信息补齐才发探针。

    用户规范（2026-10-28）：设置模型时应等用户把信息补充完整（baseUrl / 模型 / key），
    提交完整后再测连通性 —— 继承来的 key 也要明示来源，不允许"静默就用"。
    """
    fallback_env = "MEMORY_EMBEDDER_API_KEY" if kind == "embedding" else "MEMORY_LLM_API_KEY"
    while True:
        if new.get("apiKey"):
            key_disp = "手填（" + _mask(str(new["apiKey"])) + "，将存入 config.json）"
        else:
            env_name = str(new.get("keyEnv") or fallback_env)
            key_disp = f"环境变量 ${env_name}（{_mask(os.getenv(env_name, ''))}）"
        print()
        tui.kv([
            ("baseUrl", str(new.get("baseUrl") or "（空）")),
            (tr_key_model_label(kind), str(new.get("model") or "（空）")),
            ("密钥", key_disp),
        ])
        # 完整性只认 new 自己的 keyEnv 指向的变量：换端点后 keyEnv 已指向新端点
        # 变量名（值空），若回落看旧约定变量，会把旧端点的值误判为"完整"→旧 key
        # 打新端点 → 401（Q4 函数级用例红的正是这里）。
        eff_env = str(new.get("keyEnv") or "")
        complete = bool(str(new.get("baseUrl") or "").strip()) and bool(str(new.get("model") or "").strip()) and (
            bool(str(new.get("apiKey") or "").strip())
            or bool(eff_env and os.getenv(eff_env, ""))
        )
        if not complete:
            tui.warn("信息还不完整（baseUrl / 模型 / 密钥至少缺一项），先补齐再测连通性")
        # key 决定显式化（2026-10-29 用户反馈）：换全新模型时用户常要换新 key，
        # 「一路回车继承」会把这个入口藏没。这里给三选一，继承是默认快路，
        # 「换一把 key」是显式入口，取消兜底。
        if complete:
            choice = tui.select(
                "密钥怎么用？",
                [
                    ("就用上面这把（回车直接测）", "key 没换过、属于这个端点", "k"),
                    ("换一把 key", "换了全新模型 / key 已轮换；粘贴新 key 后再测", "n"),
                    ("取消本次设置", "不改配置", "x"),
                ],
                default=0,
            )
            if choice == 2:
                raise tui.Aborted()
            if choice == 1:
                new_key = tui.ask_secret(
                    "新 API Key", hint="逐字粘贴，前后别带空格"
                ).strip()
                if not new_key:
                    # 填了空 key：不许静默回落旧 key（401 家族），回到选择
                    tui.warn("key 为空，未改动；重新选择或取消")
                    continue
                new["apiKey"] = new_key
                new["keyEnv"] = fallback_env
            return new
        # 信息不完整：只能补 key（baseUrl / 模型在上面各自问过）
        new_key = tui.ask_secret(
            "API Key", hint="信息不完整，先补 key；逐字粘贴，前后别带空格"
        ).strip()
        if not new_key and not (eff_env and os.getenv(eff_env, "")):
            raise tui.Aborted()
        new["apiKey"] = new_key
        new["keyEnv"] = fallback_env


def tr_key_model_label(kind: str) -> str:
    return "模型名" if kind == "embedding" else "模型名"


def _is_auth_error(msg: str) -> bool:
    return " 401" in msg or " 403" in msg or "401:" in msg or "403:" in msg


def step_embedding(cfg: dict, _depth: int = 0) -> dict:
    emb = dict(cfg.get("embedding") or {})
    cur = presets.embedding_index(emb)

    # 只留主流几家 + 自定义（C1）：副标题不再堆区分度 / 延迟 / 维度三列指标，
    # 那些数字看一次就够，摆在选择页上只会把屏幕撑成两屏半。
    options = [(p.label, "") for p in presets.EMBEDDING_PRESETS]
    options.append(("自定义（手填 baseUrl + model）", ""))
    options = [(lab, det, k) for (lab, det), k in zip(options, tui.keyseq(len(options)))]

    # 当前配置是自定义端点（cur=-1）但确实已配过时，默认光标必须落「自定义」项，
    # 否则用户回车想「保持现状」，实际落进第 1 个预设 → 秒变跨端点 → 被追问
    # 新 key（2026-10-28 UI 级测试 B 场景红的正是这里；step_llm 一直是这么做的）。
    _has_custom = bool(str(emb.get("baseUrl") or "").strip())
    idx = tui.select(
        "[1/4] 向量模型",
        options,
        default=cur if cur >= 0 else (len(options) - 1 if _has_custom else 0),
        extra_hint="DeepSeek 官方没有向量接口（只有 chat），所以这里不列它",
    )
    chosen = presets.EMBEDDING_PRESETS[idx] if idx < len(presets.EMBEDDING_PRESETS) else None

    # 密钥是否可用只能问统一解析入口：配置里的明文可能为空（0.9.11 起只留 keyEnv），
    # 真正生效的 key 常常在环境变量里。用 emb.get("apiKey") 判断会让「同一项重配」
    # 每次都追问 key，用户一回车就把空串交给探针 → 401（2026-10-28 实测事故）。
    have_key = bool(cfgmod.resolve_embed_key({"embedding": emb}))
    key_env = str(emb.get("keyEnv") or "MEMORY_EMBEDDER_API_KEY")
    same_endpoint = str(emb.get("baseUrl") or "").rstrip("/") == (
        chosen.base_url.rstrip("/") if chosen else "\0"
    )

    if chosen is not None and cur == idx and have_key:
        # 选的就是当前项且密钥可用：不重问 key，但仍重新验证（key 可能已轮换）
        new = {
            **emb,
            "preset": chosen.id,
            "provider": chosen.provider,
            "baseUrl": chosen.base_url,
            "model": chosen.model,
        }
        if not chosen.needs_key:
            new["apiKey"] = ""
        elif str(emb.get("keyEnv") or "") and os.getenv(str(emb["keyEnv"]), ""):
            # 密钥已经在环境变量里，就别再把明文烤进配置。首次安装时 `_fill_from_env()`
            # 会把 env 值复制进 emb["apiKey"]，直接 `{**emb}` 就把它一起存下去了。
            new["apiKey"] = ""
            new["keyEnv"] = emb["keyEnv"]
        if chosen.needs_key:
            new = _confirm_credentials(new, kind="embedding")
    elif chosen is None:
        # 自定义：默认值预填当前值，连按回车就能"只改模型名"。
        # keyEnv 必须原样保留 —— 老代码在这里把 keyEnv 清成空串，配好环境变量的机器
        # 一进自定义就"密钥丢失"，只能重新贴一把明文回来。
        new = dict(emb)
        new["preset"] = "custom"
        new["provider"] = "openai"
        new["baseUrl"] = tui.ask(
            "baseUrl",
            default=str(emb.get("baseUrl") or ""),
            hint="OpenAI 兼容入口，通常以 /v1 结尾（百炼用 /compatible-mode/v1）",
        )
        new["model"] = tui.ask("模型名", default=str(emb.get("model") or ""))
        new["dims"] = int(emb.get("dims") or 0)
        # same_endpoint 必须按「刚填的新 baseUrl」对旧值算，不能写死 True：
        # 自建网关之间切换（A 网关 → B 网关）也是跨端点，静默继承旧 key 只会 401
        # （2026-10-28 UI 级测试 D 场景红的正是这里；预设→预设路径当天已修）。
        custom_same = str(new["baseUrl"]).rstrip("/") == str(emb.get("baseUrl") or "").rstrip("/")
        api_key, env_name = _pick_key(emb, "", key_env, same_endpoint=custom_same, fallback_env="MEMORY_EMBEDDER_API_KEY")
        if env_name:
            new["apiKey"] = ""
            new["keyEnv"] = env_name
        else:
            new["apiKey"] = tui.ask_secret(
                "API Key", hint="环境变量里没有可用的 key；逐字粘贴，前后别带空格"
            )
            # keyEnv 保留约定变量名（不清空！）：清空后 resolve_embed_key 会回落
            # 读旧约定变量，把别的端点的 key 打到新端点上（401 复发路径）。
            new["keyEnv"] = "MEMORY_EMBEDDER_API_KEY"
        new = _confirm_credentials(new, kind="embedding")
    else:
        new = chosen.to_cfg()
        if chosen.needs_key:
            api_key, env_name = _pick_key(emb, chosen.key_env, key_env, same_endpoint, "MEMORY_EMBEDDER_API_KEY")
            if env_name:
                # 同端点（或新端点自带专用变量）：继承，不落盘
                tui.info(f"用环境变量 ${env_name} 里的密钥（{_mask(os.getenv(env_name, ''))}），不落盘")
                new["apiKey"] = ""
                new["keyEnv"] = env_name
            elif api_key:
                new["apiKey"] = api_key
            else:
                # 既没有新端点专用变量、又跨端点：旧 key 不能借（C3）。
                if emb.get("baseUrl"):
                    tui.warn(f"端点已从 {emb.get('baseUrl')} 换到 {chosen.base_url}，旧密钥不能复用")
                    tui.note(
                        f"环境变量里没有 ${chosen.key_env}；旧端点那把 key 属于另一家服务，"
                        "继续用只会拿到 401，所以这里必须填新的"
                    )
                new["apiKey"] = tui.ask_secret(
                    "API Key",
                    hint=f"{chosen.label} 需要该端点自己的 key；逐字粘贴，前后别带空格",
                )
                # keyEnv 保留新端点自己的变量名（通常是空的），**不要清成空串**：
                # 空串会让 config.resolve_embed_key 回落到 $MEMORY_EMBEDDER_API_KEY，
                # 也就是又拿旧端点的 key 去打新端点 —— 正是 401 的来源。
                # 指到新端点变量名后解析结果就是空，探针会报「密钥为空」并让你重填。
                new["keyEnv"] = chosen.key_env
        else:
            new["apiKey"] = ""
            new["keyEnv"] = ""
        if chosen.needs_key and cur != idx:
            new = _confirm_credentials(new, kind="embedding")

    print()
    tui.spinner_line("正在真实请求一次嵌入以确定维度…")
    ok, msg, dims = probe_embedding(new)
    if ok:
        tui.ok(msg)
        new["dims"] = dims
    else:
        tui.err(msg)
        if _is_auth_error(msg) and not new.get("apiKey") and new.get("keyEnv"):
            # C5：401 最常见的成因就是"拿旧端点的 key 打新端点"，必须说破，
            # 否则用户只会反复重试同一个动作。
            tui.note(
                f"${new['keyEnv']} 里的 key 可能属于旧端点"
                + (f"（{emb.get('baseUrl')}）" if emb.get("baseUrl") else "")
                + "，换端点要同时换 key"
            )
        choice = tui.select(
            "向量模型验证失败，怎么办？",
            [
                ("重新填写 retry", "回到本步骤最前面重来", "r"),
                ("仍然保存 save", "离线或端点暂时不可用时选这个，之后用 hy-mem setup 校正", "s"),
                ("放弃本次设置 abort", "不写入任何改动", "x"),
            ],
            default=0,
        )
        if choice == 2:
            raise tui.Aborted()
        if choice == 0:
            if _depth >= 3:
                tui.warn("重试次数过多，保留原配置")
                raise tui.Aborted()
            return step_embedding({**cfg, "embedding": new}, _depth + 1)
    return new


# --------------------------------------------------------------------------
# 步骤 2/4：运行模式
# --------------------------------------------------------------------------
def step_mode(cfg: dict) -> str:
    cur = presets.mode_index(str(cfg.get("mode") or "pro"))
    mode_keys = {"lite": "l", "pro": "p", "ultra": "u"}
    options = [
        (p.label, p.detail + " ｜ " + p.use_case, mode_keys.get(p.id, "")) for p in presets.MODE_PRESETS
    ]
    idx = tui.select("[2/4] 运行模式 MEMORY_MODE", options, default=cur, extra_hint="模式决定写入开销，随时可改；改后已有记忆不受影响")
    return presets.MODE_PRESETS[idx].id


# --------------------------------------------------------------------------
# 步骤 3/4：LLM（pro / ultra 必需）
# --------------------------------------------------------------------------
def step_llm(cfg: dict, _depth: int = 0) -> dict:
    llm = dict(cfg.get("llm") or {})
    cur = presets.llm_index(llm)

    # 与向量模型同一套收敛策略（C1）：三家 + 自定义，副标题只留一句人话
    options = [(p.label, "") for p in presets.LLM_PRESETS]
    options.append(("自定义（手填 baseUrl + model）", ""))
    options = [(lab, det, k) for (lab, det), k in zip(options, tui.keyseq(len(options)))]

    idx = tui.select(
        "[3/4] LLM 模型（pro/ultra 用来抽取事实）",
        options,
        default=cur if cur >= 0 else len(presets.LLM_PRESETS),
        extra_hint="pro / ultra 模式必需；lite 模式用不到",
    )
    chosen = presets.LLM_PRESETS[idx] if idx < len(presets.LLM_PRESETS) else None
    key_env = str(llm.get("keyEnv") or "")
    same_endpoint = str(llm.get("baseUrl") or "").rstrip("/") == (
        chosen.base_url.rstrip("/") if chosen else "\0"
    )

    if chosen is None:
        # 自定义：默认值预填当前值 —— 自建网关（如本机 newapi）不必每次重敲三个字段。
        # keyEnv 必须保留，否则"配好环境变量"的机器一进自定义就密钥丢失。
        new = dict(llm)
        new["preset"] = "custom"
        new["baseUrl"] = tui.ask("baseUrl", default=str(llm.get("baseUrl") or ""))
        new["model"] = tui.ask("模型名", default=str(llm.get("model") or ""))
        # 同向量模型：same_endpoint 按「刚填的新 baseUrl」算，写死 True 会让
        # 自建网关切换时静默继承旧 key（UI 级测试 E3 场景红的正是这里）。
        custom_same = str(new["baseUrl"]).rstrip("/") == str(llm.get("baseUrl") or "").rstrip("/")
        api_key, env_name = _pick_key(llm, "", key_env, same_endpoint=custom_same, fallback_env="MEMORY_LLM_API_KEY")
        if env_name:
            new["apiKey"] = ""
            new["keyEnv"] = env_name
        else:
            new["apiKey"] = tui.ask_secret(
                "API Key", hint="环境变量里没有可用的 key；逐字粘贴，前后别带空格"
            )
            # 同向量模型：keyEnv 保留约定变量名，清空会让 resolve_llm_key 回落
            # 读旧约定变量 → 旧端点 key 打新端点（401 复发）。
            new["keyEnv"] = "MEMORY_LLM_API_KEY"
        new = _confirm_credentials(new, kind="llm")
    else:
        new = chosen.to_cfg()
        api_key, env_name = _pick_key(llm, chosen.key_env, key_env, same_endpoint, "MEMORY_LLM_API_KEY")
        if env_name:
            tui.info(f"用环境变量 ${env_name} 里的密钥（{_mask(os.getenv(env_name, ''))}），不落盘")
            new["apiKey"] = ""
            new["keyEnv"] = env_name
        elif api_key:
            new["apiKey"] = api_key
            if llm.get("keyEnv"):
                new["keyEnv"] = str(llm["keyEnv"])
        else:
            if llm.get("baseUrl"):
                tui.warn(f"端点已从 {llm.get('baseUrl')} 换到 {chosen.base_url}，旧密钥不能复用")
                tui.note(
                    f"环境变量里没有 ${chosen.key_env}；旧端点那把 key 属于另一家服务，"
                    "继续用只会拿到 401，所以这里必须填新的"
                )
            new["apiKey"] = tui.ask_secret(
                "API Key", hint=f"{chosen.label} 需要该端点自己的 key；逐字粘贴，前后别带空格"
            )
            # 同向量模型：保留新端点声明的变量名，别清空（见 step_embedding 的注释）
            new["keyEnv"] = chosen.key_env
        if chosen is not None:
            new = _confirm_credentials(new, kind="llm")

    print()
    tui.spinner_line("正在真实调用一次 LLM 验证…")
    ok, msg = probe_llm(new)
    if ok:
        tui.ok(msg)
    else:
        tui.err(msg)
        if _is_auth_error(msg) and not new.get("apiKey") and new.get("keyEnv"):
            tui.note(
                f"${new['keyEnv']} 里的 key 可能属于旧端点"
                + (f"（{llm.get('baseUrl')}）" if llm.get("baseUrl") else "")
                + "，换端点要同时换 key"
            )
        choice = tui.select(
            "LLM 验证失败，怎么办？",
            [("重新填写 retry", "回到本步骤重来", "r"), ("仍然保存 save", "之后用 hy-mem setup 改", "s")],
            default=0,
        )
        if choice == 0:
            if _depth >= 3:
                tui.warn("重试次数过多，保留原配置")
                raise tui.Aborted()
            return step_llm({**cfg, "mode": "pro"}, _depth + 1)
    return new


# --------------------------------------------------------------------------
# 步骤 4/4：存储目录
# --------------------------------------------------------------------------
def _describe_dir(path: str) -> list[tuple[str, str]]:
    size, files = storage_ops.dir_usage(path)
    free = storage_ops.free_space(path)
    pairs = [("路径", path)]
    if files:
        pairs.append(("已占用", f"{storage_ops.human_size(size)} / {files} 个文件"))
    else:
        pairs.append(("已占用", "空目录"))
    if free:
        pairs.append(("所在盘可用", storage_ops.human_size(free)))
    return pairs


def step_storage(cfg: dict) -> dict:
    st = dict(cfg.get("storage") or {})
    old = str(st.get("dataDir") or "")
    default_new = str(cfgmod.default_data_dir())

    print()
    print(c.dim("  记忆库（向量 + 图 + 队列库）都在这个目录，备份就是拷它。"))
    for k, v in _describe_dir(old):
        print(c.gray(f"    {k:<10}") + v)
    print()

    idx = tui.select(
        "存储位置",
        [
            ("保持当前目录 keep", default_new if old == default_new else "不改动", "k"),
            ("用默认目录 default", default_new, "d"),
            ("换一个目录 new", "输入新路径；若新目录为空会询问是否把现有数据迁过去", "n"),
        ],
        default=0,
    )

    if idx == 0:
        st["dataDir"] = old
        return st

    if idx == 1:
        path = default_new
    else:
        path = ""
        while True:
            raw = tui.ask("新目录", hint="绝对路径，可用 %USERPROFILE% 或 ~；留空取消")
            if not raw:
                st["dataDir"] = old
                return st
            path = storage_ops.normalize_path(raw)
            good, msg = storage_ops.check_target(path)
            if good:
                tui.ok(f"{path}  {c.dim('可用')}")
                break
            tui.err(msg)

    if path == old:
        tui.info("与当前目录相同，不做改动")
        st["dataDir"] = old
        return st

    # 换目录：旧目录有数据时，默认迁过去 —— 否则用户会以为记忆丢了
    size, files = storage_ops.dir_usage(old)
    if files and tui.confirm(
        f"把现有数据（{storage_ops.human_size(size)} / {files} 个文件）迁移到新目录？",
        default=True,
    ):
        tui.spinner_line("复制中…")
        ok, msg = storage_ops.migrate(old, path)
        (tui.ok if ok else tui.err)(msg)
        if not ok:
            if not tui.confirm("迁移失败，仍然切换到新目录？（新目录可能不完整）", default=False):
                st["dataDir"] = old
                return st
    st["dataDir"] = path
    return st


# --------------------------------------------------------------------------
# 附加项：检索阈值 / 队列参数
# --------------------------------------------------------------------------
def step_search(cfg: dict) -> dict:
    s = dict(cfg.get("search") or {})
    cur = float(s.get("minScore") or 0.35)
    options = [
        ("0.30 宽松 loose", "召回多、噪声也多；临时排查「怎么没搜到」时用", "a"),
        ("0.35 平衡（默认）balanced", "实测 flash 模型：正例 0.6+，干扰 0.33 落在阈值下", "b"),
        ("0.45 严格 strict", "只要高置信命中；适合库很大、噪声明显时", "c"),
        ("手动输入 manual", "填 0~1 之间的小数", "d"),
    ]
    presets_map = [0.30, 0.35, 0.45]
    default = min(range(3), key=lambda i: abs(presets_map[i] - cur))
    idx = tui.select("检索阈值 minScore", options, default=default)
    if idx < 3:
        s["minScore"] = presets_map[idx]
    else:
        while True:
            raw = tui.ask("minScore", default=str(cur))
            try:
                v = float(raw)
            except ValueError:
                tui.err("要一个小数，例如 0.38")
                continue
            if 0 <= v <= 1:
                s["minScore"] = round(v, 3)
                break
            tui.err("取值范围 0~1")
    return s


def step_queue(cfg: dict) -> dict:
    q = dict(cfg.get("queue") or {})
    print()
    print(c.dim("  队列参数影响异步写入的失败重试与并发安全，一般不用改。"))
    while True:
        raw = tui.ask("失败重试次数 maxAttempts", default=str(q.get("maxAttempts", 3)))
        if raw.isdigit() and int(raw) >= 1:
            q["maxAttempts"] = int(raw)
            break
        tui.err("要 ≥1 的整数")
    while True:
        raw = tui.ask("单任务锁超时秒数 lockTimeout", default=str(q.get("lockTimeout", 180)))
        if raw.isdigit() and int(raw) >= 30:
            q["lockTimeout"] = int(raw)
            break
        tui.err("要 ≥30 的整数（pro 模式单条最慢 40s+）")
    return q


# --------------------------------------------------------------------------
# 摘要 / 提交
# --------------------------------------------------------------------------
STEPS = [
    ("embedding", "向量模型", step_embedding),
    ("mode", "运行模式 MEMORY_MODE", step_mode),
    ("llm", "LLM 模型", step_llm),
    ("storage", "存储目录", step_storage),
    ("search", "检索阈值", step_search),
    ("queue", "队列参数", step_queue),
]

_STEP_TITLES = {k: t for k, t, _ in STEPS}


def _summary_rows(cfg: dict) -> list[tuple[str, str]]:
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    # 密钥一律看解析结果：明文常为空（只留 keyEnv），真正的 key 在环境变量里
    emb_key = cfgmod.resolve_embed_key(cfg)
    emb_src = "" if emb.get("apiKey") else (f"（来自 ${emb.get('keyEnv')}）" if emb.get("keyEnv") else "")
    rows = [
        ("向量模型", f"{emb.get('model')}  ({emb.get('dims')} 维, {emb.get('provider')})"),
        ("嵌入入口", str(emb.get("baseUrl") or "（本地 ONNX）")),
        ("嵌入密钥", (_mask(emb_key) or "（未设置）") + (emb_src if emb_key else "")),
        ("运行模式", str(cfg.get("mode"))),
    ]
    if cfg.get("mode") in ("pro", "ultra"):
        key = cfgmod.resolve_llm_key(cfg)
        src = "" if llm.get("apiKey") else (f"（来自 ${llm.get('keyEnv')}）" if llm.get("keyEnv") else "")
        rows += [
            ("LLM 模型", f"{llm.get('model')} @ {llm.get('baseUrl')}"),
            ("LLM 密钥", (_mask(key) or "（未设置）") + src),
            ("TLS 校验", "跳过（insecure）" if llm.get("insecureTls") else "正常"),
        ]
    size, files = storage_ops.dir_usage(str((cfg.get("storage") or {}).get("dataDir") or ""))
    rows.append(("存储目录", str((cfg.get("storage") or {}).get("dataDir")) + (f"  ({files} 文件)" if files else "")))
    rows.append(("检索阈值", str((cfg.get("search") or {}).get("minScore"))))
    return rows


def _credential_diff(old_cfg: dict, cfg: dict) -> list[tuple[str, str, str]]:
    """新旧配置里「模型凭据」的差异行（旧值 → 新值），无差异返回空表。"""
    rows: list[tuple[str, str, str]] = []
    for section, label in (("embedding", "向量模型"), ("llm", "LLM 模型")):
        old_s, new_s = old_cfg.get(section) or {}, cfg.get(section) or {}
        for field, name in (("baseUrl", "baseUrl"), ("model", "模型"), ("keyEnv", "密钥变量")):
            ov, nv = str(old_s.get(field) or ""), str(new_s.get(field) or "")
            if ov != nv:
                rows.append((f"{label} · {name}", ov or "（空）", nv or "（空）"))
    return rows


def _commit(old_cfg: dict, cfg: dict) -> int:
    """展示摘要 → 有旧凭据时提示覆盖 → 处理维度变更 → 保存 → 自检。"""
    print()
    print(tui.title("将要保存的配置"))
    print(tui.kv(_summary_rows(cfg)))

    # 用户规范（2026-10-28）：连通性通过后，如果有旧配置，要明确提示是否覆盖。
    # 旧的「写入配置？」不区分首次/覆盖；这里在有凭据变化时先摆出旧→新对比，
    # 让「覆盖」是一次知情的确认，而不是顺手回车。
    diff = _credential_diff(old_cfg, cfg)
    if diff:
        print()
        tui.warn("已有旧配置，以下几项将被覆盖：")
        for name, ov, nv in diff:
            print(f"    {name}: {ov} → {nv}")
        if not tui.confirm("覆盖旧配置？", default=True):
            tui.warn("已取消，配置未改动。")
            return 0

    changed, why = cfgmod.probe_dim_change(old_cfg, cfg)
    rebuild = False
    if changed:
        print()
        tui.warn("嵌入模型发生变化：" + why)
        data_dir = str((cfg.get("storage") or {}).get("dataDir") or "")
        size, files = storage_ops.dir_usage(data_dir)
        if files:
            print(c.dim(f"  数据目录已有 {storage_ops.human_size(size)} / {files} 个文件。"
                        "不重建的话，新旧向量混在一起，检索结果会失真。"))
            # 重建是破坏性操作，不能和「写入配置？」串成两条默认「是」的连问：
            # 那样用户一路回车就等于换模型顺手清库（2026-10-28 实测），之后不敢再改模型。
            # 这里给「先不重建」一个明确出口并设为默认，重建必须显式选。
            action = tui.select(
                "数据目录怎么处理？",
                [
                    ("重建（备份后清空，推荐）", "旧向量与新模型不兼容，混用会让检索失真", "r"),
                    ("先不重建", "只改配置；旧向量仍在，检索可能失真，之后可在 设置 → 存储 里清空重建", "k"),
                    ("放弃这次修改", "不改配置，也不动数据", "x"),
                ],
                default=1,
            )
            if action == 2:
                raise tui.Aborted()
            rebuild = action == 0

    print()
    if not tui.confirm("写入配置？", default=True):
        tui.warn("已取消，配置未改动。")
        return 0

    cfg = {**cfg, "setupComplete": True, "setupSkipped": False}
    path = cfgmod.save(cfg)
    cfgmod.apply_env(cfg)
    tui.ok(f"配置已写入 {path}")

    if rebuild:
        ok, detail = storage_ops.wipe(str(cfg["storage"]["dataDir"]))
        if ok:
            tui.ok("数据目录已重建" + (f"，原数据备份在 {detail}" if detail and detail.startswith("已备份") else ""))
        else:
            tui.err(f"重建失败：{detail}")

    print()
    print(tui.section("配置自检"))
    bad = 0
    for name, passed, detail in cfgmod.validate(cfg):
        if passed:
            print(f"  {tui.icon('ok')} {ljust(name)}{c.dim(detail)}")
        else:
            bad += 1
            print(f"  {tui.icon('err')} {ljust(name)}{c.red(detail)}")
    print()
    if bad:
        tui.warn(f"{bad} 项未通过，可重新运行 hy-mem setup 修正")
    else:
        tui.ok("配置完整")
    return 0 if bad == 0 else 1


def ljust(s: str, width: int = 22) -> str:
    return s + " " * max(0, width - tui.vwidth(s))


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def needs_setup() -> tuple[bool, str]:
    """是否该走首次设置。基于「配置是否完整」而不是「文件是否存在」。

    只看文件存在会漏掉一类真实故障：产物包里预置了一份**没填密钥**的
    config.json，文件在、配置不可用，于是首次运行直接进主界面，用户以为
    已经配好了 —— 这正是旧版的行为。
    """
    try:
        if not cfgmod.exists():
            return True, "还没有配置文件"
        cfg = cfgmod.load()
    except Exception as e:  # noqa: BLE001
        return True, f"配置读取失败：{type(e).__name__}"
    # 用户显式选过「先跳过」就不再劝，直到他自己在设置里跑一遍完整向导
    if cfg.get("setupSkipped"):
        return False, ""
    if not cfg.get("setupComplete"):
        # 旧版本配置没有这个标记。若配置本身已经完整可用，就不该再把人拽进
        # 向导（用户的原话是「每次都让我重新设置」）—— 只认「真的缺东西」。
        missing = [n for n, passed, _ in cfgmod.validate(cfg) if not passed]
        if not missing:
            return False, ""
        return True, "配置未完成首次设置"
    bad = [n for n, passed, _ in cfgmod.validate(cfg) if not passed]
    if bad:
        return True, "缺少关键项：" + "、".join(bad[:4])
    return False, ""


def run_setup(scope: str = "all", existing: dict | None = None, first_run: bool = False) -> int:
    """scope=all 走四步向导；scope=<step key> 只改一项（设置页用）。"""
    cfg = cfgmod.load() if existing is None else existing
    old_cfg = dict(cfg)

    try:
        if scope == "all":
            if first_run:
                _welcome()
            emb = step_embedding(cfg)
            cfg = {**cfg, "embedding": emb}

            mode = step_mode(cfg)
            cfg = {**cfg, "mode": mode}

            if mode in ("pro", "ultra"):
                llm = step_llm(cfg)
                cfg = {**cfg, "llm": llm}
            else:
                tui.info("lite 模式不调 LLM，跳过 LLM 设置（以后可单独在「设置」里补）")

            storage = step_storage(cfg)
            cfg = {**cfg, "storage": storage}
        else:
            step = next((s for s in STEPS if s[0] == scope), None)
            if step is None:
                tui.err(f"未知的设置项：{scope}")
                return 1
            key, title, fn = step
            print(tui.title(f"设置 · {title}"))
            value = fn(cfg)
            if key == "mode":
                cfg = {**cfg, "mode": value}
                if value in ("pro", "ultra") and not (cfg.get("llm") or {}).get("model"):
                    tui.warn("pro/ultra 需要 LLM，请再设置 LLM 模型")
            elif key == "search":
                cfg = {**cfg, "search": value}
            elif key == "queue":
                cfg = {**cfg, "queue": value}
            elif key == "storage":
                cfg = {**cfg, "storage": value}
            elif key == "llm":
                cfg = {**cfg, "llm": value}
            else:
                cfg = {**cfg, key: value}

    except tui.Aborted:
        print()
        tui.warn("已取消，配置未改动。")
        return 0

    try:
        return _commit(old_cfg, cfg)
    except tui.Aborted:
        print()
        tui.warn("已取消，配置未改动。")
        return 0


def _welcome() -> None:
    print()
    print(tui.title("欢迎使用 hy-mem"))
    print(
        tui.panel(
            [
                c.bold("首次运行需要选四件事，之后随时可改。"),
                "",
                c.cyan("1 向量模型") + "   写入/检索的语义空间，决定库的维度（只影响新库）",
                c.cyan("2 运行模式") + "   lite 纯向量最快 ／ pro 加 LLM 抽取事实并自动打标签",
                c.cyan("3 LLM 模型") + "   pro 及以上需要；lite 会自动跳过",
                c.cyan("4 存储目录") + "   记忆库落盘位置，可迁移",
                "",
                c.dim("密钥由你自己填，本程序不预置任何 key；填不全时命令会直接告诉缺哪项、怎么补。"),
                c.dim("身份默认「通用」：多个智能体共享同一份记忆；要隔离就 hy-mem user set <名字>。"),
                "",
                c.dim("全程可用 ↑↓ 选择、Enter 确认、Esc 返回；Ctrl+C 随时退出且不写入。"),
                c.dim("粘贴：Ctrl+V 或右键；复制：鼠标拖选后右键（Ctrl+C 是中断，别用它复制）。"),
                c.dim("配置保存在 ") + c.dim(str(cfgmod.config_path())),
            ]
        )
    )
    tui.pause("按任意键开始…")
