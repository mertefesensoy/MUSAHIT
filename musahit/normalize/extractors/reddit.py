"""Reddit body/title extractor.

The Reddit ingester stores the post payload as JSON in ``raw_content``
(per the Reddit ingester impl doc): ``{title, selftext, comments,
author, score, num_comments}``. This extractor flattens that into a
single body text:

::

    <selftext>

    --- Yorumlar ---

    <comment 1 body>

    <comment 2 body>

    <comment 3 body>

The Turkish marker ``Yorumlar`` keeps the briefing readable in mixed-
language reading and groups social signal under one section the operator
can mentally skip when scanning.
"""

from __future__ import annotations

import json

COMMENT_SEPARATOR: str = "\n\n--- Yorumlar ---\n\n"


def extract_reddit_body(raw_content: bytes) -> tuple[str, str]:
    """Return ``(title, body)`` from a Reddit row's JSON payload."""
    try:
        text = raw_content.decode("utf-8") if isinstance(raw_content, bytes) else raw_content
        payload = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return "", ""

    title = (payload.get("title") or "").strip()
    selftext = (payload.get("selftext") or "").strip()

    comments = payload.get("comments") or []
    comment_bodies: list[str] = []
    for c in comments:
        if isinstance(c, dict):
            body = (c.get("body") or "").strip()
            if body:
                comment_bodies.append(body)

    parts: list[str] = []
    if selftext:
        parts.append(selftext)
    if comment_bodies:
        if parts:
            body = parts[0] + COMMENT_SEPARATOR + "\n\n".join(comment_bodies)
        else:
            body = COMMENT_SEPARATOR.lstrip("\n") + "\n\n".join(comment_bodies)
    else:
        body = parts[0] if parts else ""

    return title, body.strip()


__all__ = ["COMMENT_SEPARATOR", "extract_reddit_body"]
