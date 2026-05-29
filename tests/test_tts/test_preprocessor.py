"""Tests for musahit.tts.preprocessor."""

from __future__ import annotations

from musahit.tts.preprocessor import (
    ABBREVIATIONS,
    ALL_DORMANT_VOICE_NOTE,
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


# ── Arc ID rewriting ──────────────────────────────────────────────────────


class TestArcIdRewriting:
    """The preprocessor rewrites ``arc_YYYYMMDD_NNNN`` to ``hikaye N``.

    The written briefing (briefing.md) keeps full arc IDs for
    cross-referencing. The TTS-bound text uses only the trailing serial
    to avoid Piper reading the YYYYMMDD prefix as a huge integer.
    """

    def test_single_arc_id(self) -> None:
        out = preprocess_for_tts("arc_20260523_0001")
        assert "hikaye 1" in out

    def test_triple_digit_serial(self) -> None:
        out = preprocess_for_tts("arc_20260526_0167")
        assert "hikaye 167" in out

    def test_multiple_arc_ids_in_bullet_list(self) -> None:
        text = "Diğer hikayeler: arc_20260524_0072 · arc_20260525_0003"
        out = preprocess_for_tts(text)
        assert "hikaye 72" in out
        assert "hikaye 3" in out
        assert "arc_" not in out

    def test_inline_prose(self) -> None:
        text = "...bağlantılı arc_20260523_0001 sürüyor."
        out = preprocess_for_tts(text)
        assert "hikaye 1" in out
        assert "arc_20260523_0001" not in out

    def test_no_arc_ids_passthrough(self) -> None:
        text = "Hiçbir hikaye referansı yok."
        out = preprocess_for_tts(text)
        assert out.strip() == text

    def test_backtick_wrapped_arc_id(self) -> None:
        text = "`arc_20260523_0001` hakkında bilgi"
        out = preprocess_for_tts(text)
        assert "hikaye 1" in out
        assert "arc_20260523_0001" not in out


# ── Dormancy skip (2026-05-29 Group-A) ─────────────────────────────────────


class TestDormancySkip:
    """Itemized arc lines carry a recency suffix; the voice briefing
    drops DORMANT lines (N gün önce, N≥2) and keeps FRESH (bugün/dün)."""

    def test_dormant_line_dropped(self) -> None:
        text = (
            "- Taze gelişme · ŞİDDETLİ · YARGI · `arc_20260529_0001` · bugün\n"
            "- Eski gelişme · MATERYAL · POLİTİKA · `arc_20260523_0006` · 6 gün önce\n"
        )
        out = preprocess_for_tts(text)
        assert "Taze gelişme" in out
        assert "Eski gelişme" not in out

    def test_fresh_lines_kept(self) -> None:
        text = (
            "- Bugünkü olay · YARGI · `arc_20260529_0001` · bugün\n"
            "- Dünkü olay · POLİTİKA · `arc_20260528_0002` · dün\n"
        )
        out = preprocess_for_tts(text)
        assert "Bugünkü olay" in out
        assert "Dünkü olay" in out

    def test_two_days_is_dropped(self) -> None:
        # Exactly the dormancy boundary · 2 gün önce → dropped.
        text = "- Sınır olay · POLİTİKA · `arc_x` · 2 gün önce\n"
        out = preprocess_for_tts(text)
        assert "Sınır olay" not in out

    def test_all_dormant_block_becomes_note(self) -> None:
        text = (
            "- Birinci · POLİTİKA · `arc_20260523_0001` · 6 gün önce\n"
            "- İkinci · EKONOMİ · `arc_20260524_0002` · 5 gün önce\n"
        )
        out = preprocess_for_tts(text)
        assert "Birinci" not in out
        assert "İkinci" not in out
        assert ALL_DORMANT_VOICE_NOTE in out

    def test_mixed_block_keeps_fresh_no_note(self) -> None:
        text = (
            "- Taze · YARGI · `arc_a` · bugün\n"
            "- Eski · POLİTİKA · `arc_b` · 4 gün önce\n"
        )
        out = preprocess_for_tts(text)
        assert "Taze" in out
        assert "Eski" not in out
        # A surviving fresh line means NO all-dormant note.
        assert ALL_DORMANT_VOICE_NOTE not in out

    def test_prose_ending_in_gun_once_mid_sentence_not_dropped(self) -> None:
        # Recency match is line-end anchored · a DEFCON-3 prose line that
        # merely mentions "6 gün önce" mid-sentence must NOT be dropped.
        text = "Olay 6 gün önce açıklandı ve hâlâ sürüyor.\n"
        out = preprocess_for_tts(text)
        assert "hâlâ sürüyor" in out

    def test_dormancy_skip_preserves_non_arc_lines(self) -> None:
        text = (
            "### Önemli başlık\n"
            "Bu bir DEFCON 3 özeti.\n"
            "- Taze arc · YARGI · `arc_a` · bugün\n"
            "- Eski arc · POLİTİKA · `arc_b` · 8 gün önce\n"
        )
        out = preprocess_for_tts(text)
        assert "Önemli başlık" in out
        assert "Bu bir Defkon Üç özeti." in out
        assert "Taze arc" in out
        assert "Eski arc" not in out

    def test_arc_id_rewrite_still_works_on_fresh_line(self) -> None:
        text = "- Taze · YARGI · `arc_20260529_0007` · bugün\n"
        out = preprocess_for_tts(text)
        assert "hikaye 7" in out
        assert "arc_20260529_0007" not in out
        # Backtick stripped from the spoken text.
        assert "`" not in out

    def test_llm_prose_ending_in_gun_once_not_dropped(self) -> None:
        # An LLM DEFCON-3 metadata line ending in "· N gün önce" is NOT a
        # deterministic arc bullet (no leading "- ", no backtick arc id), so
        # it must survive — the dormancy filter is scoped to arc bullets.
        text = "**Güncelleme** · 3 gün önce\nDevam eden bir gelişme var.\n"
        out = preprocess_for_tts(text)
        assert "Güncelleme · 3 gün önce" in out
        assert "Devam eden bir gelişme var." in out

    def test_bullet_without_arc_id_not_dropped(self) -> None:
        # A bullet that lacks a backtick arc id is not a deterministic arc
        # line · it is left alone even if it ends in a recency phrase.
        text = "- Serbest madde · 4 gün önce\n"
        out = preprocess_for_tts(text)
        assert "Serbest madde" in out

    def test_orphaned_highlight_header_dropped_when_all_dormant(self) -> None:
        # >10-arc highlight block whose voiced top-10 are all dormant: the
        # "### Öne Çıkanlar" subheader must not be left stranded before the
        # all-dormant note.
        text = (
            "### Öne Çıkanlar\n"
            "\n"
            "- Birinci · POLİTİKA · `arc_a` · 3 gün önce\n"
            "- İkinci · EKONOMİ · `arc_b` · 4 gün önce\n"
        )
        out = preprocess_for_tts(text)
        assert "Öne Çıkanlar" not in out
        assert ALL_DORMANT_VOICE_NOTE in out
        assert out.count(ALL_DORMANT_VOICE_NOTE) == 1

    def test_two_dormant_blocks_split_by_blank_emit_single_note(self) -> None:
        text = (
            "- A · POLİTİKA · `arc_a` · 3 gün önce\n"
            "- B · EKONOMİ · `arc_b` · 4 gün önce\n"
            "\n"
            "- C · TOPLUM · `arc_c` · 5 gün önce\n"
            "- D · YARGI · `arc_d` · 6 gün önce\n"
        )
        out = preprocess_for_tts(text)
        assert out.count(ALL_DORMANT_VOICE_NOTE) == 1
        for h in ("A ·", "B ·", "C ·", "D ·"):
            assert h not in out


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
