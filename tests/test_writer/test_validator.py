"""Tests for musahit.writer.validator."""

from __future__ import annotations

from musahit.writer.template import DOCUMENT_TITLE, TEMPLATE_SECTIONS
from musahit.writer.validator import validate_briefing_markdown, validate_section


def _valid_skeleton() -> str:
    lines = [DOCUMENT_TITLE, ""]
    for s in TEMPLATE_SECTIONS:
        lines.extend([s.marker, "", "içerik", ""])
    return "\n".join(lines) + "\n"


class TestValidBriefing:
    def test_skeleton_passes(self) -> None:
        assert validate_briefing_markdown(_valid_skeleton()) == []

    def test_extra_subsection_h3_does_not_fail(self) -> None:
        body = _valid_skeleton().replace(
            "## ❯ DEFCON 3 · MATERYAL\n",
            "## ❯ DEFCON 3 · MATERYAL\n\n### POLİTİKA\n\n### EKONOMİ\n",
        )
        # Subsections are content, not structural; validator passes.
        assert validate_briefing_markdown(body) == []


class TestInvalidBriefing:
    def test_empty_input_fails(self) -> None:
        errors = validate_briefing_markdown("")
        assert errors
        assert any("empty" in e for e in errors)

    def test_missing_title_fails(self) -> None:
        body = _valid_skeleton().replace(DOCUMENT_TITLE, "# Wrong Title", 1)
        errors = validate_briefing_markdown(body)
        assert any("first non-blank line" in e for e in errors)

    def test_missing_section_fails(self) -> None:
        # Drop the AÇIK GELİŞMELER section.
        skeleton = _valid_skeleton()
        body = skeleton.replace(
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP\n\niçerik\n\n",
            "",
        )
        errors = validate_briefing_markdown(body)
        assert any("missing required sections" in e for e in errors)

    def test_section_in_wrong_order_fails(self) -> None:
        # Swap DEFCON 4 and AÇIK GELİŞMELER positions.
        skeleton = _valid_skeleton()
        body = skeleton.replace(
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP\n\niçerik\n\n"
            "## ❯ DEFCON 4 · GÜNDEM\n\niçerik\n\n",
            "## ❯ DEFCON 4 · GÜNDEM\n\niçerik\n\n"
            "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP\n\niçerik\n\n",
        )
        errors = validate_briefing_markdown(body)
        assert errors
        assert any("out of order" in e or "missing required" in e for e in errors)

    def test_extra_top_level_section_fails(self) -> None:
        # Inject an unexpected top-level section.
        body = _valid_skeleton() + "## ❯ UNEXPECTED · BÖLÜM\n\ngarbage\n"
        errors = validate_briefing_markdown(body)
        assert any("unexpected top-level section" in e for e in errors)

    def test_marker_without_arrow_fails(self) -> None:
        # Replace one ## ❯ section marker with a plain ## (no arrow).
        body = _valid_skeleton().replace(
            "## ❯ SİSTEM LOG\n",
            "## SİSTEM LOG\n",
            1,
        )
        errors = validate_briefing_markdown(body)
        # The plain `## SİSTEM LOG` is an "unexpected top-level section"
        # (it doesn't match the required marker line exactly); and the
        # required SİSTEM LOG section is then missing.
        assert errors


class TestPlaceholderEchoRejected:
    """Regression for the 2026-05-23 smoke run · Trendyol echoed the
    skeleton's literal ``[içerik buraya · …]`` placeholder verbatim into
    AÇIK GELİŞMELER and AMBİYANS · DEFCON 5. The validator now catches
    this so the bad output is rejected and the writer retries."""

    def test_briefing_with_echoed_placeholder_fails(self) -> None:
        body = _valid_skeleton().replace(
            "## ❯ AMBİYANS · DEFCON 5\n\niçerik\n\n",
            "## ❯ AMBİYANS · DEFCON 5\n\n[içerik buraya · şablon talimatlarına bak]\n\n",
            1,
        )
        errors = validate_briefing_markdown(body)
        assert any("unfilled template placeholder" in e for e in errors), errors

    def test_briefing_with_partial_placeholder_fragment_fails(self) -> None:
        """Matching only the opening substring keeps the check robust
        against any future tweak to the placeholder trailer."""
        body = _valid_skeleton().replace(
            "## ❯ DEFCON 4 · GÜNDEM\n\niçerik\n\n",
            "## ❯ DEFCON 4 · GÜNDEM\n\n[içerik buraya tamamlanmadı]\n\n",
            1,
        )
        errors = validate_briefing_markdown(body)
        assert any("unfilled template placeholder" in e for e in errors), errors

    def test_clean_briefing_does_not_trigger_placeholder_rejection(
        self,
    ) -> None:
        """A briefing without the placeholder fragment passes (the
        existing _valid_skeleton uses bare 'içerik' which must not
        match the substring guard)."""
        errors = validate_briefing_markdown(_valid_skeleton())
        assert not any("unfilled template placeholder" in e for e in errors)


