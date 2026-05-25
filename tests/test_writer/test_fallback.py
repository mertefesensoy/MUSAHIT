"""Tests for musahit.writer.fallback · the deterministic Python renderer."""

from __future__ import annotations

from datetime import date, datetime

from musahit.score.defcon import DEFCON
from musahit.writer.fallback import (
    VOICED_OPEN_ARCS_CAP,
    render_fallback_briefing,
)
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


# ── Regression: AÇIK GELİŞMELER subsection split (2026-05-24) ──────────────


def _arc(
    id_: str,
    *,
    peak: int,
    headline: str,
    last_update_at: datetime | None = None,
    created_at: datetime | None = None,
    category: str = "POLİTİKA",
) -> ArcView:
    return ArcView(
        id=id_,
        headline=headline,
        summary="kısa özet",
        state="OPEN",
        peak_defcon=peak,
        category=category,
        last_update_at=last_update_at,
        created_at=created_at,
    )


def _payload_with_open_arcs(arcs: list[ArcView]) -> BriefingPayload:
    return BriefingPayload(
        date=date(2026, 5, 24),
        run_id="run_arcs_test",
        clusters_by_defcon={},
        open_arc_updates=arcs,
        resolved_arcs=[],
        peak_defcon=int(DEFCON.AMBIENT),
        cluster_count=0,
        arc_count=len(arcs),
        open_arc_count=len(arcs),
        ambient_count=0,
        failed_sources=[],
        stages_done=["ingest", "normalize", "cluster", "score", "arc-link"],
    )


def _open_arcs_block(body: str) -> str:
    start = body.index("## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP")
    end = body.index("## ❯ DEFCON 4 · GÜNDEM")
    return body[start:end]


