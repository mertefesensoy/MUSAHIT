"""Tests for musahit.ingest.reddit — PRAW-backed Reddit ingester.

PRAW is a sync library wrapped in :func:`asyncio.to_thread`; the tests
inject a fake PRAW client through the ingester's constructor so no
network call and no PRAW authentication ever happens. The fake mirrors
the PRAW attributes the ingester actually reads — anything else on a
real :class:`praw.models.Submission` is irrelevant for our coverage.
"""

from __future__ import annotations

import json
import types
from collections.abc import Generator, Iterable
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import prawcore
import pytest

from musahit.common.ids import article_id
from musahit.common.migrations import init_db
from musahit.common.types import IngestStatus
from musahit.ingest.reddit import RedditIngester
from musahit.ingest.reddit_subreddits import SubredditConfig
from musahit.ingest.sources import get_source, seed_sources

REDDIT_SOURCE_ID = "reddit_turkey"
TEST_SUBREDDITS: tuple[SubredditConfig, ...] = (
    SubredditConfig(name="Turkey"),
    SubredditConfig(name="europe", flair_filter=("Turkey",)),
)
# A timestamp the test treats as "now" so the 24-h window is deterministic.
NOW_EPOCH: float = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC).timestamp()


# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeComment:
    def __init__(self, body: str, author: str = "redditor", score: int = 1) -> None:
        self.body = body
        self.author = types.SimpleNamespace(name=author)
        self.score = score


class FakePost:
    """Duck-types :class:`praw.models.Submission` for the attributes we read."""

    def __init__(
        self,
        *,
        id: str,
        title: str,
        created_utc: float,
        score: int = 0,
        num_comments: int = 0,
        selftext: str = "",
        is_self: bool = True,
        url: str = "",
        link_flair_text: str | None = None,
        author: str | None = "redditor",
        permalink: str | None = None,
        comments: list[FakeComment] | None = None,
    ) -> None:
        self.id = id
        self.title = title
        self.created_utc = created_utc
        self.score = score
        self.num_comments = num_comments
        self.selftext = selftext
        self.is_self = is_self
        self.url = url or f"https://www.reddit.com/r/Turkey/comments/{id}/"
        self.link_flair_text = link_flair_text
        self.author = types.SimpleNamespace(name=author) if author else None
        self.permalink = permalink or f"/r/Turkey/comments/{id}/test-title/"
        self.comments = comments or []


class FakeSubreddit:
    def __init__(self, posts: list[FakePost]) -> None:
        self._posts = posts

    def new(self, limit: int | None = None) -> Iterable[FakePost]:
        if limit is None:
            return iter(self._posts)
        return iter(self._posts[:limit])


class FakeRedditClient:
    def __init__(self, posts_by_subreddit: dict[str, list[FakePost]]) -> None:
        self._posts_by_subreddit = posts_by_subreddit

    def subreddit(self, name: str) -> FakeSubreddit:
        return FakeSubreddit(self._posts_by_subreddit.get(name, []))


