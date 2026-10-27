"""doctor.py — skill 漂移检测（只读）

对比 skills-manager 中央库与各宿主 skills 目录，输出 orphan / missing 清单。
退出码：0 = 无孤儿文件；1 = 存在孤儿；2 = 中央库为空或不可读。

用法：
    python doctor.py
    python doctor.py --json
"""
import json
import pathlib
import sys

HOSTS = {
    "workbuddy": pathlib.Path.home() / ".workbuddy" / "skills",
    "qoder": pathlib.Path.home() / ".qoder" / "skills",
    "trae_cn": pathlib.Path.home() / ".trae-cn" / "skills",
    "claude_code": pathlib.Path.home() / ".claude" / "skills",
    "trae": pathlib.Path.home() / ".trae" / "skills",
}
LIBRARY = pathlib.Path.home() / ".skills-manager" / "skills"


def names(path):
    if not path.exists():
        return None
    return {d.name for d in path.iterdir() if d.is_dir()}


def main():
    as_json = "--json" in sys.argv

    lib = names(LIBRARY)
    if lib is None:
        print(f"中央库不存在：{LIBRARY}", file=sys.stderr)
        return 2
    if not lib:
        print(f"中央库为空：{LIBRARY}", file=sys.stderr)
        return 2

    drift = {}
    has_orphan = False
    for key, path in HOSTS.items():
        host = names(path)
        if host is None:
            drift[key] = {"exists": False}
            continue
        orphan = sorted(host - lib)
        missing = sorted(lib - host)
        drift[key] = {
            "exists": True,
            "total": len(host),
            "orphan": orphan,
            "missing_count": len(missing),
        }
        if orphan:
            has_orphan = True

    report = {
        "library": {"path": str(LIBRARY), "total": len(lib)},
        "hosts": drift,
        "verdict": "DRIFT" if has_orphan else "CLEAN",
    }

    if as_json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(f"中央库 {len(lib)} 个：{LIBRARY}")
        for key, info in drift.items():
            if not info.get("exists"):
                print(f"  {key:<12} 未安装")
                continue
            mark = "DRIFT" if info["orphan"] else "ok"
            print(
                f"  {key:<12} {info['total']:>3} 个  "
                f"孤儿 {len(info['orphan']):>2}  库中缺失 {info['missing_count']:>2}  [{mark}]"
            )
            if info["orphan"]:
                print(f"      孤儿: {', '.join(info['orphan'][:8])}"
                      + (" …" if len(info["orphan"]) > 8 else ""))
        print(f"\n结论：{report['verdict']}")

    return 1 if has_orphan else 0


if __name__ == "__main__":
    sys.exit(main())
