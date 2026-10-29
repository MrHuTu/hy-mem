# -*- coding: utf-8 -*-
"""操作日志（ops.log）测试 —— 只碰沙箱目录，零网络。

覆盖三件必须成立的事：
1. 只保留最近 50 条，且保留的是**最新**的（追加后截尾，不是丢新留旧）；
2. 密钥类参数打码、正文/查询词不落盘（日志不该成为记忆内容的第二份副本）；
3. 日志目录写不进去时不抛异常 —— 日志是附加能力，不能连累命令本身。

跑法：.venv/Scripts/python.exe scripts/test_oplog.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 必须在 import oplog/config 之前定好沙箱：oplog.path() 读的是 config.load()
_SANDBOX = Path(tempfile.mkdtemp(prefix="hymem-oplog-"))
os.environ["MEMORY_DATA_DIR"] = str(_SANDBOX / "data")
os.environ["HY_MEM_CONFIG"] = str(_SANDBOX / "config.json")
os.environ.pop("HY_MEM_LANG", None)

from cli.core import oplog  # noqa: E402

results: list[bool] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append(bool(passed))
    mark = "PASS" if passed else "FAIL"
    print(f"  {mark}  {name}" + (f"  → {detail}" if detail else ""))


def _reset() -> None:
    """每个用例从空日志开始，避免相互污染。"""
    oplog.clear()


def case_keep_latest() -> None:
    print("\n只保留最近 50 条（且留下的是最新的）")
    _reset()
    for i in range(60):
        oplog.record(["search", "查询词不该进日志", "-n", "3"], 0, 0.12, extra=f"#{i:02d}")
    lines = oplog.recent(oplog.MAX_KEEP)
    text = "\n".join(lines)
    check("上限常量为 50", oplog.MAX_KEEP == 50, str(oplog.MAX_KEEP))
    check("文件里正好 50 行", len(lines) == 50, f"{len(lines)} 行")
    check("最旧的两条已被淘汰", "#07" not in text and "#09" not in text)
    check("分界处保留 #10", "#10" in text)
    check("最新一条是 #59", "#59" in text)
    check("顺序与写入一致（旧→新）", lines[-1].rstrip().endswith("#59") if lines else False)

    # 再写一条：最老的 #10 应该被挤掉，总数仍是 50
    oplog.record(["queue"], 0, 0.01, extra="#60")
    lines2 = oplog.recent(oplog.MAX_KEEP)
    check("再写一条仍是 50 行", len(lines2) == 50, f"{len(lines2)} 行")
    check("#10 被挤掉", "#10" not in "\n".join(lines2))
    check("#60 已在最末", lines2[-1].rstrip().endswith("#60"))


def case_mask() -> None:
    print("\n脱敏：密钥类参数打码，正文与查询词不落盘")
    _reset()
    oplog.record(["config", "set", "embedding.apiKey=sk-super-secret"], 0, 0.01)
    oplog.record(["config", "set", "--tag", "work"], 0, 0.01)
    oplog.record(["add", "--async", "--tag", "work", "--", "用户的私密正文"], 0, 0.01)
    oplog.record(["search", "用户的私密查询", "-n", "5"], 0, 0.01)
    oplog.record(["get", "3f9a2c1b"], 0, 0.01)
    text = "\n".join(oplog.recent())
    check("apiKey 的值被打码", "apiKey=***" in text and "sk-super-secret" not in text)
    check("--tag 不被误判成密钥", "--tag work" in text)
    check("add 的正文不落盘", "用户的私密正文" not in text and "add --async --tag work" in text)
    check("search 的查询词不落盘", "用户的私密查询" not in text and "search -n 5" in text)
    check("id 这类定位信息保留", "3f9a2c1b" in text)


def case_failure_line() -> None:
    print("\n失败行：状态 / 错误码 / 摘要")
    _reset()
    oplog.record(["search", "-n", "3"], 2, 0.4, error="E_ENDPOINT 向量库服务异常：连不上")
    line = oplog.recent()[0]
    e = oplog.parse(line)
    check("状态是 FAIL", e["status"] == "FAIL", e["status"])
    check("摘要含错误码", "E_ENDPOINT" in e["summary"], e["summary"])
    check("耗时写成秒", e["duration"].endswith("s"), e["duration"])
    check("命令列是子命令", e["cmd"] == "search", e["cmd"])

    oplog.record(["list", "-n", "5"], 0, 0.02)
    e2 = oplog.parse(oplog.recent()[-1])
    check("成功行状态是 OK", e2["status"] == "OK", e2["status"])

    # 一行被手改坏时，parse 不能抛（日志页会整体崩掉）
    bad = oplog.parse("这行不是日志格式")
    check("坏行落进 summary 而不抛异常", bad["summary"] == "这行不是日志格式", bad["summary"])


def case_unwritable() -> None:
    print("\n日志目录不可写时不抛异常（不影响命令本身）")
    blocker = _SANDBOX / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    old = os.environ.get("MEMORY_DATA_DIR")
    # 目录的父级是个文件：mkdir 必然失败，正好模拟「写不进去」
    os.environ["MEMORY_DATA_DIR"] = str(blocker / "data")
    try:
        raised = ""
        try:
            oplog.record(["add", "x"], 0, 0.01)
        except Exception as e:  # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
        check("record 静默失败", raised == "", raised)

        raised = ""
        try:
            oplog.clear()
        except Exception as e:  # noqa: BLE001
            raised = f"{type(e).__name__}: {e}"
        check("clear 抛给调用方（由命令自己决定怎么说）", "blocker" in raised, raised)
    finally:
        if old is None:
            os.environ.pop("MEMORY_DATA_DIR", None)
        else:
            os.environ["MEMORY_DATA_DIR"] = old


def case_path() -> None:
    print("\n路径与清空")
    p = oplog.path()
    check("日志落在数据目录下的 ops.log", p.name == "ops.log" and "data" in str(p), str(p))
    _reset()
    check("clear 之后为空", oplog.recent() == [], str(len(oplog.recent())))
    check("clear 之后文件仍存在", p.exists(), str(p))


def main() -> int:
    print(f"沙箱：{_SANDBOX}")
    case_keep_latest()
    case_mask()
    case_failure_line()
    case_unwritable()
    case_path()
    bad = results.count(False)
    print(f"\n{'全部通过' if not bad else f'{bad} 项失败'}（{len(results) - bad}/{len(results)}）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
