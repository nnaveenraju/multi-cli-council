"""Regression tests for article/metadata section splitting.

The model controls this formatting, so section order and heading level vary
between runs. These cases previously silently dropped the change log.
"""

from council.pipeline import _split_draft, _split_final
from council.sections import article_body, split_sections


def test_change_log_without_revision_plan_is_captured():
    """Previously lost: changelog was only parsed inside the plan branch."""
    text = "# My Paper\nBody text.\n# Change Log\n- fixed things\n"
    paper, plan, changelog = _split_final(text)
    assert paper == "# My Paper\nBody text.\n"
    assert changelog.startswith("# Change Log")
    assert "fixed things" in changelog
    assert plan == ""
    # and it must not leak into the published article
    assert "# Change Log" not in paper


def test_normal_order():
    text = "# P\nBody.\n# Revision Plan Applied\nplan\n# Change Log\nlog\n"
    paper, plan, changelog = _split_final(text)
    assert paper == "# P\nBody.\n"
    assert plan.startswith("# Revision Plan Applied")
    assert "plan" in plan
    assert changelog.startswith("# Change Log")
    assert "log" in changelog
    # sections must not bleed into each other
    assert "Change Log" not in plan


def test_reversed_order():
    text = "# P\nBody.\n# Change Log\nlog\n# Revision Plan Applied\nplan\n"
    paper, plan, changelog = _split_final(text)
    assert paper == "# P\nBody.\n"
    assert "plan" in plan
    assert "log" in changelog
    assert "Revision Plan" not in changelog


def test_h2_headings_and_remaining_risks_excluded():
    text = "# P\nBody.\n## Change Log\nlog\n# Remaining risks\nrisky\n"
    paper, _plan, changelog = _split_final(text)
    assert paper == "# P\nBody.\n"
    assert "log" in changelog
    assert "risky" not in changelog


def test_no_metadata_leaves_body_intact():
    text = "# P\n\nJust an article.\n"
    paper, plan, changelog = _split_final(text)
    assert paper == text.strip() + "\n"
    assert plan == ""
    assert changelog == ""


def test_marker_at_start_of_document():
    """Old implementation required a leading newline, missing offset-0 markers."""
    body, sections = split_sections("# Change Log\nonly this\n")
    assert body == "\n"
    assert "Change Log" in sections


def test_article_body_helper_matches_split():
    """article_body() is the shared entry point for pipeline/export/images."""
    text = "# P\nBody.\n# Change Log\nlog\n"
    assert article_body(text) == split_sections(text)[0] == "# P\nBody.\n"


def test_h2_figures_gallery_is_not_a_metadata_section():
    """`# Figures` is content the exporter keeps, not metadata to strip."""
    text = "# P\nBody.\n\n# Figures\n\n![a](final/images/figure_01.png)\n"
    assert "Figures" in article_body(text)
    assert "figure_01.png" in article_body(text)


def test_split_draft_claims_trace():
    paper, claims = _split_draft("# Paper\nBody.\n# Claims Trace\n- c1\n")
    assert paper == "# Paper\nBody.\n"
    assert claims.startswith("# Claims Trace")
    assert "c1" in claims


def test_prose_mentioning_section_name_is_not_split():
    """Only top-level headings count, not inline references."""
    text = "# P\n\nSee the Change Log for details.\n"
    paper, _plan, changelog = _split_final(text)
    assert "Change Log for details" in paper
    assert changelog == ""
