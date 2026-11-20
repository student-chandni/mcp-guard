#!/usr/bin/env python3
"""Pretend to be Claude Desktop: start the guarded demo server and send three
tool calls at once. Answer the popups in any order."""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

CALLS = [
    ("get_weather", {"city": "Bengaluru"}),
    ("write_note", {"title": "groceries", "body": "milk, eggs"}),
    ("delete_file", {"path": "/Users/me/Documents/taxes-2026.pdf"}),
]


def main():
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp_guard", "proxy", "--name", "demo", "--",
         sys.executable, os.path.join(HERE, "demo_server.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "concurrent-demo", "version": "1.0"}}})
    print("initialize ->", proc.stdout.readline().strip()[:100])
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    for i, (tool, args) in enumerate(CALLS, start=1):
        send({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        print(f"sent #{i} {tool} {args}")

    names = {i: t for i, (t, _) in enumerate(CALLS, start=1)}
    for _ in CALLS:
        resp = json.loads(proc.stdout.readline())
        res = resp.get("result", {})
        status = "BLOCKED" if res.get("isError") else "ALLOWED"
        print(f"#{resp['id']} {names[resp['id']]}: {status}: {res['content'][0]['text']}")

    proc.stdin.close()
    proc.wait(timeout=10)


if __name__ == "__main__":
    main()
