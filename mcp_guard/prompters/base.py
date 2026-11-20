import json
import threading
from typing import Any

from mcp_guard.protocol import ApprovalRequest

ALLOW = "allow"
ALLOW_ALWAYS = "allow_always"
BLOCK = "block"
TIMEOUT = "timeout"
CANCELLED = "cancelled"

MAX_ARGS_CHARS = 900

# bidi overrides/isolates - could be used to make the popup text lie
_BIDI = {0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}


def sanitize(text: str) -> str:
    return "".join(
        ch for ch in text
        if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127 and ord(ch) not in _BIDI)
    )


def format_arguments(arguments: Any) -> str:
    try:
        text = json.dumps(arguments, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(arguments)
    if len(text) > MAX_ARGS_CHARS:
        text = text[:MAX_ARGS_CHARS] + f"\n... ({len(text) - MAX_ARGS_CHARS} more characters)"
    return sanitize(text)


def format_details(req: ApprovalRequest) -> str:
    label = {"tools/call": "Tool", "resources/read": "Resource", "prompts/get": "Prompt"}
    header = (
        f"{req.client} wants to run an MCP request.\n\n"
        f"Server:     {req.server}  ({req.transport})\n"
        f"{label.get(req.method, 'Target')}:       {req.tool}\n"
        f"Method:     {req.method}\n\n"
        "Arguments:\n"
    )
    return sanitize(header) + format_arguments(req.arguments)


class Prompter:
    name = "base"

    def ask(self, req: ApprovalRequest, timeout: float, cancelled: threading.Event) -> str:
        """Called from a worker thread. Blocks until answered; should return
        quickly once `cancelled` is set."""
        raise NotImplementedError

    def run_mainloop(self, stop: threading.Event) -> None:
        # Tk needs the main thread, others just idle here
        while not stop.wait(0.5):
            pass


class StaticPrompter(Prompter):
    def __init__(self, answer: str):
        self.name = f"static-{answer}"
        self.answer = answer

    def ask(self, req, timeout, cancelled):
        return self.answer
