"""Tests for musahit.tts.preprocessor."""

from __future__ import annotations

from musahit.tts.preprocessor import (
    ABBREVIATIONS,
    DEFCON_TR_NUMBERS,
    preprocess_for_tts,
)

# ── Abbreviation expansion ─────────────────────────────────────────────────


class TestAbbreviationExpansion:
    def test_tcmb_expanded(self) -> None:
        out = preprocess_for_tts("TCMB faiz kararını açıkladı.")
        assert "TCMB" not in out
        assert ABBREVIATIONS["TCMB"] in out

    def test_bddk_expanded(self) -> None:
        out = preprocess_for_tts("BDDK denetim raporu yayımladı.")
        assert "BDDK" not in out
        assert ABBREVIATIONS["BDDK"] in out

    def test_partial_match_not_expanded(self) -> None:
        # "TBMM" should expand but "TCMBANK" (made up) should NOT — the
        # regex uses word boundaries.
        out = preprocess_for_tts("TBMM ve TCMBANK")
        assert ABBREVIATIONS["TBMM"] in out
        assert "TCMBANK" in out  # untouched — not in dictionary, no \b match

    def test_lowercase_acronym_not_expanded(self) -> None:
        # Acronyms in MÜŞAHİT's output are always uppercase. Lowercase
        # "tcmb" in a quote should NOT be expanded — case-sensitive.
        out = preprocess_for_tts("alıntıdaki tcmb metni")
        assert "tcmb" in out

    def test_multiple_acronyms_all_expanded(self) -> None:
        out = preprocess_for_tts("TBMM ve AYM birlikte karar verdi.")
        assert "TBMM" not in out
        assert "AYM" not in out


# ── DEFCON Turkish numbering ───────────────────────────────────────────────


class TestDefconNumbering:
    """The preprocessor respells ``DEFCON N`` as ``Defkon [Turkish numeral]``.

    The ``Defkon`` respelling is a pronunciation nudge, NOT a
    translation — operator audio QA on 2026-05-23 found that Piper's
    Turkish voice was applying Turkish phoneme rules to ``DEFCON``
    (producing roughly "De-Fe-Kon"). Turkish speakers familiar with
    the term use the English-style "Def-Kon"; the mixed-case ``k``
    respelling nudges the voice into the right phoneme path. The
    written briefing (briefing.md, dashboard HTML) is unchanged.
    """

    def test_defcon_2_to_iki(self) -> None:
        out = preprocess_for_tts("DEFCON 2 seviyesinde değerlendirildi.")
        assert "DEFCON 2" not in out
        assert "Defkon İki" in out

    def test_defcon_5_to_bes(self) -> None:
        out = preprocess_for_tts("DEFCON 5 olarak işaretlendi.")
        assert "Defkon Beş" in out

    def test_defcon_1_to_bir(self) -> None:
        out = preprocess_for_tts("DEFCON 1 alarmı.")
        assert "Defkon Bir" in out

    def test_defcon_3_to_uc(self) -> None:
        out = preprocess_for_tts("DEFCON 3 olarak sınıflandı.")
        assert "Defkon Üç" in out

    def test_defcon_4_to_dort(self) -> None:
        out = preprocess_for_tts("DEFCON 4 gündemine alındı.")
        assert "Defkon Dört" in out

    def test_all_defcon_levels_use_defkon_respelling(self) -> None:
        # All five levels respell DEFCON → Defkon and produce a
        # Turkish numeral. The strict assertion guards against a
        # silent regression to the un-respelled "DEFCON" form.
        for n in (1, 2, 3, 4, 5):
            out = preprocess_for_tts(f"DEFCON {n}")
            assert f"Defkon {DEFCON_TR_NUMBERS[n]}" in out
            # No remnant of the all-caps form.
            assert f"DEFCON {n}" not in out

    def test_middle_dot_separator(self) -> None:
        # Header lines and metadata lines often write the DEFCON
        # numeral after the term with a ``·`` separator
        # ("Zirve DEFCON · 3", "**Zirve DEFCON** · 2" after bold
        # strip). The regex tolerates spaces around the separator.
        out = preprocess_for_tts("Zirve DEFCON · 3")
        assert "Defkon Üç" in out
        assert "DEFCON" not in out

    def test_colon_separator(self) -> None:
        out = preprocess_for_tts("DEFCON: 4")
        assert "Defkon Dört" in out

    def test_hyphen_separator(self) -> None:
        out = preprocess_for_tts("DEFCON-2 acil")
        assert "Defkon İki" in out

    def test_separator_without_spaces(self) -> None:
        # ``DEFCON·5`` (no whitespace around the separator) — guards
        # against the regex requiring at least one space.
        out = preprocess_for_tts("DEFCON·5")
        assert "Defkon Beş" in out

    def test_standalone_defcon_becomes_defkon(self) -> None:
        # Bare DEFCON without a trailing numeral is also respelled —
        # header labels like "Zirve DEFCON" still flow through Piper
        # and need the same phoneme nudge. Note: this is a behaviour
        # change from the initial respelling work (which left bare
        # DEFCON untouched); the regex tightening on 2026-05-23 added
        # the standalone case to the matched set.
        out = preprocess_for_tts("Zirve DEFCON")
        assert "Defkon" in out
        assert "DEFCON" not in out

    def test_standalone_defcon_in_prose(self) -> None:
        # ``DEFCON ölçeği`` (DEFCON followed by a non-digit word) →
        # the optional digit-capture group fails to match, falls back
        # to standalone DEFCON → respelled as "Defkon".
        out = preprocess_for_tts("DEFCON ölçeği aşağıdadır.")
        assert "Defkon ölçeği" in out
        assert "DEFCON" not in out