class TestOpenArcsSubsectionSplit:
    def test_eleven_arcs_produce_one_highlight_and_one_overflow(self) -> None:
        arcs = [
            _arc(f"arc_a_{i:04d}", peak=int(DEFCON.MATERIAL), headline=f"A{i}")
            for i in range(11)
        ]
        body = render_fallback_briefing(_payload_with_open_arcs(arcs))
        block = _open_arcs_block(body)
        assert "### Öne Çıkanlar" in block
        assert "### Diğer Açık Hikayeler" in block
        # Highlight subsection includes 10 full ### blocks (one per
        # arc) · count is most easily verified by counting overflow
        # bullets in the Diğer subsection.
        diger_start = block.index("### Diğer Açık Hikayeler")
        diger_body = block[diger_start:]
        overflow_bullets = [
            line for line in diger_body.splitlines()
            if line.startswith("- ") and "`arc_a_" in line
        ]
        assert len(overflow_bullets) == 1

    def test_ten_arcs_produce_only_highlight_subsection(self) -> None:
        arcs = [
            _arc(f"arc_b_{i:04d}", peak=int(DEFCON.MATERIAL), headline=f"B{i}")
            for i in range(VOICED_OPEN_ARCS_CAP)
        ]
        body = render_fallback_briefing(_payload_with_open_arcs(arcs))
        block = _open_arcs_block(body)
        assert "### Öne Çıkanlar" in block
        assert "### Diğer Açık Hikayeler" not in block

    def test_sort_order_severity_then_recency(self) -> None:
        # Three SEVERE arcs (most severe = lowest int), two MATERIAL,
        # two ROUTINE. Within each severity tier, vary last_update_at
        # so we can pin the order.
        arcs = [
            _arc(
                "arc_rt_old",
                peak=int(DEFCON.ROUTINE),
                headline="ROUTINE_OLD",
                last_update_at=datetime(2026, 5, 20, 12),
            ),
            _arc(
                "arc_sv_old",
                peak=int(DEFCON.SEVERE),
                headline="SEVERE_OLD",
                last_update_at=datetime(2026, 5, 20, 12),
            ),
            _arc(
                "arc_mt_recent",
                peak=int(DEFCON.MATERIAL),
                headline="MATERIAL_RECENT",
                last_update_at=datetime(2026, 5, 24, 9),
            ),
            _arc(
                "arc_sv_recent",
                peak=int(DEFCON.SEVERE),
                headline="SEVERE_RECENT",
                last_update_at=datetime(2026, 5, 24, 9),
            ),
        ]
        body = render_fallback_briefing(_payload_with_open_arcs(arcs))
        block = _open_arcs_block(body)
        # Expected order in Öne Çıkanlar: SEVERE_RECENT, SEVERE_OLD,
        # MATERIAL_RECENT, ROUTINE_OLD.
        idx_sr = block.index("SEVERE_RECENT")
        idx_so = block.index("SEVERE_OLD")
        idx_mr = block.index("MATERIAL_RECENT")
        idx_ro = block.index("ROUTINE_OLD")
        assert idx_sr < idx_so < idx_mr < idx_ro

    def test_arcs_without_last_update_at_do_not_crash_sort(self) -> None:
        """ArcView.last_update_at can be None · the sort fallback uses
        created_at, then epoch 0.0 · neither path raises."""
        arcs = [
            _arc("arc_no_dates_1", peak=int(DEFCON.MATERIAL), headline="ND1"),
            _arc(
                "arc_no_dates_2",
                peak=int(DEFCON.MATERIAL),
                headline="ND2",
                created_at=datetime(2026, 5, 1),
            ),
            _arc(
                "arc_has_dates",
                peak=int(DEFCON.MATERIAL),
                headline="HD",
                last_update_at=datetime(2026, 5, 24),
            ),
        ]
        body = render_fallback_briefing(_payload_with_open_arcs(arcs))
        block = _open_arcs_block(body)
        # All three rendered into the highlight subsection (count is 3
        # which is below the cap so no Diğer subsection).
        assert "### Diğer Açık Hikayeler" not in block
        for h in ("ND1", "ND2", "HD"):
            assert h in block
        # The arc with the most-recent last_update_at sorts first.
        # ND1 has no dates (epoch 0). ND2 has created_at 2026-05-01 only.
        # HD has last_update_at 2026-05-24. Expected order: HD, ND2, ND1.
        idx_hd = block.index("HD")
        idx_nd2 = block.index("ND2")
        idx_nd1 = block.index("ND1")
        assert idx_hd < idx_nd2 < idx_nd1

    def test_overflow_bullet_shape_matches_spec(self) -> None:
        arcs = [
            _arc(
                f"arc_pad_{i:04d}",
                peak=int(DEFCON.MATERIAL),
                headline=f"pad{i}",
                last_update_at=datetime(2026, 5, 23, 9),
            )
            for i in range(VOICED_OPEN_ARCS_CAP)
        ]
        arcs.append(
            _arc(
                "arc_overflow_one",
                peak=int(DEFCON.AMBIENT),
                headline="Düşük öncelikli arc",
                last_update_at=datetime(2026, 5, 1),
                category="TOPLUM",
            )
        )
        body = render_fallback_briefing(_payload_with_open_arcs(arcs))
        block = _open_arcs_block(body)
        # Format: "- {headline} · {DEFCON_LABEL_TR} · {category} · `{arc_id}`"
        assert (
            "- Düşük öncelikli arc · AMBİYANS · TOPLUM · `arc_overflow_one`"
            in block
        )

    def test_split_briefing_still_passes_validator(self) -> None:
        arcs = [
            _arc(f"arc_v_{i:04d}", peak=int(DEFCON.MATERIAL), headline=f"v{i}")
            for i in range(15)
        ]
        body = render_fallback_briefing(_payload_with_open_arcs(arcs))
        assert validate_briefing_markdown(body) == []


# ── Regression: empty-headline placeholder rendering (2026-05-25) ──────────


