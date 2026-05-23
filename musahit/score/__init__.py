"""Score stage: two-step DEFCON classification + bias-promotion ceiling.

Step 1 — the worker LLM (Qwen2.5 7B via Ollama) reads a cluster and emits a
``raw_defcon`` per the calibration ladder in ADR-004. Step 2 — the deterministic
promotion rules from ADR-005 derive a ``ceiling_defcon`` from the cluster's
``bands_present`` (with PRIMARY override + X/Reddit hard cap + cross-band/side
counts). The final score is ``min(raw, ceiling)`` per ADR-005's formula.
"""
