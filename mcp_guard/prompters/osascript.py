import subprocess

from mcp_guard.prompters.base import (
    ALLOW, ALLOW_ALWAYS, BLOCK, CANCELLED, TIMEOUT, Prompter, format_details,
)

# Text goes in via argv so nothing in the tool arguments can end up as
# AppleScript. "activate" brings the dialog in front of whatever app is focused.
# Block is the default button so hitting Enter by accident doesn't approve.
SCRIPT = [
    "on run argv",
    "activate",
    'display dialog (item 1 of argv) with title (item 2 of argv) '
    'buttons {"Block", "Always Allow", "Allow"} default button "Block" '
    'cancel button "Block" with icon caution giving up after (item 3 of argv as integer)',
    "end run",
]


class OsaScriptPrompter(Prompter):
    name = "osascript"

    def ask(self, req, timeout, cancelled):
        cmd = ["osascript"]
        for line in SCRIPT:
            cmd += ["-e", line]
        cmd += ["--", format_details(req), f"MCP Guard: approve {req.tool}?", str(max(1, int(timeout)))]

        # one osascript process per request -> several dialogs can be open at once
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        while True:
            try:
                out, _ = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancelled.is_set():
                    proc.kill()
                    proc.communicate()
                    return CANCELLED

        if proc.returncode != 0:  # Block/Escape -> "User canceled"
            return BLOCK
        if "gave up:true" in out:
            return TIMEOUT
        if "button returned:Always Allow" in out:
            return ALLOW_ALWAYS
        if "button returned:Allow" in out:
            return ALLOW
        return BLOCK
