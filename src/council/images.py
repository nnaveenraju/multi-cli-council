"""Post-finalize image planning and generation for a session."""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any

from rich.console import Console

from council.config import CouncilConfig
from council.export import resolve_final_markdown
from council.models import invoke_model
from council.prompts import render_prompt
from council.sections import article_body
from council.storage import SessionStore


async def generate_images(
    config: CouncilConfig,
    store: SessionStore,
    *,
    count: int | None = None,
    style: str | None = None,
    console: Console | None = None,
) -> dict[str, Any]:
    """
    After finalize: plan figures from the paper, write prompts + SVG assets.

    Artifacts under final/images/:
      plan.json, plan.md, figure_NN.svg/.png/.prompt.md, index.md
    """
    console = console or Console()
    paper_path = resolve_final_markdown(store)
    # Plan figures from the article body only, not the revision metadata.
    paper = article_body(paper_path.read_text(encoding="utf-8"))

    n = count if count is not None else config.images.default_count
    style_s = style or config.images.style
    meta = store.load_meta()
    title = meta.get("title") or "Paper"

    out_dir = store.path / "final" / "images"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve planner seat
    if "image_planner" in config.roles and config.roles["image_planner"].provider:
        seat = config.seat_invoke_spec("image_planner")
    else:
        seat = {
            "provider": "claude",
            "model": config.resolve_model("claude", "opus"),
            "label": "Claude Opus (image planner)",
            "tools": "minimal",
            "timeout_seconds": 900,
            "member_id": "image_planner",
        }

    console.print(
        f"  [bold cyan]images[/] planning with "
        f"{seat['provider']}:{seat['model']} ({n} figures)"
    )

    prompt = render_prompt(
        config.project_root,
        "images_plan.md",
        title=title,
        paper=paper[:60000],
        count=str(n),
        style=style_s,
    )
    result = await invoke_model(
        config,
        provider=seat["provider"],
        model=seat["model"],
        prompt=prompt,
        system=(
            "You plan illustrations for a blog/research article. "
            "Output ONLY valid JSON as specified."
        ),
        tools=seat.get("tools", "minimal"),
        timeout_seconds=int(seat.get("timeout_seconds", 900)),
        cwd=out_dir / "_planner",
        label=str(seat.get("label", "image_planner")),
        member_id="image_planner",
    )
    store.write_text("final/images/planner_raw.md", result.text or result.error or "")
    if not result.ok:
        raise RuntimeError(f"Image planning failed: {result.error}")

    plan = _parse_plan_json(result.text or "")
    figures = plan.get("figures")
    if not isinstance(figures, list) or not figures:
        raise RuntimeError("Image planner returned no figures")

    # Cap to requested count
    plan["figures"] = figures[:n]
    plan_path = out_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    store.write_text("final/images/plan.md", _plan_markdown(plan))

    generated: list[dict[str, str]] = []
    for idx, fig in enumerate(plan["figures"], start=1):
        if not isinstance(fig, dict):
            # A malformed entry must not kill the whole job after the
            # planner already succeeded.
            console.print(f"  [yellow]Skipping malformed figure #{idx}: {fig!r}[/]")
            continue
        fig_id = f"figure_{idx:02d}"
        fig_title = str(fig.get("title") or f"Figure {idx}")
        fig_prompt = str(fig.get("image_prompt") or fig.get("prompt") or fig_title)
        fig_type = str(fig.get("type") or "illustration")
        caption = str(fig.get("caption") or fig_title)
        section = str(fig.get("section") or "")

        prompt_rel = ""
        if config.images.include_prompts:
            prompt_file = out_dir / f"{fig_id}.prompt.md"
            prompt_file.write_text(
                f"# {fig_title}\n\n"
                f"**Type:** {fig_type}\n"
                f"**Section:** {section}\n"
                f"**Caption:** {caption}\n\n"
                f"## Image generation prompt\n\n{fig_prompt}\n\n"
                f"## Style\n\n{style_s}\n",
                encoding="utf-8",
            )
            prompt_rel = str(prompt_file.relative_to(store.path))

        elements = fig.get("elements") or []
        if isinstance(elements, str):
            # Model returned prose instead of a list — one label, not chars.
            elements = [elements]
        svg_path = out_dir / f"{fig_id}.svg"
        png_path = out_dir / f"{fig_id}.png"
        if config.images.include_svg:
            svg = _render_svg_figure(
                title=fig_title,
                caption=caption,
                fig_type=fig_type,
                elements=elements,
                index=idx,
            )
            svg_path.write_text(svg, encoding="utf-8")

        # Always write PNG for Word embed (Pillow)
        from council.media import render_png_figure

        render_png_figure(
            png_path,
            title=fig_title,
            caption=caption,
            fig_type=fig_type,
            elements=elements,
            index=idx,
        )

        generated.append(
            {
                "id": fig_id,
                "title": fig_title,
                "type": fig_type,
                "prompt": prompt_rel,
                "svg": str(svg_path.relative_to(store.path)) if svg_path.exists() else "",
                "png": str(png_path.relative_to(store.path)) if png_path.exists() else "",
                "caption": caption,
                "section": section,
            }
        )
        console.print(f"  [green]✓[/] {fig_id}: {fig_title}")

    # Optional external generator command from config env-like field
    # (future: config.images.generator_cmd)

    index = _index_markdown(title, generated, style_s)
    store.write_text("final/images/index.md", index)

    # Markdown with embedded SVG refs for optional re-export
    illustrated = _illustrated_paper(paper, generated)
    store.write_text("final/paper_with_figures.md", illustrated)

    store.mark_artifact("images_index", "final/images/index.md")
    store.mark_artifact("images_plan", "final/images/plan.json")
    store.mark_artifact("paper_with_figures", "final/paper_with_figures.md")
    store.update_meta(images_generated=len(generated))

    return {
        "count": len(generated),
        "figures": generated,
        "dir": str(out_dir),
        "index": "final/images/index.md",
    }


