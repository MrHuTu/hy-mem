"""运行环境自检 —— 专治「双击闪退」。

为什么需要单独一层：
    用户把 hy-mem.exe 单独拷给别人（没带 `_internal/`），双击后窗口一闪就没了。
    那不是环境问题，也不是缺 Python —— 缺的是**打包出来的那半个运行时**。
    PyInstaller 的 `--onedir` 模式里，`_internal/` 才是真正的 Python（解释器
    dll、标准库、chromadb、onnxruntime 全在里面），exe 只是个引导器。引导器
    发现 dll 不在，直接在 `LoadLibrary` 阶段退出，**一行 Python 代码都不会执行**
    —— 所以 Python 里的任何 try/except 都拦不住它。

结论是必须分两层防：
    1. 外层：`启动 hy-mem.cmd`（构建时生成并放进产物/压缩包）。它能在 Python
       起来之前检查 `_internal/` 是否存在，缺了就中文提示 + `pause`，窗口不会消失。
    2. 内层：本模块（Python 已起来之后）。检查 Windows 版本、位数、安装目录可写、
       运行库、磁盘空间，并把「怎么修、去哪下载」写清楚。

两条纪律：
  * 本模块不 import 项目内任何其它模块（config / store / tui 都不行）——
    它要在「什么都还没初始化」的时候就能跑。
  * 任何函数都不许抛异常。它是最后一道兜底，自己崩了就没意义了。
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import time
from pathlib import Path

# Python 3.12 官方支持的最低系统：Windows 8.1 / Server 2012 R2（版本号 6.3）
MIN_WIN = (6, 3)
MIN_DISK_MB = 300

# 修复指引里用到的下载地址。国内优先用镜像 —— 官方站点在部分网络下会超时，
# 而这几个镜像在国内长期稳定。
MIRRORS = {
    "python": "https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe",
    "python_index": "https://mirrors.huaweicloud.com/python/",
    "pypi": "https://pypi.tuna.tsinghua.edu.cn/simple",
    "vc": "https://aka.ms/vs/17/release/vc_redist.x64.exe",
}

# 冻结产物里必须存在的运行时标志物。少任何一个，exe 双击必闪退。
_REQUIRED = ("base_library.zip", "chromadb")


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """安装根：冻结后是 exe 所在目录，源码模式是 cli/ 的上一级。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def internal_dir() -> Path:
    return app_root() / "_internal"


def log_path() -> Path:
    """启动日志落点。优先用户目录（安装目录常常不可写），其次安装目录。"""
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        first = Path(base) / "hy-mem" / "logs"
    else:
        first = Path.home() / ".hy-mem" / "logs"
    for cand in (first, app_root() / "logs"):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".probe"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            return cand / "startup.log"
        except Exception:  # noqa: BLE001
            continue
    return Path(os.getenv("TEMP") or "/tmp") / "hy-mem-startup.log"


def write_log(text: str) -> Path | None:
    """追加一段启动日志。永不抛异常 —— 写不了就算了。"""
    try:
        p = log_path()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n===== {stamp} =====\n{text.rstrip()}\n")
        return p
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# 单项探测
# --------------------------------------------------------------------------
def windows_version() -> tuple[int, int, int] | None:
    """(major, minor, build)。非 Windows 或取不到返回 None。"""
    try:
        v = sys.getwindowsversion()  # type: ignore[attr-defined]
        return (int(v.major), int(v.minor), int(v.build))
    except Exception:  # noqa: BLE001
        return None


