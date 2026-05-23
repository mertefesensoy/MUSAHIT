"""Reddit ingester via PRAW.

Reddit is the seventh and final ingester. The PRAW client is synchronous,
so the whole fetch runs inside :func:`asyncio.to_thread` to honor the
``async def fetch(source)`` contract from the Ingester Protocol without
forcing the rest of the pipeline to deal with a thread-blocked event loop.

Filters (ADR-003):

* Window: posts created in the last 24h (UTC).
* Threshold: ``score >= reddit_min_score (50)`` OR
  ``num_comments >= reddit_min_comments (25)``.
* Per-subreddit overrides: ``r/europe`` requires a Turkey flair.

The ingester DOES NOT enforce the DEFCON 4 hard cap from ADR-005 — that
ceiling is applied by the score/promotion stage. The ingester just lands
qualifying posts in ``raw_articles``.

Persistence shape per post:

* ``url`` = full Reddit URL constructed from ``post.permalink``.
* ``article_id`` = :func:`musahit.common.ids.article_id` on
  ``(source_id, url)`` — shared formula (ADR-014).
* ``feed_entry_id`` = ``post.id`` (Reddit's native post id like ``"1abc23"``).
* ``canonical_timestamp`` = ``post.created_utc`` as naive UTC.
* ``raw_content`` = JSON-encoded payload (title, selftext truncated 500 chars,
  top 3 comments truncated 200 chars each, author, score, num_comments).
* ``headers`` = subreddit, score, num_comments, flair, ``external_url``
  (null for selftext posts).
* ``ON CONFLICT (id) DO NOTHING`` for cross-fetch dedup.

The raw_content JSON is the per-ingester contract for the normalize stage:
ADR-006 specifies ``articles.body`` as text, so the normalize stage will
need to flatten this payload into a plain-text body (title + selftext +
comments) when it lands. The JSON shape is documented in the implementation
doc so that flattening is straightforward.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import duckdb
import prawcore

try:
    import praw
except ImportError:  # pragma: no cover
    praw = None  # type: ignore[assignment]

from musahit.common.config import Settings
from musahit.common.ids import article_id
from musahit.common.logging import get_logger
from musahit.common.time import to_utc_naive, utcnow
from musahit.common.types import IngestStatus
from musahit.ingest import USER_AGENT, IngestResult
from musahit.ingest.reddit_subreddits import (
    SUBREDDITS,
    SubredditConfig,
    matches_subreddit_filter,
)
from musahit.ingest.sources import Source

_log = get_logger("musahit.ingest.reddit")

WINDOW_SECONDS: float = 24 * 60 * 60  # 24h rolling window per ADR-003.
SELFTEXT_TRUNC: int = 500
COMMENT_TRUNC: int = 200
TOP_COMMENTS: int = 3


# ── Helpers ────────────────────────────────────────────────────────────────


def _truncate(text: str | None, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters; ``None`` becomes ``""``."""
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit]


def _author_name(post: Any) -> str | None:
    """PRAW exposes ``post.author`` as a Redditor or ``None`` (deleted)."""
    author = getattr(post, "author", None)
    if author is None:
        return None
    return getattr(author, "name", None) or str(author) or None


def _top_comments(post: Any, limit: int = TOP_COMMENTS) -> list[dict[str, Any]]:
    """Take up to ``limit`` top-level comments, truncated and structured.

    PRAW's ``post.comments`` is a ``CommentForest``; for cheap tests we
    accept anything iterable. ``replace_more(limit=0)`` would strip
    "MoreComments" stubs but requires a real PRAW connection — we skip
    it and just guard against the stubs at iteration time.
    """
    out: list[dict[str, Any]] = []
    try:
        forest = getattr(post, "comments", None) or []
        for comment in list(forest)[:limit]:
            body = getattr(comment, "body", None)
            if not body:
                continue
            out.append(
                {
                    "author": _author_name(comment),
                    "body": _truncate(body, COMMENT_TRUNC),
                    "score": getattr(comment, "score", None),
                }
            )
    except Exception:
        # Comments are best-effort; if PRAW errors on lazy load we drop them.
        return []
    return out


