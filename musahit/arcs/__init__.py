"""Arc-linking stage: bind scored clusters into long-lived event threads.

Per ADR-008 every scored cluster is matched against OPEN/WATCH arcs from
the last 30 days. Match requires both ``cosine ≥ 0.55`` on the centroid
AND ``Jaccard ≥ 0.4`` on the stopword-filtered entity set. Unmatched
clusters seed new arcs. A cleanup pass at the end of the run advances
arc states per the ADR-008 transition rules.
"""
