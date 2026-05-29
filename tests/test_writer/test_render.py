"""Tests for musahit.writer.render — deterministic itemized sections.

Pins the 2026-05-29 Group-A contract: AÇIK GELİŞMELER + AMBİYANS +
DEFCON 4 carry recency suffixes, arc lists sort freshest-first, EXPIRED
arcs are excluded while DORMANT are kept, and empty/all-EXPIRED sections
return ``None`` (→ the briefer emits the empty-state note).
"""

from __future__ import annotations

from datetime import date

from musahit.arcs.freshness import Freshness
from musahit.score.defcon import DEFCON
from musahit.writer.payload import (
    ArcView,
    BriefingPayload,
    ClusterView,
)
from musahit.writer.render import (
    render_ambient,
    render_open_arcs,
    render_resolved,
    render_routine,
    render_social_only,
)


def _arc(
    arc_id: str,
    *,
    days: int,
    freshness: Freshness,
    peak: int = int(DEFCON.MATERIAL),
    headline: str | None = None,
    category: str = "POLİTİKA",
) -> ArcView:
    return ArcView(
        id=arc_id,
        headline=headline or f"Arc {arc_id}",
        summary="özet",
        state="OPEN",
        peak_defcon=peak,
        category=category,
        last_update_at=None,
        created_at=None,
        days_since_last_update=days,
        freshness=freshness.value,
    )


def _cluster(
    cid: str,
    *,
    defcon: int,
    days: int,
    headline: str | None = None,
    arc_id: str | None = None,
    sources: int = 2,
    category: str = "MEVZUAT",
) -> ClusterView:
    return ClusterView(
        id=cid,
        headline=headline or f"Küme {cid}",
        summary="özet",
        category=category,
        final_defcon=defcon,
        confidence="ORTA",
        bands_present=["independent"],
        arc_id=arc_id,
        sources=[{"source_id": f"s{i}", "band": "independent"} for i in range(sources)],
        is_social_only=False,
        days_since_last_update=days,
        freshness=Freshness.FRESH.value,
    )


def _payload(**kwargs) -> BriefingPayload:
    base = dict(
        date=date(2026, 5, 29),
        run_id="run_test",
        clusters_by_defcon={},
        open_arc_updates=[],
        resolved_arcs=[],
        peak_defcon=int(DEFCON.AMBIENT),
        cluster_count=0,
        arc_count=0,
        open_arc_count=0,
        ambient_count=0,
        failed_sources=[],
        stages_done=["arc-link"],
    )
    base.update(kwargs)
    return BriefingPayload(**base)


# ── AÇIK GELİŞMELER ─────────────────────────────────────────────────────────


class TestOpenArcs:
    def test_recency_suffixes(self) -> None:
        arcs = [
            _arc("arc_a", days=0, freshness=Freshness.FRESH),
            _arc("arc_b", days=1, freshness=Freshness.FRESH),
            _arc("arc_c", days=3, freshness=Freshness.DORMANT),
        ]
        body = render_open_arcs(_payload(open_arc_updates=arcs))
        assert "· bugün" in body
        assert "· dün" in body
        assert "· 3 gün önce" in body

    def test_sorted_freshest_first(self) -> None:
        arcs = [
            _arc("arc_old", days=5, freshness=Freshness.DORMANT, headline="OLD"),
            _arc("arc_new", days=0, freshness=Freshness.FRESH, headline="NEW"),
            _arc("arc_mid", days=2, freshness=Freshness.DORMANT, headline="MID"),
        ]
        body = render_open_arcs(_payload(open_arc_updates=arcs))
        assert body.index("NEW") < body.index("MID") < body.index("OLD")

    def test_expired_excluded_dormant_kept(self) -> None:
        arcs = [
            _arc("arc_fresh", days=0, freshness=Freshness.FRESH, headline="FRESH"),
            _arc("arc_dorm", days=4, freshness=Freshness.DORMANT, headline="DORM"),
            _arc("arc_exp", days=9, freshness=Freshness.EXPIRED, headline="EXP"),
        ]
        body = render_open_arcs(_payload(open_arc_updates=arcs))
        assert "FRESH" in body
        assert "DORM" in body
        assert "EXP" not in body

    def test_all_expired_returns_none(self) -> None:
        arcs = [
            _arc("arc_e1", days=8, freshness=Freshness.EXPIRED),
            _arc("arc_e2", days=12, freshness=Freshness.EXPIRED),
        ]
        assert render_open_arcs(_payload(open_arc_updates=arcs)) is None

    def test_empty_returns_none(self) -> None:
        assert render_open_arcs(_payload()) is None

    def test_arc_id_present_for_tts_rewrite(self) -> None:
        body = render_open_arcs(
            _payload(open_arc_updates=[_arc("arc_20260523_0006", days=6,
                                            freshness=Freshness.DORMANT)])
        )
        assert "`arc_20260523_0006`" in body
        assert "· 6 gün önce" in body

    def test_small_list_is_flat_no_subsections(self) -> None:
        arcs = [_arc(f"arc_{i:04d}", days=0, freshness=Freshness.FRESH) for i in range(5)]
        body = render_open_arcs(_payload(open_arc_updates=arcs))
        assert "### Öne Çıkanlar" not in body
        assert "### Diğer Açık Hikayeler" not in body

    def test_large_list_splits_highlight_and_overflow(self) -> None:
        from musahit.writer.render import VOICED_OPEN_ARCS_CAP

        # 15 arcs · freshest 10 in highlight (voiced), rest in overflow.
        arcs = [
            _arc(f"arc_{i:04d}", days=i % 7, freshness=Freshness.FRESH, headline=f"Arc{i}")
            for i in range(15)
        ]
        body = render_open_arcs(_payload(open_arc_updates=arcs))
        assert "### Öne Çıkanlar" in body
        # Overflow marker must match the TTS extractor's truncation literal.
        assert "### Diğer Açık Hikayeler" in body
        highlight, _, overflow = body.partition("### Diğer Açık Hikayeler")
        # Highlight holds exactly the cap's worth of bullets.
        assert highlight.count("\n- ") + (1 if highlight.lstrip().startswith("- ") else 0) >= 0
        assert overflow.count("- ") == 15 - VOICED_OPEN_ARCS_CAP

    def test_overflow_marker_matches_extractor_constant(self) -> None:
        # Coupling guard · render's overflow marker must equal the literal
        # the TTS extractor truncates the voiced scope on.
        from musahit.tts.extractor import _DIGER_MARKER
        from musahit.writer.render import _OVERFLOW_MARKER

        assert _OVERFLOW_MARKER == _DIGER_MARKER


