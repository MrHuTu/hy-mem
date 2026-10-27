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
        # 典型是 HTTP 头里有非 ASCII（http.client / httpx 都按 latin-1/ascii 编码
        # 请求头），**请求根本没发出去**。这是「输入内容不对」，不是网络故障，
        # 所以给 1（改配置能解决）而不是 2。
        # 2026-10-27：中文粘进密钥就是这么一路冒到顶层的。
        return (
            EXIT_USAGE,
            "E_CONFIG",
            "密钥或地址里含非 ASCII 字符（中文/全角/零宽），只能是 ASCII；"
            "重跑 hy-mem setup --step embedding 重新填一次",
        )
    if name in _INTERNAL_TYPES:
        return EXIT_INTERNAL, "E_INTERNAL", "这多半是程序缺陷：加 --verbose 复跑可拿到堆栈"
    if name in _RUNTIME_TYPES:
        return EXIT_RUNTIME, "E_RUNTIME", "先跑 `hy-mem doctor`；嵌入密钥无效用 `hy-mem setup --step embedding`"
    return EXIT_RUNTIME, "E_RUNTIME", "加 --verbose 复跑可看到详细堆栈"


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
