# -*- coding: utf-8 -*-
"""中英切换（i18n）测试 —— 零网络，沙箱配置。

要守住的三条：
1. **默认中文时输出逐字不变**。文案包了 tr() 之后，中文界面必须和改造前完全
   一致（测试里断言了大量中文字面量，一个字都不能飘）。
2. `HY_MEM_LANG=en` 时 tr() 真的生效，且缺译回落中文（绝不报错、绝不露出 key）。
3. `hy-mem config set lang=en` 之后 lang() 立刻读到 en（含 mtime 缓存失效）。

跑法：.venv/Scripts/python.exe scripts/test_i18n.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "cli" / "hy_mem_cli.py"
sys.path.insert(0, str(REPO))

_SANDBOX = Path(tempfile.mkdtemp(prefix="hymem-i18n-"))
os.environ["MEMORY_DATA_DIR"] = str(_SANDBOX / "data")
os.environ["HY_MEM_CONFIG"] = str(_SANDBOX / "config.json")
os.environ.pop("HY_MEM_LANG", None)

from cli.core import errors  # noqa: E402
from cli.ui import i18n  # noqa: E402
from cli.ui import menu  # noqa: E402

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(bool(passed))
    mark = "PASS" if passed else "FAIL"
    print(f"  {mark}  {name}" + (f"  → {detail}" if detail else ""))


def run(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CLI)] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(REPO), timeout=120,
    )


def case_zh_identical() -> None:
    print("\n默认中文：与硬编码中文逐字一致")
    os.environ.pop("HY_MEM_LANG", None)
    check("lang() 默认是 zh", i18n.lang() == "zh", i18n.lang())
    for s in ("检索", "写记忆", "队列", "标签", "设置", "帮助"):
        check(f"tr({s!r}) 原样返回", i18n.tr(s) == s, i18n.tr(s))
    check("带占位符的中文照常格式化",
          i18n.tr("共 {} 条；删除用 hy-mem delete <id>", 3) == "共 3 条；删除用 hy-mem delete <id>",
          i18n.tr("共 {} 条；删除用 hy-mem delete <id>", 3))

    # 控制台表头是改造中最容易改歪的一块（中文标签 + 按键提示拼接）
    got = menu._banner_text()
    check("表头提示行是中文", "help 看命令" in got and "Tab 补全" in got, got)
    check("未知命令提示是中文",
          i18n.tr("未知命令 {}", "xyzzy") == "未知命令 xyzzy", i18n.tr("未知命令 {}", "xyzzy"))
    check("退出提示是中文", i18n.tr("退出。") == "退出。", i18n.tr("退出。"))
    check("表格表头仍是中文", i18n.tr("分数") == "分数" and i18n.tr("标签") == "标签",
          i18n.tr("分数"))
    check("身份标签仍是「通用（default）」",
          i18n.tr("通用（{}）", "default") == "通用（default）",
          i18n.tr("通用（{}）", "default"))

    hint = errors.classify_error(RuntimeError("boom"))[2]
    check("errors 提示仍是中文", "hy-mem doctor" in hint and "先跑" in hint, hint)


def case_en() -> None:
    print("\nHY_MEM_LANG=en：tr 生效")
    os.environ["HY_MEM_LANG"] = "en"
    try:
        check("lang() 读到 env", i18n.lang() == "en", i18n.lang())
        check("检索 → Search", i18n.tr("检索") == "Search", i18n.tr("检索"))
        check("占位符跟随译文",
              i18n.tr("共 {} 条；删除用 hy-mem delete <id>", 2) == "2 total; delete with hy-mem delete <id>",
              i18n.tr("共 {} 条；删除用 hy-mem delete <id>", 2))
        banner = menu._banner_text()
        check("表头提示行变英文", "help for commands" in banner and "help 看命令" not in banner, banner)
        check("未知命令提示变英文",
              i18n.tr("未知命令 {}", "x") == "Unknown command x", i18n.tr("未知命令 {}", "x"))
        check("退出提示变英文", i18n.tr("退出。") == "Bye.", i18n.tr("退出。"))
        check("缺译回落中文", i18n.tr("这句确实没人翻译过") == "这句确实没人翻译过",
              i18n.tr("这句确实没人翻译过"))
        check("缺译不抛异常（带参数也不炸）",
              i18n.tr("没人翻译过的 {}", 1) == "没人翻译过的 1", i18n.tr("没人翻译过的 {}", 1))
        hint = errors.classify_error(RuntimeError("boom"))[2]
        check("errors 提示切英文", "Run `hy-mem doctor` first" in hint, hint)

        p = run(["version"])
        check("CLI 输出也是英文", "(source mode)" in p.stdout, p.stdout.splitlines()[0] if p.stdout else "")
    finally:
        os.environ.pop("HY_MEM_LANG", None)


def case_config_set() -> None:
    print("\nconfig set lang=en：写配置并立即生效")
    check("缓存预热（当前 zh）", i18n.lang() == "zh", i18n.lang())
    p = run(["config", "set", "lang=en"])
    check("config set 成功", p.returncode == 0, (p.stderr or "").strip()[:120])
    check("lang() 读到 en（mtime 缓存已失效）", i18n.lang() == "en", i18n.lang())
    check("tr 跟着变", i18n.tr("设置") == "Settings", i18n.tr("设置"))

    p = run(["config", "set", "lang=zh"])
    check("切回中文", p.returncode == 0 and i18n.lang() == "zh", i18n.lang())

    # 非法值不能偷偷生效：回落 zh，但 set_lang 会明确拒绝
    p = run(["config", "set", "lang=fr"])
    check("非法 lang 不生效（回落 zh）", i18n.lang() == "zh", i18n.lang())
    raised = ""
    try:
        i18n.set_lang("fr")
    except ValueError as e:
        raised = str(e)
    check("set_lang 拒绝非法值", "lang" in raised, raised)
    run(["config", "set", "lang=zh"])


def main() -> int:
    print(f"沙箱：{_SANDBOX}")
    case_zh_identical()
    case_en()
    case_config_set()
    bad = results.count(False)
    print(f"\n{'全部通过' if not bad else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
