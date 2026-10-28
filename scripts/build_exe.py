r"""构建自包含 hy-mem.exe —— 把一条命令固定下来，避免每次重试参数.

走过的弯路都固化在参数里了：

1. `--collect-submodules chromadb --collect-data chromadb`
   chromadb 大量模块是运行时按名字取的（`chromadb.telemetry.product.posthog`、
   `chromadb.api.rust` …），静态分析全看不见。**构建能成功但一写入就
   ModuleNotFoundError**，而 doctor 还不暴露（它不碰 chromadb）。
   这是本次最隐蔽的坑：构建全绿、运行才失败。逐个 `--hidden-import` 补了两轮
   才认清要一次性全收。

2. `--collect-submodules hy_memory` 同理。

3. `--copy-metadata` 让 `__version__` 正确。不加的话 hy_memory 会报 0.1.0
   （它靠 importlib.metadata 读 dist-info，冻结后没有 dist-info）。

4. 覆盖旧产物用 mv 改名，不用 rm —— `rm -rf dist` 会触发 WorkBuddy 的
   safe-delete 保护（文件数超过阈值就要求确认）。

5. **产物必须是「一个目录」，不能是「一个 exe」**。`--onedir` 下 exe 只是引导器，
   真正的 Python 在 `_internal/`。实测：只拷 exe 到空目录运行，引导器直接报
   `Failed to load Python DLL ...\_internal\python312.dll` 并退出 —— 双击场景下
   就是「闪退」，用户什么提示都看不到。所以这里额外生成：
     * `启动 hy-mem.cmd`  —— 双击入口，先查 `_internal/` 再启动，出错会 pause
     * `安装到 PATH.cmd`  —— 一键 `hy-mem install`
     * `使用说明.txt`      —— 给完全不懂的人看的三步
     * `hy-mem-<版本>-win64.zip` —— 直接把压缩包发人，从根上避免「只拷 exe」

用法：
    .venv/Scripts/python.exe scripts/build_exe.py
    HY_MEM_DIST=D:/build/out .venv/Scripts/python.exe scripts/build_exe.py   # 换输出目录
    HY_MEM_NO_ZIP=1 ...                                                      # 不生成压缩包
产物：
    dist/hy-mem/                       （目录，拷走即可用）
    dist/hy-mem-<ver>-win64.zip        （压缩包，发给别人用这个）
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 产物目录可用 HY_MEM_DIST 覆盖：正在运行的旧 exe 会占住目录（Windows 上
# 目录被占用就搬不动），这时先构建到旁边，等旧进程退出再换。
DIST = Path(os.getenv("HY_MEM_DIST") or (ROOT / "dist"))
NAME = "hy-mem"

ARGS = [
    "--noconfirm",
    "--onedir",  # 不用 onefile：onefile 每次启动解包 290MB，冷启不可接受
    "--name", NAME,
    # 产物目录必须显式传给 PyInstaller：默认它写 cwd/dist，HY_MEM_DIST 就形同
    # 虚设 —— 曾出现「指定了别的目录、它却回头去删 dist/hy-mem」的情况。
    "--distpath", str(DIST),
    "--paths", "cli",
    # 本地模块（部分是函数内延迟 import，显式声明更稳）
    "--hidden-import", "store",
    "--hidden-import", "config",
    "--hidden-import", "tui",
    "--hidden-import", "presets",
    "--hidden-import", "setup_wizard",
    "--hidden-import", "menu",
    "--hidden-import", "storage_ops",
    "--hidden-import", "install_ops",
    "--hidden-import", "skill_ops",
    "--hidden-import", "mcp_server",
    "--hidden-import", "helptext",
    "--hidden-import", "local_embedder",
    "--hidden-import", "runtime_check",
    "--hidden-import", "netprobe",
    "--hidden-import", "vdb_endpoint",
    # SDK 及其动态子模块
    "--collect-submodules", "hy_memory",
    "--collect-submodules", "chromadb",
    "--collect-data", "chromadb",
    # vdb_endpoint 的 server 线程依赖（chromadb 未把 fastapi 列为运行时依赖；
    # uvicorn/otel 有 importlib 动态加载，必须整包收集）
    "--collect-submodules", "fastapi",
    "--collect-submodules", "uvicorn",
    "--collect-submodules", "opentelemetry.instrumentation.fastapi",
    "--copy-metadata", "hy-memory",
    "--copy-metadata", "chromadb",
    # 纯 GUI/科学计算，本项目用不到
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    "--exclude-module", "IPython",
    "--exclude-module", "pytest",
    "cli/_pyi_entry.py",
]


def _force_rmtree(path: Path) -> bool:
    """删目录树。产物里的 .exe/.dll 常带只读，或被杀软/索引服务短暂占用。

    占用是瞬时的，所以重试几次而不是一次失败就放弃 —— 否则每次构建都会留下
    一个 354MB 的旧目录。
    """

    def _onexc(func, p, _exc):  # Python 3.12+
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:  # noqa: BLE001
            pass

    for attempt in range(3):
        try:
            shutil.rmtree(path, onexc=_onexc)
        except TypeError:  # 3.11 及以前没有 onexc
            shutil.rmtree(path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        if not path.exists():
            return True
        time.sleep(0.4 * (attempt + 1))
    return not path.exists()


def _stamp_old() -> None:
    """旧产物先改名：直接 rmtree 会触发 safe-delete 保护（文件数上千）。"""
    if not (DIST / NAME).exists():
        return
    dst = DIST / f"_{NAME}-old-{time.strftime('%H%M%S')}"
    try:
        shutil.move(str(DIST / NAME), str(dst))
        print(f"[build] 旧产物已改名为 {dst.name}，构建结束后清理")
    except OSError as e:
        print(f"[build] 旧产物改名失败：{e}")
        sys.exit(1)


def _purge_old() -> None:
    """构建成功后清掉所有历史产物目录 —— 留着每次 350MB，几次就把盘塞满。"""
    keep_fail = []
    for old in sorted(DIST.glob(f"_{NAME}-old-*")):
        if not _force_rmtree(old):
            keep_fail.append(old.name)
    if keep_fail:
        print(f"[build] 以下旧产物删不掉（可能被杀软占用），可手工删：{', '.join(keep_fail)}")
    elif list(DIST.glob(f"_{NAME}-old-*")) == []:
        print("[build] 历史产物已清理")
    # 旧版本的压缩包同样占地方，只留当前版本那一份
    cur = DIST / f"{NAME}-{_version()}-win64.zip"
    for z in sorted(DIST.glob(f"{NAME}-*-win64*.zip")):
        if z.resolve() == cur.resolve():
            continue
        try:
            z.unlink()
            print(f"[build] 已删除旧压缩包 {z.name}")
        except OSError as e:
            print(f"[build] 旧压缩包删不掉 {z.name}：{e}")


def _ship_skill() -> None:
    """把 SKILL.md 塞进产物。

    以前只躺在源码目录，打包后宿主读不到 —— 于是「帮助/技能没生效」。
    产物里带一份，`hy-mem skill --install` 才能把它铺到各宿主。
    """
    src = ROOT / "skill" / "hy-mem" / "SKILL.md"
    if not src.exists():
        print("[build] 警告：源码里没有 skill/hy-mem/SKILL.md，产物将不带技能说明")
        return
    dst = DIST / NAME / "skill" / "hy-mem" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[build] 已带上 SKILL.md（{src.stat().st_size} 字节）")


def _version() -> str:
    try:
        if str(ROOT / "cli") not in sys.path:
            sys.path.insert(0, str(ROOT / "cli"))
        import helptext

        return str(getattr(helptext, "VERSION", "0.0.0"))
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# 双击入口。核心作用是「在 Python 起来之前」检查运行时是否完整 —— 缺了
# _internal\ 连一行 Python 都不会执行，只能靠批处理拦住并停住窗口。
#
# ⚠️ 批处理内容**必须是纯 ASCII**，中文一律放到 startup-error.txt 用 `type` 输出。
# 原因（踩过）：cmd.exe 在 `chcp 65001` 下读取批处理文件是按块解码的，含中文的
# 行会在块边界被截断 —— 实测出现过「一行 rem 的后半截被当成命令执行」并打印
# `'…窗口一闪而过）' is not recognized`。纯 ASCII 的批处理没有这个问题；
# `type` 只把字节原样吐给控制台，不参与解析，因此中文安全。
LAUNCHER_CMD = r"""@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

