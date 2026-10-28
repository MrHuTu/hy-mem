"""仓库密钥/隐私扫描器——提交前跑一遍，防止把 LLM 密钥带上 GitHub。

为什么要有这个脚本
----------------
仓库里既有「测试脚本」，也有「一次性实验脚本」，后者最容易图省事把本机的
config.json 路径、甚至密钥字面量直接写进源码。人眼在几千行里翻不出一个
51 字符的 token，所以判定必须交给机器，并且**默认从严**：宁可误报让人确认，
也不能漏网——泄漏一次就得轮换密钥 + 改写 git 历史。

判定分三级
--------
ERROR  真的不能进仓库：疑似密钥字面量、config.json 被纳入版本控制
WARN   不该出现但改不改由人定：本机绝对路径、内部主机名、明文 apiKey 字段
OK     没有发现

内部主机名不写死在脚本里
----------------------
扫描器自身也会被公开，所以「哪些主机算内部资产」不能硬编码进来。约定从
`HY_MEM_SECRET_HOSTS` 读（逗号分隔），没设就只做通用规则检查：

    HY_MEM_SECRET_HOSTS="newapi.example.com,10.0.0.5" python scripts/check_secrets.py

用法
----
    python scripts/check_secrets.py            # 扫工作区（推荐提交前跑）
    python scripts/check_secrets.py --staged   # 只扫已 git add 的改动
    python scripts/check_secrets.py --all      # 连未跟踪文件一起扫
    python scripts/check_secrets.py --json

退出码：发现 ERROR 为 1，只有 WARN 为 0（不挡提交，但要人看一眼）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 不扫的目录：虚拟环境、产物、构建中间物、版本库内部、运行时数据
SKIP_DIRS = {
    ".git", ".venv", "venv", "dist", "build", "__pycache__",
    ".mypy_cache", ".pytest_cache", "node_modules", "data", ".models",
    "tmp-hymem-sweep",
    # 本地临时脚本区：整目录被 .gitignore 忽略，永不入库，扫它只会刷屏
    # （里面的探针/实验本来就带本机路径和内部主机名，那是允许的）
    ".local-test",
}
# 不扫的扩展名：二进制/产物不可能是源码泄漏，读进来只有噪声
SKIP_SUFFIXES = {
    ".exe", ".dll", ".pyd", ".so", ".dylib", ".zip", ".7z", ".gz",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp",
    ".sqlite3", ".db", ".whl", ".pyz", ".onnx", ".bin",
}
# 扫描器自己会命中自己的规则字面量，单独排除（规则文本不是密钥）
SELF = Path(__file__).resolve()

# ---- 规则 ---------------------------------------------------------------
# 1) 常见的"像密钥"的长串。前缀不全枚举，靠「熵 + 长度」兜住没见过的厂商。
#    这里刻意不写具体厂商的完整格式，避免把规则变成一份密钥字典。
KEY_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{16,}"), "疑似 API 密钥（sk- 长串）"),
    (re.compile(r"\b(?:api[_-]?key|apikey|secret|token|password|passwd)\b\s*[:=]\s*['\"]([A-Za-z0-9._\-]{20,})['\"]", re.I),
     "疑似硬编码的密钥赋值"),
    (re.compile(r"\bBearer\s+([A-Za-z0-9._\-]{24,})"), "疑似硬编码的 Bearer token"),
]

# 2) 明文密钥字段：配置文件里 apiKey 非空（哪怕值看着像占位符，也让人确认）
APIKEY_FIELD = re.compile(r'"apiKey"\s*:\s*"([^"]*)"')

# 3) 隐私：带用户名的绝对路径（泄漏账号名，比「装在哪个盘」敏感得多）
PRIVACY_PATTERNS = [
    (re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s\"')]+"), "本机用户目录路径"),
    # 前面不能紧挨字母/斜杠，否则 tui.py 里「up/down/home/end/…」这种按键名列表会被误判
    (re.compile(r"(?<![\w/.])/(?:home|Users)/[A-Za-z][\w.-]+/"), "本机用户目录路径"),
]

# 4) 本机绝对路径：只说明「装在哪个盘」，隐私性弱，默认折叠计数、不逐条刷屏
LOCAL_PATH_PATTERNS = [
    (re.compile(r"[A-Za-z]:[\\/](?:tools_open|work|projects|src)[\\/][^\s\"')]+"), "本机绝对路径"),
]

# 占位符/测试用值：这些是故意的假值，允许出现（否则整个测试套件都在报警）
FAKE_MARKERS = (
    "sk-test", "sk-ok", "sk-中文", "sk-super-secret", "sk-xxxx",
    "your-key", "your_key", "placeholder", "<", "***", "...", "${",
)


def _term_width() -> int:
    try:
        return max(60, min(120, os.get_terminal_size().columns))
    except OSError:
        return 100


def _iter_files(staged: bool, scan_all: bool) -> list[Path]:
    if staged:
        try:
            out = subprocess.run(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                cwd=REPO, capture_output=True, text=True, timeout=20,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        return [REPO / line.strip() for line in out.splitlines() if line.strip()]

    files: list[Path] = []
    for root, dirs, names in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith("_hy-mem-old")]
        for name in names:
            p = Path(root) / name
            if p.resolve() == SELF:
                continue
            if p.suffix.lower() in SKIP_SUFFIXES:
                continue
            if not scan_all and _is_ignored(p):
                continue
            files.append(p)
    return files


_ignore_cache: dict[str, bool] = {}


def _is_ignored(p: Path) -> bool:
    """.gitignore 里的文件不算仓库内容（config.json 就在这里）。"""
    try:
        rel = str(p.relative_to(REPO))
    except ValueError:
        return True
    if rel not in _ignore_cache:
        r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO)
        _ignore_cache[rel] = r.returncode == 0
    return _ignore_cache[rel]


def _verdict(kind: str, value: str) -> str:
    low = value.lower()
    if any(m in low for m in FAKE_MARKERS):
        return "ok"
    return kind


def scan_text(text: str, hosts: list[str]) -> list[tuple[str, str, int, str]]:
    """返回 (级别, 说明, 行号, 片段)。片段打码后保留可读性。"""
    hits: list[tuple[str, str, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for pat, desc in KEY_PATTERNS:
            for m in pat.finditer(line):
                raw = m.group(1) if m.groups() else m.group(0)
                if _verdict(desc, raw) != "ok":
                    hits.append(("ERROR", desc, lineno, _mask(raw)))

        for m in APIKEY_FIELD.finditer(line):
            raw = m.group(1)
            # 短值一律当占位符：真实密钥不会只有几个字符，测试里大量
            # "apiKey": "k" / "没有啊" 这类假值，报出来只会淹没真问题。
            if len(raw) >= 8 and _verdict("明文 apiKey 字段", raw) != "ok":
                hits.append(("WARN", "配置里存了明文 apiKey", lineno, _mask(raw)))

        for pat, desc in PRIVACY_PATTERNS:
            m = pat.search(line)
            if m:
                hits.append(("WARN", desc, lineno, m.group(0)))

        for pat, desc in LOCAL_PATH_PATTERNS:
            m = pat.search(line)
            if m:
                # INFO：只是「装在哪个盘」，默认不逐条打印，避免把真问题淹掉
                hits.append(("INFO", desc, lineno, m.group(0)))

        for host in hosts:
            if host and host in line:
                hits.append(("WARN", "出现内部主机名", lineno, host))
    return hits


def _mask(raw: str) -> str:
    raw = raw.strip()
    if len(raw) <= 10:
        return raw[:3] + "***"
    return raw[:6] + "***" + raw[-2:] + f"({len(raw)} 字符)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扫描仓库里的明文密钥与隐私信息")
    ap.add_argument("--staged", action="store_true", help="只扫已 git add 的改动")
    ap.add_argument("--all", action="store_true", help="连 gitignore 的文件一起扫")
    ap.add_argument("--verbose", action="store_true", help="连本机安装路径（INFO）一并列出")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    hosts = [h.strip() for h in os.getenv("HY_MEM_SECRET_HOSTS", "").split(",") if h.strip()]

    files = _iter_files(args.staged, args.all)
    findings: list[dict] = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(p.relative_to(REPO)) if p.is_relative_to(REPO) else str(p)
        for level, desc, lineno, frag in scan_text(text, hosts):
            findings.append({"level": level, "file": rel, "line": lineno,
                             "desc": desc, "snippet": frag})

    errors = [f for f in findings if f["level"] == "ERROR"]
    warns = [f for f in findings if f["level"] == "WARN"]
    infos = [f for f in findings if f["level"] == "INFO"]

    # config.json 被纳入版本控制 = 最严重的一种（0.9.9 之前的真实事故）
    tracked_cfg = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "config.json"],
        cwd=REPO, capture_output=True, text=True,
    )
    if tracked_cfg.returncode == 0:
        errors.append({"level": "ERROR", "file": "config.json", "line": 0,
                       "desc": "config.json 被纳入版本控制（里面是真实密钥）", "snippet": "git rm --cached config.json"})

    if args.json:
        print(json.dumps({"errors": errors, "warnings": warns, "info": infos,
                          "scanned": len(files)}, ensure_ascii=False, indent=1))
        return 1 if errors else 0

    width = _term_width()
    print(f"扫描 {len(files)} 个文件" + (f"，内部主机 {len(hosts)} 个" if hosts else "（未设 HY_MEM_SECRET_HOSTS，跳过主机名检查）"))
    if not errors and not warns:
        print("OK  没有发现明文密钥或用户目录路径")
        if infos:
            print(f"（另有 {len(infos)} 处本机安装路径，属隐私但不敏感；加 --verbose 可看）")
        return 0

    for item in errors:
        loc = f"{item['file']}:{item['line']}" if item["line"] else item["file"]
        line = f"ERROR  {loc}  {item['desc']}  {item['snippet']}"
        print(line[:width])
    for item in warns:
        loc = f"{item['file']}:{item['line']}"
        line = f"WARN   {loc}  {item['desc']}  {item['snippet']}"
        print(line[:width])
    if args.verbose:
        for item in infos:
            loc = f"{item['file']}:{item['line']}"
            print(f"INFO   {loc}  {item['desc']}  {item['snippet']}"[:width])

    print(f"\n合计 ERROR {len(errors)} / WARN {len(warns)} / INFO {len(infos)}")
    if errors:
        print("ERROR 必须清掉再提交：真密钥一律走环境变量，代码里只留变量名。")
        return 1
    print("没有 ERROR：密钥没进仓库。WARN 项确认不是真实资产后即可提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
