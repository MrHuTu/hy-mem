# -*- coding: utf-8 -*-
"""scene（working/coding）全量测试：S1–S6 的验收点合并成一套（沙箱隔离）。

来源：`.local-test/` 的四个 repro（repro_scene_add 7/7、repro_scene_mcp 15/15、
repro_scene_chain 15/15、repro_scene_profile 11/11）稳定后转正合并，共享一个沙箱，
避免每套各起一次库。

覆盖：
  A 离线面：tools/list schema、参数拦截、config chainRepo 校验、helptext 同步
  B CLI 编码链路：E_NO_TRACE 拦截 → 两轮同文件写入 → 独立库生成 → 检索分路 →
    diff 链两版 → 默认不注入 → scope 不匹配零命中
  C MCP 面（真 stdio）：参数拦截四类 → 真轨迹写入 → scene 检索 → chain_block
  D chat 与画像：chat 缺省仍异步 → chat 拒 messages → profile 读 L0

跑法：
    .venv/Scripts/python.exe tests/test_scene.py [exe路径]
判定：全部检查 PASS。缺真实密钥时显式跳过（不产出假绿）。
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
    """同步 stdio 客户端：一条请求一行 JSON，按 id 匹配响应。

    stderr 必须落文件而不是 PIPE：SDK 初始化会刷大量 INFO 日志，管道缓冲区
    （64KB）写满后子进程会阻塞在写 stderr 上，我们又在等 stdout → 死锁。
    """

    def __init__(self, base: list[str], env: dict, stderr_path: Path | None = None):
        self._err = open(stderr_path or os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        self.p = subprocess.Popen(
            base, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._err, text=True, encoding="utf-8", env=env,
        )
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 300) -> dict:
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
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
            if msg.get("id") == self._id:
                return msg
        raise TimeoutError(f"{method} 超时")

    def notify(self, method: str, params: dict | None = None) -> None:
        """发通知（无 id）：服务端不返回响应，不能等 —— 等就是死锁。"""
        req: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            req["params"] = params
        self.p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def tool(self, name: str, args: dict) -> tuple[dict | None, bool, str]:
        """调工具，返回 (解析后的 JSON, isError, 原文)。"""
        resp = self.call("tools/call", {"name": name, "arguments": args})
        content = (resp or {}).get("result") or {}
        text = "".join(c.get("text", "") for c in content.get("content") or [])
        try:
            obj = json.loads(text)
        except Exception:  # noqa: BLE001
            obj = None
        return obj, bool(content.get("isError")), text

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.p.kill()
        try:
            self._err.close()
        except Exception:  # noqa: BLE001
            pass


WS_CLI = "github.com/test/scene-cli"
WS_MCP = "github.com/test/scene-mcp"

DIFF_1 = """diff --git a/src/client.py b/src/client.py
--- a/src/client.py
+++ b/src/client.py
@@ -10,2 +10,2 @@ def request(timeout=1):
-TIMEOUT = 1
+TIMEOUT = 30"""
DIFF_2 = """diff --git a/src/client.py b/src/client.py
--- a/src/client.py
+++ b/src/client.py
@@ -10,2 +10,2 @@ def request(timeout=30):
-TIMEOUT = 30
+TIMEOUT = 60"""


def trace(diff: str | None, said: str, tool_result: str = "") -> list[dict]:
    """构造一条 coding 轨迹。diff 放进 tool content —— 链层只从文本读 diff。"""
    return [
        {"role": "user", "content": said},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "apply_patch",
                         "arguments": json.dumps({"path": "src/client.py"})},
        }]},
        {"role": "tool", "tool_call_id": "c1", "name": "git_diff",
         "content": diff or tool_result or "patched"},
        {"role": "assistant", "content": "done"},
    ]


def main() -> int:
    exe = sys.argv[1] if len(sys.argv) > 1 else None
    if not _secrets.have_all():
        # 编码链路要真的调抽取模型 + 嵌入模型。缺密钥时的「全绿」是假象。
        print(f"跳过 test_scene：缺少真实密钥（{_secrets.describe()}）")
        print("  本测试会真实调用向量模型与抽取模型，请先配置环境变量。")
        return 0

    sb = Path(tempfile.mkdtemp(prefix="hymem-scene-"))
    (sb / "data").mkdir()
    base_cfg = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
    # 沙箱必须同时固定 dataDir 与 mode：
    #   * dataDir —— config 里的绝对路径不会因为设了 MEMORY_DATA_DIR 就失效？
    #     会失效（env 优先），但显式写死更保险，避免误触真实库；
    #   * mode=pro —— 本套同时起 MCP 与 CLI 两个进程，ultra 下 kuzu 图库独占，
    #     后起的那个必定 `GraphStore (kuzu) init failed`（2026-10-30 实测 D2/D3 四连红）。
    #     scene 编码链与 mode 无关，固定 pro 才能稳定复现。
    base_cfg["storage"]["dataDir"] = str(sb / "data")
    base_cfg["mode"] = "pro"
    (sb / "config.json").write_text(
        json.dumps(_secrets.sandbox_config(base_cfg), ensure_ascii=False), encoding="utf-8"
    )
    env = dict(os.environ, HY_MEM_CONFIG=str(sb / "config.json"),
               MEMORY_DATA_DIR=str(sb / "data"), NO_COLOR="1")
    for k in ("MEMORY_CODING_CHAIN_REPO_ROOT", "MEMORY_CODING_CHAIN_BLOCK_ENABLED"):
        env.pop(k, None)
    cli_base = [exe] if exe else [PY, "-m", "hymem"]
    mcp_base = [exe, "mcp"] if exe else [PY, "-m", "hymem", "mcp"]
    t0 = time.time()
    print("沙箱：", sb)
    print("被测 CLI：", " ".join(cli_base))

    def run(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run(cli_base + args, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)

    def jload(p: subprocess.CompletedProcess) -> dict:
        try:
            return json.loads([ln for ln in (p.stdout or "").splitlines() if ln.startswith("{")][-1])
        except Exception:  # noqa: BLE001
            return {}

    # ================= A 离线面 =================
    print("\n== A 离线面（schema / 参数 / 配置 / 文案）==")
    mcp = McpClient(mcp_base, env, stderr_path=sb / "mcp_stderr.log")
    mcp.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "test_scene", "version": "1"}})
    mcp.notify("notifications/initialized")
    tools = {t["name"]: t for t in mcp.call("tools/list").get("result", {}).get("tools", [])}
    add_props = tools.get("add_memory", {}).get("inputSchema", {}).get("properties", {})
    srch_props = tools.get("search_memory", {}).get("inputSchema", {}).get("properties", {})
    check("A1 add_memory 有 scene 三件套 + messages",
          all(k in add_props for k in ("messages", "scene", "workspace_id", "branch")),
          list(add_props))
    check("A2 search_memory 有 scene/scope/sources/chain_block",
          all(k in srch_props for k in ("scene", "workspace_id", "branch",
                                        "include_sources", "exclude_sources", "chain_block")),
          list(srch_props))

    p = run(["help", "add"])
    h = p.stdout or ""
    check("A3 help add 写清 scene 管线与 E_NO_TRACE",
          "--scene" in h and "--trace" in h and "E_NO_TRACE" in h, h[:80])
    check("A3 help add 写清 diff 链靠 tool 消息里的 diff", "git diff" in h)
    p = run(["help", "search"])
    h = p.stdout or ""
    check("A4 help search 写清两库互不可见", "--scene" in h and "互不" in h, h[:80])
    check("A4 help search 写清 --chain-block", "--chain-block" in h)

    bad_path = str(sb / "not-a-repo")
    p = run(["--json", "config", "set", f"coding.chainRepo={bad_path}"])
    check("A5 不存在的 chainRepo 被拒",
          p.returncode != 0 and "路径不存在" in ((p.stdout or "") + (p.stderr or "")),
          (p.stdout or "")[-100:])
    (sb / "not-a-repo").mkdir()
    p = run(["--json", "config", "set", f"coding.chainRepo={bad_path}"])
    check("A5 非 Git 目录被拒",
          p.returncode != 0 and "不是 Git 仓库" in ((p.stdout or "") + (p.stderr or "")))
    p = run(["--json", "config", "set", f"coding.chainRepo={REPO}"])
    check("A6 合法仓库根写入成功", p.returncode == 0 and jload(p).get("ok") is True)
    p = run(["--json", "config", "validate"])
    checks = {c["name"]: c["pass"] for c in (jload(p).get("checks") or [])}
    check("A6 validate chainRepo PASS", checks.get("coding.chainRepo") is True)
    p = run(["config"])
    check("A6 config show 展示 diff 链仓库", "diff 链仓库" in (p.stdout or ""))

    # ================= C 前半：MCP 参数拦截（纯参数，不联网）=================
    print("\n== C MCP 参数拦截 ==")
    obj, is_err, text = mcp.tool("add_memory", {
        "scene": "coding", "messages": [{"role": "user", "content": "今天天气不错"}]})
    check("C1 无轨迹被拒（不许静默回退 chat）",
          is_err and "静默" in text and "零写入" in text, text[:120])
    obj, is_err, text = mcp.tool("add_memory", {
        "scene": "coding", "text": "x", "messages": trace(None, "改超时")})
    check("C2 text 与 messages 二选一", is_err and "二选一" in text, text[:100])
    obj, is_err, text = mcp.tool("add_memory", {
        "scene": "coding", "messages": trace(None, "改超时"), "branch": "master"})
    check("C3 branch 缺 workspace 被拒", is_err and "workspace_id" in text, text[:100])
    obj, is_err, text = mcp.tool("add_memory", {"scene": "chat", "messages": trace(None, "x")})
    check("C4 scene=chat 传 messages 被拒", is_err, text[:100])
    obj, is_err, text = mcp.tool("search_memory", {"query": "x", "chain_block": True})
    check("C4 chat 检索传 chain_block 被拒", is_err and "coding" in text, text[:100])

    # ================= B CLI 编码链路（真模型）=================
    print("\n== B CLI 编码链路（两轮写入 + 检索 + diff 链）==")
    plain = sb / "plain.json"
    plain.write_text(json.dumps([{"role": "user", "content": "今天天气不错"}],
                                ensure_ascii=False), encoding="utf-8")
    t1 = sb / "t1.json"
    t2 = sb / "t2.json"
    t1.write_text(json.dumps(trace(DIFF_1, "raise request timeout from 1s to 30s in src/client.py"),
                             ensure_ascii=False), encoding="utf-8")
    t2.write_text(json.dumps(trace(DIFF_2, "later bump request timeout from 30s to 60s"),
                             ensure_ascii=False), encoding="utf-8")

    p = run(["--json", "add", "--scene", "coding", "--trace", str(plain)])
    check("B1 无轨迹拒绝 E_NO_TRACE",
          '"E_NO_TRACE"' in (p.stdout or "") and p.returncode != 0, (p.stdout or "")[-110:])

    p = run(["--json", "add", "--scene", "coding", "--workspace", WS_CLI,
             "--branch", "master", "--trace", str(t1)])
    o1 = jload(p)
    check("B2 第一轮写入成功", o1.get("ok") is True, str(o1)[:110])
    check("B2 事实层恰 1 条（官方契约）", len(o1.get("fact_ids") or []) == 1, o1.get("fact_ids"))
    check("B2 链层识别到改动", (o1.get("chain_rounds") or 0) >= 1, o1.get("chain_rounds"))

    cv_db = sb / "data" / "data" / "coding_memory_cv.db"
    check("B3 独立库 coding_memory_cv.db 生成", cv_db.exists(), str(cv_db))
    if cv_db.exists():
        import sqlite3

        conn = sqlite3.connect(f"file:{cv_db}?mode=ro", uri=True)
        tabs = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        check("B3 facts 表存在", any("fact" in t.lower() for t in tabs), tabs[:6])

    p = run(["--json", "add", "--scene", "coding", "--workspace", WS_CLI,
             "--branch", "master", "--trace", str(t2)])
    o2 = jload(p)
    check("B4 第二轮写入成功", o2.get("ok") is True, str(o2)[:110])

    p = run(["--json", "search", "client 超时配置怎么改的", "--scene", "coding",
             "--workspace", WS_CLI, "--branch", "master", "-n", "5"])
    so = jload(p)
    facts = so.get("facts") or []
    ids = [f.get("fact_id") or f.get("id") for f in facts if isinstance(f, dict)]
    check("B5 scene 检索 facts 非空", len(facts) >= 1, str(facts)[:120])
    check("B5 命中刚写入的 fact",
          any(i in (o2.get("fact_ids") or []) for i in ids if i),
          f"命中={ids} 写入={o2.get('fact_ids')}")

    p = run(["--json", "search", "client 超时配置怎么改的", "--scene", "coding",
             "--workspace", WS_CLI, "--branch", "master", "--chain-block", "-n", "5"])
    cb = jload(p).get("chain_block") or ""
    check("B6 chain_block 非空", bool(cb.strip()), cb[:100])
    check("B6 链上是两版（第1版 + 第2版）", ("第1版" in cb) and ("第2版" in cb), cb[:200])
    check("B6 diff 含两轮改动值", ("TIMEOUT = 30" in cb) and ("TIMEOUT = 60" in cb), cb[:200])

    p = run(["--json", "search", "client 超时配置怎么改的", "--scene", "coding",
             "--workspace", WS_CLI, "--branch", "master", "-n", "5"])
    check("B7 不带开关不注入 chain_block", jload(p).get("chain_block") is None)

    p = run(["--json", "search", "client 超时配置怎么改的", "--scene", "coding",
             "--workspace", "github.com/test/other-repo", "-n", "5"])
    so = jload(p)
    check("B8 换 workspace 零命中", so.get("count") == 0, str(so)[:100])

    # ================= C 后半：MCP 真轨迹写入 + 检索 =================
    print("\n== C MCP 真轨迹写入 + 检索 ==")
    obj, is_err, text = mcp.tool("add_memory", {
        "scene": "coding", "messages": trace(None, "fix timeout in src/client.py by raising it to 30s",
                                             tool_result=DIFF_1),
        "workspace_id": WS_MCP, "branch": "master"})
    check("C5 MCP 写 coding 成功", not is_err and (obj or {}).get("ok") is True, text[:140])
    m_facts = (obj or {}).get("fact_ids") or []
    check("C5 fact_ids 恰 1 条", len(m_facts) == 1, m_facts)
    check("C5 返回注明同步落库", "同步" in ((obj or {}).get("note") or ""))

    obj, is_err, text = mcp.tool("search_memory", {
        "query": "search client 超时怎么修的", "scene": "coding",
        "workspace_id": WS_MCP, "branch": "master", "limit": 5})
    hit = (obj or {}).get("facts") or []
    check("C6 MCP scene 检索命中", len(hit) >= 1, str(hit)[:120])
    hit_ids = [h.get("fact_id") or h.get("id") for h in hit if isinstance(h, dict)]
    check("C6 命中刚写入的 fact", any(i in m_facts for i in hit_ids if i), f"{hit_ids} vs {m_facts}")

    obj, is_err, text = mcp.tool("search_memory", {
        "query": "search client 超时怎么修的", "scene": "coding",
        "workspace_id": WS_MCP, "branch": "master", "chain_block": True, "limit": 5})
    check("C7 MCP chain_block 非空", bool(((obj or {}).get("chain_block") or "").strip()), text[:120])

    obj, is_err, text = mcp.tool("search_memory", {
        "query": "search client 超时怎么修的", "scene": "coding",
        "workspace_id": "github.com/test/nowhere"})
    check("C8 scope 不匹配零命中", (obj or {}).get("count") == 0, text[:100])
    check("C8 零命中带排障 hint", bool((obj or {}).get("hint")), (obj or {}).get("hint", "")[:80])

    # ================= D chat 与画像 =================
    print("\n== D chat 缺省与 profile ==")
    obj, is_err, text = mcp.tool("add_memory", {"text": "scene 测试：chat 缺省路径仍走异步队列"})
    check("D1 chat 缺省仍异步入队", bool((obj or {}).get("job_ids")) and not is_err, text[:110])

    p = run(["--json", "profile"])
    o = jload(p)
    check("D2 profile 可读且结构齐全",
          p.returncode == 0 and all(k in o for k in ("user", "count", "memories", "elapsed_ms")),
          str(o)[:110])

    p = run(["--json", "add", "我叫胡超，在江西做后端开发，主要写 Java 和 Spring Boot。"], timeout=300)
    check("D3 自述事实写入成功", bool(jload(p).get("memory_id") or jload(p).get("memory_ids")),
          (p.stdout or "")[:100])
    p = run(["--json", "profile"])
    o = jload(p)
    blob = json.dumps(o.get("memories") or [], ensure_ascii=False)
    check("D3 profile 读到 L0 画像", (o.get("count") or 0) >= 1, blob[:140])
    check("D3 画像含自述信息", any(k in blob for k in ("胡超", "江西", "后端")), blob[:160])

    mcp.close()

    elapsed = time.time() - t0
    bad = results.count(False)
    print(f"\n{'全部通过' if not bad else f'存在失败：{bad} 项'}（{len(results)-bad}/{len(results)}）"
          f"，总耗时 {elapsed:.0f}s")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
