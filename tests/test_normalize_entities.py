"""Tests for musahit.normalize.entities + entities_vocab."""

from __future__ import annotations

from musahit.normalize.entities import extract_entities
from musahit.normalize.entities_vocab import EntityType


def _types(entities: list[dict]) -> list[str]:
    return [e["type"] for e in entities]


def _texts(entities: list[dict]) -> list[str]:
    return [e["text"] for e in entities]


class TestPartyDetection:
    def test_canonical_short_form(self) -> None:
        ents = extract_entities("AKP ve CHP arasındaki tartışma sürüyor.")
        assert "AKP" in _texts(ents)
        assert "CHP" in _texts(ents)
        assert all(t == EntityType.PARTY.value for t in _types(ents))

    def test_alias_resolves_to_canonical(self) -> None:
        ents = extract_entities("Cumhuriyet Halk Partisi bu kararı eleştirdi.")
        assert "CHP" in _texts(ents)

    def test_no_substring_match(self) -> None:
        # "akademi" must NOT match "AKP" — word-boundary discipline.
        ents = extract_entities("Bu bir akademik araştırmadır.")
        assert "AKP" not in _texts(ents)


class TestInstitutionDetection:
    def test_aym_canonical(self) -> None:
        ents = extract_entities("AYM kararı bugün açıklandı.")
        assert "AYM" in _texts(ents)

    def test_anayasa_mahkemesi_alias(self) -> None:
        ents = extract_entities("Anayasa Mahkemesi yeni bir karar verdi.")
        assert "AYM" in _texts(ents)
        assert EntityType.INSTITUTION.value in _types(ents)

    def test_tcmb_with_diacritics(self) -> None:
        ents = extract_entities("TCMB başkanı faiz kararını duyurdu.")
        assert "TCMB" in _texts(ents)


class TestPersonDetection:
    def test_full_name(self) -> None:
        ents = extract_entities("Recep Tayyip Erdoğan açılışta konuştu.")
        assert "Recep Tayyip Erdoğan" in _texts(ents)

    def test_surname_only(self) -> None:
        ents = extract_entities("Şimşek mali politikayı savundu.")
        assert "Mehmet Şimşek" in _texts(ents)


class TestCompanyDetection:
    def test_thy_short_form(self) -> None:
        ents = extract_entities("THY yeni hatlar açıklıyor.")
        assert "Türk Hava Yolları" in _texts(ents)

    def test_full_name(self) -> None:
        ents = extract_entities("Türk Telekom yeni yatırım planı sundu.")
        assert "Türk Telekom" in _texts(ents)


class TestTurkishLocaleFolding:
    def test_capital_dotted_i_matches_lowercase_i(self) -> None:
        # "İYİ Parti" written with mixed casing.
        ents = extract_entities("iyi parti milletvekilleri toplandı.")
        assert "İYİ Parti" in _texts(ents)

    def test_dotless_capital_I_handled(self) -> None:
        # "TIP" (not the abbreviation but as alias) should match TİP.
        ents = extract_entities("Türkiye İşçi Partisi seçim çağrısı yaptı.")
        assert "TİP" in _texts(ents)


class TestNonOverlapping:
    def test_longest_match_wins_at_a_given_start(self) -> None:
        # Both "Recep Tayyip Erdoğan" (canonical) and "Erdoğan" (alias)
        # match; the longer form covers the shorter.
        ents = extract_entities("Cumhurbaşkanı Recep Tayyip Erdoğan açıklama yaptı.")
        # Exactly one span per "Erdoğan" mention.
        names = [e for e in ents if e["text"] == "Recep Tayyip Erdoğan"]
        assert len(names) == 1

    def test_spans_are_within_text_length(self) -> None:
        text = "AKP ve CHP koalisyonu konuşuldu."
        ents = extract_entities(text)
        for ent in ents:
            start, end = ent["span"]
            assert 0 <= start < end <= len(text.translate(  # locale-folded length differs
                str.maketrans({"İ": "i", "I": "ı"})
            ).lower())


class TestEmptyInputs:
    def test_empty_string(self) -> None:
        assert extract_entities("") == []

    def test_none(self) -> None:
        assert extract_entities(None) == []

    def test_text_without_any_entity(self) -> None:
        assert extract_entities("Bugün hava çok güzeldi.") == []
