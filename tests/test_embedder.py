"""Tests for musahit.cluster.embedder.

OllamaEmbeddingClient is verified through ``httpx.MockTransport`` so no
real Ollama server is required. FakeEmbeddingClient is the pattern
every downstream Ollama-using test should reuse.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from musahit.cluster.centroid import cosine_similarity
from musahit.cluster.embedder import (
    DEFAULT_BATCH_SIZE,
    DIMENSION,
    FakeEmbeddingClient,
    OllamaEmbeddingClient,
)

# ── FakeEmbeddingClient ────────────────────────────────────────────────────


class TestFakeEmbeddingClient:
    async def test_dimension_is_1024(self) -> None:
        vectors = await FakeEmbeddingClient().embed(["hello world"])
        assert len(vectors) == 1
        assert len(vectors[0]) == DIMENSION

    async def test_deterministic_across_calls(self) -> None:
        client = FakeEmbeddingClient()
        a = await client.embed(["Türkiye ekonomisi bugün"])
        b = await client.embed(["Türkiye ekonomisi bugün"])
        assert a == b

    async def test_similar_texts_have_high_cosine(self) -> None:
        client = FakeEmbeddingClient()
        # Heavy token overlap — both about Turkey's economic situation today.
        text_a = (
            "Türkiye ekonomisi bugün yeni bir gelişme yaşadı ve enflasyon"
            " rakamları açıklandı"
        )
        text_b = (
            "Türkiye ekonomisi bugün enflasyon rakamları ile yeni bir"
            " gelişme yaşadı"
        )
        vectors = await client.embed([text_a, text_b])
        sim = cosine_similarity(vectors[0], vectors[1])
        assert sim >= 0.8, f"expected ≥ 0.8, got {sim}"

    async def test_different_texts_have_low_cosine(self) -> None:
        client = FakeEmbeddingClient()
        text_a = "Türkiye ekonomisi enflasyon faiz lira"
        text_b = "football match scores premier league weekend"
        vectors = await client.embed([text_a, text_b])
        sim = cosine_similarity(vectors[0], vectors[1])
        assert sim < 0.5, f"expected < 0.5, got {sim}"

    async def test_empty_input_returns_zero_vector_safely(self) -> None:
        # Edge case: an article with no tokens (after .split()) produces
        # a zero vector. Cosine against any other vector is 0 by our
        # convention (no ZeroDivisionError surfaced).
        vectors = await FakeEmbeddingClient().embed([""])
        assert vectors[0] == [0.0] * DIMENSION


# ── OllamaEmbeddingClient ──────────────────────────────────────────────────


def _make_client(
    responder: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(responder))


class TestOllamaEmbeddingClient:
    async def test_single_batch_post(self) -> None:
        captured: list[dict] = []

        def responder(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            n = len(json.loads(request.content)["input"])
            embeddings = [[0.1] * DIMENSION for _ in range(n)]
            return httpx.Response(
                200,
                content=json.dumps({"model": "bge-m3", "embeddings": embeddings}),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(client=_make_client(responder))
        vectors = await client.embed(["one", "two", "three"])
        assert len(vectors) == 3
        assert all(len(v) == DIMENSION for v in vectors)
        assert len(captured) == 1
        assert captured[0]["model"] == "bge-m3"
        assert captured[0]["input"] == ["one", "two", "three"]

    async def test_batches_respect_batch_size(self) -> None:
        # 120 inputs at DEFAULT_BATCH_SIZE → ceil(120 / size) calls;
        # each call carries up to DEFAULT_BATCH_SIZE items, with the
        # final call carrying the remainder.
        seen_sizes: list[int] = []

        def responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen_sizes.append(len(body["input"]))
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "model": "bge-m3",
                        "embeddings": [[0.0] * DIMENSION for _ in body["input"]],
                    }
                ),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=DEFAULT_BATCH_SIZE,
        )
        vectors = await client.embed([f"t{i}" for i in range(120)])
        assert len(vectors) == 120
        full_batches, remainder = divmod(120, DEFAULT_BATCH_SIZE)
        expected = [DEFAULT_BATCH_SIZE] * full_batches
        if remainder:
            expected.append(remainder)
        assert seen_sizes == expected

    async def test_explicit_batch_size_of_50_still_supported(self) -> None:
        # Callers may opt back into the pre-2026-05-28 batch size of 50.
        seen_sizes: list[int] = []

        def responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen_sizes.append(len(body["input"]))
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "model": "bge-m3",
                        "embeddings": [[0.0] * DIMENSION for _ in body["input"]],
                    }
                ),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=50,
        )
        vectors = await client.embed([f"t{i}" for i in range(120)])
        assert len(vectors) == 120
        assert seen_sizes == [50, 50, 20]

    async def test_http_error_raises(self) -> None:
        # A single-item batch that 500s every retry exhausts the budget
        # and re-raises. backoff_base_seconds=0 keeps the test fast.
        def responder(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"down")

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            backoff_base_seconds=0.0,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed(["x"])

    async def test_empty_input_returns_empty_no_call(self) -> None:
        calls: list[int] = []

        def responder(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, content=b"{}")

        client = OllamaEmbeddingClient(client=_make_client(responder))
        result = await client.embed([])
        assert result == []
        assert calls == []


# ── Issue 2 · Resilience (retry + adaptive halving) ────────────────────────


def _embed_response(n: int, value: float = 0.0) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps(
            {"model": "bge-m3", "embeddings": [[value] * DIMENSION for _ in range(n)]}
        ),
        headers={"content-type": "application/json"},
    )


class TestEmbedderResilience:
    async def test_embed_retries_on_500_then_succeeds(self) -> None:
        """First call 500s, retry succeeds · result correct, retry logged."""
        attempts: list[int] = []

        def responder(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            body = json.loads(request.content)
            if len(attempts) == 1:
                return httpx.Response(500, content=b"transient")
            return _embed_response(len(body["input"]))

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=8,
            backoff_base_seconds=0.0,
        )
        vectors = await client.embed(["a", "b", "c"])
        assert len(vectors) == 3
        assert all(len(v) == DIMENSION for v in vectors)
        # 1 fail + 1 success.
        assert len(attempts) == 2

    async def test_embed_halves_batch_on_persistent_failure(self) -> None:
        """A batch fails all retries; halves succeed · vectors in input order."""
        # Strategy: fail when the batch input contains BOTH "poison"
        # markers (i.e. the full batch); succeed otherwise. The full
        # batch must therefore halve before the halves can succeed.
        attempts: list[dict] = []

        def responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            inputs = body["input"]
            attempts.append({"size": len(inputs), "inputs": list(inputs)})
            both_present = "p1" in inputs and "p2" in inputs
            if both_present:
                return httpx.Response(500, content=b"oom")
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "model": "bge-m3",
                        # Tag each item's vector by token index so we can
                        # verify ordering downstream.
                        "embeddings": [
                            [float(_tag(t))] * DIMENSION for t in inputs
                        ],
                    }
                ),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=8,
            backoff_base_seconds=0.0,
        )
        vectors = await client.embed(["p1", "x", "y", "p2"])
        # Output must match input ORDER (p1, x, y, p2).
        assert [v[0] for v in vectors] == [
            float(_tag("p1")),
            float(_tag("x")),
            float(_tag("y")),
            float(_tag("p2")),
        ]
        # The full batch was tried (and 500'd through retries) at least once,
        # then halves succeeded.
        full_batch_attempts = [a for a in attempts if a["size"] == 4]
        assert len(full_batch_attempts) >= 1
        half_size_attempts = [a for a in attempts if a["size"] == 2]
        assert len(half_size_attempts) >= 2

    async def test_embed_single_item_failure_raises(self) -> None:
        """Item that fails every retry surfaces · upstream guard then fires."""

        def responder(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"perma-down")

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=4,
            backoff_base_seconds=0.0,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed(["a"])

    async def test_embed_preserves_order_across_batches(self) -> None:
        """Inputs > batch size · output length and order preserved."""

        def responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                content=json.dumps(
                    {
                        "model": "bge-m3",
                        "embeddings": [
                            [float(_tag(t))] * DIMENSION for t in body["input"]
                        ],
                    }
                ),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=3,
            backoff_base_seconds=0.0,
        )
        inputs = [f"tok_{i}" for i in range(10)]
        vectors = await client.embed(inputs)
        assert len(vectors) == 10
        assert [v[0] for v in vectors] == [float(_tag(t)) for t in inputs]

    async def test_embed_4xx_does_not_retry(self) -> None:
        """A 400 (caller bug) is NOT retryable · single attempt, raise."""
        attempts: list[int] = []

        def responder(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(400, content=b"bad request")

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            backoff_base_seconds=0.0,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed(["x"])
        assert len(attempts) == 1

    async def test_embed_429_is_retryable(self) -> None:
        """Rate-limit responses are transient · retry exhausts then raises."""
        attempts: list[int] = []

        def responder(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(429, content=b"slow down")

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            max_retries=2,
            backoff_base_seconds=0.0,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.embed(["x"])
        # 1 initial + 2 retries = 3 attempts.
        assert len(attempts) == 3


def _tag(token: str) -> int:
    """Stable small integer tag for a token · used to verify order in tests."""
    # Each test passes simple tokens · the tag fits in a float comfortably.
    return sum(ord(c) for c in token) % 997
