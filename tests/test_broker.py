import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from mcp_guard import config as cfgmod
from mcp_guard.client import BrokerClient
from mcp_guard.protocol import ApprovalRequest

from conftest import ScriptedPrompter


def req(tool, server="demo"):
    return ApprovalRequest(server=server, method="tools/call", tool=tool, arguments={"a": 1})


def test_allow_block_and_audit(start_broker, guard_home):
    start_broker(ScriptedPrompter({"delete_file": "block"}))
    client = BrokerClient()
    assert client.decide(req("get_weather")) == ("allow", "user")
    assert client.decide(req("delete_file")) == ("block", "user")
    entries = [json.loads(l) for l in (guard_home / "audit.jsonl").read_text().splitlines()]
    assert [e["decision"] for e in entries] == ["allow", "block"]
    assert entries[0]["args_sha256"] and entries[0]["tool"] == "get_weather"


def test_always_allow_skips_future_prompts(start_broker):
    prompter = ScriptedPrompter({"write_note": "allow_always"})
    start_broker(prompter)
    client = BrokerClient()
    assert client.decide(req("write_note")) == ("allow", "user-always")
    assert client.decide(req("write_note")) == ("allow", "policy-rule")
    assert len(prompter.asked) == 1
    # The grant is per (server, tool), not global.
    client.decide(req("write_note", server="other"))
    assert len(prompter.asked) == 2


def test_policy_rule_decides_without_prompt(start_broker):
    prompter = ScriptedPrompter()
    start_broker(prompter, rules=[{"tool": "delete_*", "action": "block"}])
    assert BrokerClient().decide(req("delete_file")) == ("block", "policy-rule")
    assert prompter.asked == []


def test_timeout_blocks(start_broker):
    start_broker(ScriptedPrompter(gates={"slow": threading.Event()}), timeout_seconds=0.5)
    assert BrokerClient().decide(req("slow")) == ("block", "timeout")


def test_concurrent_requests_are_independent(start_broker):
    gates = {"a": threading.Event(), "b": threading.Event(), "c": threading.Event()}
    start_broker(ScriptedPrompter({"b": "block"}, gates=gates))
    client = BrokerClient()
    results, done_order = {}, []

    def run(tool):
        results[tool] = client.decide(req(tool))
        done_order.append(tool)

    threads = [threading.Thread(target=run, args=(t,)) for t in "abc"]
    for t in threads:
        t.start()
    time.sleep(0.3)
    assert len(json.loads(urllib.request.urlopen(_authed("/pending")).read())["pending"]) == 3
    # Answer in reverse order; each call must resolve as soon as its own popup does.
    for tool in "cba":
        gates[tool].set()
        time.sleep(0.15)
    for t in threads:
        t.join(5)
    assert done_order == ["c", "b", "a"]
    assert results == {"a": ("allow", "user"), "b": ("block", "user"), "c": ("allow", "user")}


def test_cancel_closes_popup(start_broker):
    prompter = ScriptedPrompter(gates={"slow": threading.Event()})
    start_broker(prompter)
    client = BrokerClient()
    r = req("slow")
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("d", client.decide(r)))
    t.start()
    time.sleep(0.2)
    client.cancel(r.request_id)
    t.join(5)
    assert out["d"] == ("block", "cancelled")


def test_max_pending_limits_popup_flood(start_broker):
    gate = threading.Event()
    start_broker(ScriptedPrompter(gates={"slow": gate}), max_pending=1)
    client = BrokerClient()
    t = threading.Thread(target=client.decide, args=(req("slow"),))
    t.start()
    time.sleep(0.2)
    assert client.decide(req("slow")) == ("block", "too-many-pending")
    gate.set()
    t.join(5)


def _authed(path, token=None, headers=None):
    state = cfgmod.read_state()
    h = {"Authorization": f"Bearer {token or state['token']}"}
    h.update(headers or {})
    return urllib.request.Request(f"http://127.0.0.1:{state['port']}{path}", headers=h)


@pytest.mark.parametrize("token,headers", [
    ("wrong-token", None),
    (None, {"Origin": "https://evil.example"}),
    (None, {"Host": "attacker.example:80"}),
])
def test_api_rejects_unauthorized_callers(start_broker, token, headers):
    start_broker(ScriptedPrompter())
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(_authed("/pending", token, headers))
    assert exc.value.code == 401


def test_state_file_is_private(start_broker, guard_home):
    start_broker(ScriptedPrompter())
    assert (guard_home / "broker.json").stat().st_mode & 0o077 == 0


def test_fail_closed_and_fail_open_without_broker(guard_home):
    cfg = cfgmod.Config(port=0, autostart_broker=False)
    assert BrokerClient(cfg).decide(req("x")) == ("block", "broker-unavailable")
    cfg.fail_mode = "allow"
    assert BrokerClient(cfg).decide(req("x")) == ("allow", "broker-unavailable")