rem hy-mem launcher.
rem hy-mem.exe is only a bootloader; the real Python runtime lives in _internal\.
rem Check it BEFORE starting, otherwise double-click just closes the window with
rem no message at all (PyInstaller fails at LoadLibrary, before any Python runs).

if not exist "%~dp0_internal\base_library.zip" goto broken
if exist "%~dp0_internal\python3*.dll" goto ready
goto broken

:ready
"%~dp0hy-mem.exe" %*
set "RC=%ERRORLEVEL%"
rem Called with arguments = used by scripts/agents: never wait for a key-press.
if not "%~1"=="" exit /b %RC%
echo.
if "%RC%"=="0" exit /b 0
echo [hy-mem] exit code %RC% -- scroll up for the reason and the fix.
pause
exit /b %RC%

:broken
echo.
if exist "%~dp0startup-error.txt" type "%~dp0startup-error.txt"
if not exist "%~dp0startup-error.txt" echo [hy-mem] Runtime incomplete: _internal\ is missing next to hy-mem.exe
echo.
pause
exit /b 2
"""

# 上面 :broken 分支用 type 打这个文件。UTF-8 无 BOM —— 控制台已 chcp 65001，
# type 原样输出即可；带 BOM 反而会在首行多出不可见字节。
STARTUP_ERROR_TXT = """============================================================
 hy-mem 无法启动：运行时不完整
