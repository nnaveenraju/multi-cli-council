from pathlib import Path

from council.export import export_session
from council.media import render_png_figure, rewrite_md_images_to_png
from council.storage import SessionStore


def test_render_png_figure(tmp_path: Path):
    dest = tmp_path / "figure_01.png"
    render_png_figure(
        dest,
        title="Agent Network Overview",
        caption="How brokers connect",
        fig_type="architecture",
        elements=["Broker", "Agent A", "Agent B"],
        index=1,
    )
    assert dest.exists()
    assert dest.stat().st_size > 5000


def test_word_with_images_embeds_png(tmp_path: Path):
    store = SessionStore(tmp_path, session_id="imgexport")
    store.update_meta(title="Demo")
    img_dir = store.path / "final" / "images"
    img_dir.mkdir(parents=True)
    png = img_dir / "figure_01.png"
    render_png_figure(
        png,
        title="Demo Figure",
        caption="Caption",
        fig_type="diagram",
        elements=["A", "B"],
        index=1,
    )
    store.write_text(
        "final/paper_with_figures.md",
        "# Demo\n\nIntro paragraph.\n\n# Figures\n\n"
        "## Demo Figure\n\n"
        "![Demo Figure](final/images/figure_01.png)\n\n"
        "*Caption*\n",
    )
    out = export_session(store, fmt="docx", with_images=True)
    assert out.exists()
    assert out.suffix == ".docx"
    assert out.stat().st_size > 10_000


def test_rewrite_md_prefers_png(tmp_path: Path):
    svg = tmp_path / "final" / "images" / "figure_01.svg"
    svg.parent.mkdir(parents=True)
    svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    png = tmp_path / "final" / "images" / "figure_01.png"
    render_png_figure(
        png,
        title="T",
        caption="C",
        fig_type="diagram",
        elements=["X"],
        index=1,
    )
    md = "![T](final/images/figure_01.svg)\n"
    out = rewrite_md_images_to_png(md, tmp_path)
    assert ".png" in out
