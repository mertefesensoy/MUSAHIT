"""Tests for musahit.normalize.language."""

from __future__ import annotations

from musahit.normalize.language import UNKNOWN, detect_language


class TestDetectLanguage:
    def test_empty_returns_unknown(self) -> None:
        assert detect_language("") == UNKNOWN
        assert detect_language(None) == UNKNOWN

    def test_short_text_returns_unknown(self) -> None:
        # Under 20 chars → unknown regardless of language signal.
        assert detect_language("Merhaba") == UNKNOWN
        assert detect_language("Hello world") == UNKNOWN

    def test_long_turkish_text_returns_tr(self) -> None:
        text = (
            "Türkiye Büyük Millet Meclisi bugün yeni yasama yılına başladı. "
            "Cumhurbaşkanı Erdoğan açılış konuşmasında ekonomi politikalarını anlattı."
        )
        assert detect_language(text) == "tr"

    def test_long_english_text_returns_en(self) -> None:
        text = (
            "The Turkish parliament convened today to debate the new fiscal policy "
            "package proposed by the finance minister last week."
        )
        assert detect_language(text) == "en"

    def test_deterministic_across_calls(self) -> None:
        # langdetect is probabilistic but we seed the factory at import time.
        text = "Bu bir test cümlesidir çünkü dil tespiti tekrarlanabilir olmalı."
        first = detect_language(text)
        second = detect_language(text)
        assert first == second
