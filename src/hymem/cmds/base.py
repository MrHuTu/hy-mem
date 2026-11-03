"""CLI 公共助手：输出/退出码/就绪度闸门/身份解析/转发 stub（真源在 core 与 backends）。

S2 拆分（docs/specs/2026-11-02-cli-split-ports/）：本模块从 hymem/cli.py 按域拆入，
逻辑零变化——全局状态存于 cmds.base，跨模块经 _base 属性读写。
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

from hymem.core import netprobe
from hymem.core import store
from hymem.core import vdb_endpoint
from hymem.core.timefilter import _in_range, _item_ts, _parse_time as _parse_when

from hymem.core.errors import EXIT_INTERNAL, EXIT_OK, EXIT_RUNTIME, EXIT_USAGE  # noqa: F401
from hymem.core.errors import classify_error, error_detail  # noqa: F401
from hymem.core.two_phase import CommitFailed, ExtractFailed  # noqa: F401  供 except 引用


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
        from hymem.core import config as cfgmod

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
            from hymem.core import config as cfgmod2

            os.environ["MEMORY_DATA_DIR"] = str(cfgmod2.default_data_dir())
        except Exception:  # noqa: BLE001
            pass


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


_LAST_FAIL: tuple[str, str] | None = None


def fail(msg: str, code: int = EXIT_RUNTIME, hint: str = "", extra: dict | None = None, err_code: str | None = None) -> int:
    """报错。--json 时在 stdout 也留一份结构化错误，agent 不必去解析 stderr。

    error.code 用稳定的字符串枚举（E_USAGE / E_RUNTIME / E_INTERNAL），
    和退出码语义一一对应；数字退出码仍在进程返回值里，两者不要混着读。
    err_code 允许调用方给出比数字退出码更精确的枚举（如 E_CONFIG）；
    extra 用于附加机器可读的细节（例如配置缺项清单），合并进 error 对象。
    """
    global _LAST_FAIL
    _LAST_FAIL = (err_code or _EXIT_CODES.get(code, "E_RUNTIME"), msg)
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


def readiness(cfg: dict | None = None) -> dict:
    """「现在到底能不能读/写记忆」：缺什么、补哪一项、怎么补。

    以前配不全的典型表现是：写入报一个 SDK 内部的 httpx/401 堆栈，或者检索
    静默返回空列表 —— 两种都让人误判成「程序坏了」「没有记忆」。这里把
    缺项翻成人话，并给出**可以直接复制执行的修复命令**。
    """
    from hymem.core import config as cfgmod

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
        if not cfgmod.resolve_embed_key(cfg):
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
        cfgmod.resolve_embed_key(cfg),
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
    global _LAST_FAIL

    from hymem.core import config as cfgmod

    missing = state["missing"]
    lead = f"配置还没配好，暂不执行：{action}" if action else "配置还没配好"
    msg = lead + "（缺 " + "、".join(m["item"] for m in missing) + "）"
    hint = "hy-mem setup"
    if _JSON:
        return fail(msg, EXIT_USAGE, hint, extra={"kind": "config", "missing": missing})
    try:
        from hymem.ui import tui
        from hymem.ui.tui import c

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
    # 这条路径是「直接打印 + 返回退出码」，没经过 fail()：手动补一次，
    # 否则操作日志里只剩一个 FAIL、看不出是配置不全（ops.log 靠它记原因）
    _LAST_FAIL = ("E_CONFIG", msg)
    return EXIT_USAGE


def require_ready(action: str = "", cfg: dict | None = None) -> int | None:
    """放行返回 None；配置不全则打印缺项并返回退出码。"""
    state = readiness(cfg)
    if state["ok"]:
        return None
    return _report_missing(state, action)


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


def embedding_provider() -> str:
    from hymem.core.clientops import embedding_provider as _f
    return _f()


def build_embedding_service():
    from hymem.core.clientops import build_embedding_service as _f
    return _f()


def make_client(mode: str | None):
    """兼容名。S1 起统一走协议层工厂（ports.registry.make_backend）——
    返回 MemoryBackend 包装（未列出的属性透传底层 SDK client，调用点零感知）；
    未来换 backend 时本函数是 CLI 侧唯一改动点。"""
    from hymem.ports.registry import make_backend

    return make_backend(mode)


def find_derived_nodes(client: object, root_id: str) -> list[str]:
    from hymem.core.clientops import find_derived_nodes as _f
    return _f(client, root_id)


def sweep_derived_nodes(client: object, root_id: str) -> tuple[list[str], str]:
    from hymem.core.clientops import sweep_derived_nodes as _f
    return _f(client, root_id)


def delete_memory_with_cascade(client: object, memory_id: str) -> dict:
    from hymem.core.clientops import delete_memory_with_cascade as _f
    return _f(client, memory_id)


def update_memory(client: object, memory_id: str, content: str):
    from hymem.core.clientops import update_memory as _f
    return _f(client, memory_id, content)


def add_sync_with_retry(client: object, text: str, *, attempts: int = 4, **kw):
    from hymem.core.clientops import add_sync_with_retry as _f
    return _f(client, text, attempts=attempts, **kw)


def extract_phase(client: object, text: str, **kw):
    from hymem.core.two_phase import extract_phase as _f
    return _f(client, text, **kw)


def prepare_commit(client: object, text: str, agent_result, **kw):
    from hymem.core.two_phase import prepare_commit as _f
    return _f(client, text, agent_result, **kw)


def commit_extracted_with_retry(client: object, text: str, agent_result, *, prepared: dict | None = None, attempts: int = 4, **kw):
    from hymem.core.two_phase import commit_extracted_with_retry as _f
    return _f(client, text, agent_result, prepared=prepared, attempts=attempts, **kw)


def data_dir() -> Path:
    from hymem.core.procs import data_dir as _f
    return _f()


def _worker_cap() -> int:
    from hymem.core.procs import _worker_cap as _f
    return _f()


def _spawn_detached_worker(idle_exit: float = 90.0, extra_args: list | None = None) -> bool:
    from hymem.core.procs import _spawn_detached_worker as _f
    return _f(idle_exit, extra_args)


def _spawn_detached_serve(idle_exit: float = 1800.0) -> bool:
    from hymem.core.procs import _spawn_detached_serve as _f
    return _f(idle_exit)


def _parse_memory_at(v):
    from hymem.core.searchops import _parse_memory_at as _f
    return _f(v)


def _snapshot_ids(client, ident: dict, cap: int = 2000) -> tuple[set[str], bool]:
    from hymem.core.searchops import _snapshot_ids as _f
    return _f(client, ident, cap)


def _ids_of(result: dict) -> list[str]:
    from hymem.core.searchops import _ids_of as _f
    return _f(result)


def _new_ids_only(returned: list[str], before: set[str] | None) -> list[str]:
    from hymem.core.searchops import _new_ids_only as _f
    return _f(returned, before)


def _bind_tags(ids: list[str], tags: list[str], source: str = "user") -> int:
    from hymem.core.searchops import _bind_tags as _f
    return _f(ids, tags, source)


def _flatten_search(resp: dict) -> list:
    from hymem.core.searchops import _flatten_search as _f
    return _f(resp)


def _embedding_unhealthy() -> str:
    from hymem.core.searchops import _embedding_unhealthy as _f
    return _f()


_SURROGATE_RE = None


def _strip_surrogates(text: str) -> tuple[str, int]:
    """剔除孤立的代理项（U+D800–U+DFFF）。返回 (清理后文本, 剔除个数)。

    为什么必须清：孤立代理项一进 utf-8 编码就抛
    `UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcaa'
     in position N: surrogates not allowed`，而且抛出点在建 HTTP 请求体的
    路上，离真正的输入处隔了好几层，看起来像是密钥/地址的错。
    """
    global _SURROGATE_RE
    if _SURROGATE_RE is None:
        import re

        _SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
    clean, n = _SURROGATE_RE.subn("", text or "")
    return clean, n


def _decode_stdin(raw: bytes) -> tuple[str, str]:
    """stdin 字节 → 文本，返回 (文本, 实际用到的编码)。

    为什么不用 `sys.stdin.read()`：冻结进程（PyInstaller）的 stdin 编码是
    **系统 locale（中文 Windows 上是 cp936）**，而错误处理是 surrogateescape。
    上游按 UTF-8 管道喂中文时，cp936 会把字节切错，剩下的零头变成孤立代理项
    ——2026-10-28 线上事故就是这么来的：`printf '刘德华…' | hy-mem add`
    直接炸 `'\\udcaa' ... surrogates not allowed`，提示还指到密钥上去了。

    所以一律按字节读、自己解码：BOM 优先 → UTF-8 → GBK（cmd.exe 的历史习惯）
    → 最后兜底 replace，任何情况下都不把孤立代理项带进后续流程。
    """
    if not raw:
        return "", "utf-8"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8-sig"
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace"), "utf-16"
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8/replace"


def _read_text(arg_text: str | None) -> str:
    text = arg_text
    if text is None:
        buf = getattr(sys.stdin, "buffer", None)
        if buf is not None:
            text, enc = _decode_stdin(buf.read())
            if enc not in ("utf-8", "utf-8-sig", "utf-16"):
                # 兼容旧习惯，但必须说破：猜错会写成乱码，静默最糟
                print(f"hy-mem: 警告：stdin 不是 UTF-8，已按 {enc} 解码；"
                      "建议改 `chcp 65001` 或直接把内容作为参数传入", file=sys.stderr)
        else:
            text = sys.stdin.read()
    text, dropped = _strip_surrogates(text or "")
    if dropped:
        print(f"hy-mem: 警告：输入里有 {dropped} 个无法编码的字符（多来自非 UTF-8 "
              "终端/管道），已剔除；内容可能不完整", file=sys.stderr)
    return text.strip()


def _split_lines(text: str) -> list[str]:
    """按行拆条。一次 add 的多个事实会被压成一条 l2_fact，稀释向量拉低召回。"""
    parts = [p.strip() for p in text.replace("\r", "").split("\n") if p.strip()]
    return parts if len(parts) > 1 else [text]


def _bind_tags_to_new(client, ident: dict, before: set[str], returned: list[str],
                      tags: list[str], *, before_complete: bool = True) -> tuple[list[str], str]:
    from hymem.core.searchops import _bind_tags_to_new as _f
    return _f(client, ident, before, returned, tags, before_complete=before_complete)


def _remember_auto(items: list[dict]) -> None:
    from hymem.core.searchops import _remember_auto as _f
    return _f(items)


def _effective_tags(items: list[dict]) -> dict[str, set[str]]:
    from hymem.core.searchops import _effective_tags as _f
    return _f(items)


def _filter_by_tags(items: list[dict], tags: list[str] | None, tag_mode: str = "all") -> list[dict]:
    from hymem.core.searchops import _filter_by_tags as _f
    return _f(items, tags, tag_mode)


def _pending_jobs() -> int:
    from hymem.core.searchops import _pending_jobs as _f
    return _f()


def config_defaults_user() -> str:
    from hymem.core import config as _c

    return _c.identity_defaults()

# 显式导出清单：pyflakes 不识别 `# noqa`，re-export 只能靠 __all__ 标记为「已使用」。
# 这里列的是本模块对外暴露的全部名字（不含 __dunder），添加/删除导出时必须同步改这里。
__all__ = [
    "CommitFailed",
    "DEFAULT_ENV",
    "EXIT_INTERNAL",
    "EXIT_OK",
    "EXIT_RUNTIME",
    "EXIT_USAGE",
    "ExtractFailed",
    "Path",
    "SKILL_ROOT",
    "_EXIT_CODES",
    "_FIELDS",
    "_GLOBAL_FLAGS",
    "_GLOBAL_VALUED",
    "_JSON",
    "_LAST_FAIL",
    "_Palette",
    "_QUIET",
    "_SURROGATE_RE",
    "_add_kwargs",
    "_bind_tags",
    "_bind_tags_to_new",
    "_decode_stdin",
    "_effective_tags",
    "_embedding_unhealthy",
    "_filter_by_tags",
    "_flatten_search",
    "_ids_of",
    "_in_range",
    "_item_ts",
    "_new_ids_only",
    "_parse_memory_at",
    "_parse_when",
    "_pending_jobs",
    "_project",
    "_read_text",
    "_remember_auto",
    "_report_missing",
    "_resolve_home",
    "_snapshot_ids",
    "_spawn_detached_serve",
    "_spawn_detached_worker",
    "_split_lines",
    "_strip_surrogates",
    "_worker_cap",
    "add_sync_with_retry",
    "annotations",
    "argparse",
    "asyncio",
    "build_embedding_service",
    "c",
    "classify_error",
    "commit_extracted_with_retry",
    "config_defaults_user",
    "data_dir",
    "datetime",
    "delete_memory_with_cascade",
    "embedding_provider",
    "emit",
    "error_detail",
    "extract_phase",
    "fail",
    "find_derived_nodes",
    "hoist_globals",
    "identity",
    "identity_lists",
    "json",
    "load_env",
    "make_client",
    "netprobe",
    "ok",
    "os",
    "prepare_commit",
    "readiness",
    "require_ready",
    "socket",
    "store",
    "subprocess",
    "sweep_derived_nodes",
    "sys",
    "time",
    "update_memory",
    "vdb_endpoint",
]
