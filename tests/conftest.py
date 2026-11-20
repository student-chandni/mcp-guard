import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_guard import config as cfgmod  # noqa: E402
from mcp_guard.broker import BrokerServer  # noqa: E402
from mcp_guard.prompters import ALLOW, Prompter  # noqa: E402

DEMO_SERVER = str(ROOT / "examples" / "demo_server.py")


class ScriptedPrompter(Prompter):
    """Answers by tool name. ``gates`` lets a test hold a popup open until it
    sets the matching Event, to simulate a slow human."""

    name = "scripted"

    def __init__(self, answers=None, default=ALLOW, gates=None):
        self.answers = answers or {}
        self.default = default
        self.gates = gates or {}
        self.asked = []
        self.lock = threading.Lock()

    def ask(self, req, timeout, cancelled):
        with self.lock:
            self.asked.append(req)
        gate = self.gates.get(req.tool)
        if gate is not None:
            deadline = time.monotonic() + timeout
            while not gate.is_set():
                if cancelled.is_set():
                    return "cancelled"
                if time.monotonic() > deadline:
                    return "timeout"
                time.sleep(0.02)
        return self.answers.get(req.tool, self.default)


@pytest.fixture
def guard_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_GUARD_HOME", str(tmp_path))
    return tmp_path


def write_config(home: Path, **overrides):
    data = {"port": 0, "autostart_broker": False, "timeout_seconds": 10}
    data.update(overrides)
    (home / "config.json").write_text(json.dumps(data))


@pytest.fixture
def start_broker(guard_home):
    servers = []

    def _start(prompter, **cfg_overrides):
        write_config(guard_home, **cfg_overrides)
        server = BrokerServer(cfgmod.Config.load(), prompter)
        server.start()
        servers.append(server)
        return server

    yield _start
    for s in servers:
        s.close()


@pytest.fixture
def child_env(guard_home):
    env = dict(os.environ)
    env["MCP_GUARD_HOME"] = str(guard_home)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env
