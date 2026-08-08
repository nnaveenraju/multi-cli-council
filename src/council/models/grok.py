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
        # `off` is the same isolation, used by peer review.
        #
        # Grok's built-in tool names are NOT Claude's — the shell is
        # `run_terminal_command`, so a deny-list of "Bash,Edit,Write,Shell"
        # matched nothing and the model simply curled URLs through the
        # terminal (verified empirically). Use the allow-list instead: only
        # read_file / list_dir / grep survive. --disable-web-search stays as
        # belt-and-suspenders for the search/fetch tools.
        #
        # Residual gap: grok always grants the MCP meta-tools (search_tool,
        # use_tool) and `grok -p` has no MCP-disable flag, so MCP servers in
        # the user's grok config that expose network tools remain reachable.
        if req.tools in ("off", "minimal"):
            cmd.append("--disable-web-search")
            cmd.extend(["--tools", "read_file,list_dir,grep"])

        if "--always-approve" not in cmd:
            cmd.append("--always-approve")

        for a in self.extra_args:
            if a not in cmd:
                cmd.append(a)
        return cmd
