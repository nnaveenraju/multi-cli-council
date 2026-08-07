"""Regression tests for subprocess timeout, retry backoff, and parallel cap."""

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from council.config import load_config
from council.models import registry
from council.models.base import BaseAdapter, InvokeRequest, ModelResult


class _SleepAdapter(BaseAdapter):
    """Spawns a shell that outlives its direct child, like a real agentic CLI."""

    provider = "sleeper"

    def __init__(self, marker: str, seconds: int = 30) -> None:
        super().__init__(bin_path="/bin/sh")
        self.marker = marker
        self.seconds = seconds

    def build_command(self, req: InvokeRequest, prompt_file: Path) -> list[str]:
        return ["/bin/sh", "-c", f"sleep {self.seconds} # {self.marker}"]


async def test_timeout_returns_promptly(tmp_path: Path):
    """Previously reported a 2s timeout after blocking for the full 30s."""
    adapter = _SleepAdapter(marker="council_timeout_probe")
    started = time.monotonic()
    result = await adapter.invoke(
        InvokeRequest(prompt="x", timeout_seconds=2, cwd=tmp_path)
    )
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert "Timed out after 2s" in (result.error or "")
    # Generous bound: the bug produced ~30s here.
    assert elapsed < 10, f"timeout blocked for {elapsed:.1f}s"


async def test_timeout_kills_process_tree(tmp_path: Path):
    """proc.kill() alone leaves descendants running; the group must be killed."""
    marker = "council_orphan_probe_xyz"
    adapter = _SleepAdapter(marker=marker, seconds=47)
    await adapter.invoke(InvokeRequest(prompt="x", timeout_seconds=1, cwd=tmp_path))
    await asyncio.sleep(1.0)

    ps = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True)
    survivors = [
        line
        for line in ps.stdout.splitlines()
        if marker in line and "ps -eo" not in line
    ]
    assert not survivors, f"orphaned processes survived: {survivors}"


async def test_successful_invoke_is_not_delayed(tmp_path: Path):
    class Fast(BaseAdapter):
        provider = "fast"

        def build_command(self, req, prompt_file):
            return ["/bin/echo", "hello"]

    result = await Fast(bin_path="/bin/echo").invoke(
        InvokeRequest(prompt="x", timeout_seconds=30, cwd=tmp_path)
    )
    assert result.ok is True
    assert result.text == "hello"


async def test_missing_binary_reports_real_command(tmp_path: Path):
    class Missing(BaseAdapter):
        provider = "missing"

        def build_command(self, req, prompt_file):
            return ["/nonexistent/council-probe-bin", "--flag"]

    result = await Missing(bin_path="/nonexistent/council-probe-bin").invoke(
        InvokeRequest(prompt="x", timeout_seconds=5, cwd=tmp_path)
    )
    assert result.ok is False
    assert "Binary not found" in (result.error or "")
    # `dir()` instead of `locals()` used to drop the argv here.
    assert "--flag" in result.command


async def test_retry_applies_backoff(monkeypatch):
    config = load_config()
    config.invoke.retries = 1
    monkeypatch.setattr(registry, "_RETRY_BACKOFF_SECONDS", 0.2)

    attempts: list[float] = []

    class Failing(BaseAdapter):
        provider = "claude"

        def build_command(self, req, prompt_file):
            return ["/bin/false"]

        async def invoke(self, req):
            attempts.append(time.monotonic())
            return ModelResult(
                ok=False, text="", provider="claude", model=req.model, error="boom"
            )

    monkeypatch.setattr(registry, "get_adapter", lambda provider, config: Failing("x"))
    result = await registry.invoke_model(
        config, provider="claude", model="m", prompt="p"
    )

    assert result.ok is False
    assert len(attempts) == 2, "retries config must actually retry"
    assert attempts[1] - attempts[0] >= 0.2, "retry must back off, not hammer instantly"


async def test_max_parallel_is_enforced(monkeypatch):
    config = load_config()
    config.invoke.max_parallel = 3
    config.invoke.retries = 0

    state = {"live": 0, "peak": 0}

    class Tracking(BaseAdapter):
        provider = "claude"

        def build_command(self, req, prompt_file):
            return ["/bin/true"]

        async def invoke(self, req):
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            await asyncio.sleep(0.05)
            state["live"] -= 1
            return ModelResult(ok=True, text="x", provider="claude", model=req.model)

    monkeypatch.setattr(registry, "get_adapter", lambda provider, config: Tracking("x"))
    await asyncio.gather(
        *[
            registry.invoke_model(config, provider="claude", model="m", prompt="p")
            for _ in range(9)
        ]
    )
    assert state["peak"] <= 3, f"max_parallel not enforced (peak {state['peak']})"


@pytest.mark.parametrize("limit", [0, -1])
async def test_unlimited_parallel_when_disabled(monkeypatch, limit):
    """A non-positive cap means "no limit" rather than deadlocking."""
    config = load_config()
    config.invoke.max_parallel = limit
    config.invoke.retries = 0

    class Ok(BaseAdapter):
        provider = "claude"

        def build_command(self, req, prompt_file):
            return ["/bin/true"]

        async def invoke(self, req):
            return ModelResult(ok=True, text="x", provider="claude", model=req.model)

    monkeypatch.setattr(registry, "get_adapter", lambda provider, config: Ok("x"))
    results = await asyncio.gather(
        *[
            registry.invoke_model(config, provider="claude", model="m", prompt="p")
            for _ in range(4)
        ]
    )
    assert all(r.ok for r in results)
