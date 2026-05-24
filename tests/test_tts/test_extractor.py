"""Tests for musahit.tts.extractor."""

from __future__ import annotations

from musahit.tts.extractor import (
    ALL_MARKERS,
    CLOSING_LINE,
    MARKER_AMBIENT,
    MARKER_DEFCON_1_2,
    MARKER_DEFCON_3,
    MARKER_DEFCON_4,
    MARKER_OPEN_ARCS,
    MARKER_RESOLVED,
    MARKER_SOCIAL_ONLY,
    MARKER_SYSTEM_LOG,
    SKIPPED_MARKERS,
    VOICED_MARKERS,
    extract_voiced_briefing,
    extract_voiced_sections,
)


def _full_briefing() -> str:
    """A briefing covering every ADR-009 section with telltale strings.

    Each section's body contains a unique string we can grep for in
    assertions · ``PRIORITY_BODY``, ``MATERIAL_BODY``, etc.
    """
    return "\n".join(
        [
            "# MÜŞAHİT · GÜNLÜK BRİF",
            "",
            "**Tarih** · 23 Mayıs 2026 · Cumartesi",
            "**Zirve DEFCON** · 2",
            "**İşlenen olay** · 47",
            "",
            "---",
            "",
            MARKER_DEFCON_1_2,
            "",
            "### PRIORITY_HEADLINE",
            "PRIORITY_BODY",
            "",
            "---",
            "",
            MARKER_DEFCON_3,
            "",
            "### POLİTİKA",
            "#### MATERIAL_HEADLINE",
            "MATERIAL_BODY",
            "**Kaynaklar** · sabah·gov_aligned · cumhuriyet·opposition",
            "",
            "---",
            "",
            MARKER_OPEN_ARCS,
            "",
            "### OPEN_ARC_HEADLINE",
            "OPEN_ARC_BODY",
            "",
            "---",
            "",
            MARKER_DEFCON_4,
            "",
            "- ROUTINE_HEADLINE · POLİTİKA · kaynaklar (3)",
            "",
            "---",
            "",
            MARKER_SOCIAL_ONLY,
            "",
            "SOCIAL_BODY",
            "",
            "---",
            "",
            MARKER_AMBIENT,
            "",
            "AMBIENT_BODY",
            "",
            "---",
            "",
            MARKER_RESOLVED,
            "",
            "RESOLVED_BODY",
            "",
            "---",
            "",
            MARKER_SYSTEM_LOG,
            "",
            "SYSTEM_LOG_BODY",
        ]
    )


# ── Voiced sections present ────────────────────────────────────────────────


