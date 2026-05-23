"""Deterministic Python-rendered fallback briefing.

Per ADR-012 § Stage 6 Writer, if the writer model produces malformed
output after ``max_retries``, the pipeline switches to this renderer.
The output is structurally identical to what the writer is asked to
produce (same eight sections, same markers, same header) but the prose
is utilitarian — one-sentence summaries, no cross-band framing
discipline, no direct quotes. The operator gets a briefing; it's just
uglier than the LLM-generated version.

By construction, this renderer's output passes
:func:`musahit.writer.validator.validate_briefing_markdown` — the
section markers are taken directly from
:mod:`musahit.writer.template`.
"""

from __future__ import annotations

from datetime import date

from musahit.common.types import Category
from musahit.score.defcon import DEFCON, DEFCON_LABEL_TR
from musahit.writer.payload import (
    BUCKET_MATERIAL,
    BUCKET_PRIORITY,
    BUCKET_ROUTINE,
    ArcView,
    BriefingPayload,
    ClusterView,
)
from musahit.writer.template import DOCUMENT_TITLE, TEMPLATE_SECTIONS

_NO_ITEMS_TR = "Bugün bu bölümde öğe yok."


def render_fallback_briefing(payload: BriefingPayload) -> str:
    """Render a complete, validator-passing briefing markdown from ``payload``."""
    chunks: list[str] = [
        DOCUMENT_TITLE,
        "",
        _render_header(payload),
        "",
        "---",
    ]
    # The TEMPLATE_SECTIONS order is load-bearing; iterate and dispatch
    # to a section-specific renderer per marker.
    renderers = {
        "## ❯ DEFCON 1-2 · ÖNCELİKLİ": _render_priority,
        "## ❯ DEFCON 3 · MATERYAL": _render_material,
        "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP": _render_open_arcs,
        "## ❯ DEFCON 4 · GÜNDEM": _render_routine,
        "## ❯ DİKKAT · YALNIZCA SOSYALDE": _render_social_only,
        "## ❯ AMBİYANS · DEFCON 5": _render_ambient,
        "## ❯ KAPATILAN HİKAYELER": _render_resolved_arcs,
        "## ❯ SİSTEM LOG": _render_system_log,
    }
    for section in TEMPLATE_SECTIONS:
        chunks.extend(["", section.marker, ""])
        chunks.append(renderers[section.marker](payload))
        chunks.append("")
        chunks.append("---")
    return "\n".join(chunks).rstrip() + "\n"


def _render_header(payload: BriefingPayload) -> str:
    return "\n".join(
        [
            f"**Tarih** · {_format_date(payload.date)}",
            f"**İşlenen olay** · {payload.cluster_count}",
            f"**Açık hikaye** · {payload.open_arc_count}",
            f"**Zirve DEFCON** · {payload.peak_defcon}",
            f"**Run** · `{payload.run_id}`",
        ]
    )


def _render_priority(payload: BriefingPayload) -> str:
    clusters = _bucket(payload, BUCKET_PRIORITY)
    if not clusters:
        return _NO_ITEMS_TR
    return "\n\n".join(_render_full_cluster(c) for c in clusters)


def _render_material(payload: BriefingPayload) -> str:
    clusters = _bucket(payload, BUCKET_MATERIAL)
    if not clusters:
        return _NO_ITEMS_TR
    by_cat: dict[str, list[ClusterView]] = {}
    for c in clusters:
        by_cat.setdefault(c.category or Category.UNCLASSIFIED.value, []).append(c)
    parts: list[str] = []
    for cat, items in by_cat.items():
        parts.append(f"### {cat}")
        for c in items:
            parts.append(_render_compact_cluster(c))
    return "\n\n".join(parts)


def _render_open_arcs(payload: BriefingPayload) -> str:
    if not payload.open_arc_updates:
        return _NO_ITEMS_TR
    return "\n\n".join(_render_arc(a) for a in payload.open_arc_updates)


def _render_routine(payload: BriefingPayload) -> str:
    clusters = _bucket(payload, BUCKET_ROUTINE)
    if not clusters:
        return _NO_ITEMS_TR
    lines: list[str] = []
    for c in clusters:
        cat = c.category or "SINIFLANDIRILMADI"
        n = len(c.sources)
        arc = f" · {c.arc_id}" if c.arc_id else ""
        lines.append(f"- {c.headline or '(başlıksız)'} · {cat} · kaynaklar ({n}){arc}")
    return "\n".join(lines)


