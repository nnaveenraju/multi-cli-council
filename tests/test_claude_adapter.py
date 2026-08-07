"""Regression tests for Claude argv construction: `--` separator and MCP scope."""

import json
from pathlib import Path

import pytest

from council.config import McpServerConfig, load_config
from council.models.base import InvokeRequest
from council.models.claude import ClaudeAdapter
from council.models.registry import get_adapter


def _cmd(adapter: ClaudeAdapter, tmp_path: Path, **kwargs) -> list[str]:
    req = InvokeRequest(cwd=tmp_path, **kwargs)
    prompt_file = adapter.prepare_prompt_file(req, tmp_path / "_invoke")
    return adapter.build_command(req, prompt_file)


@pytest.fixture
def adapter() -> ClaudeAdapter:
    return ClaudeAdapter(bin_path="claude")


@pytest.mark.parametrize("tools", ["off", "minimal", "web"])
def test_prompt_is_separated_by_double_dash(adapter, tmp_path: Path, tools: str):
    """A variadic flag would otherwise swallow the prompt as another value."""
    cmd = _cmd(adapter, tmp_path, prompt="Reply OK", system="Be terse.", tools=tools)
    assert cmd[-2] == "--"
    assert "Reply OK" in cmd[-1]


@pytest.mark.parametrize("tools", ["off", "minimal", "web"])
def test_long_system_prompt_still_separated(adapter, tmp_path: Path, tools: str):
    """The bug's live trigger: >=4000 chars skips --append-system-prompt,
    leaving a variadic tools flag adjacent to the prompt."""
    cmd = _cmd(adapter, tmp_path, prompt="Reply OK", system="x" * 5000, tools=tools)
    assert "--append-system-prompt" not in cmd
    assert cmd[-2] == "--"


def test_double_dash_appears_once(adapter, tmp_path: Path):
    cmd = _cmd(adapter, tmp_path, prompt="hi", system="s", tools="web")
    assert cmd.count("--") == 1


def test_strict_mcp_config_always_set(adapter, tmp_path: Path):
    """Without this, the host's global MCP servers leak into every stage."""
    for tools in ("off", "minimal", "web"):
        cmd = _cmd(adapter, tmp_path, prompt="hi", system="s", tools=tools)
        assert "--strict-mcp-config" in cmd


def test_no_mcp_config_flag_when_no_servers(adapter, tmp_path: Path):
    cmd = _cmd(adapter, tmp_path, prompt="hi", system="s", tools="web")
    assert "--mcp-config" not in cmd


def test_tool_modes_scope_servers_to_stages(tmp_path: Path):
    adapter = ClaudeAdapter(
        bin_path="claude",
        mcp_servers={
            "docs": {"command": "npx", "args": ["-y", "x"], "tool_modes": ["web"]},
            "always": {"command": "npx", "args": ["-y", "y"], "tool_modes": []},
        },
    )

    web = _cmd(adapter, tmp_path, prompt="hi", system="s", tools="web")
    servers = json.loads(web[web.index("--mcp-config") + 1])["mcpServers"]
    assert set(servers) == {"docs", "always"}
    # tool_modes is our own metadata; it must not reach the CLI payload.
    assert "tool_modes" not in json.dumps(servers)

    minimal = _cmd(adapter, tmp_path, prompt="hi", system="s", tools="minimal")
    servers = json.loads(minimal[minimal.index("--mcp-config") + 1])["mcpServers"]
    assert set(servers) == {"always"}, "web-only server must not load in minimal"


def test_mcp_tools_are_allowlisted(tmp_path: Path):
    """Loading a server is not enough — its tools must be permitted too."""
    adapter = ClaudeAdapter(
        bin_path="claude",
        mcp_servers={"docs": {"command": "npx", "args": ["-y", "x"]}},
    )
    for tools in ("minimal", "web"):
        cmd = _cmd(adapter, tmp_path, prompt="hi", system="s", tools=tools)
        allowed = cmd[cmd.index("--allowedTools") + 1]
        assert "mcp__docs" in allowed


def test_disabled_server_is_dropped(tmp_path: Path):
    adapter = ClaudeAdapter(
        bin_path="claude",
        mcp_servers={"off_one": {"command": "npx", "enabled": False}},
    )
    cmd = _cmd(adapter, tmp_path, prompt="hi", system="s", tools="web")
    assert "--mcp-config" not in cmd


def test_web_mode_grants_network_tools(adapter, tmp_path: Path):
    cmd = _cmd(adapter, tmp_path, prompt="hi", system="s", tools="web")
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "WebSearch" in allowed and "WebFetch" in allowed


@pytest.mark.parametrize("tools", ["off", "minimal"])
def test_non_web_modes_grant_no_network_tools(adapter, tmp_path: Path, tools: str):
    cmd = _cmd(adapter, tmp_path, prompt="hi", system="s", tools=tools)
    granted = cmd[cmd.index("--tools") + 1]
    assert "WebSearch" not in granted and "WebFetch" not in granted
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"


def test_http_server_serializes_as_url(tmp_path: Path):
    spec = McpServerConfig(url="https://example.com/mcp")
    entry = spec.to_mcp_entry()
    assert entry == {"type": "http", "url": "https://example.com/mcp"}


def test_stdio_server_serializes_command_and_env():
    spec = McpServerConfig(command="npx", args=["-y", "pkg"], env={"K": "v"})
    assert spec.to_mcp_entry() == {
        "command": "npx",
        "args": ["-y", "pkg"],
        "env": {"K": "v"},
    }


def test_mcp_servers_rejected_for_non_claude_providers():
    """grok/kimi CLIs have no --mcp-config; fail loudly instead of ignoring."""
    config = load_config()
    config.providers["grok"].mcp_servers = {"x": McpServerConfig(command="npx")}
    with pytest.raises(ValueError, match="does not support mcp_servers"):
        get_adapter("grok", config)


def test_claude_adapter_receives_configured_servers():
    config = load_config()
    config.providers["claude"].mcp_servers = {
        "docs": McpServerConfig(command="npx", args=["-y", "x"], tool_modes=["web"])
    }
    adapter = get_adapter("claude", config)
    assert isinstance(adapter, ClaudeAdapter)
    assert adapter.mcp_servers["docs"]["command"] == "npx"
    assert adapter.mcp_servers["docs"]["tool_modes"] == ["web"]
