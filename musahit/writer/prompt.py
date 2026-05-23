"""Writer prompt construction.

The prompt is one big string with five parts:

1. System role (Turkish · neutral · resmi register · no opinions).
2. DEFCON schema reminder.
3. The template structure (with section markers preserved verbatim).
4. The day's data · clusters bucketed, open arcs, resolved arcs, log.
5. Output instruction (fill template; do not alter section markers).

Discipline rules are inlined per ADR-009 § Discipline rules. The
prompt does NOT include sources' full article bodies · only headlines,
summaries, and source/band tags · to keep total input under
Trendyol-LLM's 32K context window even on heavy days.

Estimated input size (worst case):

* System + schema + template + rules: ~3K tokens
* 50 priority/material clusters at ~150 tokens each: ~7.5K
* 20 routine clusters at ~50 tokens each: ~1K
* 10 open + 5 resolved arcs at ~100 tokens each: ~1.5K
* System log: ~200 tokens

Total: ~13K input tokens. Well under 32K; per ADR-009 § Negative,
"acceptable for Trendyol-LLM."
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
- Şablon yapısını DEĞİŞTİRME · bölüm başlıklarını harfi harfine koru."""

OUTPUT_INSTRUCTION = (
    "Aşağıdaki şablonu doldur. Bölüm başlıklarını ('## ❯ …') aynen koru. "
    "Hiçbir bölümü atlamak veya yeniden sıralamak yok. Eksik veri varsa "
    "ilgili bölümü kısa bir 'bugün öğe yok' notuyla geç."
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


# ── Top-level builder ──────────────────────────────────────────────────────


def build_writer_prompt(payload: BriefingPayload) -> str:
    """Compose the writer prompt for one nightly run."""
    return "\n\n".join(
        [
            SYSTEM_ROLE,
            _defcon_schema_block(),
            "ŞABLON:",
            _template_skeleton(),
            DISCIPLINE_RULES,
            "BUGÜNÜN İÇERİĞİ:",
            f"Tarih · {payload.date.isoformat()}",
            _clusters_data_block(payload),
            _arcs_data_block(payload),
            _system_log_block(payload),
            OUTPUT_INSTRUCTION,
            "ÇIKTI (markdown):",
        ]
    )


__all__ = [
    "DISCIPLINE_RULES",
    "OUTPUT_INSTRUCTION",
    "SYSTEM_ROLE",
    "build_writer_prompt",
]
