"""Canonical id-derivation helpers shared across all pipeline stages.

The single function exported here, :func:`article_id`, is the load-bearing
piece of ADR-014. Every ingester, every backfill script, and every debugging
tool that needs to compute a `raw_articles.id` or `articles.id` value MUST
import this function rather than re-implementing the formula — drift between
implementations would silently break cross-fetch dedup, foreign keys in
`cluster_articles`, and the promotion audit log.

See `ADR-014-article-id-formula.md` for the rationale and the alternatives
that were considered.
"""

from __future__ import annotations

import hashlib


def article_id(source_id: str, url: str) -> str:
    """Canonical article identifier per ADR-014.

    Returns the hex SHA-256 of ``f"{source_id}|{url}"`` (UTF-8 encoded). The
    formula intentionally excludes time and content components so that:

    - Re-fetching the same URL from the same source yields the same id
      (``INSERT OR IGNORE`` then suppresses the duplicate row).
    - Two sources syndicating the same URL still produce *different* ids,
      preserving cross-band corroboration semantics in ADR-005.
    - Article-version drift (silent editorial edits) does NOT mint a new id;
      revision tracking is an explicit open question in ADR-014 and is
      deferred to a future additive table.

    Args:
        source_id: The originating ``Source.id`` slug (e.g. ``"bianet"``).
        url: The article's canonical URL — the entry's ``link`` for RSS,
            the page URL for HTML scrapes, the disclosure URL for KAP, etc.

    Returns:
        64-character lowercase hex string suitable for a ``TEXT PRIMARY KEY``.
    """
    return hashlib.sha256(f"{source_id}|{url}".encode()).hexdigest()


__all__ = ["article_id"]
