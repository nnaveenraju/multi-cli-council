"""Stage 0 — normalize author seed (points + links)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Seed(BaseModel):
    title: str = "Untitled research"
    main_points: list[str] = Field(default_factory=list)
    seed_links: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)

    def main_points_md(self) -> str:
        if not self.main_points:
            return "_No main points provided._"
        return "\n".join(f"- {p}" for p in self.main_points)

    def links_md(self) -> str:
        if not self.seed_links:
            return "_No seed links provided._"
        return "\n".join(f"- {u}" for u in self.seed_links)

    def goals_md(self) -> str:
        items = self.goals + [f"Constraint: {c}" for c in self.constraints]
        if not items:
            return "_None specified._"
        return "\n".join(f"- {g}" for g in items)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(), sort_keys=False, allow_unicode=True)


def load_seed_file(path: Path) -> Seed:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text) or {}
        return Seed.model_validate(data)
    # Markdown / plain: treat whole file as points block if structured poorly
    return parse_seed_markdown(text)


def parse_seed_markdown(text: str) -> Seed:
    """Best-effort parse of a simple markdown seed file."""
    title = "Untitled research"
    points: list[str] = []
    links: list[str] = []
    goals: list[str] = []
    section = "points"

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if line.startswith("# "):
            title = line[2:].strip()
            continue
        if lower.startswith("## links") or lower.startswith("## seed"):
            section = "links"
            continue
        if lower.startswith("## goal") or lower.startswith("## constraints"):
            section = "goals"
            continue
        if lower.startswith("## point") or lower.startswith("## main"):
            section = "points"
            continue
        if line.startswith("http://") or line.startswith("https://"):
            # Bare URL line — keep verbatim (strip() only; stripping "-" from
            # both ends would mangle URLs ending in a dash).
            links.append(line)
            continue
        if line.startswith("- ") or line.startswith("* "):
            item = line[2:].strip()
        elif line[0:1].isdigit() and "." in line[:4]:
            item = line.split(".", 1)[1].strip()
        else:
            item = line
        if item.startswith("http://") or item.startswith("https://"):
            links.append(item)
        elif section == "goals":
            goals.append(item)
        elif section == "links":
            links.append(item)
        else:
            points.append(item)

    return Seed(title=title, main_points=points, seed_links=links, goals=goals)


def build_seed(
    *,
    title: str | None = None,
    points: list[str] | None = None,
    links: list[str] | None = None,
    goals: list[str] | None = None,
    seed_file: Path | None = None,
    points_file: Path | None = None,
    links_file: Path | None = None,
) -> Seed:
    seed = Seed()
    if seed_file:
        seed = load_seed_file(seed_file)
    if points_file:
        text = points_file.read_text(encoding="utf-8")
        parsed = parse_seed_markdown(text)
        seed.main_points = parsed.main_points or seed.main_points
        if parsed.title != "Untitled research":
            seed.title = parsed.title
        if parsed.seed_links:
            seed.seed_links = parsed.seed_links
        if parsed.goals:
            seed.goals = parsed.goals
    if links_file:
        raw_links = [
            ln.strip()
            for ln in links_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        seed.seed_links = raw_links
    if title:
        seed.title = title
    if points:
        seed.main_points = points
    if links:
        # support comma-separated single string entries
        expanded: list[str] = []
        for item in links:
            if "," in item and item.count("http") > 1:
                expanded.extend(x.strip() for x in item.split(",") if x.strip())
            else:
                expanded.append(item)
        seed.seed_links = expanded
    if goals:
        seed.goals = goals
    return seed


def write_seed_artifacts(
    session_path_write: Callable[[str, str], Path],
    seed: Seed,
) -> dict[str, str]:
    """Write input/* artifacts; session_path_write is SessionStore.write_text."""
    arts = {
        "seed_yaml": "input/seed.yaml",
        "points": "input/points.md",
        "links": "input/links.txt",
        "goals": "input/goals.md",
    }
    session_path_write("input/seed.yaml", seed.to_yaml())
    session_path_write("input/points.md", f"# {seed.title}\n\n{seed.main_points_md()}\n")
    session_path_write(
        "input/links.txt",
        "\n".join(seed.seed_links) + ("\n" if seed.seed_links else ""),
    )
    session_path_write("input/goals.md", seed.goals_md() + "\n")
    return arts
