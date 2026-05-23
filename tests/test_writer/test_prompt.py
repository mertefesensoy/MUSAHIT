"""Tests for musahit.writer.prompt."""

from __future__ import annotations

from datetime import date

from musahit.score.defcon import DEFCON
from musahit.writer.payload import (
    ArcView,
    BriefingPayload,
    ClusterView,
)
from musahit.writer.prompt import (
    DISCIPLINE_RULES,
    OUTPUT_INSTRUCTION,
    SYSTEM_ROLE,
    build_writer_prompt,
)
from musahit.writer.template import TEMPLATE_SECTIONS


def _payload() -> BriefingPayload:
    return BriefingPayload(
        date=date(2026, 5, 23),
        run_id="run_test",
        clusters_by_defcon={
            int(DEFCON.SEVERE): [
                ClusterView(
                    id="cl1",
                    headline="Test başlığı",
                    summary="Özet.",
                    category="POLİTİKA",
                    final_defcon=int(DEFCON.SEVERE),
                    confidence="YÜKSEK",
                    bands_present=["gov_aligned", "opposition"],
                    arc_id="arc_20260518_0001",
                    sources=[
                        {"source_id": "sabah", "band": "gov_aligned"},
                        {"source_id": "cumhuriyet", "band": "opposition"},
                    ],
                    is_social_only=False,
                ),
            ],
        },
        open_arc_updates=[
            ArcView(
                id="arc_20260518_0001",
                headline="Test arc",
                summary="Sürüyor.",
                state="OPEN",
                peak_defcon=int(DEFCON.SEVERE),
                category="YARGI",
                last_update_at=None,
                created_at=None,
            )
        ],
        resolved_arcs=[],
        peak_defcon=int(DEFCON.SEVERE),
        cluster_count=1,
        arc_count=5,
        open_arc_count=3,
        ambient_count=10,
        failed_sources=[],
        stages_done=["ingest", "normalize", "cluster", "score", "arc-link"],
    )


class TestPromptContents:
    def test_includes_system_role(self) -> None:
        assert SYSTEM_ROLE in build_writer_prompt(_payload())

    def test_includes_discipline_rules(self) -> None:
        prompt = build_writer_prompt(_payload())
        # Sample one identifying phrase from the rules block.
        assert "KAYNAK·BAND" in prompt
        assert DISCIPLINE_RULES in prompt

    def test_includes_output_instruction(self) -> None:
        assert OUTPUT_INSTRUCTION in build_writer_prompt(_payload())

    def test_template_skeleton_includes_all_section_markers(self) -> None:
        prompt = build_writer_prompt(_payload())
        for s in TEMPLATE_SECTIONS:
            assert s.marker in prompt

    def test_defcon_schema_block_present(self) -> None:
        prompt = build_writer_prompt(_payload())
        assert "DEFCON ÖLÇEĞİ" in prompt
        # Sample anchor present.
        assert "15 Temmuz 2016 darbe girişimi" in prompt

    def test_day_data_includes_cluster_headline(self) -> None:
        prompt = build_writer_prompt(_payload())
        assert "Test başlığı" in prompt

    def test_day_data_includes_arc_id(self) -> None:
        prompt = build_writer_prompt(_payload())
        assert "arc_20260518_0001" in prompt

    def test_prompt_is_a_single_string(self) -> None:
        prompt = build_writer_prompt(_payload())
        assert isinstance(prompt, str)
        assert len(prompt) > 0


class TestPromptSize:
    def test_prompt_well_under_32k_chars(self) -> None:
        # Trendyol-LLM v1.8 has a 32K token context window.
        # Each Turkish token is roughly 4 characters on average; so
        # 32K tokens ≈ 128K characters. Our worst-case fixture should
        # be well under that. This is a sanity guard against accidental
        # mega-prompts (e.g., dumping raw article bodies in).
        prompt = build_writer_prompt(_payload())
        assert len(prompt) < 16_000  # ~4K tokens — leaves room for output
