"""CLI 接口契约测试（对齐「标准自包含 CLI 规范」第三、五章）。

只跑不需要联网的命令：help / --version / config / queue / install / 参数错误。
涉及嵌入端点的命令（add / search / stats）留给 test_e2e，避免测试依赖网络。

跑法：
    .venv/Scripts/python.exe scripts/test_cli.py
    HY_MEM_TEST_EXE=dist/hy-mem/hy-mem.exe .venv/Scripts/python.exe scripts/test_cli.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "src" / "hymem" / "cli.py"  # 直跑形态（bootstrap 兜底）

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(passed)
    mark = "PASS" if passed else "FAIL"
    print(f"  {mark}  {name}" + (f"  → {detail}" if detail else ""))


def target() -> list[str]:
    """被测程序：默认源码模式，可用 HY_MEM_TEST_EXE 指向打包产物。"""
    exe = os.getenv("HY_MEM_TEST_EXE")
    if exe:
        return [str(Path(exe).resolve())]
    return [sys.executable, str(CLI)]


def run(
    args: list[str], stdin: str | None = None, timeout: int = 60, env_extra: dict | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env.setdefault("HY_MEM_CONFIG", str(ROOT / "config.json"))
    env.setdefault("HY_MEM_HOME", str(ROOT))
    # 配置闸门要「密钥齐备」才会放行到参数校验。以前靠 config.json 里存着真 key
    # 来满足这个前提，现在密钥只在环境变量里（仓库里不许有明文），所以这里给一对
    # 明显是假的占位值——本套件不发起任何真实模型调用，真值没有意义。
    env.setdefault("MEMORY_EMBEDDER_API_KEY", "sk-test-cli-placeholder")
    env.setdefault("MEMORY_LLM_API_KEY", "sk-test-cli-placeholder")
    env.pop("HY_MEM_USER_ID", None)
    if env_extra:
        env.update({k: v for k, v in env_extra.items() if v is not None})
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
    return subprocess.run(
        target() + args,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        cwd=str(ROOT),
    )


def one_json_object(out: str) -> tuple[bool, object]:
    """stdout 必须是「单一合法 JSON 对象」，不能夹带日志行。"""
    text = out.strip()
    if not text:
        return False, None
    try:
        return True, json.loads(text)
    except json.JSONDecodeError:
        return False, None


def main() -> int:
    print("被测命令：", " ".join(target()))

    # ---- 1. help：人类入口 ----
    print("\nhelp（人类入口）")
    p = run(["help"])
    check("exit 0", p.returncode == 0, f"exit={p.returncode}")
    check("有内容", len(p.stdout) > 500, f"{len(p.stdout)} 字节")
    for piece in ("全局选项", "命令", "退出码"):
        check(f"含「{piece}」", piece in p.stdout)
    check("stderr 干净", p.stderr.strip() == "", p.stderr.strip()[:80])

    # ---- 2. help --json：机器入口 ----
    print("\nhelp --json（机器入口）")
    p = run(["help", "--json"])
    ok, spec = one_json_object(p.stdout)
    check("stdout 是单一 JSON", ok)
    if ok and isinstance(spec, dict):
        for key in ("commands", "global_options", "env", "exit_codes", "hard_rules", "workflows", "version"):
            check(f"含顶层键 {key}", key in spec)
        cmds = {c["name"] for c in spec.get("commands", [])}
        for must in ("add", "search", "help", "install", "menu", "setup", "doctor", "delete", "forget"):
            check(f"命令清单含 {must}", must in cmds)
        add = next((c for c in spec.get("commands", []) if c["name"] == "add"), {})
        check("add 有 examples", bool(add.get("examples")))
        check("add 的选项带类型", all("type" in o or o.get("kind") == "flag" for o in add.get("options", [])))
        check("规格体积合理（<200KB）", len(p.stdout) < 200_000, f"{len(p.stdout)} 字节")

    # 两种等价写法必须一致
    a = run(["help", "--json"]).stdout.strip()
    b = run(["--help-json"]).stdout.strip()
    check("help --json 与 --help-json 等价", a == b)

    # ---- 3. --version ----
    print("\n版本")
    p = run(["--version"])
    check("exit 0", p.returncode == 0)
    check("输出含版本号", "hy-mem " in p.stdout and p.stdout.strip().split()[-1][0].isdigit(), p.stdout.strip())
    check("中文不乱码", "\ufffd" not in p.stdout)

    # ---- 4. 退出码语义 ----
    print("\n退出码语义")
    p = run(["get"])  # 缺位置参数 → 用法错误
    check("用法错误 exit 1", p.returncode == 1, f"exit={p.returncode}")
    check("用法错误时 stdout 为空", p.stdout.strip() == "", p.stdout[:80])
    check("用法错误写 stderr", "hy-mem:" in p.stderr, p.stderr.strip()[:80])

    p = run(["delete", "no-such-memory-id", "--dry-run", "--json"])
    check("运行时错误 exit 2", p.returncode == 2, f"exit={p.returncode}")
    ok, obj = one_json_object(p.stdout)
    check("错误也在 stdout 给 JSON", ok)
    if ok and isinstance(obj, dict):
        err = obj.get("error") or {}
        check("error.code 是字符串枚举", isinstance(err.get("code"), str), str(err.get("code")))
        check("error 带 message", bool(err.get("message")))
        check("error 带 hint 字段", "hint" in err)

    # ---- 5. 全局选项位置无关 ----
    print("\n全局选项位置无关")
    cfg = str((ROOT / "config.json").resolve())
    run(["--json", "config", "path"])  # 基线：不带 --config 也能跑（结果不作断言）
    p2 = run(["--json", "config", "path", "--config", cfg])
    check("--config 放末尾不报错", p2.returncode == 0, p2.stderr.strip()[:80])
    check("--config 确实被采用", Path(p2.stdout.strip().strip('"')) == Path(cfg) or cfg in p2.stdout, p2.stdout.strip()[:80])

    # ---- 6. --fields / --quiet ----
    print("\n输出裁剪")
    p = run(["--json", "--fields", "version", "help", "--json"])
    ok, obj = one_json_object(p.stdout)
    check("--fields 只剩指定键", ok and isinstance(obj, dict) and list(obj) == ["version"], str(list(obj) if ok else p.stdout[:60]))
    p = run(["--quiet", "config", "path"])
    check("--quiet 成功时无输出", p.returncode == 0 and p.stdout.strip() == "", repr(p.stdout[:60]))
    p = run(["--json", "--quiet", "config", "path"])
    check("--quiet 不吞 --json 的结果", p.returncode == 0 and p.stdout.strip() != "", repr(p.stdout[:60]))

    # ---- 7. 非交互不阻塞 ----
    print("\n非 TTY 行为")
    t0 = time.time()
    p = run([], stdin="")
    dt = time.time() - t0
    check("无参数不阻塞（<15s）", dt < 15, f"{dt:.1f}s")
    check("无参数 exit 0", p.returncode == 0, f"exit={p.returncode}")
    check("无参数给出帮助", "help" in p.stdout and len(p.stdout) > 200)

    t0 = time.time()
    p = run(["menu"], stdin="")
    dt = time.time() - t0
    check("menu 在非 TTY 下不阻塞（<15s）", dt < 15, f"{dt:.1f}s")

    # ---- 8. install ----
    print("\ninstall")
    p = run(["install", "--print"])
    check("install --print exit 0", p.returncode == 0, f"exit={p.returncode}")
    check("给出启动器路径", "bin" in p.stdout.lower() or "hy-mem" in p.stdout, p.stdout.strip().splitlines()[:1])

    # ---- 9. 破坏性操作有护栏 ----
    print("\n破坏性操作护栏")
    p = run(["forget"])
    check("forget 不加 --yes 拒绝执行", p.returncode == 1, f"exit={p.returncode}")
    p = run(["config", "reset"])
    check("config reset 不加 --yes 拒绝执行", p.returncode == 1, f"exit={p.returncode}")

    # ---- 10. 身份：默认通用 / 可单独切换 / 环境变量临时覆盖 ----
    print("\n身份 user_id（默认通用，可单独切换）")
    import tempfile

    sandbox = Path(tempfile.mkdtemp(prefix="hymem-test-"))
    iso = {"HY_MEM_CONFIG": str(sandbox / "config.json"), "HY_MEM_HOME": str(ROOT)}

    p = run(["--json", "user"], env_extra=iso)
    ok, obj = one_json_object(p.stdout)
    uid0 = obj.get("userId") if ok and isinstance(obj, dict) else None
    check("未设置时默认通用", uid0 == "default", str(uid0))
    check("标记为通用", bool(ok and isinstance(obj, dict) and obj.get("general")))

    p = run(["--json", "user", "set", "writer"], env_extra=iso)
    ok, obj = one_json_object(p.stdout)
    check("user set 生效", p.returncode == 0 and ok and obj.get("userId") == "writer", p.stdout.strip()[:80])
    p = run(["--json", "user"], env_extra=iso)
    ok, obj = one_json_object(p.stdout)
    check("切换后来源是配置文件", ok and obj.get("source") == "config", str(obj.get("source") if ok else p.stdout[:60]))
    check("切换不动数据目录", "dataDir" not in (obj if isinstance(obj, dict) else {}))
    p = run(["--json", "user", "list"], env_extra=iso)
    ok, obj = one_json_object(p.stdout)
    users = [u.get("userId") for u in (obj or {}).get("users", [])] if ok and isinstance(obj, dict) else []
    check("user list 能看到它", "writer" in users, str(users))

    p = run(["user", "reset"], env_extra=iso)
    p2 = run(["--json", "user"], env_extra=iso)
    ok, obj = one_json_object(p2.stdout)
    check("user reset 回到通用", ok and obj.get("userId") == "default" and obj.get("general"), p2.stdout.strip()[:80])

    p = run(["--json", "user"], env_extra={**iso, "HY_MEM_USER_ID": "tmp-agent"})
    ok, obj = one_json_object(p.stdout)
    check("环境变量优先于配置", ok and obj.get("userId") == "tmp-agent" and obj.get("source") == "env", p.stdout.strip()[:80])

    # ---- 11. 配置不全：给人的提示 + 给 agent 的结构化错误 ----
    print("\n配置不全时的提示")
    bad_cfg = sandbox / "incomplete.json"
    bad_cfg.write_text(
        json.dumps(
            {
                "setupComplete": False,
                "mode": "pro",
                "embedding": {
                    "provider": "openai",
                    "baseUrl": "http://127.0.0.1:9/v1",
                    "model": "nope",
                    "apiKey": "",
                    "dims": 1024,
                },
                "llm": {
                    "baseUrl": "http://127.0.0.1:9/v1",
                    "model": "nope",
                    "apiKey": "",
                    "keyEnv": "HY_MEM_TEST_NOKEY",
                },
                "storage": {"dataDir": str(sandbox / "data")},
                "search": {"minScore": 0.35},
            }
        ),
        encoding="utf-8",
    )
    iso_bad = {
        "HY_MEM_CONFIG": str(bad_cfg),
        "HY_MEM_ENV_FILE": str(sandbox / "no-such.env"),  # 别让仓库里的 .env 把 key 补上
        "MEMORY_EMBEDDER_API_KEY": None,
        "MEMORY_LLM_API_KEY": None,
        "YSJX_API_KEY": None,
    }
    t0 = time.time()
    p = run(["--json", "search", "任意查询"], env_extra=iso_bad, timeout=60)
    dt = time.time() - t0
    ok, obj = one_json_object(p.stdout)
    err = (obj or {}).get("error", {}) if ok and isinstance(obj, dict) else {}
    missing = [m.get("item") for m in err.get("missing", [])] if isinstance(err, dict) else []
    check("缺密钥时 exit 1（不是 2）", p.returncode == 1, f"exit={p.returncode}")
    check("error.code=E_CONFIG", err.get("code") == "E_CONFIG", str(err.get("code")))
    check("列出 embedding.apiKey", "embedding.apiKey" in missing, str(missing))
    check("列出 llm.apiKey", "llm.apiKey" in missing, str(missing))
    check("不联网、直接返回", dt < 10, f"{dt:.1f}s")

    p = run(["search", "任意查询"], env_extra=iso_bad)
    check("人话里给修复命令", "setup --step embedding" in p.stdout, p.stdout.strip()[:120])
    check("人话里说明为什么拦", "配置不全" in p.stdout or "还没配好" in p.stdout)

    # ---- 11b. 密钥含中文：闸门当场拦，不让它走到 HTTP 层 ----
    # 真因：key 进 Authorization 头，http.client 用 latin-1 编码，中文会在请求
    # 发出前炸成 UnicodeEncodeError（position 7-9），界面上只剩 HTTP 0。
    print("\n密钥含非 ASCII（中文粘贴）")
    cn_cfg = sandbox / "cfg-cn.json"
    cn_cfg.write_text(
        json.dumps(
            {
                "mode": "pro",
                "embedding": {
                    "provider": "openai",
                    "baseUrl": "https://example.invalid/v1",
                    "model": "m",
                    "apiKey": "没有啊",
                    "dims": 1024,
                },
                "llm": {
                    "baseUrl": "https://example.invalid/v1",
                    "model": "m",
                    "apiKey": "sk-中文",
                },
                "storage": {"dataDir": str(sandbox / "data")},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    iso_cn = {
        "HY_MEM_CONFIG": str(cn_cfg),
        "HY_MEM_ENV_FILE": str(sandbox / "no-such.env"),
        "MEMORY_EMBEDDER_API_KEY": None,
        "MEMORY_LLM_API_KEY": None,
        "YSJX_API_KEY": None,
    }
    t0 = time.time()
    p = run(["--json", "search", "任意查询"], env_extra=iso_cn, timeout=60)
    dt = time.time() - t0
    ok, obj = one_json_object(p.stdout)
    err = (obj or {}).get("error", {}) if ok and isinstance(obj, dict) else {}
    missing = [m.get("item") for m in err.get("missing", [])] if isinstance(err, dict) else []
    check("中文密钥被闸门拦下（exit 1）", p.returncode == 1, f"exit={p.returncode}")
    check("列出 embedding.apiKey", "embedding.apiKey" in missing, str(missing))
    check("列出 llm.apiKey", "llm.apiKey" in missing, str(missing))
    check("判据是字符集不是缺项", "ASCII" in json.dumps(err, ensure_ascii=False), str(err)[:120])
    check("不联网、直接返回", dt < 10, f"{dt:.1f}s")

    # ---- 12. help --json 收录新命令 ----
    print("\nhelp --json 收录新命令")
    p = run(["--help-json"])
    ok, obj = one_json_object(p.stdout)
    cmds = (obj or {}).get("commands", []) if ok and isinstance(obj, dict) else []
    names = [c.get("name") for c in cmds if isinstance(c, dict)]
    check("有 user 命令", "user" in names, str(names[:14]))
    entry = next((c for c in cmds if isinstance(c, dict) and c.get("name") == "user"), {})
    check("user 也有语义说明", bool(entry.get("when")), str(list(entry))[:80])

    # ---- 13. SDK 对齐命令：update / history / add 透传参数 ----
    print("\nSDK 对齐（update / history / add 透传）")
    p = run(["--help-json"])
    ok, obj = one_json_object(p.stdout)
    cmds = {c["name"]: c for c in (obj or {}).get("commands", [])} if ok else {}
    check("help --json 收录 update", "update" in cmds)
    check("help --json 收录 history", "history" in cmds)
    upd = cmds.get("update", {})
    check("update 标记 mutates", upd.get("mutates") is True, str(upd.get("mutates")))

    # update 缺新内容（stdin 为空）→ 用法错 exit 1，不碰 SDK
    p = run(["update", "some-id"], stdin="")
    check("update 缺内容 exit 1", p.returncode == 1, f"exit={p.returncode}")
    check("update 缺内容提示从 stdin 读", "stdin" in p.stderr or "新内容" in p.stderr, p.stderr.strip()[:80])

    # history 缺 memory_id → argparse 用法错 exit 1
    p = run(["history"])
    check("history 缺 id exit 1", p.returncode == 1, f"exit={p.returncode}")

    # add 坏 --metadata：进队列前就拦，exit 1
    p = run(["--json", "add", "x", "--metadata", "{bad"])
    check("add 坏 metadata exit 1", p.returncode == 1, f"exit={p.returncode}")
    ok, obj = one_json_object(p.stdout)
    err = (obj or {}).get("error") or {} if ok else {}
    check("坏 metadata 报合法 JSON 提示", "JSON" in str(err.get("message", "")), str(err.get("message"))[:80])

    # add 坏 --memory-at：同样进队列前拦截
    p = run(["--json", "add", "x", "--memory-at", "not-a-date"])
    check("add 坏 memory-at exit 1", p.returncode == 1, f"exit={p.returncode}")

    # search 坏 --since：用法错 exit 1（不该静默当不过滤）
    p = run(["--json", "search", "q", "--since", "bad-time"])
    check("search 坏 since exit 1", p.returncode == 1, f"exit={p.returncode}")
    ok, obj = one_json_object(p.stdout)
    err = (obj or {}).get("error") or {} if ok else {}
    check("坏 since 报时间格式提示", "时间格式" in str(err.get("message", "")), str(err.get("message"))[:80])

    # ── R1 级联清扫单测（直接 import，不起子进程、不发网络）────────
    class FakeCol:
        """模拟 chroma collection：where 查询按 source_raw_memory_id 匹配。"""

        def __init__(self, rows):
            self.rows = rows  # {id: {"source_raw_memory_id": ...}}

        def get(self, ids=None, where=None, include=None):
            if ids is not None:
                hit = [i for i in ids if i in self.rows]
            else:
                key = (where or {}).get("source_raw_memory_id", {}).get("$eq")
                hit = [i for i, r in self.rows.items() if r.get("source_raw_memory_id") == key]
            return {"ids": hit, "metadatas": [self.rows[i] for i in hit]}

    class FakeClient:
        def __init__(self, rows):
            self._vector_store = type("VS", (), {"_collection": FakeCol(rows)})()
            self.deleted = []

        def delete(self, nid):
            self.deleted.append(nid)
            self.rows = getattr(self, "rows", None)
            return {"success": True, "deleted_count": 1}

    from hymem.core.clientops import find_derived_nodes, sweep_derived_nodes  # noqa: E402

    rows = {
        "root": {"source_raw_memory_id": ""},
        "ghost1": {"source_raw_memory_id": "root"},
        "ghost2": {"source_raw_memory_id": "root"},
        "other": {"source_raw_memory_id": "another"},
    }
    fc = FakeClient(rows)
    got = find_derived_nodes(fc, "root")
    check("R1 find_derived 定位派生节点", sorted(got) == ["ghost1", "ghost2"], str(got))

    purged, note = sweep_derived_nodes(fc, "root")
    check("R1 sweep 级联删除派生节点", sorted(purged) == ["ghost1", "ghost2"] and note == "", f"{purged}|{note}")
    check("R1 sweep 走公开 client.delete", sorted(fc.deleted) == ["ghost1", "ghost2"], str(fc.deleted))

    class BrokenVS:
        _collection = None  # 模拟 SDK 内部结构变化

    bc = FakeClient(rows)
    bc._vector_store = BrokenVS()
    purged, note = sweep_derived_nodes(bc, "root")
    check("R1 内部不可达时降级且带 note", purged == [] and "可能残留" in note, f"{purged}|{note[:40]}")

    # ── P0-2 MCP 删除/更新对齐 CLI（离线假 SDK，沙箱 store）───────────────
    print("\nP0-2 MCP 删除/更新：校验 success + 级联清扫")
    _p02 = Path(tempfile.mkdtemp(prefix="hymem-p02-"))
    _p02_env = {"MEMORY_DATA_DIR": str(_p02 / "data")}
    _saved_p02 = {k: os.environ.get(k) for k in _p02_env}
    os.environ.update(_p02_env)
    try:
        from hymem.mcp import server as mcp_server  # noqa: E402

        class _P02Col:
            def __init__(self, rows):
                self.rows = rows

            def get(self, ids=None, where=None, include=None):
                key = (where or {}).get("source_raw_memory_id", {}).get("$eq")
                hit = [i for i, r in self.rows.items() if key and r.get("source_raw_memory_id") == key]
                return {"ids": hit, "metadatas": []}

        class _P02Client:
            def __init__(self, mode="ok"):
                self.mode = mode
                self.deleted = []
                self.rows = {
                    "root": {"source_raw_memory_id": ""},
                    "ghost1": {"source_raw_memory_id": "root"},
                    "ghost2": {"source_raw_memory_id": "root"},
                }
                self._vector_store = type("VS", (), {"_collection": _P02Col(self.rows)})()

            def delete(self, nid):
                self.deleted.append(nid)
                if self.mode == "fail-root" and nid == "root":
                    return {"success": False, "deleted_count": 0}
                return {"success": True, "deleted_count": 1}

            def get(self, mid):
                return {"memory_id": mid, "content": "old"}

            def update(self, mid, content):
                if self.mode == "fail-update":
                    return {"success": False, "error_message": "update boom"}
                return {"success": True, "memory_id": mid, "content": content}

        _old_client = mcp_server._CLIENT
        c = _P02Client()
        mcp_server._CLIENT = c
        try:
            out = json.loads(mcp_server._tool_delete_memory({"memory_id": "root", "confirm": True}))
            check("P0-2 MCP 删除成功返回 deleted", out.get("deleted") == "root", str(out)[:100])
            check("P0-2 MCP 删除级联清扫派生节点",
                  sorted(out.get("swept") or []) == ["ghost1", "ghost2"], str(out)[:100])
            check("P0-2 MCP 删除确实删了派生节点",
                  sorted(c.deleted) == ["ghost1", "ghost2", "root"], str(c.deleted))
        finally:
            mcp_server._CLIENT = _old_client

        c = _P02Client("fail-root")
        mcp_server._CLIENT = c
        try:
            raised = False
            try:
                mcp_server._tool_delete_memory({"memory_id": "root", "confirm": True})
            except Exception:  # noqa: BLE001
                raised = True
            check("P0-2 MCP 删除失败以工具错误返回（不报成功）", raised)
        finally:
            mcp_server._CLIENT = _old_client

        c = _P02Client()
        mcp_server._CLIENT = c
        try:
            out = json.loads(mcp_server._tool_update_memory(
                {"memory_id": "root", "content": "new", "confirm": True}))
            check("P0-2 MCP 更新成功仍返回 updated", out.get("updated") == "root" and out.get("result"),
                  str(out)[:100])
        finally:
            mcp_server._CLIENT = _old_client

        c = _P02Client("fail-update")
        mcp_server._CLIENT = c
        try:
            raised = False
            try:
                mcp_server._tool_update_memory({"memory_id": "root", "content": "new", "confirm": True})
            except Exception:  # noqa: BLE001
                raised = True
            check("P0-2 MCP 更新失败以工具错误返回（不报成功）", raised)
        finally:
            mcp_server._CLIENT = _old_client
    finally:
        for k, v in _saved_p02.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil as _shutil

        _shutil.rmtree(_p02, ignore_errors=True)

    # ── P1-1 MCP 同步 add：正确解包 vdb 快照，失败/不完整不打标签 ──────
    print("\nP1-1 MCP 同步 add 标签快照")
    _p11 = Path(tempfile.mkdtemp(prefix="hymem-p11-"))
    _p11_env = {"MEMORY_DATA_DIR": str(_p11 / "data")}
    _saved_p11 = {k: os.environ.get(k) for k in _p11_env}
    os.environ.update(_p11_env)
    try:
        from hymem.mcp import server as mcp_server  # noqa: E402
        from hymem.core import store as _store, two_phase

        class _P11Client:
            def __init__(self, memories):
                self.memories = memories
                self.fail = False

            def list_memories(self, limit=200, offset=0, **kw):
                if self.fail:
                    raise RuntimeError("list failed")
                # 真 SDK 按 limit/offset 分页；不返回空页就永远不知道到底
                return {"vdb": {"memories": list(self.memories)[offset:offset + limit]}}

        def _commit_with(old, new):
            def _commit(client, text, ar, **kw):
                client.memories = [{"memory_id": old}, {"memory_id": new}]
                return {"success": True, "memory_id": "l1", "memory_ids": [old, new]}
            return _commit

        real = (two_phase.extract_phase, two_phase.prepare_commit, two_phase.commit_extracted_with_retry)
        _old_client = mcp_server._CLIENT
        try:
            c = _P11Client([{"memory_id": "old1"}])
            mcp_server._CLIENT = c
            two_phase.extract_phase = lambda *a, **k: {"ok": True}
            two_phase.prepare_commit = lambda *a, **k: {}
            two_phase.commit_extracted_with_retry = _commit_with("old1", "new1")
            out1 = json.loads(mcp_server._tool_add_memory(
                {"text": "x", "tags": ["t1"], "async": False}))
            conn = _store.connect()
            try:
                got1 = _store.tags_for(conn, ["old1", "new1"])
            finally:
                conn.close()
            check("P1-1 vdb 解包后只给新节点打标签",
                  got1.get("new1") == ["t1"] and not got1.get("old1"), str(got1))
            check("P1-1 同步 add 输出契约未变",
                  out1.get("mode") == "sync" and out1.get("memory_ids") == ["old1", "new1"], str(out1))

            c2 = _P11Client([{"memory_id": "old2"}])
            c2.fail = True
            mcp_server._CLIENT = c2
            two_phase.commit_extracted_with_retry = _commit_with("old2", "new2")
            out2 = json.loads(mcp_server._tool_add_memory(
                {"text": "x", "tags": ["t2"], "async": False}))
            conn = _store.connect()
            try:
                got2 = _store.tags_for(conn, ["old2", "new2"])
            finally:
                conn.close()
            check("P1-1 快照失败时跳过标签绑定",
                  not got2.get("new2") and not got2.get("old2"), str(got2))
            check("P1-1 快照失败时输出 note 说明", "未绑定" in (out2.get("note") or ""), str(out2))
        finally:
            two_phase.extract_phase, two_phase.prepare_commit, two_phase.commit_extracted_with_retry = real
            mcp_server._CLIENT = _old_client
    finally:
        for k, v in _saved_p11.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil as _shutil

        _shutil.rmtree(_p11, ignore_errors=True)

    # ── R3 fail-fast：抢不到写锁立即 E_LOCK，不干等 180s ────────────────
    print("\nR3 sync add 抢锁 fail-fast")
    sandbox2 = Path(tempfile.mkdtemp(prefix="hymem-lock-"))
    (sandbox2 / "data").mkdir()
    (sandbox2 / "config.json").write_text(
        (ROOT / "config.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    sb_env = {"HY_MEM_CONFIG": str(sandbox2 / "config.json"), "MEMORY_DATA_DIR": str(sandbox2 / "data")}
    saved_env = {k: os.environ.get(k) for k in sb_env}
    os.environ.update(sb_env)  # 测试进程的 store 指向同一沙箱以占锁
    try:
        from hymem.core import store as _store  # noqa: E402

        lk = _store.WriteLock(timeout=0.5)
        lk.__enter__()
        try:
            import time as _time

            t0 = _time.time()
            p = run(["--json", "add", "锁占用时的失败路径"], env_extra=sb_env, timeout=60)
            elapsed = _time.time() - t0
            okj, obj = one_json_object(p.stdout)
            err = (obj or {}).get("error") or {}
            check("R3 锁占用 → 非零退出", p.returncode != 0, f"exit={p.returncode} elapsed={elapsed:.1f}s")
            check("R3 报 E_LOCK", err.get("code") == "E_LOCK", str(err.get("code")))
            msg = str(err.get("message", ""))
            hint = str(err.get("hint", ""))
            check("R3 三要素：本条未入库", "未入库" in msg, msg[:60])
            check("R3 三要素：单条可重试 + 多条用 --async", "重试" in hint and "--async" in hint, hint[:60])
            check("R3 快速失败（<30s，非 180s 等锁）", elapsed < 30, f"{elapsed:.1f}s")
            # 零写入：库里没有这条
            p = run(["--json", "list", "-n", "5"], env_extra=sb_env, timeout=60)
            okj, obj = one_json_object(p.stdout)
            mems = (obj or {}).get("memories") or [] if okj else []
            check("R3 零写入（list 为空）", okj and len(mems) == 0, f"count={len(mems)}")
        finally:
            lk.__exit__(None, None, None)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # R3.3 口径：help add 必须写明两段式锁范围 + E_LOCK 快速失败 + --async 正路
    p = run(["help", "add"])
    check("R3 help add 两段式口径", "两段式" in p.stdout and "锁外" in p.stdout, "")
    check("R3 help add E_LOCK 快速失败", "E_LOCK" in p.stdout and "零写入" in p.stdout, "")
    check("R3 help add 指向 --async", "--async" in p.stdout, "")

    # ---- I SDK 对齐补全（docs/specs/2026-11-02-sdk-alignment-gaps/）----
    # 全部离线：只测参数校验/确认门/help 注册，不发起真实模型调用。
    print("\nI SDK 对齐：search 新参数 / 确认门 / help 注册")
    p = run(["help", "--json"])
    okj, spec_i = one_json_object(p.stdout)
    if okj and isinstance(spec_i, dict):
        cmds_i = {c["name"] for c in spec_i.get("commands", [])}
        for must in ("backup", "restore", "vdb-scan", "vdb-plan", "vdb-reembed",
                     "vdb-reembed-graph", "vdb-drop", "graph-build"):
            check(f"I1 help 注册 {must}", must in cmds_i)
    else:
        check("I1 help --json 可解析", False, p.stdout[:80])

    p = run(["search"])
    check("I2 search 缺查询 exit 1", p.returncode == 1, f"exit={p.returncode}")
    check("I2 提示 query/--queries 两种给法", "--queries" in (p.stderr + p.stdout))

    p = run(["search", "hello", "--queries", "a,b"])
    check("I3 queries 与位置参数 query 互斥 exit 1", p.returncode == 1, f"exit={p.returncode}")

    p = run(["search", "--queries", "1,2,3,4,5,6"])
    check("I4 queries 超 5 路拒绝 exit 1", p.returncode == 1, f"exit={p.returncode}")

    p = run(["search", "--scene", "working", "--queries", "a,b", "-u", "tester"])
    check("I5 scene 检索不支持 queries exit 1", p.returncode == 1, f"exit={p.returncode}")

    for _cmd, _extra in (("vdb-reembed", ["--source", "a", "--target", "b"]),
                         ("vdb-drop", ["--path", "a"]),
                         ("restore", ["--snapshot", "a"])):
        p = run([_cmd] + _extra)
        check(f"I6 {_cmd} 无 --yes 拒绝 exit 1", p.returncode == 1, f"exit={p.returncode}")
        check(f"I6 {_cmd} 提示加 --yes", "--yes" in (p.stderr + p.stdout))

    # ---- S1 协议层守护断言（docs/specs/2026-11-02-cli-split-ports/）----
    # vendor 触点只准出现在 backends/：防止「只抽一半」的协议层被悄悄绕过。
    import re as _re

    _src_root = Path(__file__).resolve().parent.parent / "src" / "hymem"
    _vendor_hits, _priv_hits, _run_hits = [], [], []
    for _f in _src_root.rglob("*.py"):
        _rel = _f.relative_to(_src_root).as_posix().replace("\\", "/")
        if _rel.startswith("backends/") or _rel == "_pyi_entry.py":
            continue
        _t = _f.read_text(encoding="utf-8")
        if _re.search(r"^\s*(from|import)\s+hy_memory", _t, _re.M):
            _vendor_hits.append(_rel)
        if _re.search(r"(client|b|bk)\._(graph_store|get_chain_store|registry|loop_thread|config\b)", _t):
            _priv_hits.append(_rel)
        if _re.search(r"asyncio\.run", _t) and not _rel.startswith("cmds/"):
            _run_hits.append(_rel)
    check("G1 vendor import 只在 backends/", not _vendor_hits, str(_vendor_hits))
    check("G1 SDK 私有触点只在 backends/", not _priv_hits, str(_priv_hits))
    check("G1 asyncio.run 只在 backends/（cmds 仅本地嵌入探针）", not _run_hits, str(_run_hits))
    # 适配器能被协议工厂构造（结构化鸭子类型：显式面 + 透传）
    from hymem.backends.hymemory.backend import HyMemBackend
    _PROTO_METHODS = (
        "add", "get", "update", "delete", "delete_all", "search", "list_memories",
        "history", "get_recent_history", "finalize_session", "get_write_outcome",
        "get_user_basic_info", "get_metrics", "close", "evolve",
        "count_facts", "chain_diff_blocks", "submit_in_writer_lock",
    )
    check("G1 HyMemBackend 显式面覆盖 MemoryBackend 全部协议方法",
          all(callable(getattr(HyMemBackend, m, None)) for m in _PROTO_METHODS),
          str([m for m in _PROTO_METHODS if not callable(getattr(HyMemBackend, m, None))]))

    # ---- G2 上游补丁守护（docs/specs/2026-11-02-ultra-graph-degrade/，U1 见 docs/UPSTREAM-ISSUES.md）----
    # pip 重装 venv 会丢掉 site-packages 里的补丁；这条断言让它当场暴露而不是
    # 等到多进程死信才被发现。补丁本体与重放：scripts/apply_upstream_patches.py。
    _marker = "[hy-mem patch 2026-11-02 graph-degrade]"
    _sdk_client = ROOT / ".venv" / "Lib" / "site-packages" / "hy_memory" / "client.py"
    _vendor_client = ROOT / "vendor" / "hy-memory" / "hy_memory" / "client.py"
    check("G2 site-packages SDK 含 graph-degrade 补丁标记",
          _sdk_client.exists() and _marker in _sdk_client.read_text(encoding="utf-8"),
          str(_sdk_client))
    check("G2 vendor 快照含 graph-degrade 补丁标记",
          _vendor_client.exists() and _marker in _vendor_client.read_text(encoding="utf-8"),
          str(_vendor_client))

    # ---- P1-6 config validate：有 FAIL 必须非零退出，--json 契约不变 ----
    print("\nconfig validate 退出码（P1-6）")
    _val_dir = Path(tempfile.mkdtemp(prefix="hymem-val-"))
    _val_cfg = _val_dir / "config.json"
    _val_cfg.write_text(
        json.dumps({
            "mode": "pro",
            "embedding": {"provider": "openai", "baseUrl": "https://example.invalid/v1",
                          "model": "m", "apiKey": "", "dims": "abc"},
            "llm": {"baseUrl": "https://example.invalid/v1", "model": "m", "apiKey": ""},
            "storage": {"dataDir": str(_val_dir / "data")},
        }),
        encoding="utf-8",
    )
    _val_env = {
        "HY_MEM_CONFIG": str(_val_cfg),
        "HY_MEM_HOME": str(_val_dir),
        "MEMORY_EMBEDDER_API_KEY": "sk-test-cli-placeholder",
        "MEMORY_LLM_API_KEY": "sk-test-cli-placeholder",
    }
    p = run(["--json", "config", "validate"], env_extra=_val_env)
    ok, obj = one_json_object(p.stdout)
    checks = {c.get("name"): c.get("pass") for c in (obj or {}).get("checks", [])} if ok and isinstance(obj, dict) else {}
    check("有 FAIL 时 exit 非零", p.returncode != 0, f"exit={p.returncode}")
    check("--json 输出仍是单一 JSON 对象", ok and isinstance(obj, dict), p.stdout.strip()[:100])
    check("embedding.dims 标为 FAIL", checks.get("embedding.dims") is False, str(checks.get("embedding.dims")))
    check("非法值不打印 traceback", "Traceback" not in (p.stdout + p.stderr), p.stderr.strip()[:100])

    # 正常配置仍应 exit 0（不能把正常流程一起改坏）
    _val_ok = _val_dir / "ok.json"
    _val_ok.write_text(
        json.dumps({
            "mode": "lite",
            "embedding": {"provider": "openai", "baseUrl": "https://example.invalid/v1",
                          "model": "m", "apiKey": "sk-test-cli-placeholder", "dims": 1024},
            "storage": {"dataDir": str(_val_dir / "data")},
        }),
        encoding="utf-8",
    )
    p = run(["--json", "config", "validate"],
            env_extra={**_val_env, "HY_MEM_CONFIG": str(_val_ok)})
    ok, obj = one_json_object(p.stdout)
    check("配置通过时 exit 0", p.returncode == 0 and ok and obj.get("ok") is True,
          f"exit={p.returncode} {p.stdout.strip()[:80]}")
    import shutil as _shutil

    _shutil.rmtree(_val_dir, ignore_errors=True)

    # ---- P1-7 检索共享：唯一实现守护 + CLI/MCP 输出契约快照 ----
    print("\nP1-7 CLI/MCP 检索共享")
    _read_py = _src_root / "cmds" / "read.py"
    _srv_py = _src_root / "mcp" / "server.py"
    _read_src = _read_py.read_text(encoding="utf-8")
    _srv_src = _srv_py.read_text(encoding="utf-8")
    check("P1-7 read.py 走共享 searchcore", "searchcore(" in _read_src)
    check("P1-7 server.py 走共享 searchcore", "searchcore(" in _srv_src)
    check("P1-7 read.py 不再直接调用 client.search", ".search(" not in _read_src)
    check("P1-7 server.py 不再直接调用 client.search", ".search(" not in _srv_src)
    _dup_hits = []
    for _f in _src_root.rglob("*.py"):
        _rel = _f.relative_to(_src_root).as_posix()
        _t = _f.read_text(encoding="utf-8")
        if "want <= have" in _t or 'for bucket in ("normal", "proactive", "schema")' in _t:
            _dup_hits.append(_rel)
    check("P1-7 标签过滤/拍平实现唯一（仅 backends）",
          len(_dup_hits) == 1 and _dup_hits[0].startswith("backends/hymemory/searchops.py"),
          str(_dup_hits))

    _p17_dir = Path(tempfile.mkdtemp(prefix="hymem-p17-"))
    (_p17_dir / "data").mkdir()
    _p17_env = {k: os.environ.get(k) for k in ("MEMORY_DATA_DIR", "HY_MEM_DATA_DIR")}
    os.environ["MEMORY_DATA_DIR"] = str(_p17_dir / "data")
    try:
        import contextlib as _ctx
        import io as _io

        class _P17Client:
            _mode = "pro"

            def __init__(self) -> None:
                self.closed = False
                self.calls: list = []

            def search(self, query=None, **kw):
                self.calls.append((query, kw))
                return {"memories": {"normal": [
                    {"memory_id": "m1", "content": "hello", "score": 0.9,
                     "tags": ["auto1"], "_tags": ["manual1"]}]}, "elapsed_ms": 1.0}

            def list_memories(self, **kw):
                return {"vdb": {"memories": []}}

            def close(self):
                self.closed = True

        from hymem.cmds import read as _readmod
        from hymem.cmds.parser import build_parser as _build_parser

        _real_make, _real_ready = _readmod.make_client, _readmod.require_ready
        _fake_cli = _P17Client()
        try:
            _readmod.make_client = lambda mode: _fake_cli
            _readmod.require_ready = lambda *a, **k: None
            _cli_args = _build_parser().parse_args(
                ["--json", "-u", "u1", "search", "hello", "-n", "3"])
            _cli_buf = _io.StringIO()
            with _ctx.redirect_stdout(_cli_buf):
                _cli_rc = _readmod.cmd_search(_cli_args)
            _cli_obj = json.loads(_cli_buf.getvalue().strip() or "{}")
        finally:
            _readmod.make_client, _readmod.require_ready = _real_make, _real_ready
        for _k in ("count", "min_score", "elapsed_ms", "tags_filter", "results",
                   "schemas", "schema_available"):
            check(f"CLI search 契约字段 {_k}", _k in _cli_obj, str(sorted(_cli_obj)))
        _cli_first = (_cli_obj.get("results") or [{}])[0]
        check("CLI search 结果条目未被投影改写",
              _cli_first.get("memory_id") == "m1" and _cli_first.get("tags") == ["auto1"],
              str(_cli_first)[:120])
        check("CLI search rc=0", _cli_rc == 0, str(_cli_rc))

        from hymem.mcp import server as _mcpmod

        _old_client = _mcpmod._CLIENT
        _fake_mcp = _P17Client()
        _mcpmod._CLIENT = _fake_mcp
        try:
            _mcp_obj = json.loads(_mcpmod._tool_search_memory({"query": "hello", "limit": 3}))
        finally:
            _mcpmod._CLIENT = _old_client
        for _k in ("query", "count", "hits", "offset", "schemas", "schema_available"):
            check(f"MCP search 契约字段 {_k}", _k in _mcp_obj, str(sorted(_mcp_obj)))
        _mcp_first = (_mcp_obj.get("hits") or [{}])[0]
        check("MCP hit 字段未删改",
              {"memory_id", "content", "score", "when"} <= set(_mcp_first), str(_mcp_first)[:120])
    finally:
        for _k, _v in _p17_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        import shutil as _shutil2

        _shutil2.rmtree(_p17_dir, ignore_errors=True)

    # ---- P2 静默路径收口：serve 配置失败 / 非法 HY_MEM_MIN_SCORE ----
    print("\n静默路径：serve 端口回退 / 非法 HY_MEM_MIN_SCORE")
    _sil_dir = Path(tempfile.mkdtemp(prefix="hymem-silent-"))
    _sil_cfg = _sil_dir / "config.json"
    _sil_cfg.write_text(json.dumps({"mode": "pro", "servePort": "abc"}), encoding="utf-8")
    _sil_env = {k: os.environ.get(k) for k in
                ("HY_MEM_CONFIG", "HY_MEM_ENV_FILE", "HY_MEM_MIN_SCORE")}
    try:
        os.environ["HY_MEM_CONFIG"] = str(_sil_cfg)
        os.environ["HY_MEM_ENV_FILE"] = str(_sil_dir / "no-such.env")
        from hymem.cmds import queue as _qmod

        _port_fn = getattr(_qmod, "_wanted_serve_port", None)
        _err_buf = _io.StringIO()
        with _ctx.redirect_stderr(_err_buf):
            _fallback_port = _port_fn() if callable(_port_fn) else None
        check("P2 serve 配置失败仍回退随机端口（不阻断起库）", _fallback_port == 0,
              f"port={_fallback_port}")
        check("P2 serve 配置失败往 stderr 报原因",
              "配置" in _err_buf.getvalue() and "随机端口" in _err_buf.getvalue(),
              _err_buf.getvalue()[:140])

        os.environ["HY_MEM_CONFIG"] = str(_sil_dir / "missing.json")
        os.environ["HY_MEM_MIN_SCORE"] = "abc"
        from hymem.core import config as _cfgmod

        _err2 = _io.StringIO()
        with _ctx.redirect_stderr(_err2):
            _loaded = _cfgmod.load()
        check("P2 非法 HY_MEM_MIN_SCORE 告警（不再完全无声）",
              "HY_MEM_MIN_SCORE" in _err2.getvalue(), _err2.getvalue()[:140])
        check("P2 非法 HY_MEM_MIN_SCORE 被忽略（回退默认 0.35）",
              (_loaded.get("search") or {}).get("minScore") == 0.35,
              str((_loaded.get("search") or {}).get("minScore")))
    finally:
        for _k, _v in _sil_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        import shutil as _shutil3

        _shutil3.rmtree(_sil_dir, ignore_errors=True)

    # ---- L SDK 一致性：scene 能力声明 / chat 检索钉死（UPSTREAM-ISSUES U4/U5）----
    print("\nL SDK 一致性：scene 能力 / chat 钉死")
    import contextlib as _ctx
    import io as _io

    from hymem.mcp import server as _scmod

    class _SceneCapClient:
        """假 client：记录 add/search 调用，用于判断入口是否真的进了 SDK。"""

        _mode = "pro"

        def __init__(self) -> None:
            self.adds: list = []
            self.searches: list = []
            self.add_scene: str | None = None  # 覆盖 SDK 回执 scene（模拟降级）

        def close(self) -> None:
            pass

        def add(self, content, **kw):
            self.adds.append((content, kw))
            return {"success": True, "scene": self.add_scene or kw.get("scene") or "chat",
                    "fact_ids": ["f1"], "experience_ids": []}

        def search(self, query=None, **kw):
            self.searches.append((query, kw))
            return {"memories": {"normal": [
                {"memory_id": "chat1", "content": "chat 命中", "score": 0.9}]}, "elapsed_ms": 1.0}

    class _RouteClient(_SceneCapClient):
        """假 client：按 scene 参数返回 chat 或生产力结构（模拟 SDK 三分类）。"""

        def __init__(self, route: str) -> None:
            super().__init__()
            self.route = route

        def search(self, query=None, **kw):
            self.calls = self.searches
            self.searches.append((query, kw))
            if kw.get("scene") == "chat":
                return {"memories": {"normal": [
                    {"memory_id": "chat1", "content": "chat 库命中", "score": 0.9}]},
                    "elapsed_ms": 1.0}
            if self.route == "productivity":
                return {"scene": "productivity", "facts": [{"fact": "f"}],
                        "experiences": [], "elapsed_ms": 1.0}
            return {"memories": {"normal": [
                {"memory_id": "m1", "content": "chat ok", "score": 0.9}]}, "elapsed_ms": 1.0}

    _sc_dir = Path(tempfile.mkdtemp(prefix="hymem-scencap-"))
    (_sc_dir / "data").mkdir()
    _sc_cfg = _sc_dir / "config.json"
    _sc_env = {k: os.environ.get(k) for k in (
        "HY_MEM_CONFIG", "MEMORY_DATA_DIR", "HY_MEM_DATA_DIR", "MEMORY_CODING_ENABLED")}
    _sc_trace = [{"role": "assistant", "content": "改超时",
                  "tool_calls": [{"id": "1", "type": "function",
                                  "function": {"name": "edit", "arguments": "{}"}}]}]
    _old_sc_client = _scmod._CLIENT
    try:
        os.environ["MEMORY_DATA_DIR"] = str(_sc_dir / "data")
        os.environ["HY_MEM_CONFIG"] = str(_sc_cfg)
        os.environ.pop("MEMORY_CODING_ENABLED", None)
        _fake_cap = _SceneCapClient()
        _scmod._CLIENT = _fake_cap

        # L1/L2/L3：lite —— 能力声明为 false，add/search 必须明确失败且零写入
        _sc_cfg.write_text(json.dumps({"mode": "lite"}), encoding="utf-8")
        _ver = json.loads(_scmod._tool_get_version({}))
        check("L1 lite 下 capabilities.scene=false",
              _ver.get("capabilities", {}).get("scene") is False,
              str(_ver.get("capabilities")))
        _add = json.loads(_scmod._tool_add_memory(
            {"scene": "working", "messages": _sc_trace}))
        check("L2 lite 下 add scene=working 明确失败（非 ok=true 谎报）",
              _add.get("ok") is False and _add.get("error") == "scene_unavailable",
              str(_add)[:160])
        check("L2b lite 下 add scene 零写入（未触达 SDK）", not _fake_cap.adds,
              f"adds={len(_fake_cap.adds)}")
        check("L2c 失败响应 applied_scene=null（不冒充已落库场景）",
              _add.get("applied_scene") is None, str(_add.get("applied_scene")))
        _srch = json.loads(_scmod._tool_search_memory({"query": "q", "scene": "coding"}))
        check("L3 lite 下 search scene 明确失败（可解释）",
              _srch.get("ok") is False and _srch.get("error") == "scene_unavailable",
              str(_srch)[:160])

        # L4：pro + MEMORY_CODING_ENABLED=false（SDK 生产力链路关闭）
        os.environ["MEMORY_CODING_ENABLED"] = "false"
        _sc_cfg.write_text(json.dumps({"mode": "pro"}), encoding="utf-8")
        _ver2 = json.loads(_scmod._tool_get_version({}))
        check("L4 coding 关闭时 capabilities.scene=false",
              _ver2.get("capabilities", {}).get("scene") is False,
              str(_ver2.get("capabilities")))
        _add2 = json.loads(_scmod._tool_add_memory(
            {"scene": "coding", "messages": _sc_trace}))
        check("L4b coding 关闭时 add scene 明确失败且零写入",
              _add2.get("ok") is False and _add2.get("error") == "scene_unavailable"
              and not _fake_cap.adds, str(_add2)[:160])

        # L5：pro + coding 默认开启 —— 行为不变
        os.environ.pop("MEMORY_CODING_ENABLED", None)
        _sc_cfg.write_text(json.dumps({"mode": "pro"}), encoding="utf-8")
        _ver3 = json.loads(_scmod._tool_get_version({}))
        check("L5 正常配置 capabilities.scene=true",
              _ver3.get("capabilities", {}).get("scene") is True,
              str(_ver3.get("capabilities")))
        _add3 = json.loads(_scmod._tool_add_memory(
            {"scene": "working", "messages": _sc_trace}))
        check("L5b 正常配置 add scene 仍成功且真实进入 SDK",
              _add3.get("ok") is True and _add3.get("mode") == "working"
              and len(_fake_cap.adds) == 1
              and _fake_cap.adds[0][1].get("scene") == "working",
              f"ok={_add3.get('ok')} mode={_add3.get('mode')} adds={len(_fake_cap.adds)}")

        # L10/L11：ISSUE-C —— scene schema 的 default/enum/描述要点
        _toolsmap = {t["name"]: t for t in _scmod.TOOLS}
        _sch_add = _toolsmap["add_memory"]["inputSchema"]["properties"]["scene"]
        _sch_srch = _toolsmap["search_memory"]["inputSchema"]["properties"]["scene"]
        check("L10 add scene schema 有 default=chat 且 enum 明确",
              _sch_add.get("default") == "chat"
              and _sch_add.get("enum") == ["chat", "working", "coding"],
              f"default={_sch_add.get('default')} enum={_sch_add.get('enum')}")
        _d10 = str(_sch_add.get("description") or "")
        check("L10b add scene 描述含判定规则/错库后果/跨库/能力开关",
              all(k in _d10 for k in ("diff", "coding", "working", "0 命中",
                                      "跨库", "capabilities.scene")),
              _d10)
        check("L11 search scene schema 有 default=chat 且 enum 含 chat",
              _sch_srch.get("default") == "chat"
              and _sch_srch.get("enum") == ["chat", "working", "coding"],
              f"default={_sch_srch.get('default')} enum={_sch_srch.get('enum')}")
        _d11 = str(_sch_srch.get("description") or "")
        check("L11b search scene 描述区分 working/coding 且写清错库后果",
              all(k in _d11 for k in ("working", "coding", "空结果", "跨库",
                                      "capabilities.scene")),
              _d11)

        # L12/L13：ISSUE-C —— add 回执给 request/stored 两值，降级要显式告警
        check("L12 正常写入回执 requested_scene=stored_scene=working",
              _add3.get("requested_scene") == "working"
              and _add3.get("stored_scene") == "working"
              and _add3.get("scene_degraded") is False,
              f"req={_add3.get('requested_scene')} stored={_add3.get('stored_scene')} "
              f"deg={_add3.get('scene_degraded')}")
        _deg_client = _SceneCapClient()
        _deg_client.add_scene = "chat"   # 模拟 SDK 把 working 降级写进 chat
        _old_deg = _scmod._CLIENT
        _scmod._CLIENT = _deg_client
        try:
            _add_deg = json.loads(_scmod._tool_add_memory(
                {"scene": "working", "messages": _sc_trace}))
        finally:
            _scmod._CLIENT = _old_deg
        check("L13 降级回执：stored_scene=chat / scene_degraded=true / 有 warning",
              _add_deg.get("stored_scene") == "chat"
              and _add_deg.get("scene_degraded") is True
              and bool(_add_deg.get("warning"))
              and "stored_scene" in str(_add_deg.get("note") or ""),
              str(_add_deg)[:180])

        # L14：ISSUE-C —— scene 检索回执区分 requested / resolved
        _srch2 = json.loads(_scmod._tool_search_memory({"query": "q", "scene": "coding"}))
        check("L14 scene 检索回执：scene_requested=scene_resolved=coding，旧 scene 保留",
              _srch2.get("scene_requested") == "coding"
              and _srch2.get("scene_resolved") == "coding"
              and "scene" in _srch2,
              f"req={_srch2.get('scene_requested')} res={_srch2.get('scene_resolved')} "
              f"scene={_srch2.get('scene')}")

        # L6/L7：ISSUE-B —— 三分类判 productivity 时必须钉回 chat 库
        from hymem.core.searchops import searchcore as _searchcore

        _fake_prod = _RouteClient("productivity")
        _r1 = _searchcore(_fake_prod, None, queries=["a", "b"], search_mode="agentic")
        check("L6 productivity 路由被钉回 chat 库（有命中，不再返回 0）",
              [x.get("memory_id") for x in _r1["items"]] == ["chat1"],
              str(_r1["items"])[:140])
        check("L6b 只在误路由时重试一次，且重试用 scene=chat + 目标 query",
              len(_fake_prod.searches) == 2
              and _fake_prod.searches[0][1].get("scene") is None
              and _fake_prod.searches[1][1].get("scene") == "chat"
              and _fake_prod.searches[1][0] == "b",
              str([(q, k.get("scene")) for q, k in _fake_prod.searches]))
        _fake_chat = _RouteClient("chat")
        _r2 = _searchcore(_fake_chat, None, queries=["a", "b"], search_mode="agentic")
        check("L7 判 chat 时不重试，queries/agentic 能力原样保留",
              len(_fake_chat.searches) == 1
              and _fake_chat.searches[0][1].get("queries") == ["a", "b"]
              and _fake_chat.searches[0][1].get("search_mode") == "agentic"
              and [x.get("memory_id") for x in _r2["items"]] == ["m1"],
              str([(q, sorted(k)) for q, k in _fake_chat.searches]))

        # L8：MCP 入口同样受保护
        _fake_mcp_prod = _RouteClient("productivity")
        _old_cli = _scmod._CLIENT
        _scmod._CLIENT = _fake_mcp_prod
        try:
            _mcp_s = json.loads(_scmod._tool_search_memory(
                {"queries": ["a", "b"], "search_mode": "agentic"}))
        finally:
            _scmod._CLIENT = _old_cli
        check("L8 MCP search 入口返回 chat 库命中",
              [x.get("memory_id") for x in _mcp_s.get("hits") or []] == ["chat1"],
              str(_mcp_s.get("hits"))[:140])

        # L9：CLI 入口同样受保护
        from hymem.cmds import read as _readmod2
        from hymem.cmds.parser import build_parser as _build_parser2

        _fake_cli_prod = _RouteClient("productivity")
        _real_make2, _real_ready2 = _readmod2.make_client, _readmod2.require_ready
        try:
            _readmod2.make_client = lambda mode: _fake_cli_prod
            _readmod2.require_ready = lambda *a, **k: None
            _cli_args2 = _build_parser2().parse_args(
                ["--json", "-u", "u1", "search", "--queries", "a,b",
                 "--search-mode", "agentic"])
            _buf2 = _io.StringIO()
            with _ctx.redirect_stdout(_buf2):
                _rc2 = _readmod2.cmd_search(_cli_args2)
            _cli_obj2 = json.loads(_buf2.getvalue().strip() or "{}")
        finally:
            _readmod2.make_client, _readmod2.require_ready = _real_make2, _real_ready2
        check("L9 CLI search 入口返回 chat 库命中",
              [x.get("memory_id") for x in _cli_obj2.get("results") or []] == ["chat1"]
              and _rc2 == 0,
              f"rc={_rc2} results={str(_cli_obj2.get('results'))[:120]}")
    finally:
        _scmod._CLIENT = _old_sc_client
        for _k, _v in _sc_env.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        import shutil as _shutil4

        _shutil4.rmtree(_sc_dir, ignore_errors=True)

    bad = results.count(False)
    print(f"\n{'全部通过' if bad == 0 else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
