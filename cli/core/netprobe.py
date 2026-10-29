"""统一的出站 HTTP 探测 —— 所有外部请求只从这里走.

存在的理由（2026-10-27 的真实故障）：用户把中文粘进 API Key，向导把它拼进
`Authorization: Bearer <key>`，而 `http.client.putheader` 用 **latin-1** 编码
请求头，中文字符编不了，于是在「请求发出之前」就抛
`UnicodeEncodeError: 'latin-1' codec can't encode characters in position 7-8`。
旧代码把这个异常兜成 `status = 0`，展示层再拼成 `HTTP 0` —— 一个「输入内容
不对」的问题，长得像「网络/服务器故障」，用户只能反复查网络、换模型，
永远查不到根上。

两条设计约束，就是为了让这类错误不可能再出现：

  1. **类型上不存在 0**。成功是 `Sent(status: int >= 100)`，没发出是
     `NotSent(reason)`。`status == 0` 这个约定被彻底删掉，新调用点想犯
     同样的错都没有地方可犯（`Sent.__post_init__` 会直接拦）。
  2. **发送前先查**。`precheck()` 复用 `config.secret_charset_problem()` 同一
     套判定，在组包之前就说清「这个字符串进不了 HTTP 报文，是第几位、
     什么字符」，不依赖事后从异常里猜偏移量。

`scripts/test_setup.py` 里有一条源码扫描断言守着：除本模块外，`cli/` 下不许
直接调 `urlopen`，也不许再出现 `status == 0` 的写法。
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from cli.core import config as cfgmod


# --------------------------------------------------------------------------
# 结果类型：发出 / 没发出，二选一，没有第三种
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Sent:
    """请求真的发出去了。`status` 是服务器给的码，一定是 >= 100 的真实值。"""

    status: int
    payload: dict
    seconds: float = 0.0

    def __post_init__(self) -> None:
        # 这条断言就是「杜绝 HTTP 0」的最后一道锁：0/None 只能表示没发出，
        # 想构造一个 status=0 的「成功结果」会在这里直接炸掉（开发期可见）。
        if not isinstance(self.status, int) or self.status < 100:
            raise ValueError(
                f"Sent.status 必须是真实 HTTP 状态码，收到 {self.status!r}；"
                "请求没发出请用 NotSent"
            )

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def label(self) -> str:
        return f"HTTP {self.status}"


@dataclass(frozen=True)
class NotSent:
    """请求没有离开本机。`reason` 可以直接展示给人看。"""

    reason: str
    cause: str = ""  # precheck / encoding / dns / refused / tls / timeout / network / other
    seconds: float = 0.0
    detail: str = ""


ProbeResult = Sent | NotSent


# --------------------------------------------------------------------------
# 发送前预检
# --------------------------------------------------------------------------
def _check_url(url: str) -> str | None:
    if not url or not url.strip():
        return "地址为空"
    if any(ch.isspace() for ch in url):
        return "地址里有空格或换行（多半是从文档里复制时带进来的）"
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as e:
        return f"地址无法解析：{e}"
    if parts.scheme not in ("http", "https"):
        hint = "（是不是漏了 https://）" if not parts.scheme else ""
        return f"地址协议是「{parts.scheme or '空'}」，只支持 http/https{hint}"
    host = parts.hostname or ""
    if not host:
        return "地址里没有主机名"
    try:
        host.encode("idna")  # 中文域名走 IDNA 是合法的，这里只验能不能转
    except UnicodeError:
        return f"主机名「{host}」不能转换成 IDNA"
    for label, part in (("路径", parts.path), ("查询串", parts.query), ("锚点", parts.fragment)):
        if not part:
            continue
        bad = cfgmod.secret_charset_problem(part)
        if bad:
            return (
                f"地址的{label}{bad}（URL 里不能出现中文/全角/空白，"
                "域名用 punycode，带中文的参数请 percent-encode）"
            )
    return None


def precheck(url: str, key: str = "", key_label: str = "API Key") -> str | None:
    """发请求前检查「这个字符串能不能进 HTTP 报文」。返回问题描述，None 表示没问题。

    这里刻意不检查请求体（JSON body 走 UTF-8，中文合法），只查**会进 URL
    或请求头**的部分 —— 那才是 latin-1 编码会炸的地方。
    """
    bad = _check_url(url)
    if bad:
        return bad
    if key:
        p = cfgmod.secret_charset_problem(key)
        if p:
            return f"{key_label} {p} —— 密钥只能是 ASCII 字符（字母、数字、- 和 _）"
    return None


# --------------------------------------------------------------------------
# 发送
# --------------------------------------------------------------------------
def _ssl_ctx(insecure: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _parse(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:300]}
    return value if isinstance(value, dict) else {"data_raw": value}


def _from_net_error(reason: Any, timeout: int, t0: float) -> NotSent:
    """socket/URLError 的 reason 花样很多，翻成「接下来该做什么」。"""
    text = str(reason)
    low = text.lower()
    if isinstance(reason, socket.gaierror) or "getaddrinfo" in low or "name or service not known" in low:
        msg, cause = (
            "域名解析失败（检查 baseUrl 拼写；公司网络或代理环境下可能需要设置 HTTPS_PROXY）",
            "dns",
        )
    elif "tunnel connection failed" in low or "proxy" in low:
        # 走代理时，urllib 会把「代理自己报的 502/407」塞进 URLError.reason，
        # 文本里带 "502 Bad Gateway" 很容易被误读成「目标服务返回 502」。
        msg, cause = (
            "代理连不通（这是代理返回的错误，不是目标服务的状态码；"
            "检查 HTTPS_PROXY / HTTP_PROXY 设置，或临时取消代理）",
            "proxy",
        )
    elif "refused" in low:
        msg, cause = "连接被拒绝（地址/端口不通，或服务没在该端口上监听）", "refused"
    elif "certificate" in low or "ssl" in low or "tls" in low:
        msg, cause = "TLS 证书校验失败（自签证书可在向导里选「跳过证书校验」）", "tls"
    elif "timed out" in low or "timeout" in low:
        msg, cause = f"连接超时（{timeout}s 内没有响应；检查网络/代理，或调大超时）", "timeout"
    elif "unreachable" in low:
        msg, cause = "网络不可达（检查网线/代理/防火墙）", "network"
    else:
        msg, cause = f"网络错误：{text}", "network"
    return NotSent(
        f"请求没有发出：{msg}",
        cause=cause,
        seconds=time.time() - t0,
        detail=f"{type(reason).__name__}: {text}" if not isinstance(reason, str) else text,
    )


def post_json(
    url: str,
    payload: dict,
    key: str = "",
    timeout: int = 45,
    insecure: bool = False,
    key_label: str = "API Key",
) -> ProbeResult:
    """POST 一段 JSON。要么返回 `Sent`（带真实状态码），要么返回 `NotSent`。"""
    t0 = time.time()

    bad = precheck(url, key, key_label)
    if bad:
        # 一个字节都没发出去。此时报「HTTP 什么」都是错的。
        return NotSent(f"请求没有发出：{bad}", cause="precheck", detail=bad)

    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx(insecure)) as resp:
            return Sent(int(resp.status), _parse(resp.read()), time.time() - t0)
    except urllib.error.HTTPError as e:
        # 真·HTTP 错误：服务器给了码，交给调用方判断
        return Sent(int(e.code), _parse(e.read()), time.time() - t0)
    except UnicodeEncodeError as e:
        # 预检已经拦掉了 key 与 URL；能走到这儿说明是「我们自己的代码」
        # 往请求头/报文里塞了非 ASCII —— 仍然按「没发出」处理，绝不报成 HTTP。
        chunk = ""
        if isinstance(getattr(e, "object", None), str):
            chunk = e.object[max(0, e.start - 1) : e.end + 1]
        return NotSent(
            "请求没有发出：HTTP 头里含非 ASCII 字符"
            f"（{chunk!r}）—— 常见原因是 API Key 或地址里混进了中文/全角/零宽字符",
            cause="encoding",
            seconds=time.time() - t0,
            detail=str(e),
        )
    except urllib.error.URLError as e:
        return _from_net_error(e.reason, timeout, t0)
    except (socket.timeout, TimeoutError) as e:
        return _from_net_error(e, timeout, t0)
    except ssl.SSLError as e:
        return _from_net_error(e, timeout, t0)
    except Exception as e:  # noqa: BLE001
        return NotSent(
            f"请求没有发出：{type(e).__name__}: {str(e)[:200]}",
            cause="other",
            seconds=time.time() - t0,
            detail=f"{type(e).__name__}: {e}",
        )


# --------------------------------------------------------------------------
# 结果 → 人话
# --------------------------------------------------------------------------
def get_json(url: str, timeout: float = 5, direct: bool = False) -> ProbeResult:
    """GET 一个 JSON 端点。要么返回 `Sent`（带真实状态码），要么返回 `NotSent`。

    direct=True 时绕过一切代理（urllib 默认吃 HTTPS_PROXY/系统代理，本机
    回环探活绝不能被代理劫持）。典型调用方：vdb_endpoint 的 chroma 心跳。
    """
    t0 = time.time()
    req = urllib.request.Request(url, method="GET")
    try:
        if direct:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            resp_ctx = opener.open(req, timeout=timeout)
        else:
            resp_ctx = urllib.request.urlopen(req, timeout=timeout)
        with resp_ctx as resp:
            return Sent(int(resp.status), _parse(resp.read()), time.time() - t0)
    except urllib.error.HTTPError as e:
        return Sent(int(e.code), _parse(e.read()), time.time() - t0)
    except urllib.error.URLError as e:
        return _from_net_error(e.reason, timeout, t0)
    except (socket.timeout, TimeoutError) as e:
        return _from_net_error(e, timeout, t0)
    except Exception as e:  # noqa: BLE001
        return NotSent(
            f"请求没有发出：{type(e).__name__}: {str(e)[:200]}",
            cause="other",
            seconds=time.time() - t0,
            detail=f"{type(e).__name__}: {e}",
        )


def brief(payload: Any, limit: int = 220) -> str:
    """从响应体里取一句最像「错误原因」的话。"""
    if not isinstance(payload, dict):
        return str(payload)[:limit]
    e = payload.get("error")
    if isinstance(e, dict):
        return str(e.get("message") or e.get("code") or e)[:limit]
    if e:
        return str(e)[:limit]
    if payload.get("raw"):
        return str(payload["raw"])[:limit]
    if payload.get("message"):
        return str(payload["message"])[:limit]
    return json.dumps(payload, ensure_ascii=False)[:limit]


_STATUS_HINT = {
    400: "｜请求被拒绝：模型名或参数不对，检查 model 是否与服务端一致",
    401: "｜密钥被拒绝：无效 / 过期 / 填错了别家的 key，或前后多了空格",
    403: "｜密钥没有这个模型或这个接口的权限，去控制台确认已开通",
    404: "｜入口路径不对：OpenAI 兼容接口一般以 /v1 结尾；百炼 MaaS 要用 /compatible-mode/v1 而不是 /api/v1",
    413: "｜请求体过大：内容太长，试试 --split 分段写入",
    429: "｜触发限流或配额用尽：等一会儿再试，或换一个 key",
    500: "｜服务端内部错误：稍后重试",
    502: "｜网关错误：上游服务不可达，稍后重试",
    503: "｜服务暂不可用：稍后重试",
    504: "｜网关超时：稍后重试",
}


def status_hint(status: int) -> str:
    """真实状态码 → 「接下来该做什么」。空串表示没有专门提示。"""
    return _STATUS_HINT.get(int(status), "")


def explain(result: ProbeResult, hint: str = "") -> str:
    """把结果翻成一行可直接展示的文案。`NotSent` 永远不带 HTTP 字样。"""
    if isinstance(result, NotSent):
        return result.reason
    return f"{result.label}: {brief(result.payload)}{hint or status_hint(result.status)}"
