"""Gateway in front of the demo Streamable-HTTP server."""

import asyncio
import subprocess
import sys
import socket
import time

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from mcp_guard.config import Config  # noqa: E402
from mcp_guard.client import BrokerClient  # noqa: E402
from mcp_guard.http_gateway import Gateway  # noqa: E402

from conftest import ROOT, ScriptedPrompter  # noqa: E402


@pytest.fixture
def upstream():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(ROOT / "examples" / "demo_http_server.py"), "--port", str(port)],
                            stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(5)


def run(coro):
    return asyncio.run(coro)


async def _client(gw):
    client = TestClient(TestServer(gw.app()))
    await client.start_server()
    return client


def call(mid, tool):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": tool, "arguments": {"path": "/x"}}}


HEADERS = {"Accept": "application/json, text/event-stream"}


def test_gateway_allows_blocks_and_passes_through(start_broker, upstream):
    prompter = ScriptedPrompter({"delete_file": "block"})
    start_broker(prompter)

    async def scenario():
        gw = Gateway(upstream, "remote-demo", cfg=Config.load(), broker=BrokerClient(Config.load()),
                     path_secret="s3cret")
        client = await _client(gw)
        try:
            # Wrong / missing secret path never reaches upstream.
            assert (await client.post("/mcp", json=call(1, "get_weather"))).status == 404
            init = await client.post("/s3cret/mcp", headers=HEADERS, json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-ai-web", "version": "1"}}})
            sid = init.headers["Mcp-Session-Id"]
            assert (await init.json())["result"]["serverInfo"]["name"] == "mcp-guard-demo-http"
            h = dict(HEADERS, **{"Mcp-Session-Id": sid})
            listed = await (await client.post("/s3cret/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).json()
            assert len(listed["result"]["tools"]) == 4
            ok = await (await client.post("/s3cret/mcp", headers=h, json=call(2, "get_weather"))).json()
            assert "sunny" in ok["result"]["content"][0]["text"]
            denied = await (await client.post("/s3cret/mcp", headers=h, json=call(3, "delete_file"))).json()
            assert denied["id"] == 3 and denied["result"]["isError"] is True
        finally:
            await client.close()

    run(scenario())
    assert [r.tool for r in prompter.asked] == ["get_weather", "delete_file"]
    assert prompter.asked[0].client == "claude-ai-web 1" and prompter.asked[0].transport == "http"


def test_gateway_concurrent_calls(start_broker, upstream):
    import threading
    gates = {"get_weather": threading.Event(), "write_note": threading.Event()}
    start_broker(ScriptedPrompter(gates=gates))

    async def scenario():
        gw = Gateway(upstream, "remote-demo", cfg=Config.load(), broker=BrokerClient(Config.load()))
        client = await _client(gw)
        try:
            t1 = asyncio.ensure_future(client.post("/mcp", headers=HEADERS, json=call(1, "get_weather")))
            t2 = asyncio.ensure_future(client.post("/mcp", headers=HEADERS, json=call(2, "write_note")))
            await asyncio.sleep(0.4)
            gates["write_note"].set()
            r2 = await asyncio.wait_for(t2, 5)
            assert not t1.done()
            gates["get_weather"].set()
            r1 = await asyncio.wait_for(t1, 5)
            assert (await r1.json())["id"] == 1 and (await r2.json())["id"] == 2
        finally:
            await client.close()

    run(scenario())


def test_gateway_upstream_down_returns_jsonrpc_error(start_broker):
    start_broker(ScriptedPrompter())

    async def scenario():
        gw = Gateway("http://127.0.0.1:1/mcp", "dead", cfg=Config.load(), broker=BrokerClient(Config.load()))
        client = await _client(gw)
        try:
            resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
            body = await resp.json()
            assert resp.status == 502 and body["id"] == 5 and "unreachable" in body["error"]["message"]
        finally:
            await client.close()

    run(scenario())
