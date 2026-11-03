"""hy-mem 的配置层 —— config.json 是设置的单一真源.

为什么要有它（而不是继续堆 .env）：
  1. .env 只能平铺 KEY=VALUE，表达不了「当前选的是哪个预设」「改过哪些项」。
  2. 设置向导要能读回当前值做「当前 ←」标记，纯文本 env 反向解析容易错。
  3. 切换向量模型必须原子地同时改 provider/base_url/model/dims 四项，
     分散在 4 个 env 里改，漏一个就是 EmbeddingDimensionMismatchError。

与 .env 的关系（优先级从高到低）：
    命令行参数  >  config.json  >  .env  >  进程真实环境变量（仅密钥例外）
config.json 存在时，其字段**强制覆盖** .env 同名项 —— 因为它是用户在向导里
显式选择的；.env 退回为「config.json 没覆盖到的杂项」兜底。

密钥策略：config.json 里的 llm.apiKey 默认留空，改为从 keyEnv 指定的环境变量
继承（本项目是 YSJX_API_KEY）。只有用户明确要求保存时才落盘。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1
CONFIG_NAME = "config.json"

# 「通用身份」的内部 id。之所以不叫 general/公共，是因为它必须和历史数据
# 对齐：早期版本未设 user_id 时落库用的就是 "default"，改名等于让老记忆
# 「消失」。对外展示统一说「通用」，对 SDK 仍然是 default。
GENERAL_USER_ID = "default"
GENERAL_LABEL = "通用"


def _root() -> Path:
    """安装根目录。冻结成 exe 后 __file__ 不可靠，交给 hymem.cli 统一判断。"""
    env = os.getenv("HY_MEM_HOME")
    if env:
        return Path(env).resolve()
    from hymem.core.paths import app_root as _ar

    return _ar()


def config_path() -> Path:
    p = os.getenv("HY_MEM_CONFIG")
    if p:
        return Path(p)
    return _root() / CONFIG_NAME


# --------------------------------------------------------------------------
# 默认值
# --------------------------------------------------------------------------


DEFAULTS: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "setupComplete": False,
    "setupSkipped": False,
    "mode": "pro",
    # 记忆后端选择（docs/specs/2026-11-02-cli-split-ports/，JDBC 式协议层）：
    # 协议在 hymem/ports/，实现在 hymem/backends/。默认 hy-memory；
    # 未来接 memvid 等开源实现时在 ports/registry.py 注册新名字即可。
    "backend": {
        "memory": "hy-memory",
    },
    # serve 固定端口（U3 加固，docs/specs/2026-11-02-endpoint-refresh/）：
    # 0 = 随机空闲端口（历史行为）。设为固定值后 serve 重启不再换端口，
    # 长驻 client 冻结的向量端点恒有效；被占用时回退随机并 stderr 告警。
    "servePort": 0,
    # 界面语言（zh / en）。放顶层而不是 ui.language：后者是历史遗留、从未生效，
    # 真正决定文案的是 i18n.lang()，它读的就是这个字段。
    "lang": "zh",
    "embedding": {
        "preset": "qwen-flash",
        "provider": "openai",
        "baseUrl": "https://cn-beijing.maas.qianwenaiapi.com/compatible-mode/v1",
        "model": "qwen3.7-text-embedding-flash",
        "apiKey": "",
        # 密钥只放环境变量；这里记的是变量名，轮换 key 不用改配置
        "keyEnv": "MEMORY_EMBEDDER_API_KEY",
        "dims": 1024,
    },
    "llm": {
        "preset": "ysjx-deepseek-flash",
        "baseUrl": "https://newapi.jxdata.com:18000/v1",
        "model": "deepseek-v4-flash",
        "apiKey": "",
        "keyEnv": "YSJX_API_KEY",
        "insecureTls": True,
    },
    "storage": {
        "dataDir": "",
    },
    # 身份默认值：智能体调用时不该被迫传 -u。留空则回落到环境变量，
    # 再回落到 "default"（= 通用）。agent_id 故意不设默认 —— SDK 自己会用
    # default_agent，一旦我们另设一个，旧的隔离键（user:default_agent）
    # 就对不上，历史记忆会「消失」。
    #
    # user_id 的语义（多智能体场景的核心）：
    #   * 不设 → 通用（default）：所有智能体共享同一份记忆，这是默认行为；
    #   * 设了 → 该 id 自成一套记忆，与其它 id 互不可见，用于隔离；
    #   * 切换只改这一个字段，不动数据目录、不动模型配置。
    "identity": {
        "userId": "",
        "known": [],
    },
    "search": {
        "minScore": 0.35,
    },
    # coding 场景专属（2026-10-30 S4）：diff 版本链的仓库根。留空则链层只从
    # 工具轨迹里的 path/old/new 确定性替换动作识别改动；填了本地 Git 仓库路径
    # 才会去读工作区 diff。检索侧是否注入 diff 块不在这里——那是按次的
    # --chain-block 开关（默认关，省 token）。
    "coding": {
        "chainRepo": "",
    },
    # L6 认知层（ultra 专属，2026-10-30）。evolve 会现造一个只用于归纳的 client：
    # model 留空表示沿用 llm.model（但强烈建议填强模型——意图分类模型做不了
    # 开放生成，实测只回 7 个 token 就放弃）；maxTokens 是 agent 单轮输出上限，
    # SDK 默认 2000 会把 schema JSON 截断；schemaLimit/MinScore 只作用于检索
    # 正路的语义通道（反路首版不做）。schemaMinScore=0.25 的依据：evolve 归纳的
    # schema 是英文（SDK 提示词决定），中文 query 对它的语义分实测 0.17~0.34，
    # 0.4（SDK 面向英文场景的默认）会全部滤空——真实库 4 条 schema × 4 个中文
    # query 实测最高 0.3445，全被 0.4 挡掉（docs/specs/2026-10-30-l6-schema-empty/）。
    # language：归纳输出语言。"" 跟随 SDK 检测——但它对技术型中文记忆失灵
    # （zh_ratio 阈值 0.7，开发者内容 ASCII 密度高，实测 0.318 判 en），
    # "zh"/"en" 强制注入输出语言指令，无视检测结果。上游 issue 已提，SDK
    # 修复前靠这个自保（docs/specs/2026-10-30-evolve-language/）。
    "evolve": {
        "model": "",
        "maxTokens": 8000,
        "schemaLimit": 3,
        "schemaMinScore": 0.25,
        "language": "",
        # 自动沉淀（docs/specs/2026-10-30-evolve-auto/）：不再靠模型手动调
        # evolve 工具，add 之后顺带检查，素材攒够就后台跑（事件驱动，无
        # 定时线程）。enabled 仅 ultra 生效；minNewFacts 是「自上次沉淀以来
        # 新增 L2 fact 数」的触发阈值——SDK 的 no_clusters 让试跑零成本，
        # 阈值只是防孤簇白烧 LLM；cooldownHours 是防跑飞的硬约束（对话
        # 密集时一天几十次 add，没有它每条都触发检查起线程）。
        "auto": {
            "enabled": True,
            "minNewFacts": 8,
            "cooldownHours": 24,
        },
    },
    "queue": {
        "maxAttempts": 3,
        "lockTimeout": 180,
        "staleSec": 900,
    },
    "health": {
        "countWarn": 200,
        "countCrit": 500,
        "latWarn": 30,
        "latCrit": 60,
    },
    "ui": {
        "color": True,
        "language": "zh",
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def exists() -> bool:
    return config_path().exists()


# 最近一次 load_raw 的失败信息。损坏的 config.json 会被隔离成 .bad-<ts>，
# 真正隔离不了时（权限/只读卷）由 config set 拒绝覆盖——绝不能让默认值
# 静默盖掉用户原来的密钥配置。


_last_load_error: dict[str, Any] | None = None


def load_error() -> dict[str, Any] | None:
    """最近一次读取失败的信息：path/reason/backup（backup 为 None 表示没备成）。"""
    return dict(_last_load_error) if _last_load_error else None


def _quarantine(p: Path) -> Path | None:
    """把读不动的配置文件挪到 .bad-<ts>，保住原内容再继续。失败返回 None。"""
    dst = p.with_name(f"{p.name}.bad-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        p.replace(dst)
        return dst
    except OSError:
        return None


def load_raw() -> dict:
    global _last_load_error
    p = config_path()
    if not p.exists():
        _last_load_error = None
        return {}
    try:
        # utf-8-sig：BOM 是编码问题，不该当损坏文件处理
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"配置根不是 JSON 对象，而是 {type(data).__name__}")
        _last_load_error = None
        return data
    except (json.JSONDecodeError, OSError, ValueError) as e:
        backup = _quarantine(p)
        _last_load_error = {
            "path": str(p),
            "reason": f"{type(e).__name__}: {e}",
            "backup": str(backup) if backup else None,
        }
        kept = f"已备份到 {backup}" if backup else "无法备份，文件留在原处（config set 将拒绝覆盖）"
        print(f"hy-mem: 配置文件读取失败（{_last_load_error['reason']}），{kept}", file=sys.stderr)
        return {}


def _fill_from_env(cfg: dict) -> None:
    """config.json 不存在时，从已加载的环境变量（含 .env）回填。

    动机：本项目先用 .env 跑通过一段时间，用户再进向导时如果看到一堆默认值，
    等于要重填一遍，且会误以为配置丢了。回填后「当前 ←」标记能正确指到
    现有配置上，向导退化成「确认 + 微调」。

    只在 config.json 不存在时调用：一旦落盘，它就是唯一真源，不再被 env 反向污染。
    """
    env = os.environ

    def take(name: str) -> str:
        return env.get(name, "").strip()

    mode = take("MEMORY_MODE")
    if mode in ("lite", "pro", "ultra"):
        cfg["mode"] = mode

    emb = cfg["embedding"]
    provider = take("MEMORY_EMBEDDER_PROVIDER")
    if provider:
        emb["provider"] = provider
    if emb["provider"] == "local":
        m = take("MEMORY_LOCAL_EMBED_MODEL")
        if m:
            emb["model"] = m
        emb["baseUrl"] = ""
    else:
        for env_name, key in (
            ("MEMORY_EMBEDDER_BASE_URL", "baseUrl"),
            ("MEMORY_EMBEDDER_MODEL", "model"),
            ("MEMORY_EMBEDDER_API_KEY", "apiKey"),
        ):
            v = take(env_name)
            if v:
                emb[key] = v
    dims = take("MEMORY_EMBEDDING_DIMS")
    if dims.isdigit():
        emb["dims"] = int(dims)

    llm = cfg["llm"]
    for env_name, key in (
        ("MEMORY_LLM_BASE_URL", "baseUrl"),
        ("MEMORY_LLM_MODEL", "model"),
        ("MEMORY_LLM_API_KEY", "apiKey"),
    ):
        v = take(env_name)
        if v:
            llm[key] = v
    # MEMORY_LLM_API_KEY 常由 YSJX_API_KEY 继承而来，这种情况不该把它当"用户手填的 key"，
    # 否则配置里会固化一份密钥副本，key 轮换后两处不一致。
    key_env = llm.get("keyEnv") or ""
    if key_env and take("MEMORY_LLM_API_KEY") and take("MEMORY_LLM_API_KEY") == take(key_env):
        llm["apiKey"] = ""

    data_dir = take("MEMORY_DATA_DIR")
    if data_dir:
        cfg["storage"]["dataDir"] = data_dir

    ms = take("HY_MEM_MIN_SCORE")
    if ms:
        try:
            cfg["search"]["minScore"] = float(ms)
        except ValueError:
            # 坏值不能静默：用户以为设了阈值，实际跑的是默认值。告警后忽略，
            # 保留配置文件里的 minScore（没有则用默认 0.35）。
            print(
                f"hy-mem: 忽略非法的 HY_MEM_MIN_SCORE={ms!r}（应为数字，如 0.35）",
                file=sys.stderr,
            )


def load() -> dict:
    """返回与默认值深合并后的完整配置。

    环境变量优先级（0.9.10 / PRD F6）：只有**没有配置文件**时才从 env 回填
    是老实现，导致「设了 MEMORY_DATA_DIR=<沙箱> 却仍写进安装目录」——测试
    会污染真实记忆库，多实例也无法隔离。现在两个目录类变量无条件覆盖配置：

      MEMORY_DATA_DIR   队列库 + 端口文件所在目录（store.data_dir 读它）
      HY_MEM_DATA_DIR   默认数据目录（仅当 storage.dataDir 为空时生效）

    其余变量（密钥、模型、模式）仍只在无配置文件时回填，避免 shell 里的
    临时变量悄悄改掉用户保存的设置。
    """
    cfg = _deep_merge(DEFAULTS, load_raw())
    if not exists():
        _fill_from_env(cfg)
    env_dd = os.getenv("MEMORY_DATA_DIR", "").strip()
    if env_dd:
        cfg["storage"]["dataDir"] = os.path.expandvars(os.path.expanduser(env_dd))
    # storage.dataDir 留空表示「用安装根下的 data」，避免默认值写死机器路径
    if not cfg["storage"].get("dataDir"):
        cfg["storage"]["dataDir"] = str(default_data_dir())
    return cfg


def data_dir_source() -> str:
    """当前 storage.dataDir 来自哪里：env / config / default（doctor 用）。"""
    if os.getenv("MEMORY_DATA_DIR", "").strip():
        return "env:MEMORY_DATA_DIR"
    if str((load_raw().get("storage") or {}).get("dataDir") or "").strip():
        return "config"
    if os.getenv("HY_MEM_DATA_DIR", "").strip():
        return "env:HY_MEM_DATA_DIR"
    return "default"


def identity_defaults(cfg: dict | None = None) -> str:
    """解析默认 user_id：环境变量 > 配置文件 > 通用（default）。

    优先级把环境变量放前面，是为了让多智能体临时隔离身份时不必改配置
    （`HY_MEM_USER_ID=writer hy-mem add ...` 一条命令换身份）。
    """
    env = os.getenv("HY_MEM_USER_ID", "").strip()
    if env:
        return env
    return identity_configured(cfg) or GENERAL_USER_ID


def identity_configured(cfg: dict | None = None) -> str:
    """只读配置里显式设定的 user_id（不看环境变量）。空串表示「通用」。"""
    cfg = cfg if cfg is not None else load()
    return str((cfg.get("identity") or {}).get("userId") or "").strip()


def identity_source(cfg: dict | None = None) -> str:
    """当前生效身份来自哪里，用于 doctor / user show 说清来路。"""
    if os.getenv("HY_MEM_USER_ID", "").strip():
        return "env"
    if identity_configured(cfg):
        return "config"
    return "general"


def identity_label(uid: str) -> str:
    """把内部 id 翻成给人看的名字：default → 通用（default）。

    文案走 i18n（延迟 import：本模块被 i18n 自己依赖，顶层导入会绕成环）。
    """
    if uid != GENERAL_USER_ID:
        return uid
    # S1 分层（2026-10-29）：core 不依赖 ui —— 报错/标签文案的 i18n 属于
    # UI 层关注点，这里恒用中文（GENERAL_LABEL）。
    return f"{GENERAL_LABEL}（{uid}）"


def set_user_id(uid: str) -> tuple[Path, str]:
    """只改身份，不碰任何别的配置。传空串 = 回到通用。

    返回 (配置文件路径, 生效后的 user_id)。
    """
    cfg = load()
    ident = dict(cfg.get("identity") or {})
    uid = (uid or "").strip()
    if uid in ("", "-", GENERAL_USER_ID, GENERAL_LABEL):
        ident["userId"] = ""
        effective = GENERAL_USER_ID
    else:
        ident["userId"] = uid
        effective = uid
    known = [str(x) for x in (ident.get("known") or []) if str(x).strip()]
    if effective not in known:
        known.append(effective)
    ident["known"] = known[-20:]
    cfg["identity"] = ident
    return save(cfg), effective



def default_data_dir() -> Path:
    """默认数据目录：安装根下的 data（整包拷贝即迁移）。

    想放到别处（例如不写安装目录的规范场景）就把 storage.dataDir 或
    环境变量 HY_MEM_DATA_DIR 指过去。
    """
    env = os.getenv("HY_MEM_DATA_DIR", "").strip()
    if env:
        return Path(os.path.expandvars(os.path.expanduser(env)))
    return _root() / "data"


def reset() -> None:
    p = config_path()
    if p.exists():
        p.unlink()


def save(cfg: dict) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = copy.deepcopy(cfg)
    payload["version"] = CONFIG_VERSION
    # 只写回非默认的 dataDir，避免把安装根的绝对路径固化进配置
    if payload["storage"].get("dataDir") == str(default_data_dir()):
        payload["storage"]["dataDir"] = ""
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    _tighten(p)
    return p


def _tighten(p: Path) -> None:
    """配置里有密钥，尽量收紧权限。Windows 上 os.chmod 语义有限，尽力而为。"""
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 映射到环境变量
# --------------------------------------------------------------------------


def resolve_llm_key(cfg: dict) -> str:
    """LLM 密钥：配置里的明文 > keyEnv 指定的环境变量。"""
    llm = cfg.get("llm") or {}
    if llm.get("apiKey"):
        return str(llm["apiKey"])
    env_name = llm.get("keyEnv") or ""
    if env_name:
        return os.getenv(env_name, "")
    return ""


def resolve_embed_key(cfg: dict) -> str:
    """向量模型密钥：配置里的明文 > keyEnv 指定的环境变量 > 约定变量名。

    第三个来源是历史行为（配置里 key 留空、keyEnv 也没写，靠
    MEMORY_EMBEDDER_API_KEY 跑），保留它是为了让升级前就配好环境变量的机器继续能用。

    但 **keyEnv 一旦显式写了名字，就只认那个变量，不再回落到约定变量名**。
    原因是 2026-10-28 的 401 事故：换到新端点后 keyEnv 指向新端点的变量名（值为空），
    再回落去读旧的 $MEMORY_EMBEDDER_API_KEY，等于拿别家的 key 打新端点 ——
    报 401 而不是「没配 key」，用户会一直以为是自己粘贴错了。
    """
    emb = cfg.get("embedding") or {}
    if emb.get("apiKey"):
        return str(emb["apiKey"])
    env_name = str(emb.get("keyEnv") or "")
    if env_name:
        return os.getenv(env_name, "")
    return os.getenv("MEMORY_EMBEDDER_API_KEY", "")


# 从聊天窗口/文档粘密钥时最容易带进来的不可见字符


_INVISIBLE = {
    "\ufeff": "BOM 标记",
    "\u200b": "零宽空格",
    "\u200c": "零宽连接符",
    "\u200d": "零宽连字",
    "\u00a0": "不换行空格",
    "\u3000": "全角空格",
}


def secret_charset_problem(raw: str) -> str | None:
    """密钥只可能是 ASCII 可见字符；返回问题描述，None 表示没问题。

    为什么必须校验：key 最终进 HTTP 头 `Authorization: Bearer <key>`，而
    `http.client` 用 **latin-1** 编码头，遇到中文会在「发请求」那一刻抛
    `UnicodeEncodeError: 'latin-1' codec can't encode characters in position 7-9`
    —— 第 7 位之后正好是 key 的头几个字符（"Bearer " 占 7 位）。请求根本没
    发出，但报错长得像网络故障，界面上只剩 `HTTP 0` 或一串偏移量。
    2026-10-27 有用户在向导里把中文粘进密钥框，就这么卡住。

    放在 config 里而不是 tui 里，是因为三处都要用同一套判定：
    向导输入口（`tui.ask_secret`）、配置闸门（`readiness`）、doctor 探活。
    """
    for pos, ch in enumerate(raw):
        if ch in _INVISIBLE:
            return f"第 {pos + 1} 位是{_INVISIBLE[ch]}（多半是粘自聊天窗口）"
        if ord(ch) > 127:
            return f"第 {pos + 1} 位是「{ch}」（非 ASCII 字符）"
        if ch.isspace():
            return f"第 {pos + 1} 位是空白字符"
        if ord(ch) < 32:
            return f"第 {pos + 1} 位是控制字符（0x{ord(ch):02x}）"
    return None


def to_env(cfg: dict) -> dict[str, str]:
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    env: dict[str, str] = {
        "MEMORY_MODE": str(cfg.get("mode") or "pro"),
        "MEMORY_DATA_DIR": str((cfg.get("storage") or {}).get("dataDir") or ""),
        "MEMORY_PIPELINE_TRACE_ENABLED": "false",
        "MEMORY_EMBEDDER_PROVIDER": str(emb.get("provider") or "openai"),
        "MEMORY_EMBEDDING_DIMS": str(emb.get("dims") or 1024),
        "HY_MEM_MIN_SCORE": str((cfg.get("search") or {}).get("minScore", 0.35)),
        "HY_MEM_QUEUE_MAX_ATTEMPTS": str((cfg.get("queue") or {}).get("maxAttempts", 3)),
        "HY_MEM_LOCK_TIMEOUT": str((cfg.get("queue") or {}).get("lockTimeout", 180)),
        "HY_MEM_STALE_SEC": str((cfg.get("queue") or {}).get("staleSec", 900)),
        "HY_MEM_HEALTH_COUNT_WARN": str((cfg.get("health") or {}).get("countWarn", 200)),
        "HY_MEM_HEALTH_COUNT_CRIT": str((cfg.get("health") or {}).get("countCrit", 500)),
        "HY_MEM_HEALTH_LAT_WARN": str((cfg.get("health") or {}).get("latWarn", 30)),
        "HY_MEM_HEALTH_LAT_CRIT": str((cfg.get("health") or {}).get("latCrit", 60)),
        "HY_MEM_HOME": str(_root()),
    }
    if env["MEMORY_EMBEDDER_PROVIDER"] == "local":
        env["MEMORY_LOCAL_EMBED_MODEL"] = str(emb.get("model") or "")
    else:
        env["MEMORY_EMBEDDER_BASE_URL"] = str(emb.get("baseUrl") or "")
        env["MEMORY_EMBEDDER_MODEL"] = str(emb.get("model") or "")
        env["MEMORY_EMBEDDER_API_KEY"] = resolve_embed_key(cfg)

    env["MEMORY_LLM_BASE_URL"] = str(llm.get("baseUrl") or "")
    env["MEMORY_LLM_MODEL"] = str(llm.get("model") or "")
    # agent 单轮输出上限（2026-10-30 L6 实测）：SDK 默认 2000，而 S2 归纳一轮要
    # 输出上千 token，撞上限后 JSON 不闭合 → "Failed to parse operations JSON"，
    # 表现为「deepseek 时好时坏」，其实是截断。max_tokens 只是上限，放宽不会让
    # S1 抽取多写内容（它本来就输出很短）。
    env["MEMORY_AGENT_MAX_TOKENS"] = str((cfg.get("evolve") or {}).get("maxTokens", 8000))
    # coding diff 链（S4）：只在填了仓库根时注入，空值不写——SDK 拿不到这个
    # 变量就退回「只认工具轨迹里的 path/old/new」，不会去猜仓库。
    chain_repo = str((cfg.get("coding") or {}).get("chainRepo") or "")
    if chain_repo:
        env["MEMORY_CODING_CHAIN_REPO_ROOT"] = chain_repo
    key = resolve_llm_key(cfg)
    if key:
        env["MEMORY_LLM_API_KEY"] = key
    if llm.get("insecureTls"):
        env["PYTHONHTTPSVERIFY"] = "0"
        env["CURL_CA_BUNDLE"] = ""
    return env


def apply_env(cfg: dict) -> None:
    """写入进程环境。config.json 是显式选择，因此覆盖 .env 已设的值。"""
    for k, v in to_env(cfg).items():
        if v != "":
            os.environ[k] = v


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def _as_int(value: Any) -> tuple[int | None, str | None]:
    """安全转 int：返回 (值, 错误说明)。None/空串表示「未设置」，不算错误。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, bool):
        return None, f"{value!r} 不是整数"
    try:
        if isinstance(value, float):
            if not value.is_integer():
                return None, f"{value!r} 不是整数"
            return int(value), None
        return int(str(value).strip()), None
    except (TypeError, ValueError):
        return None, f"{value!r} 不是整数"


