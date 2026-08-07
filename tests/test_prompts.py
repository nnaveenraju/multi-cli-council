from pathlib import Path

from council.prompts import render, render_prompt


def test_render_vars():
    out = render("Hello {{ name }}!", name="Council")
    assert out == "Hello Council!"


def test_research_prompt_exists():
    root = Path(__file__).resolve().parents[1]
    text = render_prompt(
        root,
        "research.md",
        title="T",
        main_points="- a",
        seed_links="- https://x",
        goals="- g",
        role_slant="literature_map",
        label="Claude",
    )
    assert "literature_map" in text
    assert "https://x" in text
