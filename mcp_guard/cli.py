import argparse
import json
import os
import signal
import sys
from pathlib import Path

from mcp_guard import config as cfgmod


def _cmd_broker(args) -> int:
    from mcp_guard.broker import run_broker
    return run_broker(args.prompter)


def _cmd_proxy(args) -> int:
    from mcp_guard.stdio_proxy import run_proxy
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run_proxy(args.name, command)


def _cmd_gateway(args) -> int:
    try:
        from mcp_guard.http_gateway import run_gateway
    except ImportError:
        print("The gateway needs aiohttp: pip install 'mcp-guard[gateway]'", file=sys.stderr)
        return 1
    return run_gateway(args.upstream, args.name, args.host, args.port, args.secret)


def _cmd_install(args, install: bool) -> int:
    from mcp_guard import installer
    path = Path(args.config) if args.config else installer.default_config_path()
    try:
        changed, skipped, backup = installer.apply(path, install, only=args.server or None)
    except (OSError, ValueError) as e:
        print(f"Could not update {path}: {e}", file=sys.stderr)
        return 1
    verb = "Guarded" if install else "Restored"
    print(f"{verb}: {', '.join(changed) if changed else 'nothing to change'}  ({path})")
    for s in skipped:
        print(f"Skipped: {s}")
    if backup:
        print(f"Backup: {backup}")
    if changed:
        print("Restart Claude Desktop (Cmd+Q, then reopen) to apply.")
    return 0


def _cmd_status(args) -> int:
    from mcp_guard import installer
    from mcp_guard.client import BrokerClient
    state = cfgmod.read_state()
    if BrokerClient().healthy():
        print(f"Broker: running (pid {state['pid']}, 127.0.0.1:{state['port']})")
    else:
        print("Broker: not running (starts automatically on the first MCP request)")
    path = Path(args.config) if args.config else installer.default_config_path()
    print(f"Claude Desktop config: {path}")
    for name, st in installer.status(path):
        print(f"  {name}: {st}")
    print(f"Audit log: {cfgmod.audit_path()}")
    return 0


def _cmd_stop(_args) -> int:
    state = cfgmod.read_state()
    if not state:
        print("Broker is not running")
        return 0
    try:
        os.kill(int(state["pid"]), signal.SIGTERM)
        print(f"Sent SIGTERM to broker pid {state['pid']}")
    except (ProcessLookupError, PermissionError, ValueError):
        print("Broker process not found; removing stale state file")
        cfgmod.state_path().unlink(missing_ok=True)
    return 0


def _cmd_log(args) -> int:
    path = cfgmod.audit_path()
    if not path.exists():
        print("No decisions recorded yet")
        return 0
    lines = path.read_text(encoding="utf-8").splitlines()[-args.n:]
    for line in lines:
        e = json.loads(line)
        print(f"{e['ts']}  {e['decision'].upper():5}  {e['server']}/{e['tool']}  "
              f"[{e['reason']}, {e['wait_ms']}ms, {e['client']}]")
    return 0


def _cmd_service(args) -> int:
    from mcp_guard import service
    return service.install_service() if args.action == "install" else service.uninstall_service()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-guard", description="Approve or block MCP tool calls from Claude.")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("broker", help="run the approval broker (popups, policy, audit log)")
    b.add_argument("--prompter", choices=["auto", "osascript", "tk", "static-allow", "static-block"],
                   help="popup backend (default from config: auto)")
    b.set_defaults(func=_cmd_broker)

    x = sub.add_parser("proxy", help="(used by Claude Desktop) wrap a stdio MCP server")
    x.add_argument("--name", required=True, help="server name shown in the popup")
    x.add_argument("command", nargs=argparse.REMAINDER, help="-- <server command> [args...]")
    x.set_defaults(func=_cmd_proxy)

    g = sub.add_parser("gateway", help="approval gateway in front of a remote (HTTP) MCP server")
    g.add_argument("--upstream", required=True, help="real MCP endpoint, e.g. https://example.com/mcp")
    g.add_argument("--name", required=True, help="server name shown in the popup")
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--secret", default="auto",
                   help="secret URL path prefix ('auto' = random, '' = none)")
    g.set_defaults(func=_cmd_gateway)

    for name, install in (("install", True), ("uninstall", False)):
        i = sub.add_parser(name, help=f"{'wrap' if install else 'unwrap'} stdio servers in claude_desktop_config.json")
        i.add_argument("--config", help="path to claude_desktop_config.json")
        i.add_argument("--server", action="append", help="only this server (repeatable)")
        i.set_defaults(func=lambda a, _i=install: _cmd_install(a, _i))

    s = sub.add_parser("status", help="show broker and guarded servers")
    s.add_argument("--config", help="path to claude_desktop_config.json")
    s.set_defaults(func=_cmd_status)

    sub.add_parser("stop", help="stop the broker").set_defaults(func=_cmd_stop)

    lg = sub.add_parser("log", help="show recent decisions")
    lg.add_argument("-n", type=int, default=20)
    lg.set_defaults(func=_cmd_log)

    v = sub.add_parser("service", help="run the broker at login (macOS LaunchAgent)")
    v.add_argument("action", choices=["install", "uninstall"])
    v.set_defaults(func=_cmd_service)
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    sys.exit(args.func(args))