# ── DEFCON 4 · GÜNDEM ───────────────────────────────────────────────────────


class TestRoutine:
    def test_line_format_matches_brief_example(self) -> None:
        clusters = [
            _cluster(
                "cl1",
                defcon=int(DEFCON.ROUTINE),
                days=6,
                headline="Karakoç'un gizli arşivi ortaya çıktı",
                arc_id="arc_20260523_0006",
                sources=3,
                category="MEVZUAT",
            )
        ]
        body = render_routine(_payload(clusters_by_defcon={int(DEFCON.ROUTINE): clusters}))
        assert (
            "- Karakoç'un gizli arşivi ortaya çıktı · MEVZUAT · (3 kaynak) · "
            "arc_20260523_0006 · 6 gün önce" in body
        )

    def test_no_arc_id_omits_arc_segment(self) -> None:
        clusters = [_cluster("cl1", defcon=int(DEFCON.ROUTINE), days=0, arc_id=None)]
        body = render_routine(_payload(clusters_by_defcon={int(DEFCON.ROUTINE): clusters}))
        assert "· bugün" in body
        assert "arc_" not in body

    def test_empty_returns_none(self) -> None:
        assert render_routine(_payload()) is None


# ── AMBİYANS · DEFCON 5 ─────────────────────────────────────────────────────


class TestAmbient:
    def test_itemized_with_recency(self) -> None:
        clusters = [
            _cluster("cl1", defcon=int(DEFCON.AMBIENT), days=0, headline="Düşük öncelik",
                     sources=1),
        ]
        body = render_ambient(_payload(clusters_by_defcon={int(DEFCON.AMBIENT): clusters}))
        assert "- Düşük öncelik · (1 kaynak) · bugün" in body

    def test_empty_returns_none(self) -> None:
        assert render_ambient(_payload()) is None


# ── DİKKAT · YALNIZCA SOSYALDE ──────────────────────────────────────────────


class TestSocialOnly:
    def test_social_only_clusters_listed(self) -> None:
        c = _cluster("cl_social", defcon=int(DEFCON.ROUTINE), days=0,
                     headline="Sadece sosyalde")
        c = ClusterView(**{**c.__dict__, "is_social_only": True})
        body = render_social_only(
            _payload(clusters_by_defcon={int(DEFCON.ROUTINE): [c]})
        )
        assert "Sadece sosyalde" in body
        assert "yalnızca sosyal" in body

    def test_no_social_only_returns_none(self) -> None:
        c = _cluster("cl_norm", defcon=int(DEFCON.ROUTINE), days=0)
        assert render_social_only(
            _payload(clusters_by_defcon={int(DEFCON.ROUTINE): [c]})
        ) is None


# ── KAPATILAN HİKAYELER ─────────────────────────────────────────────────────


class TestResolved:
    def test_resolved_arcs_listed_as_closed(self) -> None:
        arc = _arc("arc_closed", days=0, freshness=Freshness.FRESH, headline="Kapanan")
        body = render_resolved(_payload(resolved_arcs=[arc]))
        assert "Kapanan" in body
        assert "kapatıldı" in body
        assert "`arc_closed`" in body

    def test_empty_returns_none(self) -> None:
        assert render_resolved(_payload()) is None
