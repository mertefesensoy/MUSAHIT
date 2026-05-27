"""Writer prompt construction.

The prompt is one big string with six parts:

1. System role (Turkish · neutral · resmi register · no opinions).
2. DEFCON schema reminder.
3. Discipline rules (ADR-009).
4. The day's data · clusters bucketed, open arcs, resolved arcs, log.
5. Template skeleton (positioned at the end so it sits closest to the
   generation point — Trendyol-LLM 7B follows the structure more
   reliably when the skeleton is in its recency window).
6. Output instruction (fill template; do not alter section markers).

Discipline rules are inlined per ADR-009 § Discipline rules. The
prompt does NOT include sources' full article bodies · only headlines,
summaries, and source/band tags · to keep total input under
Trendyol-LLM's 32K context window even on heavy days.

Estimated input size (heavy day, 800+ clusters):

* System + schema + rules: ~2K tokens
* 200–300 priority/material clusters at ~60 tokens each: ~15K
* 400+ routine/ambient clusters at ~20 tokens each: ~8K
* 30 open + 10 resolved arcs at ~80 tokens each: ~3.2K
* Template skeleton + instructions: ~1.5K
* System log: ~300 tokens

Total: ~25–30K input tokens on heavy days. Within Trendyol-LLM's
32K context but with less margin than light days (~13K). Per ADR-009
§ Negative, "acceptable for Trendyol-LLM" — but at the upper bound.
"""

from __future__ import annotations

from collections.abc import Iterable

from musahit.common.types import Category
from musahit.score.defcon import DEFCON, DEFCON_ANCHORS, DEFCON_LABEL_TR
from musahit.writer.payload import (
    BUCKET_AMBIENT,
    BUCKET_MATERIAL,
    BUCKET_PRIORITY,
    BUCKET_ROUTINE,
    ArcView,
    BriefingPayload,
    ClusterView,
    FailedSource,
)
from musahit.writer.template import DOCUMENT_TITLE, TEMPLATE_SECTIONS

SYSTEM_ROLE = (
    "Sen MÜŞAHİT'in yazar modelisin. Türkiye gündemini tarafsız ve resmi "
    "Türkçeyle özetlersin. Görüş bildirmezsin. Olguları kaynak atıflarıyla "
    "sunarsın. Farklı bantlar farklı çerçeveler kullandığında ikisini de "
    "gösterirsin."
)

DISCIPLINE_RULES = """\
KURALLAR (ADR-009):
- Yorum yapma · sadece raporla.
- Bantlar farklı çerçeveledğinde her iki çerçeveyi de "KAYNAK·BAND" atıflarıyla göster.
- Doğrudan alıntı: olay başına en fazla 1 adet · 15 kelimenin altında · tırnak içinde · atıflı.
- Her öğede "Güven · YÜKSEK/ORTA/DÜŞÜK" göster.
- Bir arc'a bağlı her öğede arc_id göster.
- Resmi Türkçe register · argo yok · Türkçesi olan yerde İngilizce alıntı yok.
- Şablon yapısını DEĞİŞTİRME · bölüm başlıklarını harfi harfine koru.
- DEFCON ölçeği (1-5) ile şablon bölümleri (8 sabit bölüm) farklı şeylerdir \
· şablon bölümlerini bölme."""

TEMPLATE_LEAD_IN = (
    "AŞAĞIDAKİ ŞABLONDA TAM OLARAK 8 ÜST DÜZEY BÖLÜM VAR. "
    "YENİ BÖLÜM EKLEME · ALT BÖLÜM EKLEME · BÖLÜMLERİ BÖLME YOK. "
    "BAŞLIKLAR HARFI HARFINE KORUNACAK."
)

SECTION_ROSTER: str = "\n".join(
    ["GEÇERLİ BÖLÜMLER (yalnızca bunlar):"]
    + [f"{i + 1}. {s.marker}" for i, s in enumerate(TEMPLATE_SECTIONS)]
)

OUTPUT_INSTRUCTION = (
    "Yukarıdaki şablonu doldur. Bölüm başlıklarını ('## ❯ ...') aynen koru. "
    "Hiçbir bölümü atlamak veya yeniden sıralamak yok. Eksik veri varsa "
    "ilgili bölümü kısa bir 'bugün öğe yok' notuyla geç."
)

SECTION_INSTRUCTION_TEMPLATE = (
    "GÖREV · Aşağıdaki tek bir bölümü yaz · BAŞKA HİÇBİR BÖLÜM YAZMA.\n\n"
    "Hedef bölüm · {marker}\n"
)


# ── DEFCON schema reminder ─────────────────────────────────────────────────