def _full_permalink(permalink: str) -> str:
    """Reddit's ``permalink`` is a leading-slash path; prepend the host."""
    if permalink.startswith("http://") or permalink.startswith("https://"):
        return permalink
    return f"https://www.reddit.com{permalink}"


def _passes_universal_filter(
    post: Any,
    *,
    now_epoch: float,
    min_score: int,
    min_comments: int,
) -> bool:
    """Apply the ADR-003 universal filter (window + threshold)."""
    created = float(getattr(post, "created_utc", 0.0) or 0.0)
    if created <= 0 or (now_epoch - created) > WINDOW_SECONDS:
        return False
    score = int(getattr(post, "score", 0) or 0)
    num_comments = int(getattr(post, "num_comments", 0) or 0)
    return score >= min_score or num_comments >= min_comments


# ── Ingester ───────────────────────────────────────────────────────────────


class RedditIngester:
    """:class:`~musahit.ingest.Ingester` for ``SourceKind.API`` (Reddit).

    Constructor dependencies (matching the html.py DI pattern):

    * ``client`` — a PRAW :class:`praw.Reddit` instance (or a duck-typed
      fake for tests). When ``None`` the ingester lazily builds one from
      ``settings``.
    * ``settings`` — credentials and threshold defaults; when ``None`` the
      ingester does NOT build a client and returns ``SKIPPED`` instead of
      crashing.
    * ``subreddits`` — defaults to the module-level :data:`SUBREDDITS`.
    * ``utcnow_epoch`` — clock injection for tests that need the 24-hour
      window to be deterministic.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        client: Any | None = None,
        settings: Settings | None = None,
        subreddits: tuple[SubredditConfig, ...] | None = None,
        min_score: int | None = None,
        min_comments: int | None = None,
        utcnow_epoch: float | None = None,
    ) -> None:
        self._conn = conn
        self._client = client
        self._settings = settings
        self._subreddits = subreddits if subreddits is not None else SUBREDDITS
        self._min_score = min_score
        self._min_comments = min_comments
        self._utcnow_epoch = utcnow_epoch

    async def fetch(self, source: Source) -> IngestResult:
        return await asyncio.to_thread(self._fetch_sync, source)

    # ── Sync core ───────────────────────────────────────────────────────

    def _fetch_sync(self, source: Source) -> IngestResult:
        log = _log.bind(source_id=source.id)

        client = self._client
        if client is None:
            client = self._build_client(log)
            if client is None:
                return IngestResult(
                    status=IngestStatus.SKIPPED,
                    error="Reddit credentials missing",
                )

        # Threshold resolution: explicit override > Settings > hard defaults.
        min_score, min_comments = self._resolve_thresholds()
        now_epoch = (
            self._utcnow_epoch
            if self._utcnow_epoch is not None
            else datetime.now().timestamp()
        )
        fetched_at = utcnow()
        before = self._row_count()

        try:
            for sub_config in self._subreddits:
                self._process_subreddit(
                    client=client,
                    source=source,
                    sub_config=sub_config,
                    fetched_at=fetched_at,
                    now_epoch=now_epoch,
                    min_score=min_score,
                    min_comments=min_comments,
                    log=log,
                )
        except prawcore.exceptions.ResponseException as exc:
            log.warning("reddit_response_error", error=str(exc))
            return IngestResult(
                status=IngestStatus.HTTP_ERROR,
                error=f"{type(exc).__name__}: {exc}",
            )
        except prawcore.exceptions.RequestException as exc:
            # prawcore wraps network-level failures (DNS, connect, read) here.
            log.warning("reddit_request_error", error=str(exc))
            return IngestResult(
                status=IngestStatus.TIMEOUT,
                error=f"{type(exc).__name__}: {exc}",
            )

        inserted = self._row_count() - before
        log.info("reddit_ok", inserted=inserted)
        return IngestResult(status=IngestStatus.OK, count=inserted)

    # ── Per-subreddit loop ──────────────────────────────────────────────

    def _process_subreddit(
        self,
        *,
        client: Any,
        source: Source,
        sub_config: SubredditConfig,
        fetched_at: datetime,
        now_epoch: float,
        min_score: int,
        min_comments: int,
        log: Any,
    ) -> None:
        subreddit = client.subreddit(sub_config.name)
        for post in subreddit.new(limit=sub_config.listing_limit):
            if not _passes_universal_filter(
                post,
                now_epoch=now_epoch,
                min_score=min_score,
                min_comments=min_comments,
            ):
                continue
            if not matches_subreddit_filter(
                sub_config,
                flair_text=getattr(post, "link_flair_text", None),
                title=getattr(post, "title", "") or "",
            ):
                continue
            try:
                self._persist_post(source, post, sub_config, fetched_at)
            except Exception as exc:
                # Per-post isolation: one broken post doesn't drop the rest.
                log.warning(
                    "reddit_post_persist_error",
                    subreddit=sub_config.name,
                    post_id=getattr(post, "id", None),
                    error=f"{type(exc).__name__}: {exc}",
                )

    # ── Persistence ─────────────────────────────────────────────────────

    def _persist_post(
        self,
        source: Source,
        post: Any,
        sub_config: SubredditConfig,
        fetched_at: datetime,
    ) -> None:
        permalink = getattr(post, "permalink", "") or ""
        full_url = _full_permalink(permalink)
        row_id = article_id(source.id, full_url)

        created_utc = float(getattr(post, "created_utc", 0.0) or 0.0)
        canonical_ts = (
            to_utc_naive(datetime.fromtimestamp(created_utc, tz=UTC))
            if created_utc > 0
            else None
        )

        is_self = bool(getattr(post, "is_self", False))
        external_url = None if is_self else getattr(post, "url", None)

        payload = {
            "title": getattr(post, "title", "") or "",
            "selftext": _truncate(getattr(post, "selftext", "") or "", SELFTEXT_TRUNC),
            "comments": _top_comments(post),
            "author": _author_name(post),
            "score": int(getattr(post, "score", 0) or 0),
            "num_comments": int(getattr(post, "num_comments", 0) or 0),
        }
        raw_content = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        ingester_metadata = {
            "subreddit": sub_config.name,
            "score": payload["score"],
            "num_comments": payload["num_comments"],
            "flair": getattr(post, "link_flair_text", None),
            "external_url": external_url,
        }

        self._conn.execute(
            """
            INSERT INTO raw_articles (
                id, source_id, url, fetched_at,
                raw_content, content_type, headers, fetch_status_code,
                feed_entry_id, canonical_timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO NOTHING
            """,
            [
                row_id,
                source.id,
                full_url,
                fetched_at,
                raw_content,
                "application/json",
                json.dumps(ingester_metadata, ensure_ascii=False),
                200,
                getattr(post, "id", None) or None,
                canonical_ts,
            ],
        )

    # ── Configuration plumbing ──────────────────────────────────────────

    def _resolve_thresholds(self) -> tuple[int, int]:
        if self._min_score is not None and self._min_comments is not None:
            return self._min_score, self._min_comments
        settings = self._settings
        if settings is not None:
            min_score = (
                self._min_score
                if self._min_score is not None
                else settings.reddit_min_score
            )
            min_comments = (
                self._min_comments
                if self._min_comments is not None
                else settings.reddit_min_comments
            )
            return min_score, min_comments
        # Hard fallbacks if no settings and no overrides (e.g., tests).
        return self._min_score or 50, self._min_comments or 25

    def _build_client(self, log: Any) -> Any | None:
        if praw is None:
            log.warning("reddit_praw_missing")
            return None
        settings = self._settings
        if (
            settings is None
            or not settings.reddit_client_id
            or not settings.reddit_client_secret
        ):
            log.warning("reddit_credentials_missing")
            return None
        return praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent or USER_AGENT,
        )

    def _row_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()
        return int(row[0]) if row else 0


__all__ = [
    "COMMENT_TRUNC",
    "RedditIngester",
    "SELFTEXT_TRUNC",
    "TOP_COMMENTS",
    "WINDOW_SECONDS",
]
