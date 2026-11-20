import json

from mcp_guard import installer


def test_install_uninstall_roundtrip(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    original = {
        "globalShortcut": "",
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@mcp/server-filesystem", "/tmp"],
                           "env": {"FOO": "1"}},
            "bare": {"command": "/usr/local/bin/srv"},
            "remote": {"type": "http", "url": "https://example.com/mcp"},
        },
    }
    path.write_text(json.dumps(original))

    changed, skipped, backup = installer.apply(path, True, python="/venv/bin/python")
    assert sorted(changed) == ["bare", "filesystem"]
    assert skipped and "remote" in skipped[0]
    assert backup and json.loads(backup.read_text()) == original
    wrapped = json.loads(path.read_text())["mcpServers"]["filesystem"]
    assert wrapped["command"] == "/venv/bin/python"
    assert wrapped["args"] == ["-m", "mcp_guard", "proxy", "--name", "filesystem", "--",
                               "npx", "-y", "@mcp/server-filesystem", "/tmp"]
    assert wrapped["env"] == {"FOO": "1"}

    # Idempotent.
    assert installer.apply(path, True, python="/venv/bin/python")[0] == []

    installer.apply(path, False)
    assert json.loads(path.read_text()) == original


def test_status_and_only_filter(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}))
    installer.apply(path, True, python="py", only=["a"])
    assert dict(installer.status(path)) == {"a": "guarded", "b": "NOT guarded (run `mcp-guard install`)"}