def _defcon_schema_block() -> str:
    lines: list[str] = ["DEFCON ÖLÇEĞİ (ADR-004):"]
    for level in DEFCON:
        label = DEFCON_LABEL_TR[level]
        anchors = DEFCON_ANCHORS.get(level, ())
        anchor_preview = "; ".join(anchors[:2]) if anchors else ""
        lines.append(f"  {int(level)} · {label} · örnek: {anchor_preview}")
    return "\n".join(lines)


# ── Template skeleton ──────────────────────────────────────────────────────


def _template_skeleton() -> str:
    """Render the empty template skeleton fed to the writer LLM.

    Each section's prompt_instruction (from TemplateSection) is rendered
    under its marker. The 2026-05-23 smoke run showed a single literal
    placeholder caused Trendyol-LLM to echo the placeholder text verbatim
    in sections it found ambiguous; per-section instructions including
    the empty-state phrase eliminate the ambiguity. See
    ``docs/implementations/2026-05-24-template-placeholder-fix.md``.
    """
    parts: list[str] = [DOCUMENT_TITLE, ""]
    for section in TEMPLATE_SECTIONS:
        parts.extend(["", section.marker, "", section.prompt_instruction])
    return "\n".join(parts)


# ── Day's data ─────────────────────────────────────────────────────────────


def _cluster_block(cluster: ClusterView) -> str:
    lines: list[str] = [f"### {cluster.headline}".strip() or "### (başlıksız)"]
    label = DEFCON_LABEL_TR.get(DEFCON(cluster.final_defcon), str(cluster.final_defcon))
    conf = cluster.confidence or "DÜŞÜK"
    cat = cluster.category or "SINIFLANDIRILMADI"
    lines.append(
        f"DEFCON · {label} · Kategori · {cat} · Güven · {conf}"
    )
    if cluster.arc_id:
        lines.append(f"Arc · {cluster.arc_id}")
    if cluster.summary:
        lines.append(cluster.summary)
    if cluster.sources:
        src_strs = [f"{s['source_id']}·{s['band']}" for s in cluster.sources]
        lines.append("Kaynaklar · " + " · ".join(src_strs))
    if cluster.is_social_only:
        lines.append("(yalnızca sosyal · DEFCON 4 üst sınırı uygulanır)")
    return "\n".join(lines)


def _cluster_bucket(
    payload: BriefingPayload, levels: Iterable[int]
) -> list[ClusterView]:
    out: list[ClusterView] = []
    for level in levels:
        out.extend(payload.clusters_by_defcon.get(level, []))
    return out


def _clusters_data_block(payload: BriefingPayload) -> str:
    priority = _cluster_bucket(payload, BUCKET_PRIORITY)
    material = _cluster_bucket(payload, BUCKET_MATERIAL)
    routine = _cluster_bucket(payload, BUCKET_ROUTINE)
    ambient = _cluster_bucket(payload, BUCKET_AMBIENT)
    social_only = [
        c for c in priority + material + routine if c.is_social_only
    ]
    sections: list[str] = []
    sections.append("ÖNCELİKLİ (DEFCON 1-2):")
    if priority:
        sections.extend(_cluster_block(c) for c in priority)
    else:
        sections.append("(bugün öğe yok)")

    sections.append("\nMATERYAL (DEFCON 3):")
    if material:
        # group by category
        by_cat: dict[str, list[ClusterView]] = {}
        for c in material:
            by_cat.setdefault(c.category or Category.UNCLASSIFIED.value, []).append(c)
        for cat, clusters in by_cat.items():
            sections.append(f"### {cat}")
            sections.extend(_cluster_block(c) for c in clusters)
    else:
        sections.append("(bugün öğe yok)")

    sections.append("\nGÜNDEM (DEFCON 4):")
    if routine:
        for c in routine:
            cat = c.category or "SINIFLANDIRILMADI"
            srcs = "(" + str(len(c.sources)) + " kaynak)"
            arc = f" · {c.arc_id}" if c.arc_id else ""
            sections.append(f"- {c.headline} · {cat} · {srcs}{arc}")
    else:
        sections.append("(bugün öğe yok)")

    sections.append("\nYALNIZCA SOSYAL (DİKKAT bölümü için):")
    if social_only:
        for c in social_only:
            sections.append(f"- {c.headline}")
    else:
        sections.append("(bugün öğe yok)")

    # AMBİYANS bucket · added 2026-05-24 after the smoke-run echo bug ·
    # the section header was empty in the data block so the LLM had
    # nothing to fold into AMBİYANS · DEFCON 5 and echoed the placeholder
    # instead. See docs/implementations/2026-05-24-template-placeholder-fix.md.
    sections.append("\nAMBİYANS (DEFCON 5):")
    if ambient:
        for c in ambient:
            srcs = "(" + str(len(c.sources)) + " kaynak)"
            sections.append(f"- {c.headline} · {srcs}")
    else:
        sections.append("(bugün öğe yok)")

    return "\n".join(sections)


