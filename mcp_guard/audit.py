import hashlib
import json
import os
import threading
import time
from pathlib import Path

from mcp_guard.protocol import ApprovalRequest

MAX_BYTES = 5 * 1024 * 1024
PREVIEW_CHARS = 500


class AuditLog:
    def __init__(self, path: Path, log_arguments: bool = True):
        self.path = path
        self.log_arguments = log_arguments
        self._lock = threading.Lock()

    def record(self, req: ApprovalRequest, decision: str, reason: str, started: float) -> None:
        args_json = json.dumps(req.arguments, sort_keys=True, default=str)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": req.request_id,
            "client": req.client,
            "transport": req.transport,
            "server": req.server,
            "method": req.method,
            "tool": req.tool,
            "decision": decision,
            "reason": reason,
            "wait_ms": int((time.monotonic() - started) * 1000),
            "args_sha256": hashlib.sha256(args_json.encode()).hexdigest(),
        }
        if self.log_arguments:
            entry["args_preview"] = args_json[:PREVIEW_CHARS]
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    os.replace(self.path, self.path.with_suffix(".jsonl.1"))
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass  # don't let logging break approvals