def _as_float(value: Any) -> tuple[float | None, str | None]:
    """安全转 float：返回 (值, 错误说明)。None/空串表示「未设置」，不算错误。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, bool):
        return None, f"{value!r} 不是数字"
    try:
        return float(str(value).strip()), None
    except (TypeError, ValueError):
        return None, f"{value!r} 不是数字"


def validate(cfg: dict) -> list[tuple[str, bool, str]]:
    """返回 [(检查项, 是否通过, 说明)]。不发起网络请求，纯静态校验。"""
    out: list[tuple[str, bool, str]] = []
    _le = load_error()
    if _le:
        _where = f"；已备份到 {_le['backup']}" if _le.get("backup") else "；无法备份，config set 会拒绝覆盖"
        out.append(("config.json", False, f"{_le['reason']}{_where}"))
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    mode = cfg.get("mode")

    out.append(("mode", mode in ("lite", "pro", "ultra"), f"MEMORY_MODE={mode}"))

    # 记忆后端名：白名单来自 ports/registry（延迟 import 防环）。
    # import 失败是真实故障，必须 FAIL 并带上原因——绝不能吞成 True。
    _bk = str(((cfg.get("backend") or {}).get("memory")) or "hy-memory")
    try:
        from hymem.ports.registry import available_backends as _ab

        out.append(("backend.memory", _bk in _ab(), _bk))
    except Exception as e:  # noqa: BLE001
        out.append(("backend.memory", False, f"{_bk}（注册表不可用：{type(e).__name__}: {e}）"))

    provider = str(emb.get("provider") or "").lower()
    out.append(("embedding.provider", provider in ("openai", "local"), f"provider={provider}"))
    dims, dims_err = _as_int(emb.get("dims"))
    if dims_err:
        out.append(("embedding.dims", False, f"非法值：{dims_err}"))
    elif dims is None:
        out.append(("embedding.dims", False, "未设置"))
    else:
        out.append(("embedding.dims", dims > 0, f"dims={dims}"))
    if provider == "openai":
        out.append(("embedding.baseUrl", bool(emb.get("baseUrl")), str(emb.get("baseUrl") or "未设置")))
        out.append(("embedding.model", bool(emb.get("model")), str(emb.get("model") or "未设置")))
        emb_key = resolve_embed_key(cfg)
        emb_src = "config.json" if emb.get("apiKey") else (emb.get("keyEnv") or "MEMORY_EMBEDDER_API_KEY")
        out.append(("embedding.apiKey", bool(emb_key), f"来自 {emb_src}" if emb_key else f"{emb_src} 为空"))

    if mode in ("pro", "ultra"):
        out.append(("llm.baseUrl", bool(llm.get("baseUrl")), str(llm.get("baseUrl") or "未设置")))
        out.append(("llm.model", bool(llm.get("model")), str(llm.get("model") or "未设置")))
        key = resolve_llm_key(cfg)
        src = "config.json" if llm.get("apiKey") else (llm.get("keyEnv") or "环境变量")
        out.append(("llm.apiKey", bool(key), f"来自 {src}" if key else f"{src} 为空"))

    dd = str((cfg.get("storage") or {}).get("dataDir") or "")
    looks_ok = bool(dd) and (os.path.isabs(dd) or "\\" in dd or "/" in dd)
    out.append(("storage.dataDir", looks_ok, dd if looks_ok else f"{dd!r} 不像绝对路径"))

    # coding diff 链仓库根：留空合法（只认工具轨迹里的 path/old/new），
    # 填了就必须是带 .git 的真实目录——否则链层 best-effort 静默跳过 diff，
    # 用户会以为链开着。
    cr = str((cfg.get("coding") or {}).get("chainRepo") or "")
    if not cr:
        out.append(("coding.chainRepo", True, "未设置（只认工具轨迹里的 path/old/new）"))
    else:
        p = Path(cr).expanduser()
        ok = p.is_dir() and (p / ".git").exists()
        out.append(("coding.chainRepo", ok, cr if ok else f"{cr} 不是 Git 仓库根目录"))

    # L6 认知层（ultra 专属）。model 留空合法（沿用 llm.model），但要提示：
    # 意图分类模型做不了开放归纳，实测只回 7 个 token 就放弃，等于白跑。
    # 取值一律回落 DEFAULTS：老配置/部分配置里没有这一段，直接 .get(...) or 0
    # 会把「缺省」误判成「填了非法值」，setup 的校验就会莫名报红。
    # 注意 schemaLimit 的 0 是合法值（= 关检索），不能用 or 兜底。
    ev = cfg.get("evolve") or {}
    ev_def = DEFAULTS["evolve"]
    ev_model = str(ev.get("model") or "")
    out.append((
        "evolve.model",
        True,
        f"{ev_model}" if ev_model else f"未设置，沿用 llm.model={llm.get('model') or '未设置'}",
    ))
    mt, mt_err = _as_int(ev.get("maxTokens"))
    if mt is None and mt_err is None:
        mt = int(ev_def["maxTokens"])
    out.append((
        "evolve.maxTokens",
        False if mt_err else mt >= 2000,
        f"非法值：{mt_err}" if mt_err else f"{mt}（低于 2000 会截断 schema JSON）",
    ))
    sl, sl_err = _as_int(ev.get("schemaLimit"))
    if sl is None and sl_err is None:
        sl = int(ev_def["schemaLimit"])
    out.append((
        "evolve.schemaLimit",
        False if sl_err else 0 <= sl <= 10,
        f"非法值：{sl_err}" if sl_err else f"{sl}（0 关检索，上限 10）",
    ))
    sms, sms_err = _as_float(ev.get("schemaMinScore"))
    if sms is None and sms_err is None:
        sms = float(ev_def["schemaMinScore"])
    out.append((
        "evolve.schemaMinScore",
        False if sms_err else 0.0 < sms <= 1.0,
        f"非法值：{sms_err}" if sms_err else
        f"{sms}（中文 query 对英文 schema 实测 0.17~0.34，0.25 落在相关/无关空档；"
        "过高会静默滤空，过低会放进无关归纳）",
    ))
    out.append((
        "evolve.language",
        str(ev.get("language") or "") in ("", "zh", "en"),
        f"{ev.get('language') or '(auto 跟随 SDK 检测)'}（zh/en 强制归纳输出语言，"
        "技术型中文记忆被归纳成英文 schema 时配 zh）",
    ))
    _auto = ev.get("auto") or {}
    _auto_def = ev_def.get("auto", {})
    _auto_enabled = bool(_auto.get("enabled", _auto_def.get("enabled", True)))
    _auto_min, _auto_min_err = _as_int(_auto.get("minNewFacts"))
    if _auto_min is None and _auto_min_err is None:
        _auto_min = int(_auto_def.get("minNewFacts") or 8)
    _auto_cd, _auto_cd_err = _as_float(_auto.get("cooldownHours"))
    if _auto_cd is None and _auto_cd_err is None:
        _auto_cd = float(_auto_def.get("cooldownHours") or 24)
    if _auto_min_err or _auto_cd_err:
        out.append((
            "evolve.auto",
            False,
            f"非法值：{_auto_min_err or _auto_cd_err}（minNewFacts/cooldownHours 必须是数字）",
        ))
    else:
        out.append((
            "evolve.auto",
            _auto_min >= 1 and _auto_cd > 0,
            f"enabled={_auto_enabled}（仅 ultra 生效）, minNewFacts={_auto_min}, "
            f"cooldownHours={_auto_cd:g}——add 后自动沉淀 L6，失败按 2^n 退避（上限 7 天）",
        ))
    # 注意：这条恒为 True。pro/lite 下 L6 不可用是**预期状态**不是配置错误，
    # 标红会让用户以为配坏了。想用 L6 的人看到提示就知道该切 ultra。
    if mode != "ultra":
        out.append((
            "evolve.mode",
            True,
            f"mode={mode}，L6 不启用（evolve 会被拒；要归纳先 config set mode=ultra）",
        ))
    return out


def probe_dim_change(old_cfg: dict, new_cfg: dict) -> tuple[bool, str]:
    """切换嵌入模型时判断是否需要重建数据目录。

    维度变化是硬不兼容：旧向量读不出来，SDK 直接抛
    EmbeddingDimensionMismatchError。模型变了但维度相同（如 flash→v4 都是
    1024）技术上也应重建，否则新旧向量来自不同语义空间，检索会失真。
    """
    oe = old_cfg.get("embedding") or {}
    ne = new_cfg.get("embedding") or {}
    same_model = (oe.get("model"), oe.get("dims")) == (ne.get("model"), ne.get("dims"))
    if same_model:
        return False, ""
    if int(oe.get("dims") or 0) != int(ne.get("dims") or 0):
        return True, (
            f"维度 {oe.get('dims')} → {ne.get('dims')}：旧向量完全不可读，必须重建数据目录"
        )
    return True, (
        f"模型 {oe.get('model')} → {ne.get('model')}（维度同为 {ne.get('dims')}）："
        f"维度兼容但语义空间不同，混用会导致检索失真，建议重建"
    )


def coding_enabled() -> bool:
    """SDK 生产力链路（coding/enable）是否开启——与 `CodingConfig.enable` 同口径。

    SDK 只从环境变量 `MEMORY_CODING_ENABLED` 读该开关（默认 True，
    见 vendor/hy-memory/hy_memory/config.py）；config.json 的 `coding` 段只有
    `chainRepo`，不承载 enable，因此这里也必须以 env 为唯一事实源。
    """
    v = os.getenv("MEMORY_CODING_ENABLED")
    if v is None:
        return True
    return v.lower() in ("true", "1", "yes", "on")


def scene_status(mode: str | None = None) -> tuple[bool, str]:
    """scene（working/coding）在当前配置下是否真实可用；返回 (可用, 原因)。

    与 SDK 的生效条件对齐（vendor client.py 的 add/search scene 分支）：
    `mode != lite` 且 coding 链路开启。lite 只做 chat 原始向量检索，
    显式 scene 会被 SDK 静默降级——能力声明与入口都必须按这个真实条件判断。
    """
    m = str(mode or (load().get("mode") or "pro"))
    if m == "lite":
        return False, "lite 模式只做 chat 原始向量检索（SDK 会忽略 scene 管线）"
    if not coding_enabled():
        return False, "MEMORY_CODING_ENABLED=false：SDK 生产力链路已关闭"
    return True, ""
