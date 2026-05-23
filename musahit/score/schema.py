"""Pydantic models for parsing the worker LLM's JSON output.

The classifier asks Qwen2.5 7B to return a strict JSON object matching
:class:`WorkerResponse`. Validation runs on every reply; a
:class:`pydantic.ValidationError` triggers the classifier's retry loop.
After the configured ``max_retries`` the classifier falls back to a
canned, conservative response (``category=UNCLASSIFIED``,
``defcon=AMBIENT``, ``confidence_self="low"``) so the pipeline never
stalls on a single misbehaving LLM call.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from musahit.common.types import Category

WorkerConfidence = Literal["high", "medium", "low"]


class WorkerResponse(BaseModel):
    """The shape Qwen2.5 7B is instructed to return.

    Bounds and types are enforced by pydantic. The classifier never
    consumes a partially-validated object — either the parse succeeds
    fully or the response is treated as malformed.
    """

    defcon: int = Field(ge=0, le=5)
    category: Category
    confidence_self: WorkerConfidence
    entities: list[str] = Field(default_factory=list)
    summary: str = Field(default="", max_length=500)
    headline: str = Field(default="", max_length=200)


def parse_worker_response(raw: str) -> WorkerResponse:
    """Parse a raw LLM completion into a :class:`WorkerResponse`.

    The wrapper exists so callers can ``try/except`` on a single
    exception type — :class:`pydantic.ValidationError` — and not have to
    branch on JSON-decoding vs schema-validation. We also strip common
    LLM artifacts (triple-backtick ``json`` code fences, leading/trailing prose)
    so a model that wraps its JSON in markdown still parses.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip leading ``` and trailing ``` fences with optional language tag.
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    # Some models still embed prose around the JSON object — locate the
    # outermost { ... } and try just that span.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError as exc:
        # Re-raise as ValidationError so callers have one type to catch.
        raise ValidationError.from_exception_data(
            "WorkerResponse",
            [{"type": "json_invalid", "loc": (), "input": raw, "ctx": {"error": str(exc)}}],
        ) from exc
    return WorkerResponse.model_validate(data)


__all__ = [
    "WorkerConfidence",
    "WorkerResponse",
    "parse_worker_response",
]
