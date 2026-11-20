"""Approval broker.

One background process that shows the popups, applies rules and writes the
audit log. The stdio proxies (one per MCP server) and the HTTP gateway all ask
it for decisions over a small localhost HTTP API:

    GET  /health
    POST /decide          body = ApprovalRequest, returns when the user answers
    POST /cancel/<id>
    GET  /pending

Everything except /health needs the bearer token from ~/.mcp-guard/broker.json.
"""

import errno
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional, Tuple

from mcp_guard import config as cfgmod
from mcp_guard.audit import AuditLog
from mcp_guard.policy import Policy
from mcp_guard.prompters import (
    ALLOW, ALLOW_ALWAYS, BLOCK, CANCELLED, TIMEOUT, Prompter, create_prompter,
)
from mcp_guard.protocol import ApprovalRequest

MAX_BODY = 1024 * 1024


class Broker:
    def __init__(self, cfg: cfgmod.Config, prompter: Prompter, audit: AuditLog):
        self.cfg = cfg
        self.prompter = prompter
        self.audit = audit
        self.policy = Policy(cfg.rules)
        self._pending: Dict[str, Tuple[ApprovalRequest, threading.Event]] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max(4, cfg.max_pending),
                                        thread_name_prefix="prompt")

    def decide(self, req: ApprovalRequest) -> Tuple[str, str]:
        started = time.monotonic()
        decision, reason = self._decide(req)
        self.audit.record(req, decision, reason, started)
        return decision, reason

    def _decide(self, req: ApprovalRequest) -> Tuple[str, str]:
        ruled = self.policy.evaluate(req)
        if ruled:
            return ruled, "policy-rule"

        cancelled = threading.Event()
        with self._lock:
            if len(self._pending) >= self.cfg.max_pending:
                return BLOCK, "too-many-pending"
            self._pending[req.request_id] = (req, cancelled)

        try:
            # Run the popup on a pool thread so we can give up on it if the
            # prompter hangs for some reason.
            fut = self._pool.submit(self.prompter.ask, req, self.cfg.timeout_seconds, cancelled)
            try:
                answer = fut.result(timeout=self.cfg.timeout_seconds + 15)
            except FutureTimeout:
                cancelled.set()
                answer = TIMEOUT
            except Exception as e:
                print(f"mcp-guard broker: prompter error: {e!r}", file=sys.stderr)
                return BLOCK, "prompter-error"
        finally:
            with self._lock:
                self._pending.pop(req.request_id, None)

        if answer == ALLOW_ALWAYS:
            self.policy.grant(req.server, req.tool)
            return ALLOW, "user-always"
        if answer == ALLOW:
            return ALLOW, "user"
        if answer == TIMEOUT:
            return BLOCK, "timeout"
        if answer == CANCELLED:
            return BLOCK, "cancelled"
        return BLOCK, "user"

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            entry = self._pending.get(request_id)
        if entry:
            entry[1].set()
        return entry is not None

    def pending(self):
        with self._lock:
            return [r.to_dict() for r, _ in self._pending.values()]

    def shutdown(self) -> None:
        with self._lock:
            for _, ev in self._pending.values():
                ev.set()
        self._pool.shutdown(wait=False)


def make_handler(broker: Broker, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "mcp-guard"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            # Reject anything that looks like it came from a browser (Origin
            # header, or a Host that isn't localhost = DNS rebinding) before
            # even looking at the token.
            if self.headers.get("Origin"):
                return False
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost"):
                return False
            got = self.headers.get("Authorization", "")
            return hmac.compare_digest(got.encode(), f"Bearer {token}".encode())

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True, "pid": os.getpid(),
                                        "pending": len(broker.pending())})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            if self.path == "/pending":
                return self._send(200, {"pending": broker.pending()})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "request too large"})
            raw = self.rfile.read(length) if length else b""

            if self.path == "/decide":
                try:
                    req = ApprovalRequest.from_dict(json.loads(raw))
                    req.server, req.tool, req.method = str(req.server), str(req.tool), str(req.method)
                except (ValueError, TypeError) as e:
                    return self._send(400, {"error": f"bad request: {e}"})
                decision, reason = broker.decide(req)
                return self._send(200, {"decision": decision, "reason": reason})

            if self.path.startswith("/cancel/"):
                found = broker.cancel(self.path[len("/cancel/"):])
                return self._send(200, {"cancelled": found})

            self._send(404, {"error": "not found"})

    return Handler


class BrokerServer:
    def __init__(self, cfg: cfgmod.Config, prompter: Prompter):
        self.cfg = cfg
        self.token = secrets.token_urlsafe(32)
        self.broker = Broker(cfg, prompter, AuditLog(cfgmod.audit_path(), cfg.log_arguments))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", cfg.port),
                                         make_handler(self.broker, self.token))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.stop = threading.Event()

    def start(self) -> None:
        state = {"port": self.port, "token": self.token, "pid": os.getpid()}
        cfgmod.write_private(cfgmod.state_path(), json.dumps(state))
        threading.Thread(target=self.httpd.serve_forever, name="broker-http", daemon=True).start()

    def close(self) -> None:
        self.stop.set()
        self.broker.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        # only remove the state file if it's still ours
        state = cfgmod.read_state()
        if state and state.get("pid") == os.getpid():
            try:
                cfgmod.state_path().unlink()
            except OSError:
                pass


def run_broker(prompter_name: Optional[str] = None) -> int:
    cfg = cfgmod.Config.load()
    prompter = create_prompter(prompter_name or cfg.prompter)
    try:
        server = BrokerServer(cfg, prompter)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            # fixed port doubles as a single-instance lock
            print(f"mcp-guard broker: port {cfg.port} in use, another broker is probably running",
                  file=sys.stderr)
            return 0
        raise

    server.start()
    print(f"mcp-guard broker listening on 127.0.0.1:{server.port} "
          f"(pid {os.getpid()}, popup: {prompter.name})", file=sys.stderr)

    def on_signal(signum, frame):
        server.stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        prompter.run_mainloop(server.stop)
    finally:
        server.close()
        print("mcp-guard broker stopped", file=sys.stderr)
    return 0
