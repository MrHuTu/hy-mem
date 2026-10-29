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
    p1 = run(["--json", "config", "path"])
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

    bad = results.count(False)
    print(f"\n{'全部通过' if bad == 0 else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
