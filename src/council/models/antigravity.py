"""Google Antigravity CLI adapter (`agy -p`)."""

from __future__ import annotations

from pathlib import Path

from council.models.base import ARG_MAX_SOFT, BaseAdapter, InvokeRequest


class AntigravityAdapter(BaseAdapter):
    """Shell out to Antigravity's non-interactive print mode.

    CLI shape (verified against agy 1.1.x)::

        agy -p "<prompt>" --output-format text --model <id> \\
            --print-timeout <Ns> --dangerously-skip-permissions

    There is no --prompt-file and no per-tool allow/deny flags equivalent to
    Claude/Grok, so large prompts use on-disk file indirection and tool modes
    are best-effort (same honesty bar as Kimi).
    """

    provider = "antigravity"

    def build_command(self, req: InvokeRequest, prompt_file: Path) -> list[str]:
        prompt = prompt_file.read_text(encoding="utf-8")
        if len(prompt) > ARG_MAX_SOFT:
            path = prompt_file.resolve()
            if not path.is_file():
                raise FileNotFoundError(
                    f"Antigravity large-prompt file missing (cannot put "
                    f"{len(prompt)} chars on argv): {path}"
                )
            prompt = (
                "Open and follow the complete instructions in this file, then "
                f"print only the final Markdown output:\n{path}"
            )

        # -p / --print / --prompt: single-shot non-interactive run
        cmd = [self.bin_path, "-p", prompt, "--output-format", "text"]

        if req.model:
            cmd.extend(["--model", req.model])

        # Map our per-invoke budget onto agy's print-mode wait (Go duration).
        if req.timeout_seconds and req.timeout_seconds > 0:
            cmd.extend(["--print-timeout", f"{int(req.timeout_seconds)}s"])

        # Headless: auto-approve tool permission prompts (research needs web).
        if "--dangerously-skip-permissions" not in cmd:
            cmd.append("--dangerously-skip-permissions")

        # NOTE: agy has no web-search / tool allow-list flags. `req.tools` is
        # accepted for interface parity but cannot be enforced here.

        for a in self.extra_args:
            if a not in cmd:
                cmd.append(a)
        return cmd
