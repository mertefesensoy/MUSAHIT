"""curl_cffi-backed HTTP fetcher for government sources.

The 2026-05-25 spike (`scripts/triage/spike_curl_cffi.py`) established that
every ``*.gov.tr`` origin we ingest sits behind an Akamai-style CDN that
fingerprints the TLS ClientHello and returns 403 / 503 / connection-reset
to any client whose handshake doesn't match a real browser. Standard
``httpx`` fails every time; `curl_cffi` with ``firefox133`` impersonation
clears the gate. A follow-up spike (`spike_session_pdf.py`) found that
direct PDF fetches also fail — the Resmî Gazete PDF endpoint requires a
prior visit to the homepage (to seed session cookies) plus a Referer
header. The same pattern is expected to apply to the other ``*.gov.tr``
HTML sources whose listing pages link into deeper article URLs.

This module exposes:

* :class:`GovHttpResponse` — minimal ``content`` / ``status_code`` /
  ``headers`` shape compatible with :class:`httpx.Response` so existing
  ingester helpers (``_persist_article``, ``_process_pdf``) consume both
  without changes.
* :class:`GovHttpFetcher` — :class:`typing.Protocol` with ``bootstrap``
  (visit a URL, discard the body, accumulate cookies) and ``fetch``
  (perform a real request with optional Referer).
* :class:`CurlCffiGovHttpFetcher` — production implementation wrapping a
  :class:`curl_cffi.requests.Session`. Synchronous Session methods are
  wrapped in :func:`asyncio.to_thread` so the rest of the pipeline stays
  async-clean.
* :class:`FakeGovHttpFetcher` — in-memory route-table fetcher for tests;
  records every call's ``(url, referer)`` pair so suites can assert
  bootstrap ordering and Referer values without touching the network.
* :data:`SOURCE_IDS_USING_GOV_HTTP` — frozenset of source ids that MUST
  route through ``gov_http`` rather than ``httpx``. Tested in the spike
  or sharing the same ``*.gov.tr`` TLS-fingerprint pattern.
* :data:`GOV_BOOTSTRAP_URL` / :data:`GOV_REFERER` — per-source maps that
  encode the bootstrap visit and Referer header values. Both are derived
  from the source's ``url`` field by default (the source IS its own
  bootstrap), but the maps let us override per-source when the homepage
  URL differs from the listing/content URL.

The :func:`make_gov_http_fetcher_for` factory builds a production fetcher
preconfigured for a given source. Tests bypass this by injecting a
:class:`FakeGovHttpFetcher` into the ingester constructor instead.

Why a separate module rather than extending ``httpx``: curl_cffi's
``Session`` API is sync-only and uses keyword arguments (``impersonate``,
``verify``) that don't map cleanly onto ``httpx.AsyncClient``. Keeping the
adapter focused here avoids leaking curl_cffi types through the ingester
layer and lets non-gov ingesters keep their existing httpx path
unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from musahit.common.logging import get_logger

_log = get_logger("musahit.ingest.gov_http")

# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_IMPERSONATE: str = "firefox133"
"""Default curl_cffi browser fingerprint · validated against gov sources
in the 2026-05-25 spike. Listed in curl_cffi's BrowserType enum; bumping
to a newer fingerprint is safe if a future curl_cffi release ships it."""

DEFAULT_TIMEOUT_SECONDS: float = 60.0
"""Generous default · the Resmî Gazete PDF can run 15-20 MB and benefits
from up to ~180s on a slow link, but 60s is a sane per-call ceiling for
HTML listings and small article pages. Callers (e.g. the PDF ingester)
override per-request when they need longer."""

PDF_MAGIC_BYTES: bytes = b"%PDF"
"""ISO 32000 magic number · every well-formed PDF starts with ``%PDF`` in
the first 4 bytes. Used by :class:`musahit.ingest.resmi_gazete.
ResmiGazeteIngester._process_pdf` to reject HTML / error pages returned
with status 200 (the Akamai CDN sometimes serves a JS challenge page
under content-type application/pdf when the session is dirty)."""


# ── Per-source config maps ──────────────────────────────────────────────────

SOURCE_IDS_USING_GOV_HTTP: frozenset[str] = frozenset(
    {
        # PDF ingester (resmi_gazete) — main + Mükerrer URLs.
        "resmi_gazete",
        # HTML ingesters — TLS-fingerprinted Akamai-fronted *.gov.tr origins.
        # Validated by the 2026-05-25 curl_cffi spike except `tbmm` which
        # shares the *.gov.tr pattern; included pre-emptively so the next
        # smoke run exercises it.
        "anayasa_mahkemesi",
        "cumhurbaskanligi",
        "yargitay",
        "tbmm",
    }
)

GOV_BOOTSTRAP_URL: dict[str, str] = {
    # Source id → URL to visit before any content fetch. Most are the
    # source's own homepage; the PDF source needs the homepage explicitly
    # because the daily-PDF URL is on a different path.
    "resmi_gazete": "https://www.resmigazete.gov.tr/",
    "anayasa_mahkemesi": "https://www.anayasa.gov.tr/",
    "cumhurbaskanligi": "https://www.tccb.gov.tr/",
    "yargitay": "https://www.yargitay.gov.tr/",
    "tbmm": "https://www.tbmm.gov.tr/",
}

GOV_REFERER: dict[str, str] = {
    # Source id → Referer header value to send with content fetches.
    # Same URL as the bootstrap by default; kept as a separate map in
    # case a future source needs a different referer (e.g. the listing
    # page URL distinct from the homepage).
    "resmi_gazete": "https://www.resmigazete.gov.tr/",
    "anayasa_mahkemesi": "https://www.anayasa.gov.tr/",
    "cumhurbaskanligi": "https://www.tccb.gov.tr/",
    "yargitay": "https://www.yargitay.gov.tr/",
    "tbmm": "https://www.tbmm.gov.tr/",
}


# ── Response shape ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GovHttpResponse:
    """Minimal httpx.Response-compatible shape.

    Implements the ``.content`` / ``.status_code`` / ``.headers`` surface
    that ``_persist_article`` and ``_process_pdf`` use. Header keys are
    lowercased on construction so the consumers' lowercase ``.get()``
    calls hit consistently regardless of the upstream casing (curl_cffi
    preserves server casing; httpx normalises). Headers are stored as a
    plain ``dict[str, str]`` rather than a CaseInsensitiveDict because
    the consumers only ever do lowercase lookups.
    """

    content: bytes
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_raw(
        cls,
        content: bytes,
        status_code: int,
        headers: dict[str, str] | Any | None = None,
    ) -> GovHttpResponse:
        """Construct with lowercased header keys (the consumers' convention)."""
        if headers is None:
            normalised: dict[str, str] = {}
        else:
            normalised = {str(k).lower(): str(v) for k, v in dict(headers).items()}
        return cls(content=bytes(content), status_code=int(status_code), headers=normalised)


# ── Protocol + production + fake ────────────────────────────────────────────


@runtime_checkable
class GovHttpFetcher(Protocol):
    """Shape the gov-source ingesters depend on.

    ``bootstrap`` is the cookie-seeding visit · idempotent, no observable
    response (we don't return it). ``fetch`` is the real content request,
    returns a :class:`GovHttpResponse`. ``close`` releases any underlying
    transport (the curl_cffi Session). All three are async so test fakes
    can be ``await``-ed in the same code path as the production fetcher.
    """

    async def bootstrap(self, url: str) -> None: ...

    async def fetch(
        self, url: str, *, referer: str | None = None
    ) -> GovHttpResponse: ...

    async def close(self) -> None: ...


class CurlCffiGovHttpFetcher:
    """Production fetcher · wraps :class:`curl_cffi.requests.Session`.

    The Session is constructed lazily on first call so test suites that
    don't exercise gov sources can keep this module importable without a
    curl_cffi install on PATH. ``bootstrap`` is idempotent: repeated
    calls with the same URL only fire one HTTP request (the second sees
    ``_bootstrapped_urls`` and returns immediately). Each call wraps the
    blocking ``Session.get`` in :func:`asyncio.to_thread`.

    On Windows the system trust store can lag on freshly-renewed gov.tr
    certificates. Passing ``ca_bundle`` (defaults to ``certifi.where()``)
    routes verification through the certifi bundle, which tracks Mozilla
    NSS root updates and ships current with the package. Set
    ``verify=False`` (anti-pattern) via constructor only if absolutely
    necessary — currently the certifi bundle is enough for every probed
    source.
    """

    def __init__(
        self,
        impersonate: str = DEFAULT_IMPERSONATE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        ca_bundle: str | bool | None = None,
        bootstrap_url: str | None = None,
    ) -> None:
        self._impersonate = impersonate
        self._timeout_seconds = timeout_seconds
        # ca_bundle: None → certifi default (resolved lazily on first use
        # so importing this module doesn't import certifi for callers who
        # never construct a fetcher). True → curl_cffi's default. False →
        # disable verification (testing only). str → explicit bundle path.
        self._ca_bundle = ca_bundle
        self._configured_bootstrap_url = bootstrap_url
        self._session: Any | None = None
        self._bootstrapped_urls: set[str] = set()

    def _ensure_session(self) -> Any:
        """Lazy-construct the curl_cffi Session on first use."""
        if self._session is not None:
            return self._session
        # Lazy import keeps the module importable without curl_cffi
        # installed (e.g. CI matrix where gov-source tests are skipped).
        from curl_cffi import requests as cc_requests

        self._session = cc_requests.Session()
        return self._session

    def _resolve_ca_bundle(self) -> str | bool:
        """Return the verify argument to pass to curl_cffi."""
        if self._ca_bundle is None:
            import certifi

            return certifi.where()
        return self._ca_bundle

    async def bootstrap(self, url: str) -> None:
        """Visit ``url`` once to seed session cookies. Idempotent."""
        if url in self._bootstrapped_urls:
            return
        log = _log.bind(url=url, phase="bootstrap")
        log.info("gov_http_bootstrap")
        await asyncio.to_thread(self._sync_get, url, None)
        self._bootstrapped_urls.add(url)

    async def fetch(
        self, url: str, *, referer: str | None = None
    ) -> GovHttpResponse:
        """Fetch ``url`` with optional Referer; returns the response."""
        # If a bootstrap URL was configured at construction and hasn't
        # been visited yet, do it now so the cookies are ready.
        if (
            self._configured_bootstrap_url is not None
            and self._configured_bootstrap_url not in self._bootstrapped_urls
        ):
            await self.bootstrap(self._configured_bootstrap_url)
        return await asyncio.to_thread(self._sync_get, url, referer)

    async def close(self) -> None:
        if self._session is None:
            return
        # curl_cffi Session.close is sync; wrap for symmetry.
        try:
            await asyncio.to_thread(self._session.close)
        except Exception as exc:  # pragma: no cover · defensive
            _log.warning("gov_http_close_failed", error=str(exc))
        self._session = None

    def _sync_get(self, url: str, referer: str | None) -> GovHttpResponse:
        """Synchronous Session.get wrapped by ``asyncio.to_thread``."""
        session = self._ensure_session()
        kwargs: dict[str, Any] = {
            "impersonate": self._impersonate,
            "timeout": self._timeout_seconds,
            "verify": self._resolve_ca_bundle(),
        }
        if referer is not None:
            kwargs["headers"] = {"Referer": referer}
        response = session.get(url, **kwargs)
        return GovHttpResponse.from_raw(
            content=response.content,
            status_code=response.status_code,
            headers=response.headers,
        )


@dataclass
class FakeGovHttpFetcher:
    """In-memory fetcher for tests.

    Construct with a route table mapping URL → (content, status_code,
    headers) tuples. Every ``fetch`` / ``bootstrap`` call is recorded
    in :attr:`calls` so tests can assert ordering, bootstrap-then-fetch,
    and Referer values. ``bootstrap`` calls record ``(url, None)`` and
    drop the response body; ``fetch`` calls record ``(url, referer)``
    and return the response.

    Missing URLs return a 404 :class:`GovHttpResponse` rather than
    raising, matching the production fetcher's behaviour where a 404
    becomes an upstream-decided ``IngestResult`` rather than an
    exception.
    """

    routes: dict[str, tuple[bytes, int, dict[str, str] | None]] = field(
        default_factory=dict
    )
    calls: list[tuple[str, str | None]] = field(default_factory=list)
    bootstrap_calls: list[str] = field(default_factory=list)
    closed: bool = False

    async def bootstrap(self, url: str) -> None:
        self.bootstrap_calls.append(url)
        # Bootstrap visits are also recorded in calls for tests that
        # assert "the same URL is hit twice" sort of semantics.
        self.calls.append((url, None))

    async def fetch(
        self, url: str, *, referer: str | None = None
    ) -> GovHttpResponse:
        self.calls.append((url, referer))
        if url in self.routes:
            content, status, headers = self.routes[url]
            return GovHttpResponse.from_raw(
                content=content,
                status_code=status,
                headers=headers or {"content-type": "application/octet-stream"},
            )
        return GovHttpResponse.from_raw(
            content=b"not found",
            status_code=404,
            headers={"content-type": "text/plain"},
        )

    async def close(self) -> None:
        self.closed = True


# ── Factory helpers ─────────────────────────────────────────────────────────


def make_gov_http_fetcher_for(source_id: str) -> CurlCffiGovHttpFetcher:
    """Construct a production fetcher pre-configured for ``source_id``.

    Looks up :data:`GOV_BOOTSTRAP_URL` so the first ``fetch`` triggers
    the cookie-seeding visit automatically. Callers that need different
    settings (e.g. a non-default impersonate string, a custom CA bundle)
    should construct :class:`CurlCffiGovHttpFetcher` directly.

    Raises ``KeyError`` if ``source_id`` is not in
    :data:`SOURCE_IDS_USING_GOV_HTTP` so a typo in the caller surfaces
    immediately rather than silently bypassing the gov path.
    """
    if source_id not in SOURCE_IDS_USING_GOV_HTTP:
        raise KeyError(
            f"source_id={source_id!r} is not registered for gov_http use; "
            f"add it to SOURCE_IDS_USING_GOV_HTTP + GOV_BOOTSTRAP_URL + "
            f"GOV_REFERER before constructing a fetcher for it"
        )
    return CurlCffiGovHttpFetcher(bootstrap_url=GOV_BOOTSTRAP_URL.get(source_id))


def referer_for(source_id: str) -> str | None:
    """Return the Referer header value for ``source_id``; ``None`` if unset."""
    return GOV_REFERER.get(source_id)


__all__ = [
    "DEFAULT_IMPERSONATE",
    "DEFAULT_TIMEOUT_SECONDS",
    "GOV_BOOTSTRAP_URL",
    "GOV_REFERER",
    "PDF_MAGIC_BYTES",
    "SOURCE_IDS_USING_GOV_HTTP",
    "CurlCffiGovHttpFetcher",
    "FakeGovHttpFetcher",
    "GovHttpFetcher",
    "GovHttpResponse",
    "make_gov_http_fetcher_for",
    "referer_for",
]
