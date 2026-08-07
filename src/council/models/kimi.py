"""Kimi Code CLI adapter."""

from __future__ import annotations

import re
from pathlib import Path

from council.models.base import ARG_MAX_SOFT, BaseAdapter, InvokeRequest


class KimiAdapter(BaseAdapter):
    provider = "kimi"

    def build_command(self, req: InvokeRequest, prompt_file: Path) -> list[str]:
        prompt = prompt_file.read_text(encoding="utf-8")
        # Kimi -p takes the prompt string; for large bodies, point at the file
        if len(prompt) > ARG_MAX_SOFT:
            prompt = (
                "Read and follow the full prompt in this file, then print only "
                f"the final Markdown output:\n{prompt_file.resolve()}"
            )

        # IMPORTANT: kimi forbids combining -p/--prompt with --auto or --yolo.
        # Prompt mode is non-interactive; do not append those flags.
        cmd = [self.bin_path, "-p", prompt, "--output-format", "text"]

        if req.model:
            cmd.extend(["-m", req.model])

        for a in self.extra_args:
            if a in {"--auto", "-y", "--yolo"}:
                continue
            if a not in cmd:
                cmd.append(a)
        return cmd

    def extract_text(self, stdout: str, stderr: str) -> str:
        text = super().extract_text(stdout, stderr)
        # Strip kimi CLI chrome
        lines = []
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("kimi version"):
                continue
            if s.startswith("To resume this session:"):
                continue
            if s.startswith("• "):
                s = s[2:]
            lines.append(s if line.startswith("• ") else line)
        cleaned = "\n".join(lines).strip()
        # Drop leading bullet meta paragraphs that aren't the answer body
        cleaned = re.sub(
            r"^(The user asked.*?\n\n)+",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )
        return cleaned.strip()
