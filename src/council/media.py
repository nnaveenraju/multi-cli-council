"""Image helpers: PNG figure rendering and SVG→PNG conversion for Word embed."""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any


def ensure_raster_image(
    path: Path,
    *,
    title: str = "",
    caption: str = "",
    fig_type: str = "diagram",
    elements: list[Any] | None = None,
    index: int = 1,
) -> Path | None:
    """
    Return a PNG/JPEG path Word can embed.

    Preference order:
    1. Existing .png/.jpg next to the path
    2. Convert .svg → .png (external tools if present)
    3. Render a matching PNG with Pillow from figure metadata
    """
    path = Path(path)
    if not path.exists() and path.suffix.lower() == ".svg":
        # try sibling png
        png = path.with_suffix(".png")
        if png.exists():
            return png

    if path.exists() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif"}:
        return path

    if path.exists() and path.suffix.lower() == ".svg":
        png = path.with_suffix(".png")
        if png.exists() and png.stat().st_mtime >= path.stat().st_mtime:
            return png
        converted = svg_to_png(path, png)
        if converted:
            return converted
        # Fall back to Pillow render using labels scraped from SVG or metadata
        labels = elements or _labels_from_svg(path)
        render_png_figure(
            png,
            title=title or path.stem,
            caption=caption,
            fig_type=fig_type,
            elements=labels,
            index=index,
        )
        return png if png.exists() else None

    # path may already be a relative missing file — try .png sibling name
    png = path.with_suffix(".png") if path.suffix else Path(str(path) + ".png")
    if png.exists():
        return png
    return None


def svg_to_png(svg_path: Path, png_path: Path) -> Path | None:
    """Best-effort SVG→PNG via system tools."""
    svg_path = Path(svg_path)
    png_path = Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)

    candidates: list[list[str]] = []
    if shutil.which("rsvg-convert"):
        candidates.append(
            ["rsvg-convert", "-w", "1600", "-o", str(png_path), str(svg_path)]
        )
    if shutil.which("magick"):
        candidates.append(
            ["magick", "-background", "none", str(svg_path), "-resize", "1600x", str(png_path)]
        )
    if shutil.which("convert"):
        candidates.append(
            ["convert", "-background", "none", str(svg_path), "-resize", "1600x", str(png_path)]
        )
    if shutil.which("inkscape"):
        candidates.append(
            [
                "inkscape",
                str(svg_path),
                "--export-type=png",
                f"--export-filename={png_path}",
                "-w",
                "1600",
            ]
        )

    for cmd in candidates:
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60)
            if png_path.exists() and png_path.stat().st_size > 0:
                return png_path
        except Exception:  # noqa: BLE001
            continue

    # Try cairosvg if installed
    try:
        import cairosvg  # type: ignore

        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path), output_width=1600)
        if png_path.exists():
            return png_path
    except Exception:  # noqa: BLE001
        pass

    return None