# ── Markdown stripping ─────────────────────────────────────────────────────


class TestMarkdownStripping:
    def test_bold_removed(self) -> None:
        out = preprocess_for_tts("**Önemli** bilgi")
        assert "**" not in out
        assert "Önemli bilgi" in out

    def test_italic_removed(self) -> None:
        out = preprocess_for_tts("*vurgulu* sözcük")
        assert "vurgulu sözcük" in out

    def test_links_become_label_text(self) -> None:
        out = preprocess_for_tts("[bağlantı metni](https://example.com)")
        assert "bağlantı metni" in out
        assert "example.com" not in out

    def test_headers_stripped(self) -> None:
        out = preprocess_for_tts("# Başlık\n## Alt başlık\nGövde")
        assert "Başlık" in out
        assert "Alt başlık" in out
        assert "Gövde" in out
        assert "#" not in out

    def test_horizontal_rules_removed(self) -> None:
        out = preprocess_for_tts("Önce\n\n---\n\nSonra")
        assert "---" not in out

    def test_arrow_marker_stripped(self) -> None:
        out = preprocess_for_tts("❯ Bir bölüm")
        assert "❯" not in out


# ── Source attribution removal ─────────────────────────────────────────────


class TestSourceLineRemoval:
    def test_kaynaklar_line_removed_with_bold(self) -> None:
        text = "Olay özeti.\n\n**Kaynaklar** · sabah·gov_aligned · sozcu·opposition"
        out = preprocess_for_tts(text)
        assert "Kaynaklar" not in out
        assert "sabah" not in out
        # Body should still be there.
        assert "Olay özeti" in out

    def test_kaynaklar_without_bold(self) -> None:
        text = "Olay özeti.\nKaynaklar · ntv·centrist"
        out = preprocess_for_tts(text)
        assert "Kaynaklar" not in out
        assert "ntv" not in out


# ── Whitespace handling ────────────────────────────────────────────────────


class TestWhitespace:
    def test_blank_lines_collapsed(self) -> None:
        # Three+ blank lines should collapse to one blank line.
        out = preprocess_for_tts("Önce.\n\n\n\n\nSonra.")
        assert "\n\n\n" not in out
        assert "Önce" in out
        assert "Sonra" in out

    def test_empty_input_returns_empty(self) -> None:
        assert preprocess_for_tts("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        # All-markdown content with no payload words → empty after strip.
        assert preprocess_for_tts("---\n\n---") == ""


# ── Order-of-operations integration ────────────────────────────────────────


class TestIntegratedFlow:
    def test_realistic_briefing_chunk(self) -> None:
        text = "\n".join(
            [
                "## ❯ DEFCON 1-2 · ÖNCELİKLİ",
                "",
                "### Olay Başlığı",
                "**DEFCON** · ŞİDDETLİ · **Kategori** · YARGI",
                "",
                "TCMB ve BDDK ortak açıklama yaptı.",
                "",
                "**Kaynaklar** · sabah·gov_aligned · cumhuriyet·opposition",
            ]
        )
        out = preprocess_for_tts(text)
        # Markdown stripped, abbreviations expanded, source line gone.
        assert "**" not in out
        assert "##" not in out
        assert "❯" not in out
        assert "TCMB" not in out
        assert ABBREVIATIONS["TCMB"] in out
        assert "BDDK" not in out
        assert ABBREVIATIONS["BDDK"] in out
        assert "Kaynaklar" not in out
        # Headline and body still there.
        assert "Olay Başlığı" in out
        assert "ortak açıklama yaptı" in out
