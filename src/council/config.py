"""Load and resolve council configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

ToolsMode = Literal["off", "minimal", "web"]


class McpServerConfig(BaseModel):
    """One MCP server exposed to a provider's CLI.

    `command`/`args`/`env` follow the standard MCP stdio shape; `url` is used
    for HTTP servers. `tool_modes` limits which tool modes load this server
    (default: all), and `enabled` allows switching one off without deleting it.
    """

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    enabled: bool = True
    tool_modes: list[ToolsMode] = Field(default_factory=list)

    def to_mcp_entry(self) -> dict[str, Any]:
        """Serialize to the CLI's `mcpServers` JSON shape."""
        if self.url:
            entry: dict[str, Any] = {"type": "http", "url": self.url}
        else:
            entry = {"command": self.command, "args": list(self.args)}
        if self.env:
            entry["env"] = dict(self.env)
        return entry


class ProviderConfig(BaseModel):
    bin: str
    default_model: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    # Opt-in MCP servers. Only the Claude adapter supports these today.
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)


class MemberConfig(BaseModel):
    provider: str
    model: str | None = None
    label: str = ""
    role_slant: str = ""


# Default for RoleConfig.synthesis_excerpt_chars. Rationale: ~5k tokens of
# evidence context — enough for a critic to check claims against sources —
# while keeping (excerpt + full draft + prompt template) under the ~80k-char
# argv soft cap that kimi/agy hit with `-p`-style invocation. Tunable per
# role in config.yaml; this is only the fallback when a config omits the key.
DEFAULT_SYNTHESIS_EXCERPT_CHARS = 20_000


class RoleConfig(BaseModel):
    """Stage role: either a fixed seat (provider/model) or multi-participant."""

    provider: str | None = None
    model: str | None = None
    participants: list[str] = Field(default_factory=list)
    tools: ToolsMode = "minimal"
    timeout_seconds: int = 900
    parallel: bool = True
    # Critique only: how much of research/synthesis.md goes into each critic's
    # prompt. The draft is never truncated; the evidence excerpt is. Raise on
    # source-heavy runs, lower to stay under argv limits for -p-style CLIs.
    synthesis_excerpt_chars: int = DEFAULT_SYNTHESIS_EXCERPT_CHARS
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    label: str = ""


class PipelineConfig(BaseModel):
    stages: list[str] = Field(
        default_factory=lambda: ["seed", "research", "draft", "critique", "finalize"]
    )
    research_required: bool = True


class StorageConfig(BaseModel):
    sessions_dir: str = "data/sessions"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class InvokeConfig(BaseModel):
    max_parallel: int = 3
    retries: int = 1
    prefer_text_output: bool = True


class OutputConfig(BaseModel):
    """Artifact formats after finalize. md is always produced by the pipeline."""

    default_format: Literal["md", "docx"] = "md"
    # Optional auto-export after finalize (in addition to paper_final.md)
    auto_export: list[Literal["md", "docx"]] = Field(default_factory=lambda: ["md"])


class ImagesConfig(BaseModel):
    """Post-finalize image generation defaults."""

    default_count: int = 4
    style: str = "clean technical blog illustration, flat vector, high contrast"
    # provider/model for planning (falls back to roles.image_planner)
    include_svg: bool = True
    include_prompts: bool = True


class CouncilConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    members: dict[str, MemberConfig]
    roles: dict[str, RoleConfig]
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    invoke: InvokeConfig = Field(default_factory=InvokeConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)

    # Runtime (not from yaml)
    project_root: Path = Field(default_factory=Path.cwd)
    config_path: Path | None = None

    def sessions_path(self) -> Path:
        p = Path(self.storage.sessions_dir)
        if not p.is_absolute():
            p = self.project_root / p
        return p

    def resolve_model(self, provider: str, model: str | None) -> str | None:
        if model:
            return model
        prov = self.providers.get(provider)
        return prov.default_model if prov else None

    def member_invoke_spec(self, member_id: str, stage_role: str | None = None) -> dict[str, Any]:
        """Resolve provider/model/label/tools for a named council member."""
        if member_id not in self.members:
            raise KeyError(f"Unknown member: {member_id}")
        member = self.members[member_id]
        provider = member.provider
        model = member.model
        tools: ToolsMode = "minimal"
        timeout = 900
        label = member.label or f"{provider}:{model or 'default'}"

        if stage_role and stage_role in self.roles:
            role = self.roles[stage_role]
            tools = role.tools
            timeout = role.timeout_seconds
            if member_id in role.overrides:
                ov = role.overrides[member_id]
                provider = ov.get("provider", provider)
                model = ov.get("model", model)
                if "label" in ov:
                    label = ov["label"]

        model = self.resolve_model(provider, model)
        return {
            "member_id": member_id,
            "provider": provider,
            "model": model,
            "label": label,
            "role_slant": member.role_slant,
            "tools": tools,
            "timeout_seconds": timeout,
        }

    def seat_invoke_spec(self, role_name: str) -> dict[str, Any]:
        """Resolve a single-seat role (chairman, draft_writer, finalize)."""
        if role_name not in self.roles:
            raise KeyError(f"Unknown role: {role_name}")
        role = self.roles[role_name]
        if not role.provider:
            raise ValueError(f"Role {role_name} has no provider (use member_invoke_spec)")
        model = self.resolve_model(role.provider, role.model)
        label = role.label or f"{role.provider}:{model or 'default'} ({role_name})"
        return {
            "member_id": role_name,
            "provider": role.provider,
            "model": model,
            "label": label,
            "role_slant": role_name,
            "tools": role.tools,
            "timeout_seconds": role.timeout_seconds,
        }


def find_project_root(start: Path | None = None) -> Path:
    """Walk up looking for config.yaml or pyproject.toml."""
    cur = (start or Path.cwd()).resolve()
    for _ in range(8):
        if (cur / "config.yaml").exists() or (cur / "pyproject.toml").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return (start or Path.cwd()).resolve()


def load_config(
    path: Path | str | None = None,
    project_root: Path | str | None = None,
) -> CouncilConfig:
    root = Path(project_root).resolve() if project_root else find_project_root()
    cfg_path = Path(path) if path else root / "config.yaml"
    if not cfg_path.is_absolute():
        cfg_path = (root / cfg_path).resolve()

    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    config = CouncilConfig.model_validate(raw)
    config.project_root = root
    config.config_path = cfg_path

    # Optional local override
    local = root / "config.local.yaml"
    if local.exists():
        local_raw = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        merged = _deep_merge(raw, local_raw)
        config = CouncilConfig.model_validate(merged)
        config.project_root = root
        config.config_path = cfg_path

    return config


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
