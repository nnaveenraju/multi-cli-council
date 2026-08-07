"""Resolve adapters and invoke with retries."""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from council.models.base import BaseAdapter, InvokeRequest, ModelResult, ToolsMode
from council.models.claude import ClaudeAdapter
from council.models.grok import GrokAdapter
from council.models.kimi import KimiAdapter

if TYPE_CHECKING:
    from council.config import CouncilConfig

# Seconds before the first retry; doubled per subsequent attempt.
_RETRY_BACKOFF_SECONDS = 5.0

# Failure prefixes that retrying cannot fix — don't burn a backoff on them.
_NON_RETRYABLE_PREFIXES = ("Binary not found", "Timed out")

# Semaphores per event loop, keyed weakly so loops from completed
# asyncio.run() calls don't leak entries (and ids can't be recycled onto a
# semaphore bound to a dead loop).
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Semaphore]] = (
    weakref.WeakKeyDictionary()
)


@contextlib.asynccontextmanager
async def _parallel_slot(config: CouncilConfig) -> AsyncIterator[None]:
    """Cap concurrent model subprocesses at config.invoke.max_parallel."""
    limit = config.invoke.max_parallel
    if limit <= 0:  # non-positive means "no cap"
        yield
        return
    loop = asyncio.get_running_loop()
    by_limit = _slots.get(loop)
    if by_limit is None:
        by_limit = _slots[loop] = {}
    sem = by_limit.get(limit)
    if sem is None:
        sem = by_limit[limit] = asyncio.Semaphore(limit)
    async with sem:
        yield


def get_adapter(provider: str, config: CouncilConfig) -> BaseAdapter:
    if provider not in config.providers:
        raise KeyError(f"Unknown provider: {provider}")
    prov = config.providers[provider]
    mapping: dict[str, type[BaseAdapter]] = {
        "claude": ClaudeAdapter,
        "grok": GrokAdapter,
        "kimi": KimiAdapter,
    }
    cls = mapping.get(provider)
    if not cls:
        raise KeyError(f"No adapter for provider: {provider}")
    if cls is ClaudeAdapter:
        # Only the Claude CLI exposes --mcp-config / --strict-mcp-config.
        return ClaudeAdapter(
            bin_path=prov.bin,
            extra_args=prov.extra_args,
            mcp_servers={
                name: spec.to_mcp_entry() | {"tool_modes": list(spec.tool_modes)}
                for name, spec in prov.mcp_servers.items()
                if spec.enabled
            },
        )
    if prov.mcp_servers:
        raise ValueError(
            f"Provider '{provider}' does not support mcp_servers "
            "(only 'claude' does); remove the key from config."
        )
    return cls(bin_path=prov.bin, extra_args=prov.extra_args)


async def invoke_model(
    config: CouncilConfig,
    *,
    provider: str,
    model: str | None,
    prompt: str,
    system: str | None = None,
    tools: ToolsMode = "minimal",
    timeout_seconds: int = 900,
    cwd: Path | None = None,
    label: str = "",
    member_id: str = "",
) -> ModelResult:
    if tools not in ("off", "minimal", "web"):
        # An unknown mode must not silently fall through to an adapter
        # default (for Claude that default would broaden tool access).
        raise ValueError(f"Unknown tools mode: {tools!r}")
    adapter = get_adapter(provider, config)
    req = InvokeRequest(
        prompt=prompt,
        system=system,
        model=model or config.resolve_model(provider, None),
        tools=tools,
        timeout_seconds=timeout_seconds,
        cwd=cwd,
        label=label,
        member_id=member_id,
    )

    retries = max(0, config.invoke.retries)
    last: ModelResult | None = None
    async with _parallel_slot(config):
        for attempt in range(retries + 1):
            last = await adapter.invoke(req)
            if last.ok:
                return last
            if attempt < retries:
                if (last.error or "").startswith(_NON_RETRYABLE_PREFIXES):
                    # Deterministic failure (missing binary) or a full timeout
                    # already spent the budget — retrying can't help.
                    break
                # Back off before retrying: an immediate re-invoke usually
                # fails again for the same reason (rate limit, transient auth).
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (2**attempt))
    assert last is not None
    return last
