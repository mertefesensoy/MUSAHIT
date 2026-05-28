"""Tests for musahit.cluster.embedder.

OllamaEmbeddingClient is verified through ``httpx.MockTransport`` so no
real Ollama server is required. FakeEmbeddingClient is the pattern
every downstream Ollama-using test should reuse.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from musahit.cluster.centroid import cosine_similarity
from musahit.cluster.embedder import (
    DEFAULT_BATCH_SIZE,
    DIMENSION,
    FakeEmbeddingClient,
    OllamaEmbeddingClient,
    _is_valid_vector,
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
                        # Non-zero values so _is_valid_vector accepts
                        # them (all-zeros is rejected as degenerate per
                        # Solution B).
                        "embeddings": [[0.1] * DIMENSION for _ in body["input"]],
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
                        "embeddings": [[0.1] * DIMENSION for _ in body["input"]],
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

    async def test_http_error_single_item_becomes_none(self) -> None:
        # Solution B (2026-05-28) · a single-item batch that 500s every
        # retry returns ``[None]`` instead of raising · the clusterer
        # then drops that article and clusters the rest. The pre-
        # Solution-B behavior was raise; this test pins the new contract.
        def responder(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"down")

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            backoff_base_seconds=0.0,
        )
        result = await client.embed(["x"])
        assert result == [None]

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


def _embed_response(n: int, value: float = 0.1) -> httpx.Response:
    # Default 0.1 (not 0.0) so _is_valid_vector accepts the result ·
    # Solution B rejects all-zeros as degenerate.
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

    async def test_embed_single_item_failure_becomes_none(self) -> None:
        """Solution B · single-item permanent failure → None at that index.

        Pre-Solution-B this raised httpx.HTTPStatusError. After
        2026-05-28's bge-m3 NaN poison-article incident, per-item
        permanent failure returns None so the clusterer can drop just
        that article and cluster the rest."""

        def responder(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"perma-down")

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=4,
            backoff_base_seconds=0.0,
        )
        result = await client.embed(["a"])
        assert result == [None]

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

    async def test_embed_4xx_does_not_retry_single_item_becomes_none(
        self,
    ) -> None:
        """A 400 (caller bug) is NOT retryable · single attempt at the
        transport · single-item permanent failure → None (Solution B).

        The "does not retry" semantic survives Solution B: only ONE HTTP
        attempt happens, then the per-item base case fires."""
        attempts: list[int] = []

        def responder(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(400, content=b"bad request")

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            backoff_base_seconds=0.0,
        )
        result = await client.embed(["x"])
        assert result == [None]
        assert len(attempts) == 1

    async def test_embed_429_is_retryable_then_becomes_none(self) -> None:
        """Rate-limit responses are transient · retry exhausts then the
        single-item base case substitutes None (Solution B)."""
        attempts: list[int] = []

        def responder(_request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(429, content=b"slow down")

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            max_retries=2,
            backoff_base_seconds=0.0,
        )
        result = await client.embed(["x"])
        assert result == [None]
        # 1 initial + 2 retries = 3 attempts.
        assert len(attempts) == 3


def _tag(token: str) -> int:
    """Stable small integer tag for a token · used to verify order in tests."""
    # Each test passes simple tokens · the tag fits in a float comfortably.
    return sum(ord(c) for c in token) % 997


# ── Solution B · per-item None partial alignment (2026-05-28) ─────────────


class TestSolutionBPerItemNone:
    async def test_embed_returns_none_for_persistent_item_failure(self) -> None:
        """Designated poison input 500s every retry · result has None at
        that index · valid vectors elsewhere · order preserved.

        Models the bge-m3 NaN-poison-article incident: one specific
        clean input ("Sezon açıldı") makes the model emit a NaN-laced
        vector that Ollama cannot serialize, returning 500. The poison
        item must end up as None at its original input index after
        retry → halving → single-item base case."""
        poison = "Sezon açıldı"

        def responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            inputs = body["input"]
            if poison in inputs:
                return httpx.Response(
                    500,
                    content=json.dumps({
                        "error": "failed to encode response: "
                                 "json: unsupported value: NaN"
                    }),
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                200,
                content=json.dumps({
                    "model": "bge-m3",
                    "embeddings": [
                        [float(_tag(t))] * DIMENSION for t in inputs
                    ],
                }),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=4,
            backoff_base_seconds=0.0,
        )
        inputs = ["a", "b", poison, "d", "e"]
        result = await client.embed(inputs)

        # Length contract: one slot per input.
        assert len(result) == len(inputs)
        # Poison at index 2 → None. Others valid vectors.
        assert result[0] is not None
        assert result[1] is not None
        assert result[2] is None
        assert result[3] is not None
        assert result[4] is not None
        # Order check on the surviving vectors.
        assert result[0][0] == float(_tag("a"))
        assert result[1][0] == float(_tag("b"))
        assert result[3][0] == float(_tag("d"))
        assert result[4][0] == float(_tag("e"))


class TestIsValidVector:
    """Unit tests for the defensive vector-validation helper."""

    def test_valid_vector_accepted(self) -> None:
        vec = [0.1] * DIMENSION
        assert _is_valid_vector(vec) is True

    def test_is_valid_vector_rejects_none(self) -> None:
        assert _is_valid_vector(None) is False

    def test_is_valid_vector_rejects_empty(self) -> None:
        assert _is_valid_vector([]) is False

    def test_is_valid_vector_rejects_wrong_dim(self) -> None:
        vec = [0.1] * (DIMENSION - 1)
        assert _is_valid_vector(vec) is False
        vec_bigger = [0.1] * (DIMENSION + 1)
        assert _is_valid_vector(vec_bigger) is False

    def test_is_valid_vector_rejects_nan(self) -> None:
        vec = [0.1] * DIMENSION
        vec[42] = float("nan")
        assert _is_valid_vector(vec) is False

    def test_is_valid_vector_rejects_inf(self) -> None:
        vec_pos = [0.1] * DIMENSION
        vec_pos[100] = float("inf")
        assert _is_valid_vector(vec_pos) is False
        vec_neg = [0.1] * DIMENSION
        vec_neg[200] = float("-inf")
        assert _is_valid_vector(vec_neg) is False

    def test_is_valid_vector_rejects_all_zeros(self) -> None:
        vec = [0.0] * DIMENSION
        assert _is_valid_vector(vec) is False

    def test_is_valid_vector_rejects_non_numeric_components(self) -> None:
        vec = [0.1] * DIMENSION
        vec[5] = "not a number"  # type: ignore[assignment]
        assert _is_valid_vector(vec) is False

    def test_is_valid_vector_accepts_single_nonzero_component(self) -> None:
        # An L2-normalized vector with only one nonzero index is valid ·
        # the all-zeros guard requires ANY nonzero, not all-nonzero.
        vec = [0.0] * DIMENSION
        vec[0] = 1.0
        assert _is_valid_vector(vec) is True


class TestVectorValidationCoercesToNone:
    """When Ollama returns 200 with a corrupt embedding (NaN/Inf/wrong
    dim/all-zeros), _post_batch validates each vector and substitutes
    None · the corrupt vector never reaches the clusterer."""

    async def test_nan_in_200_response_becomes_none(self) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            inputs = body["input"]
            embeddings: list[list[float]] = []
            for t in inputs:
                if t == "poison":
                    vec = [0.1] * DIMENSION
                    vec[10] = float("nan")
                    embeddings.append(vec)
                else:
                    embeddings.append([float(_tag(t))] * DIMENSION)
            return httpx.Response(
                200,
                content=json.dumps({"model": "bge-m3",
                                    "embeddings": embeddings}),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=8,
            backoff_base_seconds=0.0,
        )
        result = await client.embed(["a", "poison", "b"])
        assert result[0] is not None
        assert result[1] is None
        assert result[2] is not None

    async def test_all_zeros_in_200_response_becomes_none(self) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            inputs = body["input"]
            embeddings = []
            for t in inputs:
                if t == "zero":
                    embeddings.append([0.0] * DIMENSION)
                else:
                    embeddings.append([float(_tag(t))] * DIMENSION)
            return httpx.Response(
                200,
                content=json.dumps({"model": "bge-m3",
                                    "embeddings": embeddings}),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            backoff_base_seconds=0.0,
        )
        result = await client.embed(["a", "zero", "b"])
        assert result[0] is not None
        assert result[1] is None
        assert result[2] is not None

    async def test_wrong_dim_in_200_response_becomes_none(self) -> None:
        def responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            inputs = body["input"]
            embeddings = []
            for t in inputs:
                if t == "short":
                    embeddings.append([0.1] * (DIMENSION - 1))
                else:
                    embeddings.append([float(_tag(t))] * DIMENSION)
            return httpx.Response(
                200,
                content=json.dumps({"model": "bge-m3",
                                    "embeddings": embeddings}),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            backoff_base_seconds=0.0,
        )
        result = await client.embed(["a", "short", "b"])
        assert result[0] is not None
        assert result[1] is None
        assert result[2] is not None


class TestOrderPreservationWithNone:
    """A None in the middle of a batch must not shift the surviving
    vectors out of their input indices."""

    async def test_order_preserved_across_batches_with_none(self) -> None:
        poison = "POISON"

        def responder(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            inputs = body["input"]
            if poison in inputs:
                return httpx.Response(500, content=b"down")
            return httpx.Response(
                200,
                content=json.dumps({
                    "model": "bge-m3",
                    "embeddings": [
                        [float(_tag(t))] * DIMENSION for t in inputs
                    ],
                }),
                headers={"content-type": "application/json"},
            )

        client = OllamaEmbeddingClient(
            client=_make_client(responder),
            batch_size=3,
            backoff_base_seconds=0.0,
        )
        inputs = ["a", "b", "c", "d", poison, "f", "g", "h", "i", "j"]
        result = await client.embed(inputs)
        assert len(result) == 10
        assert result[4] is None
        for i, token in enumerate(inputs):
            if i == 4:
                continue
            assert result[i] is not None
            assert result[i][0] == float(_tag(token))
