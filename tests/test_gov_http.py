"""Tests for musahit.ingest.gov_http.

Covers the response shape, fake fetcher behavior, and the factory + lookup
helpers. The production fetcher (:class:`CurlCffiGovHttpFetcher`) is tested
at the behavioral surface that doesn't need a live curl_cffi session ·
deeper integration is exercised by the operator's spike scripts at
``scripts/triage/`` rather than the CI suite (rate limiting on real gov
sources is real, see ``goal``).
"""

from __future__ import annotations

import pytest

from musahit.ingest.gov_http import (
    DEFAULT_IMPERSONATE,
    DEFAULT_TIMEOUT_SECONDS,
    GOV_BOOTSTRAP_URL,
    GOV_REFERER,
    PDF_MAGIC_BYTES,
    SOURCE_IDS_USING_GOV_HTTP,
    CurlCffiGovHttpFetcher,
    FakeGovHttpFetcher,
    GovHttpFetcher,
    GovHttpResponse,
    make_gov_http_fetcher_for,
    referer_for,
)

# ── TestGovHttpResponse ───────────────────────────────────────────────────


class TestGovHttpResponse:
    def test_from_raw_lowercases_header_keys(self) -> None:
        resp = GovHttpResponse.from_raw(
            content=b"hello",
            status_code=200,
            headers={"Content-Type": "text/html", "ETag": "abc"},
        )
        assert resp.headers == {"content-type": "text/html", "etag": "abc"}

    def test_from_raw_handles_none_headers(self) -> None:
        resp = GovHttpResponse.from_raw(
            content=b"", status_code=200, headers=None
        )
        assert resp.headers == {}

    def test_content_coerced_to_bytes(self) -> None:
        # Some HTTP libs hand back bytearray or memoryview; we always
        # store immutable bytes so downstream persistence is safe.
        resp = GovHttpResponse.from_raw(
            content=bytearray(b"abc"), status_code=200
        )
        assert isinstance(resp.content, bytes)
        assert resp.content == b"abc"


# ── TestFakeGovHttpFetcher ────────────────────────────────────────────────


class TestFakeGovHttpFetcher:
    async def test_routes_table_lookup(self) -> None:
        f = FakeGovHttpFetcher(
            routes={
                "https://x.example/a": (b"alpha", 200, {"content-type": "text/html"})
            }
        )
        resp = await f.fetch("https://x.example/a")
        assert resp.status_code == 200
        assert resp.content == b"alpha"
        assert resp.headers["content-type"] == "text/html"

    async def test_unmapped_url_returns_404(self) -> None:
        f = FakeGovHttpFetcher(routes={})
        resp = await f.fetch("https://nope.example/")
        assert resp.status_code == 404
        # Body is non-empty so tests can distinguish from a 200-empty
        # response if they happen to mix the two.
        assert resp.content == b"not found"

    async def test_records_calls_with_referer(self) -> None:
        f = FakeGovHttpFetcher(routes={"u": (b"", 200, None)})
        await f.fetch("u", referer="https://ref.example/")
        await f.fetch("u")  # no referer
        assert f.calls == [
            ("u", "https://ref.example/"),
            ("u", None),
        ]

    async def test_bootstrap_records_in_separate_list_and_calls(self) -> None:
        f = FakeGovHttpFetcher(routes={"home": (b"<html/>", 200, None)})
        await f.bootstrap("home")
        await f.fetch("home", referer=None)
        # bootstrap_calls is the dedicated record; calls also contains it
        # so tests that just assert "this URL was visited" stay simple.
        assert f.bootstrap_calls == ["home"]
        assert f.calls == [("home", None), ("home", None)]

    async def test_close_sets_flag(self) -> None:
        f = FakeGovHttpFetcher()
        assert f.closed is False
        await f.close()
        assert f.closed is True


# ── TestModuleConfig ──────────────────────────────────────────────────────


