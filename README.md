# mcp-guard

Intercepts MCP tool calls from Claude Desktop and Claude Web and asks you to
allow or block each one before it runs.

When Claude calls a tool, mcp-guard holds the request and pops up a dialog
(on top of whatever you're doing) showing the client, server, tool and
arguments. You pick **Allow**, **Always Allow** (this tool, until the broker
restarts) or **Block**. Allowed calls go to the server untouched. Blocked calls
never reach the server, and Claude gets back a "Blocked by the user" tool error.

How it works and why it's built this way: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Setup

Needs Python 3.9+.

```bash
git clone <repo-url> mcp-guard && cd mcp-guard
python3 -m venv .venv
.venv/bin/pip install -e '.[gateway]'    # [gateway] pulls in aiohttp, only needed for Claude Web
```

`mcp-guard install` writes the absolute path of this venv's python into
Claude's config, so don't move the venv afterwards (or rerun install if you do).

## Claude Desktop

```bash
.venv/bin/mcp-guard install     # wraps every stdio server in claude_desktop_config.json
.venv/bin/mcp-guard status
```

Then quit Claude Desktop completely (Cmd+Q) and open it again.

The broker (the process that shows the popups) starts by itself on the first
request. If you'd rather have it start at login (macOS):

```bash
.venv/bin/mcp-guard service install
```

You can also just run `.venv/bin/mcp-guard broker` in a terminal to watch it.

If you don't have an MCP server to test with, use the demo one. Add this to
`claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`,
Windows: `%APPDATA%\Claude\`) and run `mcp-guard install` again:

```json
{
  "mcpServers": {
    "demo": { "command": "python3", "args": ["/ABSOLUTE/PATH/mcp-guard/examples/demo_server.py"] }
  }
}
```

Then in Claude: *"Use the demo get_weather tool for Pune"* (allow it), and
*"Use the demo delete_file tool to delete /tmp/old.log"* (block it -
delete_file is fake anyway).

To undo: `mcp-guard uninstall` puts the original commands back, and
`mcp-guard service uninstall` removes the LaunchAgent.

## Claude Web (claude.ai)

claude.ai doesn't run MCP servers on your machine. Custom connectors are called
from Anthropic's backend, so you can't catch that traffic from the browser.
What you can do is put the gateway in front of the MCP server and register the
gateway as the connector:

```bash
# the real server (demo here, or any Streamable HTTP MCP endpoint)
.venv/bin/python examples/demo_http_server.py --port 9000

# gateway - prints a connector URL with a random secret in the path
.venv/bin/mcp-guard gateway --upstream http://127.0.0.1:9000/mcp --name demo-remote --port 8765

# make it reachable from the internet
cloudflared tunnel --url http://127.0.0.1:8765     # or: ngrok http 8765
```

In claude.ai: Settings > Connectors > Add custom connector, URL
`https://<tunnel-host>/<secret>/mcp`. Tool calls through that connector now pop
up on your machine. The same URL works as a custom connector in Claude Desktop.

For a local stdio server you'd need a stdio-to-HTTP bridge in front of it first
(supergateway, mcp-proxy, etc.) and then point `--upstream` at that.

## Config

`~/.mcp-guard/config.json`, everything optional:

```json
{
  "timeout_seconds": 120,
  "fail_mode": "block",
  "prompter": "auto",
  "intercept_methods": ["tools/call"],
  "rules": [
    {"server": "filesystem", "tool": "read_*", "action": "allow"},
    {"tool": "delete_*", "action": "block"}
  ],
  "log_arguments": true,
  "max_pending": 32,
  "autostart_broker": true,
  "port": 47823
}
```

- `timeout_seconds` - unanswered popups get blocked after this
- `fail_mode` - what to do if the broker can't be reached (`block` or `allow`)
- `prompter` - `auto` (native dialog on macOS, Tk elsewhere), `osascript`, `tk`, `static-allow`, `static-block`
- `intercept_methods` - add `resources/read` / `prompts/get` if you want those gated too
- `rules` - glob matches on server/tool/method, first match wins, checked before asking
- `max_pending` - max popups open at once, anything over that is blocked

## Other commands

```bash
mcp-guard status        # broker + which servers are wrapped
mcp-guard log -n 50     # recent decisions (~/.mcp-guard/audit.jsonl)
mcp-guard stop          # stop the broker
```

Proxy logs end up in Claude's MCP logs, e.g.
`~/Library/Logs/Claude/mcp-server-demo.log`.

## Without Claude

```bash
.venv/bin/python examples/concurrent_demo.py
```

Starts the guarded demo server and fires three tool calls at once. You get
three popups; answer them in any order.

## Tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The proxy tests spawn a real proxy + demo server subprocess and use a scripted
prompter instead of real popups.

## Layout

```
mcp_guard/
  broker.py        popups, rules, audit log, localhost API
  client.py        what the proxies use to talk to the broker
  stdio_proxy.py   Claude Desktop side
  http_gateway.py  Claude Web / remote connector side
  installer.py     edits claude_desktop_config.json
  policy.py        rules + "always allow"
  protocol.py      JSON-RPC bits
  audit.py
  service.py       LaunchAgent
  prompters/       osascript, tk, static
examples/          demo servers + concurrent client
tests/
```
