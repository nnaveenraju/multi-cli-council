"""Splitting model output into an article body plus trailing metadata sections.

Both the pipeline (persisting `revision_plan.md` / `change_log.md`) and the
exporter (stripping metadata from the published article) need this, so it
lives here rather than in either one.

The model controls this formatting, so section order and heading level vary
between runs; sections are matched independently rather than positionally.
"""

from __future__ import annotations

import re

# Trailing metadata sections a model may append after the article body.
META_SECTIONS = (
    "Revision Plan Applied",
    "Change Log",
    "Remaining risks",
    "Claims Trace",
    "Outline Followed",
)

_SECTION_RE = re.compile(
    rf"^#{{1,2}}[ \t]+({'|'.join(re.escape(s) for s in META_SECTIONS)})[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)


def split_sections(text: str) -> tuple[str, dict[str, str]]:
    """Split leading article body from trailing top-level metadata sections.

    Returns ``(body, {section_title: section_text_including_heading})``.
    Sections are recognized anywhere at top level and in any order; the body
    is everything before the first one. Only real headings count, so prose
    mentioning a section name is left in the body.
    """
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return text.strip() + "\n", {}

    body = text[: matches[0].start()]
    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        canonical = next(s for s in META_SECTIONS if s.lower() == match.group(1).lower())
        chunk = text[match.start() : end].strip()
        # Repeated heading (model emitted it twice) — keep the fuller one.
        if canonical not in sections or len(chunk) > len(sections[canonical]):
            sections[canonical] = chunk
    return body.strip() + "\n", sections


def article_body(text: str) -> str:
    """Return just the article, dropping any trailing metadata sections."""
    body, _ = split_sections(text)
    return body
