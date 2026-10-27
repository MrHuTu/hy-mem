"""设置向导 / 存储运维的行为自测 —— 不发真实网络请求，用桩替换交互与探测.

覆盖的是「配错才知道」那类故障：
  - 首次设置是否把 setupComplete 写进去（否则每次都进向导）
  - lite 模式是否真的跳过 LLM 步骤
  - 单步修改是否只动那一项、其余不动
  - 数据目录迁移是否复制了文件、源目录是否保留
  - 路径校验能否挡住明显不是路径的输入

用法：.venv/Scripts/python.exe scripts/test_setup.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cli"))

TMP = Path(tempfile.mkdtemp(prefix="hy-mem-test-"))
os.environ["HY_MEM_HOME"] = str(TMP / "home")
os.environ["HY_MEM_CONFIG"] = str(TMP / "home" / "config.json")
os.environ["HY_MEM_TUI"] = "0"
(TMP / "home").mkdir(parents=True, exist_ok=True)

import config as cfgmod  # noqa: E402
import setup_wizard  # noqa: E402
import storage_ops  # noqa: E402
import tui  # noqa: E402

# stub() 会把 probe_llm 换成桩，这里先留一份真身，供第 7 节的判定逻辑测试用。
_REAL_PROBE_LLM = setup_wizard.probe_llm

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
    seq["select"] = [0, 1, 0]
    seq["secret"] = ["sk-test-embed"]
    seq["ask"] = []
    # step_storage 的 select 选 0=保持当前目录
    seq["select"].append(0)
    data_dir = str(TMP / "data")
    # 先造一个「有数据的旧目录」，避免走迁移分支
    (TMP / "data").mkdir(exist_ok=True)
    (TMP / "data" / "keep.txt").write_text("x", encoding="utf-8")

    rc = setup_wizard.run_setup(scope="all")
    cfg = cfgmod.load_raw()
    check("run_setup 返回 0", rc == 0, f"rc={rc}")
    check("setupComplete 已写入", cfg.get("setupComplete") is True)
    check("mode=pro", cfg.get("mode") == "pro", str(cfg.get("mode")))
    check("向量模型已定", cfg["embedding"]["model"] == "qwen3.7-text-embedding-flash", cfg["embedding"]["model"])
    check("dims=1024（实测值）", cfg["embedding"]["dims"] == 1024, str(cfg["embedding"]["dims"]))
    check("LLM 已定", cfg["llm"]["model"] == "deepseek-v4-flash", cfg["llm"]["model"])
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
    check("其它字段未变（embedding）", after["embedding"] == before["embedding"])
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
        def _p(url, body, key, timeout, insecure):
            msg = {"role": "assistant", "content": content}
            if reasoning:
                msg["reasoning_content"] = reasoning
            return status, {"choices": [{"message": msg, "finish_reason": finish}]}, 0.4

        return _p

    orig_post = setup_wizard._post_json
    base = {"baseUrl": "https://x/v1", "model": "m", "apiKey": "k"}
    try:
        setup_wizard._post_json = fake_post("可用")
        ok1, d1 = _REAL_PROBE_LLM(dict(base))
        check("content 有内容 → 可用", ok1, d1)

        setup_wizard._post_json = fake_post("", "想了一大段", "length")
        ok2, d2 = _REAL_PROBE_LLM(dict(base))
        check("推理模型 content 空 → 仍判可用", ok2, d2)
        check("提示里点明是推理模型", "推理模型" in d2, d2)

        setup_wizard._post_json = fake_post("", "", "stop")
        ok3, d3 = _REAL_PROBE_LLM(dict(base))
        check("真·空响应 → 不可用", not ok3, d3)

        setup_wizard._post_json = fake_post("", "", "stop", 401)
        ok4, d4 = _REAL_PROBE_LLM(dict(base))
        check("HTTP 401 → 不可用", not ok4, d4)
    finally:
        setup_wizard._post_json = orig_post

    # ---- 8. 队列积压要能被看见 ----
    # 起因：异步写入只入队不落库，用户写完立刻检索 → 0 条，以为记忆丢了。
    # 现在 search/list 会带「队列里还有 N 条没落库」的线索，这里锁住计数函数。
    print("\n队列积压可见")
    import hy_mem_cli  # noqa: E402
    import store  # noqa: E402

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
    import runtime_check as rc

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
    import hy_mem_cli as _cli

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
    import local_embedder as _lemb

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

    bad = results.count(False)
    print(f"\n{'全部通过' if not bad else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
