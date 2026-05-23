# ============================================================================
# FILE-PROTECTED · musahit/score/defcon.py
# Modifications require an ADR amendment + explicit operator override.
# See BOOTSTRAP.md § File protection list and ADR-004.
# ============================================================================
"""DEFCON schema — the six-level severity ladder from ADR-004.

This module is the **single source of truth** for the DEFCON ladder. The
prompt builder reads :data:`DEFCON_ANCHORS` to inject the calibration
examples into the worker prompt; the promotion module reads
:class:`DEFCON` for the enum values; the briefing renderer reads
:data:`DEFCON_LABEL_TR` for Turkish display strings. Any code that needs
to talk about DEFCON imports from here — no enum duplication, no string
literals scattered across the codebase.

Why FILE-PROTECTED:

* The ladder is operator-curated (ADR-004 § Anchor examples) and reflects
  a deliberate editorial reading of Turkish history. Changes are
  authored, not refactored.
* The promotion rules in ADR-005 are coupled to the enum values (numeric
  ordering matters for the `min(raw, ceiling)` formula). Any reorder is
  a contract change.
* The briefing template, dashboard tabs, and audit log all key off these
  values. Silent additions break the rest of the pipeline.

To amend (add a level, change a label, refine an anchor), write a
follow-up ADR superseding ADR-004 and reference it in the operator
override that lands the edit.
"""

from __future__ import annotations

from enum import IntEnum


class DEFCON(IntEnum):
    """Discrete six-level severity ladder.

    Lower number = more severe. The integer values are load-bearing for
    :func:`musahit.score.promotion.final_defcon` (the formula is
    ``min(raw, ceiling)``).
    """

    UNTHINKABLE = 0
    ACUTE = 1
    SEVERE = 2
    MATERIAL = 3
    ROUTINE = 4
    AMBIENT = 5


# Turkish display strings exactly as listed in ADR-004 § Schema constants.
DEFCON_LABEL_TR: dict[DEFCON, str] = {
    DEFCON.UNTHINKABLE: "DÜŞÜNÜLEMEZ",
    DEFCON.ACUTE: "AKUT",
    DEFCON.SEVERE: "ŞİDDETLİ",
    DEFCON.MATERIAL: "MATERYAL",
    DEFCON.ROUTINE: "GÜNDEM",
    DEFCON.AMBIENT: "AMBİYANS",
}


# Levels that require a manual operator override before they can land
# in the briefing. UNTHINKABLE is gated independently of any other rule;
# the writer/dashboard checks this set before serialising.
DEFCON_REQUIRES_OVERRIDE: frozenset[DEFCON] = frozenset({DEFCON.UNTHINKABLE})


# Anchor examples per level — verbatim from ADR-004. The prompt builder
# injects these into the worker's classification prompt so the model has
# concrete Turkey-specific reference points.
DEFCON_ANCHORS: dict[DEFCON, tuple[str, ...]] = {
    DEFCON.UNTHINKABLE: (
        "Anayasal düzenin yıkılması · hükümetin feshi · askeri yönetim kurulması",
        "Akkuyu nükleer kazası · radyoaktif sızıntı",
        "Komşu ülkeyle (Yunanistan · Suriye) doğrudan sıcak savaş ya da Rusya ile",
        "NATO'dan resmi çıkış",
        "Devlet-paralizi düzeyinde kitlesel terör saldırısı (>500 kurban)",
        "Cumhurbaşkanına suikast",
        "Hiperenflasyona geçiş · TL'nin resmen terk edilmesi · para birimi değişimi",
        "Sivil savaş ya da silahlı ayaklanmanın 72 saatten uzun sürmesi",
    ),
    DEFCON.ACUTE: (
        "15 Temmuz 2016 darbe girişimi",
        "12 Eylül 1980 darbesi",
        "1971 muhtırası",
    ),
    DEFCON.SEVERE: (
        "İmamoğlu mahkumiyet ve hapis cezası",
        "23 Haziran 2019 İstanbul yenileme seçimi",
        "CHP genel merkez baskını (2025)",
        "HDP kapatma davası",
        "2018 Ağustos kur krizi · 500+ bps acil faiz hareketi",
    ),
    DEFCON.MATERIAL: (
        "İstanbul Sözleşmesi'nden çekilme",
        "S-400 ABD yaptırım haberleri",
        "TCMB sürpriz 500 baz puan faiz kararı",
        "Berat Albayrak istifası",
    ),
    DEFCON.ROUTINE: (
        "Aylık enflasyon açıklaması",
        "Standart kabine toplantısı özeti",
        "Düzenli meclis komisyonu çıktıları",
    ),
    DEFCON.AMBIENT: (
        "Köşe yazarı yorumu",
        "Rutin parti kongresi",
        "Altta haber kaynağı bulunmayan X viralliği",
        "Birincil kaynak olmayan Reddit ipliği",
    ),
}


__all__ = [
    "DEFCON",
    "DEFCON_ANCHORS",
    "DEFCON_LABEL_TR",
    "DEFCON_REQUIRES_OVERRIDE",
]
