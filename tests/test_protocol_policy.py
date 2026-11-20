from mcp_guard.policy import Policy
from mcp_guard.prompters.base import format_details, sanitize
from mcp_guard.protocol import (
    ApprovalRequest, build_approval_request, client_label, denial_response, is_request,
)


def call(name="delete_file", args=None, mid=7):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
            "params": {"name": name, "arguments": args or {"path": "/x"}}}


def test_build_request_and_denial_shape():
    req = build_approval_request(call(), "fs", "claude-ai 1.0", "stdio")
    assert (req.server, req.tool, req.arguments, req.rpc_id) == ("fs", "delete_file", {"path": "/x"}, 7)
    resp = denial_response(call(), "user")
    assert resp["id"] == 7 and resp["result"]["isError"] is True
    assert "Blocked by the user" in resp["result"]["content"][0]["text"]


def test_non_tool_denial_is_jsonrpc_error():
    msg = {"jsonrpc": "2.0", "id": "a", "method": "resources/read", "params": {"uri": "file:///etc/passwd"}}
    assert build_approval_request(msg, "s", "c", "http").tool == "file:///etc/passwd"
    assert denial_response(msg, "timeout")["error"]["code"] == -32001


def test_is_request_and_client_label():
    assert is_request(call())
    assert not is_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert client_label({"clientInfo": {"name": "claude-ai", "version": "0.1"}}) == "claude-ai 0.1"
    assert client_label(None) == "unknown"


def test_policy_rules_and_grants():
    p = Policy([{"server": "fs", "tool": "read_*", "action": "allow"},
                {"tool": "delete_*", "action": "block"}])
    mk = lambda s, t: ApprovalRequest(server=s, method="tools/call", tool=t)
    assert p.evaluate(mk("fs", "read_file")) == "allow"
    assert p.evaluate(mk("other", "read_file")) is None
    assert p.evaluate(mk("any", "delete_file")) == "block"
    p.grant("other", "read_file")
    assert p.evaluate(mk("other", "read_file")) == "allow"


def test_popup_text_strips_spoofing_characters():
    req = ApprovalRequest(server="s", method="tools/call", tool="t",
                          arguments={"x": "safe‮evil\rhidden\x1b[2J"})
    text = format_details(req)
    assert "‮" not in text and "\x1b" not in text and "\r" not in text
    assert sanitize("a\nb\tc") == "a\nb\tc"


def test_long_arguments_are_truncated_in_popup():
    req = ApprovalRequest(server="s", method="tools/call", tool="t", arguments={"blob": "x" * 5000})
    assert "more characters" in format_details(req)