def _parse_plan_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # Extract fenced JSON if present
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    # Find first { ... last }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if isinstance(data, list):
        return {"figures": data}
    if "figures" not in data and "images" in data:
        data["figures"] = data["images"]
    return data


def _plan_markdown(plan: dict[str, Any]) -> str:
    lines = ["# Image plan", ""]
    for i, fig in enumerate(plan.get("figures") or [], start=1):
        if not isinstance(fig, dict):
            # Planner JSON drift (strings/nulls) must not crash plan.md write.
            lines.append(f"## Figure {i}: (malformed entry)")
            lines.append(f"- Raw: {fig!r}")
            lines.append("")
            continue
        lines.append(f"## Figure {i}: {fig.get('title', '')}")
        lines.append(f"- Type: {fig.get('type', '')}")
        lines.append(f"- Section: {fig.get('section', '')}")
        lines.append(f"- Caption: {fig.get('caption', '')}")
        lines.append("")
        lines.append(str(fig.get("image_prompt") or fig.get("prompt") or ""))
        lines.append("")
    return "\n".join(lines)


def _index_markdown(title: str, figures: list[dict[str, str]], style: str) -> str:
    lines = [
        f"# Images for: {title}",
        "",
        f"Style: {style}",
        "",
        "| # | Title | Type | PNG | SVG | Prompt |",
        "|---|-------|------|-----|-----|--------|",
    ]
    for i, f in enumerate(figures, start=1):
        lines.append(
            f"| {i} | {f['title']} | {f['type']} | `{f.get('png', '')}` | "
            f"`{f.get('svg', '')}` | `{f.get('prompt', '')}` |"
        )
    lines.extend(
        [
            "",
            "## Usage",
            "",
            "1. PNG files embed into Word (`council word <id> --with-images`).",
            "2. SVG files open in a browser or design tool.",
            "3. `*.prompt.md` files can be pasted into any image generator.",
            "4. `final/paper_with_figures.md` references PNG figures.",
            "",
            "```bash",
            "council word <session_id> --with-images",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _illustrated_paper(paper: str, figures: list[dict[str, str]]) -> str:
    """Append a figures gallery with PNG embeds for Word export."""
    body = paper.rstrip() + "\n\n---\n\n# Figures\n\n"
    for f in figures:
        # Prefer PNG for Word; fall back to SVG path
        img = f.get("png") or f.get("svg") or ""
        body += f"## {f['title']}\n\n"
        if img:
            body += f"![{f['title']}]({img})\n\n"
        if f.get("caption"):
            body += f"*{f['caption']}*\n\n"
    return body


def _render_svg_figure(
    *,
    title: str,
    caption: str,
    fig_type: str,
    elements: list[Any],
    index: int,
) -> str:
    """Generate a clean technical SVG card (no external renderer needed)."""
    w, h = 960, 540
    # palette
    bg = "#0f1419"
    panel = "#1a2332"
    accent = "#4f8cff"
    accent2 = "#3dd68c"
    text = "#e8eaed"
    muted = "#9aa0a6"

    labels: list[str] = []
    for el in elements[:6]:
        if isinstance(el, str):
            labels.append(el)
        elif isinstance(el, dict):
            labels.append(str(el.get("label") or el.get("name") or el))
    if not labels:
        labels = [title[:40], fig_type.replace("_", " ").title(), "Key concept"]

    # Layout boxes in a row/flow
    n = max(len(labels), 1)
    box_w = min(200, (w - 80) // n - 16)
    box_h = 80
    y = 200
    boxes = []
    total_w = n * box_w + (n - 1) * 24
    x0 = (w - total_w) // 2
    for i, lab in enumerate(labels):
        x = x0 + i * (box_w + 24)
        boxes.append((x, y, lab[:28]))

    arrows = ""
    for i in range(len(boxes) - 1):
        x1 = boxes[i][0] + box_w
        x2 = boxes[i + 1][0]
        mid_y = y + box_h / 2
        arrows += (
            f'<line x1="{x1}" y1="{mid_y}" x2="{x2}" y2="{mid_y}" '
            f'stroke="{accent}" stroke-width="3" marker-end="url(#arrow)"/>\n'
        )

    box_svg = ""
    for i, (x, by, lab) in enumerate(boxes):
        color = accent if i % 2 == 0 else accent2
        wrapped = textwrap.wrap(lab, width=18) or [lab]
        tspan = ""
        for ti, line in enumerate(wrapped[:3]):
            tspan += (
                f'<tspan x="{x + box_w / 2}" dy="{"1.2em" if ti else 0}">'
                f"{_xml(line)}</tspan>"
            )
        box_svg += f"""
  <rect x="{x}" y="{by}" width="{box_w}" height="{box_h}" rx="12"
        fill="{panel}" stroke="{color}" stroke-width="2"/>
  <text x="{x + box_w / 2}" y="{by + box_h / 2 - 8}" fill="{text}"
        font-family="system-ui,Segoe UI,sans-serif" font-size="14"
        text-anchor="middle" font-weight="600">{tspan}</text>
"""

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3"
            orient="auto" markerUnits="strokeWidth">
      <path d="M0,0 L8,3 L0,6 Z" fill="{accent}"/>
    </marker>
  </defs>
  <rect width="100%" height="100%" fill="{bg}"/>
  <rect x="24" y="24" width="{w - 48}" height="{h - 48}" rx="16"
        fill="{panel}" stroke="#2a3544" stroke-width="1"/>
  <text x="48" y="64" fill="{accent}" font-family="system-ui,sans-serif"
        font-size="13" font-weight="700" letter-spacing="0.08em">
    FIGURE {index:02d} · {_xml(fig_type.upper())}
  </text>
  <text x="48" y="100" fill="{text}" font-family="system-ui,sans-serif"
        font-size="28" font-weight="700">{_xml(title[:60])}</text>
  <text x="48" y="132" fill="{muted}" font-family="system-ui,sans-serif"
        font-size="14">{_xml(caption[:100])}</text>
  {arrows}
  {box_svg}
  <text x="{w / 2}" y="{h - 48}" fill="{muted}" font-family="system-ui,sans-serif"
        font-size="12" text-anchor="middle">Local LLM Council · generated figure</text>
</svg>
'''


def _xml(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
