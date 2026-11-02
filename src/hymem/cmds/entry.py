"""CLI 入口：main() / cmd_mcp / 操作日志记录 / 冻结编码强制。

S2 拆分（docs/specs/2026-11-02-cli-split-ports/）：本模块从 hymem/cli.py 按域拆入，
逻辑零变化——全局状态存于 cmds.base，跨模块经 _base 属性读写。
"""
from __future__ import annotations

import sys

from hymem.cmds.base import (  # noqa: F401
    EXIT_OK, _EXIT_CODES, _GLOBAL_FLAGS, _GLOBAL_VALUED, _LAST_FAIL,
    classify_error, error_detail, fail, hoist_globals, load_env,
    os, sys, time,
)
from hymem.cmds import base as _base  # 全局状态经 _base 读写
from hymem.cmds.parser import (  # noqa: F401  parser 不反向 import entry（cmd_mcp 延迟）
    _print_short_help,
    build_parser,
    cmd_help,
)


def _force_utf8() -> None:
    """冻结成 exe 后强制 stdout/stderr 的编码。

    症状：源码模式中文正常，PyInstaller 打包后所有中文变乱码（输出 GBK 字节）。

    根因（实测得出，不是猜的）：本机环境已经设了 `PYTHONIOENCODING=utf-8` 和
    `PYTHONUTF8=1`，但 **PyInstaller 的冻结进程并不遵守它们** —— 字节抓出来是
    `bcc7 d2e4`（GBK 的"命令"），说明 stdout.encoding 仍是 cp936。

    所以不能"有 PYTHONIOENCODING 就跳过"，必须主动 reconfigure：
    - 优先用 PYTHONIOENCODING 指定的编码（尊重用户环境）
    - 否则用 utf-8
    - HY_MEM_ENCODING 可强制覆盖（GBK 的 cmd.exe 里需要时用）
    - 仅 frozen 时执行：源码模式下 Python 能正确推断编码，强改反而会在 GBK 终端出错
    """
    if not getattr(sys, "frozen", False):
        return
    want = os.getenv("HY_MEM_ENCODING") or os.getenv("PYTHONIOENCODING") or "utf-8"
    enc = (want.split(":", 1)[0] or "utf-8").strip()

    def _norm(s: str) -> str:
        return (s or "").lower().replace("-", "").replace("_", "")

    for stream in (sys.stdout, sys.stderr):
        try:
            if not hasattr(stream, "reconfigure"):
                continue
            if _norm(getattr(stream, "encoding", "")) == _norm(enc):
                continue
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:  # noqa: BLE001
            pass


def cmd_mcp(args) -> int:
    """MCP stdio 服务器：宿主按会话拉起，随会话退出。

    铁律：此模式下 stdout 只准有协议响应。load_env 后直接进 serve() 主循环，
    不经过 ok()/print() 那套人类输出。
    """
    load_env()
    if getattr(args, "transport", "stdio") == "http":
        from hymem.mcp.transport_http import serve_http

        return serve_http(getattr(args, "port", 18088))
    from hymem.mcp import server as mcp_server

    return mcp_server.serve()


def _sub_argv(argv: list[str], cmd: str) -> list[str]:
    """从（已提升全局选项的）argv 里切出「子命令 + 它自己的参数」。

    为什么不能直接取 argv：全局选项被 hoist 到最前面了，argv[0] 往往是
    `--json` 而不是子命令；日志摘要要靠它判断「哪些位置参数是正文」。
    """
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GLOBAL_FLAGS:
            i += 1
            continue
        if tok in _GLOBAL_VALUED:
            i += 2
            continue
        name = tok.split("=", 1)[0]
        if "=" in tok and (name in _GLOBAL_VALUED or name in _GLOBAL_FLAGS):
            i += 1
            continue
        return [tok] + list(argv[i + 1:])
    return [cmd] if cmd else []


