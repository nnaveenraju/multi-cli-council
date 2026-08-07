"""Base types for CLI model invocation."""

from __future__ import annotations

import abc
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ToolsMode = Literal["off", "minimal", "web"]

# Soft cap for putting prompt text on argv (macOS ARG_MAX is larger; stay safe)
ARG_MAX_SOFT = 80_000


def _detect_cli_error(stdout: str, stderr: str, text: str, exit_code: int | None) -> str | None:
    """Catch CLI usage errors that sometimes still exit 0.

    stderr is always scanned. The model's own output (stdout/extracted text)
    is only scanned when the run already looks failed (non-zero exit or empty
    output) — a technical article may legitimately contain phrases like
    "unknown model" and must not be mistaken for a CLI failure.
    """
    blob = "\n".join([stderr or "", stdout or "", text or ""]).strip()
    if not blob:
        return "empty output"
    haystacks = [stderr or ""]
    if exit_code != 0 or not text.strip():
        haystacks.append("\n".join([stdout or "", text or ""]))
    blob = "\n".join(haystacks).strip()
    if not blob:
        return None
    low = blob.lower()
    patterns = (
        "error: invalid value",
        "error: cannot combine",
        "error: unexpected argument",
        "is not a model this version",
        "unrecognized arguments",
        "unknown model",
        "for more information, try '--help'",
    )
    for p in patterns:
        if p in low:
            # return first meaningful line
            for line in blob.splitlines():
                if line.strip().lower().startswith("error") or p in line.lower():
                    return line.strip()
            return p
    return None


@dataclass
class InvokeRequest:
    prompt: str
    system: str | None = None
    model: str | None = None
    tools: ToolsMode = "minimal"
    timeout_seconds: int = 900
    cwd: Path | None = None
    label: str = ""
    member_id: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)
    # Write full prompt to this file (debugging)
    prompt_path: Path | None = None
    # Adapters set this (in build_command) when the prompt should be piped to
    # stdin instead of placed on argv (e.g. oversized prompt with tools off,
    # where the file-indirection trick is impossible).
    prompt_via_stdin: bool = False


@dataclass
class ModelResult:
    ok: bool
    text: str
    provider: str
    model: str | None
    exit_code: int | None = None
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    command: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        m = self.model or "default"
        return f"{self.provider}:{m}"


class BaseAdapter(abc.ABC):
    provider: str

    def __init__(self, bin_path: str, extra_args: list[str] | None = None) -> None:
        self.bin_path = bin_path
        self.extra_args = list(extra_args or [])

    @abc.abstractmethod
    def build_command(self, req: InvokeRequest, prompt_file: Path) -> list[str]:
        """Build argv for non-interactive invoke. Prompt is also on disk."""

    def prepare_prompt_file(self, req: InvokeRequest, work_dir: Path) -> Path:
        work_dir.mkdir(parents=True, exist_ok=True)
        path = req.prompt_path or (work_dir / "prompt.txt")
        body = req.prompt
        if req.system:
            body = f"SYSTEM INSTRUCTIONS:\n{req.system}\n\n---\n\nUSER TASK:\n{req.prompt}"
        path.write_text(body, encoding="utf-8")
        return path

    async def invoke(self, req: InvokeRequest) -> ModelResult:
        import asyncio

        work = req.cwd or Path.cwd()
        work.mkdir(parents=True, exist_ok=True)
        prompt_file = self.prepare_prompt_file(req, work / "_invoke")
        cmd = self.build_command(req, prompt_file)

        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(work),
                stdin=(
                    asyncio.subprocess.PIPE if req.prompt_via_stdin else None
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(req),
                # Own process group so a timeout can kill the whole tree.
                # These CLIs spawn tool subprocesses that would otherwise
                # survive and hold the pipes open.
                start_new_session=True,
            )
            try:
                stdin_b = (
                    prompt_file.read_bytes() if req.prompt_via_stdin else None
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=stdin_b),
                    timeout=req.timeout_seconds,
                )
            except TimeoutError:
                await self._terminate_tree(proc)
                duration = time.monotonic() - started
                return ModelResult(
                    ok=False,
                    text="",
                    provider=self.provider,
                    model=req.model,
                    exit_code=None,
                    duration_seconds=duration,
                    error=f"Timed out after {req.timeout_seconds}s",
                    command=cmd,
                )

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            duration = time.monotonic() - started
            text = self.extract_text(stdout, stderr)
            cli_error = _detect_cli_error(stdout, stderr, text, proc.returncode)
            ok = proc.returncode == 0 and bool(text.strip()) and not cli_error
            return ModelResult(
                ok=ok,
                text=text.strip(),
                provider=self.provider,
                model=req.model,
                exit_code=proc.returncode,
                duration_seconds=duration,
                stdout=stdout,
                stderr=stderr,
                error=(
                    None
                    if ok
                    else (
                        cli_error
                        or stderr.strip()
                        or (text.strip() if not ok and proc.returncode != 0 else None)
                        or (
                            "empty output"
                            if proc.returncode == 0
                            else f"exit {proc.returncode}"
                        )
                    )
                ),
                command=cmd,
            )
        except FileNotFoundError:
            return ModelResult(
                ok=False,
                text="",
                provider=self.provider,
                model=req.model,
                error=f"Binary not found: {self.bin_path}",
                command=cmd if "cmd" in locals() else [self.bin_path],
            )
        except Exception as exc:  # noqa: BLE001
            return ModelResult(
                ok=False,
                text="",
                provider=self.provider,
                model=req.model,
                error=str(exc),
                duration_seconds=time.monotonic() - started,
            )

    @staticmethod
    async def _terminate_tree(proc: Any, grace_seconds: float = 5.0) -> None:
        """Kill the child's whole process group, then reap without hanging.

        Reaping needs its own timeout: the first `communicate()` was cancelled
        by `wait_for`, and any surviving descendant holding the pipes open
        would otherwise block this second call indefinitely.
        """
        import asyncio
        import os
        import signal

        for sig in (signal.SIGTERM, signal.SIGKILL):
            if proc.returncode is not None:
                break
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                # No process group (already reaped, or start_new_session
                # unsupported) — fall back to killing just the child.
                try:
                    proc.kill()
                except ProcessLookupError:
                    return
            try:
                await asyncio.wait_for(proc.communicate(), timeout=grace_seconds)
                return
            except (TimeoutError, ValueError):
                continue

    def extract_text(self, stdout: str, stderr: str) -> str:
        """Best-effort clean text from CLI stdout."""
        text = stdout.strip()
        if not text:
            return ""
        # If JSON blob with result field (claude --output-format json)
        if text.startswith("{") and '"result"' in text:
            try:
                import json

                data = json.loads(text)
                if isinstance(data, dict) and "result" in data:
                    return str(data["result"])
            except Exception:  # noqa: BLE001
                pass
        # Strip ANSI
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        return text

    def _env(self, req: InvokeRequest) -> dict[str, str]:
        import os

        env = dict(os.environ)
        env.update(req.extra_env)
        # Prefer non-interactive behavior
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        return env