class TestModuleConfig:
    def test_default_impersonate_is_firefox133(self) -> None:
        """The 2026-05-25 spike validated firefox133 specifically · changing
        the default is a roadmap decision, not a refactor."""
        assert DEFAULT_IMPERSONATE == "firefox133"

    def test_default_timeout_is_60_seconds(self) -> None:
        assert DEFAULT_TIMEOUT_SECONDS == 60.0

    def test_pdf_magic_bytes_constant(self) -> None:
        # ISO 32000 (PDF spec) requires '%PDF' in the first 4 bytes.
        # If this constant ever changes, every PDF magic check in the
        # ingest layer needs review.
        assert PDF_MAGIC_BYTES == b"%PDF"

    def test_every_gov_source_has_bootstrap_url(self) -> None:
        for sid in SOURCE_IDS_USING_GOV_HTTP:
            assert sid in GOV_BOOTSTRAP_URL, (
                f"{sid!r} is in SOURCE_IDS_USING_GOV_HTTP but missing "
                "from GOV_BOOTSTRAP_URL · the fetcher's session bootstrap "
                "step would have no URL to visit"
            )

    def test_every_gov_source_has_referer(self) -> None:
        for sid in SOURCE_IDS_USING_GOV_HTTP:
            assert sid in GOV_REFERER, (
                f"{sid!r} is in SOURCE_IDS_USING_GOV_HTTP but missing "
                "from GOV_REFERER · per-source Referer header is required"
            )

    def test_resmi_gazete_is_gov_http(self) -> None:
        # The PDF ingester depends on this · pin it explicitly.
        assert "resmi_gazete" in SOURCE_IDS_USING_GOV_HTTP

    def test_danistay_not_in_gov_http(self) -> None:
        # danistay was dropped 2026-05-25 (architecturally unreachable);
        # it must not appear in any gov_http config map.
        assert "danistay" not in SOURCE_IDS_USING_GOV_HTTP
        assert "danistay" not in GOV_BOOTSTRAP_URL
        assert "danistay" not in GOV_REFERER


# ── TestReferersAndFactory ────────────────────────────────────────────────


class TestReferersAndFactory:
    def test_referer_for_known_source(self) -> None:
        # Every gov source has a Referer (typically the homepage URL).
        assert referer_for("resmi_gazete") == "https://www.resmigazete.gov.tr/"

    def test_referer_for_unknown_source_returns_none(self) -> None:
        # Non-gov sources just don't have an entry · returning None is the
        # quiet path the ingesters handle (they pass referer=None to the
        # fetcher when there isn't one).
        assert referer_for("does_not_exist") is None
        # ap_tr is HTML but not gov-tagged; no Referer.
        assert referer_for("ap_tr") is None

    def test_factory_returns_curl_cffi_fetcher_for_known_id(self) -> None:
        fetcher = make_gov_http_fetcher_for("resmi_gazete")
        assert isinstance(fetcher, CurlCffiGovHttpFetcher)
        # The fetcher carries the bootstrap URL configured in
        # GOV_BOOTSTRAP_URL · ready to seed cookies on first fetch.
        assert (
            fetcher._configured_bootstrap_url
            == GOV_BOOTSTRAP_URL["resmi_gazete"]
        )

    def test_factory_raises_keyerror_for_unknown_id(self) -> None:
        with pytest.raises(KeyError, match="not registered"):
            make_gov_http_fetcher_for("does_not_exist")

    def test_factory_raises_for_dropped_source(self) -> None:
        # danistay was dropped · accidentally constructing a fetcher for
        # it should fail loudly rather than silently work.
        with pytest.raises(KeyError):
            make_gov_http_fetcher_for("danistay")


# ── TestCurlCffiGovHttpFetcher (behavioral surface, no real curl) ─────────