def _record_operation(args, argv: list[str], seconds: float, rc: int, exc) -> None:
    """把这次执行写进操作日志。

    只在这一处记：交互界面的每次操作都是一次子进程调用，同样经过 main()，
    所以不用在几十个 cmd_* 里各插一行 —— 少一处记录点就少一处漏记。
    """
    cmd = str(getattr(args, "cmd", "") or "")
    if cmd in ("mcp", "log"):
        # mcp 是协议模式（stdout 属于宿主，别碰）；log 自己读日志，
        # 记它只会把真正的操作挤出 50 条窗口
        return
    # 失败原因有两种来路：异常走 classify_error（在 oplog 里做），
    # 「已判定为错、正常返回退出码」的走 fail() / _report_missing 留下的那一句；
    # 都没有就只记退出码对应的枚举，至少能看出是用法错还是运行时错
    if exc is not None:
        error = exc
    elif rc != 0:
        error = f"{_base._LAST_FAIL[0]} {_base._LAST_FAIL[1]}" if _base._LAST_FAIL else _EXIT_CODES.get(rc, "E_RUNTIME")
    else:
        error = None
    try:
        from hymem.core import oplog

        oplog.record(
            _sub_argv(argv, cmd),
            rc,
            seconds,
            error=error,
            verbose=bool(getattr(args, "verbose", False)),
        )
    except Exception:  # noqa: BLE001
        pass  # 日志是附加能力：写不进去也不能改变命令本身的退出码


