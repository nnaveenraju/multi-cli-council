"""Grok Build CLI adapter."""

from __future__ import annotations

from pathlib import Path

from council.models.base import ARG_MAX_SOFT, BaseAdapter, InvokeRequest


class GrokAdapter(BaseAdapter):
    provider = "grok"

    def build_command(self, req: InvokeRequest, prompt_file: Path) -> list[str]:
        prompt = prompt_file.read_text(encoding="utf-8")
        cmd = [self.bin_path]

        # Prefer file-based prompt for reliability / length
        if len(prompt) > ARG_MAX_SOFT:
            cmd.extend(["--prompt-file", str(prompt_file.resolve())])
            # -p still required by some versions for headless exit; short stub
            cmd.extend(["-p", f"Execute the prompt file: {prompt_file.resolve()}"])
        else:
            cmd.extend(["-p", prompt])

        # Grok accepts: plain | json | streaming-json | streaming-messages-json
        cmd.extend(["--output-format", "plain"])

        if req.model:
            cmd.extend(["-m", req.model])

        if req.system and len(req.system) < 4000:
            cmd.extend(["--system-prompt-override", req.system])

        # `minimal` is used by critique seats: offline + non-mutating.
        # `off` is the same isolation, used by peer review. Both must deny
        # shell/write tools — web-disable alone leaves Bash/Write available.
        if req.tools in ("off", "minimal"):
            cmd.append("--disable-web-search")
            cmd.extend(["--disallowed-tools", "Bash,Edit,Write,Shell"])

        if "--always-approve" not in cmd:
            cmd.append("--always-approve")

        for a in self.extra_args:
            if a not in cmd:
                cmd.append(a)
        return cmd
