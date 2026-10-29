# -*- coding: utf-8 -*-
"""设置向导 UI 级自测 —— 真 CLI 子进程 + 管道喂键 + 本地回环假端点。

为什么函数级桩测不够（四条铁律第 4 条，2026-10-28 立）：

  `test_setup.py` 直接调 `step_embedding()` 并把 `probe_embedding` 换成桩，
  这一层验证的是「向导逻辑对不对」。但 401 事故发生在**真实按键流**里：
  设置 → 选当前项 → 按 N → 探针发出请求 → 401。函数级桩测一步都复现不了：

    - 按键流会不会走错分支（编号菜单选错项、回车落错默认值）
    - keyEnv 的继承/回落规则在「第二次进设置」时是否真的按 C3/C4 执行
    - 探针发出的 Authorization 头到底是哪把 key
    - 401 发生时用户看到的报错能不能定位到「key 属于旧端点」

  所以这一套**不桩任何产品代码**（铁律 2），只做三件事：

    1. 起两台本地 HTTP 假端点（各自只认自己签发的 key，别家 key 一律 401）
    2. 用真子进程跑 `hy-mem setup --step embedding/llm`，stdin 管道喂按键
    3. 断言退出码 / 输出文案 / config.json 最终形态 / 假端点看到的请求头

  假端点为什么必要：UI 级测试不能真调云厂商（没 key、慢、花钱、不可复现），
  但又必须让「旧 key 打新端点 → 401」真实发生，而不是桩出一个 401。

交互规范（2026-10-28 用户口径）：设置模型时先等用户把 baseUrl / 模型 / key
补充完整，确认后才测连通性；不可用可重设；通过后**有旧配置时**提示是否覆盖。

本套 2026-10-28 首跑即抓到三个真 bug（均在「第二次配置」家族，已修）：
  · 自定义分支 same_endpoint 写死 True → 换 baseUrl 静默继承旧 key（C/E 红）
  · 自定义配置的默认光标落第 1 个预设而非「自定义」→ 回车秒变跨端点（B 红）
  · ask_secret 空回车放行 → apiKey=""+keyEnv=约定变量 → resolve 回落拿旧 key（C 红）

场景覆盖（每个可配置项至少测两遍，铁律 1）：
  A. 首次设置（config 不存在）→ 走通整个管道、keyEnv 正确落盘
  B. 二次进设置、选当前项（回车）→ 三件套确认明示来源、继承不落盘、无变化不问覆盖
  C. 二次进设置、自定义换 baseUrl（跨端点）→ 不继承旧 key、探针用新 key 命中
  D. 二次进设置、换预设端点 → 明确告知旧 key 不能复用、EOF 中止不落盘
  E. LLM 同型场景 B/C（step_llm 与 step_embedding 同构）

用法：.venv/Scripts/python.exe scripts/test_setup_ui.py
（不打真云厂商；假端点只绑 127.0.0.1 随机端口）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
CLI = REPO / "src" / "hymem" / "cli.py"

results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    mark = "  PASS  " if cond else "  FAIL  "
    print(mark + name + (f"  → {detail}" if detail and not cond else ""))


# ---------------------------------------------------------------------------
# 假 OpenAI 兼容端点：每台只认自己的一套 key，别家 key 一律 401
# ---------------------------------------------------------------------------
EMB_KEY1 = "sk-emb-key-s1-111"   # 只被假端点1 接受
EMB_ROT = "sk-emb-key-s1-rot9"   # S1 的轮换新 key（同端点升级场景用）
EMB_KEY2 = "sk-emb-key-s2-222"   # 只被假端点2 接受 —— 跨端点必须换 key 的判据
LLM_KEY1 = "sk-llm-key-s1-111"
LLM_KEY2 = "sk-llm-key-s2-222"
SEEN: list[dict] = []  # 所有假端点收到的请求（tag/path/key/auth_ok）


def make_handler(emb_key: str, llm_key: str, tag: str):
    class _FakeOpenAI(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静音访问日志
            pass

        def _reply(self, status: int, body: dict):
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            auth = self.headers.get("Authorization") or ""
            key = auth[7:] if auth.startswith("Bearer ") else ""
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
            want = emb_key if "/embeddings" in self.path else llm_key
            if isinstance(want, str):
                want = {want}
            rec = {"tag": tag, "path": self.path, "key": key,
                   "model": str(payload.get("model") or ""), "auth_ok": key in want}
            SEEN.append(rec)
            if not rec["auth_ok"]:
                self._reply(401, {"error": {"message": "Token is invalid."}})
                return
            if "/embeddings" in self.path:
                self._reply(200, {"data": [{"embedding": [0.1] * 8, "index": 0}]})
            elif "/chat/completions" in self.path:
                self._reply(200, {
                    "choices": [{"message": {"role": "assistant", "content": "可用"},
                                 "finish_reason": "stop"}]
                })
            else:
                self._reply(404, {"error": {"message": "not found"}})

    return _FakeOpenAI


def start_fake(emb_key: str, llm_key: str, tag: str) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(emb_key, llm_key, tag))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


# ---------------------------------------------------------------------------
# 驱动器：真子进程跑 setup，管道喂键
# ---------------------------------------------------------------------------
def drive(step, keys, sandbox, env_extra=None, timeout=120):
    """跑 `hy-mem setup --step <step>`，返回 (退出码, 合并输出, 最终 config)。"""
    env = {
        **os.environ,
        "HY_MEM_TUI": "0",  # 管道下走编号菜单（真产品降级路径）
        "NO_COLOR": "1",
        "HY_MEM_CONFIG": str(sandbox / "config.json"),
        "MEMORY_DATA_DIR": str(sandbox / "data"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(REPO / "src"),
        # 宿主机真 key 不许泄进测试判定
        "MEMORY_EMBEDDER_API_KEY": "",
        "MEMORY_LLM_API_KEY": "",
    }
    env.update(env_extra or {})
    argv = [PY, str(CLI), "setup"] + ([] if step == "all" else ["--step", step])
    proc = subprocess.run(
        argv,
        input=keys.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    out = (proc.stdout + b"\n---STDERR---\n" + proc.stderr).decode("utf-8", "replace")
    cfg = {}
    cfg_path = sandbox / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cfg = {}
    return proc.returncode, out, cfg


def mkcfg(sb: Path, emb_base: str, llm_base: str | None = None, mode: str = "lite") -> None:
    """预写一份「已配置到假端点」的 config，模拟第二次进设置。"""
    cfg = {
        "setupComplete": True, "mode": mode,
        "embedding": {
            "preset": "custom", "provider": "openai", "baseUrl": emb_base,
            "model": "test-emb-1", "dims": 8, "apiKey": "", "keyEnv": "MEMORY_EMBEDDER_API_KEY",
        },
    }
    if llm_base:
        cfg["llm"] = {
            "preset": "custom", "baseUrl": llm_base, "model": "test-llm-1",
            "apiKey": "", "keyEnv": "MEMORY_LLM_API_KEY",
        }
    sb.joinpath("config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------
def scenario_first_use(base: str) -> None:
    """A. 首次设置：config 不存在 → 走通整个管道（自定义 + 环境变量 key）。"""
    print("\nA. 首次设置（config 不存在）")
    sb = Path(tempfile.mkdtemp(prefix="hymem-uiA-"))
    m = len(SEEN)
    # config 不存在时向导会先 _fill_from_env 预填（qwen-flash+env key），所以：
    # 4=自定义 → baseUrl → 模型 → 追问 key（预填被 _pick_key 判定不可继承）→ KEY1
    # → 三选一(回车=就用这把) → 探针 200 → 覆盖预填配置(y) → 维度变了选「先不重建」(2)
    # → 写入(y)
    rc, out, cfg = drive("embedding",
                         f"4\n{base}\ntest-emb-a\n{EMB_KEY1}\n\ny\n2\ny\n", sb,
                         {"MEMORY_EMBEDDER_API_KEY": EMB_KEY1})
    req = SEEN[m:]
    # 自检可能对预填 LLM 缺 key 报 WARN（rc=1），只要配置写入就算走通
    check("A1 首次设置完成并写入(rc∈{0,1})", rc in (0, 1) and cfg.get("embedding", {}).get("model") == "test-emb-a",
          f"rc={rc}\n{out[-500:]}")
    check("A2 baseUrl/model 落盘为新端点", cfg.get("embedding", {}).get("baseUrl") == base
          and cfg.get("embedding", {}).get("model") == "test-emb-a",
          json.dumps(cfg.get("embedding", {}), ensure_ascii=False))
    check("A3 手填 key 固化进 config、keyEnv 清空",
          cfg.get("embedding", {}).get("apiKey") == EMB_KEY1
          and cfg.get("embedding", {}).get("keyEnv") == "",
          json.dumps(cfg.get("embedding", {}), ensure_ascii=False))
    check("A4 探针发出且带环境变量 key", any(r["auth_ok"] and r["path"].endswith("/embeddings") for r in req),
          json.dumps(req, ensure_ascii=False))
    check("A5 预填旧值被手填覆盖时弹「覆盖旧配置」", "覆盖旧配置" in out, out[-300:])
    check("A6 三选一密钥环节出现", "密钥怎么用" in out, out[-800:])


def scenario_second_same(base: str) -> None:
    """B. 二次进设置、选当前项（回车=自定义）→ 确认明示来源、继承不落盘、无变化不问覆盖。"""
    print("\nB. 二次进设置 · 选当前项（信息不变）")
    sb = Path(tempfile.mkdtemp(prefix="hymem-uiB-"))
    mkcfg(sb, base)
    m = len(SEEN)
    # 回车=自定义 → baseUrl 回车保留 → 模型回车保留 → 三选一(回车=存入配置文件，固化 env 值)
    # → 探针 200 → 固化让 keyEnv 有→空 = diff，弹覆盖(y) → 写入(y)
    rc, out, cfg = drive("embedding", "\n\n\n\ny\ny\n", sb,
                         {"MEMORY_EMBEDDER_API_KEY": EMB_KEY1})
    req = SEEN[m:]
    check("B1 干净退出(rc=0)", rc == 0, f"rc={rc}")
    check("B2 三件套确认出现且明示环境变量来源",
          ("密钥怎么用" in out) and ("$MEMORY_EMBEDDER_API_KEY" in out or "环境变量" in out),
          out[-800:])
    check("B3 探针用继承的 key 命中", any(r["auth_ok"] and r["path"].endswith("/embeddings") for r in req),
          json.dumps(req, ensure_ascii=False))
    check("B4 env 值被固化进 config（apiKey=KEY1、keyEnv 清空）",
          cfg.get("embedding", {}).get("apiKey") == EMB_KEY1
          and cfg.get("embedding", {}).get("keyEnv") == "",
          repr(cfg.get("embedding", {}).get("apiKey"))[:30])
    check("B5 固化改变 keyEnv 时弹「覆盖旧配置」（明示存储位置变化）", "覆盖旧配置" in out, out[-300:])


def scenario_custom_cross(base1: str, base2: str) -> None:
    """C. 二次进设置、自定义换 baseUrl（跨端点）→ 不继承旧 key、探针用新 key 命中。"""
    print("\nC. 二次进设置 · 自定义换 baseUrl（跨端点）")
    sb = Path(tempfile.mkdtemp(prefix="hymem-uiC-"))
    mkcfg(sb, base1)  # 旧端点 = 假端点1
    m = len(SEEN)
    # 4=自定义 → 新 baseUrl=假端点2 → 模型名回车保留 → 追问新 key（跨端点）→ KEY2
    # → 三选一(回车=就用这把) → 探针 200 → 覆盖旧配置(y) → 写入(y)
    rc, out, cfg = drive("embedding", f"4\n{base2}\n\n{EMB_KEY2}\n\ny\ny\n", sb,
                         {"MEMORY_EMBEDDER_API_KEY": EMB_KEY1})
    req = SEEN[m:]
    check("C1 旧端点未收到任何请求", not any(r["tag"] == "S1" for r in req),
          json.dumps(req, ensure_ascii=False))
    check("C2 新端点探针命中（新 key 被接受）",
          any(r["tag"] == "S2" and r["auth_ok"] and r["key"] == EMB_KEY2
              and r["path"].endswith("/embeddings") for r in req),
          json.dumps(req, ensure_ascii=False))
    check("C3 旧 key 从未打到新端点（S2 收到的都不是 KEY1）",
          not any(r["tag"] == "S2" and r["key"] == EMB_KEY1 for r in req),
          json.dumps(req, ensure_ascii=False))
    check("C4 baseUrl 已换成新端点", cfg.get("embedding", {}).get("baseUrl") == base2,
          repr(cfg.get("embedding", {}).get("baseUrl")))
    check("C5 有凭据变化时弹出「覆盖旧配置」", "覆盖旧配置" in out, out[-600:])
    check("C6 手填 key 落 apiKey（用户自己的选择）", cfg.get("embedding", {}).get("apiKey") == EMB_KEY2,
          repr(cfg.get("embedding", {}).get("apiKey")))


def scenario_switch_preset_abort(base: str) -> None:
    """D. 二次进设置、换预设端点 → 告知旧 key 不能复用；EOF 中止时不落盘。"""
    print("\nD. 二次进设置 · 换预设端点（EOF 中止校验）")
    sb = Path(tempfile.mkdtemp(prefix="hymem-uiD-"))
    mkcfg(sb, base)
    old_cfg_text = (sb / "config.json").read_text(encoding="utf-8")
    # 2=GLM（跨端点）→ 追问新 key → EOF（管道喂完）→ Aborted
    rc, out, cfg = drive("embedding", "2\n", sb, {"MEMORY_EMBEDDER_API_KEY": EMB_KEY1})
    check("D1 明确告知旧密钥不能复用", ("旧密钥不能复用" in out) or ("必须填新的" in out), out[-500:])
    check("D2 EOF 中止后 config 未被改动", (sb / "config.json").read_text(encoding="utf-8") == old_cfg_text,
          "config 被改写了")
    check("D3 输出含取消提示", ("已取消" in out) or ("配置未改动" in out), out[-300:])


def scenario_llm(base: str) -> None:
    """E. LLM 同型：二次选当前项（继承明示）+ 自定义跨端点（不继承旧 key）。"""
    print("\nE. LLM · 二次选当前项 / 自定义跨端点")
    # E1: 二次选当前项
    sb = Path(tempfile.mkdtemp(prefix="hymem-uiE-"))
    mkcfg(sb, base, llm_base=base, mode="pro")
    m = len(SEEN)
    rc, out, cfg = drive("llm", "\n\n\n\ny\n", sb, {"MEMORY_LLM_API_KEY": LLM_KEY2})
    req = SEEN[m:]
    check("E1 LLM 三选一密钥环节出现", "密钥怎么用" in out, out[-600:])
    check("E2 LLM 探针用环境变量 key 命中",
          any(r["auth_ok"] and r["path"].endswith("/chat/completions") for r in req),
          json.dumps(req, ensure_ascii=False))
    # E2: 自定义换 baseUrl（当前配置指向假端点2，验证切换路径）
    sb2 = Path(tempfile.mkdtemp(prefix="hymem-uiE2-"))
    mkcfg(sb2, "https://old-llm.example.com/v1", llm_base="https://old-llm.example.com/v1", mode="pro")
    m2 = len(SEEN)
    rc2, out2, cfg2 = drive("llm", f"4\n{base}\n\n{LLM_KEY2}\n\ny\ny\n", sb2,
                            {"MEMORY_LLM_API_KEY": LLM_KEY1})
    req2 = SEEN[m2:]
    check("E3 LLM 旧 key（KEY1）从未打到新端点",
          not any(r["key"] == LLM_KEY1 for r in req2),
          json.dumps(req2, ensure_ascii=False))
    check("E4 LLM baseUrl 已切换", cfg2.get("llm", {}).get("baseUrl") == base,
          repr(cfg2.get("llm", {}).get("baseUrl")))
    check("E5 LLM 手填 key 落 apiKey", cfg2.get("llm", {}).get("apiKey") == LLM_KEY2,
          repr(cfg2.get("llm", {}).get("apiKey")))


def scenario_key_rotate(base: str) -> None:
    """F. 同端点换新 key（用户 2026-10-29 反馈原点）：三选一显式选「换一把 key」。"""
    print("\nF. 同端点 · 三选一显式换 key（key 轮换）")
    sb = Path(tempfile.mkdtemp(prefix="hymem-uiF-"))
    mkcfg(sb, base)  # 当前 keyEnv=MEMORY_EMBEDDER_API_KEY，环境里是旧 KEY1
    m = len(SEEN)
    # 回车=自定义 → baseUrl/模型回车保留 → 三选一选 2（换一把 key）→ 填轮换 key
    # → 回到三选一(回车=就用新填的) → 探针 200 → apiKey 变了有 diff 问覆盖(y) → 写入(y)
    rc, out, cfg = drive("embedding", f"\n\n\n2\n{EMB_ROT}\n\ny\ny\n", sb,
                         {"MEMORY_EMBEDDER_API_KEY": EMB_KEY1})
    req = SEEN[m:]
    check("F1 三选一菜单出现", "密钥怎么用" in out, out[-600:])
    check("F2 换 key 后探针用新 key", any(r["key"] == EMB_ROT and r["auth_ok"] for r in req),
          json.dumps(req, ensure_ascii=False))
    check("F3 旧 key 没被发出去", not any(r["key"] == EMB_KEY1 for r in req),
          json.dumps(req, ensure_ascii=False))
    check("F4 新 key 落 apiKey", cfg.get("embedding", {}).get("apiKey") == EMB_ROT,
          repr(cfg.get("embedding", {}).get("apiKey")))


def scenario_settings_menu(base1: str) -> None:
    """G. setup 无参 = 设置菜单：选一项改一项改完回菜单，key 固化进 config。"""
    print("\nG. 设置菜单 · key 固化")
    sb = Path(tempfile.mkdtemp(prefix="hymem-uiG-"))
    mkcfg(sb, base1)
    m = len(SEEN)
    # 1(向量) → 回车(自定义) → baseUrl/模型回车 → 三选一回车(存文件) → 探针
    # → 覆盖(y) → 写入(y) → 回菜单 → 8(退出)
    rc, out, cfg = drive("all", f"1\n\n\n\n\ny\ny\n8\n", sb,
                         {"MEMORY_EMBEDDER_API_KEY": EMB_KEY1})
    req = SEEN[m:]
    check("G1 设置菜单出现（每项带当前值）", "设置（选一项改一项" in out and "向量模型 · test-emb-1" in out, out[:400])
    check("G2 key 固化进 config（apiKey=env 值、keyEnv 清空）",
          cfg.get("embedding", {}).get("apiKey") == EMB_KEY1
          and cfg.get("embedding", {}).get("keyEnv") == "",
          json.dumps(cfg.get("embedding", {}), ensure_ascii=False)[:120])
    check("G3 探针在固化后命中", any(r["auth_ok"] for r in req if r["path"].endswith("/embeddings")),
          json.dumps(req, ensure_ascii=False)[:120])
    check("G4 改完回菜单可继续", "完成，退出设置" in out, out[-200:])


def scenario_pro_linkage(base: str) -> None:
    """H. pro 联动：mode 选 pro 且 LLM 不可用 → 同流强制进 LLM 设置。"""
    print("\nH. pro 联动 LLM")
    sb = Path(tempfile.mkdtemp(prefix="hymem-uiH-"))
    (sb / "config.json").write_text(json.dumps({
        "setupComplete": True, "mode": "lite",
        "embedding": {"preset": "custom", "provider": "openai", "baseUrl": base,
                      "model": "test-emb-1", "dims": 8, "apiKey": "sk-any", "keyEnv": ""},
    }, ensure_ascii=False), encoding="utf-8")
    m = len(SEEN)
    # mode 选 2(pro) → 联动警告 → LLM:4(自定义) → baseUrl → 模型 → key 追问 → KEY2
    # → 三选一回车(存文件) → 探针 → 覆盖(y) → 写入(y)
    # 预填段 keyEnv=YSJX_API_KEY 且本机真实存在——沙箱必须清掉，否则
    # _pick_key 会判「LLM 已配好」跳过联动（G/H 语义测的是手配路径）
    rc, out, cfg = drive("mode", f"2\n4\n{base}\ntest-llm-1\n{LLM_KEY2}\n\ny\ny\n", sb,
                         {"MEMORY_LLM_API_KEY": LLM_KEY2, "YSJX_API_KEY": ""})
    req = SEEN[m:]
    check("H1 选 pro 触发 LLM 联动引导", "需要 LLM 抽取事实" in out, out[-400:])
    check("H2 同流完成 LLM 配置并固化 key",
          cfg.get("llm", {}).get("model") == "test-llm-1"
          and cfg.get("llm", {}).get("apiKey") == LLM_KEY2,
          json.dumps(cfg.get("llm", {}), ensure_ascii=False)[:120])
    check("H3 mode 已存 pro", cfg.get("mode") == "pro", repr(cfg.get("mode")))


def scenario_fail_menu(base: str) -> None:
    """I. 验证失败菜单按场景分流：单步无 save（只能重配或取消），首次向导有 save（离线出口）。"""
    print("\nI. 失败菜单 · 测试不过不放行")
    # I1: 单步（已配置）→ 401 → 只有 retry/abort
    sb = Path(tempfile.mkdtemp(prefix="hymem-uiI1-"))
    (sb / "config.json").write_text(json.dumps({
        "setupComplete": True, "mode": "lite",
        "embedding": {"preset": "custom", "provider": "openai", "baseUrl": base,
                      "model": "test-emb-1", "dims": 8, "apiKey": "sk-wrong", "keyEnv": ""},
    }, ensure_ascii=False), encoding="utf-8")
    rc, out, cfg = drive("embedding", "\n\n\n\n", sb)
    check("I1 单步失败菜单出现", "验证失败" in out, out[-300:])
    check("I2 单步失败无「仍然保存」（测试不过不放行）", "仍然保存" not in out, out[-300:])
    # I3: 首次向导（无 config）→ 401 → 有 save（离线出口）
    sb2 = Path(tempfile.mkdtemp(prefix="hymem-uiI2-"))
    rc2, out2, _ = drive("all", f"\n4\n{base}\ntest-emb-x\nsk-bad\n\n", sb2,
                         env_extra={"YSJX_API_KEY": "", "MEMORY_LLM_API_KEY": ""})
    check("I3 首次向导失败菜单出现", "验证失败" in out2, out2[-300:])
    check("I4 首次向导保留 save（离线可存）", "仍然保存" in out2, out2[-300:])


def main() -> int:
    print("假端点启动中…")
    srv1, base1 = start_fake({EMB_KEY1, EMB_ROT}, LLM_KEY1, "S1")
    srv2, base2 = start_fake(EMB_KEY2, LLM_KEY2, "S2")
    print(f"假端点1: {base1}")
    print(f"假端点2: {base2}")
    try:
        scenario_first_use(base1)
        scenario_second_same(base1)
        scenario_custom_cross(base1, base2)
        scenario_switch_preset_abort(base1)
        scenario_llm(base2)
        scenario_key_rotate(base1)
        scenario_settings_menu(base1)
        scenario_pro_linkage(base2)
        scenario_fail_menu(base1)
    finally:
        srv1.shutdown(); srv1.server_close()
        srv2.shutdown(); srv2.server_close()

    n_pass = sum(1 for _, ok_, _ in results if ok_)
    n_all = len(results)
    print(f"\n{'全部通过' if n_pass == n_all else '存在失败'}（{n_pass}/{n_all}）")
    return 0 if n_pass == n_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
