"""Load and render prompt templates from prompts/."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def prompts_dir(project_root: Path) -> Path:
    return project_root / "prompts"


def load_template(project_root: Path, name: str) -> str:
    path = prompts_dir(project_root) / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def render(template: str, **vars: Any) -> str:
    """Simple {{ var }} substitution (no logic). Missing keys → empty string."""

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        val = vars.get(key, "")
        return "" if val is None else str(val)

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, template)


def render_prompt(project_root: Path, name: str, **vars: Any) -> str:
    return render(load_template(project_root, name), **vars)
