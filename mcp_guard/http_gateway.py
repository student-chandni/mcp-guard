"""Reverse proxy for remote (Streamable HTTP) MCP servers.

claude.ai custom connectors are called from Anthropic's servers, not from the
browser, so there's nothing to intercept locally. Instead this runs in front of
the MCP server and you register its (tunnelled) URL as the connector. Same
idea as the stdio proxy: hold tools/call POSTs, stream everything else.
"""

import asyncio
import json
import secrets
import sys
from typing import Dict, Optional
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from mcp_guard.client import BrokerClient
from mcp_guard.config import Config
from mcp_guard.protocol import (
    build_approval_request, client_label, denial_response, is_request, parse_message,
)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}
MAX_BODY = 4 * 1024 * 1024


def log(msg: str) -> None:
    print(f"mcp-guard[gateway]: {msg}", file=sys.stderr, flush=True)


class Gateway:
    def __init__(self, upstream: str, name: str, cfg: Optional[Config] = None,
                 broker: Optional[BrokerClient] = None, path_secret: Optional[str] = None):
        self.upstream = upstream.rstrip("/")
        self.upstream_path = urlsplit(self.upstream).path
        self.name = name
        self.cfg = cfg or Config.load()
        self.broker = broker or BrokerClient(self.cfg)
        self.path_secret = path_secret
        self.http: Optional[ClientSession] = None
        self._client_by_session: Dict[str, str] = {}
        self._pending: Dict[tuple, str] = {}   # (session, json(rpc id)) -> request id

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        app.on_startup.append(self._startup)
        app.on_cleanup.append(self._cleanup)
        return app

    async def _startup(self, app) -> None:
        # no total timeout because SSE streams stay open; don't decompress so
        # we can pass bodies through as-is
        self.http = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10),
                                  auto_decompress=False)

    async def _cleanup(self, app) -> None:
        if self.http:
            await self.http.close()

    def _upstream_url(self, request: web.Request) -> Optional[str]:
        path = request.path
        if self.path_secret:
            prefix = f"/{self.path_secret}"
            if path != prefix and not path.startswith(prefix + "/"):
                return None
            path = path[len(prefix):]

        if path in ("", "/", "/mcp") or path == self.upstream_path:
            target = self.upstream
        else:
            # e.g. /.well-known/oauth-protected-resource
            parts = urlsplit(self.upstream)
            target = f"{parts.scheme}://{parts.netloc}{path}"
        if request.query_string:
            target += "?" + request.query_string
        return target

    async def handle(self, request: web.Request) -> web.StreamResponse:
        target = self._upstream_url(request)
        if target is None:
            return web.Response(status=404, text="not found")

        body = await request.read() if request.can_read_body else None
        session_id = request.headers.get("Mcp-Session-Id", "")
        msg = parse_message(body) if (request.method == "POST" and body) else None

        if isinstance(msg, dict) and msg.get("method") == "notifications/cancelled":
            rpc_id = (msg.get("params") or {}).get("requestId")
            rid = self._pending.get((session_id, json.dumps(rpc_id)))
            if rid:
                await asyncio.get_running_loop().run_in_executor(None, self.broker.cancel, rid)

        if isinstance(msg, list):
            batch = msg
        elif msg is not None:
            batch = [msg]
        else:
            batch = []
        held = [m for m in batch if is_request(m) and m.get("method") in self.cfg.intercept_methods]
        if held:
            reason = await self._approve(held, session_id, request)
            if reason is not None:
                denials = [denial_response(m, reason) for m in batch if is_request(m)]
                return web.json_response(denials if isinstance(msg, list) else denials[0])

        is_init = isinstance(msg, dict) and msg.get("method") == "initialize"
        return await self._forward(request, target, body, init_msg=msg if is_init else None)

    async def _approve(self, held, session_id: str, request: web.Request) -> Optional[str]:
        """Returns None if everything was allowed, else the block reason."""
        loop = asyncio.get_running_loop()
        client = (self._client_by_session.get(session_id)
                  or request.headers.get("User-Agent", "remote client"))
        for m in held:
            req = build_approval_request(m, self.name, client, "http")
            key = (session_id, json.dumps(m.get("id")))
            self._pending[key] = req.request_id
            try:
                decision, reason = await loop.run_in_executor(None, self.broker.decide, req)
            except asyncio.CancelledError:
                # client hung up while the popup was open
                await loop.run_in_executor(None, self.broker.cancel, req.request_id)
                raise
            finally:
                self._pending.pop(key, None)
            log(f"{decision.upper()} {m.get('method')} {req.tool} ({reason})")
            if decision != "allow":
                return reason
        return None

    async def _forward(self, request: web.Request, target: str, body: Optional[bytes],
                       init_msg=None) -> web.StreamResponse:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        try:
            async with self.http.request(request.method, target, headers=headers, data=body,
                                         allow_redirects=False) as up:
                sid = up.headers.get("Mcp-Session-Id")
                if init_msg is not None and sid:
                    self._client_by_session[sid] = client_label(init_msg.get("params"))

                resp = web.StreamResponse(
                    status=up.status,
                    headers={k: v for k, v in up.headers.items() if k.lower() not in HOP_BY_HOP},
                )
                await resp.prepare(request)
                async for chunk in up.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (ClientError, asyncio.TimeoutError, OSError) as e:
            log(f"upstream error for {request.method} {target}: {e!r}")
            msg = parse_message(body) if body else None
            payload = {
                "jsonrpc": "2.0",
                "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32002,
                          "message": f"MCP Guard: upstream server unreachable ({type(e).__name__})"},
            }
            return web.json_response(payload, status=502)


def run_gateway(upstream: str, name: str, host: str, port: int, secret: Optional[str]) -> int:
    if secret == "auto":
        secret = secrets.token_urlsafe(18)
    gw = Gateway(upstream, name, path_secret=secret or None)
    gw.broker.ensure_broker()

    prefix = f"/{secret}" if secret else ""
    log(f"guarding '{name}' -> {upstream}")
    log(f"connector URL: http://{host}:{port}{prefix}/mcp  (expose it with a tunnel for claude.ai)")
    # handler_cancellation so a dropped request also closes its popup
    web.run_app(gw.app(), host=host, port=port, print=None, handler_cancellation=True)
    return 0
