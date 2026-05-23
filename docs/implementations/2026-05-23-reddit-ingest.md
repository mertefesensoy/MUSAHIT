# Implementation: Reddit ingester · `musahit/ingest/reddit.py`

**Date** · 2026-05-23
**Author** · Claude Code (Mert Efe Şensoy directing)
**ADR refs** · ADR-003 · ADR-005 · ADR-006 · ADR-012 · ADR-014 · ADR-015

---

## ❯ Problem / Motivation

Build step 7 of 20 — the last ingester. Reddit is the only social source MÜŞAHİT
ingests in v0.1 (X was deferred per ADR-013 amendment 2; Telegram was excluded
per the operator). The Turkey-focused subreddits and the Turkey-flair traffic in
r/europe occasionally surface real news before mainstream outlets pick it up,
and surface operator-relevant *reaction* signal that no news feed captures.

Two implementation constraints differentiate Reddit from the previous ingesters:

1. **PRAW is synchronous.** The official Reddit API wrapper does not expose an
   async interface and the project shape doesn't justify swapping in `asyncpraw`
   (different dependency, different bugs, no real concurrency win at this volume).
   The whole fetch runs inside `asyncio.to_thread` so the Ingester Protocol's
   `async def fetch(source)` contract is satisfied without blocking the event
   loop.
2. **OAuth credentials live in `.env`.** ADR-003 spec'd this and
   `musahit/common/config.py` already exposes `reddit_client_id`,
   `reddit_client_secret`, `reddit_user_agent`. Missing credentials → ingester
   returns `IngestStatus.SKIPPED`, not a crash. The pipeline keeps running.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/ingest/reddit_subreddits.py` | New. `SubredditConfig` frozen dataclass + `SUBREDDITS` tuple (4 entries) + `matches_subreddit_filter()` helper. |
| `musahit/ingest/reddit.py` | New. `RedditIngester` implementing the `Ingester` Protocol; PRAW client built lazily from `Settings` or injected via constructor. |
| `musahit/common/time.py` | Added `tr_local_date()` (TR is UTC+3 year-round, no DST since 2016). |
| `musahit/ingest/resmi_gazete.py` | Refactored to import and use the shared `tr_local_date()`; deleted the local `_tr_today()` helper. |
| `tests/test_reddit.py` | 11 tests across 7 classes — covers success path, all three filter rules, link-vs-selftext shape, both prawcore exceptions, idempotence, and the missing-credentials SKIPPED path. |
| `tests/common/test_time.py` | 3 additional tests for `tr_local_date()`. |
| `memory/MEMORY.md` | Extended with the project-wide "enum expansion is an ADR amendment, never a silent code change" convention. |
| `docs/implementations/2026-05-23-reddit-ingest.md` | This document. |

No FILE-PROTECTED file (`sources.py`, `defcon.py`, `promotion.py`, any ADR)
was modified. `pyproject.toml` already declared `praw>=7.7`; no dependency
change was needed.

---

## ❯ Implementation Approach

### PRAW inside `asyncio.to_thread`

```python
async def fetch(self, source: Source) -> IngestResult:
    return await asyncio.to_thread(self._fetch_sync, source)

def _fetch_sync(self, source: Source) -> IngestResult:
    # client setup + per-subreddit loop + persistence + exception translation