def _writable(d: Path) -> tuple[bool, str]:
    """探测 d 所在位置能不能写。

    刻意**不创建** d 本身：自检要在每条命令前跑，顺手造出一个空 data/ 目录
    会让「产物是否干净」这类检查失真，也会在只读部署里留下垃圾。找不到 d
    就向上找到最近的已存在祖先再试写。
    """
    target = d
    for _ in range(6):
        if target.exists():
            break
        if target.parent == target:  # 到根了
            break
        target = target.parent
    try:
        # 探针名带 pid：并发启动（多子代理各拉起各的进程）时同名探针会互相
        # 踩——A 进程 unlink 的瞬间 B 进程正在写同一文件，Windows 下直接
        # PermissionError，被误判成「目录不可写」拒绝启动（并发压测实测 6 进
        # 程，1/6 因此 rc=2）。唯一文件名消除竞态，探测语义不变。
        probe = target / f".hm_write_probe.{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, str(d)
    except Exception as e:  # noqa: BLE001
        return False, f"{d}: {type(e).__name__}: {e}"


def _has_python_dll(internal: Path) -> bool:
    try:
        return any(internal.glob("python3*.dll"))
    except Exception:  # noqa: BLE001
        return False


def _vcruntime_ok() -> tuple[bool, str]:
    """VC++ 运行库能否加载。C 扩展（chromadb / onnxruntime）都依赖它。"""
    if os.name != "nt":
        return True, "非 Windows，跳过"
    try:
        import ctypes

        ctypes.WinDLL("vcruntime140.dll")  # type: ignore[attr-defined]
        return True, "vcruntime140.dll 可加载"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _free_mb(d: Path) -> int:
    try:
        return int(shutil.disk_usage(str(d)).free / 1048576)
    except Exception:  # noqa: BLE001
        return -1


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def checks(full: bool = True) -> list[dict]:
    """返回检查项列表。`full=False` 只做廉价的必检项（每次启动都跑）。

    每项：{"item", "ok", "detail", "fix", "fatal", "url"}
    `fatal=False` 表示「不理想但不该判定为环境不可用」。
    """
    out: list[dict] = []

    def add(item: str, passed: bool, detail: str, fix: str = "", fatal: bool = True, url: str = "") -> None:
        out.append({"item": item, "ok": bool(passed), "detail": detail, "fix": fix, "fatal": bool(fatal), "url": url})

    # 1) 系统版本 —— Python 3.12 运行时不支持 Win8 以下
    if os.name == "nt":
        v = windows_version()
        if v is None:
            add("windows", False, "无法识别 Windows 版本", "系统版本读不出来，换一台机器试试。", url=MIRRORS["python_index"])
        else:
            good = (v[0], v[1]) >= MIN_WIN
            add(
                "windows",
                good,
                f"Windows {v[0]}.{v[1]}（build {v[2]}）",
                "需要 Windows 8.1 / Server 2012 R2 及以上；更低的系统装不了 Python 3.12 运行时，只能升级系统或换机。",
            )
    else:
        add("windows", True, f"{sys.platform}（源码模式，不做 Windows 版本限制）", fatal=False)

    # 2) 位数 —— 产物是 64 位，32 位 Python 跑不起来
    bits = struct.calcsize("P") * 8
    add(
        "arch",
        bits == 64,
        f"{bits} 位运行时",
        "需要 64 位 Windows。32 位系统得用 32 位产物（在 32 位环境重新构建），或换机。",
    )

    # 3) 包完整性 —— 这一项就是「只拷了 exe」的直接检测
    if is_frozen():
        internal = internal_dir()
        missing: list[str] = []
        if not internal.is_dir():
            missing.append("_internal/（整个目录）")
        else:
            if not _has_python_dll(internal):
                missing.append("_internal/python3xx.dll")
            for name in _REQUIRED:
                if not (internal / name).exists():
                    missing.append(f"_internal/{name}")
        add(
            "package",
            not missing,
            f"{app_root()}（缺失：{', '.join(missing)}）" if missing else str(app_root()),
            "只拷了 hy-mem.exe。`_internal/` 才是真正的 Python 运行时，必须和 exe 放在一起。"
            "把整个文件夹（或整个压缩包解压后的目录）一起拷走，不要单独拖 exe 出来。",
        )
    else:
        add("package", True, "源码模式，跳过产物完整性检查", fatal=False)

    # 4) 可写目录 —— 数据目录默认就在安装根旁边，不可写等于全部功能不可用。
    #    先看 MEMORY_DATA_DIR：装在只读位置（Program Files）但数据目录另指的
    #    情况是合法的，不能误判成环境不可用。
    dd = Path(os.getenv("MEMORY_DATA_DIR") or (app_root() / "data"))
    ok_dir, detail = _writable(dd)
    add(
        "writable",
        ok_dir,
        detail,
        "把整个目录放到可写位置（例如 D:\\hy-mem 或桌面），不要放 C:\\Program Files、"
        "光盘、压缩包内直接双击；若被杀毒软件锁定，把该目录加入信任；"
        "也可以设 MEMORY_DATA_DIR 把数据目录指到别处（写入/检索仍可用）。",
    )

    if not full:
        return out

    # 5) VC++ 运行库
    vc_ok, vc_detail = _vcruntime_ok()
    add(
        "vcruntime",
        vc_ok,
        vc_detail,
        "安装「Microsoft Visual C++ 2015-2022 Redistributable (x64)」："
        f"{MIRRORS['vc']}（微软官方短链，国内一般可直接下；慢的话用腾讯软件中心/"
        "华为云镜像站搜同名运行库）。本包通常已内置，缺了才需要装。",
        fatal=False,
        url=MIRRORS["vc"],
    )

    # 6) 控制台 —— 被 GUI 方式拉起时 stdout 可能是 None，输出全丢
    console_ok = sys.stdout is not None and sys.stderr is not None
    add(
        "console",
        console_ok,
        "标准输出可用" if console_ok else "stdout/stderr 不可用（被当 GUI 程序拉起？）",
        "在 cmd / PowerShell 里运行，或双击「启动 hy-mem.cmd」，不要用不带控制台的方式调用。",
        fatal=False,
    )

    # 7) 磁盘余量 —— 数据目录要长期增长
    free = _free_mb(app_root())
    add(
        "disk",
        free < 0 or free >= MIN_DISK_MB,
        f"可用 {free} MB" if free >= 0 else "无法读取磁盘余量",
        f"数据目录至少预留 {MIN_DISK_MB} MB；清理磁盘后重试。",
        fatal=False,
    )

    # 8) 运行时版本（纯信息，永远 PASS）
    add(
        "python",
        True,
        f"{sys.version.split()[0]}（{'冻结' if is_frozen() else '源码'}）",
        fatal=False,
    )
    return out


