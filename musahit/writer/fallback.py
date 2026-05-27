"""Deterministic Python-rendered fallback briefing.

Per ADR-012 § Stage 6 Writer, if the writer model produces malformed
output after ``max_retries``, the pipeline switches to this renderer.
The output is structurally identical to what the writer is asked to
produce (same eight sections, same markers, same header) but the prose
is utilitarian · one-sentence summaries, no cross-band framing
discipline, no direct quotes. The operator gets a briefing; it's just
uglier than the LLM-generated version.

By construction, this renderer's output passes
:func:`musahit.writer.validator.validate_briefing_markdown` · the
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

# AÇIK GELİŞMELER · DEVAM EDEN TAKİP voicing cap. Per the 2026-05-24
# ADR-009 amendment: the first VOICED_OPEN_ARCS_CAP arcs (ordered by
# (peak_defcon ASC, last_update_at DESC)) go under "### Öne Çıkanlar"
# and are voiced by Piper. Any remaining arcs go under "### Diğer Açık
# Hikayeler" as a visual-only bullet list and are excluded from the
# TTS scope (see musahit/tts/extractor.py for the matching truncation).
VOICED_OPEN_ARCS_CAP: int = 10
_HIGHLIGHT_SUBSECTION_MARKER: str = "### Öne Çıkanlar"
_OTHER_SUBSECTION_MARKER: str = "### Diğer Açık Hikayeler"

# Arc-evolution markers (2026-05-25). The Güncelleme prefix introduces an
# active-today arc's evolved body using ``last_update_summary``. The
# stalled marker is dropped from voiced TTS scope by
# ``musahit/tts/extractor.py``; the Güncelleme prefix IS voiced. Kept here
# as module constants so tests and the TTS extractor can import the exact
# strings rather than re-deriving them.
ARC_UPDATE_PREFIX: str = "**Güncelleme** ·"
ARC_STALLED_MARKER: str = "*Bu arc'da bugün yeni gelişme yok.*"


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
    """Render AÇIK GELİŞMELER with the Öne Çıkanlar / Diğer split.

    The first ``VOICED_OPEN_ARCS_CAP`` arcs (sorted by
    ``(peak_defcon ASC, last_update_at DESC fallback created_at)``) go
    under ``### Öne Çıkanlar`` with full ``_render_arc`` blocks · this
    is the voiced subsection. Any remaining arcs go under
    ``### Diğer Açık Hikayeler`` as one-line bullets · visual-only,
    excluded from the TTS scope by ``musahit/tts/extractor.py``. If
    the total count is ``≤ VOICED_OPEN_ARCS_CAP``, only the highlight
    subsection is rendered.
    """
    if not payload.open_arc_updates:
        return _NO_ITEMS_TR

    ordered = sorted(payload.open_arc_updates, key=_arc_sort_key)
    highlighted = ordered[:VOICED_OPEN_ARCS_CAP]
    overflow = ordered[VOICED_OPEN_ARCS_CAP:]

    parts: list[str] = [_HIGHLIGHT_SUBSECTION_MARKER, ""]
    parts.append("\n\n".join(_render_arc(a) for a in highlighted))

    if overflow:
        parts.extend(["", _OTHER_SUBSECTION_MARKER, ""])
        for a in overflow:
            parts.append(_render_arc_overflow_bullet(a))

    return "\n".join(parts)


def _arc_sort_key(arc: ArcView) -> tuple[int, int, float]:
    """Active-then-severity-then-recency sort key for the voiced cap split.

    Returns ``(active_tier, peak_defcon, -epoch_seconds)`` so ``sorted(...,
    key=)`` ascending yields:

    1. ``active_tier`` (0 for active-today, 1 for stalled) · puts every
       active-today arc ahead of every stalled arc. This is the
       2026-05-25 arc-evolution priority rule · active arcs always
       compete for the voiced cap before stalled ones do.
    2. ``peak_defcon`` ascending · lower int = more severe wins.
    3. ``-epoch_seconds`` · most recent first within the same severity.

    Falls back to ``created_at`` when ``last_update_at`` is missing; both
    missing tiebreaks to epoch 0.0 (deterministic, predictable for tests).

    Backward-compat note: legacy fixtures that build ``ArcView`` without
    setting ``is_active_today`` default to ``False`` (stalled). The
    relative ordering within stalled or within active is unchanged from
    the pre-2026-05-25 ``(peak, -epoch)`` key, so prior tests that
    construct a single-tier list still pass.
    """
    active_tier = 0 if arc.is_active_today else 1
    dt = arc.last_update_at or arc.created_at
    epoch = dt.timestamp() if dt is not None else 0.0
    return (active_tier, int(arc.peak_defcon), -epoch)


def _render_arc_overflow_bullet(arc: ArcView) -> str:
    """One-line bullet form for ``### Diğer Açık Hikayeler``."""
    label = DEFCON_LABEL_TR.get(DEFCON(arc.peak_defcon), str(arc.peak_defcon))
    cat = arc.category or "SINIFLANDIRILMADI"
    headline = arc.headline or "(başlıksız)"
    return f"- {headline} · {label} · {cat} · `{arc.id}`"


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
    """Render one arc block · branches on ``is_active_today`` per arc-evolution.

    Three output shapes:

    * **Closing** (``closing=True``) · used for the RESOLVED arcs section.
      Standard header + seed summary + "Bu hikaye bugün kapatıldı." line.
      Not affected by ``is_active_today`` · a freshly resolved arc reads
      its closure note rather than an active/stalled tag.
    * **Active-today** (``is_active_today=True``) · standard header,
      then a Güncelleme prefix carrying ``last_update_summary`` (the most
      recent joining cluster's summary). Falls back to ``summary`` when
      ``last_update_summary`` is empty (migration 004 backfill makes this
      rare, but legacy fixtures without the field land here).
    * **Stalled** (open arc with ``is_active_today=False``) · standard
      header PLUS a Son güncelleme · X gün önce line so the operator
      sees how stale the story has become. Body is the seed ``summary``
      followed by the italic stalled marker
      (``*Bu arc'da bugün yeni gelişme yok.*``). The marker is voiced-
      excluded by ``musahit.tts.extractor`` so the operator hears the
      summary once and isn't told "no update today" verbally for every
      stalled arc · the visual marker is enough on the dashboard.
    """
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

    if closing:
        if arc.summary:
            lines.append("")
            lines.append(arc.summary)
        lines.append("")
        lines.append("Bu hikaye bugün kapatıldı.")
        return "\n".join(lines)

    if arc.is_active_today:
        update_body = arc.last_update_summary or arc.summary
        if update_body:
            lines.append("")
            lines.append(f"{ARC_UPDATE_PREFIX} {update_body}")
    else:
        # Stalled: add the Son güncelleme line into the header block, then
        # render seed summary + italic stalled marker as the body.
        lines.append(
            f"**Son güncelleme** · {arc.days_since_last_update} gün önce"
        )
        if arc.summary:
            lines.append("")
            lines.append(arc.summary)
        lines.append("")
        lines.append(ARC_STALLED_MARKER)
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


def render_section_stub(section_idx: int) -> str:
    """Render a placeholder stub for a section whose LLM generation failed."""
    section = TEMPLATE_SECTIONS[section_idx]
    return (
        f"{section.marker}\n\n"
        f"Bu bölüm üretilemedi · yedek metin kullanıldı."
    )


__all__ = [
    "ARC_STALLED_MARKER",
    "ARC_UPDATE_PREFIX",
    "VOICED_OPEN_ARCS_CAP",
    "render_fallback_briefing",
    "render_section_stub",
]