def _arcs_data_block(payload: BriefingPayload) -> str:
    sections: list[str] = ["AÇIK ARC GÜNCELLEMELERİ:"]
    if payload.open_arc_updates:
        for arc in payload.open_arc_updates:
            sections.append(_arc_block(arc))
    else:
        sections.append("(bugün güncelleme yok)")
    sections.append("\nBUGÜN KAPATILAN ARC'LAR:")
    if payload.resolved_arcs:
        for arc in payload.resolved_arcs:
            sections.append(_arc_block(arc))
    else:
        sections.append("(bugün kapatılan yok)")
    return "\n".join(sections)


def _arc_block(arc: ArcView) -> str:
    lines: list[str] = [f"### {arc.headline} · {arc.id}"]
    label = DEFCON_LABEL_TR.get(DEFCON(arc.peak_defcon), str(arc.peak_defcon))
    cat = arc.category or "SINIFLANDIRILMADI"
    lines.append(f"Zirve DEFCON · {label} · Kategori · {cat}")
    if arc.summary:
        lines.append(arc.summary)
    return "\n".join(lines)


def _system_log_block(payload: BriefingPayload) -> str:
    lines = [
        "SİSTEM LOG:",
        f"Çalıştırılan aşamalar · {' · '.join(payload.stages_done) or '(yok)'}",
        f"İşlenen olay · {payload.cluster_count}",
        f"Açık hikaye · {payload.open_arc_count}",
        f"Zirve DEFCON · {payload.peak_defcon}",
    ]
    if payload.failed_sources:
        lines.append("Başarısız kaynak:")
        for f in payload.failed_sources:
            lines.append(f"  - {f.source_id} · {f.status} · {f.error_detail}")
    else:
        lines.append("Başarısız kaynak · (yok)")
    return "\n".join(lines)


def _format_failed_sources(failed: list[FailedSource]) -> str:  # pragma: no cover
    return ", ".join(f"{f.source_id} ({f.status})" for f in failed)


# ── Per-section data builders ─────────────────────────────────────────────


def _section_data_priority(payload: BriefingPayload) -> str:
    clusters = _cluster_bucket(payload, BUCKET_PRIORITY)
    if not clusters:
        return "(bugün öğe yok)"
    return "\n".join(_cluster_block(c) for c in clusters)


def _section_data_material(payload: BriefingPayload) -> str:
    clusters = _cluster_bucket(payload, BUCKET_MATERIAL)
    if not clusters:
        return "(bugün öğe yok)"
    by_cat: dict[str, list[ClusterView]] = {}
    for c in clusters:
        by_cat.setdefault(c.category or Category.UNCLASSIFIED.value, []).append(c)
    sections: list[str] = []
    for cat, items in by_cat.items():
        sections.append(f"### {cat}")
        sections.extend(_cluster_block(c) for c in items)
    return "\n".join(sections)


def _section_data_open_arcs(payload: BriefingPayload) -> str:
    if not payload.open_arc_updates:
        return "(bugün güncelleme yok)"
    return "\n".join(_arc_block(arc) for arc in payload.open_arc_updates)


def _section_data_routine(payload: BriefingPayload) -> str:
    clusters = _cluster_bucket(payload, BUCKET_ROUTINE)
    if not clusters:
        return "(bugün öğe yok)"
    lines: list[str] = []
    for c in clusters:
        cat = c.category or "SINIFLANDIRILMADI"
        srcs = "(" + str(len(c.sources)) + " kaynak)"
        arc = f" · {c.arc_id}" if c.arc_id else ""
        lines.append(f"- {c.headline} · {cat} · {srcs}{arc}")
    return "\n".join(lines)


def _section_data_social_only(payload: BriefingPayload) -> str:
    social_only = [
        c
        for levels in (BUCKET_PRIORITY, BUCKET_MATERIAL, BUCKET_ROUTINE)
        for c in _cluster_bucket(payload, levels)
        if c.is_social_only
    ]
    if not social_only:
        return "(bugün öğe yok)"
    return "\n".join(f"- {c.headline}" for c in social_only)


def _section_data_ambient(payload: BriefingPayload) -> str:
    clusters = _cluster_bucket(payload, BUCKET_AMBIENT)
    if not clusters:
        return "(bugün öğe yok)"
    lines: list[str] = []
    for c in clusters:
        srcs = "(" + str(len(c.sources)) + " kaynak)"
        lines.append(f"- {c.headline} · {srcs}")
    return "\n".join(lines)


