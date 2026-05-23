"""Subreddit configuration for the Reddit ingester.

Each entry pairs a subreddit name with the per-subreddit filters that
narrow its post stream to Turkey-relevant content. Most named subreddits
(``Turkey``, ``TurkeyJerky``, ``AskTurkey``) need no extra filtering —
their entire feed is on-topic by definition. ``europe`` is included for
high-traffic Turkey threads but requires a flair match because its
default feed spans the whole continent.

Universal filters (last-24h window, score/comment thresholds) live in the
ingester. Per-subreddit overrides live here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubredditConfig:
    """Per-subreddit filter overrides.

    Attributes:
        name: Subreddit name without the ``r/`` prefix. Used with
            :meth:`praw.Reddit.subreddit`.
        flair_filter: When non-empty, the post's ``link_flair_text`` must
            contain at least one of these strings (case-insensitive) for
            the post to be included. ``r/europe`` is the canonical case.
        title_keywords: When non-empty, the post's title must contain at
            least one of these strings (case-insensitive). Currently unused
            but reserved for future hand-tuning of broad subreddits.
        listing_limit: Maximum number of posts to retrieve from each
            subreddit's ``new()`` listing per fetch. Keeps PRAW API usage
            bounded; ``None`` means PRAW's default (typically 100).
    """

    name: str
    flair_filter: tuple[str, ...] = ()
    title_keywords: tuple[str, ...] = ()
    listing_limit: int | None = 100


# Order: high-signal Turkey-specific subs first, then the Europe-filtered tail.
SUBREDDITS: tuple[SubredditConfig, ...] = (
    SubredditConfig(name="Turkey"),
    SubredditConfig(name="TurkeyJerky"),
    SubredditConfig(name="AskTurkey"),
    SubredditConfig(name="europe", flair_filter=("Turkey", "Türkiye")),
)


def matches_subreddit_filter(
    config: SubredditConfig,
    flair_text: str | None,
    title: str,
) -> bool:
    """Return True if a post passes the subreddit's per-source filter.

    Universal filters (last-24h window, score/comment thresholds) are the
    ingester's responsibility; this helper checks only the per-subreddit
    flair and title-keyword rules. ``True`` when no overrides are set so
    Turkey-specific subs pass everything through.
    """
    if config.flair_filter:
        flair = (flair_text or "").lower()
        if not any(kw.lower() in flair for kw in config.flair_filter):
            return False
    if config.title_keywords:
        lowered_title = title.lower()
        if not any(kw.lower() in lowered_title for kw in config.title_keywords):
            return False
    return True


__all__ = ["SUBREDDITS", "SubredditConfig", "matches_subreddit_filter"]