def main(argv=None) -> int:
    _force_utf8()
    load_env()

    # 升级接管：全新解压（本安装根还没有 config.json）时，若 PATH 上仍指向
    # 另一个带配置的 hy-mem 安装，就沿用它的配置、记忆数据原位复用 —— 用户
    # 升级 = 解压新包 + 直接用，不用重走向导、不丢记忆。接管后要重放
    # apply_env，否则 MEMORY_DATA_DIR 还停在新目录，队列会跟主库分家。
    # 说明走 stderr：stdout 属于协议/结果，不能混入。
    try:
        from hymem.core import config as _cfg_ad
        from hymem.core import install_ops as _iol_ad

        _ad_info, _ad_notes = _iol_ad.adopt_previous_install()
        if _ad_info:
            _cfg_ad.apply_env(_cfg_ad.load())
            for _n in _ad_notes:
                print("hy-mem: " + _n, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass  # 探测失败不该挡住正常启动，install/doctor 里还能看到诊断

    argv_list = hoist_globals(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv_list)
    _base._QUIET = bool(getattr(args, "quiet", False))
    _base._JSON = bool(getattr(args, "json", False))
    _base._FIELDS = [f.strip() for f in str(getattr(args, "fields", "") or "").split(",") if f.strip()]
    if getattr(args, "no_color", False):
        os.environ["NO_COLOR"] = "1"
    if _base._QUIET and not _base._JSON:
        # --quiet 表示「只要退出码」。各处零散的 print 不可能逐个改干净，
        # 直接从流这一层掐掉；--json 例外，因为那份输出本身就是结果。
        import io as _io

        sys.stdout = _io.StringIO()

    # 身份回填：智能体调用时不该被迫传 -u。
    # 这同时修掉一个真实报错：search 走复数形参，未给 -u 时会传 [None]，
    # SDK 侧过滤成空列表后抛 "user_ids is required and cannot be empty"。
    if not getattr(args, "user", None):
        try:
            from hymem.core import config as _cfgmod

            args.user = _cfgmod.identity_defaults()
        except Exception:  # noqa: BLE001
            args.user = "default"

    # --help-json：不用进子命令就能拿到整份规格（agent 最省事的一条路）
    if getattr(args, "help_json", False):
        args.cmd = "help"
        args.command = None
        args.json = True
        args.func = cmd_help

    # SDK 会往 root logger 打大量 INFO（handlers 自带、propagate=False），
    # 全局 disable 是唯一稳的压法，否则输出无法被 agent 解析
    import logging

    # fastembed 用 loguru 而不是标准 logging，`logging.disable()` 管不到它，
    # 首次运行会在 stderr 打一串 "Could not download model …" 干扰解析。
    # 非 --verbose 就摘掉它的 handler（loguru 是进程级全局，移除即静默）。
    if not getattr(args, "verbose", False):
        try:
            from loguru import logger as _lg

            _lg.remove()
        except Exception:  # noqa: BLE001
            pass

    interactive = args.cmd in ("menu", "setup") or not getattr(args, "cmd", None)
    if not getattr(args, "verbose", False) and not interactive:
        # INFO/WARNING 里全是 pipeline 诊断；reconcile 还会把"BM25_ALL_ZERO"
        # 这类正常统计打成 ERROR，混进 stdout/stderr 会让 agent 解析失败。
        logging.disable(logging.ERROR)

    # 无子命令：TTY 下进交互界面；非 TTY（被 agent/skill 调用）时打帮助，
    # 绝不能在这里卡住等输入 —— 那会让调用方直接超时。
    if not getattr(args, "cmd", None):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            _print_short_help()
            return EXIT_OK
        try:
            from hymem.ui import tui

            tui.enable_ansi()
            from hymem.ui import setup_wizard

            need, why = setup_wizard.needs_setup()
            if need:
                print(tui.title("欢迎使用 hy-mem"))
                print(tui.panel([tui.c.dim(f"检测到：{why}")]))
                # 一次 y/N 就够：首次运行只有「配 / 不配」两种走向，
                # 弹一屏二选一列表等于让人先读两项说明再按键（2026-10-28 交互精简）
                tui.note("首次设置会依次问：向量模型 → 运行模式 →（pro/ultra 才问）LLM → 存储目录")
                tui.note("跳过也能用控制台，未配好前写入/检索会提示缺哪项；随时 setup 补齐")
                if tui.confirm("现在开始设置？", default=True):
                    rc = setup_wizard.run_setup(first_run=True)
                    if rc != 0:
                        # 不再「按任意键继续」：控制台马上就会出现在下一行，
                        # 多按一次键纯属多余（2026-10-28 交互精简）
                        tui.warn("设置未全部通过；进控制台后可用 setup 补齐")
                else:
                    from hymem.core import config as cfgmod

                    cfg = cfgmod.load()
                    cfg["setupComplete"] = True
                    cfg["setupSkipped"] = True
                    cfgmod.save(cfg)
                    tui.warn("已跳过：配置可能不完整，随时可用「设置」补齐")
            from hymem.ui import menu

            return menu.run_menu(menu.subprocess_dispatch)
        except (KeyboardInterrupt, tui.Aborted):
            print()
            return EXIT_OK

    # 计时与记录都放在这一处：CLI 直调与交互界面（子进程调 CLI）共用同一条路径，
    # 于是 ops.log 覆盖全部子命令，无需在几十个 cmd_* 里各记一次。
    started = time.time()
    exc: BaseException | None = None
    _base._LAST_FAIL = None
    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        rc = fail("已中断", 130)
    except Exception as e:  # noqa: BLE001
        # 交互层用 Aborted 表示「用户取消」，那不是错误，不该打堆栈或 exit 2
        if type(e).__name__ == "Aborted":
            print()
            print("hy-mem: 已取消", file=sys.stderr)
            rc = EXIT_OK
        else:
            exc = e
            code, err_code, hint = classify_error(e)
            if getattr(args, "verbose", False):
                import traceback

                traceback.print_exc()
            # --json 的结构化错误由 fail() 统一输出（_base._JSON 已置位）——别在这里
            # 手工再 print 一份：曾经两处各打一行，agent 会收到两行 JSON。
            rc = fail(error_detail(e), code, hint=hint, err_code=err_code)
    _record_operation(args, argv_list, time.time() - started, rc, exc)
    return rc
