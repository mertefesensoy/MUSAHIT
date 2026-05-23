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
    _clusters_data_block,
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
        assert len(prompt) < 16_000  # ~4K tokens · leaves room for output


# ── Regression for 2026-05-23 placeholder-echo bug ─────────────────────────


def _payload_with_ambient() -> BriefingPayload:
    return BriefingPayload(
        date=date(2026, 5, 24),
        run_id="run_ambient_test",
        clusters_by_defcon={
            int(DEFCON.AMBIENT): [
                ClusterView(
                    id="cl_amb1",
                    headline="Düşük öncelikli bir gelişme",
                    summary="Özet.",
                    category="TOPLUM",
                    final_defcon=int(DEFCON.AMBIENT),
                    confidence="DÜŞÜK",
                    bands_present=["centrist"],
                    arc_id=None,
                    sources=[
                        {"source_id": "bianet", "band": "centrist"},
                        {"source_id": "diken", "band": "centrist"},
                    ],
                    is_social_only=False,
                ),
                ClusterView(
                    id="cl_amb2",
                    headline="İkinci ambiyans öğesi",
                    summary="",
                    category=None,
                    final_defcon=int(DEFCON.AMBIENT),
                    confidence="DÜŞÜK",
                    bands_present=["independent"],
                    arc_id=None,
                    sources=[{"source_id": "anka", "band": "independent"}],
                    is_social_only=False,
                ),
            ],
        },
    )


class TestTemplateSkeletonInstructions:
    """The skeleton fed to the writer LLM must carry the per-section
    Turkish instructions added on 2026-05-24, and MUST NOT contain the
    old single literal placeholder that caused Trendyol to echo it
    verbatim."""

    def test_skeleton_contains_each_sections_prompt_instruction(self) -> None:
        prompt = build_writer_prompt(_payload())
        for s in TEMPLATE_SECTIONS:
            assert s.prompt_instruction in prompt, (
                f"prompt_instruction for {s.name} missing from skeleton"
            )

    def test_skeleton_does_not_contain_the_old_literal_placeholder(self) -> None:
        """Exact opening fragment of the old placeholder must be absent."""
        prompt = build_writer_prompt(_payload())
        assert "[içerik buraya" not in prompt


class TestAmbientClusterRendering:
    """The 2026-05-23 bug had two roots · the missing AMBİYANS data
    block was one of them. With ambient clusters in the payload the
    block must render headline + source-count bullets."""

    def test_ambient_bucket_renders_headlines(self) -> None:
        block = _clusters_data_block(_payload_with_ambient())
        assert "AMBİYANS (DEFCON 5):" in block
        assert "- Düşük öncelikli bir gelişme · (2 kaynak)" in block
        assert "- İkinci ambiyans öğesi · (1 kaynak)" in block

    def test_ambient_bucket_renders_empty_state_when_no_data(self) -> None:
        block = _clusters_data_block(_payload())  # has SEVERE only
        assert "AMBİYANS (DEFCON 5):" in block
        # The empty-state phrase must follow the AMBİYANS heading
        # somewhere in the block. The text uses the standard
        # "(bugün öğe yok)" pattern.
        ambient_idx = block.index("AMBİYANS (DEFCON 5):")
        assert "(bugün öğe yok)" in block[ambient_idx:]

    def test_full_prompt_with_ambient_payload_renders_under_size_budget(
        self,
    ) -> None:
        prompt = build_writer_prompt(_payload_with_ambient())
        assert len(prompt) < 16_000
