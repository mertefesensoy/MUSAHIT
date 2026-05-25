"""Tests for musahit.ingest.sources — FILE-PROTECTED source registry."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from musahit.common.migrations import init_db
from musahit.common.types import Band, Fragility, SourceKind, Tier
from musahit.ingest.sources import (
    SOURCES,
    SOURCES_BY_ID,
    Source,
    get_source,
    get_sources_by_band,
    get_sources_by_tier,
    seed_sources,
)

# Sources whose RSS feed URL could not be verified at scaffold time.
# HTML-kind sources are exempt (they are not RSS feeds).
EXPECTED_PENDING_URL_IDS: frozenset[str] = frozenset(
    {"anadolu", "t24", "medyascope", "dw_tr", "voa_tr", "reuters_tr", "kap"}
)


# ── TestSourceDataclass ────────────────────────────────────────────────────


class TestSourceDataclass:
    def test_frozen_raises_on_mutation(self) -> None:
        s = get_source("bianet")
        with pytest.raises((AttributeError, TypeError)):
            s.id = "modified"  # type: ignore[misc]

    def test_band_is_enum_not_string(self) -> None:
        s = get_source("bianet")
        assert isinstance(s.band, Band)

    def test_tier_is_enum_not_string(self) -> None:
        s = get_source("bianet")
        assert isinstance(s.tier, Tier)

    def test_kind_is_enum_not_string(self) -> None:
        s = get_source("bianet")
        assert isinstance(s.kind, SourceKind)

    def test_fragility_is_enum_not_string(self) -> None:
        s = get_source("bianet")
        assert isinstance(s.fragility, Fragility)

    def test_all_nine_fields_present(self) -> None:
        s = get_source("trt_haber")
        assert s.id == "trt_haber"
        assert s.display_name
        assert s.band
        assert s.tier
        assert s.kind
        assert s.url
        assert s.rate_limit_seconds > 0
        assert s.fragility


# ── TestAllSources ─────────────────────────────────────────────────────────


class TestAllSources:
    def test_total_count(self) -> None:
        # 36 (was 37 before danistay dropped 2026-05-25 · see sources.py).
        assert len(SOURCES) == 36

    def test_tier_counts(self) -> None:
        news = [s for s in SOURCES if s.tier is Tier.NEWS]
        markets = [s for s in SOURCES if s.tier is Tier.MARKETS]
        gov = [s for s in SOURCES if s.tier is Tier.GOV]
        social = [s for s in SOURCES if s.tier is Tier.SOCIAL]
        assert len(news) == 24
        assert len(markets) == 6
        # GOV: 5 (was 6 before danistay dropped · see sources.py).
        assert len(gov) == 5
        assert len(social) == 1

    def test_is_tuple(self) -> None:
        assert isinstance(SOURCES, tuple)

    def test_no_duplicate_ids(self) -> None:
        ids = [s.id for s in SOURCES]
        assert len(ids) == len(set(ids))

    def test_sources_by_id_keys_match(self) -> None:
        assert set(SOURCES_BY_ID.keys()) == {s.id for s in SOURCES}

    def test_all_ids_are_lowercase_alphanum_underscores(self) -> None:
        for s in SOURCES:
            assert s.id.replace("_", "").isalnum(), f"Bad ID: {s.id!r}"
            assert s.id == s.id.lower(), f"Non-lowercase ID: {s.id!r}"

    def test_all_urls_non_empty(self) -> None:
        for s in SOURCES:
            assert s.url, f"Empty URL for {s.id!r}"

    def test_all_rate_limits_positive(self) -> None:
        for s in SOURCES:
            assert s.rate_limit_seconds > 0, f"Bad rate limit for {s.id!r}"


# ── TestADR013Amendments ───────────────────────────────────────────────────


class TestADR013Amendments:
    def test_bloomberg_ht_is_centrist(self) -> None:
        assert get_source("bloomberg_ht").band is Band.CENTRIST

    def test_x_stub_not_in_registry(self) -> None:
        assert "x_stub" not in SOURCES_BY_ID

    def test_no_deferred_sources(self) -> None:
        assert not any(s.kind is SourceKind.DEFERRED for s in SOURCES)

    def test_social_x_band_not_used_in_any_source(self) -> None:
        assert not any(s.band is Band.SOCIAL_X for s in SOURCES)


# ── TestKindConsistency ────────────────────────────────────────────────────


class TestKindConsistency:
    def test_ap_tr_is_html(self) -> None:
        assert get_source("ap_tr").kind is SourceKind.HTML

    def test_reuters_tr_is_rss(self) -> None:
        assert get_source("reuters_tr").kind is SourceKind.RSS

    def test_voa_tr_is_fragile(self) -> None:
        assert get_source("voa_tr").fragility is Fragility.FRAGILE

    def test_resmi_gazete_is_pdf(self) -> None:
        assert get_source("resmi_gazete").kind is SourceKind.PDF

    def test_reddit_is_api(self) -> None:
        assert get_source("reddit_turkey").kind is SourceKind.API

    def test_all_gov_tier_are_html_or_pdf(self) -> None:
        gov_sources = get_sources_by_tier(Tier.GOV)
        allowed = {SourceKind.HTML, SourceKind.PDF}
        for s in gov_sources:
            assert s.kind in allowed, f"{s.id} has unexpected kind {s.kind}"

    def test_gov_aligned_news_sources_are_rss(self) -> None:
        for s in get_sources_by_band(Band.GOV_ALIGNED):
            assert s.kind is SourceKind.RSS, f"{s.id} should be RSS"


# ── TestPlaceholderUrls ────────────────────────────────────────────────────


class TestPlaceholderUrls:
    """Every pending source has the verification note; no verified source does."""

    def test_pending_sources_have_note(self) -> None:
        for sid in EXPECTED_PENDING_URL_IDS:
            s = get_source(sid)
            assert "URL pending operator verification" in s.notes, (
                f"{sid!r} is expected pending but lacks the verification note"
            )

    def test_verified_sources_lack_pending_note(self) -> None:
        for s in SOURCES:
            if s.id not in EXPECTED_PENDING_URL_IDS:
                assert "URL pending operator verification" not in s.notes, (
                    f"{s.id!r} is not in the pending set but has the verification note"
                )

    def test_pending_count_is_exactly_seven(self) -> None:
        pending = [s for s in SOURCES if "URL pending operator verification" in s.notes]
        assert len(pending) == len(EXPECTED_PENDING_URL_IDS), (
            f"Expected {len(EXPECTED_PENDING_URL_IDS)} pending sources, "
            f"got {len(pending)}: {[s.id for s in pending]}"
        )


# ── TestHelpers ────────────────────────────────────────────────────────────


class TestHelpers:
    def test_get_source_returns_correct_source(self) -> None:
        s = get_source("bianet")
        assert isinstance(s, Source)
        assert s.id == "bianet"
        assert s.band is Band.INDEPENDENT

    def test_get_source_raises_key_error_on_miss(self) -> None:
        with pytest.raises(KeyError):
            get_source("nonexistent_source_id")

    def test_get_sources_by_band_gov_aligned_count(self) -> None:
        result = get_sources_by_band(Band.GOV_ALIGNED)
        assert isinstance(result, tuple)
        assert len(result) == 5

    def test_get_sources_by_tier_gov_count(self) -> None:
        result = get_sources_by_tier(Tier.GOV)
        assert isinstance(result, tuple)
        # 5 (was 6 before danistay dropped 2026-05-25 · see sources.py).
        assert len(result) == 5

    def test_get_sources_by_tier_news_count(self) -> None:
        assert len(get_sources_by_tier(Tier.NEWS)) == 24

    def test_get_sources_by_band_opposition_count(self) -> None:
        assert len(get_sources_by_band(Band.OPPOSITION)) == 4


# ── TestSeedSources ────────────────────────────────────────────────────────


class TestSeedSources:
    def test_seed_inserts_all_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        with duckdb.connect(str(db_path)) as conn:
            count = seed_sources(conn)
        assert count == len(SOURCES)

    def test_db_row_count_matches(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        with duckdb.connect(str(db_path)) as conn:
            seed_sources(conn)
            db_count = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        assert db_count == len(SOURCES)

    def test_seed_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        with duckdb.connect(str(db_path)) as conn:
            seed_sources(conn)
        with duckdb.connect(str(db_path)) as conn:
            seed_sources(conn)
            db_count = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        assert db_count == len(SOURCES)

    def test_bloomberg_ht_band_stored_as_centrist(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        with duckdb.connect(str(db_path)) as conn:
            seed_sources(conn)
            row = conn.execute("SELECT band FROM sources WHERE id = 'bloomberg_ht'").fetchone()
        assert row is not None
        assert row[0] == "centrist"

    def test_enum_values_stored_as_strings(self, tmp_path: Path) -> None:
        db_path = tmp_path / "x.duckdb"
        init_db(db_path, load_vss=False)
        with duckdb.connect(str(db_path)) as conn:
            seed_sources(conn)
            row = conn.execute(
                "SELECT band, tier, kind, fragility FROM sources WHERE id = 'resmi_gazete'"
            ).fetchone()
        assert row == ("primary_gov", "gov", "PDF", "MEDIUM")
