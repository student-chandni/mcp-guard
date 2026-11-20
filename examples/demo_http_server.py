#!/usr/bin/env python3
"""Same demo tools over Streamable HTTP (stateless, JSON responses only).

    python examples/demo_http_server.py --port 9000
"""

import argparse
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from demo_server import TOOLS, call_tool  # noqa: E402


def respond(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "mcp-guard-demo-http", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params", {})
        result = call_tool(p.get("name"), p.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("demo-http:", fmt % args, file=sys.stderr)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        msg = json.loads(body)
        out = [r for r in map(respond, msg) if r] if isinstance(msg, list) else respond(msg)
        if out is None or out == []:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            self.send_header("Mcp-Session-Id", uuid.uuid4().hex)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # no server->client SSE stream in this demo
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    a = ap.parse_args()
    print(f"demo MCP server on http://127.0.0.1:{a.port}/mcp", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
