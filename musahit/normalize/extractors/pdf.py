"""PDF body/title extractor — currently only Resmî Gazete.

The Gazette ingester already extracts each item's text via
:mod:`musahit.ingest.gazette_parsing` and stores the body in the row's
``headers`` JSON. This extractor just reads it and does light cleanup:

* Collapses runs of whitespace.
* Removes page-number artifacts that pdfplumber sometimes leaves at the
  top/bottom of a page when extracting multi-page items.

If a future PDF source produces text with very different artifacts,
add the source-specific cleanup here rather than in the ingester (the
ingester's job is to surface the raw extraction; the normalize stage
is where shape-tuning lives).
"""

from __future__ import annotations

import re
from typing import Any

# Match standalone page numbers on their own line, optionally with a
# leading word like "Sayfa". Conservative — only strips lines that are
# *only* digits (or "Sayfa N"), not real content that begins with a digit.
_PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:Sayfa\s+)?\d{1,4}\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of spaces/tabs; trim each line; preserve paragraph breaks."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(cleaned)
    body = "\n".join(lines)
    # Collapse 3+ blank lines into a paragraph break.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def extract_pdf_body(headers: dict[str, Any]) -> tuple[str, str]:
    """Return ``(title, body)`` from a PDF row's stored headers."""
    title = (headers.get("title") or "").strip()
    body = headers.get("body") or ""
    body = _PAGE_NUMBER_RE.sub("", body)
    body = _normalize_whitespace(body)
    return title, body


__all__ = ["extract_pdf_body"]
