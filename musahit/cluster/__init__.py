"""Cluster stage: bge-m3 embeddings + greedy single-pass cosine clustering.

Per ADR-001 / ADR-002 / ADR-006 / ADR-008. Reads articles from the run that
don't yet have embeddings, embeds them via the injected ``EmbeddingClient``,
partitions by language, and assigns each article to either an existing
cluster (cosine ≥ threshold within the 24h window) or a fresh one.
"""
