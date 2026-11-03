"""上游 SDK 补丁的幂等应用器（hy-mem 专用）。

背景见 docs/UPSTREAM-ISSUES.md（U1）与 docs/specs/2026-11-02-ultra-graph-degrade/。
build_exe.py 打包前调用本脚本，保证 pip 重装 venv 后补丁不丢；也可手动跑：

    .venv/Scripts/python.exe scripts/apply_upstream_patches.py

设计：
- 每个 patch = (唯一标记, 上游原文, 补丁后文本)；对目标文件做精确文本替换。
- 幂等：文件已含标记 → 跳过；原文不匹配且无标记 → 报错（上游代码变了，人工核对）。
- 落点 ×2：venv site-packages（PyInstaller 打包实际来源）+ vendor 快照（仓库留档）。
- `--check` 只读核对，构建脚本在应用后必须再跑一次；退出码非 0 即中止打包。
- vendor 子模块因此会长期显示 ` m vendor/hy-memory`：这是预期状态——补丁不属于上游，
  也不能提交进子模块（无推送权限）；任何环境重跑本脚本即可复现，无需人工改文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# PATCH-1（U1）：GraphStore 锁冲突降级续命
# 上游 kuzu 层已降级 no-op（_available=False，全部方法有守卫），client 层却
# 主动 raise——第二个 ultra client 进程构造必炸。改为 warning 放行降级。
# 上游修复后退役本条目（验证方法见 UPSTREAM-ISSUES.md U1）。
# ---------------------------------------------------------------------------
PATCH_GRAPH_DEGRADE_MARKER = "[hy-mem patch 2026-11-02 graph-degrade]"

GRAPH_DEGRADE_BEFORE = """            # 检查图数据库是否真正可用（Kuzu 会静默降级为 no-op）
            if hasattr(self._graph_store, '_available') and not self._graph_store._available:
                raise RuntimeError(
                    f"GraphStore ({self._config.graph_store.provider}) init failed. "
                    f"Use mode='pro' instead of mode='ultra' to disable Graph, or check logs for details."
                )
"""

GRAPH_DEGRADE_AFTER = """            # [hy-mem patch 2026-11-02 graph-degrade] 上游 bug 规避（U1，见 docs/UPSTREAM-ISSUES.md）：
            # kuzu 层已降级 no-op（_available=False，所有运行期方法安全返回 None），
            # 原实现在此 raise 导致第二个 ultra client 进程构造必炸（多进程场景全灭）。
            # 改为 warning 放行降级：事实/向量/检索路径不受影响，仅本进程图能力 no-op。
            # 上游修复本问题后请退役此补丁并重装 SDK。
            if hasattr(self._graph_store, '_available') and not self._graph_store._available:
                logger.warning(
                    f"GraphStore ({self._config.graph_store.provider}) init failed — "
                    "degrading to no-op (fact/vector paths unaffected; graph features "
                    "disabled in this process). Another process likely holds the kuzu lock."
                )
"""

PATCHES = [
    {
        "name": "graph-degrade",
        "marker": PATCH_GRAPH_DEGRADE_MARKER,
        "before": GRAPH_DEGRADE_BEFORE,
        "after": GRAPH_DEGRADE_AFTER,
        "targets": [
            ROOT / ".venv" / "Lib" / "site-packages" / "hy_memory" / "client.py",
            ROOT / "vendor" / "hy-memory" / "hy_memory" / "client.py",
        ],
    },
]


def apply_one(target: Path, patch: dict) -> str:
    """返回 'applied' / 'skipped' / 'conflict' / 'mismatch' / 'missing'。"""
    if not target.exists():
        return "missing"
    text = target.read_text(encoding="utf-8")
    if patch["marker"] in text and patch["before"] not in text:
        return "skipped"
    if patch["before"] in text:
        if patch["marker"] in text:
            # 标记与上游原文同时存在：文件被手工改过，自动替换有歧义，交人工。
            return "conflict"
        target.write_text(text.replace(patch["before"], patch["after"], 1), encoding="utf-8")
        return "applied"
    return "mismatch"


def check_one(target: Path, patch: dict) -> str:
    """只读核对补丁是否真的落地：'patched' / 'unpatched' / 'missing' / 'revertible'。

    'unpatched' = 含上游原文、可被本脚本安全补上；'revertible' = 既无标记也无
    原文（上游已改，需人工核对）。两者都不算通过。
    """
    if not target.exists():
        return "missing"
    text = target.read_text(encoding="utf-8")
    if patch["marker"] in text and patch["before"] not in text:
        return "patched"
    if patch["before"] in text:
        return "unpatched"
    return "revertible"


def _no_patch_text(status: str) -> str:
    return {
        "missing": "文件缺失（vendor 子模块未初始化？）",
        "unpatched": "补丁未应用（含上游原文）",
        "revertible": "上游原文与补丁标记都不存在——上游代码已变更，请人工核对 docs/UPSTREAM-ISSUES.md",
        "conflict": "补丁标记与上游原文同时存在（文件被手工改过），请人工核对",
        "mismatch": "上游原文不匹配且无补丁标记——上游代码已变更，请人工核对 docs/UPSTREAM-ISSUES.md",
    }[status]


def run_check() -> int:
    failures = 0
    for patch in PATCHES:
        for target in patch["targets"]:
            rel = target.relative_to(ROOT)
            status = check_one(target, patch)
            if status == "patched":
                print(f"[patch:check] {patch['name']} → {rel}：已生效")
            else:
                print(f"[patch:check] {patch['name']} → {rel}：{_no_patch_text(status)}")
                failures += 1
    return 1 if failures else 0


def main() -> int:
    if "--check" in sys.argv[1:]:
        return run_check()
    failures = 0
    for patch in PATCHES:
        for target in patch["targets"]:
            rel = target.relative_to(ROOT)
            result = apply_one(target, patch)
            if result == "applied":
                print(f"[patch] {patch['name']} → {rel}：已应用")
            elif result == "skipped":
                print(f"[patch] {patch['name']} → {rel}：已存在，跳过")
            else:
                print(f"[patch] {patch['name']} → {rel}：{_no_patch_text(result)}")
                failures += 1
    # 写完立刻复核：宁可在这里硬失败，也不要让「以为打过、其实没打」混进 exe。
    return run_check() if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
