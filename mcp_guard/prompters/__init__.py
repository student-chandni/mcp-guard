import shutil
import sys

from mcp_guard.prompters.base import (  # noqa: F401
    ALLOW, ALLOW_ALWAYS, BLOCK, CANCELLED, TIMEOUT, Prompter, StaticPrompter,
)


def create_prompter(name: str = "auto") -> Prompter:
    if name == "static-allow":
        return StaticPrompter(ALLOW)
    if name == "static-block":
        return StaticPrompter(BLOCK)

    use_osa = name == "osascript" or (
        name == "auto" and sys.platform == "darwin" and shutil.which("osascript"))
    if use_osa:
        from mcp_guard.prompters.osascript import OsaScriptPrompter
        return OsaScriptPrompter()

    try:
        from mcp_guard.prompters.tk import TkPrompter
        return TkPrompter()
    except ImportError:
        if name == "tk":
            raise

    print("mcp-guard: no GUI available, every request will be blocked", file=sys.stderr)
    return StaticPrompter(BLOCK)
