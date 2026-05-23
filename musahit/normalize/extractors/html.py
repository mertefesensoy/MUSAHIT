"""HTML body/title extractor — primary path is :mod:`trafilatura`.

trafilatura is the project's main-content extraction library (ADR-003).
It strips boilerplate (script, style, nav, footer, share buttons) and
returns the readable body of an article. For pages where trafilatura
returns very little — single-page-application shells, paywalled stubs,
heavily-decorated category landings — we fall back to a CSS-selector
extraction using the ``body_selector`` registered for the source in
:mod:`musahit.ingest.html_selectors`.

The fallback threshold (``< 100 chars`` of trafilatura output) is a
heuristic, not a guarantee. Real-world tuning happens via the operator's
first nightly run.
"""

from __future__ import annotations

from typing import Any

import trafilatura
from selectolax.parser import HTMLParser

from musahit.ingest.html_selectors import SELECTORS

FALLBACK_MIN_CHARS: int = 100


def _trafilatura_body(raw_html: bytes | str) -> str:
    """Run trafilatura's extractor; return the cleaned text or ``""``."""
    try:
        result = trafilatura.extract(
            raw_html,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
    except Exception:
        return ""
    return (result or "").strip()


def _selector_body(raw_html: bytes, body_selector: str) -> str:
    """Extract text from ``raw_html`` via a CSS body selector."""
    tree = HTMLParser(raw_html)
    chunks: list[str] = []
    for node in tree.css(body_selector):
        text = node.text(deep=True, separator="\n")
        if text:
            chunks.append(text.strip())
    return "\n\n".join(chunks).strip()


def extract_html_body(
    raw_content: bytes,
    source_id: str,
    headers: dict[str, Any],
) -> tuple[str, str]:
    """Return ``(title, body)`` for an HTML article row.

    Title strategy: prefer the ingester-supplied ``headers["title"]`` (the
    HTML ingester populates this from ``config.title_selector`` or
    ``<title>``); fall back to scraping ``<title>``.

    Body strategy: trafilatura first; if output is short, try the
    source's ``body_selector`` (when configured) and use whichever is
    longer. Returns the longer of the two so a thin trafilatura result
    does not overwrite a useful selector extraction.
    """
    title = headers.get("title") or ""
    if not title:
        tree = HTMLParser(raw_content)
        title_node = tree.css_first("title")
        if title_node is not None:
            title = (title_node.text(deep=True) or "").strip()

    body = _trafilatura_body(raw_content)
    if len(body) < FALLBACK_MIN_CHARS:
        config = SELECTORS.get(source_id)
        if config is not None and config.body_selector:
            fallback = _selector_body(raw_content, config.body_selector)
            if len(fallback) > len(body):
                body = fallback

    return title.strip(), body.strip()


__all__ = ["FALLBACK_MIN_CHARS", "extract_html_body"]
