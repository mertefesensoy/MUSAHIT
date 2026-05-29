"""Deterministic renderers for the itemized briefing sections.

Per the 2026-05-29 Group-A decision (D4) the five itemized sections —
AÇIK GELİŞMELER, DEFCON 4 · GÜNDEM, DİKKAT · YALNIZCA SOSYALDE,
AMBİYANS · DEFCON 5, KAPATILAN HİKAYELER — are rendered here from
payload data, **never via the LLM**. A section that makes no LLM call
cannot hallucinate, which kills the Mode-4 narrative fabrication (the
İş Bankası loan-package and Guantanamo-essay specimens) by construction
and removes five LLM calls per run. Only DEFCON 1-2 and DEFCON 3 — whose
structured cluster prose has stayed faithful — remain LLM-generated.

Each ``render_*`` function returns the section **body** (the lines that
go under the marker, no marker line) or ``None`` when the section is
empty. The briefer wraps the body under the section marker, or emits the
canonical "Bugün bu bölümde öğe yok." note when ``None`` — preserving the
existing empty-section short-circuit.

Recency · every itemized line carries a trailing Turkish recency suffix
(`· bugün` / `· dün` / `· N gün önce`) from
:func:`musahit.arcs.freshness.recency_label`, computed against the
briefing date in the payload loader. This both informs the human reader
and is the exact signal the TTS preprocessor keys off to drop dormant
lines from the spoken briefing.

Surfacing rules (the freshness axis, orthogonal to DEFCON severity):

* AÇIK GELİŞMELER lists OPEN arcs sorted **freshest-first**. EXPIRED arcs
  (idle ≥ ``EXPIRE_DAYS``) are excluded — the lifecycle pass resolves
  them, and the renderer drops any that slipped through. DORMANT arcs
  (2-6 days) stay, visibly marked by their recency suffix.
* DEFCON 4 / AMBİYANS list **this run's** clusters (today's agenda), so
  no freshness exclusion applies; the recency suffix reflects the linked
  arc's age so a routine update to a stale thread reads its true age.
"""

from __future__ import annotations

from musahit.arcs.freshness import Freshness, recency_label
from musahit.common.types import Category
from musahit.score.defcon import DEFCON, DEFCON_LABEL_TR
from musahit.writer.payload import (
    BUCKET_AMBIENT,
    BUCKET_MATERIAL,
    BUCKET_PRIORITY,
    BUCKET_ROUTINE,
    ArcView,
    BriefingPayload,
    ClusterView,
)


def _suffix(days_since: int) -> str:
    """Trailing recency suffix `· bugün|dün|N gün önce` for a line."""
    return f" · {recency_label(days_since)}"


def _defcon_label(defcon: int) -> str:
    return DEFCON_LABEL_TR.get(DEFCON(defcon), str(defcon))


def _bucket(payload: BriefingPayload, levels: tuple[int, ...]) -> list[ClusterView]:
    out: list[ClusterView] = []
    for level in levels:
        out.extend(payload.clusters_by_defcon.get(level, []))
    return out


# ── AÇIK GELİŞMELER · DEVAM EDEN TAKİP (open arcs) ─────────────────────────


# Voiced-cap split (re-used from the per-section design). The first
# VOICED_OPEN_ARCS_CAP arcs (freshest-first) go under "### Öne Çıkanlar"
# and are voiced; the rest go under "### Diğer Açık Hikayeler" as a
# markdown-only overflow. The TTS extractor truncates the voiced scope at
# the overflow marker, so the spoken briefing never tries to read hundreds
# of open-arc lines in one Piper call (a 356-line list overran Piper's
# per-chunk timeout on the 2026-05-29 live run). The dormancy skip still
# applies to whatever survives into the voiced highlight block, so a
# highlight block that is all-dormant collapses to the spoken note.
VOICED_OPEN_ARCS_CAP: int = 10
_HIGHLIGHT_MARKER: str = "### Öne Çıkanlar"
# MUST match musahit.tts.extractor._DIGER_MARKER exactly · the extractor
# keys its voiced-scope truncation off this literal.
_OVERFLOW_MARKER: str = "### Diğer Açık Hikayeler"


def _open_arc_sort_key(arc: ArcView) -> tuple[int, int, str]:
    """Freshest-first: (days_since ASC, peak_defcon ASC, id ASC).

    Smaller day-count first (today before yesterday before 6 days ago);
    ties broken by severity (lower DEFCON int = more severe wins) then
    arc id for deterministic ordering.
    """
    return (arc.days_since_last_update, int(arc.peak_defcon), arc.id)


def _open_arc_line(arc: ArcView) -> str:
    return (
        f"- {arc.headline or '(başlıksız)'} · {_defcon_label(arc.peak_defcon)}"
        f" · {arc.category or Category.UNCLASSIFIED.value}"
        f" · `{arc.id}`{_suffix(arc.days_since_last_update)}"
    )


