"""错误分类与友好化 —— CLI 与 MCP 共用一套口径。

历史上 classify_error 只服务 CLI 出口；MCP 工具失败时把裸异常
（`TypeName: message`）直接丢给客户端，AI 宿主既判断不了严重程度、
也给不出修复建议。抽到这里统一分类，两种出口各自格式化：

- CLI 要「简洁」：一行 `类型: 消息（建议）`；
- MCP 要「明确」：错误码 + 异常原文 + 建议 分行，宿主可直接转述给用户。
"""
from __future__ import annotations

# 退出码语义（对齐标准自包含 CLI 规范）：agent 靠它决定「重试 / 换招 / 报 bug」
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_RUNTIME = 2
EXIT_INTERNAL = 3


# S1 分层（2026-10-29）：core 不 import ui —— 翻译经钩子注入（依赖倒置）。
# UI 层（menu/hy_mem_cli 启动时）调用 register_tr 注入 tr；MCP 等纯服务
# 进程不注入，错误恒中文。既守住分层铁律，又保住「英文用户看英文报错」。
_TRANSLATOR = None


def register_tr(func) -> None:
    """UI 层注册翻译函数（签名：str -> str）。"""
    global _TRANSLATOR
    _TRANSLATOR = func


def _tr(text: str) -> str:
    """提示文案翻成当前界面语言；未注册翻译器时返回中文原文。"""
    if _TRANSLATOR is None:
        return text
    try:
        return _TRANSLATOR(text)
    except Exception:  # noqa: BLE001  翻译失败不能拦错误提示本身
        return text

# 2 与 3 的分界线是「能不能靠改配置/换网络解决」：端点、密钥、维度、文件
# 权限属于 2；TypeError/KeyError 这类只可能是程序自己写错了，属于 3。
_RUNTIME_TYPES = {
    "RuntimeError", "ValueError", "OSError", "ConnectionError", "TimeoutError",
    "SSLError", "HTTPError", "URLError", "JSONDecodeError", "OperationalError",
}
_INTERNAL_TYPES = {
    "TypeError", "AttributeError", "KeyError", "IndexError", "ImportError",
    "ModuleNotFoundError", "NameError", "NotImplementedError", "RecursionError",
}


