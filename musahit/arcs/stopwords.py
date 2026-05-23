"""Entity-stopword list and filter helper.

These names co-occur in almost every Turkish political/economic story:
without a stopword filter, every cluster shares enough entities with
every arc to clear the Jaccard threshold and the arc model collapses
into one big arc. ADR-008 § "Arc-cluster linking" mentions
``STOPWORD_ENTITIES`` as the mechanism that prevents this.

The list is curated. Adding a new stopword is operator-judgment work —
typically a single-line edit here when an arc starts misbehaving.
"""

from __future__ import annotations

STOPWORD_ENTITIES: frozenset[str] = frozenset(
    {
        # Country / nation
        "Türkiye",
        "Türk",
        "Cumhuriyet",
        "Vatandaş",
        # Top-level political institutions (load-bearing on their own; here
        # only because every cluster mentions at least one)
        "Devlet",
        "Hükümet",
        "İktidar",
        "Muhalefet",
        "Meclis",
        "TBMM",
        "Cumhurbaşkanı",
        "Cumhurbaşkanlığı",
        # Largest parties — they appear in every political headline; their
        # presence does NOT disambiguate an arc.
        "AKP",
        "CHP",
        "MHP",
        "AK Parti",
        # Generic political vocabulary
        "Anayasa",
        "Kanun",
        "Karar",
    }
)


def filter_stopwords(entities: set[str] | frozenset[str]) -> set[str]:
    """Return ``entities`` with :data:`STOPWORD_ENTITIES` removed."""
    return {e for e in entities if e and e not in STOPWORD_ENTITIES}


__all__ = ["STOPWORD_ENTITIES", "filter_stopwords"]