class TestValidateSection:
    def test_validate_section_accepts_valid_block(self) -> None:
        text = f"{TEMPLATE_SECTIONS[0].marker}\n\nİçerik buraya gelir.\n"
        assert validate_section(text, 0) is True

    def test_validate_section_rejects_missing_marker(self) -> None:
        text = "Some prose without a marker.\nMore content.\n"
        assert validate_section(text, 0) is False

    def test_validate_section_rejects_wrong_marker(self) -> None:
        text = f"{TEMPLATE_SECTIONS[1].marker}\n\nİçerik.\n"
        assert validate_section(text, 0) is False

    def test_validate_section_rejects_multiple_markers(self) -> None:
        text = (
            f"{TEMPLATE_SECTIONS[0].marker}\n\n"
            f"İçerik.\n\n"
            f"{TEMPLATE_SECTIONS[1].marker}\n\n"
            f"Başka içerik.\n"
        )
        assert validate_section(text, 0) is False

    def test_validate_section_rejects_empty_content(self) -> None:
        text = f"{TEMPLATE_SECTIONS[0].marker}\n\n"
        assert validate_section(text, 0) is False


# ── Issue 3b · Validator hardening for prompt echo + CoT ──────────────────


class TestValidateSectionRejectsPromptEcho:
    """The 2026-05-27 specimen had DİKKAT and AMBİYANS contaminated with
    DISCIPLINE_RULES echo and chain-of-thought scaffolding. The
    per-section validator now rejects these so the writer cannot ship
    fabricated, echoed, or CoT-scaffolded content."""

    def test_rejects_kurallar_marker(self) -> None:
        text = (
            f"{TEMPLATE_SECTIONS[4].marker}\n\n"
            "KURALLAR (ADR-009):\n"
            "- Yorum yapma · sadece raporla.\n"
        )
        assert validate_section(text, 4) is False

    def test_rejects_bolum_verisi_marker(self) -> None:
        text = (
            f"{TEMPLATE_SECTIONS[4].marker}\n\n"
            "İçerik var.\n"
            "BÖLÜM VERİSİ:\n(bugün öğe yok)\n"
        )
        assert validate_section(text, 4) is False

    def test_rejects_cikti_trailer(self) -> None:
        text = (
            f"{TEMPLATE_SECTIONS[4].marker}\n\n"
            "İçerik.\n"
            "ÇIKTI (yalnızca bu bölümün içeriği):\n"
        )
        assert validate_section(text, 4) is False

    def test_rejects_gorev_marker(self) -> None:
        text = (
            f"{TEMPLATE_SECTIONS[4].marker}\n\n"
            "GÖREV · Aşağıdaki bölümü yaz.\n"
        )
        assert validate_section(text, 4) is False


class TestValidateSectionRejectsChainOfThought:
    def test_rejects_adim_n(self) -> None:
        text = (
            f"{TEMPLATE_SECTIONS[5].marker}\n\n"
            "Adım 1: Olayları sırala.\n"
            "İçerik buradadır.\n"
        )
        assert validate_section(text, 5) is False

    def test_rejects_gerekce(self) -> None:
        text = (
            f"{TEMPLATE_SECTIONS[5].marker}\n\n"
            "Gerekçe: Tarafsız bakış için.\n"
            "İçerik buradadır.\n"
        )
        assert validate_section(text, 5) is False

    def test_accepts_clean_prose(self) -> None:
        # Legitimate Turkish prose without any echo or CoT must pass.
        text = (
            f"{TEMPLATE_SECTIONS[0].marker}\n\n"
            "### Önemli olay başlığı\n"
            "**DEFCON** · ŞİDDETLİ · **Kategori** · POLİTİKA · **Güven** · YÜKSEK\n"
            "**Kaynaklar** · bianet·centrist · cumhuriyet·opposition\n\n"
            "Bu olay bugün gerçekleşti ve birden fazla kaynak teyit etti.\n"
        )
        assert validate_section(text, 0) is True

    def test_rejects_legacy_placeholder_echo(self) -> None:
        # Regression for the 2026-05-23 placeholder-echo bug · the
        # per-section validator catches it just like the whole-briefing
        # validator does.
        text = (
            f"{TEMPLATE_SECTIONS[2].marker}\n\n"
            "[içerik buraya · şablon talimatlarına bak]\n"
        )
        assert validate_section(text, 2) is False
