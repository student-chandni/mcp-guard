import json
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_PORT = 47823


def guard_home() -> Path:
    # MCP_GUARD_HOME is mostly for tests
    home = Path(os.environ.get("MCP_GUARD_HOME") or Path.home() / ".mcp-guard")
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return home


def state_path() -> Path:
    return guard_home() / "broker.json"


def config_path() -> Path:
    return guard_home() / "config.json"


def audit_path() -> Path:
    return guard_home() / "audit.jsonl"


def log_path() -> Path:
    return guard_home() / "broker.log"


def write_private(path: Path, text: str) -> None:
    # broker.json holds the token, so 0600 + atomic replace
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


@dataclass
class Config:
    port: int = DEFAULT_PORT
    timeout_seconds: float = 120.0
    fail_mode: str = "block"          # used when the broker can't be reached
    prompter: str = "auto"
    autostart_broker: bool = True
    intercept_methods: List[str] = field(default_factory=lambda: ["tools/call"])
    rules: List[Dict[str, str]] = field(default_factory=list)
    log_arguments: bool = True
    max_pending: int = 32

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                print(f"mcp-guard: ignoring bad config {path}: {e}", file=sys.stderr)
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        if cfg.fail_mode not in ("block", "allow"):
            cfg.fail_mode = "block"
        return cfg


def read_state() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
