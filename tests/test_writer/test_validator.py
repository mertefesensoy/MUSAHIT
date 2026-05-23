"""Tests for musahit.writer.validator."""

from __future__ import annotations

from musahit.writer.template import DOCUMENT_TITLE, TEMPLATE_SECTIONS
from musahit.writer.validator import validate_briefing_markdown


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
