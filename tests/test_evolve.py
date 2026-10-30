# -*- coding: utf-8 -*-
"""L6 认知层（ultra 专属）全量测试：模式感知 + evolve + 检索隔离。

来源：`.local-test/repro_evolve.py`（16/16）稳定后转正，补上 MCP 模式感知面。

覆盖：
  A 离线面：config evolve 段校验/展示、mode 三值校验、helptext 声明 ultra
  B CLI pro 面：evolve 被前置拦截（exit 1，不泄出英文 RuntimeError）、
    search 带 schema_available=false 与 schema_note、--no-schema 可关
  C MCP pro 面（真 stdio）：tools/list 不含 evolve、硬调 evolve 被拒、
    search_memory 返回 schema_available=false 且 hits 里无 l6_schema 混入
  D ultra 面（真模型）：写同主题素材 → evolve 跑通 → 返回结构齐全 →
    弱模型不产出 schema（idle 或 +0）→ search 的 schemas 字段可用

跑法：
    .venv/Scripts/python.exe tests/test_evolve.py [exe路径]
判定：全部 PASS。缺真实密钥时显式跳过（不产出假绿）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv" / "Scripts" / "python.exe")
sys.path.insert(0, str(REPO / "scripts"))
import _secrets  # noqa: E402  密钥只从环境变量取

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(bool(passed))
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  → {str(detail)[:150]}" if detail else ""))


class McpClient:
    """同步 stdio 客户端。stderr 必须落文件不是 PIPE：SDK 初始化刷大量日志，
    管道 64KB 写满后子进程阻塞在 stderr、我们在等 stdout → 死锁。"""

    def __init__(self, base: list[str], env: dict, stderr_path: Path | None = None):
        self._err = open(stderr_path or os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        self.p = subprocess.Popen(
            base, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._err, text=True, encoding="utf-8", env=env,
        )
        self._id = 0
        self.notifications: list[str] = []  # 服务端主动推的通知（无 id），按序收集

    def call(self, method: str, params: dict | None = None, timeout: float = 300) -> dict:
        self._id += 1
        req: dict = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        self.p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("MCP 进程已退出")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if "id" not in msg:
                self.notifications.append(msg.get("method") or "?")
                continue
            if msg.get("id") == self._id:
                return msg
        raise TimeoutError(f"{method} 超时")

    def notify(self, method: str, params: dict | None = None) -> None:
        req: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        self.p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def tool(self, name: str, args: dict) -> tuple[dict | None, bool, str]:
        resp = self.call("tools/call", {"name": name, "arguments": args})
        content = (resp or {}).get("result") or {}
        text = "".join(c.get("text", "") for c in content.get("content") or [])
        try:
            return json.loads(text), bool(content.get("isError")), text
        except Exception:  # noqa: BLE001
            return None, bool(content.get("isError")), text

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=15)
        except Exception:  # noqa: BLE001
            self.p.kill()
        try:
            self._err.close()
        except Exception:  # noqa: BLE001
            pass


# 同主题、细节各异——落在 L6 的可用区：太散不成簇，太像会被判重合并。
FACTS = [
    "hy-mem 的向量库用 Chroma 嵌入式，多进程必须由 serve 仲裁。",
    "hy-mem 的向量库 collection 按嵌入维度命名，当前是 agent_memories_1024。",
    "hy-mem 的嵌入密钥只放环境变量 MEMORY_EMBEDDER_API_KEY。",
    "hy-mem 的写锁只包 SDK 标准提交段，LLM 抽取在锁外并行。",
    "hy-mem 的数据目录 data/ 不入 git，记忆库和日志都在本机。",
    "hy-mem 的 config.json 里 apiKey 恒为空，只留 keyEnv 记变量名。",
    "hy-mem 的身份切换不删记忆，旧身份数据只是看不到。",
]


def make_sandbox(mode: str, base_cfg: dict) -> Path:
    sb = Path(tempfile.mkdtemp(prefix=f"hymem-evolve-{mode}-"))
    (sb / "data").mkdir()
    cfg = _secrets.sandbox_config(base_cfg)
    cfg["mode"] = mode
    cfg.setdefault("evolve", {})
    cfg["evolve"].update({"model": "", "maxTokens": 8000,
                          "schemaLimit": 3, "schemaMinScore": 0.4})
    (sb / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return sb


def main() -> int:
    exe = sys.argv[1] if len(sys.argv) > 1 else None
    if not _secrets.have_all():
        print(f"跳过 test_evolve：缺少真实密钥（{_secrets.describe()}）")
        print("  本测试会真实调用嵌入/抽取模型跑一次 evolve，请先配置环境变量。")
        return 0

    base_cfg = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
    sb_pro = make_sandbox("pro", base_cfg)
    sb_ultra = make_sandbox("ultra", base_cfg)
    cli_base = [exe] if exe else [PY, "-m", "hymem"]
    mcp_base = [exe, "mcp"] if exe else [PY, "-m", "hymem", "mcp"]
    print("pro 沙箱：", sb_pro)
    print("ultra 沙箱：", sb_ultra)

    def env_of(sb: Path) -> dict:
        e = dict(os.environ, HY_MEM_CONFIG=str(sb / "config.json"),
                 MEMORY_DATA_DIR=str(sb / "data"), NO_COLOR="1")
        e.pop("HY_MEM_USER_ID", None)
        return e

    env_pro = env_of(sb_pro)
    env_ultra = env_of(sb_ultra)

    def run(env: dict, args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
        return subprocess.run(cli_base + args, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)

    def jload(p: subprocess.CompletedProcess) -> dict:
        try:
            return json.loads([ln for ln in (p.stdout or "").splitlines() if ln.startswith("{")][-1])
        except Exception:  # noqa: BLE001
            return {}

    # ================= A 离线面 =================
    print("\n== A 离线面（配置 / 文案 / 模式校验）==")
    v = run(env_pro, ["--json", "config", "validate"]).stdout
    names = [c.get("name") for c in (json.loads(v).get("checks") or [])] if v.strip().startswith("{") else []
    for key in ("evolve.model", "evolve.maxTokens", "evolve.schemaLimit", "evolve.schemaMinScore"):
        check(f"A1 validate 含 {key}", key in names, "")

    show_pro = jload(run(env_pro, ["--json", "config"]))
    check("A2 pro 下 evolve.enabled=false", show_pro.get("evolve", {}).get("enabled") is False,
          show_pro.get("evolve"))
    check("A2 pro 下 effectiveModel 回落 llm.model",
          show_pro.get("evolve", {}).get("effectiveModel") == show_pro.get("llm", {}).get("model"), "")

    show_ultra = jload(run(env_ultra, ["--json", "config"]))
    check("A3 ultra 下 evolve.enabled=true", show_ultra.get("evolve", {}).get("enabled") is True, "")

    p = run(env_pro, ["config", "set", "mode=ultraa"])
    check("A4 非法 mode 被拒且 exit 1", p.returncode == 1, f"rc={p.returncode}")
    check("A4 提示列出三个合法值", "lite / pro / ultra" in (p.stdout + p.stderr), "")

    h = run(env_ultra, ["help", "evolve"])
    htxt = h.stdout + h.stderr
    check("A5 helptext 声明仅 ultra", "仅 ultra 可用" in htxt, "")
    check("A5 helptext 说明素材只用一次", "只会被归纳一次" in htxt, "")
    hs = run(env_ultra, ["help", "search"]).stdout
    check("A5 search helptext 说明 schemas 独立成段", "独立成段" in hs, "")

    # ================= B CLI pro 面 =================
    print("\n== B CLI pro 面（拦截 / 检索字段）==")
    p = run(env_pro, ["evolve"])
    out = (p.stdout or "") + (p.stderr or "")
    check("B1 pro 下 evolve exit 1", p.returncode == 1, f"rc={p.returncode}")
    check("B1 提示给出切换命令", "config set mode=ultra" in out, "")
    check("B1 不泄出 SDK 英文异常", "requires mode='ultra'" not in out and "RuntimeError" not in out, "")

    s = jload(run(env_pro, ["--json", "search", "随便问点什么", "-n", "3"]))
    check("B2 pro 下 schemas 为空", (s.get("schemas") or []) == [], s.get("schemas"))
    check("B2 pro 下 schema_available=false", s.get("schema_available") is False, "")
    check("B2 pro 下带 schema_note 说明仅 ultra", "ultra" in str(s.get("schema_note") or ""), "")

    s2 = jload(run(env_pro, ["--json", "search", "随便问点什么", "--no-schema"]))
    check("B3 --no-schema 关闭且不再解释", (s2.get("schemas") or []) == []
          and not s2.get("schema_note"), s2.get("schema_note"))

    # ================= C MCP pro 面 =================
    print("\n== C MCP pro 面（工具可见性 / 拒绝语义）==")
    mcp = McpClient(mcp_base, env_pro, stderr_path=sb_pro / "mcp_stderr.log")
    try:
        mcp.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "test_evolve", "version": "1"}})
        mcp.notify("notifications/initialized")
        tools = {t["name"]: t for t in mcp.call("tools/list").get("result", {}).get("tools", [])}
        check("C1 pro 下 tools/list 不含 evolve", "evolve" not in tools, sorted(tools))
        check("C1 search_memory 有 schema 参数",
              "schema" in (tools.get("search_memory", {}).get("inputSchema", {})
                           .get("properties", {})), "")
        check("C1 search_memory 有 schema_min_score 参数",
              "schema_min_score" in (tools.get("search_memory", {}).get("inputSchema", {})
                                     .get("properties", {})), "")

        data, is_err, text = mcp.tool("evolve", {})
        check("C2 硬调 evolve 被拒（isError）", is_err, text[:100])
        check("C2 拒绝理由说明仅 ultra 并可切换", "ultra" in text, text[:120])

        vp, _, vtext = mcp.tool("get_version", {})
        check("C3 get_version 带 memoryMode", (vp or {}).get("memoryMode") == "pro", vtext[:80])
        check("C3 capabilities.evolve=false", (vp or {}).get("capabilities", {}).get("evolve") is False, "")
        check("C3 distMode 与 memoryMode 分离", "distMode" in (vp or {}), "")

        sp, _, stext = mcp.tool("search_memory", {"query": "随便问点什么", "limit": 3})
        check("C3 search_memory schema_available=false", (sp or {}).get("schema_available") is False, "")
        check("C3 hits 里没有 l6_schema 混入",
              not any((h.get("layer") == "l6_schema") for h in ((sp or {}).get("hits") or [])), "")

        # C4 模式热切换：外部 CLI 改 config.json，服务端下一条消息时发现并发通知
        init = mcp.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": "test_evolve-c4", "version": "1"}})
        check("C4 initialize 声明 listChanged 能力",
              (init.get("result", {}).get("capabilities", {}).get("tools", {}) or {}).get("listChanged") is True, "")
        mcp.call("ping")  # 记录初始 mode（pro），不触发通知
        mcp.notifications.clear()

        cfg_p = sb_pro / "config.json"
        cfg_obj = json.loads(cfg_p.read_text(encoding="utf-8"))
        cfg_obj["mode"] = "ultra"
        cfg_p.write_text(json.dumps(cfg_obj, ensure_ascii=False), encoding="utf-8")
        mcp.call("ping")
        check("C4 切 ultra 后发 tools/list_changed",
              mcp.notifications == ["notifications/tools/list_changed"], mcp.notifications)
        tools2 = {t["name"] for t in mcp.call("tools/list").get("result", {}).get("tools", [])}
        check("C4 重拉后含 evolve", "evolve" in tools2, sorted(tools2))

        cfg_obj["mode"] = "pro"
        cfg_p.write_text(json.dumps(cfg_obj, ensure_ascii=False), encoding="utf-8")
        mcp.call("ping")
        mcp.call("tools/list")
        check("C4 切回 pro 再发一次通知且列表复原",
              mcp.notifications == ["notifications/tools/list_changed", "notifications/tools/list_changed"],
              mcp.notifications)
        tools3 = {t["name"] for t in mcp.call("tools/list").get("result", {}).get("tools", [])}
        check("C4 切回后不含 evolve", "evolve" not in tools3, sorted(tools3))
    finally:
        mcp.close()

    # ================= E L6 状态可见性（离线桩，R2/R3） =================
    # 根因复盘（docs/specs/2026-10-30-l6-schema-empty/）：图库未就绪时 vector_search
    # 静默返回 []，「有 schema 但被阈值滤空」与「库是空的」返回完全一致——调用方
    # 无从分辨。E 节用桩 client 把四种 state 逐一钉住。
    print("\n== E L6 状态可见性（离线桩）==")
    import types

    from hymem.core.searchops import fetch_l6_schemas, schema_state_meta

    class _StubES:
        async def embed(self, _q):
            return [0.1] * 8

    class _StubGS:
        def __init__(self, available=True, rows=None, nodes=None):
            self._available = available
            self._rows = rows if rows is not None else []
            self._nodes = nodes if nodes is not None else []

        async def vector_search(self, **_kw):
            return self._rows

        async def get_all_nodes(self, **_kw):
            return self._nodes

    class _StubClient:
        def __init__(self, gs):
            self._mode = "ultra"
            self._graph_store = gs
            self._embed_service = _StubES()

    def _node(emb):
        return types.SimpleNamespace(embedding=emb, content="x")

    _hit = [{"node_id": "n1", "content": "c", "score": 0.6}]
    _two_nodes = [_node([0.1] * 8), _node([0.2] * 8)]
    _no_emb_nodes = [_node(None), _node(None)]

    rows, st = fetch_l6_schemas(_StubClient(_StubGS(available=False)), "q", user_id="u")
    check("E1 图库未就绪 → graph_unavailable（不再谎报可用）",
          rows == [] and st == "graph_unavailable", st)
    rows, st = fetch_l6_schemas(_StubClient(_StubGS(nodes=_two_nodes)), "q", user_id="u")
    check("E2 有料被阈值滤空 → empty:2",
          rows == [] and st == "empty:2", st)
    rows, st = fetch_l6_schemas(_StubClient(_StubGS(nodes=_no_emb_nodes)), "q", user_id="u")
    check("E3 全缺 embedding → no_embedding:2",
          rows == [] and st == "no_embedding:2", st)
    rows, st = fetch_l6_schemas(_StubClient(_StubGS(rows=_hit)), "q", user_id="u")
    check("E4 正常命中 → state 空且行数保持",
          st == "" and len(rows) == 1 and rows[0]["content"] == "c", f"{st} {rows}")

    _cases = {
        "": (True, ""), "off": (False, ""),
        "disabled": (False, "ultra"), "graph_unavailable": (False, "kuzu"),
        "empty:2": (True, "2 条"), "no_embedding:3": (False, "3 条"),
        "error: X": (False, "X"),
    }
    _bad = {k: v for k, v in
            ((k, schema_state_meta(k, 0.25)) for k in _cases)
            if v[0] != _cases[k][0] or _cases[k][1] not in v[1]}
    check("E5 schema_state_meta 七种 state 映射口径", not _bad, str(_bad))

    from hymem.core import config as _cfg_e

    check("E6 schemaMinScore 默认 0.25（中文 query 对英文 schema 实测口径）",
          float((_cfg_e.DEFAULTS.get("evolve") or {}).get("schemaMinScore") or 0) == 0.25,
          repr((_cfg_e.DEFAULTS.get("evolve") or {}).get("schemaMinScore")))

    # ================= D ultra 面 =================
    print("\n== D ultra 面（真模型：写入 → evolve → 检索）==")
    uid = "l6_test"
    n = 0
    t0 = time.time()
    for f in FACTS:
        r = jload(run(env_ultra, ["--json", "--user", uid, "add", f], timeout=300))
        if r.get("success") or r.get("ok"):
            n += 1
    check("D1 素材写入", n >= 4, f"{n}/{len(FACTS)} 条，{time.time() - t0:.0f}s")

    ev = jload(run(env_ultra, ["--json", "--user", uid, "evolve", "--batches", "1"], timeout=900))
    check("D2 evolve 跑通", ev.get("ok") is True, str(ev.get("error"))[:100])
    check("D2 返回结构齐全（批次/token/schema 计数）",
          all(k in ev for k in ("batches_completed", "prompt_tokens", "completion_tokens",
                                "schemas_before", "schemas_after")), sorted(ev))
    check("D2 记录了使用的模型", bool(ev.get("model")), ev.get("model"))
    # 7 条素材多半够不到聚簇门槛（L6 要「同主题且细节各异、≥4 条不被判重合并」），
    # 这时 SDK 直接返回 no_clusters/facts，LLM 根本不会被调用。两种情况要分开断言，
    # 否则「一条没产出」既可能是没料也可能是模型空转，混在一起就等于没测。
    if int(ev.get("batches_completed") or 0) == 0:
        check("D3 未成簇时给出明确原因（no_clusters/no_facts）",
              ev.get("continuation_reason") in ("no_clusters", "no_facts"),
              f"reason={ev.get('continuation_reason')} fresh_left={ev.get('fresh_facts_remaining')}")
    else:
        check("D3 弱模型不产出 schema（空转或判重丢弃）",
              bool(ev.get("idle")) or int(ev.get("schemas_added") or 0) == 0,
              f"idle={ev.get('idle')} +{ev.get('schemas_added')} completion={ev.get('completion_tokens')}")

    s3 = jload(run(env_ultra, ["--json", "--user", uid, "search", "hy-mem 的向量库怎么部署", "-n", "3"]))
    check("D4 ultra 下 schema_available=true", s3.get("schema_available") is True, "")
    check("D4 schemas 是列表且元素带 content/score",
          isinstance(s3.get("schemas"), list)
          and all("content" in x and "score" in x for x in (s3.get("schemas") or [])),
          str(s3.get("schemas"))[:100])

    # ================= 汇总 =================
    bad = len([r for r in results if not r])
    print()
    if bad:
        print(f"存在失败：{bad}/{len(results)}")
        print(f"沙箱保留：{sb_pro} / {sb_ultra}")
        return 1
    print(f"全部通过（{len(results)}/{len(results)}），总耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
