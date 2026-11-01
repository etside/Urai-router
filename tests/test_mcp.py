import json
import os
import subprocess
import sys
from pathlib import Path


def _spawn(keys_path: Path, home: Path):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.Popen(
        [sys.executable, "-m", "urai_router", "mcp", "--keys", str(keys_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _call(process, message, expect=True):
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    if not expect:
        return None
    line = process.stdout.readline()
    assert line, "no MCP response"
    return json.loads(line)


def test_mcp_handshake_and_tools(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    keys = tmp_path / "keys.txt"
    keys.write_text("# empty\n")
    process = _spawn(keys, home)
    try:
        init = _call(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        assert init["result"]["serverInfo"]["name"] == "urai-router"
        assert init["result"]["protocolVersion"] == "2025-06-18"

        tools = _call(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in tools["result"]["tools"]}
        assert {"urai_chat", "urai_models", "urai_keys", "urai_health", "urai_ensure_proxy", "urai_setup"} <= names

        result = _call(
            process,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "urai_keys", "arguments": {}}},
        )
        assert result["result"]["isError"] is False
        assert "no keys loaded" in result["result"]["content"][0]["text"]

        resource = _call(
            process,
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "urai://keys"}},
        )
        assert resource["result"]["contents"][0]["mimeType"] == "application/json"

        ping = _call(process, {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}})
        assert ping["result"] == {}

        missing = _call(process, {"jsonrpc": "2.0", "id": 6, "method": "does/not/exist", "params": {}})
        assert missing["error"]["code"] == -32601
    finally:
        process.stdin.close()
        process.wait(timeout=5)
