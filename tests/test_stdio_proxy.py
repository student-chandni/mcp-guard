"""End-to-end: a real proxy subprocess wrapping the demo server, the way
Claude Desktop runs it, with an in-process broker answering via a script."""

import json
import queue
import subprocess
import sys
import threading
import time

import pytest

from conftest import DEMO_SERVER, ScriptedPrompter, write_config


class Session:
    def __init__(self, env, command=None):
        cmd = command or [sys.executable, DEMO_SERVER]
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_guard", "proxy", "--name", "demo", "--", *cmd],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in iter(self.proc.stdout.readline, b""):
            self.lines.put(json.loads(line))

    def send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def recv(self, timeout=10):
        return self.lines.get(timeout=timeout)

    def initialize(self):
        self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "claude-ai", "version": "0.14"}}})
        assert self.recv()["result"]["serverInfo"]["name"] == "mcp-guard-demo"
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, mid, tool, args=None):
        self.send({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                   "params": {"name": tool, "arguments": args or {}}})

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(10)


@pytest.fixture
def session(child_env):
    sessions = []

    def _make(**kw):
        s = Session(child_env, **kw)
        sessions.append(s)
        return s

    yield _make
    for s in sessions:
        s.close()


def test_allow_forwards_and_block_denies(start_broker, session):
    prompter = ScriptedPrompter({"delete_file": "block"})
    start_broker(prompter)
    s = session()
    s.initialize()
    s.call(1, "get_weather", {"city": "Pune"})
    allowed = s.recv()
    assert allowed["id"] == 1 and "sunny in Pune" in allowed["result"]["content"][0]["text"]
    s.call(2, "delete_file", {"path": "/important"})
    blocked = s.recv()
    assert blocked["id"] == 2 and blocked["result"]["isError"] is True
    assert "Blocked by the user" in blocked["result"]["content"][0]["text"]
    # The popup saw the real client identity, server name and arguments.
    seen = prompter.asked[1]
    assert (seen.client, seen.server, seen.tool, seen.arguments) == (
        "claude-ai 0.14", "demo", "delete_file", {"path": "/important"})


def test_non_tool_traffic_passes_while_a_call_is_pending(start_broker, session):
    gate = threading.Event()
    start_broker(ScriptedPrompter(gates={"write_note": gate}))
    s = session()
    s.initialize()
    s.call(1, "write_note", {"title": "t", "body": "b"})
    s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    first = s.recv()
    assert first["id"] == 2 and len(first["result"]["tools"]) == 4
    gate.set()
    assert s.recv()["id"] == 1


def test_simultaneous_calls_resolve_in_decision_order(start_broker, session):
    gates = {t: threading.Event() for t in ("get_weather", "write_note", "delete_file")}
    start_broker(ScriptedPrompter({"write_note": "block"}, gates=gates))
    s = session()
    s.initialize()
    for i, tool in enumerate(gates, start=1):
        s.call(i, tool, {"city": "x", "title": "n", "body": "b", "path": "/p"})
    time.sleep(0.5)
    # User answers the last popup first.
    order = []
    for tool in ("delete_file", "write_note", "get_weather"):
        gates[tool].set()
        order.append(s.recv())
    assert [r["id"] for r in order] == [3, 2, 1]
    assert [bool(r["result"].get("isError")) for r in order] == [False, True, False]


def test_client_cancellation_closes_popup_and_sends_no_response(start_broker, session):
    gate = threading.Event()
    prompter = ScriptedPrompter(gates={"slow_task": gate})
    start_broker(prompter)
    s = session()
    s.initialize()
    s.call(1, "slow_task", {"seconds": 0})
    time.sleep(0.4)
    s.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
    s.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert s.recv()["id"] == 2
    with pytest.raises(queue.Empty):
        s.recv(timeout=1)


def test_broker_down_fails_closed(guard_home, session):
    write_config(guard_home)  # no broker started, autostart disabled
    s = session()
    s.initialize()
    s.call(1, "delete_file", {"path": "/x"})
    resp = s.recv()
    assert resp["result"]["isError"] and "broker-unavailable" in resp["result"]["content"][0]["text"]


def test_batch_is_all_or_nothing(start_broker, session):
    start_broker(ScriptedPrompter({"delete_file": "block"}))
    s = session()
    s.initialize()
    s.send([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_weather", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete_file", "arguments": {}}},
    ])
    resp = s.recv()
    assert isinstance(resp, list) and all(r["result"]["isError"] for r in resp)


def test_proxy_exits_when_client_disconnects(start_broker, session):
    start_broker(ScriptedPrompter())
    s = session()
    s.initialize()
    s.proc.stdin.close()
    assert s.proc.wait(10) is not None


def test_proxy_reports_missing_server_binary(guard_home, session):
    write_config(guard_home)
    s = session(command=["/nonexistent/mcp-server"])
    assert s.proc.wait(10) == 127
    assert b"could not start MCP server" in s.proc.stderr.read()
