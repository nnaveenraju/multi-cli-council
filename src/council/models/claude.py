"""Claude Code CLI adapter."""

from __future__ import annotations

import json
from pathlib import Path

from council.models.base import ARG_MAX_SOFT, BaseAdapter, InvokeRequest

# Built-in tools granted per mode. `off` passes an empty string, which the CLI
# documents as "disable all tools".
_TOOLS_BY_MODE = {
    "off": "",
    "minimal": "Read,Glob,Grep",
    "web": "WebSearch,WebFetch,Read,Glob,Grep",
}


class ClaudeAdapter(BaseAdapter):
    provider = "claude"

    def __init__(
        self,
        bin_path: str,
        extra_args: list[str] | None = None,
        mcp_servers: dict[str, dict] | None = None,
    ) -> None:
        super().__init__(bin_path, extra_args)
        self.mcp_servers = dict(mcp_servers or {})

    def build_command(self, req: InvokeRequest, prompt_file: Path) -> list[str]:
        prompt = prompt_file.read_text(encoding="utf-8")
        cmd = [self.bin_path, "-p", "--output-format", "text"]

        if req.model:
            cmd.extend(["--model", req.model])

        # `--tools`/`--allowedTools` filter built-ins only and do NOT cover MCP:
        # without --strict-mcp-config the user's global servers (context7,
        # Ref, ...) stay reachable even in "off" mode, letting a critic hit the
        # network and making runs depend on the host's MCP setup.
        # --strict-mcp-config limits MCP to exactly what we pass here.
        servers = self._servers_for(req)
        mcp_tools = [f"mcp__{name}" for name in servers]

        tools = _TOOLS_BY_MODE.get(req.tools, _TOOLS_BY_MODE["minimal"])
        if req.tools == "web":
            cmd.extend(["--permission-mode", "auto"])
            # MCP tools must also be allow-listed to be callable.
            cmd.extend(["--allowedTools", ",".join([tools, *mcp_tools])])
        else:
            cmd.extend(["--permission-mode", "dontAsk"])
            cmd.extend(["--tools", tools])
            if mcp_tools:
                # `--tools` covers built-ins only; permit the opted-in servers.
                cmd.extend(["--allowedTools", ",".join(mcp_tools)])

        cmd.append("--strict-mcp-config")
        if servers:
            cmd.extend(["--mcp-config", json.dumps({"mcpServers": servers})])

        if req.system:
            # Keep system short on argv; long bodies go in the prompt file
            if len(req.system) < 4000:
                cmd.extend(["--append-system-prompt", req.system])

        cmd.extend(self.extra_args)

        # Avoid ARG_MAX: for large prompts, point Claude at the file with Read.
        # In tools="off" mode Read is disabled, so pipe the prompt via stdin
        # instead (`claude -p` reads the prompt from stdin when none is given).
        if len(prompt) > ARG_MAX_SOFT:
            if req.tools == "off":
                req.prompt_via_stdin = True
                return cmd
            body = (
                f"Read the full instructions and materials from this file, "
                f"then complete the task and print only the final Markdown output:\n"
                f"{prompt_file.resolve()}"
            )
        else:
            body = prompt

        # `--` terminates option parsing. Without it, a variadic flag such as
        # --tools/--allowedTools/--mcp-config swallows the prompt as another
        # value and the CLI exits with "Input must be provided...".
        cmd.extend(["--", body])
        return cmd

    def _servers_for(self, req: InvokeRequest) -> dict[str, dict]:
        """MCP servers to expose for this request.

        Servers may declare `tool_modes` to limit which tool modes they load
        in; omitting it means "all modes". This keeps research able to use
        doc-lookup servers while critique stays isolated.
        """
        selected: dict[str, dict] = {}
        for name, spec in self.mcp_servers.items():
            spec = dict(spec)
            if not spec.pop("enabled", True):
                continue
            modes = spec.pop("tool_modes", None)
            if modes and req.tools not in modes:
                continue
            selected[name] = spec
        return selected
