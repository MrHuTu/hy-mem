"""设置向导 / 存储运维的行为自测 —— 不发真实网络请求，用桩替换交互与探测.

覆盖的是「配错才知道」那类故障：
  - 首次设置是否把 setupComplete 写进去（否则每次都进向导）
  - lite 模式是否真的跳过 LLM 步骤
  - 单步修改是否只动那一项、其余不动
  - 数据目录迁移是否复制了文件、源目录是否保留
  - 路径校验能否挡住明显不是路径的输入
  - **第二次配置**能不能用（第 Q 节）：密钥只在环境变量里时，向导要能继承，
    而不是追问一个空 key 然后拿空串去请求（2026-10-28 的 401 事故）

用法：.venv/Scripts/python.exe scripts/test_setup.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="hy-mem-test-"))
os.environ["HY_MEM_HOME"] = str(TMP / "home")
os.environ["HY_MEM_CONFIG"] = str(TMP / "home" / "config.json")
os.environ["HY_MEM_TUI"] = "0"
(TMP / "home").mkdir(parents=True, exist_ok=True)

from cli.core import config as cfgmod  # noqa: E402
from cli.core import netprobe  # noqa: E402
from cli.ui import setup_wizard  # noqa: E402
from cli.core import storage_ops  # noqa: E402
from cli.core import presets as presets_mod  # noqa: E402
from cli.ui import tui  # noqa: E402

# stub() 会把 probe_llm 换成桩，这里先留一份真身，供第 7 节的判定逻辑测试用。
_REAL_PROBE_LLM = setup_wizard.probe_llm
_REAL_PROBE_EMB = setup_wizard.probe_embedding

results: list[bool] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))


def stub(**kw):
    """把交互与网络探测全部换成脚本化返回值。"""
    setup_wizard.probe_embedding = lambda emb, timeout=45: (True, "stub ok", 1024)
    setup_wizard.probe_llm = lambda llm, timeout=45: (True, "stub ok")
    tui.confirm = lambda q, default=False, **k: kw.get("confirm", True)
    tui.pause = lambda *a, **k: None


def main() -> int:
    print(f"临时目录 {TMP}")

    # ---- 1. 尚未设置时应判定 needs_setup ----
    print("\n首次设置判定")
    need, why = setup_wizard.needs_setup()
    check("无配置 → 需要设置", need, why)

    # ---- 2. 完整向导（pro）----
    print("\n完整向导（pro，选自定义不用预设 key）")
    stub()
    seq = {"select": [], "ask": [], "secret": []}

    def fake_select(q, options, default=0, **k):
        return seq["select"].pop(0)

    def fake_ask(q, default="", **k):
        return seq["ask"].pop(0) if seq["ask"] else default

    def fake_secret(q, default="", hint=None):
        return seq["secret"].pop(0) if seq["secret"] else (default or "sk-test")

    tui.select = fake_select
    tui.ask = fake_ask
    tui.ask_secret = fake_secret

    # 步骤内 select 顺序：向量模型(0=qwen-flash) → 模式(1=pro) → LLM(0=ysjx)
    # select 消耗序列（2026-10-29 起 _confirm_credentials 里的「密钥怎么用？」三选一
    # 也吃一次 select，默认 0=就用这把）：向量(0=GPT) → 三选一(0) → 模式(1=pro)
    # → LLM(0) → 三选一(0) → 存储(0)
    seq["select"] = [0, 0, 1, 0, 0, 0]
    seq["secret"] = ["sk-test-embed"]
    seq["ask"] = []
    data_dir = str(TMP / "data")
    # 先造一个「有数据的旧目录」，避免走迁移分支
    (TMP / "data").mkdir(exist_ok=True)
    (TMP / "data" / "keep.txt").write_text("x", encoding="utf-8")

    # 2026-10-29 起 scope=all 分流：无 config 的首次走顺序向导（first_run=True
    # 显式指定），已有 config 进设置菜单。本用例测的是顺序向导语义；
    # existing=None 让 run_setup 自己 load()（沙箱里 config 由前一步写入）。
    rc = setup_wizard.run_setup(scope="all", first_run=True)
    cfg = cfgmod.load_raw()
    check("run_setup 返回 0", rc == 0, f"rc={rc}")
    check("setupComplete 已写入", cfg.get("setupComplete") is True)
    check("mode=pro", cfg.get("mode") == "pro", str(cfg.get("mode")))
    check("向量模型已定", cfg["embedding"]["model"] == presets_mod.EMBEDDING_PRESETS[0].model,
          cfg["embedding"]["model"])
    check("dims=1024（实测值）", cfg["embedding"]["dims"] == 1024, str(cfg["embedding"]["dims"]))
    check("LLM 已定", cfg["llm"]["model"] == presets_mod.LLM_PRESETS[0].model, cfg["llm"]["model"])
    check("密钥已落盘（向导里显式输入）", cfg["embedding"]["apiKey"] == "sk-test-embed")

    need, why = setup_wizard.needs_setup()
    check("写完后不再需要设置", not need, why)

    # ---- 3. 单步修改只动一项 ----
    print("\n单步修改（检索阈值）")
    before = cfgmod.load_raw()
    seq["select"] = [2]  # 0.45 严格
    setup_wizard.run_setup(scope="search")
    after = cfgmod.load_raw()
    check("minScore 改为 0.45", after["search"]["minScore"] == 0.45, str(after["search"]))
    # 比 key 集合而不是整段相等：embedding 段新增了 keyEnv（密钥只记变量名），
    # 旧快照里没有这个字段，整段比较会把「新增默认字段」误判成「改了配置」。
    _emb_keys = ("baseUrl", "model", "apiKey", "dims")
    check("其它字段未变（embedding）",
          {k: after["embedding"].get(k) for k in _emb_keys}
          == {k: before["embedding"].get(k) for k in _emb_keys},
          str(after["embedding"]))
    check("其它字段未变（llm）", after["llm"] == before["llm"])

    # ---- 4. lite 模式跳过 LLM ----
    print("\nlite 模式跳过 LLM")
    llm_before = cfgmod.load_raw()["llm"]
    seq["select"] = [0]  # 模式选 lite
    setup_wizard.run_setup(scope="mode")
    now = cfgmod.load_raw()
    check("mode=lite", now.get("mode") == "lite", str(now.get("mode")))
    check("LLM 配置原样保留", now["llm"] == llm_before)

    # ---- 5. 存储运维 ----
    print("\n存储目录运维")
    src = TMP / "data"
    dst = TMP / "moved"
    (src / "sub").mkdir(parents=True, exist_ok=True)
    (src / "sub" / "a.bin").write_bytes(b"x" * 2048)
    (src / "write.lock").write_text("lock", encoding="utf-8")
    ok, msg = storage_ops.migrate(str(src), str(dst))
    check("迁移成功", ok, msg)
    check("文件已复制", (dst / "sub" / "a.bin").exists())
    check("锁文件未被复制", not (dst / "write.lock").exists())
    check("源目录保留", src.exists())
    size, files = storage_ops.dir_usage(str(dst))
    check("体积统计为正", size > 0 and files >= 1, f"{size}/{files}")

    ok, msg = storage_ops.check_target("n")
    check("非路径输入被拒", not ok, msg)
    ok, _msg = storage_ops.check_target(str(TMP / "brand-new" / "deep"))
    check("可创建的多级目录通过", ok)

    ok, msg = storage_ops.backup(str(src))
    check("备份成功", ok, msg)

    ok, msg = storage_ops.wipe(str(dst))
    check("清空成功且目录保留", ok and dst.exists(), msg)
    check("清空后为空", storage_ops.dir_usage(str(dst))[1] == 0)

    # ---- 6. 路径归一化 ----
    print("\n路径归一化")
    p = storage_ops.normalize_path('"~/hy-mem/data/"')
    check("去引号 + 展开 ~ + 去尾斜杠", "~" not in p and not p.endswith(("/", "\\")), p)
    if os.name == "nt":
        q = storage_ops.normalize_path("%USERPROFILE%\\hy-mem")
        check("展开 %VAR%", "%" not in q, q)

    # ---- 7. LLM 探测：推理模型的 content 为空不能算坏 ----
    # 起因：探针用 max_tokens=8，推理模型把配额全花在 thinking 上，
    # content 恒为空串 → doctor 对一个完全可用的模型报「环境不可用」。
    print("\n推理模型探测")

    def fake_post(content="", reasoning="", finish="stop", status=200):
        def _p(url, payload, key="", timeout=45, insecure=False, key_label="API Key"):
            msg = {"role": "assistant", "content": content}
            if reasoning:
                msg["reasoning_content"] = reasoning
            # 现在只有两种结果：Sent（发出去了，带真实状态码）/ NotSent（没发出）。
            # 这里返回 Sent —— 状态码是服务器给的，不会再出现 0。
            return netprobe.Sent(status, {"choices": [{"message": msg, "finish_reason": finish}]}, 0.4)

        return _p

    orig_post = netprobe.post_json
    base = {"baseUrl": "https://x/v1", "model": "m", "apiKey": "k"}
    try:
        netprobe.post_json = fake_post("可用")
        ok1, d1 = _REAL_PROBE_LLM(dict(base))
        check("content 有内容 → 可用", ok1, d1)

        netprobe.post_json = fake_post("", "想了一大段", "length")
        ok2, d2 = _REAL_PROBE_LLM(dict(base))
        check("推理模型 content 空 → 仍判可用", ok2, d2)
        check("提示里点明是推理模型", "推理模型" in d2, d2)

        netprobe.post_json = fake_post("", "", "stop")
        ok3, d3 = _REAL_PROBE_LLM(dict(base))
        check("真·空响应 → 不可用", not ok3, d3)

        netprobe.post_json = fake_post("", "", "stop", 401)
        ok4, d4 = _REAL_PROBE_LLM(dict(base))
        check("HTTP 401 → 不可用", not ok4, d4)

        # 请求没发出（DNS 失败）时，文案里不能出现任何 HTTP 状态码
        netprobe.post_json = lambda *a, **k: netprobe.NotSent(
            "请求没有发出：域名解析失败（检查 baseUrl 拼写）", cause="dns"
        )
        ok5, d5 = _REAL_PROBE_LLM(dict(base))
        check("未发出 → 不可用", not ok5, d5)
        check("未发出的文案里没有 HTTP", "HTTP" not in d5, d5)
        check("未发出的文案点明原因", "没有发出" in d5 and "解析" in d5, d5)
    finally:
        netprobe.post_json = orig_post

    # ---- 8. 队列积压要能被看见 ----
    # 起因：异步写入只入队不落库，用户写完立刻检索 → 0 条，以为记忆丢了。
    # 现在 search/list 会带「队列里还有 N 条没落库」的线索，这里锁住计数函数。
    print("\n队列积压可见")
    from cli import hy_mem_cli  # noqa: E402
    from cli.core import store  # noqa: E402

    _empty = hy_mem_cli._pending_jobs()
    check("空队列计为 0", _empty == 0, str(_empty))
    _conn = store.connect()
    try:
        store.enqueue(
            _conn,
            {"text": "积压可见性探针", "source": "cli", "identity": {"user_id": "default"}, "tags": []},
            mode="pro",
            max_attempts=3,
        )
    finally:
        _conn.close()
    _pend = hy_mem_cli._pending_jobs()
    check("入队后能数出积压", _pend >= 1, str(_pend))

    # ---- 10. 运行环境自检（专治「双击闪退」） ----
    print("\n运行环境自检")
    from cli.core import runtime_check as rc

    items = rc.checks(full=True)
    names = [c["item"] for c in items]
    for want in ("windows", "arch", "package", "writable", "vcruntime", "console", "disk"):
        check(f"自检含 {want}", want in names, ",".join(names))
    check("源码模式下 package 跳过但不判失败", rc.ok(items), rc.report(items)[:60])
    check("八项全通过时报告是一行", "通过" in rc.report(items) and "\n" not in rc.report(items), rc.report(items)[:60])

    # 失败项要带「怎么修」—— 这是整个自检的价值所在，不能只报「失败」
    fake = [
        {"item": "package", "ok": False, "detail": "缺 _internal", "fix": "整个目录一起拷", "fatal": True},
        {"item": "disk", "ok": False, "detail": "可用 10 MB", "fix": "清磁盘", "fatal": False},
    ]
    text = rc.report(fake)
    check("失败报告写明怎么修", "怎么修" in text and "整个目录一起拷" in text, text.splitlines()[1][:40])
    check("阻塞项与提醒项分开标注", "[阻塞]" in text and "[提醒]" in text)
    check("致命项失败即判定不可用", rc.ok(fake) is False, "fatal 项失败 → 不可用")
    check("非致命项单独出现时不阻塞", rc.ok([fake[1]]) is True)
    check("failures 默认只看致命项", len(rc.failures(fake)) == 1, str(len(rc.failures(fake))))
    check("failures 全量包含非致命项", len(rc.failures(fake, fatal_only=False)) == 2)

    # 日志落点必须可写（闪退时就是靠它留证据）
    logfile = rc.write_log("自检日志探针")
    check("启动日志可写", logfile is not None and Path(logfile).exists(), str(logfile))
    if logfile:
        body = Path(logfile).read_text(encoding="utf-8", errors="replace")
        check("日志内容落盘", "自检日志探针" in body)

    check("internal_dir 指向安装根的 _internal", rc.internal_dir().name == "_internal", str(rc.internal_dir()))
    check("interactive() 在管道下为 False", rc.interactive() is False, "被测试进程的 stdin 不是终端")

    # ---- 11. 双击启动器（产物里的 启动 hy-mem.cmd） ----
    print("\n双击启动器")
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_exe as bmod

    body = bmod.LAUNCHER_CMD
    check("启动器内容是纯 ASCII（cmd 65001 解析缺陷）", body.isascii(), "含中文会被截断成命令")
    check("启动器先查 _internal", "_internal\\base_library.zip" in body)
    check("启动器缺件时 type 中文说明", "startup-error.txt" in body and "type " in body)
    check("启动器缺件时 pause（窗口不消失）", ":broken" in body and "pause" in body)
    check("启动器带参数时不停窗口", 'if not "%~1"=="" exit /b %RC%' in body)
    check("中文说明文件本身不是 ASCII（用 type 输出）", not bmod.STARTUP_ERROR_TXT.isascii())
    check("说明里明确「不需要装 Python」", "不需要" in bmod.STARTUP_ERROR_TXT and "Python" in bmod.STARTUP_ERROR_TXT)

    # ---- 12. 显式跳过之后不再反复劝 ----
    print("\n跳过标记")
    cfg = cfgmod.load()
    cfg["setupSkipped"] = True
    cfgmod.save(cfg)
    need, why = setup_wizard.needs_setup()
    check("setupSkipped=True 时不再要求设置", not need, why)
    cfg["setupSkipped"] = False
    cfgmod.save(cfg)

    # ---- 13. 模型下载镜像（fastembed 默认拉 huggingface，国内会卡 40s） ----
    print("\n模型下载镜像")
    from cli import hy_mem_cli as _cli

    _saved = os.environ.pop("HF_ENDPOINT", None)
    try:
        _cli.load_env()
        check(
            "默认 HF_ENDPOINT 指向国内镜像",
            os.environ.get("HF_ENDPOINT") == "https://hf-mirror.com",
            str(os.environ.get("HF_ENDPOINT")),
        )
        os.environ["HF_ENDPOINT"] = "https://example.invalid"
        _cli.load_env()
        check(
            "用户显式设置不被覆盖",
            os.environ.get("HF_ENDPOINT") == "https://example.invalid",
            str(os.environ.get("HF_ENDPOINT")),
        )
    finally:
        os.environ.pop("HF_ENDPOINT", None)
        if _saved is not None:
            os.environ["HF_ENDPOINT"] = _saved

    # provider 缺省必须取 config（openai），不能落成 local —— 否则全新安装
    # 第一次 doctor 就去下 92MB 模型
    _c = cfgmod.load()
    _c["embedding"]["provider"] = "openai"
    cfgmod.save(_c)
    os.environ.pop("MEMORY_EMBEDDER_PROVIDER", None)
    check("provider 缺省取 config（openai）", _cli.embedding_provider() == "openai", _cli.embedding_provider())
    os.environ["MEMORY_EMBEDDER_PROVIDER"] = "local"
    check("env 里的 provider 优先于 config", _cli.embedding_provider() == "local", _cli.embedding_provider())
    os.environ.pop("MEMORY_EMBEDDER_PROVIDER", None)

    # 未配置时也要有确定的 data 目录（否则首次运行写进 ~/.hy_memory，
    # 向导保存后 dataDir 变成安装根/data，早先写的记忆就找不到了）
    _dd0 = os.environ.pop("MEMORY_DATA_DIR", None)
    _cli.load_env()
    _expect_dd = str(cfgmod.default_data_dir())
    check("未配置时 data 目录也落在安装根", os.environ.get("MEMORY_DATA_DIR") == _expect_dd, str(os.environ.get("MEMORY_DATA_DIR")))
    if _dd0 is not None:
        os.environ["MEMORY_DATA_DIR"] = _dd0

    # 本地嵌入的模型缓存应跟着数据目录走（换包不重下 92MB）
    from cli.core import local_embedder as _lemb

    _dd = os.environ.get("MEMORY_DATA_DIR")
    os.environ["MEMORY_DATA_DIR"] = str(TMP / "cacheprobe")
    try:
        cache = _lemb._default_cache_dir()
        check("模型缓存落在数据目录下", cache.parent.name == "cacheprobe" or "cacheprobe" in str(cache), str(cache))
    finally:
        if _dd is not None:
            os.environ["MEMORY_DATA_DIR"] = _dd
        else:
            os.environ.pop("MEMORY_DATA_DIR", None)

    # ---- N. 密钥粘进中文：预检就拦住，绝不出 "HTTP 0" ----
    # 真触发点：http.client 用 latin-1 编码请求头，key 含非 ASCII 时在「发请求」
    # 那一刻抛 UnicodeEncodeError，请求根本没出去（2026-10-27 用户实测踩到）。
    # 现在不等异常发生 —— netprobe.precheck 在组包之前就判定「进不了 HTTP 报文」。
    print("\n密钥含非 ASCII（中文粘贴） / 请求未发出")
    _r = netprobe.post_json("https://example.invalid/v1/embeddings", {"model": "m"}, "没有啊", 5)
    check("判定为「未发出」", isinstance(_r, netprobe.NotSent), type(_r).__name__)
    check("原因归类为 precheck", getattr(_r, "cause", "") == "precheck", getattr(_r, "cause", ""))
    check("指到具体位置（第 1 位）", "第 1 位" in _r.reason, _r.reason)
    check("文案里不出现 HTTP", "HTTP" not in _r.reason, _r.reason)

    for _bad, _label in (
        ("sk-ab\u3000cd", "全角空格"),
        ("sk-ab\u200bcd", "零宽空格"),
        ("\ufeffsk-ab", "BOM 标记"),
        ("sk-ab cd", "半角空格"),
    ):
        _rb = netprobe.post_json("https://example.invalid/v1/embeddings", {}, _bad, 1)
        check(f"{_label} 被拦下", isinstance(_rb, netprobe.NotSent) and _rb.cause == "precheck", _rb.reason)

    # 地址里混进中文（路径含中文）同样在预检挡住
    _ru = netprobe.post_json("https://example.invalid/v1/嵌入", {}, "sk-ok", 1)
    check("地址含中文被拦下", isinstance(_ru, netprobe.NotSent) and "路径" in _ru.reason, _ru.reason)

    # 真正会「没发出」的另一类：主机不可达。文案要指向网络原因，不是 HTTP。
    _rd = netprobe.post_json("https://no-such-host-hymem.invalid/v1/embeddings", {"model": "m"}, "sk-ok", 2)
    check("不可达主机 → 未发出", isinstance(_rd, netprobe.NotSent), type(_rd).__name__)
    # 精确断言「不出现 HTTP 状态码」：提示文案里的「HTTPS_PROXY」变量名
    # 含字母 HTTP，曾被误伤（2026-10-28，DNS 分支必现）。
    import re as _re  # noqa: E402  与下方守卫段同风格：局部导入
    check("不可达主机不出现 HTTP", _re.search(r"HTTP \d{3}", _rd.reason) is None, _rd.reason)

    # 各网络分支的文案单测 —— 不依赖本机 DNS/代理行为（有的网络会把不存在的
    # 域名劫持成一个假 IP，于是表现为超时而非解析失败）。
    import socket as _socket

    _dns = netprobe._from_net_error(_socket.gaierror(11001, "getaddrinfo failed"), 5, 0.0)
    check("DNS 文案点明解析", "解析" in _dns.reason, _dns.reason)
    check("DNS 文案给出下一步", "baseUrl" in _dns.reason, _dns.reason)
    check("DNS 归类正确", _dns.cause == "dns", _dns.cause)

    _ref = netprobe._from_net_error(ConnectionRefusedError(10061, "Connection refused"), 5, 0.0)
    check("拒连文案点明端口", "拒绝" in _ref.reason, _ref.reason)

    _tls = netprobe._from_net_error(Exception("certificate verify failed"), 5, 0.0)
    check("证书文案点明 TLS", "证书" in _tls.reason, _tls.reason)

    _to = netprobe._from_net_error(TimeoutError("timed out"), 5, 0.0)
    check("超时文案带秒数", "5s" in _to.reason, _to.reason)

    # 走代理时 urllib 会把「代理自己报的 502」塞进 reason，不能让人误读成目标服务 502
    _px = netprobe._from_net_error(Exception("Tunnel connection failed: 502 Bad Gateway"), 5, 0.0)
    check("代理失败点明是代理", "代理" in _px.reason, _px.reason)
    check("代理失败不甩 502 原文", "502" not in _px.reason, _px.reason)

    # 类型层面就堵死：不许构造 status=0 的「成功结果」
    try:
        netprobe.Sent(0, {}, 0.1)
        check("Sent 拒绝 status=0", False, "竟然构造成功了")
    except ValueError:
        check("Sent 拒绝 status=0", True)

    _ok, _msg, _dims = _REAL_PROBE_EMB(
        {"provider": "openai", "baseUrl": "https://example.invalid/v1", "model": "m", "apiKey": "没有啊"},
        timeout=5,
    )
    check("probe 判定不可用", not _ok, _msg)
    check("不再出现裸的 HTTP 0", "HTTP 0" not in _msg, _msg)
    check("说是「请求没有发出」", "没有发出" in _msg, _msg)

    _ok2, _msg2 = _REAL_PROBE_LLM(
        {"baseUrl": "https://example.invalid/v1", "model": "m", "apiKey": "sk-中文key"},
        timeout=5,
    )
    check("LLM 侧同样给人话", (not _ok2) and "非 ASCII" in _msg2, _msg2)

    # ---- O. 状态码要自带「下一步」，不能只甩英文原文 ----
    print("\nHTTP 状态码 → 下一步")
    check("401 指向密钥", "密钥" in netprobe.status_hint(401), netprobe.status_hint(401))
    check("404 指向路径", "路径" in netprobe.status_hint(404), netprobe.status_hint(404))
    check("429 指向限流", "限流" in netprobe.status_hint(429), netprobe.status_hint(429))
    check("未知码不硬编提示", netprobe.status_hint(418) == "", netprobe.status_hint(418))

    _h = netprobe.explain(netprobe.Sent(401, {"error": {"message": "invalid key"}}, 0.1))
    check("explain = 状态码 + 原文 + 下一步", _h.startswith("HTTP 401") and "invalid key" in _h and "密钥" in _h, _h)
    _hn = netprobe.explain(netprobe.NotSent("请求没有发出：域名解析失败"))
    check("explain 对未发出不加 HTTP 字样", _hn == "请求没有发出：域名解析失败", _hn)

    # ---- P. 源码守卫：出站请求只能从 netprobe 走 ----
    # 这几条是「杜绝」的机制保证：以后谁想再加一个 HTTP 调用点，如果自己拼
    # urlopen / 又造出 status=0 的约定，测试直接失败，而不是等用户双击闪退。
    print("\n出站请求收口（源码守卫）")
    import re as _re

    _urlopen_offenders, _zero_offenders = [], []
    # 不只扫 urlopen：直接 import urllib.request / requests / httpx 也算自己造轮子
    _net_import = _re.compile(
        r"^\s*(?:import|from)\s+(?:urllib\.request|requests|httpx)\b", _re.M
    )
    # S1 分层后 cli/ 是三层包结构（core/ui/mcp），守卫范围同步递归
    _all_py = sorted((ROOT / "cli").rglob("*.py"))
    for _p in _all_py:
        if _p.name == "netprobe.py":
            continue
        _src = _p.read_text(encoding="utf-8")
        if "urlopen(" in _src or _net_import.search(_src):
            _urlopen_offenders.append(_p.name)
        if _re.search(r"status\s*==\s*0", _src) or _re.search(r"return\s+0,\s*\{", _src):
            _zero_offenders.append(_p.name)
    check("cli/ 下只有 netprobe 能发 HTTP", not _urlopen_offenders, str(_urlopen_offenders))
    check("cli/ 下不再有 status==0 的约定", not _zero_offenders, str(_zero_offenders))

    _net = (ROOT / "cli" / "core" / "netprobe.py").read_text(encoding="utf-8")
    check("netprobe 拒绝 status<100", "self.status < 100" in _net)
    check("netprobe 发送前预检", "precheck(url, key" in _net or "precheck(" in _net)

    # ---- Q. 第二次配置：密钥只配在环境变量里也要能用 ----
    #
    # 事故现场（2026-10-28，用户截图 401）：
    #   设置 → 向量模型 → 选「当前项」→ 向导追问 API Key → 用户回车 → 探针拿空串请求
    #   → `401 No API-key provided`，而环境变量里那把有效 key 根本没被读到。
    # 两个根因都在「密钥判据」上：step_embedding 用 emb["apiKey"] 判断有没有 key、
    # probe_embedding 直接拿 emb["apiKey"] 当请求头。
    # 这类 bug 只有真走一次「第二次配置」才暴露 —— 首次设置时 apiKey 有值，走不到。
    print("\n第二次配置：密钥只在环境变量里（401 事故回归）")

    ENV_NAME = "MEMORY_EMBEDDER_API_KEY"
    ENV_KEY = "sk-test-env-only-key-0123456789"
    _saved_env = os.environ.get(ENV_NAME)
    _real_post = netprobe.post_json
    _seen = {"key": None, "calls": 0}

    def _fake_post(url, body, key, timeout, insecure=False, **kw):
        _seen["key"] = key
        _seen["calls"] += 1
        if url.endswith("/chat/completions"):
            return netprobe.Sent(status=200, payload={"choices": [{"message": {"content": "可用"},
                                                                    "finish_reason": "stop"}]}, seconds=0.02)
        return netprobe.Sent(status=200, payload={"data": [{"embedding": [0.01] * 1024}]}, seconds=0.02)

    def _probe_cfg(**over):
        base = dict(presets_mod.EMBEDDING_PRESETS[0].to_cfg())
        base.update({"apiKey": "", "keyEnv": ENV_NAME, "dims": 1024})
        base.update(over)
        return base

    setup_wizard.probe_embedding = _REAL_PROBE_EMB  # 用真探针，只桩掉网络
    netprobe.post_json = _fake_post

    # Q1 探针能从环境变量取到 key（而不是把空串塞进 Authorization）
    os.environ[ENV_NAME] = ENV_KEY
    _seen.update({"key": None, "calls": 0})
    _ok, _msg, _dims = _REAL_PROBE_EMB(_probe_cfg())
    check("探针用环境变量里的 key", _ok and _seen["key"] == ENV_KEY, f"ok={_ok} key={_seen['key']!r} msg={_msg}")
    check("探针拿到实测维度", _dims == 1024, str(_dims))

    # Q2 一个 key 都取不到时：不发请求，报「密钥为空」而不是 401
    os.environ.pop(ENV_NAME, None)
    _seen.update({"key": None, "calls": 0})
    _ok, _msg, _dims = _REAL_PROBE_EMB(_probe_cfg(keyEnv="HY_MEM_NOT_SET_ANYWHERE"))
    check("无密钥时不发请求", _seen["calls"] == 0, f"calls={_seen['calls']}")
    check("无密钥时报错说清来源", (not _ok) and "密钥为空" in _msg, _msg)

    # Q3 核心回归：第二次进设置、选当前项、密钥只在环境变量里
    os.environ[ENV_NAME] = ENV_KEY
    _asked: list[str] = []
    _infos: list[str] = []
    tui.select = lambda q, options, default=0, **k: 0 if "向量模型" in q else default
    tui.ask_secret = lambda q, default="", hint=None: (_asked.append(q), "")[1]
    tui.ask = lambda q, default="", **k: default
    # 三件套确认环节（2026-10-28 新增）默认通过；个别用例按需覆盖
    tui.confirm = lambda q, default=False, **k: True
    tui.info = lambda msg, *a, **k: _infos.append(str(msg))
    _seen.update({"key": None, "calls": 0})
    _new = setup_wizard.step_embedding({"mode": "pro", "embedding": _probe_cfg()})
    check("二次配置不再追问 API Key", not _asked, str(_asked))
    check("二次配置的探针用的是环境变量 key", _seen["key"] == ENV_KEY, repr(_seen["key"]))
    # 2026-10-29 语义反转：key 默认固化进 config.json（apiKey 有值、keyEnv 清空）
    check("二次配置后 key 已固化进配置（apiKey=env 值）", _new.get("apiKey") == ENV_KEY, repr(_new.get("apiKey"))[:30])
    check("固化后 keyEnv 清空（不再依赖环境变量）", _new.get("keyEnv") == "", repr(_new.get("keyEnv")))
    check("二次配置后 dims 按实测写入", _new.get("dims") == 1024, str(_new.get("dims")))

    # Q4 换到**另一个端点**：旧端点的 key 绝不能借用（那正是 401 的来源），
    # 必须当场要求填新 key，并且提示里要说清是「换了端点」而不是「key 错了」
    _asked.clear()
    _seen.update({"key": None, "calls": 0})
    tui.select = lambda q, options, default=0, **k: 1 if "向量模型" in q else default
    # 换端点被追问新 key 时，模拟用户填了新端点的 key（真实行为；旧桩返回空
    # 会被三件套确认环节按「信息不完整」拒绝 → Aborted，那本身就是防线）
    tui.ask_secret = lambda q, default="", hint=None: (_asked.append(q), "sk-zhipu-new")[1]
    _new2 = setup_wizard.step_embedding({"mode": "pro", "embedding": _probe_cfg()})
    _other = presets_mod.EMBEDDING_PRESETS[1]
    check("换端点必须重填 key（不继承旧 key）", bool(_asked), str(_asked))
    check("换端点时探针拿新 key（旧 env key 没被借用）", _seen["key"] == "sk-zhipu-new",
          repr(_seen["key"]))
    check("换端点后新 key 直存 config（keyEnv 清空）", _new2.get("apiKey") == "sk-zhipu-new"
          and _new2.get("keyEnv") == "", repr(_new2.get("keyEnv")))
    # 关键回归：新配置 apiKey=手填值，resolve 优先取它（旧 env key 不参与），不会 401
    check("换端点后 resolve 取手填新 key（不是旧 env key）",
          cfgmod.resolve_embed_key({"embedding": _new2}) == "sk-zhipu-new",
          repr(cfgmod.resolve_embed_key({"embedding": _new2})))
    check("换端点后模型已切换", _new2.get("model") == _other.model, str(_new2.get("model")))

    # Q8 同一端点只是改模型（自定义）：仍走继承，不追问
    _asked.clear()
    _seen.update({"key": None, "calls": 0})
    tui.select = lambda q, options, default=0, **k: len(options) - 1 if "向量模型" in q else default
    tui.ask = lambda q, default="", **k: default
    _same = setup_wizard.step_embedding(
        {"mode": "pro", "embedding": _probe_cfg(model="自定义同端点模型")}
    )
    check("同端点改模型不追问 key", not _asked, str(_asked))
    check("同端点改模型仍用环境变量 key", _seen["key"] == ENV_KEY, repr(_seen["key"]))
    check("同端点改模型后 key 固化（keyEnv 清空）", _same.get("apiKey") == ENV_KEY
          and _same.get("keyEnv") == "", repr(_same.get("keyEnv")))

    # Q5 预设自身要把 keyEnv 带进配置（否则重选预设会把变量名冲成空）
    check("预设 to_cfg 带 keyEnv",
          presets_mod.EMBEDDING_PRESETS[0].to_cfg().get("keyEnv") == presets_mod.EMBEDDING_PRESETS[0].key_env,
          str(presets_mod.EMBEDDING_PRESETS[0].to_cfg().get("keyEnv")))
    check("预设收敛为「主流三家 + 自定义」",
          len(presets_mod.EMBEDDING_PRESETS) == 3 and len(presets_mod.LLM_PRESETS) == 3,
          f"{len(presets_mod.EMBEDDING_PRESETS)} / {len(presets_mod.LLM_PRESETS)}")
    check("向量预设不含 DeepSeek（官方无 embedding 接口）",
          all("deepseek" not in p.base_url.lower() for p in presets_mod.EMBEDDING_PRESETS))
    check("LLM 预设含 DeepSeek",
          any("deepseek" in p.base_url.lower() for p in presets_mod.LLM_PRESETS))

    # Q6 首次安装（**配置文件还不存在**）——最容易漏的一条路径
    #
    # config.load() 在没有 config.json 时会调 _fill_from_env()，把 $MEMORY_EMBEDDER_API_KEY /
    # $MEMORY_LLM_API_KEY 的值复制进内存里的 apiKey。于是「apiKey 非空」并不代表用户手填过，
    # 只看它会误判成"已有 key，不用写回 keyEnv"，结果预设声明的变量名（$YSJX_API_KEY，本机为空）
    # 被当成密钥来源 → 探针拿到空串 → `✗ 密钥为空`。
    # 实测现场（0.9.12 打包前）：空沙箱 `setup --step llm` 提示「将继承当前密钥（sk-fw…）」
    # 紧接着却报「✗ 密钥为空（配置里的 apiKey 与 keyEnv 指向的变量都取不到）」。
    print("\n首次安装：配置不存在、密钥由环境变量回填")
    LLM_ENV = "MEMORY_LLM_API_KEY"
    LLM_KEY = "sk-test-llm-env-only-0123456789"
    # 本机用户可能真设过 $YSJX_API_KEY（2026-10-29 实测踩到），而本段的前提是
    # 「预设声明的变量名在本机为空、只有约定变量有值」。跑前临时摘掉，跑完还原。
    _saved_ysjx = os.environ.pop("YSJX_API_KEY", None)
    _saved_llm_env = os.environ.get(LLM_ENV)
    os.environ[LLM_ENV] = LLM_KEY
    setup_wizard.probe_llm = _REAL_PROBE_LLM  # 真探针，只桩网络
    setup_wizard.probe_embedding = _REAL_PROBE_EMB

    _asked.clear()
    _seen.update({"key": None, "calls": 0})
    tui.select = lambda q, options, default=0, **k: 0
    _llm_preset = presets_mod.LLM_PRESETS[0]
    _llm_cfg = dict(_llm_preset.to_cfg())
    # 复刻 _fill_from_env 的结果：apiKey 被 env 值填上，keyEnv 仍是预设/默认声明的名字
    _llm_cfg["keyEnv"] = "YSJX_API_KEY"
    _llm_cfg["apiKey"] = LLM_KEY
    _new_llm = setup_wizard.step_llm({"mode": "pro", "llm": _llm_cfg})
    check("首次安装不追问 LLM key", not _asked, str(_asked))
    check("首次安装的探针拿到环境变量 key", _seen["key"] == LLM_KEY, repr(_seen["key"]))
    check("首次安装 key 固化进配置", _new_llm.get("apiKey") == LLM_KEY, repr(_new_llm.get("apiKey"))[:30])
    check("固化后 keyEnv 清空", _new_llm.get("keyEnv") == "", repr(_new_llm.get("keyEnv")))

    # Q7 首次安装的向量模型同理：env 回填让 key_in_file 为真，但不应追问
    _asked.clear()
    _seen.update({"key": None, "calls": 0})
    _new_emb_fresh = setup_wizard.step_embedding(
        {"mode": "pro", "embedding": _probe_cfg(apiKey=ENV_KEY)}
    )
    check("首次安装不追问嵌入 key", not _asked, str(_asked))
    check("首次安装的嵌入探针拿到 env key", _seen["key"] == ENV_KEY, repr(_seen["key"]))
    check("首次安装的嵌入 key 固化", _new_emb_fresh.get("apiKey") == ENV_KEY, repr(_new_emb_fresh.get("apiKey"))[:30])
    check("固化后 keyEnv 清空", _new_emb_fresh.get("keyEnv") == "", repr(_new_emb_fresh.get("keyEnv")))

    if _saved_llm_env is None:
        os.environ.pop(LLM_ENV, None)
    else:
        os.environ[LLM_ENV] = _saved_llm_env

    # Q6 走完整单步设置（scope=embedding）落盘后，配置里仍是「apiKey 空 + keyEnv 在」
    _seen.update({"key": None, "calls": 0})
    tui.select = lambda q, options, default=0, **k: 0 if "向量模型" in q else 0
    tui.confirm = lambda q, default=False, **k: True
    _rc = setup_wizard.run_setup(scope="embedding", existing={"mode": "lite", "embedding": _probe_cfg(),
                                                              "storage": {"dataDir": str(TMP / "data")},
                                                              "search": {"minScore": 0.35}})
    _afterq = cfgmod.load_raw()
    check("单步设置返回 0", _rc == 0, str(_rc))
    check("落盘后 key 已固化（apiKey 有值）", (_afterq.get("embedding") or {}).get("apiKey") == ENV_KEY, str(_afterq.get("embedding"))[:120])
    check("落盘后 keyEnv 清空", (_afterq.get("embedding") or {}).get("keyEnv") == "", str(_afterq.get("embedding"))[:120])

    # Q7 同一类坑的另一半：LLM 预设声明 $YSJX_API_KEY，本机实际存在 $MEMORY_LLM_API_KEY。
    # 只看预设名会逼重填；继承之后还必须把 keyEnv 写回成本机那个变量名，否则保存后取不到。
    print("\n第二次配置 LLM：变量名与预设不一致")
    LLM_ENV = "MEMORY_LLM_API_KEY"
    LLM_KEY = "sk-test-llm-only-key-9876543210"
    _saved_llm_env = os.environ.get(LLM_ENV)
    os.environ[LLM_ENV] = LLM_KEY
    setup_wizard.probe_llm = _REAL_PROBE_LLM  # 用真探针，只桩掉网络
    _asked.clear()
    _seen.update({"key": None, "calls": 0})
    tui.select = lambda q, options, default=0, **k: 0
    # 三件套确认默认通过（信息齐就该测连通性；False 会死循环在「换 key→再确认」）
    tui.confirm = lambda q, default=False, **k: True
    _llm_new = setup_wizard.step_llm({
        "mode": "pro",
        "embedding": _probe_cfg(),
        "llm": {"preset": "ysjx-flash", "baseUrl": presets_mod.LLM_PRESETS[0].base_url,
                "model": presets_mod.LLM_PRESETS[0].model, "apiKey": "", "keyEnv": LLM_ENV},
    })
    check("LLM 二次配置不追问 key", not _asked, str(_asked))
    check("LLM 探针用的是本机环境变量的 key", _seen["key"] == LLM_KEY, repr(_seen["key"]))
    check("LLM 二次配置 key 固化", _llm_new.get("apiKey") == LLM_KEY, repr(_llm_new.get("apiKey"))[:30])
    check("固化后 keyEnv 清空", _llm_new.get("keyEnv") == "", repr(_llm_new.get("keyEnv")))
    if _saved_llm_env is None:
        os.environ.pop(LLM_ENV, None)
    else:
        os.environ[LLM_ENV] = _saved_llm_env
    if _saved_ysjx is not None:
        os.environ["YSJX_API_KEY"] = _saved_ysjx

    netprobe.post_json = _real_post
    if _saved_env is None:
        os.environ.pop(ENV_NAME, None)
    else:
        os.environ[ENV_NAME] = _saved_env

    bad = results.count(False)
    print(f"\n{'全部通过' if not bad else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