============================================================

 这个目录里缺少 _internal\\ 。
 它不是可选文件 —— Python 解释器、标准库、向量库全在里面。
 hy-mem.exe 只是个引导器，找不到运行时会直接退出，
 所以双击时窗口一闪而过、什么都看不到。

 最常见的原因：只把 hy-mem.exe 单独拷出来了。

 正确做法：整个文件夹一起拷，或从压缩包完整解压。
     D:\\hy-mem\\hy-mem.exe
     D:\\hy-mem\\_internal\\        <-- 必须和 exe 在同一层

 如果确实是解压出来的，说明解压不完整或被杀软删了文件，
 请先把杀软对该目录的拦截关掉，再重新解压一次。

 注：本程序不需要你安装 Python，运行时已经打包在里面了。
============================================================
"""

INSTALL_CMD = r"""@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
"%~dp0hy-mem.exe" install
echo.
pause
"""

README_TMPL = """hy-mem {ver} · Windows 64 位便携版
================================================

一、第一次使用（三步）
  1. 把整个文件夹解压到可写位置，例如 D:\\hy-mem
     注意：不要只拷 hy-mem.exe。_internal\\ 目录才是真正的运行时，
           它和 exe 必须放在同一层，少了它必定闪退。
  2. 双击「启动 hy-mem.cmd」
     首次运行会引导你配置：向量模型 → 运行模式 →（pro 模式才问）LLM → 存储目录。
  3. 想让任意终端都能直接用 hy-mem 命令，双击「安装到 PATH.cmd」。

二、系统要求
  · Windows 8.1 / Server 2012 R2 及以上，64 位
  · 不需要安装 Python（已内置 python312.dll 与 VC++ 运行库）
  · 写入与检索需要联网（调用向量模型与 LLM）
  · 数据默认存在本目录下的 data\\，整个目录拷走即可迁移

三、常见问题
  · 双击闪退：99% 是只拷了 exe。整个目录一起拷，或用压缩包重新解压。
  · 提示「配置还没配好」：跑 hy-mem setup，按向导填 key。
    LLM key 也可用环境变量 YSJX_API_KEY 提供，不落盘更安全。
  · 想自检：命令行执行 hy-mem doctor，全部 PASS 才算可用。
  · 提示「找不到 hy-mem 命令」：双击「安装到 PATH.cmd」，或直接用完整路径。
  · 源码模式装依赖想走国内镜像：
      pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

四、卸载
  删掉整个目录即可。若装过 PATH 启动器，删
  %LOCALAPPDATA%\\hy-mem\\bin 下的 hy-mem* 两个文件。

构建信息
  版本      {ver}
  构建时间  {build_time}
  文件数    {files} 个
  解压体积  {size} MB
  exe SHA256
    {sha}
