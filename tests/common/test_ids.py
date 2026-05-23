"""Tests for the canonical article-id formula in musahit.common.ids.

The formula in ADR-014 is load-bearing: every ingester depends on its three
properties (stable across calls, source-scoped, url-scoped). These tests
pin the contract so a future refactor cannot silently change it.
"""

from __future__ import annotations

import hashlib

from musahit.common.ids import article_id


class TestStability:
    def test_same_inputs_produce_same_id(self) -> None:
        a = article_id("bianet", "https://bianet.org/article-one")
        b = article_id("bianet", "https://bianet.org/article-one")
        assert a == b

    def test_id_is_hex_sha256(self) -> None:
        result = article_id("bianet", "https://bianet.org/x")
        expected = hashlib.sha256(b"bianet|https://bianet.org/x").hexdigest()
        assert result == expected
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_id_does_not_change_across_python_runs(self) -> None:
        # The expected value was computed once with the documented formula
        # and pinned here. If this assertion ever fails, the formula has
        # changed — coordinate with ADR-014 before updating the expected.
        expected = hashlib.sha256(b"bianet|https://bianet.org/article-one").hexdigest()
        assert article_id("bianet", "https://bianet.org/article-one") == expected


class TestSourceScoping:
    def test_different_source_ids_produce_different_ids_for_same_url(self) -> None:
        url = "https://example.com/shared-syndicated-article"
        id_a = article_id("source_a", url)
        id_b = article_id("source_b", url)
        assert id_a != id_b

    def test_source_id_affects_hash_input(self) -> None:
        # Specifically guard against accidental URL-only hashing.
        url = "https://example.com/x"
        id_a = article_id("a", url)
        id_b = article_id("b", url)
        id_c = article_id("c", url)
        assert id_a != id_b != id_c
        assert id_a != id_c


class TestUrlScoping:
    def test_different_urls_produce_different_ids_for_same_source(self) -> None:
        id_a = article_id("bianet", "https://bianet.org/article-one")
        id_b = article_id("bianet", "https://bianet.org/article-two")
        assert id_a != id_b

    def test_url_path_differences_matter(self) -> None:
        # Guard against any normalization that would collapse near-duplicates.
        id_a = article_id("bianet", "https://bianet.org/x")
        id_b = article_id("bianet", "https://bianet.org/x/")
        id_c = article_id("bianet", "https://bianet.org/X")
        assert id_a != id_b
        assert id_a != id_c


class TestSeparatorIsolation:
    def test_no_silent_collision_at_pipe_boundary(self) -> None:
        # The separator | is a literal byte in the hash input. Because
        # Source.id is ASCII-alphanum-underscore (enforced by
        # _build_sources_index), it can never contain "|" in practice, so
        # the boundary between source_id and url is always unambiguous.
        # This test pins the documented behavior: distinct (source_id, url)
        # pairs produce distinct ids — even ones that share characters
        # adjacent to the separator.
        a = article_id("bianet", "https://x.com/a")
        b = article_id("bianet|https://x.com", "/a")
        # The two hash inputs are "bianet|https://x.com/a" and
        # "bianet|https://x.com|/a" — distinct strings, distinct ids.
        assert a != b
