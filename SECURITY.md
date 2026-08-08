# Security Policy

## Supported versions

This project is pre-1.0. Only the latest commit on `main` receives security
fixes.

| Version | Supported |
|---------|-----------|
| `main`  | yes       |
| releases | best effort — upgrade to latest |

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report them privately via
[GitHub private vulnerability reporting](https://github.com/nnaveenraju/multi-cli-council/security/advisories/new).

Include:

- A description of the issue and its impact
- Steps to reproduce or a proof of concept
- Affected version / commit

You can expect an acknowledgment within a few days. If the issue is confirmed,
we will prepare a fix and coordinate disclosure with you.

## Security notes for users

This project shells out to third-party CLIs and lets LLMs use tools, so a few
things are worth knowing:

- **Never commit secrets.** `config.local.yaml`, `.env`, and `data/` are
  gitignored for a reason. Do not put API keys or tokens in `config.yaml` or
  seed files.
- **Stages with `tools: web` fetch untrusted pages.** Web research output is
  inherently prompt-injection-prone. Critique stages are intentionally offline
  (`minimal` / `"off"`) so reviews can't be steered by a fetched page — keep
  it that way unless you have a deliberate reason. Note the offline guarantee
  holds only for **Claude** seats (and for **Grok** built-ins); **Kimi** and
  **Antigravity** seats can still reach the network in any stage.
- **Review `extra_args` carefully.** Flags like `--always-approve` (grok) or
  `--dangerously-skip-permissions` (agy) grant the model unattended tool
  execution. Understand what your provider CLI will allow before enabling
  them.
- **MCP servers run local commands.** Anything listed under
  `providers.claude.mcp_servers` executes on your machine; only add servers
  you trust, and scope network-touching ones to `tool_modes: [web]`.
- The Web UI (`council serve`) binds to `127.0.0.1` by default. Do not expose
  it on a public interface without adding authentication.
