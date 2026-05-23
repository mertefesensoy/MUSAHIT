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
        # 120 inputs at batch_size=50 → 3 calls (50, 50, 20).
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
        assert seen_sizes == [50, 50, 20]

    async def test_http_error_raises(self) -> None:
        def responder(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"down")

        client = OllamaEmbeddingClient(client=_make_client(responder))
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
