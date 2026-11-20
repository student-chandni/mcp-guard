"""stdio proxy that sits between Claude Desktop and a local MCP server.

`mcp-guard install` changes the server command in claude_desktop_config.json to

    python -m mcp_guard proxy --name <server> -- <original command...>

so Claude talks to us and we talk to the real server. tools/call requests are
held until the broker says allow/block; everything else is passed straight
through, and nothing coming back from the server is touched.
"""

import json
import signal
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional

from mcp_guard.client import BrokerClient
from mcp_guard.config import Config
from mcp_guard.protocol import (
    build_approval_request, client_label, denial_response, dumps_line, is_request,
    parse_message,
)


def log(msg: str) -> None:
    # stdout belongs to the protocol, so logs go to stderr
    # (Claude puts it in ~/Library/Logs/Claude/mcp-server-<name>.log)
    print(f"mcp-guard[proxy]: {msg}", file=sys.stderr, flush=True)


class StdioProxy:
    def __init__(self, name: str, command: List[str], cfg: Optional[Config] = None,
                 stdin=None, stdout=None, broker: Optional[BrokerClient] = None):
        self.name = name
        self.command = command
        self.cfg = cfg or Config.load()
        self.broker = broker or BrokerClient(self.cfg)
        self.client_in = stdin or sys.stdin.buffer
        self.client_out = stdout or sys.stdout.buffer
        self.client = "unknown client"
        self.child: Optional[subprocess.Popen] = None

        self._out_lock = threading.Lock()
        self._child_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: Dict[str, str] = {}   # json(rpc id) -> broker request id
        self._cancelled = set()

    def send_to_client(self, data: bytes) -> None:
        with self._out_lock:
            try:
                self.client_out.write(data)
                self.client_out.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass  # client is gone, we'll see EOF on stdin

    def send_to_child(self, data: bytes) -> None:
        with self._child_lock:
            try:
                self.child.stdin.write(data)
                self.child.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                log("server stdin is closed, dropping message")

    def needs_approval(self, msg: Any) -> bool:
        return is_request(msg) and msg.get("method") in self.cfg.intercept_methods

    def on_client_line(self, line: bytes) -> None:
        msg = parse_message(line)
        if isinstance(msg, dict):
            if msg.get("method") == "initialize":
                self.client = client_label(msg.get("params"))
            elif msg.get("method") == "notifications/cancelled":
                self._on_cancel(msg.get("params") or {})

        held = self.needs_approval(msg) or (
            isinstance(msg, list) and any(self.needs_approval(m) for m in msg))
        if held:
            # own thread per request so one open popup doesn't stall the rest
            threading.Thread(target=self._approve_and_forward, args=(line, msg),
                             daemon=True).start()
        else:
            self.send_to_child(line)

    def _on_cancel(self, params: Dict[str, Any]) -> None:
        key = json.dumps(params.get("requestId"))
        with self._state_lock:
            request_id = self._pending.get(key)
            if request_id:
                self._cancelled.add(key)
        if request_id:
            log(f"client cancelled request {key}, closing popup")
            self.broker.cancel(request_id)

    def _approve_and_forward(self, line: bytes, msg: Any) -> None:
        batch = msg if isinstance(msg, list) else [msg]
        blocked_reason = None

        for m in batch:
            if not self.needs_approval(m):
                continue
            req = build_approval_request(m, self.name, self.client, "stdio")
            key = json.dumps(m.get("id"))
            with self._state_lock:
                self._pending[key] = req.request_id
            try:
                decision, reason = self.broker.decide(req)
            finally:
                with self._state_lock:
                    self._pending.pop(key, None)
                    was_cancelled = key in self._cancelled
                    self._cancelled.discard(key)

            log(f"{decision.upper()} {m.get('method')} {req.tool} ({reason})")
            if was_cancelled:
                return  # spec says don't respond to cancelled requests
            if decision != "allow":
                blocked_reason = reason
                break

        if blocked_reason is None:
            # forward the original bytes, not a re-serialized copy
            self.send_to_child(line)
            return

        # batches are all or nothing
        denials = [denial_response(m, blocked_reason) for m in batch if is_request(m)]
        self.send_to_client(dumps_line(denials if isinstance(msg, list) else denials[0]))

    def _pump_child_stdout(self) -> None:
        for line in iter(self.child.stdout.readline, b""):
            self.send_to_client(line)

    def _pump_client_stdin(self) -> None:
        try:
            for line in iter(self.client_in.readline, b""):
                if line.strip():
                    self.on_client_line(line)
        finally:
            # Claude closed stdin (quit or server disabled)
            self.cancel_all_pending()
            with self._child_lock:
                self.terminate_child()

    def cancel_all_pending(self) -> None:
        with self._state_lock:
            ids = list(self._pending.values())
        for request_id in ids:
            self.broker.cancel(request_id)

    def terminate_child(self, grace: float = 5.0) -> None:
        if not self.child or self.child.poll() is not None:
            return
        # closing stdin is the normal MCP shutdown, then TERM, then KILL
        try:
            self.child.stdin.close()
        except OSError:
            pass
        try:
            self.child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.child.terminate()
            try:
                self.child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.child.kill()

    def run(self) -> int:
        try:
            self.child = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=None)
        except OSError as e:
            log(f"could not start MCP server {self.command!r}: {e}")
            return 127

        log(f"guarding server '{self.name}' (pid {self.child.pid}): {' '.join(self.command)}")
        out = threading.Thread(target=self._pump_child_stdout, daemon=True)
        out.start()
        threading.Thread(target=self._pump_client_stdin, daemon=True).start()
        try:
            out.join()  # returns when the server closes stdout
            code = self.child.wait()
        finally:
            self.cancel_all_pending()
            self.terminate_child()
        log(f"server '{self.name}' exited with code {code}")
        return code


def run_proxy(name: str, command: List[str]) -> int:
    if not command:
        log("no server command given (usage: mcp-guard proxy --name NAME -- CMD ...)")
        return 2
    proxy = StdioProxy(name, command)

    def on_signal(signum, frame):
        log(f"got signal {signum}, shutting down")
        proxy.cancel_all_pending()
        proxy.terminate_child(grace=2.0)
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    # start the broker now so the first tool call doesn't have to wait for it
    threading.Thread(target=proxy.broker.ensure_broker, daemon=True).start()
    return proxy.run()
