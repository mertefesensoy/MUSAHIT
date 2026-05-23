"""Prompt builder for the worker classification call.

Single function: :func:`build_classification_prompt`. Reads the DEFCON
ladder, labels, and anchor examples from :mod:`musahit.score.defcon`
(the single source of truth) so a future ADR amendment to the ladder
or the anchors automatically propagates to every classifier call.

The prompt structure:

1. Role + task framing (Turkish OSINT classification).
2. DEFCON ladder with full anchor examples per level.
3. Output-format instruction (strict JSON matching WorkerResponse).
4. Bias-handling rule (raw DEFCON only; promotion is downstream).
5. The cluster's articles (title + lead per member).

The prompt is deterministic for a given cluster — no clocks, no random
strings, no per-run salt. That keeps the FakeLlmClient's substring-
keyed response map workable for tests.
"""

from __future__ import annotations

from dataclasses import dataclass

from musahit.common.types import Category
from musahit.score.defcon import DEFCON, DEFCON_ANCHORS, DEFCON_LABEL_TR


@dataclass(frozen=True)
class ClusterArticle:
    """Minimum article data needed for the prompt."""

    source_id: str
    band: str  # display string from Band.value
    title: str
    lead: str


def _format_anchors() -> str:
    lines: list[str] = []
    for level in DEFCON:
        label = DEFCON_LABEL_TR[level]
        lines.append(f"\nDEFCON {int(level)} · {label}")
        for anchor in DEFCON_ANCHORS.get(level, ()):
            lines.append(f"  - {anchor}")
    return "\n".join(lines)


def _format_categories() -> str:
    """ASCII-friendly list of category names for the JSON output spec."""
    return ", ".join(f'"{c.value}"' for c in Category)


def _format_articles(articles: list[ClusterArticle]) -> str:
    chunks: list[str] = []
    for i, a in enumerate(articles, start=1):
        chunks.append(
            f"--- KAYNAK {i} ({a.source_id} · {a.band}) ---\n"
            f"BAŞLIK: {a.title}\n"
            f"GİRİŞ: {a.lead}\n"
        )
    return "\n".join(chunks)


PROMPT_TEMPLATE = """\
SEN, Türkiye odaklı bir OSINT brifing sisteminin sınıflandırma çalışanısın.
Görevin: bir küme makaleyi okuyup tek bir JSON nesnesi üretmek.

== DEFCON ÖLÇEĞİ ==
{anchors}

== ÖNEMLİ ==
- Sadece RAW DEFCON'u üret. Yayın yelpazesine göre tavan uygulaması (ADR-005)
  bu adımın dışında yapılır; sen yalnızca olayın doğal şiddetini puanla.
- Cevap MUTLAKA aşağıdaki şemaya uyan tek bir JSON nesnesi olmalı. JSON
  öncesi/sonrası serbest metin yazma.

== ÇIKTI ŞEMASI ==
{{
  "defcon": 0..5,
  "category": one of {categories},
  "confidence_self": one of "high", "medium", "low",
  "entities": ["entity 1", "entity 2", ...],
  "summary": "Tek paragraf · en fazla 500 karakter",
  "headline": "Kısa Türkçe başlık · en fazla 200 karakter"
}}

== KÜME İÇERİĞİ ==
{articles}

JSON çıktısı:
"""


def build_classification_prompt(
    articles: list[ClusterArticle],
) -> str:
    """Compose the worker prompt for one cluster."""
    return PROMPT_TEMPLATE.format(
        anchors=_format_anchors(),
        categories=_format_categories(),
        articles=_format_articles(articles),
    )


__all__ = [
    "ClusterArticle",
    "PROMPT_TEMPLATE",
    "build_classification_prompt",
]
