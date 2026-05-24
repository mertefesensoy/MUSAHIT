"""Canonical template structure per ADR-009.

The briefing markdown is a fixed sequence of top-level (``##``) sections,
each prefixed with the ``❯`` marker that downstream stages (validator,
TTS scope extractor, dashboard renderer) key off. Adding or removing a
section is an ADR-009 amendment, not a silent edit.

The DEFCON-3 section nests one ``###`` subsection per category. Other
sections may have ``###`` subsections too (one per item) but those are
content, not structure · the validator does not pin them.

Each section also carries a ``prompt_instruction`` string fed to the
writer LLM under the section's marker in the skeleton. This replaced
the single literal placeholder ``[içerik buraya · şablon talimatlarına
bak]`` after the 2026-05-23 smoke run showed Trendyol-LLM echoed the
placeholder verbatim into AÇIK GELİŞMELER and AMBİYANS · DEFCON 5.
Per-section instructions including the section's empty-state phrase
give the model an unambiguous fallback. See
``docs/implementations/2026-05-24-template-placeholder-fix.md``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TemplateSection:
    """One required top-level section.

    ``prompt_instruction`` is the Turkish guidance text the writer LLM
    sees under this section's marker in the skeleton. It MUST include
    the section's empty-state phrase (``(bugün öğe yok)`` or
    equivalent) so an ambiguous section never produces an echoed
    placeholder.
    """

    marker: str  # the exact line text, e.g. "## ❯ DEFCON 1-2 · ÖNCELİKLİ"
    name: str  # short identifier for logs / error messages
    prompt_instruction: str  # Turkish writer-prompt instruction for this section


# Ordered per ADR-009 § Master template. The order is load-bearing ·
# the briefing's skim-and-stop discipline depends on DEFCON 1-2 being
# first and SİSTEM LOG last.
TEMPLATE_SECTIONS: tuple[TemplateSection, ...] = (
    TemplateSection(
        marker="## ❯ DEFCON 1-2 · ÖNCELİKLİ",
        name="defcon_1_2",
        prompt_instruction=(
            "ÖNCELİKLİ olayları (DEFCON 1-2) sırala · her olay için başlık · "
            "DEFCON · Kategori · Güven · arc_id · kaynaklar · özet. "
            "Veri yoksa: \"(bugün öğe yok)\"."
        ),
    ),
    TemplateSection(
        marker="## ❯ DEFCON 3 · MATERYAL",
        name="defcon_3",
        prompt_instruction=(
            "MATERYAL olayları (DEFCON 3) kategoriye göre alt başlıklara böl "
            "(### POLİTİKA, ### EKONOMİ, ### YARGI, ### GÜVENLİK, ### DİPLOMASİ, "
            "### MEVZUAT, ### TOPLUM · sadece bugün veri olan kategoriler). "
            "Her olay için başlık · DEFCON · Güven · arc_id · kaynaklar · özet. "
            "Veri yoksa: \"(bugün öğe yok)\"."
        ),
    ),
    TemplateSection(
        marker="## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP",
        name="open_arcs",
        prompt_instruction=(
            "Bugün güncellenen açık arc'ları iki alt başlığa böl. "
            "İlk olarak \"### Öne Çıkanlar\" (en fazla 10 arc · peak_defcon "
            "küçükten büyüğe sırala, eşitlikte en son güncellenen önce) · "
            "her arc için \"### başlık · arc_id\" satırı ardından açıldı "
            "tarihi, zirve DEFCON, kategori ve tek paragraf güncelleme özeti. "
            "Sonra \"### Diğer Açık Hikayeler\" (kalan arc'lar · tek satırlık "
            "madde işareti listesi · biçim: \"- başlık · DEFCON · kategori · "
            "`arc_id`\"). Toplam 10 veya daha az arc varsa yalnızca Öne "
            "Çıkanlar bölümünü yaz · Diğer Açık Hikayeler bölümü hiç "
            "olmasın. Veri yoksa: \"(bugün güncelleme yok)\"."
        ),
    ),
    TemplateSection(
        marker="## ❯ DEFCON 4 · GÜNDEM",
        name="defcon_4",
        prompt_instruction=(
            "GÜNDEM olaylarını (DEFCON 4) tek satırlık madde işareti listesi "
            "olarak sun · biçim: \"- başlık · kategori · (N kaynak) · arc_id\". "
            "Veri yoksa: \"(bugün öğe yok)\"."
        ),
    ),
    TemplateSection(
        marker="## ❯ DİKKAT · YALNIZCA SOSYALDE",
        name="social_only",
        prompt_instruction=(
            "YALNIZCA sosyal kaynaklarda (X, Reddit) yer alan ve henüz başka "
            "banttan teyitlenmemiş başlıkları madde işareti listesi olarak ver · "
            "her satırın sonuna \"(yalnızca sosyal · DEFCON 4 üst sınırı uygulanır)\" "
            "notu ekle. Veri yoksa: \"(bugün öğe yok)\"."
        ),
    ),
    TemplateSection(
        marker="## ❯ AMBİYANS · DEFCON 5",
        name="ambient",
        prompt_instruction=(
            "Düşük öncelikli (DEFCON 5) gündem başlıklarını kısa madde "
            "işareti listesi olarak sun · sadece başlık + kaynak sayısı. "
            "Veri yoksa: \"(bugün öğe yok)\"."
        ),
    ),
    TemplateSection(
        marker="## ❯ KAPATILAN HİKAYELER",
        name="resolved_arcs",
        prompt_instruction=(
            "Bugün RESOLVED'a geçen arc'ları sırala · her arc için "
            "### başlık · arc_id · zirve DEFCON · kategori · kapanış özeti. "
            "Veri yoksa: \"(bugün kapatılan yok)\"."
        ),
    ),
    TemplateSection(
        marker="## ❯ SİSTEM LOG",
        name="system_log",
        prompt_instruction=(
            "Çalıştırma metadatasını payload'daki SİSTEM LOG bloğundan birebir "
            "yansıt · çalıştırılan aşamalar · işlenen olay · açık hikaye · "
            "zirve DEFCON · başarısız kaynak listesi. Yorum ekleme."
        ),
    ),
)

# Headline of the briefing · the validator allows this top-level "# "
# heading before the first "## ❯ " section.
DOCUMENT_TITLE: str = "# MÜŞAHİT · GÜNLÜK BRİF"

# Category subsection markers under DEFCON 3. Order documented in ADR-009.
DEFCON_3_CATEGORY_SUBSECTIONS: tuple[str, ...] = (
    "### POLİTİKA",
    "### EKONOMİ",
    "### YARGI",
    "### GÜVENLİK",
    "### DİPLOMASİ",
    "### MEVZUAT",
    "### TOPLUM",
)


__all__ = [
    "DEFCON_3_CATEGORY_SUBSECTIONS",
    "DOCUMENT_TITLE",
    "TEMPLATE_SECTIONS",
    "TemplateSection",
]
