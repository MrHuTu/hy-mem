"""PyInstaller 打包入口.

单独存在的原因：hy-mem 平时由 .venv 的 python 直接跑，不需要可执行文件；
但要做「自包含单文件 CLI」就得把 Python 侧冻结成一个 exe，PyInstaller 需要一个
明确的 __main__ 入口。这里只做最小转发，不做任何业务逻辑。

四件事必须在最前面做：

1. **重定向编码**。PyInstaller 的冻结进程不遵守 PYTHONIOENCODING / PYTHONUTF8
   （实测：环境里明明设了 utf-8，输出仍是 `bcc7 d2e4` 这种 GBK 字节），
   必须在任何输出之前手工 reconfigure。这里做一次、hy_mem_cli._force_utf8()
   再做一次，双保险 —— argparse 的 --help 会直接打印，不能赌它一定晚于某个检查。

2. **路径不可靠**。冻结后 __file__ 指向解包临时目录，所以 CLI 侧统一用
   HY_MEM_HOME > sys.executable 目录 > 源码上两级 的顺序推导安装根。

3. **启动前体检**。别人拿到 exe 双击闪退，最常见的两种原因：
   (a) 只拷了 hy-mem.exe，没带 `_internal/` —— 这种连 Python 都起不来，
       只能由外层 `启动 hy-mem.cmd` 拦（见 runtime_check 顶部说明）；
   (b) 系统太旧 / 目录不可写 / 包不全 —— Python 已经起来了，这里能拦。
   拦下来不算完：必须把「为什么」和「怎么修」打出来，并且在**双击场景**下
   等一次按键，否则窗口一闪而过，用户什么都看不到。

4. **兜底不吞异常**。任何未捕获异常都要落一份 startup.log 再给出人话提示。
   静默退出是最糟的结果 —— 用户只会说「闪退」。
"""

import os
import sys
import traceback


def _fix_encoding() -> None:
    if not getattr(sys, "frozen", False):
        return
    want = os.getenv("HY_MEM_ENCODING") or os.getenv("PYTHONIOENCODING") or "utf-8"
    enc = (want.split(":", 1)[0] or "utf-8").strip()
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding=enc, errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _preflight(argv: list[str]) -> int | None:
    """启动前体检。返回 None = 可以继续；返回 int = 直接以该退出码结束。

    只在有问题时才输出 —— 正常情况下不能给每条命令都加噪音。
    无参数（双击 / 首次使用）跑完整检查，带参数时只跑廉价必检项，
    保证 `hy-mem search ...` 这类高频调用不被拖慢。
    """
    try:
        from cli.core import runtime_check
    except Exception:  # noqa: BLE001
        return None  # 自检模块本身有问题，不该连累主程序
    try:
        items = runtime_check.checks(full=not argv)
        if not runtime_check.failures(items):
            return None
        text = runtime_check.report(items)
        print()
        print(text)
        log = runtime_check.write_log("启动前体检未通过\n" + text)
        if log:
            print(f"\n  诊断日志：{log}")
        print("\n  处理完上面的问题再运行一次即可。")
        if runtime_check.interactive():
            print()
            runtime_check.wait_key()
        return 2
    except Exception:  # noqa: BLE001
        return None


def _crash(exc: BaseException) -> int:
    """未捕获异常的统一出口：写日志 + 人话提示 + 双击时停住窗口。"""
    tb = traceback.format_exc()
    try:
        from cli.core import runtime_check

        log = runtime_check.write_log(f"未捕获异常：{type(exc).__name__}\n{tb}")
        print()
        print("hy-mem 启动时发生内部错误。下面是诊断信息：")
        print(tb.rstrip() if len(tb) < 4000 else tb[:4000] + "\n…（已截断）")
        if log:
            print(f"\n  诊断日志：{log}")
        print("  可以把这份日志发给开发者；如果只是偶尔出现，重跑一次通常就好。")
        if runtime_check.interactive():
            print()
            runtime_check.wait_key()
    except Exception:  # noqa: BLE001
        print(tb, file=sys.stderr)
    return 3


def main() -> int:
    _fix_encoding()
    argv = list(sys.argv[1:])

    rc = _preflight(argv)
    if rc is not None:
        return rc

    try:
        from cli.hy_mem_cli import main as cli_main  # S1 分层：包全名

        return cli_main(argv)
    except KeyboardInterrupt:
        return 130
    except SystemExit as e:  # argparse 的正常退出路径，别当崩溃
        code = e.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    except BaseException as e:  # noqa: BLE001
        return _crash(e)


if __name__ == "__main__":
    sys.exit(main())