def _render_social_only(payload: BriefingPayload) -> str:
    clusters = [
        c
        for bucket in (BUCKET_PRIORITY, BUCKET_MATERIAL, BUCKET_ROUTINE)
        for c in _bucket(payload, bucket)
        if c.is_social_only
    ]
    if not clusters:
        return _NO_ITEMS_TR
    lines = [
        "Bu içerik yalnızca sosyalde tespit edildi · DEFCON 4 üst sınırı uygulanıyor.",
        "",
    ]
    for c in clusters:
        lines.append(f"- {c.headline or '(başlıksız)'}")
    return "\n".join(lines)


def _render_ambient(payload: BriefingPayload) -> str:
    n = payload.ambient_count
    return (
        f"{n} başlık ambiyans olarak işaretlendi · burada listelenmez · "
        "dashboard'da görüntülenebilir."
    )


def _render_resolved_arcs(payload: BriefingPayload) -> str:
    if not payload.resolved_arcs:
        return _NO_ITEMS_TR
    return "\n\n".join(_render_arc(a, closing=True) for a in payload.resolved_arcs)


def _render_system_log(payload: BriefingPayload) -> str:
    lines = [
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
    return "\n".join(lines)


# ── Cluster + arc renderers ────────────────────────────────────────────────


def _render_full_cluster(c: ClusterView) -> str:
    label = DEFCON_LABEL_TR.get(DEFCON(c.final_defcon), str(c.final_defcon))
    cat = c.category or "SINIFLANDIRILMADI"
    conf = c.confidence or "DÜŞÜK"
    lines = [
        f"### {c.headline or '(başlıksız)'}",
        f"**DEFCON** · {label} · **Kategori** · {cat} · **Güven** · {conf}",
    ]
    if c.arc_id:
        lines.append(f"**Arc** · {c.arc_id}")
    if c.summary:
        lines.append("")
        lines.append(c.summary)
    if c.sources:
        srcs = " · ".join(
            f"{s['source_id']}·{s['band']}" for s in c.sources
        )
        lines.append("")
        lines.append(f"**Kaynaklar** · {srcs}")
    return "\n".join(lines)


def _render_compact_cluster(c: ClusterView) -> str:
    conf = c.confidence or "DÜŞÜK"
    arc = f" · **Arc** · {c.arc_id}" if c.arc_id else ""
    lines = [
        f"#### {c.headline or '(başlıksız)'}",
        f"**Güven** · {conf}{arc}",
    ]
    if c.summary:
        lines.append("")
        lines.append(c.summary)
    return "\n".join(lines)


def _render_arc(arc: ArcView, *, closing: bool = False) -> str:
    label = DEFCON_LABEL_TR.get(DEFCON(arc.peak_defcon), str(arc.peak_defcon))
    cat = arc.category or "SINIFLANDIRILMADI"
    lines = [
        f"### {arc.headline or '(başlıksız)'} · {arc.id}",
    ]
    if arc.created_at is not None:
        lines.append(
            f"**Açıldı** · {_format_date(arc.created_at.date())} · "
            f"**Zirve DEFCON** · {label} · **Kategori** · {cat}"
        )
    else:
        lines.append(f"**Zirve DEFCON** · {label} · **Kategori** · {cat}")
    if arc.summary:
        lines.append("")
        lines.append(arc.summary)
    if closing:
        lines.append("")
        lines.append("Bu hikaye bugün kapatıldı.")
    return "\n".join(lines)


# ── Helpers ────────────────────────────────────────────────────────────────


def _bucket(payload: BriefingPayload, levels: tuple[int, ...]) -> list[ClusterView]:
    out: list[ClusterView] = []
    for level in levels:
        out.extend(payload.clusters_by_defcon.get(level, []))
    return out


_MONTHS_TR = [
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]


def _format_date(d: date) -> str:
    return f"{d.day} {_MONTHS_TR[d.month - 1]} {d.year}"


__all__ = ["render_fallback_briefing"]
