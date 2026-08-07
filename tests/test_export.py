from pathlib import Path

from council.export import markdown_to_docx, normalize_format
from council.storage import SessionStore


def test_normalize_format():
    assert normalize_format("md") == "md"
    assert normalize_format(".markdown") == "md"
    assert normalize_format("word") == "docx"
    assert normalize_format("docx") == "docx"


def test_markdown_to_docx(tmp_path: Path):
    md = """# Hello

This is **bold** and *italic*.

## Section

- one
- two

1. a
2. b
"""
    dest = tmp_path / "out.docx"
    markdown_to_docx(md, dest, title="Hello")
    assert dest.exists()
    assert dest.stat().st_size > 1000


def test_export_md_session(tmp_path: Path):
    from council.export import export_session

    store = SessionStore(tmp_path, session_id="testexport")
    store.write_text(
        "final/paper_final.md",
        "# Title\n\nBody text.\n\n# Revision Plan Applied\n\nignore me\n",
    )
    store.update_meta(title="Title")
    out = export_session(store, fmt="md")
    text = out.read_text(encoding="utf-8")
    assert "Body text" in text
    assert "Revision Plan" not in text
