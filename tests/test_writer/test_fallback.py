"""Tests for musahit.writer.fallback — the deterministic Python renderer."""

from __future__ import annotations

from datetime import date

from musahit.score.defcon import DEFCON
from musahit.writer.fallback import render_fallback_briefing
from musahit.writer.payload import (
    ArcView,
    BriefingPayload,
    ClusterView,
    FailedSource,
)
from musahit.writer.validator import validate_briefing_markdown


def _empty_payload() -> BriefingPayload:
    return BriefingPayload(
        date=date(2026, 5, 23),
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
        stages_done=["ingest", "normalize", "cluster", "score", "arc-link"],
    )


def _full_payload() -> BriefingPayload:
    return BriefingPayload(
        date=date(2026, 5, 23),
        run_id="run_test",
        clusters_by_defcon={
            int(DEFCON.SEVERE): [
                ClusterView(
                    id="cl_priority",
                    headline="Önemli olay",
                    summary="Olayın iki cümlelik özeti.",
                    category="POLİTİKA",
                    final_defcon=int(DEFCON.SEVERE),
                    confidence="YÜKSEK",
                    bands_present=["gov_aligned", "opposition"],
                    arc_id="arc_20260518_0001",
                    sources=[
                        {"source_id": "sabah", "band": "gov_aligned"},
                        {"source_id": "cumhuriyet", "band": "opposition"},
                    ],
                    is_social_only=False,
                ),
            ],
            int(DEFCON.MATERIAL): [
                ClusterView(
                    id="cl_material",
                    headline="Materyal olay",
                    summary="Daha küçük olay.",
                    category="EKONOMİ",
                    final_defcon=int(DEFCON.MATERIAL),
                    confidence="ORTA",
                    bands_present=["centrist"],
                    arc_id=None,
                    sources=[{"source_id": "dunya", "band": "centrist"}],
                    is_social_only=False,
                ),
            ],
            int(DEFCON.ROUTINE): [
                ClusterView(
                    id="cl_routine",
                    headline="Rutin haber",
                    summary="",
                    category="TOPLUM",
                    final_defcon=int(DEFCON.ROUTINE),
                    confidence="DÜŞÜK",
                    bands_present=["social_x"],
                    arc_id=None,
                    sources=[{"source_id": "x_stub", "band": "social_x"}],
                    is_social_only=True,
                ),
            ],
        },
        open_arc_updates=[
            ArcView(
                id="arc_20260518_0001",
                headline="İmamoğlu davası",
                summary="Mahkeme sürüyor.",
                state="OPEN",
                peak_defcon=int(DEFCON.SEVERE),
                category="YARGI",
                last_update_at=None,
                created_at=None,
            )
        ],
        resolved_arcs=[
            ArcView(
                id="arc_20260420_0009",
                headline="Eski olay",
                summary="Kapatıldı.",
                state="RESOLVED",
                peak_defcon=int(DEFCON.MATERIAL),
                category="POLİTİKA",
                last_update_at=None,
                created_at=None,
            )
        ],
        peak_defcon=int(DEFCON.SEVERE),
        cluster_count=3,
        arc_count=15,
        open_arc_count=4,
        ambient_count=47,
        failed_sources=[
            FailedSource("ap_tr", "HTTP_ERROR", "HTTP 503"),
        ],
        stages_done=["ingest", "normalize", "cluster", "score", "arc-link"],
    )


class TestAlwaysPassesValidator:
    def test_empty_payload_passes(self) -> None:
        body = render_fallback_briefing(_empty_payload())
        assert validate_briefing_markdown(body) == []

    def test_full_payload_passes(self) -> None:
        body = render_fallback_briefing(_full_payload())
        assert validate_briefing_markdown(body) == []


class TestDeterminism:
    def test_same_payload_same_output(self) -> None:
        p = _full_payload()
        assert render_fallback_briefing(p) == render_fallback_briefing(p)


class TestSectionPresence:
    def test_all_eight_sections_in_output(self) -> None:
        body = render_fallback_briefing(_full_payload())
        for marker in (
            "## ❯ DEFCON 1-2 · ÖNCELİKLİ",
            "## ❯ DEFCON 3 · MATERYAL",
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP",
            "## ❯ DEFCON 4 · GÜNDEM",
            "## ❯ DİKKAT · YALNIZCA SOSYALDE",
            "## ❯ AMBİYANS · DEFCON 5",
            "## ❯ KAPATILAN HİKAYELER",
            "## ❯ SİSTEM LOG",
        ):
            assert marker in body

    def test_ambient_count_surfaces_in_text(self) -> None:
        body = render_fallback_briefing(_full_payload())
        assert "47 başlık ambiyans" in body

    def test_priority_cluster_renders_full_block(self) -> None:
        body = render_fallback_briefing(_full_payload())
        assert "Önemli olay" in body
        assert "arc_20260518_0001" in body
        assert "Olayın iki cümlelik özeti." in body

    def test_routine_cluster_compacted(self) -> None:
        body = render_fallback_briefing(_full_payload())
        # routine renders as a single-line bullet
        assert "- Rutin haber" in body

    def test_social_only_flagged_in_dikkat_section(self) -> None:
        body = render_fallback_briefing(_full_payload())
        # The social_only cluster from routine bucket also lists under DİKKAT.
        dikkat_idx = body.index("## ❯ DİKKAT · YALNIZCA SOSYALDE")
        ambient_idx = body.index("## ❯ AMBİYANS · DEFCON 5")
        dikkat_block = body[dikkat_idx:ambient_idx]
        assert "Rutin haber" in dikkat_block

    def test_failed_sources_listed_in_system_log(self) -> None:
        body = render_fallback_briefing(_full_payload())
        log_idx = body.index("## ❯ SİSTEM LOG")
        log = body[log_idx:]
        assert "ap_tr" in log

    def test_no_failed_sources_renders_yok(self) -> None:
        body = render_fallback_briefing(_empty_payload())
        assert "(yok)" in body