def classify_error(e: BaseException) -> tuple[int, str, str]:
    """异常 → (退出码, 错误码, 人话提示)。"""
    name = type(e).__name__
    if name == "Aborted":
        return EXIT_OK, "CANCELLED", ""
    if name == "UnicodeEncodeError":
        msg = str(e)
        if "surrogate" in msg:
            # 孤立代理项：输入侧的问题，不是密钥。2026-10-28 事故里这条被
            # 误报成「密钥含中文」，用户照着改了三次密钥都没用 —— 因为真正
            # 的源头是管道/终端的编码把 UTF-8 字节切出了 \udcXX。
            return (
                EXIT_USAGE,
                "E_ENCODING",
                _tr(
                    "写入内容里有无法编码的字符（多为终端/管道不是 UTF-8）："
                    "改用参数直传 `hy-mem add \"内容\"`，或先 `chcp 65001` 再管道"
                ),
            )
        # 典型是 HTTP 头里有非 ASCII（http.client / httpx 都按 latin-1/ascii 编码
        # 请求头），**请求根本没发出去**。这是「输入内容不对」，不是网络故障，
        # 所以给 1（改配置能解决）而不是 2。
        # 2026-10-27：中文粘进密钥就是这么一路冒到顶层的。
        return (
            EXIT_USAGE,
            "E_CONFIG",
            _tr(
                "密钥或地址里含非 ASCII 字符（中文/全角/零宽），只能是 ASCII；"
                "重跑 hy-mem setup --step embedding 重新填一次"
            ),
        )
    if name == "ExtractFailed":
        # R4 两段式：Phase 1 LLM 抽取失败（零写入）。入口层已给专项文案，
        # 这里兜统一分发的底：runtime 级、不指向密钥。
        return EXIT_RUNTIME, "E_EXTRACT", _tr(
            "LLM 抽取失败（本条零写入）：多为 LLM/网络瞬时故障，单条可直接重试；"
            "反复失败跑 hy-mem doctor 检查 LLM 配置"
        )
    if name == "CommitFailed":
        # R4 两段式：Phase 2 提交段失败（L1 原文已存档，结论可能缺失）
        return EXIT_RUNTIME, "E_COMMIT", _tr(
            "落库提交失败：原始对话已存档（L1），可检索的结论可能缺失；"
            "重跑本命令即可，reconcile 会吸收重复"
        )
    if name in _INTERNAL_TYPES:
        return EXIT_INTERNAL, "E_INTERNAL", _tr("这多半是程序缺陷：加 --verbose 复跑可拿到堆栈")
    # R2（2026-10-29）：sqlite 瞬态占用单列。WAL「最后一连接关闭→删 -wal/-shm→
    # 下连接重建」窗口里，已打开连接写入会报 readonly/locked（Windows 实测约 5%）。
    # 此前落进 _RUNTIME_TYPES 的通用 hint 指向「嵌入密钥无效」——纯误导。
    _msg = str(e).lower()
    if "database" in _msg and ("readonly" in _msg or "locked" in _msg or "busy" in _msg):
        return EXIT_RUNTIME, "E_DB_BUSY", _tr(
            "记忆库正被并发写入瞬态占用（SQLite WAL 重建窗口）：稍等重跑本命令即可；"
            "反复出现跑 `hy-mem doctor` 自检。这与密钥无关"
        )
    if "graphstore (kuzu) init failed" in _msg:
        # ultra 的 L6 图库是 kuzu 独占锁（`kuzu.Database(path)` 无 read_only），
        # 同一数据目录第二个进程必失败。实测 2 进程并发 add → 1 成 1 败。
        # 这条错误以前落进 _RUNTIME_TYPES，给的是「嵌入密钥无效」，纯误导。
        return EXIT_RUNTIME, "E_RUNTIME", _tr(
            "L6 图库（kuzu）初始化失败。最常见原因是另一个 hy-mem 进程正占着同一数据目录"
            "——kuzu 是独占锁，ultra 属单写者模式：停掉 MCP 服务或其它 hy-mem 进程后重试；"
            "需要多进程/多子代理并发写入，请用 mode=pro。"
            "若确认无其它进程占用，再跑 `hy-mem doctor` 看 kuzu 依赖与日志"
        )
    if name == "EndpointUnavailable":        # 向量库服务活着却连不上：明确失败，绝不静默回落嵌入式读旧索引
        return (
            EXIT_RUNTIME,
            "E_ENDPOINT",
            _tr(
                "向量库服务异常：先 `hy-mem serve` 看它的报错，或 `hy-mem doctor` 自检；"
                "确认无误后可以 `hy-mem queue` 观察后台任务是否卡住"
            ),
        )
    if name in _RUNTIME_TYPES:
        return EXIT_RUNTIME, "E_RUNTIME", _tr(
            "先跑 `hy-mem doctor`；嵌入密钥无效用 `hy-mem setup --step embedding`"
        )
    return EXIT_RUNTIME, "E_RUNTIME", _tr("加 --verbose 复跑可看到详细堆栈")


def error_detail(e: BaseException) -> str:
    """异常原文（`类型: 消息`）——日志与结构化输出的 message 字段统一用它。"""
    return f"{type(e).__name__}: {e}"


def format_tool_error(e: BaseException) -> str:
    """MCP tools/call 失败时给客户端的文本：错误码 + 原文 + 建议。

    宿主（AI agent）读这段文本后要能直接向用户转述「出了什么事、怎么办」，
    所以三要素分行写全，不玩单行压缩。
    """
    _, err_code, hint = classify_error(e)
    lines = [f"错误 [{err_code}] {error_detail(e)}"]
    if hint:
        lines.append(f"建议：{hint}")
    return "\n".join(lines)