class RaisingRedditClient:
    """Client that raises a given exception when ``.subreddit(...)`` is called."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def subreddit(self, _name: str) -> FakeSubreddit:
        raise self._exc


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture()
def ingest_db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "test.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    try:
        yield conn
    finally:
        conn.close()


def _reddit_source() -> object:
    return get_source(REDDIT_SOURCE_ID)


def _qualifying_selfpost(id_: str = "p1") -> FakePost:
    """Convenience: a self-post that passes the universal filter."""
    return FakePost(
        id=id_,
        title="Selfpost title",
        created_utc=NOW_EPOCH - 3600,  # 1h ago
        score=100,
        num_comments=42,
        selftext="x" * 800,  # exercises truncation
        is_self=True,
        comments=[FakeComment("first comment body" * 30) for _ in range(5)],
    )


def _qualifying_linkpost(id_: str = "p2") -> FakePost:
    return FakePost(
        id=id_,
        title="External article",
        created_utc=NOW_EPOCH - 7200,
        score=75,
        num_comments=10,
        is_self=False,
        url="https://news.example.com/story",
    )


def _below_threshold_post() -> FakePost:
    return FakePost(
        id="weak",
        title="Low engagement",
        created_utc=NOW_EPOCH - 600,
        score=5,
        num_comments=2,
        is_self=True,
    )


def _old_post() -> FakePost:
    # 2 days old → outside the 24h window.
    return FakePost(
        id="old",
        title="Stale",
        created_utc=NOW_EPOCH - (48 * 60 * 60),
        score=999,
        num_comments=999,
        is_self=True,
    )


def _ingester(
    db: duckdb.DuckDBPyConnection,
    client: object,
) -> RedditIngester:
    return RedditIngester(
        conn=db,
        client=client,
        subreddits=TEST_SUBREDDITS,
        min_score=50,
        min_comments=25,
        utcnow_epoch=NOW_EPOCH,
    )


# ── TestSuccessfulFetch ────────────────────────────────────────────────────


class TestSuccessfulFetch:
    async def test_canned_posts_persisted_with_expected_columns(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _reddit_source()
        client = FakeRedditClient(
            {
                "Turkey": [_qualifying_selfpost("a1"), _qualifying_linkpost("a2")],
                "europe": [],
            }
        )

        result = await _ingester(ingest_db, client).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 2
        rows = ingest_db.execute(
            """
            SELECT url, feed_entry_id, canonical_timestamp, headers, raw_content
            FROM raw_articles ORDER BY url
            """
        ).fetchall()
        assert len(rows) == 2
        for url, feed_entry_id, canonical_ts, headers_json, raw in rows:
            assert url.startswith("https://www.reddit.com/r/Turkey/comments/")
            assert feed_entry_id in {"a1", "a2"}
            assert isinstance(canonical_ts, datetime)
            assert canonical_ts.tzinfo is None
            headers = json.loads(headers_json)
            assert headers["subreddit"] == "Turkey"
            assert headers["score"] in {75, 100}
            # raw_content is JSON bytes carrying the post payload.
            payload = json.loads(bytes(raw).decode("utf-8"))
            assert payload["title"] in {"Selfpost title", "External article"}

    async def test_article_id_uses_full_permalink(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _reddit_source()
        post = _qualifying_selfpost("xyz")
        client = FakeRedditClient({"Turkey": [post], "europe": []})

        await _ingester(ingest_db, client).fetch(source)

        ids = {
            r[0] for r in ingest_db.execute("SELECT id FROM raw_articles").fetchall()
        }
        expected_url = f"https://www.reddit.com{post.permalink}"
        assert ids == {article_id(REDDIT_SOURCE_ID, expected_url)}


# ── TestFilters ────────────────────────────────────────────────────────────


class TestFilters:
    async def test_below_threshold_post_is_filtered(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _reddit_source()
        client = FakeRedditClient(
            {"Turkey": [_below_threshold_post()], "europe": []}
        )

        result = await _ingester(ingest_db, client).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 0
        assert _row_count(ingest_db) == 0

    async def test_post_older_than_24h_filtered(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _reddit_source()
        client = FakeRedditClient({"Turkey": [_old_post()], "europe": []})

        result = await _ingester(ingest_db, client).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 0

    async def test_europe_flair_filter_excludes_non_turkey_flair(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _reddit_source()
        with_flair = FakePost(
            id="eu_tr",
            title="Turkey news in r/europe",
            created_utc=NOW_EPOCH - 1000,
            score=200,
            num_comments=50,
            is_self=True,
            link_flair_text="Turkey",
        )
        without_flair = FakePost(
            id="eu_fr",
            title="French news in r/europe",
            created_utc=NOW_EPOCH - 1000,
            score=200,
            num_comments=50,
            is_self=True,
            link_flair_text="France",
        )
        client = FakeRedditClient(
            {"Turkey": [], "europe": [with_flair, without_flair]}
        )

        result = await _ingester(ingest_db, client).fetch(source)

        assert result.status is IngestStatus.OK
        assert result.count == 1
        rows = ingest_db.execute(
            "SELECT feed_entry_id FROM raw_articles"
        ).fetchall()
        assert {r[0] for r in rows} == {"eu_tr"}


# ── TestExternalLink ───────────────────────────────────────────────────────


class TestExternalLink:
    async def test_link_post_captures_external_url_in_headers(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _reddit_source()
        client = FakeRedditClient(
            {"Turkey": [_qualifying_linkpost("link1")], "europe": []}
        )

        await _ingester(ingest_db, client).fetch(source)

        row = ingest_db.execute(
            "SELECT headers FROM raw_articles WHERE feed_entry_id = 'link1'"
        ).fetchone()
        headers = json.loads(row[0])
        assert headers["external_url"] == "https://news.example.com/story"

    async def test_selftext_post_has_null_external_url(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _reddit_source()
        client = FakeRedditClient(
            {"Turkey": [_qualifying_selfpost("self1")], "europe": []}
        )

        await _ingester(ingest_db, client).fetch(source)

        row = ingest_db.execute(
            "SELECT headers FROM raw_articles WHERE feed_entry_id = 'self1'"
        ).fetchone()
        headers = json.loads(row[0])
        assert headers["external_url"] is None


# ── TestPrawcoreErrors ─────────────────────────────────────────────────────


class TestPrawcoreErrors:
    async def test_response_exception_returns_http_error(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # ResponseException requires a Response-like object with status_code.
        fake_response = types.SimpleNamespace(
            status_code=403, json=lambda: {"message": "forbidden"}
        )
        exc = prawcore.exceptions.ResponseException(fake_response)
        client = RaisingRedditClient(exc)

        result = await _ingester(ingest_db, client).fetch(_reddit_source())

        assert result.status is IngestStatus.HTTP_ERROR
        assert result.count == 0
        assert _row_count(ingest_db) == 0

    async def test_request_exception_returns_timeout(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # prawcore.RequestException wraps a requests.RequestException.
        # Constructing it directly with a stand-in works for our test path.
        original = RuntimeError("simulated network drop")
        request_args: tuple = ()
        request_kwargs: dict = {}
        exc = prawcore.exceptions.RequestException(
            original, request_args, request_kwargs
        )
        client = RaisingRedditClient(exc)

        result = await _ingester(ingest_db, client).fetch(_reddit_source())

        assert result.status is IngestStatus.TIMEOUT
        assert result.count == 0


# ── TestIdempotence ────────────────────────────────────────────────────────


class TestIdempotence:
    async def test_second_fetch_writes_no_duplicates(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        source = _reddit_source()
        # Build two fresh clients each iterating from the start.
        post = _qualifying_selfpost("rerun")
        ingester = _ingester(
            ingest_db, FakeRedditClient({"Turkey": [post], "europe": []})
        )
        first = await ingester.fetch(source)

        ingester2 = _ingester(
            ingest_db, FakeRedditClient({"Turkey": [post], "europe": []})
        )
        second = await ingester2.fetch(source)

        assert first.count == 1
        assert second.status is IngestStatus.OK
        assert second.count == 0
        assert _row_count(ingest_db) == 1


# ── TestMissingCredentials ─────────────────────────────────────────────────


class TestMissingCredentials:
    async def test_no_client_no_settings_returns_skipped(
        self, ingest_db: duckdb.DuckDBPyConnection
    ) -> None:
        # No client injected, no Settings — production-safe SKIPPED path.
        ingester = RedditIngester(
            conn=ingest_db, client=None, settings=None, subreddits=TEST_SUBREDDITS
        )
        result = await ingester.fetch(_reddit_source())
        assert result.status is IngestStatus.SKIPPED


# ── Helpers ────────────────────────────────────────────────────────────────


def _row_count(conn: duckdb.DuckDBPyConnection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM raw_articles").fetchone()
    return int(row[0]) if row else 0