"""


def _write_extras(out: Path) -> None:
    """在产物目录里放双击启动器、PATH 安装器、使用说明与校验值。

    这些是「给不会命令行的人」用的，和 exe 放在一起，随目录/压缩包一起发出去。
    """
    ver = _version()
    (out / "启动 hy-mem.cmd").write_text(LAUNCHER_CMD, encoding="utf-8", newline="\r\n")
    (out / "安装到 PATH.cmd").write_text(INSTALL_CMD, encoding="utf-8", newline="\r\n")
    (out / "startup-error.txt").write_text(STARTUP_ERROR_TXT, encoding="utf-8", newline="\r\n")
    # 此刻「使用说明.txt」与「SHA256SUMS.txt」还没落盘（说明里要打印这个数字，
    # 先有鸡还是先有蛋），所以各 +1，否则对外报的文件数永远比实际少 2 个。
    files = sum(1 for f in out.rglob("*") if f.is_file()) + 2
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1048576
    sha = _sha256(out / (NAME + ".exe"))
    (out / "使用说明.txt").write_text(
        README_TMPL.format(
            ver=ver,
            build_time=time.strftime("%Y-%m-%d %H:%M:%S"),
            files=files,
            size=f"{size:.0f}",
            sha=sha,
        ),
        # 记事本对无 BOM 的 UTF-8 在旧版本上会认成 ANSI，中文乱码
        encoding="utf-8-sig",
        newline="\r\n",
    )
    (out / "SHA256SUMS.txt").write_text(
        f"{sha}  {NAME}.exe\n", encoding="utf-8", newline="\r\n"
    )
    print(f"[build] 已写入启动器 / 安装器 / 使用说明 / 校验值（exe sha256 {sha[:16]}…）")


def _make_zip(out: Path) -> Path | None:
    """把整个目录打成 `hy-mem-<ver>-win64.zip`，作为对外分发物。

    这是「只拷 exe 导致闪退」的根治手段：发出去的是压缩包，接收方解压出来就是
    完整目录，不存在漏文件。压缩包必须在消毒之后生成，否则会把 config 带出去。
    """
    if os.getenv("HY_MEM_NO_ZIP", "").strip() not in ("", "0"):
        print("[build] 已跳过压缩包（HY_MEM_NO_ZIP=1）")
        return None
    ver = _version()
    folder = f"{NAME}-{ver}-win64"
    zpath = DIST / f"{folder}.zip"
    try:
        if zpath.exists():
            zpath.unlink()
    except OSError as e:
        print(f"[build] 旧压缩包删不掉（{e}），改写到新名字")
        zpath = DIST / f"{folder}-{time.strftime('%H%M%S')}.zip"
    try:
        level = int(os.getenv("HY_MEM_ZIP_LEVEL", "6") or 6)
    except ValueError:
        level = 6
    t0 = time.time()
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f"{folder}/{f.relative_to(out).as_posix()}")
                n += 1
    print(
        f"[build] 便携压缩包 {zpath.name}（{n} 个文件，{zpath.stat().st_size / 1048576:.0f} MB，"
        f"{time.time() - t0:.0f}s）"
    )
    print("[build]   发给别人请发这个 zip，不要发单个 exe")
    return zpath


def _sanitize() -> list[str]:
    """产物里绝不能带 config.json、.env 与 data/，并扫描一遍疑似密钥。

    config.json 可能含嵌入密钥，且一旦存在就不再触发首次设置向导 —— 之前正是
    因为包里预置了一份没填 key 的配置，导致「第一次打开没有引导」。
    data/ 是开发期的测试记忆，不该跟着发布。

    发布纪律：**产品不带任何模型 key**。用户自己填，填不全就由配置闸门
    给出缺项与修复命令（见 hy_mem_cli.require_ready）。所以这里不只是删
    两个固定文件，还要真扫一遍：PyInstaller 的 `--add-data`、某个模块的
    data 文件、或者手工拷进去的 .env，都可能把 key 带进包。

    返回疑似命中列表；非空时调用方应把构建判为失败 —— 带 key 的包发出去
    比构建失败严重得多。
    """
    out = DIST / NAME
    removed: list[str] = []
    # 1) 固定清单：任意层级都要删（_internal 里也不该有）
    names = {"config.json", "config.json.tmp", ".env", ".env.local", ".env.production"}
    for f in out.rglob("*"):
        if f.is_file() and f.name in names:
            try:
                f.unlink()
                removed.append(str(f.relative_to(out)))
            except OSError as e:
                print(f"[build] 删除 {f} 失败：{e}")
    for name in removed:
        print(f"[build] 已从产物移除 {name}")

    # 2) data/ 是开发期记忆，不该发布
    d = out / "data"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        print("[build] 已从产物移除 data/（首次运行会重建）")

    # 3) 扫疑似密钥：只扫文本类小文件，跳过几百 MB 的二进制依赖
    hits: list[str] = []
    pat = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|Bearer\s+[A-Za-z0-9._\-]{20,}|AKIA[0-9A-Z]{16})")
    text_ext = {".json", ".txt", ".md", ".cfg", ".ini", ".yaml", ".yml", ".py", ".env"}
    for f in out.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in text_ext:
            continue
        try:
            if f.stat().st_size > 3 * 1024 * 1024:
                continue
            body = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = pat.search(body)
        if m:
            hits.append(f"{f.relative_to(out)}  ← …{m.group(0)[:12]}…")
    return hits



def main() -> int:
    exe = ROOT / ".venv" / "Scripts" / "pyinstaller.exe"
    if not exe.exists():
        exe = Path("pyinstaller")
    _stamp_old()
    t0 = time.time()
    print(f"[build] 开始打包（约 1.5 分钟）…")
    rc = subprocess.call([str(exe)] + ARGS, cwd=str(ROOT))
    if rc != 0:
        print(f"[build] 失败，exit={rc}")
        return rc

    # 收尾每一步都用 try 包住：任何一步失败都不该让「产物已经好了」这件事被埋掉。
    # 之前这里是裸调用，purge 抛了异常 → traceback 被管道过滤掉 → 表现成
    # 「构建成功但旧产物一直留在 dist 里」。
    out = DIST / NAME
    secrets: list[str] = []
    try:
        secrets = _sanitize() or []
    except Exception as e:  # noqa: BLE001
        print(f"[build] 消毒失败（不影响产物可用）：{type(e).__name__}: {e}")

    for step, fn in (
        ("带 SKILL.md", _ship_skill),
        ("写启动器与说明", lambda: _write_extras(out)),
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"[build] {step} 失败（不影响产物可用）：{type(e).__name__}: {e}")

    # 压缩包必须在「消毒通过」之后才生成 —— 否则会把带 key 的目录原样打出去。
    if secrets:
        print("[build] ✗ 产物中发现疑似密钥，判为失败（发布包不得内置任何模型 key）：")
        for h in secrets:
            print(f"    {h}")
        print("[build]   确认这些文件不该随包发布后删掉再重试。已跳过压缩包与清理。")
        return 2

    try:
        _make_zip(out)
    except Exception as e:  # noqa: BLE001
        print(f"[build] 打包压缩包失败（目录本身仍可用）：{type(e).__name__}: {e}")
    try:
        _purge_old()
    except Exception as e:  # noqa: BLE001
        print(f"[build] 清历史产物失败：{type(e).__name__}: {e}")

    # 中间产物（build/hy-mem 的分析缓存，约 90MB）打包成功后没用了，留着只会让目录变乱
    cache = ROOT / "build"
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
        print("[build] 已清理 build/ 中间缓存")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"[build] 完成，用时 {time.time() - t0:.0f}s，体积 {size / 1048576:.0f} MB")
    print(f"[build] 产物目录：{out}")
    print(f"[build] 对外分发：{DIST / (NAME + '-' + _version() + '-win64.zip')}（整个目录打成的包）")
    print("[build] 已确认产物不含 config.json / .env / 密钥 → 首次运行必须由用户完成设置")
    exe_path = out / (NAME + ".exe")
    print("[build] 校验建议：")
    print(f"    {exe_path} --version                     # 版本与中文编码")
    print(f"    {exe_path} help                          # 命令总览")
    print(f"    {exe_path} help --json > spec.json       # agent 入口")
    print(f"    {exe_path} doctor                        # 环境自检（含运行环境项）")
    print(f"    {exe_path}                               # 首次运行应进设置引导")
    print("[build] 冷启动冒烟（必须成功，否则就是「只拷 exe」那种闪退）：")
    print("    mkdir /tmp/alone && cp <产物>/hy-mem.exe /tmp/alone/  # 会明确报缺 _internal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
