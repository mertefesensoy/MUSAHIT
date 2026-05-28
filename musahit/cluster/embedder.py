"""Embedding clients — production (Ollama / bge-m3) and test (deterministic fake).

The :class:`EmbeddingClient` Protocol is the seam every downstream stage
will use to acquire vectors. Step 10 introduces it; the score stage
(step 11) and arc-link stage (step 12) reuse the same Protocol with the
same :class:`FakeEmbeddingClient` for tests.

Per ADR-002, the production embedder is bge-m3 served by a local Ollama
instance on ``http://localhost:11434``. Embeddings are 1024-dimensional
floats; the cluster_embeddings / article_embeddings columns are
``FLOAT[1024]`` (ADR-006).

Return-type contract · ``embed()`` returns ``list[list[float] | None]``
of the SAME LENGTH as the input ``texts``. A value of ``None`` at
position *i* signals "this single input could not be embedded" · the
clusterer treats those positions as drops and clusters the rest. This
is Solution B from the 2026-05-28 NaN-poison-article incident: bge-m3
emits NaN for one specific clean input, Ollama 500s on serializing it,
and the only robust fix is per-item partial alignment. Total transport
failures (Ollama down) still propagate as exceptions · the clusterer's
Issue-1 guard turns them into a clean re-runnable stage failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Any, Protocol

import httpx

from musahit.common.logging import get_logger

_log = get_logger("musahit.cluster.embedder")

DIMENSION: int = 1024
DEFAULT_OLLAMA_URL: str = "http://localhost:11434"
DEFAULT_MODEL: str = "bge-m3"
# The pre-Issue-2 default was 50. Lowered to 16 after the 2026-05-28
# Ollama-OOM incident · smaller per-call batches reduce the peak memory
# footprint when bge-m3 is loaded alongside other 4-5GB models. Per-batch
# retries + adaptive halving handle the rare batches that still 500.
DEFAULT_BATCH_SIZE: int = 16
DEFAULT_TIMEOUT_SECONDS: float = 60.0
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BACKOFF_BASE_SECONDS: float = 2.0


# ── Protocol ───────────────────────────────────────────────────────────────


class EmbeddingClient(Protocol):
    """Async embedding API.

    Returns one slot per input text. The slot is a 1024-dim vector when
    the embedding succeeded · ``None`` when the embedder could not
    produce a usable vector for that specific input (per-item failure).
    Total transport failures (Ollama unreachable, etc.) surface as
    exceptions; the per-item ``None`` path covers per-item model
    failures (NaN/Inf/wrong-dim/all-zeros).
    """

    async def embed(self, texts: list[str]) -> list[list[float] | None]: ...


# ── Ollama implementation ──────────────────────────────────────────────────


class OllamaEmbeddingClient:
    """Production embedder: POSTs batches to Ollama's ``/api/embed``.

    Ollama's modern embed endpoint (since v0.1.30+) accepts an ``input``
    list and returns matching ``embeddings``. The client slices the
    caller's full ``texts`` list into ``batch_size``-chunks (default 16
    after the 2026-05-28 OOM incident, down from 50). Each batch is
    retried with exponential backoff on transient errors (HTTP 5xx,
    timeout, connection); a batch that still fails after retries is
    halved recursively down to single items.

    Per-item failure path (Solution B · 2026-05-28) · a SINGLE-item
    batch that still fails after retries returns ``[None]`` for that
    position instead of raising. Combined with halving's left+right
    reassembly, this preserves alignment: the ``None`` lands at the
    exact original index. The clusterer then drops that article and
    clusters the rest. Total transport failures (Ollama unreachable)
    still raise so the Issue-1 guard fires a clean stage failure.

    Vector validation · every vector returned by Ollama is passed
    through :func:``_is_valid_vector``. Invalid vectors (wrong
    dimension, NaN/Inf, all-zeros) are coerced to ``None`` defensively
    so the clusterer never sees a corrupt embedding even if a future
    Ollama version coerces NaN to null in the JSON response.

    Order preservation: batches are processed sequentially; halving
    splits a slice in place and recursively embeds each half, then
    concatenates left+right · the output position of every input text
    is preserved.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_MODEL,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._batch_size = batch_size
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._injected_client = client

    async def embed(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []

        if self._injected_client is not None:
            return await self._embed_with(self._injected_client, texts)
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            return await self._embed_with(client, texts)

    async def _embed_with(
        self, client: httpx.AsyncClient, texts: list[str]
    ) -> list[list[float] | None]:
        out: list[list[float] | None] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors = await self._embed_batch_resilient(client, batch)
            out.extend(vectors)
        return out

    async def _embed_batch_resilient(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float] | None]:
        """Try the batch with retries; on persistent failure, halve and recurse.

        Recursive base case · a single-item batch that still fails after
        all retries returns ``[None]`` for that position. Halving's
        ``left + right`` reassembly keeps the None at the input index.

        Multi-item base case · raises so the caller can halve. The
        halving loop is what eventually drives a poison item down to a
        single-item batch where the None substitution fires.
        """
        try:
            return await self._embed_batch_with_retry(client, batch)
        except Exception as exc:
            if len(batch) <= 1:
                reason = _classify_failure_reason(exc)
                _log.warning(
                    "embed_item_skipped",
                    item_index_in_batch=0,
                    reason=reason,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return [None]
            mid = len(batch) // 2
            _log.warning(
                "embed_batch_halved",
                batch_size=len(batch),
                left_size=mid,
                right_size=len(batch) - mid,
                error=f"{type(exc).__name__}: {exc}",
            )
            left = await self._embed_batch_resilient(client, batch[:mid])
            right = await self._embed_batch_resilient(client, batch[mid:])
            return left + right

    async def _embed_batch_with_retry(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float] | None]:
        """One batch · 1 + max_retries attempts · exponential backoff.

        Returns ``list[list[float] | None]`` of length ``len(batch)``
        on success · individual positions may already be ``None`` from
        the per-vector validation in ``_post_batch``. On retry
        exhaustion this method still RAISES so the halving layer can
        split the batch · only the single-item base case in
        ``_embed_batch_resilient`` substitutes ``[None]``.
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._post_batch(client, batch)
            except (httpx.HTTPStatusError, httpx.TransportError,
                    httpx.TimeoutException) as exc:
                last_exc = exc
                if not _is_retryable(exc):
                    raise
                if attempt < self._max_retries:
                    sleep_for = self._backoff_base_seconds * (2 ** attempt)
                    _log.warning(
                        "embed_batch_retry",
                        attempt=attempt,
                        batch_size=len(batch),
                        sleep_seconds=sleep_for,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    await asyncio.sleep(sleep_for)
                    continue
                raise
        # Defensive · should be unreachable; satisfies the type checker.
        assert last_exc is not None
        raise last_exc

    async def _post_batch(
        self, client: httpx.AsyncClient, batch: list[str]
    ) -> list[list[float] | None]:
        """POST the batch · return validated vectors (None for invalid).

        Defensive vector validation runs on every returned embedding so
        a future Ollama that returns 200 with NaN coerced to null/0
        still produces a clean ``None`` rather than feeding a corrupt
        vector to the clusterer.
        """
        response = await client.post(
            f"{self._base_url}/api/embed",
            json={"model": self._model, "input": batch},
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        raw_vectors = data.get("embeddings") or []
        validated: list[list[float] | None] = []
        for i, vec in enumerate(raw_vectors):
            if _is_valid_vector(vec):
                validated.append(list(vec))
            else:
                reason = _classify_vector_reason(vec)
                _log.warning(
                    "embed_item_skipped",
                    item_index_in_batch=i,
                    reason=reason,
                )
                validated.append(None)
        # Pad with None if the server returned fewer items than we sent
        # (defensive · should not happen with the modern endpoint).
        while len(validated) < len(batch):
            _log.warning(
                "embed_item_skipped",
                item_index_in_batch=len(validated),
                reason="missing_in_response",
            )
            validated.append(None)
        return validated


def _is_retryable(exc: Exception) -> bool:
    """Transient errors worth retrying: 5xx, timeout, connection failures.

    4xx (except 408/429) are caller bugs — retrying won't help, so they
    surface immediately.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status in (408, 429)
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _is_valid_vector(vec: Any) -> bool:
    """Return True only for a usable embedding vector.

    Rejects · ``None`` · empty list · wrong dimension · any NaN/Inf
    component · all-zeros (degenerate). The all-zeros check catches a
    Future-Ollama-coerces-NaN-to-0 scenario where every component is
    silently zeroed · such a vector clusters with nothing useful and
    pollutes centroids, so we treat it as a failure.
    """
    if vec is None:
        return False
    if not isinstance(vec, (list, tuple)):
        return False
    if len(vec) != DIMENSION:
        return False
    any_nonzero = False
    for x in vec:
        if not isinstance(x, (int, float)):
            return False
        if math.isnan(x) or math.isinf(x):
            return False
        if x != 0.0:
            any_nonzero = True
    return any_nonzero