class TestPlaceholderHeadlineRendering:
    """Per docs/investigations/2026-05-25-empty-headlines.md · the
    classifier's Option B fix writes a non-empty placeholder when the
    worker LLM fails, so the renderer sees a real headline and does NOT
    substitute ``(başlıksız)``. Pinned here so the two fixes stay coupled.
    """

    PLACEHOLDER_HEADLINE = "(sınıflandırılamadı)"
    PLACEHOLDER_SUMMARY = (
        "Skorlama modeli bu kümede geçerli yanıt üretemedi. "
        "Operatör incelemesi bekliyor."
    )

    def _placeholder_arc(self) -> ArcView:
        return ArcView(
            id="arc_placeholder_test",
            headline=self.PLACEHOLDER_HEADLINE,
            summary=self.PLACEHOLDER_SUMMARY,
            state="OPEN",
            peak_defcon=int(DEFCON.AMBIENT),
            category="SINIFLANDIRILMADI",
            last_update_at=datetime(2026, 5, 25, 8, 0),
            created_at=datetime(2026, 5, 25, 8, 0),
            last_update_headline=self.PLACEHOLDER_HEADLINE,
            last_update_summary=self.PLACEHOLDER_SUMMARY,
            last_update_cluster_id="cl_placeholder",
            is_active_today=True,
            days_since_last_update=0,
        )

    def test_placeholder_arc_renders_without_baslıksız(self) -> None:
        body = render_fallback_briefing(
            _payload_with_open_arcs([self._placeholder_arc()])
        )
        block = _open_arcs_block(body)
        # The renderer's "or '(başlıksız)'" substitution does NOT fire
        # because the headline is non-empty · only "(sınıflandırılamadı)"
        # appears in the arc block.
        assert "(başlıksız)" not in block
        assert self.PLACEHOLDER_HEADLINE in block

    def test_placeholder_summary_renders_as_guncelleme_body(self) -> None:
        body = render_fallback_briefing(
            _payload_with_open_arcs([self._placeholder_arc()])
        )
        # Active-today rendering uses the **Güncelleme** prefix for the
        # body; the placeholder summary appears under it.
        assert "**Güncelleme**" in body
        assert "Skorlama modeli" in body

    def test_overflow_bullet_form_renders_placeholder_text(self) -> None:
        """Stalled arcs land in the bulleted overflow as
        ``- {headline} · {DEFCON} · {category} · `{id}``` · the
        placeholder headline must appear in that bullet (no
        ``(başlıksız)`` substitution)."""
        # Force overflow by pushing more than 10 active-today arcs.
        arcs = [
            ArcView(
                id=f"arc_pad_{i:04d}",
                headline=f"Real arc {i}",
                summary="kısa özet",
                state="OPEN",
                peak_defcon=int(DEFCON.MATERIAL),
                category="POLİTİKA",
                last_update_at=datetime(2026, 5, 25, 9),
                created_at=datetime(2026, 5, 25, 9),
                is_active_today=True,
            )
            for i in range(VOICED_OPEN_ARCS_CAP)
        ]
        # Stalled placeholder arc · sorts to overflow under the new key
        # `(active_tier, peak, -epoch)` because is_active_today=False.
        stalled_placeholder = ArcView(
            id="arc_stalled_placeholder",
            headline=self.PLACEHOLDER_HEADLINE,
            summary=self.PLACEHOLDER_SUMMARY,
            state="OPEN",
            peak_defcon=int(DEFCON.AMBIENT),
            category="SINIFLANDIRILMADI",
            last_update_at=datetime(2026, 5, 23),
            created_at=datetime(2026, 5, 23),
            is_active_today=False,
            days_since_last_update=2,
        )
        arcs.append(stalled_placeholder)
        body = render_fallback_briefing(_payload_with_open_arcs(arcs))
        block = _open_arcs_block(body)
        diger_start = block.index("### Diğer Açık Hikayeler")
        diger_body = block[diger_start:]
        assert (
            "- (sınıflandırılamadı) · AMBİYANS · SINIFLANDIRILMADI "
            "· `arc_stalled_placeholder`"
        ) in diger_body
        # And NOT the generic placeholder.
        generic_bullet = (
            "(başlıksız) · AMBİYANS · SINIFLANDIRILMADI "
            "· `arc_stalled_placeholder`"
        )
        assert generic_bullet not in diger_body
