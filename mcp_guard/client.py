import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

from mcp_guard import config as cfgmod
from mcp_guard.protocol import ApprovalRequest

_spawn_lock = threading.Lock()


class BrokerUnavailable(Exception):
    pass


class BrokerClient:
    def __init__(self, cfg: Optional[cfgmod.Config] = None):
        self.cfg = cfg or cfgmod.Config.load()

    def _call(self, method: str, path: str, payload=None, timeout: float = 5.0):
        # re-read every time - the broker may have restarted with a new token
        state = cfgmod.read_state()
        if not state:
            raise BrokerUnavailable("broker is not running")
        req = urllib.request.Request(
            f"http://127.0.0.1:{state['port']}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {state['token']}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise BrokerUnavailable(f"broker returned HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise BrokerUnavailable(str(e)) from e

    def healthy(self) -> bool:
        try:
            return bool(self._call("GET", "/health", timeout=1.0).get("ok"))
        except BrokerUnavailable:
            return False

    def ensure_broker(self) -> bool:
        if self.healthy():
            return True
        if not self.cfg.autostart_broker:
            return False
        with _spawn_lock:
            if self.healthy():
                return True
            spawn_background_broker()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if self.healthy():
                    return True
                time.sleep(0.2)
        return False

    def decide(self, req: ApprovalRequest) -> Tuple[str, str]:
        """Returns (decision, reason). Doesn't raise - falls back to fail_mode."""
        # long poll, so the timeout has to cover the user thinking about it
        wait = self.cfg.timeout_seconds + 30
        for attempt in range(2):
            try:
                resp = self._call("POST", "/decide", req.to_dict(), timeout=wait)
                if resp.get("decision") in ("allow", "block"):
                    return resp["decision"], resp.get("reason", "")
                raise BrokerUnavailable(f"unexpected broker reply: {resp}")
            except BrokerUnavailable as e:
                if attempt == 0 and self.ensure_broker():
                    continue
                print(f"mcp-guard: broker unavailable ({e}), fail_mode={self.cfg.fail_mode}",
                      file=sys.stderr)
                break
        return self.cfg.fail_mode, "broker-unavailable"

    def cancel(self, request_id: str) -> None:
        try:
            self._call("POST", f"/cancel/{request_id}", {}, timeout=2.0)
        except BrokerUnavailable:
            pass


def spawn_background_broker() -> None:
    # If two proxies race here the second broker fails to bind and exits.
    log = open(cfgmod.log_path(), "ab")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "mcp_guard", "broker"],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **kwargs)
    log.close()