def _classify_vector_reason(vec: Any) -> str:
    """Map an invalid vector to a structured reason string for logs."""
    if vec is None:
        return "none_returned"
    if not isinstance(vec, (list, tuple)) or len(vec) == 0:
        return "empty_or_non_list"
    if len(vec) != DIMENSION:
        return "wrong_dim"
    for x in vec:
        if isinstance(x, float):
            if math.isnan(x):
                return "nan_in_vector"
            if math.isinf(x):
                return "inf_in_vector"
    if not any(vec):
        return "all_zeros"
    return "unknown"


def _classify_failure_reason(exc: Exception) -> str:
    """Map a per-item retry-exhausted exception to a reason string.

    ``http_500_nan`` covers the bge-m3 NaN-serialization incident · the
    body contains the literal ``json: unsupported value: NaN`` from
    Ollama's response writer. Other 5xx fall back to
    ``exhausted_retries``.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.text or ""
        except Exception:
            body = ""
        if "NaN" in body or "unsupported value" in body:
            return "http_500_nan"
        if exc.response.status_code >= 500:
            return "exhausted_retries"
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.TransportError):
        return "transport_error"
    return "exhausted_retries"


# ── Fake (testing) implementation ──────────────────────────────────────────


class FakeEmbeddingClient:
    """Deterministic, network-free embedder for tests.

    Strategy: bag-of-words over whitespace tokens. Each token hashes to a
    fixed bucket in the 1024-dim vector and contributes a unit weight.
    The vector is L2-normalised at the end so two embeddings that share
    a token set produce a high cosine.

    Properties this gives:

    * Determinism — identical text → identical vector across runs.
    * Similarity preservation — texts sharing 80%+ tokens have cosine
      ≥ 0.8; disjoint texts have cosine < 0.5 (typically ≈ 0).
    * 1024-dim — matches the production embedder so the same DB schema
      and same cosine threshold are used in tests.

    Downstream Ollama-using stages (score, arc-link, write) should reuse
    this fake in their own tests — keep the pattern uniform.
    """

    def __init__(self, dimension: int = DIMENSION) -> None:
        self._dimension = dimension

    async def embed(self, texts: list[str]) -> list[list[float] | None]:
        # The fake never fails per-item · it always returns a vector.
        # Returning the wider ``list[float] | None`` here satisfies the
        # Solution-B Protocol contract so the fake remains a drop-in
        # replacement for OllamaEmbeddingClient in clusterer tests.
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dimension
        for token in (text or "").lower().split():
            digest = hashlib.md5(token.encode("utf-8"), usedforsecurity=False).hexdigest()
            idx = int(digest[:8], 16) % self._dimension
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [x / norm for x in vec]
        return vec


__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_MODEL",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DIMENSION",
    "EmbeddingClient",
    "FakeEmbeddingClient",
    "OllamaEmbeddingClient",
    "_is_valid_vector",
]