class TestVoicedSectionsPresent:
    def test_header_extracted(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "**Tarih** · 23 Mayıs 2026 · Cumartesi" in out
        assert "**Zirve DEFCON** · 2" in out

    def test_priority_body_kept(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "PRIORITY_BODY" in out

    def test_material_body_kept(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "MATERIAL_BODY" in out

    def test_open_arc_body_kept(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "OPEN_ARC_BODY" in out

    def test_closing_line_appended(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert CLOSING_LINE in out


# ── Skipped sections absent ────────────────────────────────────────────────


class TestSkippedSectionsAbsent:
    def test_routine_body_absent(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "ROUTINE_HEADLINE" not in out
        assert "ROUTINE_BODY" not in out

    def test_social_body_absent(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "SOCIAL_BODY" not in out

    def test_ambient_body_absent(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "AMBIENT_BODY" not in out

    def test_resolved_body_absent(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "RESOLVED_BODY" not in out

    def test_system_log_body_absent(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "SYSTEM_LOG_BODY" not in out


# ── DEFCON 3 source attribution stripping ──────────────────────────────────


class TestDefcon3SourceStripping:
    def test_kaynaklar_line_removed(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "sabah·gov_aligned" not in out
        assert "Kaynaklar" not in out

    def test_material_headline_kept(self) -> None:
        out = extract_voiced_sections(_full_briefing())
        assert "MATERIAL_HEADLINE" in out


# ── Marker constants ────────────────────────────────────────────────────────


class TestMarkerSets:
    def test_voiced_and_skipped_are_disjoint(self) -> None:
        assert set(VOICED_MARKERS).isdisjoint(set(SKIPPED_MARKERS))

    def test_all_markers_is_union(self) -> None:
        assert set(ALL_MARKERS) == set(VOICED_MARKERS) | set(SKIPPED_MARKERS)


# ── VoicedBriefing structure ───────────────────────────────────────────────


class TestVoicedBriefingStructure:
    def test_chunks_order_matches_voicing_order(self) -> None:
        voiced = extract_voiced_briefing(_full_briefing())
        chunks = voiced.chunks()
        # First chunk is header, last is closing line.
        assert "Tarih" in chunks[0]
        assert chunks[-1] == CLOSING_LINE

    def test_chunks_skip_empty_sections(self) -> None:
        # A briefing with no DEFCON 1-2 still produces a chunks list.
        bare = "\n".join(
            [
                "# MÜŞAHİT · GÜNLÜK BRİF",
                "",
                "**Tarih** · 23 Mayıs 2026",
                "",
                MARKER_DEFCON_3,
                "",
                "MATERIAL_BODY",
            ]
        )
        voiced = extract_voiced_briefing(bare)
        chunks = voiced.chunks()
        # Header, material, closing. No empty DEFCON 1-2 entry.
        assert len(chunks) == 3

    def test_found_markers_records_what_was_seen(self) -> None:
        voiced = extract_voiced_briefing(_full_briefing())
        for marker in (MARKER_DEFCON_1_2, MARKER_DEFCON_3, MARKER_OPEN_ARCS):
            assert marker in voiced.found_markers


# ── Degraded inputs ─────────────────────────────────────────────────────────


class TestDegradedInputs:
    def test_empty_briefing_returns_closing_only(self) -> None:
        out = extract_voiced_sections("")
        # Closing line is always present; no content sections.
        assert CLOSING_LINE in out

    def test_briefing_without_voiced_sections(self) -> None:
        # Just the header and SİSTEM LOG · degraded briefing.
        text = "\n".join(
            [
                "# MÜŞAHİT · GÜNLÜK BRİF",
                "",
                "**Tarih** · 23 Mayıs 2026",
                "",
                MARKER_SYSTEM_LOG,
                "",
                "SYSTEM_LOG_BODY",
            ]
        )
        out = extract_voiced_sections(text)
        # Header + closing should still be present; system log absent.
        assert "Tarih" in out
        assert "SYSTEM_LOG_BODY" not in out
        assert CLOSING_LINE in out


# ── Regression: AÇIK GELİŞMELER subsection-split truncation (2026-05-24) ───


def _briefing_with_open_arcs_split() -> str:
    """Briefing where AÇIK GELİŞMELER carries the Öne Çıkanlar / Diğer split.

    Mirrors the shape produced by the fallback renderer after the
    2026-05-24 amendment.
    """
    return "\n".join(
        [
            "# MÜŞAHİT · GÜNLÜK BRİF",
            "",
            "**Tarih** · 24 Mayıs 2026 · Cuma",
            "",
            "---",
            "",
            MARKER_OPEN_ARCS,
            "",
            "### Öne Çıkanlar",
            "",
            "### HIGHLIGHT_ARC_HEADLINE · arc_001",
            "HIGHLIGHT_BODY_LINE",
            "",
            "### Diğer Açık Hikayeler",
            "",
            "- OVERFLOW_BULLET_A · MATERYAL · POLİTİKA · `arc_002`",
            "- OVERFLOW_BULLET_B · ROUTINE · TOPLUM · `arc_003`",
            "",
            "---",
            "",
            MARKER_DEFCON_4,
            "",
            "- ROUTINE_HEADLINE · POLİTİKA · kaynaklar (3)",
        ]
    )


class TestOpenArcsSubsectionTruncation:
    def test_highlight_content_voiced(self) -> None:
        out = extract_voiced_sections(_briefing_with_open_arcs_split())
        assert "HIGHLIGHT_ARC_HEADLINE" in out
        assert "HIGHLIGHT_BODY_LINE" in out

    def test_diger_marker_line_dropped(self) -> None:
        out = extract_voiced_sections(_briefing_with_open_arcs_split())
        assert "Diğer Açık Hikayeler" not in out

    def test_overflow_bullets_excluded(self) -> None:
        out = extract_voiced_sections(_briefing_with_open_arcs_split())
        assert "OVERFLOW_BULLET_A" not in out
        assert "OVERFLOW_BULLET_B" not in out
        # And the arc_ids referenced only in the overflow bullets.
        assert "arc_002" not in out
        assert "arc_003" not in out

    def test_diger_in_other_sections_does_not_truncate(self) -> None:
        """The truncation is scoped to MARKER_OPEN_ARCS · a line that
        happens to look like the Diğer marker inside DEFCON 1-2 must
        NOT drop subsequent content."""
        text = "\n".join(
            [
                "# MÜŞAHİT · GÜNLÜK BRİF",
                "",
                MARKER_DEFCON_1_2,
                "",
                "### PRIORITY_HEADLINE",
                "PRIORITY_BODY_BEFORE",
                "### Diğer Açık Hikayeler",  # decoy outside OPEN_ARCS
                "PRIORITY_BODY_AFTER",
            ]
        )
        out = extract_voiced_sections(text)
        assert "PRIORITY_BODY_BEFORE" in out
        # When the decoy marker fires inside DEFCON 1-2 it should NOT
        # truncate · the truncation guard checks current_key == OPEN_ARCS.
        assert "PRIORITY_BODY_AFTER" in out

    def test_old_shape_briefing_still_works(self) -> None:
        """Briefings produced before the 2026-05-24 amendment have no
        Diğer marker · the extractor must still bucket all AÇIK
        GELİŞMELER content into the voiced section unchanged."""
        text = "\n".join(
            [
                "# MÜŞAHİT · GÜNLÜK BRİF",
                "",
                MARKER_OPEN_ARCS,
                "",
                "### LEGACY_ARC_ONE · arc_l01",
                "LEGACY_BODY_ONE",
                "",
                "### LEGACY_ARC_TWO · arc_l02",
                "LEGACY_BODY_TWO",
                "",
                "---",
                "",
                MARKER_DEFCON_4,
                "",
                "- ROUTINE · POLİTİKA",
            ]
        )
        out = extract_voiced_sections(text)
        assert "LEGACY_BODY_ONE" in out
        assert "LEGACY_BODY_TWO" in out
        assert "LEGACY_ARC_ONE" in out
        assert "LEGACY_ARC_TWO" in out
