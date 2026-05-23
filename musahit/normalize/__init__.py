"""Normalize stage: extract title/body/metadata from raw_articles into articles.

The :class:`Normalizer` reads ``raw_articles`` rows that have no matching
``articles`` row yet, dispatches to a per-kind extractor, enriches the result
with language/entities/lead/word_count, and writes the canonical row.
"""