def ok(items: list[dict]) -> bool:
    """环境是否可用：只看 fatal 项。"""
    return all(c["ok"] for c in items if c.get("fatal", True))


def failures(items: list[dict], fatal_only: bool = True) -> list[dict]:
    return [c for c in items if not c["ok"] and (c.get("fatal", True) or not fatal_only)]


def report(items: list[dict]) -> str:
    """人类可读报告。全部通过只回一行；有失败就逐条给「怎么修」。"""
    bad = failures(items, fatal_only=False)
    if not bad:
        return f"运行环境自检通过（{len(items)} 项）"
    lines = [f"运行环境自检发现问题（{len(bad)} 项需要处理）："]
    for c in bad:
        tag = "阻塞" if c.get("fatal", True) else "提醒"
        lines.append("")
        lines.append(f"  [{tag}] {c['item']}: {c['detail']}")
        if c.get("fix"):
            lines.append(f"         怎么修：{c['fix']}")
    if any(c.get("fatal", True) for c in bad):
        lines.append("")
        lines.append("  上面标「阻塞」的问题不解决，写入和检索都无法进行。")
    return "\n".join(lines)


def interactive() -> bool:
    """是不是「人在终端里」——只有这样才能安全地等按键。

    智能体 / 管道调用时 stdin 不是终端，等按键会直接把调用方卡死。
    """
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


def wait_key(prompt: str = "按回车键退出…") -> None:
    try:
        input(prompt)
    except Exception:  # noqa: BLE001
        pass
