"""Wrap/unwrap the stdio servers in claude_desktop_config.json.

The original command is kept after "--" in the new args, so uninstall can put
it back exactly and install is idempotent. url/remote entries are skipped.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MARKER = ["-m", "mcp_guard", "proxy"]


def default_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def is_wrapped(entry: Dict) -> bool:
    return list(entry.get("args", []))[:3] == MARKER


def wrap(name: str, entry: Dict, python: str) -> Dict:
    if is_wrapped(entry) or "command" not in entry:
        return entry
    new = dict(entry)
    new["command"] = python
    new["args"] = MARKER + ["--name", name, "--", entry["command"], *entry.get("args", [])]
    return new


def unwrap(entry: Dict) -> Dict:
    if not is_wrapped(entry):
        return entry
    args = list(entry["args"])
    original = args[args.index("--") + 1:]
    new = dict(entry)
    new["command"] = original[0]
    if original[1:]:
        new["args"] = original[1:]
    else:
        del new["args"]
    return new


def _load(path: Path) -> Dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: Dict) -> Optional[Path]:
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.mcp-guard-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def apply(path: Path, install: bool, python: Optional[str] = None,
          only: Optional[List[str]] = None) -> Tuple[List[str], List[str], Optional[Path]]:
    python = python or sys.executable
    data = _load(path)
    servers = data.get("mcpServers", {})
    changed, skipped = [], []

    for name, entry in servers.items():
        if only and name not in only:
            continue
        if "command" not in entry and not is_wrapped(entry):
            skipped.append(f"{name} (remote/url server, use `mcp-guard gateway`)")
            continue
        new = wrap(name, entry, python) if install else unwrap(entry)
        if new != entry:
            servers[name] = new
            changed.append(name)

    backup = _save(path, data) if changed else None
    return changed, skipped, backup


def status(path: Path) -> List[Tuple[str, str]]:
    out = []
    for name, entry in _load(path).get("mcpServers", {}).items():
        if is_wrapped(entry):
            out.append((name, "guarded"))
        elif "command" in entry:
            out.append((name, "NOT guarded (run `mcp-guard install`)"))
        else:
            out.append((name, "remote: guard with `mcp-guard gateway`"))
    return out
