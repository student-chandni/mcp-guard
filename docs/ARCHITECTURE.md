# Architecture

## Where to intercept

The first question was where Claude's MCP traffic actually goes, because that
decides where you can hold a request:

- **Claude Desktop + local servers**: Desktop starts each server from
  `claude_desktop_config.json` as a child process and speaks JSON-RPC over its
  stdin/stdout. Nothing goes over the network. The way in is to be the process
  Desktop starts.
- **Claude Web (and Desktop's custom connectors)**: the connector is called by
  Anthropic's backend over HTTPS. The browser never talks to the MCP server, so
  the only place to hold the request is on the server side.

So there are two interceptors, one for each case, and both ask a single broker
process for decisions.

Things I looked at and dropped:

- Sniffing / MITM: doesn't see stdio at all, doesn't see claude.ai connector
  traffic (it isn't on this machine), and would need a trusted root CA.
- A browser extension for claude.ai: it can see the UI, but the actual call is
  server-to-server, so it can't hold or drop it.
- Patching the Electron app: breaks on every update and breaks code signing.

```
Claude Desktop --stdio--> stdio proxy (one per server) --stdio--> MCP server
                               |
                               |  POST /decide  (localhost + token)
                               v
                            broker  --> popup, rules, audit log
                               ^
                               |  POST /decide
claude.ai --https--> tunnel --> http gateway --http--> remote MCP server
```

## Broker (`broker.py`)

Desktop starts one proxy per server, and the gateway is another process, so
popups, rules, "always allow" grants and the audit log live in one place
instead of being duplicated per proxy. It also keeps GUI code out of the
proxies, where stdout has to stay clean protocol output.

- `ThreadingHTTPServer` on 127.0.0.1. `/decide` is a long poll that returns
  once the user answers.
- One thread per request, so simultaneous calls don't wait on each other.
- The popup runs on a pool thread with a watchdog (timeout + 15s), so a stuck
  popup can't hold a request forever.
- The main thread goes to the prompter, because Tk needs it.

Popup backends (`prompters/`):

- macOS: `osascript` `display dialog` with `activate`, so it shows up above
  other apps. One osascript process per request, so several can be open at
  once. Text is passed via argv (no AppleScript injection), and Block is the
  default button.
- Windows/Linux: Tk, one topmost window per request, with a countdown.
- `static-allow` / `static-block` for headless use. With no GUI at all, `auto`
  ends up blocking everything.

## stdio proxy (`stdio_proxy.py`)

`mcp-guard install` rewrites each stdio entry to

```
python -m mcp_guard proxy --name <server> -- <original command...>
```

Keeping the original after `--` means uninstall doesn't need any extra state.

- Reads newline-delimited JSON-RPC from Claude. Requests whose method is in
  `intercept_methods` (default just `tools/call`) get their own thread and wait
  for the broker. Everything else (initialize, tools/list, notifications,
  responses to sampling, etc.) is forwarded immediately. Server -> client
  traffic isn't touched.
- Allow: the original line is forwarded byte for byte, so what you approved is
  exactly what runs.
- Block: the proxy answers itself with a `tools/call` result that has
  `isError: true` and the text "Blocked by the user...". I used a tool error
  rather than a JSON-RPC error because Claude shows it to the model as a
  failed tool call and the conversation carries on. Other methods get a
  JSON-RPC error (-32001).
- The client name in the popup comes from `initialize.clientInfo`.
- `notifications/cancelled` closes the popup, and per the spec no response is
  sent.
- Batches are all or nothing.
- Shutdown: stdin EOF -> close server stdin -> SIGTERM -> SIGKILL. If the
  server dies the proxy exits with its code. A missing server binary exits 127
  with a message in Claude's MCP log.

## HTTP gateway (`http_gateway.py`)

aiohttp reverse proxy for Streamable HTTP.

- POSTs containing a held method go through the broker first; if blocked, the
  gateway returns the denial itself.
- Everything else (initialize, GET SSE streams, DELETE, OAuth `.well-known`
  paths) is streamed through chunk by chunk with decompression off. Hop-by-hop
  headers are dropped; `Mcp-Session-Id`, `Authorization` etc. pass through.
- If Claude drops the request while a popup is open (`handler_cancellation`),
  the popup is cancelled.
- Upstream down -> 502 with a JSON-RPC error for the same id.
- Random secret path prefix by default so the public tunnel URL isn't open to
  anyone who finds it.

## Failure handling

| What happens | Result |
|---|---|
| broker not running | proxy starts it, waits up to 8s, retries once |
| broker restarted (new token) | client rereads broker.json and retries |
| broker unreachable | `fail_mode`, default block |
| nobody answers | blocked after `timeout_seconds` |
| popup code throws | blocked |
| too many popups | over `max_pending` gets blocked straight away |
| two brokers start | second one can't bind the port and exits |
| Claude quits | proxy cancels its popups and stops the server |
| MCP server crashes | proxy exits with the same code |
| remote upstream down | 502 + JSON-RPC error |
| audit log write fails | ignored |

## Security notes

- Broker listens on 127.0.0.1 only. The token is random per start, stored in
  `~/.mcp-guard/broker.json` (dir 0700, file 0600) and compared with
  `hmac.compare_digest`. Requests with an `Origin` header or a non-localhost
  `Host` are rejected, which covers web pages trying to hit it (CSRF / DNS
  rebinding).
- Popup text has control characters and bidi overrides stripped, and long
  arguments are cut off with a marker.
- Defaults fail closed: timeout, errors, no GUI and broker down all block.
- "Always allow" is per (server, tool) and only lasts until the broker
  restarts. Anything permanent should go in `rules`.
- The audit log stores a sha256 of the arguments plus an optional 500 char
  preview (`log_arguments: false` turns the preview off).

What it doesn't protect against: anything already running as your user can
read the token, edit Claude's config or run the server directly. It's a check
on what the model does, not a sandbox. Servers that aren't in
`claude_desktop_config.json` (e.g. Desktop Extensions installed through the UI)
aren't wrapped.
