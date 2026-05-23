"""RSS body/title extractor.

The RSS ingester already does the structural parse and stores the entry's
``title`` and ``body`` (which is the first non-empty of
``content:encoded`` / ``description`` / ``summary``) in the row's
``headers`` JSON. This extractor's job is to:

* Read those fields.
* Detect HTML markup in the body and run trafilatura when present —
  many feeds wrap their description in ``<p>``, ``<img>``, anchor tags.
"""

from __future__ import annotations

import re
from typing import Any

import trafilatura

# Conservative heuristic: any unencoded tag-like sequence triggers
# trafilatura. Feeds occasionally include `<` in plain text (math, code),
# but those almost always co-occur with `>` plus a tag name.
_HTML_MARKUP = re.compile(r"<[a-zA-Z][^>]*>")


def _looks_like_html(text: str) -> bool:
    return bool(_HTML_MARKUP.search(text))


def extract_rss_body(headers: dict[str, Any]) -> tuple[str, str]:
    """Return ``(title, body)`` from an RSS row's stored headers."""
    title = (headers.get("title") or "").strip()
    body = headers.get("body") or headers.get("summary") or ""
    body = body.strip()
    if body and _looks_like_html(body):
        try:
            cleaned = trafilatura.extract(body, include_comments=False) or ""
        except Exception:
            cleaned = ""
        # Trafilatura on a short snippet sometimes returns empty; only
        # take the cleaned version when it has comparable signal.
        if cleaned.strip():
            body = cleaned.strip()
    return title, body


__all__ = ["extract_rss_body"]
