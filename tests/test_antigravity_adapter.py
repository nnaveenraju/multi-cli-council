"""Regression tests for Antigravity (`agy -p`) argv construction."""

from pathlib import Path

import pytest

from council.config import McpServerConfig, load_config
from council.models.antigravity import AntigravityAdapter
from council.models.base import ARG_MAX_SOFT, InvokeRequest
from council.models.registry import get_adapter


def _cmd(adapter: AntigravityAdapter, tmp_path: Path, **kwargs) -> list[str]:
    req = InvokeRequest(cwd=tmp_path, **kwargs)
    prompt_file = adapter.prepare_prompt_file(req, tmp_path / "_invoke")
    return adapter.build_command(req, prompt_file)


@pytest.fixture
def adapter() -> AntigravityAdapter:
    return AntigravityAdapter(bin_path="agy")


def test_print_mode_prompt_and_format(adapter, tmp_path: Path):
    cmd = _cmd(
        adapter,
        tmp_path,
        prompt="Reply OK",
        model="gemini-3.6-flash-low",
        tools="web",
    )
    assert cmd[0] == "agy"
    assert cmd[1] == "-p"
    assert "Reply OK" in cmd[2]
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "text"
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gemini-3.6-flash-low"


def test_web_mode_auto_approves_permissions(adapter, tmp_path: Path):
    """Research browses unattended, so it is the one stage that gets the grant."""
    cmd = _cmd(adapter, tmp_path, prompt="hi", tools="web")
    assert "--dangerously-skip-permissions" in cmd
    assert "--sandbox" not in cmd


@pytest.mark.parametrize("mode", ["minimal", "off"])
def test_restricted_modes_withhold_permission_grant(adapter, tmp_path: Path, mode):
    """Critique/peer-review must not carry a blanket tool-approval flag."""
    cmd = _cmd(adapter, tmp_path, prompt="hi", tools=mode)
    assert "--dangerously-skip-permissions" not in cmd
    assert "--sandbox" in cmd


def test_print_timeout_maps_from_request(adapter, tmp_path: Path):
    cmd = _cmd(
        adapter,
        tmp_path,
        prompt="hi",
        model="gemini-3.1-pro-high",
        timeout_seconds=1800,
    )
    assert "--print-timeout" in cmd
    assert cmd[cmd.index("--print-timeout") + 1] == "1800s"


def test_large_prompt_uses_file_indirection(adapter, tmp_path: Path):
    big = "x" * (ARG_MAX_SOFT + 10)
    req = InvokeRequest(cwd=tmp_path, prompt=big, tools="web")
    prompt_file = adapter.prepare_prompt_file(req, tmp_path / "_invoke")
    cmd = adapter.build_command(req, prompt_file)
    p_arg = cmd[cmd.index("-p") + 1]
    assert big not in p_arg
    assert str(prompt_file.resolve()) in p_arg


def test_registry_resolves_antigravity():
    config = load_config()
    adapter = get_adapter("antigravity", config)
    assert isinstance(adapter, AntigravityAdapter)
    assert adapter.bin_path == "agy"


def test_mcp_servers_rejected_for_antigravity():
    config = load_config()
    config.providers["antigravity"].mcp_servers = {
        "x": McpServerConfig(command="npx")
    }
    with pytest.raises(ValueError, match="does not support mcp_servers"):
        get_adapter("antigravity", config)


def test_config_wires_antigravity_member():
    config = load_config()
    assert "antigravity" in config.providers
    assert "researcher_antigravity" in config.members
    assert "researcher_antigravity" in config.roles["research"].participants
    assert "researcher_antigravity" in config.roles["critique"].participants
    spec = config.member_invoke_spec("researcher_antigravity", "research")
    assert spec["provider"] == "antigravity"
    assert spec["tools"] == "web"
    assert spec["model"] == "gemini-3.6-flash-high"
