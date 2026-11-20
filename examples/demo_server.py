#!/usr/bin/env python3
"""Small stdio MCP server for trying out mcp-guard. No dependencies.

get_weather and slow_task are harmless, write_note writes to a temp dir,
delete_file only pretends.
"""

import json
import os
import sys
import tempfile
import threading
import time

NOTES_DIR = os.path.join(tempfile.gettempdir(), "mcp-guard-demo-notes")
_out = threading.Lock()

TOOLS = [
    {"name": "get_weather", "description": "Get the current weather for a city",
     "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}},
    {"name": "write_note", "description": "Save a note to disk",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
                     "required": ["title", "body"]}},
    {"name": "delete_file", "description": "Delete a file (demo: simulated, nothing is deleted)",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "slow_task", "description": "Wait for N seconds and report back",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
]


def send(msg):
    with _out:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def text(t, error=False):
    return {"content": [{"type": "text", "text": t}], "isError": error}


def call_tool(name, args):
    if name == "get_weather":
        return text(f"It is 22C and sunny in {args.get('city', 'somewhere')}.")
    if name == "write_note":
        os.makedirs(NOTES_DIR, exist_ok=True)
        safe = "".join(c for c in str(args.get("title", "note")) if c.isalnum() or c in "-_ ") or "note"
        path = os.path.join(NOTES_DIR, safe + ".txt")
        with open(path, "w") as fh:
            fh.write(str(args.get("body", "")))
        return text(f"Saved note to {path}")
    if name == "delete_file":
        return text(f"(simulated) deleted {args.get('path')}")
    if name == "slow_task":
        secs = min(float(args.get("seconds", 1)), 30)
        time.sleep(secs)
        return text(f"Finished after {secs:g}s")
    return text(f"Unknown tool {name}", error=True)


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return  # notification
    if method == "initialize":
        result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "mcp-guard-demo", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params", {})
        result = call_tool(p.get("name"), p.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
        return
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        # thread per request so slow_task calls can overlap
        threading.Thread(target=handle, args=(msg,), daemon=True).start()


if __name__ == "__main__":
    main()