```

`asyncio.to_thread` is the right primitive when:

- The blocking work is bounded in time (Reddit listings: ≤100 posts × 4 subs).
- We don't need concurrency *within* one fetch (one subreddit at a time is fine).
- The work is genuinely sync — wrapping doesn't pretend it's async.

If a future Reddit strategy needs concurrent per-subreddit fetches, the right
move is to push the `to_thread` boundary down to the per-subreddit level. The
current code already isolates `_process_subreddit` as a method, so the change
is local.

### Filter chain

Two layers, applied in order:

1. **Universal filter** (`_passes_universal_filter`) — ADR-003 spec:
   - `created_utc` must be within the last 24h relative to `now`.
   - AND `score >= min_score` OR `num_comments >= min_comments`.
   - Defaults come from `Settings` (`reddit_min_score=50`,
     `reddit_min_comments=25`); the constructor allows overrides for testing.
2. **Per-subreddit filter** (`matches_subreddit_filter` in
   `reddit_subreddits.py`) — currently only `r/europe` uses it:
   - `flair_filter=("Turkey", "Türkiye")` requires the post's
     `link_flair_text` to contain one of these (case-insensitive).
   - Turkey-specific subs (`r/Turkey`, `r/TurkeyJerky`, `r/AskTurkey`) have
     empty filters → pass everything.

Future per-subreddit tuning (e.g., title keywords for a broader subreddit) is
a single-line edit to `reddit_subreddits.py`.

### Persistence shape per qualifying post

| Column | Reddit value |
|---|---|
| `id` | `article_id("reddit_turkey", full_permalink_url)` |
| `source_id` | `"reddit_turkey"` |
| `url` | `f"https://www.reddit.com{post.permalink}"` |
| `fetched_at` | naive UTC (`utcnow()`); one value across all posts in a fetch |
| `raw_content` | UTF-8 JSON bytes — see "raw_content JSON shape" below |
| `content_type` | `"application/json"` (the raw_content is JSON, not the post page) |
| `headers` | JSON `{subreddit, score, num_comments, flair, external_url}` |
| `fetch_status_code` | `200` |
| `feed_entry_id` | `post.id` (e.g., `"1abc23"`) |
| `canonical_timestamp` | `to_utc_naive(datetime.fromtimestamp(post.created_utc, tz=UTC))` |

### raw_content JSON shape

```json
{
  "title": "<post title>",
  "selftext": "<truncated to 500 chars>",
  "comments": [
    {"author": "<username>", "body": "<truncated to 200 chars>", "score": <int>},
    ...up to 3 top-level comments...
  ],
  "author": "<username or null>",
  "score": <int>,
  "num_comments": <int>
}
```

ADR-006 specifies `articles.body` as plain TEXT. When the normalize stage
(step 9) processes a Reddit row, it will need to *flatten* this JSON into
plain text — concatenating title + selftext + comment bodies is the obvious
approach, but the exact format is the normalize stage's call. The ingester's
job is to preserve the post payload faithfully; flattening is per-source
work in normalize.

Per the build-plan tripwire on "raw_content JSON shape conflicts with the
normalize stage's expectations": this is acceptable because (a) the
normalize stage hasn't been implemented yet, (b) JSON in `raw_content` is
how PDF ingest works (the PDF bytes themselves are not text either), and
(c) `articles.body TEXT` is populated by the normalize stage *from*
`raw_content`, not directly mirroring it. The contract holds.

### Per-post failure isolation

Inside `_process_subreddit`, each post's persistence is wrapped in a
broad `try/except` that logs and skips. One malformed post (missing
attribute, JSON encoding failure, etc.) does not abort the rest of the
subreddit or the other subreddits.

prawcore exceptions, by contrast, bubble out of `_process_subreddit` to
the surrounding handler in `_fetch_sync` — that's the source-level
failure, not per-post:

| prawcore exception | Mapped status |
|---|---|
| `prawcore.exceptions.ResponseException` | `HTTP_ERROR` (auth, 403, 5xx) |
| `prawcore.exceptions.RequestException` | `TIMEOUT` (network, DNS, connect) |

This split matches PRAW's documented exception hierarchy — `Response*`
means we talked to Reddit and it said no; `Request*` means we didn't
reach Reddit at all.

### Missing credentials → SKIPPED

When `Settings.reddit_client_id` or `reddit_client_secret` is empty (the
default state for a freshly-cloned repo where the operator hasn't filled
`.env`), the ingester logs `reddit_credentials_missing` and returns
`IngestStatus.SKIPPED`. The pipeline keeps running; the briefing's
SİSTEM LOG footer surfaces the skip so the operator notices.

The PRAW import itself is also guarded — if `praw` is somehow missing
(import-time failure), the ingester returns SKIPPED rather than crashing
the pipeline. (In production `praw>=7.7` is a hard dependency so this
path is defensive.)

---

## ❯ Mathematical / Statistical Details

None. The thresholds are static integers from `Settings`. The 24-hour
window is a fixed subtraction.

---

## ❯ Design Decisions

**`asyncio.to_thread` over `asyncpraw`.**
`asyncpraw` is a separate library with its own quirks, version-pinning issues,
and a smaller community. PRAW is the canonical wrapper. Wrapping the
whole sync flow in `to_thread` costs us a thread per fetch — at one
ingest per night, that's negligible. The maintenance saving is real.

**`SubredditConfig` is a frozen dataclass, not just strings.**
`r/europe` already needed flair filtering at first cut. Treating each
subreddit as a config object instead of a bare name keeps per-source
tuning in one place from day one rather than retrofitting later.

**Constructor takes both `client` and `settings`.**
`client` is the explicit injection knob for tests. `settings` is the
production knob — the ingester lazily builds a PRAW client from it.
This lets tests pass a fake client with zero settings, and lets
production pass settings with no client. Forcing either-or would have
required separate ingester subclasses.

**Per-post failure isolation, not per-subreddit.**
A malformed post inside a healthy subreddit shouldn't drop the rest of
that subreddit's stream. The try/except wraps `_persist_post`, not the
whole subreddit loop. A subreddit-level prawcore failure DOES drop the
remaining subreddits (current behavior) because that signals a
credential or network problem that the next subreddit fetch will hit
too. If that turns out to be wrong, it's a one-line change inside
`_fetch_sync`.

**Synthetic URL is the full `https://www.reddit.com{permalink}`.**
PRAW's `permalink` is a path starting with `/r/...`. Prepending the host
is the well-known way to get a click-through URL the operator can paste
in a browser. The article_id helper (ADR-014) accepts any string, so
the choice between path-only and full URL is operator-ergonomics only;
full URL wins.

**`external_url` is in headers JSON, not a typed column.**
ADR-015 promoted two universal fields (`feed_entry_id`,
`canonical_timestamp`) to typed columns. `external_url` is Reddit-only
and would be NULL for every non-Reddit row; promoting it would be the
wrong precedent. The normalize stage reads `headers.external_url` on
the Reddit-specific path.