def render_open_arcs(payload: BriefingPayload) -> str | None:
    """Itemized OPEN-arc list, freshest-first, EXPIRED excluded.

    Returns ``None`` when there is no FRESH or DORMANT open arc (an
    all-EXPIRED or genuinely-empty section → the empty-state note).

    When more than :data:`VOICED_OPEN_ARCS_CAP` arcs surface, the list is
    split into a voiced ``### Öne Çıkanlar`` highlight block (the freshest
    ``VOICED_OPEN_ARCS_CAP``) and a markdown-only ``### Diğer Açık
    Hikayeler`` overflow; the markdown keeps every arc either way.
    """
    surfaced = sorted(
        (a for a in payload.open_arc_updates if a.freshness != Freshness.EXPIRED.value),
        key=_open_arc_sort_key,
    )
    if not surfaced:
        return None
    if len(surfaced) <= VOICED_OPEN_ARCS_CAP:
        return "\n".join(_open_arc_line(a) for a in surfaced)

    highlight = surfaced[:VOICED_OPEN_ARCS_CAP]
    overflow = surfaced[VOICED_OPEN_ARCS_CAP:]
    parts = [_HIGHLIGHT_MARKER, ""]
    parts.extend(_open_arc_line(a) for a in highlight)
    parts.extend(["", _OVERFLOW_MARKER, ""])
    parts.extend(_open_arc_line(a) for a in overflow)
    return "\n".join(parts)


# ── DEFCON 4 · GÜNDEM (routine clusters) ───────────────────────────────────


def _cluster_sort_key(cluster: ClusterView) -> tuple[int, str]:
    return (cluster.days_since_last_update, cluster.id)


def render_routine(payload: BriefingPayload) -> str | None:
    """DEFCON-4 routine clusters as a freshest-first bullet list.

    Line format: ``- başlık · KATEGORİ · (N kaynak) · arc_id · recency``
    (arc_id omitted when the cluster is not yet linked). These are this
    run's clusters — today's agenda — so no freshness exclusion applies;
    the recency suffix surfaces the linked arc's true age.
    """
    clusters = _bucket(payload, BUCKET_ROUTINE)
    if not clusters:
        return None
    lines: list[str] = []
    for c in sorted(clusters, key=_cluster_sort_key):
        cat = c.category or "SINIFLANDIRILMADI"
        srcs = f"({len(c.sources)} kaynak)"
        arc = f" · {c.arc_id}" if c.arc_id else ""
        lines.append(
            f"- {c.headline or '(başlıksız)'} · {cat} · {srcs}{arc}"
            f"{_suffix(c.days_since_last_update)}"
        )
    return "\n".join(lines)


# ── DİKKAT · YALNIZCA SOSYALDE (social-only clusters) ──────────────────────


def render_social_only(payload: BriefingPayload) -> str | None:
    """Social-only headlines as a bullet list with the DEFCON-4-cap note."""
    social = [
        c
        for levels in (BUCKET_PRIORITY, BUCKET_MATERIAL, BUCKET_ROUTINE)
        for c in _bucket(payload, levels)
        if c.is_social_only
    ]
    if not social:
        return None
    lines = [
        "Bu içerik yalnızca sosyalde tespit edildi · DEFCON 4 üst sınırı uygulanıyor.",
        "",
    ]
    for c in sorted(social, key=_cluster_sort_key):
        lines.append(
            f"- {c.headline or '(başlıksız)'} · (yalnızca sosyal)"
            f"{_suffix(c.days_since_last_update)}"
        )
    return "\n".join(lines)


# ── AMBİYANS · DEFCON 5 (ambient clusters) ─────────────────────────────────


def render_ambient(payload: BriefingPayload) -> str | None:
    """DEFCON-5 ambient clusters as a freshest-first bullet list.

    Replaces the prior LLM section (the Guantanamo-essay fabrication
    source) with an itemized data list · ``- başlık · (N kaynak) · recency``.
    """
    clusters = _bucket(payload, BUCKET_AMBIENT)
    if not clusters:
        return None
    lines = [
        f"- {c.headline or '(başlıksız)'} · ({len(c.sources)} kaynak)"
        f"{_suffix(c.days_since_last_update)}"
        for c in sorted(clusters, key=_cluster_sort_key)
    ]
    return "\n".join(lines)


# ── KAPATILAN HİKAYELER (resolved arcs) ────────────────────────────────────


def render_resolved(payload: BriefingPayload) -> str | None:
    """Resolved arcs as a bullet list · ``- başlık · DEFCON · KAT · arc_id · kapatıldı``."""
    if not payload.resolved_arcs:
        return None
    lines = [
        f"- {arc.headline or '(başlıksız)'} · {_defcon_label(arc.peak_defcon)}"
        f" · {arc.category or Category.UNCLASSIFIED.value}"
        f" · `{arc.id}` · kapatıldı"
        for arc in payload.resolved_arcs
    ]
    return "\n".join(lines)


# ── Dispatch ───────────────────────────────────────────────────────────────

# Section indices (into TEMPLATE_SECTIONS) rendered deterministically and
# the renderer for each. 0 (DEFCON 1-2) and 1 (DEFCON 3) stay LLM; 7
# (SİSTEM LOG) has always been deterministic via build_system_log_section.
_DETERMINISTIC_RENDERERS = {
    2: render_open_arcs,
    3: render_routine,
    4: render_social_only,
    5: render_ambient,
    6: render_resolved,
}

DETERMINISTIC_SECTION_INDICES: tuple[int, ...] = tuple(_DETERMINISTIC_RENDERERS)


def render_deterministic_section(payload: BriefingPayload, idx: int) -> str | None:
    """Render the body for deterministic section ``idx`` (``None`` if empty)."""
    return _DETERMINISTIC_RENDERERS[idx](payload)


__all__ = [
    "DETERMINISTIC_SECTION_INDICES",
    "VOICED_OPEN_ARCS_CAP",
    "render_ambient",
    "render_deterministic_section",
    "render_open_arcs",
    "render_resolved",
    "render_routine",
    "render_social_only",
]
