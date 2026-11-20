"""macOS LaunchAgent so the broker starts at login and restarts if it dies.

Optional - the first proxy starts the broker anyway.
"""

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from mcp_guard.config import log_path

LABEL = "ai.walled.mcp-guard"


def plist_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def render_plist(python: str) -> str:
    env = os.environ.get("MCP_GUARD_HOME")
    env_block = ""
    if env:
        env_block = ("  <key>EnvironmentVariables</key>\n"
                     f"  <dict><key>MCP_GUARD_HOME</key><string>{escape(env)}</string></dict>\n")
    log = escape(str(log_path()))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{escape(python)}</string><string>-m</string><string>mcp_guard</string><string>broker</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
{env_block}</dict>
</plist>
"""


def install_service() -> int:
    if sys.platform != "darwin":
        print("Only macOS is supported here. Elsewhere, add `python -m mcp_guard broker` "
              "to your startup programs.")
        return 1
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_plist(sys.executable), encoding="utf-8")

    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
    res = subprocess.run(["launchctl", "bootstrap", domain, str(path)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(f"launchctl bootstrap failed: {res.stderr.strip()}")
        return 1
    print(f"Installed and started {LABEL} ({path})")
    return 0


def uninstall_service() -> int:
    path = plist_path()
    if sys.platform == "darwin" and path.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)],
                       capture_output=True)
        path.unlink()
        print(f"Removed {LABEL}")
    else:
        print("No LaunchAgent installed")
    return 0