def render_png_figure(
    dest: Path,
    *,
    title: str,
    caption: str,
    fig_type: str,
    elements: list[Any] | None,
    index: int,
    width: int = 1600,
    height: int = 900,
) -> Path:
    """Draw a Word-friendly PNG diagram with Pillow (no SVG dependency)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError(
            "Pillow is required for PNG figures. Install with: uv pip install pillow"
        ) from exc

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    bg = (15, 20, 25)
    panel = (26, 35, 50)
    accent = (79, 140, 255)
    accent2 = (61, 214, 140)
    text_c = (232, 234, 237)
    muted = (154, 160, 166)
    border = (42, 53, 68)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
        # Prefer common system fonts; fall back to default
        candidates = [
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else None,
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
            "Arial.ttf",
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        ]
        for c in candidates:
            if not c:
                continue
            try:
                return ImageFont.truetype(c, size=size)
            except Exception:  # noqa: BLE001
                continue
        return ImageFont.load_default()

    f_label = font(22, bold=True)
    f_title = font(42, bold=True)
    f_cap = font(24)
    f_box = font(22, bold=True)
    f_foot = font(18)

    # Outer panel
    margin = 40
    draw.rounded_rectangle(
        [margin, margin, width - margin, height - margin],
        radius=24,
        fill=panel,
        outline=border,
        width=2,
    )

    # Header
    header = f"FIGURE {index:02d}  ·  {fig_type.upper()}"
    draw.text((margin + 36, margin + 28), header, fill=accent, font=f_label)
    draw.text((margin + 36, margin + 70), title[:70], fill=text_c, font=f_title)
    draw.text((margin + 36, margin + 130), caption[:120], fill=muted, font=f_cap)

    labels: list[str] = []
    for el in (elements or [])[:6]:
        if isinstance(el, str):
            labels.append(el)
        elif isinstance(el, dict):
            labels.append(str(el.get("label") or el.get("name") or el))
    if not labels:
        labels = [title[:40] or "Concept", fig_type.replace("_", " ").title(), "Key idea"]

    n = len(labels)
    box_h = 110
    gap = 28
    max_box_w = 240
    usable = width - 2 * (margin + 48)
    box_w = min(max_box_w, (usable - gap * (n - 1)) // max(n, 1))
    total = n * box_w + (n - 1) * gap
    x0 = (width - total) // 2
    y = height // 2 - 10

    for i, lab in enumerate(labels):
        x = x0 + i * (box_w + gap)
        color = accent if i % 2 == 0 else accent2
        draw.rounded_rectangle(
            [x, y, x + box_w, y + box_h],
            radius=16,
            fill=(22, 30, 42),
            outline=color,
            width=3,
        )
        # wrap label
        lines = textwrap.wrap(lab[:40], width=14) or [lab[:40]]
        ty = y + box_h // 2 - 12 * len(lines)
        for line in lines[:3]:
            bbox = draw.textbbox((0, 0), line, font=f_box)
            tw = bbox[2] - bbox[0]
            draw.text((x + (box_w - tw) / 2, ty), line, fill=text_c, font=f_box)
            ty += 26
        if i < n - 1:
            # arrow line
            ax1 = x + box_w
            ax2 = x + box_w + gap
            mid = y + box_h // 2
            draw.line([(ax1, mid), (ax2 - 8, mid)], fill=accent, width=4)
            draw.polygon(
                [(ax2 - 2, mid), (ax2 - 14, mid - 8), (ax2 - 14, mid + 8)],
                fill=accent,
            )

    foot = "Multi-CLI Council · generated figure"
    bbox = draw.textbbox((0, 0), foot, font=f_foot)
    tw = bbox[2] - bbox[0]
    draw.text(((width - tw) / 2, height - margin - 36), foot, fill=muted, font=f_foot)

    img.save(dest, format="PNG", optimize=True)
    return dest


def _labels_from_svg(svg_path: Path) -> list[str]:
    try:
        text = svg_path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    # crude: tspan contents
    labels = re.findall(r"<tspan[^>]*>([^<]+)</tspan>", text)
    cleaned = []
    for lab in labels:
        lab = lab.strip()
        if lab and lab not in cleaned and not lab.startswith("FIGURE"):
            cleaned.append(lab)
    return cleaned[:6]


def rewrite_md_images_to_png(markdown: str, base_dir: Path) -> str:
    """Rewrite ![alt](path) to prefer embeddable PNGs.

    Paths under base_dir are rewritten relative to it so exported Markdown
    stays portable; markdown_to_docx resolves them against the same base_dir.
    """

    def repl(m: re.Match[str]) -> str:
        alt, src = m.group(1), m.group(2)
        p = Path(src)
        if not p.is_absolute():
            # try as stored (final/images/figure_01.svg) and under final/
            candidates = [
                base_dir / src,
                base_dir / "final" / src,
            ]
            if base_dir.name == "export":
                candidates.append(base_dir.parent / src)
            found = next((c for c in candidates if c.exists()), None)
            p = found or (base_dir / src)
        raster = ensure_raster_image(p, title=alt or p.stem)
        if raster and raster.exists():
            try:
                shown = raster.resolve().relative_to(base_dir.resolve())
            except ValueError:
                shown = raster.resolve()
            return f"![{alt}]({shown})"
        return m.group(0)

    return re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl, markdown)