class TestCurlCffiGovHttpFetcherBehavior:
    """Tests that don't require a live curl_cffi session or network.

    The fetcher's lazy-bootstrap idempotence and the
    ``_bootstrapped_urls`` set are pure behavioral state and can be
    exercised by stubbing ``_sync_get`` to a no-op. Real HTTP behavior
    is validated by the operator's spike scripts."""

    async def test_bootstrap_idempotent(self, monkeypatch) -> None:
        fetcher = CurlCffiGovHttpFetcher(
            bootstrap_url="https://example.gov.tr/"
        )

        # Stub _sync_get so no real Session is constructed and no HTTP fires.
        calls: list[tuple[str, str | None]] = []

        def fake_sync_get(url: str, referer: str | None) -> GovHttpResponse:
            calls.append((url, referer))
            return GovHttpResponse(content=b"ok", status_code=200)

        monkeypatch.setattr(fetcher, "_sync_get", fake_sync_get)

        await fetcher.bootstrap("https://example.gov.tr/")
        await fetcher.bootstrap("https://example.gov.tr/")  # idempotent
        await fetcher.bootstrap("https://example.gov.tr/")  # idempotent

        # Only the FIRST call hits _sync_get; the second and third skip.
        assert calls == [("https://example.gov.tr/", None)]

    async def test_fetch_triggers_lazy_bootstrap_on_first_call(
        self, monkeypatch
    ) -> None:
        fetcher = CurlCffiGovHttpFetcher(
            bootstrap_url="https://example.gov.tr/"
        )

        calls: list[tuple[str, str | None]] = []

        def fake_sync_get(url: str, referer: str | None) -> GovHttpResponse:
            calls.append((url, referer))
            return GovHttpResponse(content=b"x", status_code=200)

        monkeypatch.setattr(fetcher, "_sync_get", fake_sync_get)

        await fetcher.fetch(
            "https://example.gov.tr/page", referer="https://example.gov.tr/"
        )

        # First call = bootstrap (homepage, no referer); second = real fetch.
        assert calls == [
            ("https://example.gov.tr/", None),
            ("https://example.gov.tr/page", "https://example.gov.tr/"),
        ]

    async def test_fetch_without_bootstrap_url_skips_bootstrap(
        self, monkeypatch
    ) -> None:
        fetcher = CurlCffiGovHttpFetcher(bootstrap_url=None)

        calls: list[tuple[str, str | None]] = []

        def fake_sync_get(url: str, referer: str | None) -> GovHttpResponse:
            calls.append((url, referer))
            return GovHttpResponse(content=b"y", status_code=200)

        monkeypatch.setattr(fetcher, "_sync_get", fake_sync_get)

        await fetcher.fetch("https://nogov.example/page")
        assert calls == [("https://nogov.example/page", None)]

    async def test_close_is_safe_when_session_never_constructed(
        self,
    ) -> None:
        fetcher = CurlCffiGovHttpFetcher()
        # close() before any fetch should not raise · _session stays None.
        await fetcher.close()
        assert fetcher._session is None


# ── TestProtocolConformance ───────────────────────────────────────────────


class TestResolveCaBundle:
    """Direct tests for ``_resolve_ca_bundle`` — the method whose certifi
    default caused the 2026-05-26 production SSL failures."""

    def test_default_returns_true(self) -> None:
        fetcher = CurlCffiGovHttpFetcher()
        assert fetcher._resolve_ca_bundle() is True

    def test_explicit_path_is_honoured(self) -> None:
        fetcher = CurlCffiGovHttpFetcher(ca_bundle="/custom/ca.pem")
        assert fetcher._resolve_ca_bundle() == "/custom/ca.pem"

    def test_false_disables_verification(self) -> None:
        fetcher = CurlCffiGovHttpFetcher(ca_bundle=False)
        assert fetcher._resolve_ca_bundle() is False


class TestProtocolConformance:
    def test_fake_implements_protocol(self) -> None:
        assert isinstance(FakeGovHttpFetcher(), GovHttpFetcher)

    def test_curl_cffi_implements_protocol(self) -> None:
        assert isinstance(CurlCffiGovHttpFetcher(), GovHttpFetcher)
