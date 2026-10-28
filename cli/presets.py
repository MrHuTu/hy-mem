"""预设库：向量模型 / LLM / 运行模式.

所有数字来自本项目实测（docs/PRD-agent-cli-bridge.md §8.9；测量脚本是一次性的，
放在本地 `.local-test/bench_embed.py`，不入库）。
**不要凭想象改 dims**：维度与 MEMORY_EMBEDDING_DIMS 不一致时 SDK 直接抛
EmbeddingDimensionMismatchError，旧数据无法读取。

区分度 = 正例相似度 / 近邻干扰相似度。它比「绝对分数高低」更关键：
hy-mem 用绝对阈值过滤（默认 0.35），所以模型整体分低不要紧，只要
「干扰项落在阈值下、正例落在阈值上」就是好模型；绝对分高但干扰项擦过阈值反而有害。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EmbeddingPreset:
    id: str
    label: str
    provider: str
    base_url: str
    model: str
    dims: int
    note: str
    latency_ms: int | None = None
    discrimination: float | None = None
    needs_key: bool = True
    # 密钥在环境变量里的变量名。0.9.11 起配置只留这个名字，明文不再落盘；
    # 预设必须把它一起带进 to_cfg()，否则「重选预设」会把用户自定义的变量名冲掉。
    key_env: str = "MEMORY_EMBEDDER_API_KEY"

    def to_cfg(self) -> dict:
        return {
            "preset": self.id,
            "provider": self.provider,
            "baseUrl": self.base_url,
            "model": self.model,
            "apiKey": "",
            "keyEnv": self.key_env if self.needs_key else "",
            "dims": self.dims,
        }


@dataclass(frozen=True)
class LlmPreset:
    id: str
    label: str
    base_url: str
    model: str
    key_env: str = ""
    insecure_tls: bool = False
    latency_ms: int | None = None
    note: str = ""

    def to_cfg(self) -> dict:
        return {
            "preset": self.id,
            "baseUrl": self.base_url,
            "model": self.model,
            "apiKey": "",
            "keyEnv": self.key_env,
            "insecureTls": self.insecure_tls,
        }


@dataclass(frozen=True)
class ModePreset:
    id: str
    label: str
    detail: str
    use_case: str
    requires_llm: bool


EMBEDDING_PRESETS: list[EmbeddingPreset] = [
    EmbeddingPreset(
        id="openai",
        label="GPT（text-embedding-3-small）",
        provider="openai",
        base_url="https://api.openai.com/v1",
        model="text-embedding-3-small",
        dims=1536,
        needs_key=True,
        key_env="OPENAI_API_KEY",
        note="OpenAI 官方 /embeddings；1536 维。本机无 key，维度按官方文档填，探针会以实测为准",
    ),
    EmbeddingPreset(
        id="zhipu",
        label="GLM（embedding-3）",
        provider="openai",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="embedding-3",
        dims=2048,
        needs_key=True,
        key_env="ZHIPU_API_KEY",
        note="智谱开放平台；embedding-3 默认 2048 维（也可申请 1024/512，探针以实测为准）",
    ),
    EmbeddingPreset(
        id="qwen-flash",
        label="百炼 qwen（qwen3.7-text-embedding-flash）",
        provider="openai",
        base_url="https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1",
        model="qwen3.7-text-embedding-flash",
        dims=1024,
        latency_ms=300,
        discrimination=2.02,
        key_env="MEMORY_EMBEDDER_API_KEY",
        note="本机实测：1024 维 / ~300ms / 区分度 2.02x，干扰项 0.327 稳落在 0.35 阈值下",
    ),
]

LLM_PRESETS: list[LlmPreset] = [
    LlmPreset(
        id="openai",
        label="GPT（gpt-4o-mini）",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        key_env="OPENAI_API_KEY",
        note="抽取够用、便宜快；换 gpt-4o 只需在自定义里改模型名",
    ),
    LlmPreset(
        id="zhipu",
        label="GLM（glm-4-flash）",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4-flash",
        key_env="ZHIPU_API_KEY",
        note="智谱开放平台，与 GLM 向量模型可共用同一把 key",
    ),
    LlmPreset(
        id="deepseek",
        label="DeepSeek（deepseek-chat）",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        key_env="DEEPSEEK_API_KEY",
        note="DeepSeek 官方只有 chat 接口，没有向量接口 —— 向量模型别选它",
    ),
]

MODE_PRESETS: list[ModePreset] = [
    ModePreset(
        id="lite",
        label="lite — 纯向量检索",
        detail="不调 LLM，写入 <1s；无事实抽取、无自动标签",
        use_case="只要语义检索、追求写入速度",
        requires_llm=False,
    ),
    ModePreset(
        id="pro",
        label="pro — 向量 + LLM 事实抽取（推荐）",
        detail="写入时抽取 l2_fact 并自动打 tag；单条 8~40s，随库体积增长",
        use_case="要沉淀结构化事实、要标签归档",
        requires_llm=True,
    ),
    ModePreset(
        id="ultra",
        label="ultra — 再加 Schema 归纳",
        detail="额外要求开启图记忆（MEMORY_ENABLE_GRAPH），开销最大",
        use_case="需要跨记忆归纳出抽象模式",
        requires_llm=True,
    ),
]


def find_embedding(key: str) -> EmbeddingPreset | None:
    for p in EMBEDDING_PRESETS:
        if key in (p.id, p.model):
            return p
    return None


def find_llm(key: str) -> LlmPreset | None:
    for p in LLM_PRESETS:
        if key in (p.id, p.model):
            return p
    return None


def find_mode(key: str) -> ModePreset | None:
    for p in MODE_PRESETS:
        if p.id == key:
            return p
    return None


def mode_index(mode: str) -> int:
    for i, p in enumerate(MODE_PRESETS):
        if p.id == mode:
            return i
    return 1


def embedding_index(cfg_emb: dict) -> int:
    model = str(cfg_emb.get("model") or "")
    for i, p in enumerate(EMBEDDING_PRESETS):
        if p.model == model and p.provider == cfg_emb.get("provider"):
            return i
    return -1  # 自定义


def llm_index(cfg_llm: dict) -> int:
    model = str(cfg_llm.get("model") or "")
    base = str(cfg_llm.get("baseUrl") or "")
    for i, p in enumerate(LLM_PRESETS):
        if p.model == model and p.base_url == base:
            return i
    return -1
