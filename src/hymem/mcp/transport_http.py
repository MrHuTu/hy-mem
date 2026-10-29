# -*- coding: utf-8 -*-
"""MCP HTTP transport（S4，2026-10-29）—— 复用 server.handle 的单请求处理。

形态：hy-mem mcp --transport http --port 18088
- POST /mcp：body 为单个 JSON-RPC 请求或批（数组），响应同构
- GET /healthz：探活
- 默认只绑 127.0.0.1（个人工具，不对外；团队化时再谈鉴权）

设计取舍（契约记录）：不拆 server.py 的 stdio 循环——handle() 本就协议无关，
新增 transport 是纯增量，零搬家零风险。stdio 默认行为完全不变。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hymem.mcp import server as mcp_server


def make_handler() -> type:
    class _HttpMcp(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静音访问日志（日志走 stderr 约定不变）
            pass

        def _reply(self, status: int, payload) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path == "/healthz":
                self._reply(200, {"ok": True, "server": "hy-mem mcp"})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/mcp":
                self._reply(404, {"error": "not found"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                self._reply(400, {"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "Parse error"}})
                return
            # 单请求或批：批逐条过 handle，聚合返回
            if isinstance(body, list):
                out = [r for r in (mcp_server.handle(m) for m in body) if r is not None]
                self._reply(200, out)
            else:
                resp = mcp_server.handle(body)
                self._reply(200, resp if resp is not None else {"jsonrpc": "2.0", "id": None, "result": None})

    return _HttpMcp


def serve_http(port: int, host: str = "127.0.0.1") -> int:
    srv = ThreadingHTTPServer((host, port), make_handler())
    print(f"hy-mem mcp server (http) listening on http://{host}:{port}/mcp", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