def _section_data_resolved_arcs(payload: BriefingPayload) -> str:
    if not payload.resolved_arcs:
        return "(bugün kapatılan yok)"
    return "\n".join(_arc_block(arc) for arc in payload.resolved_arcs)


_SECTION_DATA_BUILDERS = {
    0: _section_data_priority,
    1: _section_data_material,
    2: _section_data_open_arcs,
    3: _section_data_routine,
    4: _section_data_social_only,
    5: _section_data_ambient,
    6: _section_data_resolved_arcs,
}


# ── Top-level builder ──────────────────────────────────────────────────────


def build_writer_system() -> str:
    """Return the system role string for the writer LLM."""
    return SYSTEM_ROLE


def build_section_user(payload: BriefingPayload, section_idx: int) -> str:
    """Compose the user-message text for a single-section writer call.

    The returned text contains ONLY the data needed for the section at
    section_idx · plus the DISCIPLINE_RULES · plus a short reminder that
    only this section is being written.

    The section's marker is NOT in the returned text · it is sent to
    the LLM as the prefilled assistant message by the orchestrator.
    """
    if section_idx == 7:
        raise ValueError(
            "Section 7 (SİSTEM LOG) is deterministic; use build_system_log_section"
        )
    section = TEMPLATE_SECTIONS[section_idx]
    data_block = _SECTION_DATA_BUILDERS[section_idx](payload)
    return "\n\n".join([
        SECTION_INSTRUCTION_TEMPLATE.format(marker=section.marker),
        DISCIPLINE_RULES,
        f"BÖLÜM VERİSİ:\n{data_block}",
        "ÇIKTI (yalnızca bu bölümün içeriği · marker hazır verilmiştir):",
    ])


def build_system_log_section(
    payload: BriefingPayload,
    failed_section_indices: list[int],
) -> str:
    """Render the SİSTEM LOG section directly (not via LLM).

    The SİSTEM LOG is structured metadata · not creative prose · so
    the writer composes it deterministically rather than asking the
    LLM. This also lets the section faithfully report which other
    sections fell back to stubs.
    """
    marker = TEMPLATE_SECTIONS[7].marker
    lines = [
        marker,
        "",
        f"**Run** · `{payload.run_id}`",
        f"**Çalıştırılan aşamalar** · {' · '.join(payload.stages_done) or '(yok)'}",
        f"**İşlenen olay** · {payload.cluster_count}",
        f"**Açık hikaye** · {payload.open_arc_count}",
        f"**Toplam arc** · {payload.arc_count}",
        f"**Zirve DEFCON** · {payload.peak_defcon}",
    ]
    if payload.failed_sources:
        names = " · ".join(
            f"{f.source_id} ({f.status})" for f in payload.failed_sources
        )
        lines.append(f"**Başarısız kaynak** · {names}")
    else:
        lines.append("**Başarısız kaynak** · (yok)")
    if failed_section_indices:
        titles = " · ".join(
            TEMPLATE_SECTIONS[i].marker.removeprefix("## ❯ ")
            for i in failed_section_indices
        )
        lines.append(f"**Başarısız bölüm üretimi** · {titles}")
    return "\n".join(lines)


def build_writer_user(payload: BriefingPayload) -> str:
    """Deprecated · use build_section_user for per-section generation."""
    return "\n\n".join(
        [
            _defcon_schema_block(),
            DISCIPLINE_RULES,
            "BUGÜNÜN İÇERİĞİ:",
            f"Tarih · {payload.date.isoformat()}",
            _clusters_data_block(payload),
            _arcs_data_block(payload),
            _system_log_block(payload),
            TEMPLATE_LEAD_IN,
            SECTION_ROSTER,
            _template_skeleton(),
            OUTPUT_INSTRUCTION,
            "ÇIKTI (markdown):",
        ]
    )


def build_writer_prompt(payload: BriefingPayload) -> str:
    """Deprecated · use build_writer_system + build_section_user instead."""
    return f"{build_writer_system()}\n\n{build_writer_user(payload)}"


__all__ = [
    "DISCIPLINE_RULES",
    "OUTPUT_INSTRUCTION",
    "SECTION_INSTRUCTION_TEMPLATE",
    "SECTION_ROSTER",
    "SYSTEM_ROLE",
    "TEMPLATE_LEAD_IN",
    "build_section_user",
    "build_system_log_section",
    "build_writer_prompt",
    "build_writer_system",
    "build_writer_user",
]