**Refactored `tr_local_date()` into `musahit.common.time`.**
The Resmî Gazete ingester (step 6) inlined the UTC+3 shift as a local
`_tr_today()` helper. Reddit needs the same shift conceptually (for
day-bucket queries the operator may add later); promoting the helper
keeps the offset in one place. A future Türkiye DST decision is then a
one-line edit, not a search-and-replace.

---

## ❯ Operator caveats

- **Reddit API rate limits.** OAuth-authenticated clients get ~100
  requests/minute. The ingester pulls at most 4 listings × 100 posts =
  400 listing pages per night, plus the per-post lazy attribute fetches
  (which PRAW caches). Well within limits in normal operation; one
  outage day where Reddit returns 5xx repeatedly could exhaust the
  client's retry budget — prawcore's `ResponseException` then surfaces
  as `HTTP_ERROR`.
- **Reddit API changes.** Reddit's API has churned in the past
  (notably the June 2023 changes that killed third-party apps). PRAW
  generally tracks these but a future cap or pricing change is the
  operator's signal to revisit. If PRAW versions diverge, the
  contractual surface this ingester depends on is small:
  `Reddit.subreddit(name).new(limit=N)` yielding objects with
  `id`, `title`, `selftext`, `created_utc`, `score`, `num_comments`,
  `author.name`, `permalink`, `is_self`, `url`, `link_flair_text`,
  `comments`.
- **r/europe flair labels.** The flair filter looks for `"Turkey"` or
  `"Türkiye"` (case-insensitive substring). If r/europe ever changes
  the flair label (e.g., to "TR" or "Turkish"), the operator updates
  `SUBREDDITS` in `reddit_subreddits.py`.
- **DEFCON 4 hard cap is downstream.** This ingester does NOT enforce
  the social_reddit ceiling from ADR-005. Reddit posts land in
  `raw_articles` like any other source; the score/promotion stage
  applies the ceiling when computing `clusters.ceiling_defcon`. If
  step 11+ ever forgets this, the briefing will surface social posts
  at higher DEFCON than it should — but the audit log will show the
  promotion decision plainly.
- **JSON in `raw_content`.** Unlike the RSS/HTML ingesters whose
  `raw_content` is the raw HTML, Reddit's `raw_content` is JSON. The
  normalize stage (step 9) must branch on `content_type` (or
  `source.kind`) to decide how to extract the body text.

---

## ❯ Verification

```powershell
# Lint
python -m ruff check .                # All checks passed!

# Step-7 tests
python -m pytest tests/test_reddit.py tests/common/test_time.py -v
# Expected: 11 + 9 = 20 passed (test_time has the pre-existing tests + 3 new)

# Full suite — no regressions
python -m pytest tests/ -q
# Expected: 219 passed, 1 skipped
```

Goal-criteria mapping:

| Criterion | Verification |
|---|---|
| (1) `tr_local_date()` added; resmi_gazete refactored; tests | `musahit/common/time.py`; `tests/common/test_time.py::TestTrLocalDate` |
| (2) `reddit.py` Protocol impl, PRAW + `asyncio.to_thread`, credentials from Settings, subreddits, filters, persistence shape, INSERT OR IGNORE | `musahit/ingest/reddit.py`; `TestSuccessfulFetch`, `TestFilters` |
| (3) `reddit_subreddits.py` with SUBREDDITS + override hooks | `musahit/ingest/reddit_subreddits.py` |
| (4) 8 test scenarios | All covered across 7 test classes in `tests/test_reddit.py` |
| (5) Zero network | All tests use `FakeRedditClient` / `RaisingRedditClient`; no PRAW auth call ever fires |
| (6) ruff clean + pytest zero | confirmed (219 passed, 1 skipped) |
| (7) FILE-PROTECTED untouched | sources.py, defcon.py, promotion.py, ADRs unchanged; pyproject.toml has praw≥7.7 from step 1 |
| (8) This document | ✓ |
| (9) MEMORY.md enum convention | `memory/MEMORY.md` extended |

No tripwires fired: PRAW authentication works through Settings (no schema
change needed); raw_content JSON does not contradict ADR-006 because
articles.body is populated by normalize from raw_content (not directly);
no closed-set enums were expanded; no schema additions; ADRs internally
consistent.

---

## ❯ Related Docs

- BOOTSTRAP.md — build step 7 of 20
- ADR-003 — source registry; Reddit ingest strategy (PRAW, threshold, subs)
- ADR-005 — bias promotion rules; DEFCON 4 hard cap for social_reddit band
- ADR-006 — storage; `raw_articles` schema this code writes to
- ADR-012 — failure isolation
- ADR-014 — article id formula
- ADR-015 — typed metadata columns
- `docs/implementations/2026-05-23-rss-ingest.md` — first ingester, established the DI pattern
- `docs/implementations/2026-05-23-html-ingest.md` — DI extended to selectors + sleep
- `docs/implementations/2026-05-23-resmi-gazete-ingest.md` — DI extended to parser injection
