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

    def to_cfg(self) -> dict:
        return {
            "preset": self.id,
            "provider": self.provider,
            "baseUrl": self.base_url,
            "model": self.model,
            "apiKey": "",
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
        id="qwen-flash",
        label="通义 qwen3.7-embedding-flash（推荐）",
        provider="openai",
        base_url="https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1",
        model="qwen3.7-text-embedding-flash",
        dims=1024,
        latency_ms=300,
        discrimination=2.02,
        note="区分度最高 2.02x；干扰项 0.327 稳落在 0.35 阈值下，不会误召回",
    ),
    EmbeddingPreset(
        id="text-embedding-v4",
        label="通义 text-embedding-v4",
        provider="openai",
        base_url="https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1",
        model="text-embedding-v4",
        dims=1024,
        latency_ms=293,
        discrimination=1.98,
        note="绝对分更高（命中 0.821），但干扰项 0.415 会擦过阈值，可能混入无关结果",
    ),
    EmbeddingPreset(
        id="qwen-embedding",
        label="通义 qwen3.7-embedding（标准版）",
        provider="openai",
        base_url="https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1",
        model="qwen3.7-text-embedding",
        dims=1024,
        latency_ms=313,
        discrimination=1.78,
        note="区分度 1.78x，介于 flash 与本地模型之间",
    ),
    EmbeddingPreset(
        id="bge-m3-silicon",
        label="硅基流动 BAAI/bge-m3",
        provider="openai",
        base_url="https://api.siliconflow.cn/v1",
        model="BAAI/bge-m3",
        dims=1024,
        latency_ms=350,
        discrimination=None,
        note="中文效果好、有免费额度，需注册 siliconflow.cn 取 key（本机未实测）",
    ),
    EmbeddingPreset(
        id="local-bge-small",
        label="本地 ONNX bge-small-zh（零 key / 全离线）",
        provider="local",
        base_url="",
        model="BAAI/bge-small-zh-v1.5",
        dims=512,
        latency_ms=216,
        discrimination=1.47,
        needs_key=False,
        note="零成本零网络；但 512 维且区分度仅 1.47x，干扰项 0.513 高于阈值",
    ),
]

LLM_PRESETS: list[LlmPreset] = [
    LlmPreset(
        id="ysjx-deepseek-flash",
        label="本地网关 ysjx / deepseek-v4-flash（推荐）",
        base_url="https://newapi.jxdata.com:18000/v1",
        model="deepseek-v4-flash",
        key_env="YSJX_API_KEY",
        insecure_tls=True,
        latency_ms=390,
        note="实测抽取质量可用、约 0.4s；key 从环境变量继承，不落盘",
    ),
    LlmPreset(
        id="ysjx-glm",
        label="本地网关 ysjx / glm-5.3",
        base_url="https://newapi.jxdata.com:18000/v1",
        model="glm-5.3",
        key_env="YSJX_API_KEY",
        insecure_tls=True,
        latency_ms=500,
        note="同一网关的备选模型",
    ),
    LlmPreset(
        id="qwen-maas",
        label="百炼 MaaS / qwen 系列",
        base_url="https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1",
        model="qwen-plus",
        key_env="",
        insecure_tls=False,
        note="可与向量模型共用同一个 key；模型名按实际开通的填",
    ),
    LlmPreset(
        id="deepseek-official",
        label="DeepSeek 官方 api.deepseek.com",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        key_env="",
        insecure_tls=False,
        note="需自行提供 sk- 开头的 key",
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
