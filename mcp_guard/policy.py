import fnmatch
import threading
from typing import Dict, List, Optional, Set, Tuple

from mcp_guard.protocol import ApprovalRequest


class Policy:
    """Config rules (first match wins) + "Always Allow" grants from the popup.

    Grants are kept in memory only, so they go away when the broker restarts.
    """

    def __init__(self, rules: Optional[List[Dict[str, str]]] = None):
        self.rules = list(rules or [])
        self._grants: Set[Tuple[str, str]] = set()
        self._lock = threading.Lock()

    def evaluate(self, req: ApprovalRequest) -> Optional[str]:
        with self._lock:
            if (req.server, req.tool) in self._grants:
                return "allow"
        for rule in self.rules:
            if (fnmatch.fnmatchcase(req.server, rule.get("server", "*"))
                    and fnmatch.fnmatchcase(req.tool, rule.get("tool", "*"))
                    and fnmatch.fnmatchcase(req.method, rule.get("method", "*"))):
                if rule.get("action") in ("allow", "block"):
                    return rule["action"]
        return None

    def grant(self, server: str, tool: str) -> None:
        with self._lock:
            self._grants.add((server, tool))
