#!/usr/bin/env python
"""hy-mem — Hy-Memory SDK 的命令行外壳.

设计约束（照搬 CLI-Anything HARNESS.md 的三条硬规则，但代码手写）：
  1. 每个子命令都支持 --json，输出机器可解析的单行 JSON
  2. exit code 有语义：0=成功 1=用法/环境错 2=运行时错
  3. 必须有 doctor 子命令，能一条命令判定环境是否可用

为什么不用 CLI-Anything 自动生成：Hy-Memory 是简单 pip 包，Phase1-2 的
"源码分析"人工一遍就读完了，自动生成的 7 阶段产物反而增加验证成本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import netprobe
import store
from mcp_server import _in_range, _item_ts, _parse_time as _parse_when


class _Palette:
    """CLI 直出提示用的一点点颜色。

    hy_mem_cli 刻意不 import tui —— 那个模块是给交互式菜单用的，子命令不该
    为了几行淡色字把整套终端能力探测拖进来。这里只补实际用到的那几种，
    NO_COLOR 或非 TTY 时退化成纯文本。

    注意：这个文件里可以直接用 c.dim 之类，但必须走这个垫片。之前正因为写
    了 c.dim 却没定义 c，配置不全时的提示在运行期 NameError —— 而那恰好是
    最需要它说话的一条路径。
    """

    @staticmethod
    def _wrap(code: str, s: str) -> str:
        if os.getenv("NO_COLOR") or not sys.stdout.isatty():
            return s
        return f"\x1b[{code}m{s}\x1b[0m"

    def dim(self, s: str) -> str:
        return self._wrap("2", s)

    def bold(self, s: str) -> str:
        return self._wrap("1", s)

    def cyan(self, s: str) -> str:
        return self._wrap("36", s)

    def yellow(self, s: str) -> str:
        return self._wrap("33", s)


c = _Palette()

# 退出码在 errors.py 定义（CLI/MCP 共用一份口径），这里导入保持原名字可用
from errors import EXIT_INTERNAL, EXIT_OK, EXIT_RUNTIME, EXIT_USAGE  # noqa: E402
from errors import classify_error, error_detail  # noqa: E402,F401  分类口径单一来源


def _resolve_home() -> Path:
    """确定「安装根目录」，用于定位 .env。

    三种运行形态：
      1. 源码直跑（.venv/Scripts/python cli/hy_mem_cli.py）→ 上两级
      2. 被 Node 外壳拉起（设置了 HY_MEM_HOME）→ 用外壳指定的根
      3. PyInstaller 冻结成 exe → exe 所在目录（__file__ 指向解包临时目录，不可用）
    """
    env = os.getenv("HY_MEM_HOME")
    if env:
        return Path(env).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


SKILL_ROOT = _resolve_home()
DEFAULT_ENV = Path(os.getenv("HY_MEM_ENV_FILE") or (SKILL_ROOT / ".env"))


# --------------------------------------------------------------------------
# 环境装载
# --------------------------------------------------------------------------
def load_env(path: Path = DEFAULT_ENV) -> None:
    """极简 .env 解析，不引入 python-dotenv。已存在的真实环境变量优先。"""
    # 模型下载默认走国内镜像。fastembed（SDK 的 BM25 稀疏编码、本地 ONNX 嵌入）
    # 默认拉 huggingface.co：国内经常连不上，重试 3 次共 40s 后才降级，首次
    # 运行体感就是「卡住 + 一堆 ERROR」。hf-mirror.com 是长期稳定的镜像。
    # 用户显式设了 HF_ENDPOINT 就尊重用户的；离线环境可设 HF_HUB_OFFLINE=1。
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    # huggingface_hub 的进度条直接写 stderr，会污染 agent 的解析
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    if not path.exists():
        lines: list[str] = []
    else:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        # 剥行内注释（`KEY=1  # 说明`），否则 int()/float() 会把注释当值解析而崩掉
        if " #" in v:
            v = v.split(" #", 1)[0].strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
    # LLM key 复用已有的 YSJX_API_KEY，避免把密钥写进 .env 落盘
    if not os.getenv("MEMORY_LLM_API_KEY") and os.getenv("YSJX_API_KEY"):
        os.environ["MEMORY_LLM_API_KEY"] = os.environ["YSJX_API_KEY"]

    # config.json 后加载并**覆盖** .env：它是用户在设置向导里的显式选择，
    # 优先级必须高于 .env（否则改了向导却不生效，是最难排查的一类故障）。
    # 不存在时跳过，保证纯 .env 部署方式仍然可用。
    try:
        import config as cfgmod

        if cfgmod.exists():
            cfgmod.apply_env(cfgmod.load())
    except Exception:  # noqa: BLE001
        pass  # 配置层坏了不该让整个 CLI 起不来，doctor 会报出来

    # 数据目录必须在「还没配置」时就定下来。config.json 不存在时 apply_env
    # 不会跑，MEMORY_DATA_DIR 于是为空，store.py 会回落到 ~/.hy_memory ——
    # 结果是首次运行（设置向导之前）写进用户目录，向导一保存 dataDir 变成
    # 安装根/data，之前写的记忆就「消失」了。这里统一成安装根/data。
    if not os.getenv("MEMORY_DATA_DIR"):
        try:
            import config as cfgmod2

            os.environ["MEMORY_DATA_DIR"] = str(cfgmod2.default_data_dir())
        except Exception:  # noqa: BLE001
            pass


def data_dir() -> Path:
    p = Path(os.getenv("MEMORY_DATA_DIR") or (Path.home() / ".hy_memory"))
    return p


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
_FIELDS: list[str] = []
_QUIET = False
_JSON = False


def _project(obj, fields: list[str]):
    """按 --fields 裁剪输出，只保留指定键（递归作用于列表元素）。"""
    if not fields:
        return obj
    if isinstance(obj, list):
        return [_project(x, fields) for x in obj]
    if isinstance(obj, dict):
        keep = {k: v for k, v in obj.items() if k in fields}
        # 结构性的 ok/results/memories 之类若被裁掉，agent 就读不出成败，保留
        for meta in ("ok", "error", "count", "results", "memories", "items", "jobs", "tags"):
            if meta in obj and meta not in keep:
                keep[meta] = _project(obj[meta], fields) if isinstance(obj[meta], (list, dict)) else obj[meta]
        return keep
    return obj


# 可以在命令行任意位置出现的全局选项。
# 不把 --help / -h 放进来：子命令的 -h 要看的是子命令自己的帮助。
_GLOBAL_FLAGS = {"--json", "--quiet", "--no-color", "--verbose", "--help-json"}
_GLOBAL_VALUED = {
    "--fields", "--config", "--mode", "--data-dir",
    "-u", "--user", "-a", "--agent", "-s", "--session",
}


def hoist_globals(argv: list[str]) -> list[str]:
    """把全局选项搬到最前面（--config 顺带写进环境变量）。

    argparse 的父级选项只在其自身位置之前生效：`hy-mem search q --json` 会报
    unknown argument，而调用方（尤其智能体）习惯把全局参数追加在末尾。这里
    先扫一遍搬到前面，让位置不再重要。

    支持 `--` 之后的字面量：`hy-mem add -- --json` 里的 `--json` 会被当成文本。
    """
    head: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":  # 后面全是位置参数，不许再当选项解析
            rest.extend(argv[i:])
            break
        if tok in _GLOBAL_FLAGS:
            head.append(tok)
            i += 1
            continue
        if tok in _GLOBAL_VALUED:
            if i + 1 < len(argv):
                head.extend([tok, argv[i + 1]])
                if tok == "--config":
                    os.environ["HY_MEM_CONFIG"] = str(Path(argv[i + 1]).expanduser().resolve())
                i += 2
                continue
            rest.append(tok)
            i += 1
            continue
        if tok.startswith("--") and "=" in tok:
            name, value = tok.split("=", 1)
            if name in _GLOBAL_VALUED or name in _GLOBAL_FLAGS:
                head.append(tok)
                if name == "--config":
                    os.environ["HY_MEM_CONFIG"] = str(Path(value).expanduser().resolve())
                i += 1
                continue
        rest.append(tok)
        i += 1
    return head + rest


def emit(obj, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(_project(obj, _FIELDS), ensure_ascii=False, default=str))
    elif _QUIET:
        return  # --quiet：只留退出码，成功时不打任何东西
    else:
        print(human if human is not None else obj)


_EXIT_CODES = {
    EXIT_OK: "E_OK",
    EXIT_USAGE: "E_USAGE",
    EXIT_RUNTIME: "E_RUNTIME",
    EXIT_INTERNAL: "E_INTERNAL",
    130: "E_INTERRUPTED",
}


def fail(msg: str, code: int = EXIT_RUNTIME, hint: str = "", extra: dict | None = None, err_code: str | None = None) -> int:
    """报错。--json 时在 stdout 也留一份结构化错误，agent 不必去解析 stderr。

    error.code 用稳定的字符串枚举（E_USAGE / E_RUNTIME / E_INTERNAL），
    和退出码语义一一对应；数字退出码仍在进程返回值里，两者不要混着读。
    err_code 允许调用方给出比数字退出码更精确的枚举（如 E_CONFIG）；
    extra 用于附加机器可读的细节（例如配置缺项清单），合并进 error 对象。
    """
    if _JSON:
        err = {"code": err_code or _EXIT_CODES.get(code, "E_RUNTIME"), "message": msg, "hint": hint}
        if extra:
            err.update(extra)
            if extra.get("kind") == "config":
                err["code"] = "E_CONFIG"
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
    line = f"hy-mem: {msg}" + (f"（{hint}）" if hint else "")
    print(line, file=sys.stderr)
    return code


def ok(obj, as_json: bool, human: str | None = None) -> int:
    emit(obj, as_json, human)
    return EXIT_OK


# --------------------------------------------------------------------------
# 配置就绪度 —— 配不全就直说，不要让 SDK 抛一句看不懂的异常
# --------------------------------------------------------------------------
def readiness(cfg: dict | None = None) -> dict:
    """「现在到底能不能读/写记忆」：缺什么、补哪一项、怎么补。

    以前配不全的典型表现是：写入报一个 SDK 内部的 httpx/401 堆栈，或者检索
    静默返回空列表 —— 两种都让人误判成「程序坏了」「没有记忆」。这里把
    缺项翻成人话，并给出**可以直接复制执行的修复命令**。
    """
    import config as cfgmod

    cfg = cfg if cfg is not None else cfgmod.load()
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    mode = str(cfg.get("mode") or "pro")
    provider = str(emb.get("provider") or "openai").lower()

    missing: list[dict] = []

    def add(item: str, what: str, fix: str) -> None:
        missing.append({"item": item, "what": what, "fix": fix})

    if provider == "local":
        if not emb.get("model"):
            add("embedding.model", "本地 ONNX 模型名为空", "hy-mem setup --step embedding")
    else:
        if not str(emb.get("baseUrl") or "").strip():
            add("embedding.baseUrl", "向量模型入口（baseUrl）为空", "hy-mem setup --step embedding")
        if not str(emb.get("model") or "").strip():
            add("embedding.model", "向量模型名为空", "hy-mem setup --step embedding")
        if not (emb.get("apiKey") or os.getenv("MEMORY_EMBEDDER_API_KEY")):
            add(
                "embedding.apiKey",
                "向量模型密钥未设置",
                "hy-mem setup --step embedding   或   hy-mem config set embedding.apiKey=sk-xxx",
            )

    if mode in ("pro", "ultra"):
        if not str(llm.get("baseUrl") or "").strip():
            add("llm.baseUrl", "LLM 入口（baseUrl）为空", "hy-mem setup --step llm")
        if not str(llm.get("model") or "").strip():
            add("llm.model", "LLM 模型名为空", "hy-mem setup --step llm")
        if not cfgmod.resolve_llm_key(cfg):
            add(
                "llm.apiKey",
                "LLM 密钥未设置（配置文件与环境变量都没有）",
                "hy-mem setup --step llm   或   set " + (llm.get("keyEnv") or "MEMORY_LLM_API_KEY") + "=<key>",
            )

    # 字符集闸门：key 进的是 HTTP 头，http.client 用 latin-1 编码，中文/全角/零宽
    # 会在**请求发出前**炸成 UnicodeEncodeError（报的是 "position 7-9" 这种偏移量），
    # 用户只看到 HTTP 0，当成网络故障查半天。放在闸门里，一次拦住所有调用路径
    # （add / search / doctor 都先过 require_ready）。
    def charset(item: str, label: str, value, fix: str) -> None:
        if not value:
            return
        bad = cfgmod.secret_charset_problem(str(value))
        if bad:
            add(item, f"{label}{bad} —— 只能是 ASCII 字符", fix)

    charset(
        "embedding.apiKey",
        "向量模型密钥",
        emb.get("apiKey") or os.getenv("MEMORY_EMBEDDER_API_KEY"),
        "hy-mem setup --step embedding",
    )
    if mode in ("pro", "ultra"):
        charset("llm.apiKey", "LLM 密钥", cfgmod.resolve_llm_key(cfg), "hy-mem setup --step llm")
    for item, label, value, fix in (
        ("embedding.baseUrl", "向量模型入口", emb.get("baseUrl"), "hy-mem setup --step embedding"),
        ("embedding.model", "向量模型名", emb.get("model"), "hy-mem setup --step embedding"),
    ):
        if value and not str(value).isascii():
            add(item, f"{label}含非 ASCII 字符，URL/模型名只能是 ASCII", fix)
    return {"ok": not missing, "mode": mode, "missing": missing}


def _report_missing(state: dict, action: str) -> int:
    """把缺项清单打成人能照着做的样子（--json 走结构化错误）。"""
    import config as cfgmod

    missing = state["missing"]
    lead = f"配置还没配好，暂不执行：{action}" if action else "配置还没配好"
    msg = lead + "（缺 " + "、".join(m["item"] for m in missing) + "）"
    hint = "hy-mem setup"
    if _JSON:
        return fail(msg, EXIT_USAGE, hint, extra={"kind": "config", "missing": missing})
    try:
        import tui
        from tui import c

        print()
        print(f"  {tui.icon('err')} {c.bold(lead)}")
        print()
        width = max(tui.vwidth(m["item"]) for m in missing)
        for m in missing:
            print(f"    {c.yellow('缺')}  {c.bold(tui.lpad(m['item'], width))}  {m['what']}")
        print()
        print(f"  {c.bold('怎么补')}（任选其一，改完立即生效）：")
        seen: list[str] = []
        for m in missing:
            if m["fix"] not in seen:
                seen.append(m["fix"])
        for i, f in enumerate(seen, 1):
            print(f"    {i}. {c.cyan(f)}")
        print()
        print(c.dim("    交互向导会真实请求一次接口确认可用，比手改配置可靠。"))
        print(c.dim("    配置保存在 ") + c.dim(str(cfgmod.config_path())))
        print()
        print(c.dim("  说明：配置不全时不会写入或检索 —— 否则会把「配错了」误当成「没有记忆」。"))
        print()
    except Exception:  # noqa: BLE001  兜底：再差也要把缺项说出来
        print(f"hy-mem: {msg}", file=sys.stderr)
        for m in missing:
            print(f"  - {m['item']}: {m['what']} → {m['fix']}", file=sys.stderr)
    return EXIT_USAGE


def require_ready(action: str = "", cfg: dict | None = None) -> int | None:
    """放行返回 None；配置不全则打印缺项并返回退出码。"""
    state = readiness(cfg)
    if state["ok"]:
        return None
    return _report_missing(state, action)


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
def embedding_provider() -> str:
    """当前生效的嵌入来源：环境变量 > config.json > openai。

    不能简单把缺省当 local：全新安装时 env 里什么都没有，落成 local 就会在
    第一次 doctor 时去下载 92MB 的 ONNX 模型（国内还要等 40s+），而用户
    真正选的可能就是云端向量模型。config 的默认值本来就是 openai，保持一致。
    """
    p = (os.getenv("MEMORY_EMBEDDER_PROVIDER") or "").strip().lower()
    if p:
        return p
    try:
        import config as cfgmod

        return str((cfgmod.load().get("embedding") or {}).get("provider") or "openai").strip().lower()
    except Exception:  # noqa: BLE001
        return "openai"


def build_embedding_service():
    """按嵌入来源决定是否注入进程内实例；local 时返回该实例。"""
    provider = embedding_provider()
    if provider != "local":
        return None
    from local_embedder import LocalEmbeddingService

    svc = LocalEmbeddingService()
    # 先把维度对齐到环境变量，SDK 初始化时会校验二者一致
    os.environ["MEMORY_EMBEDDING_DIMS"] = str(svc.get_embedding_dims())
    return svc


def make_client(mode: str | None):
    from hy_memory import HyMemoryClient  # 延迟导入，doctor 失败时也能跑

    svc = build_embedding_service()
    kwargs = {}
    if mode:
        kwargs["mode"] = mode
    if svc is not None:
        kwargs["embedding_service"] = svc
    return HyMemoryClient(**kwargs)


def identity(args) -> dict:
    d = {}
    if getattr(args, "user", None):
        d["user_id"] = args.user
    if getattr(args, "agent", None):
        d["agent_id"] = args.agent
    if getattr(args, "session", None):
        d["session_id"] = args.session
    return d


def identity_lists(args) -> dict:
    """search 用的是复数形参（user_ids / agent_ids / session_ids）。"""
    d = {}
    if getattr(args, "user", None):
        d["user_ids"] = [args.user]
    if getattr(args, "agent", None):
        d["agent_ids"] = [args.agent]
    if getattr(args, "session", None):
        d["session_ids"] = [args.session]
    return d


def _parse_memory_at(v) -> "datetime | None":
    """--memory-at 的值 → datetime。支持 2026-10-27 / 2026-10-27 14:30:00 / ISO T 分隔。"""
    if v is None or not str(v).strip():
        return None
    s = str(v).strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"--memory-at 时间格式不认识：{s!r}（支持 2026-10-27、2026-10-27 14:30:00 这类写法）"
    )


def _add_kwargs(args) -> dict:
    """add 的三个 SDK 透传参数 → client.add 关键字参数。

    metadata 要 JSON 对象；memory_at 是补录历史时间（影响检索排序与时间过滤）；
    request_id 是幂等写入口（同一 id 重试不会写重）。解析错在这里当场报，
    不让坏数据进队列。
    """
    kw: dict = {}
    raw_md = getattr(args, "metadata", None)
    if raw_md:
        try:
            md = json.loads(raw_md)
        except json.JSONDecodeError as e:
            raise ValueError(f"--metadata 必须是合法 JSON 对象：{e}") from e
        if not isinstance(md, dict):
            raise ValueError("--metadata 必须是 JSON 对象，如 '{\"project\":\"iisw\"}'")
        kw["metadata"] = md
    mat = _parse_memory_at(getattr(args, "memory_at", None))
    if mat is not None:
        kw["memory_at"] = mat
    rid = (getattr(args, "request_id", None) or "").strip()
    if rid:
        kw["request_id"] = rid
    return kw


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    report = {"ok": True, "checks": []}

    def check(name, passed, detail, fatal=True):
        report["checks"].append({"name": name, "pass": bool(passed), "detail": detail})
        if not passed and fatal:
            report["ok"] = False

    # 1. SDK
    try:
        import hy_memory

        ver = getattr(hy_memory, "__version__", "unknown")
        check("sdk_import", True, f"hy-memory {ver}")
    except Exception as e:  # noqa: BLE001
        check("sdk_import", False, f"import failed: {e}")
        emit(report, args.json, "SDK 导入失败，先 pip install hy-memory")
        return EXIT_USAGE

    # 1.5 运行环境：系统版本 / 位数 / 包完整性 / 安装目录可写 / 运行库 / 磁盘
    #     把「双击闪退」的成因搬进自检，不必等人报障再猜。
    try:
        import runtime_check

        for rc_item in runtime_check.checks(full=True):
            detail = rc_item["detail"]
            if not rc_item["ok"] and rc_item.get("fix"):
                detail += f"（怎么修：{rc_item['fix']}）"
            check(rc_item["item"], rc_item["ok"], detail, fatal=bool(rc_item.get("fatal", True)))
    except Exception as e:  # noqa: BLE001
        check("runtime", False, f"{type(e).__name__}: {e}", fatal=False)

    # 2. 数据目录
    dd = Path(args.data_dir) if args.data_dir else data_dir()
    try:
        dd.mkdir(parents=True, exist_ok=True)
        probe = dd / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("data_dir", True, str(dd))
    except Exception as e:  # noqa: BLE001
        check("data_dir", False, f"{dd}: {e}")

    # 2.5 身份与启动器：agent 不传 -u 时用的是哪个 user_id，命令在不在 PATH 上
    try:
        import config as _cfg

        _uid = _cfg.identity_defaults()
        _src_map = {
            "env": "环境变量 HY_MEM_USER_ID",
            "config": "config.identity.userId",
            "general": "未设置 → 通用",
        }
        _src = _src_map.get(_cfg.identity_source(), "config.identity")
        _scope = "所有智能体共享" if _uid == _cfg.GENERAL_USER_ID else "仅该身份可见（隔离）"
        check(
            "identity",
            bool(_uid),
            f"user_id={_uid}（{_scope}；来自 {_src}，-u 可覆盖，hy-mem user 可切换）",
            fatal=False,
        )
    except Exception as e:  # noqa: BLE001
        check("identity", False, f"解析失败：{type(e).__name__}: {e}", fatal=False)
    try:
        import install_ops

        # 先看 PATH 上到底能不能解析到 hy-mem —— 用户可能把启动器装在别的目录
        # （~/.local/bin 之类），只盯默认落点会误报 FAIL。
        found = install_ops.which()
        if found:
            check("launcher", True, f"PATH 上可用：{found}", fatal=False)
        else:
            _lp = install_ops.launcher_path()
            _on = install_ops.on_path(_lp.parent)
            check(
                "launcher",
                False,
                f"PATH 上找不到 hy-mem（默认落点 {_lp}"
                + ("" if _on else "，其目录也不在 PATH")
                + "；跑 hy-mem install --dir <已在 PATH 的目录>）",
                fatal=False,
            )
    except Exception as e:  # noqa: BLE001
        check("launcher", False, f"{type(e).__name__}: {e}", fatal=False)

    # 3. Embedding（local=进程内 ONNX；remote=OpenAI 兼容端点）
    provider = embedding_provider()
    want_dims = int(os.getenv("MEMORY_EMBEDDING_DIMS", "0") or 0)
    base = os.getenv("MEMORY_EMBEDDER_BASE_URL", "").rstrip("/")
    model = os.getenv("MEMORY_EMBEDDER_MODEL", "")
    key = os.getenv("MEMORY_EMBEDDER_API_KEY", "")
    if provider == "local":
        # 模型没缓存时不要「顺手下载」：doctor 是诊断命令，不该让用户毫无预期
        # 地等 40s、占 92MB。只如实报告，并说清首次使用会发生什么。
        try:
            from local_embedder import _default_cache_dir

            _cache = Path(_default_cache_dir())
            _cached = any(_cache.glob("**/*.onnx"))
        except Exception:  # noqa: BLE001
            _cache, _cached = Path("?"), False
        if not _cached:
            check(
                "embedding_local",
                True,
                f"本地模型尚未缓存（{_cache}）：首次使用会下载约 92MB，已默认走 hf-mirror.com 国内镜像",
                fatal=False,
            )
            want_dims = want_dims or 0
            provider = "local-pending"
    if provider == "local":
        try:
            import fastembed  # noqa: F401

            from local_embedder import LocalEmbeddingService

            svc = LocalEmbeddingService()
            t0 = time.time()
            vec = asyncio.run(svc.embed("健康检查", use_cache=False))
            ms = int((time.time() - t0) * 1000)
            dims = len(vec)
            cached = any(Path(svc._cache_dir).glob("**/*.onnx"))
            good = dims == (want_dims or dims)
            detail = f"{svc._model_name} {dims}维 {ms}ms ({'已缓存' if cached else '首次需下载'})"
            if not good:
                detail += f" 与 MEMORY_EMBEDDING_DIMS={want_dims} 不一致！"
            check("embedding_local", good, detail)
        except Exception as e:  # noqa: BLE001
            check("embedding_local", False, f"本地嵌入失败: {type(e).__name__}: {e}")
    elif not base or not model:
        check("embedding_config", False, "MEMORY_EMBEDDER_BASE_URL / _MODEL 未配置")
    else:
        check("embedding_config", True, f"{model} @ {base}")
        if not key:
            check("embedding_live", False, "MEMORY_EMBEDDER_API_KEY 为空", fatal=False)
        else:
            res = netprobe.post_json(
                f"{base}/embeddings",
                {"model": model, "input": "健康检查"},
                key,
                timeout=30,
                key_label="MEMORY_EMBEDDER_API_KEY",
            )
            if isinstance(res, netprobe.NotSent):
                # 请求没离开本机（密钥/地址含非 ASCII、DNS、连接被拒、超时）。
                # 它和「服务器返回了错误码」是两回事，绝不能写成 HTTP 0。
                check("embedding_live", False, res.reason)
            elif res.status != 200:
                check("embedding_live", False, netprobe.explain(res))
            else:
                ms = int(res.seconds * 1000)
                vec = (res.payload.get("data") or [{}])[0].get("embedding") or []
                dims = len(vec)
                good = dims > 0 and (want_dims == 0 or dims == want_dims)
                detail = f"{dims} 维 / {ms}ms"
                if want_dims and dims != want_dims:
                    detail += f"（与 MEMORY_EMBEDDING_DIMS={want_dims} 不一致！会导致写入失败）"
                check("embedding_live", good, detail)

    # 4. 模式与 LLM（lite 不需要 LLM）
    mode = args.mode or os.getenv("MEMORY_MODE", "pro")
    check("mode", mode in ("lite", "pro", "ultra"), f"MEMORY_MODE={mode}")
    if mode in ("pro", "ultra"):
        lm = os.getenv("MEMORY_LLM_BASE_URL", "").rstrip("/")
        lk = os.getenv("MEMORY_LLM_API_KEY", "")
        lmod = os.getenv("MEMORY_LLM_MODEL", "")
        if not lm or not lk or not lmod:
            check("llm_config", False, f"{mode} 模式必须配 LLM（缺 base_url/key/model）")
        else:
            check("llm_config", True, f"{lmod} @ {lm}")
            res = netprobe.post_json(
                f"{lm}/chat/completions",
                {
                    "model": lmod,
                    "messages": [{"role": "user", "content": "回复ok"}],
                    # 不能给小值。推理模型（deepseek-v4 / kimi-k2.5 / claude 系）
                    # 会先把输出配额花在 thinking 上，8 个 token 连思考都写不完，
                    # 于是 content 永远是空串。这个坑曾让 doctor 对一个完全可用
                    # 的模型报「环境不可用」，而真实抽取流程（agent_max_tokens
                    # 默认 2000）一直是好的 —— 纯粹是探针自身太抠。
                    "max_tokens": 512,
                },
                lk,
                timeout=45,
                key_label="MEMORY_LLM_API_KEY",
            )
            if isinstance(res, netprobe.NotSent):
                check("llm_live", False, res.reason)
            elif res.status != 200:
                check("llm_live", False, netprobe.explain(res))
            else:
                ms = int(res.seconds * 1000)
                _ch = (res.payload.get("choices") or [{}])[0]
                _msg = _ch.get("message") or {}
                reply = str(_msg.get("content") or "").strip()
                think = str(_msg.get("reasoning_content") or "").strip()
                fin = str(_ch.get("finish_reason") or "")
                if reply:
                    check("llm_live", True, f"{ms}ms reply={reply[:20]!r}")
                elif think:
                    # 推理模型：content 为空、思维链有内容。鉴权、路由、连通全都正常，
                    # 这是这类模型的正常形态，不能算失败。
                    _mt = os.getenv("MEMORY_AGENT_MAX_TOKENS", "2000")
                    check("llm_live", True, f"{ms}ms 推理模型（思考 {len(think)} 字后作答，抽取配额 {_mt}）")
                else:
                    check(
                        "llm_live",
                        False,
                        f"{ms}ms 空响应 finish_reason={fin!r}：端点通但模型没给内容",
                    )

    # 5. 异步队列与写锁（worker 依赖这两个，坏了会静默丢任务）
    try:
        qconn = store.connect()
        try:
            stats = store.queue_stats(qconn)
            check(
                "queue_db",
                True,
                f"{store.db_path()} | 待处理 {stats['pending']} / 死信 {stats['dead']}",
            )
        finally:
            qconn.close()
    except Exception as e:  # noqa: BLE001
        check("queue_db", False, f"队列库不可用: {type(e).__name__}: {e}")

    try:
        t0 = time.time()
        with store.WriteLock(timeout=5) as lk:
            held = lk.acquired
        ms = int((time.time() - t0) * 1000)
        check("write_lock", held, f"{ms}ms" if held else "5s 内未拿到写锁（有 worker 正在写？）", fatal=False)
    except Exception as e:  # noqa: BLE001
        check("write_lock", False, f"{type(e).__name__}: {e}", fatal=False)

    hl = [c for c in report["checks"] if c["pass"]]
    human = "\n".join(
        f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['name']}: {c['detail']}"
        for c in report["checks"]
    )
    human = ("环境可用" if report["ok"] else "环境不可用") + "\n" + human
    emit(report, args.json, human)
    return EXIT_OK if report["ok"] else EXIT_USAGE


def _read_text(arg_text: str | None) -> str:
    text = arg_text
    if text is None:
        text = sys.stdin.read()
    return (text or "").strip()


def _split_lines(text: str) -> list[str]:
    """按行拆条。一次 add 的多个事实会被压成一条 l2_fact，稀释向量拉低召回。"""
    parts = [p.strip() for p in text.replace("\r", "").split("\n") if p.strip()]
    return parts if len(parts) > 1 else [text]


def _ids_of(result: dict) -> list[str]:
    """pro 模式下 memory_id 是 l1_raw 事件 id，memory_ids 才是抽取后的最终节点 id。

    标签必须绑最终节点，否则在 search/list 里永远对不上。
    """
    ids = result.get("memory_ids") or []
    ids = [str(i) for i in ids if i]
    if not ids:
        mid = result.get("memory_id") or result.get("id")
        if mid:
            ids = [str(mid)]
    return ids


def _snapshot_ids(client, ident: dict, cap: int = 2000) -> set[str]:
    """取写入前已存在的节点 id 集合，用于从 memory_ids 里剔除被 reconcile 合并到的老节点。

    实测：一次 add 的 memory_ids 并不等于"新建节点"，而是"本次影响到的节点"。
    写一句 CI 流水线的事实，会连带返回 8 个被合并/更新的旧节点 id。
    直接全部打标签 = 把标签污染到无关记忆上，所以必须做前后差分。
    """
    ids: set[str] = set()
    offset = 0
    # 只按 user 取全量快照：reconcile 会跨 session 合并节点，
    # 若同时按 session/agent 过滤，已知集合会偏小，导致把老节点误判为新节点。
    kwargs: dict = {}
    if (ident or {}).get("user_id"):
        kwargs["user_id"] = ident["user_id"]
    try:
        while offset < cap:
            r = client.list_memories(limit=200, offset=offset, **kwargs)
            batch = (r.get("vdb") or {}).get("memories") or []
            if not batch:
                break
            for it in batch:
                mid = it.get("memory_id")
                if mid:
                    ids.add(str(mid))
            offset += len(batch)
            if len(batch) < 200:
                break
    except Exception:  # noqa: BLE001
        pass
    return ids


def _new_ids_only(returned: list[str], before: set[str] | None) -> list[str]:
    if not returned:
        return []
    if not before:
        return returned
    fresh = [i for i in returned if i not in before]
    # 全都被识别为老节点时（内容被 reconcile 判为"已存在"），退回第一个，
    # 避免标签无处可挂；这比把标签铺到 N 个老节点上安全。
    return fresh or returned[:1]


def _bind_tags(ids: list[str], tags: list[str], source: str = "user") -> int:
    if not ids or not tags:
        return 0
    conn = store.connect()
    try:
        return store.bind_tags(conn, ids, tags, source)
    finally:
        conn.close()


def _remember_auto(items: list[dict]) -> None:
    """把检索/列表顺带看到的 LLM 标签缓存进本地索引，供 tags 统计与过滤复用。"""
    if os.getenv("HY_MEM_TAG_AUTOSYNC") == "0":
        return
    pairs = [(str(it.get("memory_id")), it.get("tags") or []) for it in items if isinstance(it, dict)]
    pairs = [(mid, tags) for mid, tags in pairs if mid and mid != "None" and tags]
    if not pairs:
        return
    try:
        conn = store.connect()
        try:
            for mid, tags in pairs:
                store.bind_tags(conn, [mid], [str(t) for t in tags], "auto")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass  # 只读路径不能因为标签缓存失败而整体报错


def _effective_tags(items: list[dict]) -> dict[str, set[str]]:
    mids = [str(it.get("memory_id")) for it in items if isinstance(it, dict) and it.get("memory_id")]
    local: dict[str, list[str]] = {}
    if mids:
        try:
            conn = store.connect()
            try:
                local = store.tags_for(conn, mids)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            local = {}
    out: dict[str, set[str]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = str(it.get("memory_id") or "")
        s = {str(t).lower() for t in (it.get("tags") or [])}
        s |= {str(t).lower() for t in local.get(mid, [])}
        out[mid] = s
    return out


def _filter_by_tags(items: list[dict], tags: list[str] | None, tag_mode: str = "all") -> list[dict]:
    """标签过滤。SDK 的 search() 没有 tag 参数，只能在召回结果上后过滤，
    因此调用方必须先 over-fetch（多取 N 倍），否则过滤后可能剩不下东西。"""
    if not tags:
        return items
    want = {t.strip().lower() for t in tags if t and t.strip()}
    if not want:
        return items
    eff = _effective_tags(items)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        have = eff.get(str(it.get("memory_id") or ""), set())
        hit = want <= have if tag_mode == "all" else bool(want & have)
        if hit:
            it["_tags"] = sorted(have)
            out.append(it)
    return out


def _spawn_detached_worker(idle_exit: float = 600.0) -> bool:
    """拉起一个脱管的后台 worker（空闲自退出），成功返回 True。

    为什么不常驻：智能体场景下「临时拉起 + 空闲自杀」最干净 —— add --async
    入队后立即返回毫秒级，落库由后台进程在 6~9s 内完成，不留僵尸进程。
    单写者由 WriteLock 与 SQLite 抢占兜底，即使拉重了也无害。
    """
    import install_ops

    argv = install_ops.target_command() + [
        "worker",
        "--idle-exit", str(idle_exit),
        "--poll", "1.0",
    ]
    try:
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW：不闪黑框
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
            )
        else:
            subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        return True
    except OSError:
        return False


def cmd_add(args) -> int:
    rc = require_ready("写入记忆")
    if rc is not None:
        return rc
    text = _read_text(args.text)
    if not text:
        return fail("没有输入内容（参数或 stdin）", EXIT_USAGE)
    tags = [t for t in (args.tag or []) if t.strip()]
    # metadata / memory_at / request_id 在进队列前就解析校验——坏参数当场报，
    # 别等 worker 消费时才炸（那时 job 已经进死信，排查成本高得多）
    try:
        extra = _add_kwargs(args)
    except ValueError as e:
        return fail(str(e), EXIT_USAGE)
    use_async = bool(getattr(args, "async_mode", False)) or os.getenv("HY_MEM_ASYNC") == "1"

    if use_async:
        # 队列 payload 必须可 JSON 序列化：datetime 转回 ISO 字符串，worker 端再解析
        queued_extra = dict(extra)
        if "memory_at" in queued_extra:
            queued_extra["memory_at"] = queued_extra["memory_at"].isoformat()
        parts = _split_lines(text) if args.split else [text]

        def _enqueue_all(conn):
            return [
                store.enqueue(
                    conn,
                    {
                        "text": p,
                        "source": args.source,
                        "identity": identity(args),
                        "tags": tags,
                        **queued_extra,
                    },
                    mode=args.mode,
                    max_attempts=int(os.getenv("HY_MEM_QUEUE_MAX_ATTEMPTS") or 3),
                )
                for p in parts
            ]

        ids = store.transient_write(_enqueue_all)
        conn = store.connect()
        try:
            stats = store.queue_stats(conn)
            # 无人消费就自动拉起临时 worker：心跳超过阈值或从无心跳且有待处理任务。
            # 阈值取 poll(1s) 的 15 倍，容掉 worker 卡顿，避免重复拉起。
            hb_age = store.worker_age(conn)
        finally:
            conn.close()
        worker_live = hb_age is not None and hb_age < 15.0
        spawned = False
        if stats["pending"] > 0 and not worker_live:
            spawned = _spawn_detached_worker()
        payload = {
            "ok": True, "queued": len(ids), "job_ids": ids, "tags": tags, "queue": stats,
            "workerLive": worker_live, "workerSpawned": spawned,
        }
        if spawned:
            tail = "已自动拉起后台 worker（空闲 10 分钟自退出），落库无需等待"
        elif worker_live:
            tail = "后台 worker 在跑，稍后自动落库"
        else:
            tail = "消费请跑：hy-mem worker"
        human = f"已入队 {len(ids)} 个任务（job {ids[0]}..{ids[-1]}），待处理 {stats['pending']} 个。{tail}"
        return ok(payload, args.json, human)

    client = make_client(args.mode)
    try:
        parts = _split_lines(text) if args.split else [text]
        before = _snapshot_ids(client, identity(args)) if tags else set()
        all_ids: list[str] = []
        tagged: list[str] = []
        last: dict = {}
        for p in parts:
            # 写锁：SDK 内部无锁，reconcile 是读改写，多进程并发会丢更新
            with store.WriteLock():
                last = client.add(p, source=args.source, **extra, **identity(args))
            ids = _ids_of(last)
            all_ids.extend(ids)
            if tags:
                fresh = _new_ids_only(ids, before)
                _bind_tags(fresh, tags, "user")
                tagged.extend(fresh)
                before.update(ids)
        if len(parts) > 1:
            payload = {"ok": True, "count": len(all_ids), "memory_ids": all_ids, "tagged_ids": tagged, "tags": tags}
            return ok(payload, args.json, f"已写入 {len(all_ids)} 条：{', '.join(i[:8] for i in all_ids)}")
        payload = dict(last)
        payload["memory_ids"] = all_ids
        if tags:
            payload["tagged_ids"] = tagged
            payload["tags"] = tags
        suffix = f"，标签 {','.join(tags)}" if tags else ""
        return ok(payload, args.json, f"已写入 memory_id={(all_ids[0] if all_ids else '?')}{suffix}")
    finally:
        client.close()



def _flatten_search(resp: dict) -> list:
    """search 返回 {memories:{schema:[],proactive:[],normal:[]}}，拍平后按 score 降序。"""
    mem = resp.get("memories") or {}
    items = []
    if isinstance(mem, dict):
        for bucket in ("normal", "proactive", "schema"):
            v = mem.get(bucket)
            if isinstance(v, list):
                items.extend(x for x in v if isinstance(x, dict))
        # 兜底：未知桶
        for k, v in mem.items():
            if k not in ("normal", "proactive", "schema") and isinstance(v, list):
                items.extend(x for x in v if isinstance(x, dict))
    elif isinstance(mem, list):
        items = [x for x in mem if isinstance(x, dict)]
    items.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return items


def _pending_jobs() -> int:
    """队列里还有多少条没落库。

    用途：解释了「刚写完却查不到」这种最常见困惑 —— 写是异步的，任务还在
    队列里等着 worker 消费，库自然没有它。查队列本身失败时返回 0，绝不
    因为一句提示而把检索搞挂。
    """
    try:
        conn = store.connect()
        try:
            stats = store.queue_stats(conn) or {}
            return int(stats.get("pending") or 0) + int(stats.get("running") or 0)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0


def _embedding_unhealthy() -> str:
    """空结果时的自证：嵌入端点到底通不通。通就返回 ""（真没命中）。

    只在「搜索结果为空」这条路上调用，正常命中不付这份时间（约 300ms）。
    """
    if os.getenv("HY_MEM_NO_PROBE", "").strip().lower() in ("1", "true", "yes"):
        return ""
    try:
        import config as cfgmod
        import setup_wizard

        emb = (cfgmod.load().get("embedding") or {})
        good, msg, _dims = setup_wizard.probe_embedding(emb, timeout=20)
        return "" if good else msg
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def cmd_search(args) -> int:
    rc = require_ready("检索记忆")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        # 默认阈值按实测调过：qwen3.7-flash 的分数带更窄
        #   噪声 0.28 / 近邻干扰 0.33 / 合法改写 0.355~0.372 / 直命中 0.5~0.66
        # SDK 默认 0.4 会漏掉全部改写问法，0.35 是这批样本的最优分割点。
        min_score = args.min_score
        if min_score is None:
            min_score = float(os.getenv("HY_MEM_MIN_SCORE") or 0.35)

        want_tags = [t for t in (args.tag or []) if t.strip()]
        # 标签在 SDK 里不是查询条件，只能后过滤 → 必须多取，否则过滤后可能空手
        fetch_limit = args.limit if not want_tags else min(200, max(args.limit * 10, 30))

        # 时间窗：since 走 SDK 原生 created_after（检索端就裁掉，省得起一堆再扔），
        # until 与无时间戳兜底放行逻辑在本地过滤（与 MCP search_memory 同一套助手）。
        try:
            since = _parse_when(getattr(args, "since", None))
            until = _parse_when(getattr(args, "until", None))
        except ValueError as e:
            return fail(str(e), EXIT_USAGE)  # 坏时间格式是用法错，exit 1
        kw = {"created_after": since} if since is not None else {}

        r = client.search(
            args.query,
            limit=fetch_limit,
            min_score=min_score,
            **kw,
            **identity_lists(args),
        )
        items = _flatten_search(r)
        if not items and not want_tags:
            # 空结果有两种截然不同的原因：真的没有相似记忆，或者嵌入端点挂了。
            # SDK 会吞掉异常并返回空列表（日志里才有 401），不区分的话 agent
            # 会把「密钥失效」当成「没有相关记忆」，一路错下去。空手时补一次
            # 真实探测，把真实原因说出来。
            why = _embedding_unhealthy()
            if why:
                return fail(
                    f"检索失败：嵌入端点在报错（{why}）—— 这不代表没有相关记忆。"
                    "修：hy-mem setup --step embedding",
                    EXIT_RUNTIME,
                )
        _remember_auto(items)
        if since is not None or until is not None:
            items = [x for x in items if _in_range(_item_ts(x), since, until)]
        if want_tags:
            items = _filter_by_tags(items, want_tags, args.tag_mode)
        items = items[: args.limit]

        if args.json:
            _payload = {
                "count": len(items),
                "min_score": min_score,
                "elapsed_ms": r.get("elapsed_ms"),
                "tags_filter": want_tags,
                "results": items,
            }
            if not items:
                # agent 只看 JSON，这条线索必须进结构体，否则它只会得出
                # 「没有相关记忆」的结论，而真相是「还没落库」。
                _pend = _pending_jobs()
                if _pend:
                    _payload["pending_jobs"] = _pend
                    _payload["hint"] = f"队列里还有 {_pend} 条未落库，先跑 hy-mem worker --once"
            return ok(_payload, True)
        if not items:
            extra = f"（标签过滤：{','.join(want_tags)}）" if want_tags else ""
            print(f"（无命中）{extra}")
            pend = _pending_jobs()
            if pend:
                print(
                    c.dim(f"  但队列里还有 {pend} 条没落库 —— 刚写的记忆很可能就在其中。")
                )
                print(c.dim("  先跑一次：hy-mem worker --once（pro 模式每条约 10~50s）"))
            return EXIT_OK
        lines = []
        for i, it in enumerate(items, 1):
            score = it.get("score")
            s = f"{score:.3f}" if isinstance(score, (int, float)) else "  -  "
            body = (it.get("content") or it.get("text") or "").replace("\n", " ")
            layer = it.get("layer") or ""
            tg = it.get("_tags") or it.get("tags") or []
            tagstr = f"  #{','.join(str(t) for t in tg)}" if tg else ""
            lines.append(f"{i:>2}. [{s}] {body[:150]}" + (f"  <{layer}>" if layer else "") + tagstr)
        return ok(items, False, "\n".join(lines))
    finally:
        client.close()


def cmd_list(args) -> int:
    rc = require_ready("读取记忆列表")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        want_tags = [t for t in (args.tag or []) if t.strip()]
        fetch_limit = args.limit if not want_tags else min(500, max(args.limit * 10, 50))
        r = client.list_memories(limit=fetch_limit, offset=args.offset, order=args.order, **identity(args))
        vdb = r.get("vdb") or {}
        items = vdb.get("memories") or []
        source = "vdb"
        # lite 模式下 l1_raw 不进 vdb 列表，回退到最近历史
        if not items and args.user:
            try:
                items = client.get_recent_history(user_id=args.user, limit=fetch_limit) or []
                source = "recent"
            except Exception:  # noqa: BLE001
                items = []
        _remember_auto(items)
        if want_tags:
            items = _filter_by_tags(items, want_tags, args.tag_mode)
        items = items[: args.limit]
        if args.json:
            return ok({"count": len(items), "source": source, "tags_filter": want_tags, "memories": items}, True)
        if not items:
            print("（空）" if not want_tags else f"（无匹配标签 {','.join(want_tags)} 的记忆）")
            _pend = _pending_jobs()
            if _pend:
                print(c.dim(f"  队列里还有 {_pend} 条没落库（hy-mem worker --once 消费）"))
            return EXIT_OK
        lines = []
        for it in items:
            if not isinstance(it, dict):
                lines.append(str(it)[:140])
                continue
            mid = (it.get("memory_id") or it.get("id") or "?")
            mid = mid if isinstance(mid, str) else str(mid)
            body = (it.get("content") or it.get("new_memory") or it.get("text") or "").replace("\n", " ")
            tg = it.get("_tags") or it.get("tags") or []
            tagstr = f"  #{','.join(str(t) for t in tg)}" if tg else ""
            lines.append(f"{mid[:18]:<18} {body[:110]}{tagstr}")
        human = "\n".join(lines)
        _pend = _pending_jobs()
        if _pend:
            human += c.dim(f"\n\n（队列里还有 {_pend} 条没落库，可能是刚写的 —— hy-mem worker --once 消费）")
        return ok(items, False, human)
    finally:
        client.close()


def cmd_get(args) -> int:
    rc = require_ready("读取记忆")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        r = client.get(args.memory_id)
        if r is None:
            return fail(f"未找到 {args.memory_id}")
        return ok(r, args.json, json.dumps(r, ensure_ascii=False, indent=2, default=str))
    finally:
        client.close()


def cmd_delete(args) -> int:
    rc = require_ready("删除记忆")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        if getattr(args, "dry_run", False):
            r = client.get(args.memory_id)
            if r is None:
                return fail(f"未找到 {args.memory_id}（dry-run 未做任何改动）")
            payload = {"ok": True, "dryRun": True, "wouldDelete": r}
            return ok(payload, args.json, "[dry-run] 将删除：\n" + json.dumps(r, ensure_ascii=False, indent=2, default=str))
        with store.WriteLock():
            r = client.delete(args.memory_id)
        return ok(r, args.json, f"已删除 {args.memory_id}")
    finally:
        client.close()


def cmd_update(args) -> int:
    """改一条记忆的内容。走 SDK 演化链：旧版本进 SHADOW 保留，不丢历史。"""
    rc = require_ready("更新记忆")
    if rc is not None:
        return rc
    content = _read_text(getattr(args, "content", None))
    if not content:
        return fail("没有新内容（位置参数或 stdin）", EXIT_USAGE)
    client = make_client(args.mode)
    try:
        # 改前先确认存在，给出「未找到」而不是 SDK 的深层报错
        if client.get(args.memory_id) is None:
            return fail(f"未找到 {args.memory_id}（可先 hy-mem list 拿 id）")
        with store.WriteLock():
            r = client.update(args.memory_id, content)
        return ok(
            r, args.json,
            f"已更新 {args.memory_id}（旧版本保留在演化链，hy-mem history {args.memory_id[:8]} 可查）",
        )
    finally:
        client.close()


def cmd_history(args) -> int:
    """查一条记忆的变更历史（演化链）：每次改动的旧版本都在这里。"""
    rc = require_ready("查询记忆历史")
    if rc is not None:
        return rc
    client = make_client(args.mode)
    try:
        items = client.history(args.memory_id) or []
        if args.json:
            return ok({"count": len(items), "memory_id": args.memory_id, "history": items}, True)
        if not items:
            print(f"（{args.memory_id} 没有变更记录——从没被 update 过）")
            return EXIT_OK
        lines = []
        for i, it in enumerate(items, 1):
            body = str(it.get("memory") or it.get("content") or it.get("text") or "").replace("\n", " ")
            when = it.get("gmt_created") or it.get("created_at") or it.get("when") or ""
            event = it.get("event") or it.get("action") or ""
            lines.append(f"{i:>2}. [{when}]{' ' + str(event) if event else ''} {body[:140]}")
        return ok(items, False, "\n".join(lines))
    finally:
        client.close()


def cmd_forget(args) -> int:
    rc = require_ready("按用户清空记忆")
    if rc is not None:
        return rc
    if not args.user:
        return fail("forget 必须指定 --user，避免误删全局记忆", EXIT_USAGE)
    client = make_client(args.mode)
    try:
        if getattr(args, "dry_run", False):
            stats = client.list_memories(user_id=args.user, limit=1000)
            items = stats.get("memories") if isinstance(stats, dict) else stats
            n = len(items or [])
            payload = {"ok": True, "dryRun": True, "scope": {"user_id": args.user}, "count": n,
                       "note": "上限 1000 条样本；实际删除数可能更多"}
            return ok(payload, args.json, f"[dry-run] 范围内至少 {n} 条，未做任何改动")
        if not args.yes:
            scope = {"user_id": args.user}
            for k in ("agent", "session"):
                if getattr(args, k, None):
                    scope[f"{k}_id"] = getattr(args, k)
            print(f"将删除范围 {json.dumps(scope, ensure_ascii=False)} 下的全部记忆。", file=sys.stderr)
            print("确认请加 --yes", file=sys.stderr)
            return EXIT_USAGE
        kwargs = {"user_id": args.user}
        if args.session:
            kwargs["session_ids"] = [args.session]
        if args.agent:
            kwargs["agent_ids"] = [args.agent]
        with store.WriteLock():
            r = client.delete_all(**kwargs)
        return ok(r, args.json, "已清空")
    finally:
        client.close()


def cmd_finalize(args) -> int:
    if not args.user:
        return fail("finalize 必须指定 --user", EXIT_USAGE)
    client = make_client(args.mode)
    try:
        r = client.finalize_session(
            user_id=args.user,
            agent_id=args.agent or "default_agent",
            session_id=args.session or "default_session",
        )
        return ok(r, args.json, "session 已收尾")
    finally:
        client.close()


def _dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _scan_all_memories(client, ident: dict, cap: int = 5000, page: int = 100) -> list[dict]:
    """分页扫全量抽取层记忆。cap 是防御上限，避免库异常大时把 CLI 拖死。"""
    items: list[dict] = []
    offset = 0
    while offset < cap:
        r = client.list_memories(limit=page, offset=offset, **ident)
        batch = (r.get("vdb") or {}).get("memories") or []
        if not batch:
            break
        items.extend([b for b in batch if isinstance(b, dict)])
        offset += len(batch)
        if len(batch) < page:
            break
    return items


# pro 单条写入延迟的经验模型。两点标定：
#   库内 10 条上下 → 实测 8.6s
#   库内 21 条上下 → 实测 25~34s（取 30s）
# 解 a + b*N 得 b≈1.95 / a≈-10.9。偏差 ±20%，只用来判断量级，不要当 SLA。
_LAT_A = -10.9
_LAT_B = 1.95


def _estimate_write_latency(n: int) -> float:
    return max(2.5, _LAT_A + _LAT_B * max(n, 1))


def _health_thresholds() -> dict:
    def _f(name: str, dflt: float) -> float:
        try:
            return float(os.getenv(name) or dflt)
        except ValueError:
            return dflt

    return {
        "count_warn": _f("HY_MEM_HEALTH_COUNT_WARN", 200),
        "count_crit": _f("HY_MEM_HEALTH_COUNT_CRIT", 500),
        "lat_warn": _f("HY_MEM_HEALTH_LAT_WARN", 30),
        "lat_crit": _f("HY_MEM_HEALTH_LAT_CRIT", 60),
        "size_crit_mb": _f("HY_MEM_HEALTH_SIZE_CRIT_MB", 2048),
    }


def _status_icon(level: str) -> str:
    return {"OK": "[OK]  ", "WARN": "[WARN]", "CRIT": "[CRIT]"}.get(level, "[ -- ]")


def cmd_stats(args) -> int:
    if not args.health:
        client = make_client(args.mode)
        try:
            r = client.get_metrics(minutes=args.minutes)
            return ok(r, args.json, json.dumps(r, ensure_ascii=False, indent=2, default=str))
        finally:
            client.close()
    return _stats_health(args)


def _stats_health(args) -> int:
    """库体积健康度：条数 / 体积 / 队列 / 写入延迟预期 / 归档建议。

    立论：pro 写入的 reconcile 是 O(存量) —— 写一条新事实会把一批旧节点标记
    update 并重写。所以「库有多大」直接决定「写一条要多久」，这是必须能
    一条命令看出来的运营指标，而不是等写入慢到不可用才发现。
    """
    th = _health_thresholds()
    rc = require_ready("健康检查（要真实读写一次以测延迟）")
    if rc is not None:
        return rc
    checks: list[dict] = []
    notes: list[str] = []

    # ---- 体积 ----
    dd = data_dir()
    qdb = dd / "hy_mem_queue.db"
    inner = dd / "data"
    total_bytes = _dir_size(dd)
    inner_bytes = _dir_size(inner) if inner.exists() else 0
    q_bytes = qdb.stat().st_size if qdb.exists() else 0
    size_mb = total_bytes / 1048576
    size_level = "CRIT" if size_mb >= th["size_crit_mb"] else ("WARN" if size_mb >= th["size_crit_mb"] / 2 else "OK")
    checks.append(
        {
            "name": "数据体积",
            "value": f"{size_mb:.1f} MB",
            "level": size_level,
            "detail": f"记忆库 {inner_bytes/1048576:.1f}M / 队列库 {q_bytes/1024:.0f}K",
        }
    )

    # ---- 条数 + 层分布 ----
    items: list[dict] = []
    layer_count: dict[str, int] = {}
    client = make_client(args.mode)
    try:
        ident = {}
        if args.user:
            ident["user_id"] = args.user
        items = _scan_all_memories(client, ident, cap=args.max_scan)
        for it in items:
            layer = str(it.get("layer") or "unknown")
            layer_count[layer] = layer_count.get(layer, 0) + 1
    finally:
        client.close()

    n = len(items)
    if n >= th["count_crit"]:
        cnt_level = "CRIT"
    elif n >= th["count_warn"]:
        cnt_level = "WARN"
    else:
        cnt_level = "OK"
    checks.append(
        {
            "name": "记忆条数",
            "value": str(n),
            "level": cnt_level,
            "detail": " / ".join(f"{k} {v}" for k, v in sorted(layer_count.items(), key=lambda x: -x[1]))
            or "（空库）",
        }
    )

    # ---- 写入延迟预期 ----
    est = _estimate_write_latency(n)
    if est >= th["lat_crit"]:
        lat_level = "CRIT"
    elif est >= th["lat_warn"]:
        lat_level = "WARN"
    else:
        lat_level = "OK"
    checks.append(
        {
            "name": "单条写入预期",
            "value": f"{est:.0f}s",
            "level": lat_level,
            "detail": f"pro 模式同步 add；上限 {th['lat_crit']:.0f}s，超过必须改用 --async",
        }
    )

    # ---- 队列 ----
    conn = store.connect()
    try:
        qs = store.queue_stats(conn)
        inv = store.tag_inventory(conn)
        stale = sum(1 for _ in conn.execute("SELECT 1 FROM jobs WHERE status='done'").fetchall())
    finally:
        conn.close()
    q_level = "CRIT" if qs["dead"] else ("WARN" if qs["pending"] > 50 or qs["running"] else "OK")
    checks.append(
        {
            "name": "异步队列",
            "value": f"待 {qs['pending']} / 死信 {qs['dead']}",
            "level": q_level,
            "detail": f"已完成 {qs['done']} 条" + (f"，最老待处理 {qs['oldest_pending_age_s']:.0f}s" if qs["oldest_pending_age_s"] else ""),
        }
    )

    # ---- 建议 ----
    if n >= th["count_warn"]:
        notes.append(
            f"条数已达 {n}（阈值 {th['count_warn']:.0f}），reconcile 开销随之线性上升。"
            f"建议执行 `hy-mem archive --by tag` 盘点，再对低频标签用 `untag`/`delete` 收缩。"
        )
    if est >= th["lat_warn"]:
        notes.append(
            f"单条同步写入预期 {est:.0f}s，已超过交互可接受范围。批量写入一律走 "
            f"`add --async` + `worker`，或临时 `--mode lite`。"
        )
    if qs["dead"]:
        notes.append(f"存在 {qs['dead']} 条死信任务，跑 `hy-mem queue --status dead` 查原因，`hy-mem retry dead` 重投。")
    if stale > 5000:
        notes.append(f"jobs 表已积累 {stale} 条已完成记录，可执行 `hy-mem stats --health --vacuum` 清理。")

    # 低频标签：只报「有用户手工标签但仅 1 条」的，作为归档候选
    thin = [t for t in inv if t["n"] == 1 and t["user_n"] >= 1]
    if thin:
        notes.append(
            "单条标签（归档候选）：" + "、".join(f"{t['tag']}" for t in thin[:10])
            + ("…" if len(thin) > 10 else "")
        )

    # 疑似测试残留
    junk_kw = ("探针", "并发测试", "测试残留", "TODO", "临时")
    junk = [it for it in items if any(k in str(it.get("content") or "") for k in junk_kw)]
    if junk:
        notes.append(f"疑似测试残留 {len(junk)} 条，可用 `hy-mem list` 核对后 `hy-mem delete <id>` 清理。")

    if not notes:
        notes.append("各项指标正常，无需干预。")

    worst = "CRIT" if any(c["level"] == "CRIT" for c in checks) else ("WARN" if any(c["level"] == "WARN" for c in checks) else "OK")
    payload = {
        "ok": True,
        "verdict": worst,
        "memory_count": n,
        "layers": layer_count,
        "size_mb": round(size_mb, 2),
        "estimated_write_latency_s": round(est, 1),
        "queue": qs,
        "top_tags": [{"tag": t["tag"], "n": t["n"]} for t in inv[:15]],
        "checks": checks,
        "advice": notes,
        "thresholds": th,
    }

    if args.vacuum:
        conn = store.connect()
        try:
            conn.execute("DELETE FROM jobs WHERE status='done'")
            conn.execute("VACUUM")
            payload["vacuumed"] = True
        finally:
            conn.close()

    if args.json:
        return ok(payload, True)

    lines = [f"hy-mem 健康检查  结论 {worst}", ""]
    w = max(len(c["name"]) for c in checks) + 4
    for c in checks:
        lines.append(f"  {_status_icon(c['level'])} {c['name']:<{w}}{c['value']:<16}{c['detail']}")
    lines.append("")
    lines.append("建议：")
    for i, t in enumerate(notes, 1):
        lines.append(f"  {i}. {t}")
    if inv:
        lines.append("")
        lines.append("标签分布（前 10）：")
        for t in inv[:10]:
            lines.append(f"  {t['tag']:<24}{t['n']:>5}")
    return ok(payload, False, "\n".join(lines))


# --------------------------------------------------------------------------
# 异步队列
# --------------------------------------------------------------------------
def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _execute_job(conn, row, get_client) -> tuple[dict, list[str]]:
    payload = json.loads(row["payload"])
    client = get_client(row["mode"])
    kind = row["kind"]
    ident = payload.get("identity") or {}

    if kind == "finalize":
        with store.WriteLock():
            result = client.finalize_session(
                user_id=payload.get("user_id") or "",
                agent_id=payload.get("agent_id") or "default_agent",
                session_id=payload.get("session_id") or "default_session",
            )
        return result, []

    tags = [t for t in (payload.get("tags") or []) if str(t).strip()]
    # add 透传参数：metadata 原样；memory_at 是 ISO 字符串要转回 datetime；
    # request_id 原样（幂等写，同一 id 重复消费不会写重——重试场景的关键护栏）
    add_kw: dict = {}
    if payload.get("metadata"):
        add_kw["metadata"] = payload["metadata"]
    if payload.get("memory_at"):
        try:
            add_kw["memory_at"] = datetime.fromisoformat(str(payload["memory_at"]))
        except ValueError:
            pass  # 坏时间戳不入参（解析在入队前已拦截，这里是防御）
    if payload.get("request_id"):
        add_kw["request_id"] = str(payload["request_id"])
    before = _snapshot_ids(client, ident) if tags else set()
    with store.WriteLock():
        result = client.add(
            payload["text"],
            source=payload.get("source") or "cli",
            **add_kw,
            **ident,
        )
    ids = _ids_of(result)
    if tags:
        fresh = _new_ids_only(ids, before)
        store.bind_tags(conn, fresh, [str(t) for t in tags], "user")
        return result, fresh
    return result, ids


def cmd_worker(args) -> int:
    rc = require_ready("消费写入队列")
    if rc is not None:
        return rc
    wid = _worker_id()
    conn = store.connect()
    clients: dict[str, object] = {}

    def get_client(mode: str | None):
        key = mode or "default"
        if key not in clients:
            clients[key] = make_client(mode)
        return clients[key]

    recovered = 0 if args.no_recover else store.requeue_stale(conn)
    if recovered:
        print(f"hy-mem: 回收 {recovered} 个僵死任务", file=sys.stderr)

    processed = ok_n = failed = 0
    last_active = time.time()
    started = time.time()
    exit_code = EXIT_OK

    try:
        while True:
            # 心跳：供 add --async 判断「有无活着的 worker」决定是否自动拉起
            try:
                store.heartbeat(conn, wid)
            except Exception:  # noqa: BLE001  心跳失败不影响消费主流程
                pass
            if args.max and ok_n + failed >= args.max:
                break
            row = store.claim_next(conn, wid)
            if row is None:
                if args.once:
                    break
                if args.idle_exit and time.time() - last_active >= args.idle_exit:
                    break
                if args.timeout and time.time() - started >= args.timeout:
                    break
                time.sleep(args.poll)
                continue

            job_id, kind = row["id"], row["kind"]
            last_active = time.time()
            t0 = time.time()
            try:
                result, ids = _execute_job(conn, row, get_client)
                store.mark_done(conn, job_id, ids)
                ok_n += 1
                ms = int((time.time() - t0) * 1000)
                msg = f"[{job_id}] {kind} ok {ms}ms ids={len(ids)}"
                print(msg, file=sys.stderr)
                if args.json:
                    emit({"ok": True, "job": job_id, "kind": kind, "ms": ms, "memory_ids": ids}, True)
            except KeyboardInterrupt:
                store.mark_failed(conn, job_id, "interrupted", retry=True)
                raise
            except Exception as e:  # noqa: BLE001
                failed += 1
                state = store.mark_failed(conn, job_id, f"{type(e).__name__}: {e}", retry=True)
                print(f"[{job_id}] {kind} FAILED -> {state}: {type(e).__name__}: {e}", file=sys.stderr)
                if args.json:
                    emit({"ok": False, "job": job_id, "state": state, "error": str(e)}, True)
                # 退避，避免对上游 LLM/嵌入端点的瞬时故障做紧密重试
                time.sleep(min(2 ** int(row["attempts"] or 1), 30))
            processed += 1
    finally:
        for c in clients.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        conn.close()

    stats = None
    conn = store.connect()
    try:
        stats = store.queue_stats(conn)
    finally:
        conn.close()
    payload = {"ok": True, "processed": processed, "succeeded": ok_n, "failed": failed, "queue": stats}
    return ok(payload, args.json, f"worker 退出：成功 {ok_n} / 失败 {failed} / 剩余待处理 {stats['pending']}")


def cmd_queue(args) -> int:
    conn = store.connect()
    try:
        recovered = 0
        if args.recover:
            recovered = store.requeue_stale(conn)
        stats = store.queue_stats(conn)
        jobs = store.list_jobs(conn, args.status, args.limit)
    finally:
        conn.close()

    if args.json:
        return ok({"ok": True, "stats": stats, "recovered": recovered, "jobs": jobs}, True)

    head = (
        f"待处理 {stats['pending']} | 运行中 {stats['running']} | 完成 {stats['done']} | 死信 {stats['dead']}"
        f" | 总计 {stats['total']}"
    )
    if stats.get("oldest_pending_age_s"):
        head += f"\n最老待处理已等待 {stats['oldest_pending_age_s']}s"
    if recovered:
        head += f"\n已回收僵死任务 {recovered} 个"
    if not jobs:
        return ok({"stats": stats}, False, head + "\n（无任务）")
    lines = [head, ""]
    for j in jobs:
        body = str((j.get("payload") or {}).get("text") or "")[:60].replace("\n", " ")
        tags = (j.get("payload") or {}).get("tags") or []
        tail = f"  #{','.join(tags)}" if tags else ""
        err = f"  ! {str(j.get('error'))[:50]}" if j.get("error") else ""
        lines.append(f"#{j['id']:<4} {j['status']:<8} try {j['attempts']}/{j['max_attempts']}  {body}{tail}{err}")
    return ok({"stats": stats, "jobs": jobs}, False, "\n".join(lines))


def cmd_retry(args) -> int:
    """把死信/失败任务放回待处理，重置重试计数。"""
    conn = store.connect()
    try:
        if args.job_id == "dead" or args.all_dead:
            cur = conn.execute(
                "UPDATE jobs SET status='pending', attempts=0, error=NULL, started_at=NULL, worker=NULL "
                "WHERE status='dead'"
            )
            n = cur.rowcount
        else:
            try:
                jid = int(args.job_id)
            except (TypeError, ValueError):
                return fail("job_id 必须是数字，或使用 dead 重投全部死信", EXIT_USAGE)
            cur = conn.execute(
                "UPDATE jobs SET status='pending', attempts=0, error=NULL, started_at=NULL, worker=NULL "
                "WHERE id=? AND status IN ('dead','done')",
                (jid,),
            )
            n = cur.rowcount
            if n == 0:
                return fail(f"job {jid} 不存在或状态不允许重投（只有 dead/done 可重投）", EXIT_USAGE)
        stats = store.queue_stats(conn)
    finally:
        conn.close()
    return ok({"ok": True, "requeued": n, "queue": stats}, args.json, f"已重投 {n} 个任务，待处理 {stats['pending']} 个")


def cmd_drain(args) -> int:
    deadline = time.time() + args.timeout
    conn = store.connect()
    try:
        while True:
            stats = store.queue_stats(conn)
            if stats["pending"] == 0 and stats["running"] == 0:
                return ok({"ok": True, "queue": stats}, args.json, f"队列已清空（完成 {stats['done']}，死信 {stats['dead']}）")
            if time.time() >= deadline:
                return fail(f"等待超时，仍有 {stats['pending']} 个待处理 / {stats['running']} 个运行中", EXIT_RUNTIME)
            time.sleep(args.poll)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 标签
# --------------------------------------------------------------------------
def cmd_tags(args) -> int:
    if getattr(args, "refresh", False):
        rc = require_ready("从记忆同步自动标签")
        if rc is not None:
            return rc
    conn = store.connect()
    try:
        refreshed = 0
        if args.refresh:
            # 从 SDK 侧拉全量，把 LLM 自动标签补进本地索引（含孤儿清理）
            client = make_client(args.mode)
            try:
                alive: set[str] = set()
                items: list[dict] = []
                offset = 0
                while offset < args.max_refresh:
                    r = client.list_memories(limit=100, offset=offset, **identity(args))
                    batch = (r.get("vdb") or {}).get("memories") or []
                    if not batch:
                        break
                    items.extend(batch)
                    offset += len(batch)
                if args.user:
                    hist = client.get_recent_history(user_id=args.user, limit=args.max_refresh) or []
                    items.extend([h for h in hist if isinstance(h, dict)])
                for it in items:
                    mid = it.get("memory_id") or it.get("id")
                    if not mid:
                        continue
                    alive.add(str(mid))
                    tg = it.get("tags") or []
                    if tg:
                        refreshed += store.bind_tags(conn, [str(mid)], [str(t) for t in tg], "auto")
                pruned = store.prune(conn, alive) if alive else 0
            finally:
                client.close()
        else:
            pruned = 0
        inv = store.tag_inventory(conn, args.min_count)
    finally:
        conn.close()

    if args.json:
        return ok({"ok": True, "count": len(inv), "tags": inv, "refreshed": refreshed, "pruned": pruned}, True)
    if not inv:
        return ok(inv, False, "（还没有标签。用 add --tag 加，或 tags --refresh 从已有记忆同步）")
    lines = [f"{'标签':<24}{'合计':>6}{'手工':>6}{'自动':>6}"]
    for t in inv:
        lines.append(f"{t['tag']:<24}{t['n']:>6}{t['user_n']:>6}{t['auto_n']:>6}")
    if refreshed or pruned:
        lines.append(f"\n同步：新增自动标签 {refreshed} 条，清理失效 {pruned} 条")
    return ok(inv, False, "\n".join(lines))


def cmd_untag(args) -> int:
    if not args.memory_id and not args.tag:
        return fail("untag 至少要给 <memory_id> 或 --tag", EXIT_USAGE)
    if not args.memory_id and args.tag and not args.yes:
        return fail(f"将删除全局标签「{args.tag}」的所有绑定，确认请加 --yes", EXIT_USAGE)
    conn = store.connect()
    try:
        n = store.untag(conn, args.memory_id, args.tag)
    finally:
        conn.close()
    return ok({"ok": True, "removed": n}, args.json, f"已移除 {n} 条标签绑定")


def cmd_archive(args) -> int:
    conn = store.connect()
    try:
        if args.by == "tag":
            rows = store.tag_inventory(conn, args.min_count)
            data = [{"group": r["tag"], "count": r["n"]} for r in rows]
        else:
            data = []
            client = make_client(args.mode)
            try:
                counter: dict[str, int] = {}
                offset = 0
                while offset < args.max_scan:
                    r = client.list_memories(limit=100, offset=offset, **identity(args))
                    batch = (r.get("vdb") or {}).get("memories") or []
                    if not batch:
                        break
                    for it in batch:
                        layer = str(it.get("layer") or "unknown")
                        counter[layer] = counter.get(layer, 0) + 1
                    offset += len(batch)
                data = [{"group": k, "count": v} for k, v in sorted(counter.items(), key=lambda x: -x[1])]
            finally:
                client.close()
    finally:
        conn.close()

    if args.json:
        return ok({"ok": True, "by": args.by, "groups": data}, True)
    if not data:
        return ok(data, False, "（无数据）")
    width = max(len(str(d["group"])) for d in data) + 2
    lines = [f"按 {args.by} 归档："]
    total = sum(d["count"] for d in data) or 1
    for d in data:
        bar = "█" * max(1, int(d["count"] / total * 30))
        lines.append(f"  {str(d['group']):<{width}}{d['count']:>5}  {bar}")
    return ok(data, False, "\n".join(lines))



# --------------------------------------------------------------------------
# 配置 / 向导 / 交互界面
# --------------------------------------------------------------------------
def cmd_help(args) -> int:
    """命令说明。--json 是给智能体的入口：一次读完所有参数与语义。"""
    import helptext

    spec = helptext.build_spec(build_parser())
    if args.json:
        if args.command:
            one = next((c for c in spec["commands"] if c["name"] == args.command), None)
            if one is None:
                return fail(f"没有这个命令：{args.command}（用 hy-mem help --json 看全部）", EXIT_USAGE)
            print(json.dumps(_project(one, _FIELDS), ensure_ascii=False))
        else:
            print(json.dumps(_project(spec, _FIELDS), ensure_ascii=False))
        return EXIT_OK
    if args.command and not any(c["name"] == args.command for c in spec["commands"]):
        return fail(f"没有这个命令：{args.command}", EXIT_USAGE)
    print(helptext.render_text(spec, args.command))
    return EXIT_OK


def cmd_install(args) -> int:
    import install_ops

    if getattr(args, "print_only", False):
        body = install_ops.render_launcher()
        where = install_ops.launcher_path()
        # print() 在 Windows 上会把 \n 再翻成 \r\n，而 body 里已经是 \r\n，
        # 直接打印会得到 \r\r\n —— cmd 里表现为多出空行，用先归一再打。
        human = (
            f"将写入：{where}\n"
            f"实际执行：{' '.join(install_ops.target_command())}\n"
            f"在 PATH 上：{'是' if install_ops.on_path(where.parent) else '否 —— 见下方提示'}\n\n"
            "启动器内容：\n" + body.replace("\r\n", "\n").rstrip()
        )
        return ok({"launcher": body, "path": str(where), "target": install_ops.target_command()}, args.json, human)

    directory = Path(args.dir).expanduser() if args.dir else None
    good, msg, paths = install_ops.install(directory)
    path = paths[0] if paths else install_ops.launcher_path(directory)
    in_path = install_ops.on_path(path.parent)
    payload = {
        "ok": good,
        "launcher": [str(p) for p in paths],
        "dir": str(path.parent),
        "onPath": in_path,
        "target": install_ops.target_command(),
        "pathHint": None if in_path else install_ops.path_hint(path.parent),
    }
    if not good:
        return fail(f"写入启动器失败：{msg}", EXIT_RUNTIME)
    lines = [
        f"目录    {path.parent}",
        f"入口    {' / '.join(p.name for p in paths)}",
        f"指向    {' '.join(install_ops.target_command())}",
        "",
    ]
    if in_path:
        lines.append("PATH 已包含该目录，现在可以直接运行 hy-mem")
    else:
        lines.append("PATH 里还没有这个目录，加一下（然后把下面这条命令写进 SKILL.md 也行）：")
        lines.append("  " + str(install_ops.path_hint(path.parent)))
    # 先接管旧安装的配置与数据，再回收旧启动器 —— 顺序不能反：
    # adopt_stale 把启动器改指当前安装后，旧安装就从 PATH 上「消失」了，
    # 之后就再也找不到它的 config.json / data 在哪。
    adopted_install, install_notes = install_ops.adopt_previous_install()
    # 回收 PATH 上残留的旧 hy-mem 启动器（比如源码模式留在 ~/.local/bin 的壳），
    # 否则它们排在 PATH 前面时会把调用引到失效入口——见 install_ops.adopt_stale 注释。
    adopted, adopt_notes = install_ops.adopt_stale()
    payload["adoptedInstall"] = adopted_install
    payload["adoptNotes"] = install_notes + adopt_notes
    if adopted_install:
        lines.append("")
        lines.append("已沿用旧安装的配置与记忆数据：")
        lines.extend("  " + n for n in install_notes)
    if adopted:
        lines.append("")
        lines.append(f"接管了 {len(adopted)} 个旧启动器：")
        lines.extend("  " + n for n in adopt_notes)
    elif adopt_notes:
        lines.append("")
        lines.extend("  " + n for n in adopt_notes)
    return ok(payload, args.json, "\n".join(lines))


def cmd_version(args) -> int:
    """版本与安装信息。自动升级就靠它：比对版本 + 定位要替换的安装根。"""
    import config as cfgmod
    import helptext
    import install_ops

    frozen = bool(getattr(sys, "frozen", False))
    root = install_ops._current_root()
    cfg_env = os.getenv("HY_MEM_CONFIG", "").strip()
    cfg_path = Path(cfg_env) if cfg_env else root / "config.json"
    configured = cfg_path.is_file()
    data_dir = ""
    if configured:
        try:
            cfg = cfgmod.load()
            data_dir = str((cfg.get("storage") or {}).get("dataDir") or root / "data")
        except Exception:  # noqa: BLE001
            configured = False
    payload = {
        "ok": True,
        "version": helptext.VERSION,
        "mode": "exe" if frozen else "source",
        "installRoot": str(root),
        "configPath": str(cfg_path),
        "configured": configured,
        "dataDir": data_dir or str(root / "data"),
    }
    human = f"hy-mem {payload['version']}（{'exe' if frozen else '源码'}模式）\n  安装根 {payload['installRoot']}\n  数据目录 {payload['dataDir']}"
    return ok(payload, args.json, human)


def cmd_migrate(args) -> int:
    """把旧安装的数据目录迁入当前安装根（升级接管后的收尾）。

    为什么是复制不是搬：原目录保留作备份，用户确认无误后自己删——
    数据安全的事，宁可多占一份磁盘，不做不可逆动作。
    """
    import shutil

    import config as cfgmod
    import install_ops

    cur = install_ops._current_root()
    target = cur / "data"
    src = store.data_dir().resolve()  # 引导已重放 env，这里就是当前生效的数据目录

    def fail(msg: str) -> int:
        return ok({"ok": False, "error": msg}, args.json, msg)

    if not (cur / "config.json").is_file():
        return fail("当前安装还没有 config.json（尚未配置/接管），无处可迁")
    if src == target.resolve():
        return ok({"ok": True, "migrated": False, "dataDir": str(src)},
                  args.json, f"数据目录已在当前安装内，无需迁移：{src}")
    if not src.is_dir():
        return fail(f"数据目录不存在：{src}")
    if target.exists() and any(target.iterdir()):
        return fail(f"目标目录非空，拒绝覆盖：{target}")
    # worker 活着时不能迁：它还握着旧目录的写权，迁完新旧两处都会写，数据分家
    conn = store.connect()
    try:
        hb = store.worker_age(conn)
    finally:
        conn.close()
    if hb is not None and hb < 15.0:
        return fail(
            f"后台 worker 正在运行（心跳 {hb:.0f}s 前），等它空闲自退后再迁移"
            "（队列清空后最多 600s 自退出，或直接结束该 python 进程）"
        )
    with store.WriteLock():  # 复制期间挡住一切写入，保证 SQLite 拷贝一致
        # write.lock 排除：它正被本进程字节锁锁着，Windows 上连自己都读不了
        # （WinError 33），而且它只是锁的载体，不是数据，不迁移。
        shutil.copytree(src, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("write.lock"))
    cfg = cfgmod.load()
    storage = cfg.get("storage") or {}
    storage["dataDir"] = str(target)
    cfg["storage"] = storage
    cfg_env = os.getenv("HY_MEM_CONFIG", "").strip()
    cfg_path = Path(cfg_env) if cfg_env else cur / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass
    cfgmod.apply_env(cfg)  # 本进程后续操作立即走新目录
    nbytes = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    human = (
        f"已迁移 {nbytes / 1e6:.1f} MB → {target}\n"
        f"  原目录保留作备份，确认无误后可删除：{src}"
    )
    return ok(
        {"ok": True, "migrated": True, "from": str(src), "to": str(target), "bytes": nbytes},
        args.json,
        human,
    )


def cmd_skill(args) -> int:
    """管 SKILL.md：定位 / 一致性 / 分发 / 打印。

    智能体「能用上」这个 CLI 需要两件事同时成立 —— 命令在 PATH 上、宿主能读到
    SKILL.md。install 解决前者，这里解决后者；两者都过一遍再宣称「接好了」。
    """
    import skill_ops

    if getattr(args, "print_only", False):
        src = skill_ops.skill_path()
        if src is None:
            return fail("找不到 SKILL.md（产物里应有 skill/hy-mem/SKILL.md）")
        body = src.read_text(encoding="utf-8")
        return ok({"path": str(src), "content": body}, args.json, body)

    if getattr(args, "install", False) or getattr(args, "dir", None):
        if args.dir:
            okk, msg = skill_ops.install_to(Path(args.dir).expanduser() / skill_ops.NAME)
            return (ok({"ok": okk, "message": msg}, args.json, msg) if okk
                    else fail(msg, EXIT_RUNTIME))
        results = skill_ops.install_all()
        if not results:
            return fail("没有检测到任何宿主的 skill 目录（可用 --dir 指定）", EXIT_USAGE)
        good = all(r[1] for r in results)
        lines = [f"{'OK  ' if r[1] else 'FAIL'}  {r[0]:<16}{r[2]}" for r in results]
        payload = {"ok": good, "results": [{"host": h, "ok": o, "message": m} for h, o, m in results]}
        return ok(payload, args.json, "\n".join(lines)) if good else fail("\n".join(lines), EXIT_RUNTIME)

    st = skill_ops.status()
    if args.json:
        return ok(st, True)
    if not st["source"]:
        return fail("找不到 SKILL.md：产物里应带 skill/hy-mem/SKILL.md", EXIT_RUNTIME)
    lines = [f"SKILL.md  {st['source']}", f"体积      {st['bytes']} 字节", ""]
    if not st["hosts"]:
        lines.append("没有检测到宿主的 skill 目录")
    for h in st["hosts"]:
        mark = "已同步" if h["inSync"] else ("有旧版" if h["exists"] else "未安装")
        lines.append(f"  {h['host']:<16}{mark:<8}{h['dir']}")
    lines.append("")
    lines.append("同步到所有宿主：hy-mem skill --install")
    lines.append("打印全文：      hy-mem skill --print")
    return ok(st, False, "\n".join(lines))


def cmd_tui_test(args) -> int:
    import tui

    tui.enable_ansi()
    return tui.keytest()


def cmd_setup(args) -> int:
    import setup_wizard

    return setup_wizard.run_setup(scope=getattr(args, "step", "all") or "all")


def cmd_menu(args) -> int:
    import menu

    return menu.run_menu(lambda argv: main(argv))


def cmd_config(args) -> int:
    import config as cfgmod

    cfg = cfgmod.load()
    action = args.action

    if action == "path":
        print(cfgmod.config_path())
        return EXIT_OK

    if action == "reset":
        p = cfgmod.config_path()
        if not p.exists():
            return ok({"ok": True, "removed": False}, args.json, "配置文件不存在，无需重置")
        if not args.yes:
            return fail(f"会删除 {p}（含密钥）。确认请加 --yes", EXIT_USAGE)
        cfgmod.reset()
        return ok({"ok": True, "removed": True, "path": str(p)}, args.json, f"已删除 {p}")

    if action == "validate":
        results = cfgmod.validate(cfg)
        bad = [r for r in results if not r[1]]
        if args.json:
            return ok(
                {
                    "ok": not bad,
                    "checks": [{"name": n, "pass": p, "detail": d} for n, p, d in results],
                },
                True,
            )
        lines = []
        for name, passed, detail in results:
            mark = "PASS" if passed else "FAIL"
            lines.append(f"[{mark}] {name:<22}{detail}")
        lines.append("")
        lines.append("结论：配置完整" if not bad else f"结论：{len(bad)} 项未通过")
        return ok(results, False, "\n".join(lines))

    if action == "set":
        if not args.pairs:
            return fail("用法：hy-mem config set key=value（点路径，如 embedding.model=xxx）", EXIT_USAGE)
        import copy

        # 必须深拷贝。浅拷贝 dict(cfg) 只复制顶层，merged["embedding"] 与
        # cfg["embedding"] 是同一个对象，改 merged 会连带改掉 old，导致
        # probe_dim_change 变成"自己和自己比"，永远判不出嵌入模型变更。
        before = copy.deepcopy(cfg)
        merged = copy.deepcopy(cfg)
        for pair in args.pairs:
            if "=" not in pair:
                return fail(f"格式错误：{pair!r}，应为 key=value", EXIT_USAGE)
            keypath, value = pair.split("=", 1)
            node = merged
            parts = keypath.strip().split(".")
            for seg in parts[:-1]:
                node = node.setdefault(seg, {})
                if not isinstance(node, dict):
                    return fail(f"路径冲突：{keypath}", EXIT_USAGE)
            old = node.get(parts[-1])
            if isinstance(old, bool):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int):
                try:
                    value = int(value)
                except ValueError:
                    return fail(f"{keypath} 需要整数，得到 {value!r}", EXIT_USAGE)
            elif isinstance(old, float):
                try:
                    value = float(value)
                except ValueError:
                    return fail(f"{keypath} 需要小数，得到 {value!r}", EXIT_USAGE)
            node[parts[-1]] = value
        changed, why = cfgmod.probe_dim_change(before, merged)
        if changed and not args.yes:
            return fail(f"嵌入模型变化（{why}），需要 --yes 确认；建议改用 hy-mem setup 走完整流程", EXIT_USAGE)
        p = cfgmod.save(merged)
        if changed:
            print(
                f"hy-mem: 注意 —— {why}。已有记忆不会自动迁移，需要时跑 hy-mem setup 重建数据目录。",
                file=sys.stderr,
            )
        return ok({"ok": True, "path": str(p), "changed": args.pairs}, args.json, f"已更新 {p}")

    # 默认 show
    emb = cfg.get("embedding") or {}
    llm = cfg.get("llm") or {}
    payload = {
        "path": str(cfgmod.config_path()),
        "exists": cfgmod.exists(),
        "mode": cfg.get("mode"),
        "embedding": {
            "model": emb.get("model"),
            "dims": emb.get("dims"),
            "provider": emb.get("provider"),
            "baseUrl": emb.get("baseUrl"),
            "apiKey": "***" if emb.get("apiKey") else "",
        },
        "llm": {
            "model": llm.get("model"),
            "baseUrl": llm.get("baseUrl"),
            "keySet": bool(cfgmod.resolve_llm_key(cfg)),
            "keyEnv": llm.get("keyEnv"),
        },
        "dataDir": (cfg.get("storage") or {}).get("dataDir"),
        "minScore": (cfg.get("search") or {}).get("minScore"),
        "identity": {
            "userId": cfgmod.identity_defaults(cfg),
            "label": cfgmod.identity_label(cfgmod.identity_defaults(cfg)),
            "source": cfgmod.identity_source(cfg),
        },
    }
    if args.json:
        return ok(payload, True)
    lines = [
        f"配置文件  {payload['path']}" + ("" if payload["exists"] else "  （尚未创建，运行 hy-mem setup）"),
        "",
        f"身份      {payload['identity']['label']}   （hy-mem user 可切换）",
        f"运行模式  {payload['mode']}",
        f"向量模型  {emb.get('model')}  ({emb.get('dims')} 维, {emb.get('provider')})",
        f"嵌入入口  {emb.get('baseUrl') or '（本地 ONNX）'}",
        f"嵌入密钥  {'已设置' if emb.get('apiKey') else '未设置'}",
        f"LLM 模型  {llm.get('model')} @ {llm.get('baseUrl')}",
        f"LLM 密钥  {'已设置' if payload['llm']['keySet'] else '未设置'}"
        + (f"（来自 ${llm.get('keyEnv')}）" if llm.get("keyEnv") and not llm.get("apiKey") else ""),
        f"存储目录  {payload['dataDir']}",
        f"检索阈值  {payload['minScore']}",
    ]
    return ok(payload, False, "\n".join(lines))


# --------------------------------------------------------------------------
# 身份（user_id）
# --------------------------------------------------------------------------
def cmd_user(args) -> int:
    """查看 / 切换 user_id —— 多智能体共享或隔离记忆的唯一开关。

    设计：user_id 是记忆的隔离键。不设 = 通用（default），所有智能体读写的
    是同一份记忆；设了 = 只属于这个名字，与其它名字互不可见。
    切换**只改这一个字段**，不碰模型配置、不碰数据目录、不动已有记忆。
    """
    import config as cfgmod

    cfg = cfgmod.load()
    action = getattr(args, "action", "show") or "show"
    if getattr(args, "reset", False) and action == "show":
        action = "reset"
    env_uid = os.getenv("HY_MEM_USER_ID", "").strip()

    if action == "set":
        uid = (getattr(args, "uid", "") or "").strip()
        if not uid:
            return fail(
                "用法：hy-mem user set <user_id>（回到通用用 hy-mem user reset）",
                EXIT_USAGE,
            )
        path, effective = cfgmod.set_user_id(uid)
        prev = cfgmod.identity_defaults(cfg)
        payload = {
            "ok": True,
            "userId": effective,
            "general": effective == cfgmod.GENERAL_USER_ID,
            "previous": prev,
            "path": str(path),
        }
        if env_uid and env_uid != effective:
            payload["envOverride"] = env_uid
        human = (
            f"身份已切到 {cfgmod.identity_label(effective)}"
            + (f"\n  注意：环境变量 HY_MEM_USER_ID={env_uid} 优先级更高，实际生效的是它"
               if env_uid and env_uid != effective else "")
            + f"\n  {cfgmod.identity_label(prev)} 下的记忆没有删，只是现在看不到；切回去就能看到。"
            + "\n  只改身份，模型配置与数据目录都没动。"
        )
        if args.json:
            return ok(payload, True)
        print(human)
        return EXIT_OK

    if action == "reset":
        path, effective = cfgmod.set_user_id("")
        payload = {"ok": True, "userId": effective, "general": True, "path": str(path)}
        if env_uid:
            payload["envOverride"] = env_uid
        return ok(
            payload,
            args.json,
            f"已回到{cfgmod.identity_label(effective)}：所有智能体共享同一份记忆"
            + (f"（但环境变量 HY_MEM_USER_ID={env_uid} 仍在覆盖）" if env_uid else ""),
        )

    effective = cfgmod.identity_defaults(cfg)
    configured = cfgmod.identity_configured(cfg)
    source = cfgmod.identity_source(cfg)
    known = [str(x) for x in ((cfg.get("identity") or {}).get("known") or []) if str(x).strip()]
    if effective not in known:
        known.append(effective)

    if action == "list":
        rows = [
            {
                "userId": u,
                "label": cfgmod.identity_label(u),
                "current": u == effective,
                "source": source if u == effective else "",
            }
            for u in known
        ]
        if args.json:
            return ok({"ok": True, "current": effective, "users": rows}, True)
        lines = []
        for r in rows:
            mark = "← 当前" if r["current"] else ""
            lines.append(f"  {r['userId']:<20}{r['label']:<14}{mark}")
        if not lines:
            lines = ["  （只见过通用身份）"]
        return ok({"ok": True, "current": effective, "users": rows}, False, "\n".join(lines))

    # show（默认）
    payload = {
        "ok": True,
        "userId": effective,
        "general": effective == cfgmod.GENERAL_USER_ID,
        "label": cfgmod.identity_label(effective),
        "source": source,
        "configured": configured or None,
        "envOverride": env_uid or None,
        "known": known,
        "configPath": str(cfgmod.config_path()),
    }
    if args.json:
        return ok(payload, True)
    src_text = {
        "env": "环境变量 HY_MEM_USER_ID（优先级最高，临时覆盖用）",
        "config": "config.json 的 identity.userId",
        "general": "未设置 → 通用（default）",
    }.get(source, source)
    lines = [
        f"当前身份  {cfgmod.identity_label(effective)}",
        f"生效来源  {src_text}",
        f"配置文件  {payload['configPath']}",
        "",
        "  通用（default）：所有智能体共享同一份记忆 —— 这是没设 user_id 时的默认行为",
        "  其它名字      ：自成一套记忆，与别处互不可见，用于多智能体隔离",
        "",
        "切换身份（只改这一个字段，不动模型配置与数据目录）：",
        "  hy-mem user set writer         # 之后读写都属于 writer",
        "  hy-mem user reset              # 回到通用，与其它智能体共享",
        "  hy-mem user list               # 见过哪些身份",
        "",
        "临时只影响一条命令：",
        '  hy-mem -u writer search "..."  # 或在环境里设 HY_MEM_USER_ID',
    ]
    return ok(payload, False, "\n".join(lines))


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    """用法错误统一 exit 1。

    argparse 默认用 2，会和「运行时错误」撞号 —— agent 拿到 2 会去查端点，
    实际只是参数写错了。这里分开：用法错 1、运行时错 2、内部错 3。
    """

    def error(self, message: str):  # noqa: D102
        self.print_usage(sys.stderr)
        print(f"hy-mem: {message}", file=sys.stderr)
        print("提示：`hy-mem help` 看全部命令，`hy-mem help --json` 拿机器可读规格", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    import helptext

    p = _Parser(
        prog="hy-mem",
        description="Hy-Memory 记忆库命令行（lite=纯向量，pro=+LLM 抽取，ultra=+Schema 归纳）",
        epilog="给智能体：`hy-mem help --json` 输出全部命令的结构化规格（参数类型/默认值/返回字段/示例）。",
    )
    p.add_argument("--json", action="store_true", help="输出机器可解析 JSON")
    p.add_argument("--quiet", action="store_true", help="成功时不打印任何内容，只看退出码")
    p.add_argument("--no-color", action="store_true", help="关闭彩色（等价 NO_COLOR=1）")
    p.add_argument("--config", metavar="PATH", help="指定配置文件路径（等价 HY_MEM_CONFIG）；放在命令行任意位置都生效")
    p.add_argument("--fields", default="", help="只保留这些顶层字段，逗号分隔（省 agent 的 token）")
    p.add_argument("--verbose", action="store_true", help="显示 SDK 日志（默认静默）")
    p.add_argument("--version", action="version", version=f"hy-mem {helptext.VERSION}", help="打印版本号并退出")
    p.add_argument("--help-json", action="store_true", help="等同于 help --json（全局规格，供 agent 读取）")
    p.add_argument("--mode", choices=["lite", "pro", "ultra"], default=None, help="覆盖 MEMORY_MODE")
    p.add_argument("--data-dir", default=None, help="覆盖 MEMORY_DATA_DIR（仅 doctor 用）")
    p.add_argument("-u", "--user", default=None, help="user_id（默认取 config.identity.userId / HY_MEM_USER_ID）")
    p.add_argument("-a", "--agent", default=None, help="agent_id（默认不设，由 SDK 用 default_agent）")
    p.add_argument("-s", "--session", default=None, help="session_id")
    sub = p.add_subparsers(dest="cmd", required=False)

    # ---- 元信息 / 交互层 ----
    ps = sub.add_parser("help", help="命令说明（--json 给智能体读的结构化规格）")
    ps.add_argument("command", nargs="?", help="只看某个命令，例如 search")
    # 与全局 --json 等价：`hy-mem help --json` 是最自然的写法，不能因为
    # argparse 的全局选项位置敏感而报错。SUPPRESS 保证不覆盖前置的全局值。
    ps.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="输出 JSON 规格（同全局 --json）")
    ps.set_defaults(func=cmd_help)
    sub.add_parser("menu", help="交互式主界面（等价于不带参数运行）").set_defaults(func=cmd_menu)
    ps = sub.add_parser("setup", help="交互式设置：向量模型 / 运行模式 / LLM / 存储目录")
    ps.add_argument(
        "--step",
        default="all",
        choices=["all", "embedding", "mode", "llm", "storage", "search", "queue"],
        help="只改其中一项（默认 all=完整四步向导）",
    )
    ps.set_defaults(func=cmd_setup)
    ps = sub.add_parser("config", help="查看 / 修改 / 重置配置")
    ps.add_argument("action", nargs="?", default="show", choices=["show", "path", "reset", "validate", "set"])
    ps.add_argument("pairs", nargs="*", help="set 时的 key=value（点路径，如 embedding.model=xxx）")
    ps.add_argument("--yes", action="store_true", help="reset 或修改嵌入模型时跳过确认")
    ps.set_defaults(func=cmd_config)

    ps = sub.add_parser("user", help="查看 / 切换 user_id（不设=通用共享，设了=独立隔离）")
    ps.add_argument("action", nargs="?", default="show", choices=["show", "set", "reset", "list"])
    ps.add_argument("uid", nargs="?", help="set 时的 user_id；reset 表示回到通用（default）")
    ps.add_argument("--reset", action="store_true", help="等价于 user reset")
    ps.set_defaults(func=cmd_user)

    sub.add_parser("doctor", help="环境自检").set_defaults(func=cmd_doctor)
    ps = sub.add_parser("install", help="生成 PATH 上的 hy-mem 启动器（让智能体直接调用）")
    ps.add_argument("--dir", default=None, help="启动器目录，默认 %%LOCALAPPDATA%%\\hy-mem\\bin")
    ps.add_argument("--print", dest="print_only", action="store_true", help="只打印将写入的内容，不落盘")
    ps.set_defaults(func=cmd_install)
    ps = sub.add_parser("skill", help="SKILL.md 在哪、各宿主副本是否一致、要不要同步过去")
    ps.add_argument("--install", action="store_true", help="把 SKILL.md 铺到所有检测到的宿主 skill 目录")
    ps.add_argument("--dir", default=None, help="只装到指定目录（该目录下会建 hy-mem/SKILL.md）")
    ps.add_argument("--print", dest="print_only", action="store_true", help="打印 SKILL.md 全文，便于直接读")
    ps.set_defaults(func=cmd_skill)
    sub.add_parser("tui-test", help="按键自检：在真实终端里验证方向键/鼠标解码").set_defaults(func=cmd_tui_test)
    sub.add_parser("version", help="版本与安装信息（--json 供自动升级比对）").set_defaults(func=cmd_version)
    sub.add_parser("migrate", help="把旧安装的数据目录迁入当前安装根（升级接管后的收尾）").set_defaults(func=cmd_migrate)

    ps = sub.add_parser("add", help="写一条记忆")
    ps.add_argument("text", nargs="?", help="记忆正文；省略则从 stdin 读")
    ps.add_argument("--source", default="cli", help="来源标记")
    ps.add_argument("--split", action="store_true", help="按行拆成多条独立写入（提升检索粒度）")
    ps.add_argument("--tag", action="append", default=None, help="绑定标签，可重复：--tag work --tag apollo")
    ps.add_argument(
        "--async",
        dest="async_mode",
        action="store_true",
        help="只入队立即返回（pro 模式单条 6~9s），由 worker 异步消费",
    )
    ps.add_argument("--metadata", default=None, help="附加元数据，JSON 对象：--metadata '{\"project\":\"iisw\"}'")
    ps.add_argument("--memory-at", dest="memory_at", default=None, help="记忆发生时间（补录历史用）：2026-10-27 或 2026-10-27 14:30:00")
    ps.add_argument("--request-id", dest="request_id", default=None, help="幂等写 id：同一 id 重复提交只落一条")
    ps.set_defaults(func=cmd_add)

    ps = sub.add_parser("search", help="语义检索")
    ps.add_argument("query", help="查询语句；越接近原话命中率越高")
    ps.add_argument("-n", "--limit", type=int, default=10, help="返回条数")
    ps.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="相似度下限，默认取 HY_MEM_MIN_SCORE 或 0.35",
    )
    ps.add_argument("--tag", action="append", default=None, help="按标签过滤（自动多取候选再筛）")
    ps.add_argument("--tag-mode", choices=["all", "any"], default="all", help="多个标签是 AND 还是 OR")
    ps.add_argument("--since", default=None, help="只要这段时间之后：30m/12h/7d/30d/1y 或 2026-01-01")
    ps.add_argument("--until", default=None, help="只要这段时间之前，格式同 --since")
    ps.set_defaults(func=cmd_search)

    ps = sub.add_parser("list", help="列出记忆")
    ps.add_argument("-n", "--limit", type=int, default=20)
    ps.add_argument("--offset", type=int, default=0)
    ps.add_argument("--order", choices=["desc", "asc"], default="desc", help="desc 最新在前（默认）/ asc 最旧在前")
    ps.add_argument("--tag", action="append", default=None, help="按标签过滤")
    ps.add_argument("--tag-mode", choices=["all", "any"], default="all")
    ps.set_defaults(func=cmd_list)

    ps = sub.add_parser("get", help="按 id 读")
    ps.add_argument("memory_id", help="记忆 id（list 命令里能看到）")
    ps.set_defaults(func=cmd_get)

    ps = sub.add_parser("delete", help="按 id 删")
    ps.add_argument("memory_id", help="记忆 id；不可恢复")
    ps.add_argument("--dry-run", action="store_true", help="只确认这条记忆存在并打印它，不真删")
    ps.set_defaults(func=cmd_delete)

    ps = sub.add_parser("update", help="改一条记忆的内容（旧版本保留在演化链）")
    ps.add_argument("memory_id", help="记忆 id（list 命令里能看到）")
    ps.add_argument("content", nargs="?", help="新内容；省略则从 stdin 读")
    ps.set_defaults(func=cmd_update)

    ps = sub.add_parser("history", help="查一条记忆的变更历史（演化链版本）")
    ps.add_argument("memory_id", help="记忆 id")
    ps.set_defaults(func=cmd_history)

    ps = sub.add_parser("forget", help="按范围批量删（需 --yes）")
    ps.add_argument("--yes", action="store_true", help="确认执行；不加只打印影响范围")
    ps.add_argument("--dry-run", action="store_true", help="只统计会删掉多少条，不做任何改动")
    ps.set_defaults(func=cmd_forget)

    ps = sub.add_parser("finalize", help="收尾 session（触发演化）")
    ps.set_defaults(func=cmd_finalize)

    ps = sub.add_parser("stats", help="运行指标 / 库体积健康度")
    ps.add_argument("--minutes", type=int, default=5)
    ps.add_argument("--health", action="store_true", help="库体积健康度：条数/体积/延迟预期/归档建议")
    ps.add_argument("--vacuum", action="store_true", help="配合 --health：清理已完成 job 记录并 VACUUM")
    ps.add_argument("--max-scan", type=int, default=5000, help="--health 时最多扫描多少条记忆")
    ps.set_defaults(func=cmd_stats)

    # ---- 异步队列 ----
    ps = sub.add_parser("worker", help="消费异步任务（单写者）")
    ps.add_argument("--once", action="store_true", help="只消费当前队列，清空即退出")
    ps.add_argument("--max", type=int, default=0, help="最多处理 N 个任务后退出（0=不限）")
    ps.add_argument("--poll", type=float, default=1.0, help="空闲轮询间隔秒")
    ps.add_argument("--idle-exit", type=float, default=0, help="空闲超过 N 秒后退出（0=常驻）")
    ps.add_argument("--timeout", type=float, default=0, help="总运行时长上限秒（0=不限）")
    ps.add_argument("--no-recover", action="store_true", help="不回收僵死的 running 任务")
    ps.set_defaults(func=cmd_worker)

    ps = sub.add_parser("queue", help="查看队列")
    ps.add_argument("--status", choices=["pending", "running", "done", "dead"], default=None)
    ps.add_argument("-n", "--limit", type=int, default=20)
    ps.add_argument("--recover", action="store_true", help="先把僵死任务放回待处理")
    ps.set_defaults(func=cmd_queue)

    ps = sub.add_parser("mcp", help="MCP stdio 服务器（宿主拉起，勿手动运行）")
    ps.set_defaults(func=cmd_mcp)

    ps = sub.add_parser("drain", help="阻塞等待队列清空")
    ps.add_argument("--timeout", type=float, default=300)
    ps.add_argument("--poll", type=float, default=1.0)
    ps.set_defaults(func=cmd_drain)

    ps = sub.add_parser("retry", help="重投死信任务（job_id 或 dead）")
    ps.add_argument("job_id", help="任务 id；填 dead 表示全部死信")
    ps.add_argument("--all-dead", action="store_true", help="等价于 job_id=dead")
    ps.set_defaults(func=cmd_retry)

    # ---- 标签 ----
    ps = sub.add_parser("tags", help="标签清单 / 从已有记忆同步自动标签")
    ps.add_argument("--min-count", type=int, default=1)
    ps.add_argument("--refresh", action="store_true", help="从 SDK 拉全量补齐 LLM 自动标签并清理孤儿")
    ps.add_argument("--max-refresh", type=int, default=2000)
    ps.set_defaults(func=cmd_tags)

    ps = sub.add_parser("untag", help="解除标签绑定")
    ps.add_argument("memory_id", nargs="?")
    ps.add_argument("--tag", default=None, help="只解除某个标签（不配 memory_id 时全局生效，需 --yes）")
    ps.add_argument("--yes", action="store_true")
    ps.set_defaults(func=cmd_untag)

    ps = sub.add_parser("archive", help="按标签或记忆层归档统计")
    ps.add_argument("--by", choices=["tag", "layer"], default="tag")
    ps.add_argument("--min-count", type=int, default=1)
    ps.add_argument("--max-scan", type=int, default=2000, help="--by layer 时最多扫描条数")
    ps.set_defaults(func=cmd_archive)

    return p


def _print_short_help() -> None:
    import helptext

    print(helptext.render_text(helptext.build_spec(build_parser())))
    print()
    print("非交互环境：请直接调用子命令；机器可读规格用 `hy-mem help --json`。", file=sys.stderr)


def _force_utf8() -> None:
    """冻结成 exe 后强制 stdout/stderr 的编码。

    症状：源码模式中文正常，PyInstaller 打包后所有中文变乱码（输出 GBK 字节）。

    根因（实测得出，不是猜的）：本机环境已经设了 `PYTHONIOENCODING=utf-8` 和
    `PYTHONUTF8=1`，但 **PyInstaller 的冻结进程并不遵守它们** —— 字节抓出来是
    `bcc7 d2e4`（GBK 的"命令"），说明 stdout.encoding 仍是 cp936。

    所以不能"有 PYTHONIOENCODING 就跳过"，必须主动 reconfigure：
    - 优先用 PYTHONIOENCODING 指定的编码（尊重用户环境）
    - 否则用 utf-8
    - HY_MEM_ENCODING 可强制覆盖（GBK 的 cmd.exe 里需要时用）
    - 仅 frozen 时执行：源码模式下 Python 能正确推断编码，强改反而会在 GBK 终端出错
    """
    if not getattr(sys, "frozen", False):
        return
    want = os.getenv("HY_MEM_ENCODING") or os.getenv("PYTHONIOENCODING") or "utf-8"
    enc = (want.split(":", 1)[0] or "utf-8").strip()

    def _norm(s: str) -> str:
        return (s or "").lower().replace("-", "").replace("_", "")

    for stream in (sys.stdout, sys.stderr):
        try:
            if not hasattr(stream, "reconfigure"):
                continue
            if _norm(getattr(stream, "encoding", "")) == _norm(enc):
                continue
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:  # noqa: BLE001
            pass


# classify_error / error_detail 已上移 cli/errors.py（CLI 与 MCP 共用分类口径），
# 文件顶部导入，这里不再保留副本。


def cmd_mcp(args) -> int:
    """MCP stdio 服务器：宿主按会话拉起，随会话退出。

    铁律：此模式下 stdout 只准有协议响应。load_env 后直接进 serve() 主循环，
    不经过 ok()/print() 那套人类输出。
    """
    load_env()
    import mcp_server

    return mcp_server.serve()


def main(argv=None) -> int:
    _force_utf8()
    load_env()

    # 升级接管：全新解压（本安装根还没有 config.json）时，若 PATH 上仍指向
    # 另一个带配置的 hy-mem 安装，就沿用它的配置、记忆数据原位复用 —— 用户
    # 升级 = 解压新包 + 直接用，不用重走向导、不丢记忆。接管后要重放
    # apply_env，否则 MEMORY_DATA_DIR 还停在新目录，队列会跟主库分家。
    # 说明走 stderr：stdout 属于协议/结果，不能混入。
    try:
        import config as _cfg_ad
        import install_ops as _iol_ad

        _ad_info, _ad_notes = _iol_ad.adopt_previous_install()
        if _ad_info:
            _cfg_ad.apply_env(_cfg_ad.load())
            for _n in _ad_notes:
                print("hy-mem: " + _n, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass  # 探测失败不该挡住正常启动，install/doctor 里还能看到诊断

    argv_list = hoist_globals(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv_list)

    global _QUIET, _FIELDS, _JSON
    _QUIET = bool(getattr(args, "quiet", False))
    _JSON = bool(getattr(args, "json", False))
    _FIELDS = [f.strip() for f in str(getattr(args, "fields", "") or "").split(",") if f.strip()]
    if getattr(args, "no_color", False):
        os.environ["NO_COLOR"] = "1"
    if _QUIET and not _JSON:
        # --quiet 表示「只要退出码」。各处零散的 print 不可能逐个改干净，
        # 直接从流这一层掐掉；--json 例外，因为那份输出本身就是结果。
        import io as _io

        sys.stdout = _io.StringIO()

    # 身份回填：智能体调用时不该被迫传 -u。
    # 这同时修掉一个真实报错：search 走复数形参，未给 -u 时会传 [None]，
    # SDK 侧过滤成空列表后抛 "user_ids is required and cannot be empty"。
    if not getattr(args, "user", None):
        try:
            import config as _cfgmod

            args.user = _cfgmod.identity_defaults()
        except Exception:  # noqa: BLE001
            args.user = "default"

    # --help-json：不用进子命令就能拿到整份规格（agent 最省事的一条路）
    if getattr(args, "help_json", False):
        args.cmd = "help"
        args.command = None
        args.json = True
        args.func = cmd_help

    # SDK 会往 root logger 打大量 INFO（handlers 自带、propagate=False），
    # 全局 disable 是唯一稳的压法，否则输出无法被 agent 解析
    import logging

    # fastembed 用 loguru 而不是标准 logging，`logging.disable()` 管不到它，
    # 首次运行会在 stderr 打一串 "Could not download model …" 干扰解析。
    # 非 --verbose 就摘掉它的 handler（loguru 是进程级全局，移除即静默）。
    if not getattr(args, "verbose", False):
        try:
            from loguru import logger as _lg

            _lg.remove()
        except Exception:  # noqa: BLE001
            pass

    interactive = args.cmd in ("menu", "setup") or not getattr(args, "cmd", None)
    if not getattr(args, "verbose", False) and not interactive:
        # INFO/WARNING 里全是 pipeline 诊断；reconcile 还会把"BM25_ALL_ZERO"
        # 这类正常统计打成 ERROR，混进 stdout/stderr 会让 agent 解析失败。
        logging.disable(logging.ERROR)

    # 无子命令：TTY 下进交互界面；非 TTY（被 agent/skill 调用）时打帮助，
    # 绝不能在这里卡住等输入 —— 那会让调用方直接超时。
    if not getattr(args, "cmd", None):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            _print_short_help()
            return EXIT_OK
        try:
            import tui

            tui.enable_ansi()
            import setup_wizard

            need, why = setup_wizard.needs_setup()
            if need:
                print(tui.title("欢迎使用 hy-mem"))
                print(tui.panel([tui.c.dim(f"检测到：{why}")]))
                choice = tui.select(
                    "先做首次设置？",
                    [
                        ("开始设置（推荐）", "向量模型 → 运行模式 →（pro/ultra 才问）LLM → 存储目录", "s"),
                        ("先跳过，直接进主界面", "稍后可在「设置」里补；未配好前写入/检索会报错", "x"),
                    ],
                    default=0,
                )
                if choice == 0:
                    rc = setup_wizard.run_setup(first_run=True)
                    if rc != 0:
                        tui.pause("设置未全部通过，按任意键继续进主界面…")
                else:
                    import config as cfgmod

                    cfg = cfgmod.load()
                    cfg["setupComplete"] = True
                    cfg["setupSkipped"] = True
                    cfgmod.save(cfg)
                    tui.warn("已跳过：配置可能不完整，随时可用「设置」补齐")
            import menu

            return menu.run_menu(lambda a: main(a))
        except (KeyboardInterrupt, tui.Aborted):
            print()
            return EXIT_OK

    try:
        return args.func(args)
    except KeyboardInterrupt:
        return fail("已中断", 130)
    except Exception as e:  # noqa: BLE001
        # 交互层用 Aborted 表示「用户取消」，那不是错误，不该打堆栈或 exit 2
        if type(e).__name__ == "Aborted":
            print()
            print("hy-mem: 已取消", file=sys.stderr)
            return EXIT_OK
        code, err_code, hint = classify_error(e)
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        # --json 的结构化错误由 fail() 统一输出（_JSON 已置位）——别在这里
        # 手工再 print 一份：曾经两处各打一行，agent 会收到两行 JSON。
        return fail(error_detail(e), code, hint=hint, err_code=err_code)


if __name__ == "__main__":
    sys.exit(main())
