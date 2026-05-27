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
    SECTION_ROSTER,
    SYSTEM_ROLE,
    TEMPLATE_LEAD_IN,
    _clusters_data_block,
    build_section_user,
    build_system_log_section,
    build_writer_prompt,
    build_writer_system,
    build_writer_user,
)
from musahit.writer.template import DOCUMENT_TITLE, TEMPLATE_SECTIONS


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


# ── Template positioning (2026-05-27 reorder) ────────────────────────────


def _heavy_payload() -> BriefingPayload:
    """Payload with enough clusters that data dominates the prompt."""
    clusters: list[ClusterView] = []
    for i in range(40):
        clusters.append(
            ClusterView(
                id=f"cl_{i}",
                headline=f"Gündem başlığı {i} · detay metni buraya gelir",
                summary=f"Küme {i} özeti. Birden fazla kaynak teyit etti.",
                category="POLİTİKA" if i % 3 == 0 else "EKONOMİ",
                final_defcon=int(DEFCON.ROUTINE),
                confidence="ORTA",
                bands_present=["gov_aligned", "opposition"],
                arc_id=f"arc_20260523_{i:04d}" if i % 5 == 0 else None,
                sources=[
                    {"source_id": "sabah", "band": "gov_aligned"},
                    {"source_id": "cumhuriyet", "band": "opposition"},
                ],
                is_social_only=False,
            )
        )
    arcs = [
        ArcView(
            id=f"arc_20260518_{i:04d}",
            headline=f"Açık hikaye {i}",
            summary=f"Hikaye {i} sürüyor.",
            state="OPEN",
            peak_defcon=int(DEFCON.SEVERE),
            category="YARGI",
            last_update_at=None,
            created_at=None,
        )
        for i in range(10)
    ]
    return BriefingPayload(
        date=date(2026, 5, 27),
        run_id="run_heavy",
        clusters_by_defcon={int(DEFCON.ROUTINE): clusters},
        open_arc_updates=arcs,
        resolved_arcs=[],
        peak_defcon=int(DEFCON.ROUTINE),
        cluster_count=40,
        arc_count=10,
        open_arc_count=10,
        ambient_count=0,
        failed_sources=[],
        stages_done=["ingest", "normalize", "cluster", "score", "arc-link"],
    )


class TestTemplatePositioning:
    """The template skeleton must sit at the END of the prompt so
    Trendyol-LLM 7B's recency bias helps it follow the structure
    on heavy-day prompts (25-30K tokens)."""

    def test_template_appears_at_end(self) -> None:
        prompt = build_writer_prompt(_heavy_payload())
        title_pos = prompt.index(DOCUMENT_TITLE)
        assert title_pos >= len(prompt) * 0.70, (
            f"DOCUMENT_TITLE at position {title_pos} is not in the last "
            f"30% of prompt (length {len(prompt)})"
        )

    def test_data_appears_before_template(self) -> None:
        prompt = build_writer_prompt(_payload())
        data_pos = prompt.index("BUGÜNÜN İÇERİĞİ")
        title_pos = prompt.index(DOCUMENT_TITLE)
        assert data_pos < title_pos, (
            "BUGÜNÜN İÇERİĞİ must appear before DOCUMENT_TITLE"
        )

    def test_template_lead_in_present(self) -> None:
        prompt = build_writer_prompt(_payload())
        assert TEMPLATE_LEAD_IN in prompt


# ── System / user split (2026-05-27) ─────────────────────────────────────


class TestPromptSplit:
    def test_build_writer_user_omits_system_role(self) -> None:
        user = build_writer_user(_payload())
        assert SYSTEM_ROLE not in user

    def test_build_writer_system_returns_system_role(self) -> None:
        assert build_writer_system() == SYSTEM_ROLE

    def test_build_writer_prompt_is_system_plus_user(self) -> None:
        payload = _payload()
        combined = build_writer_prompt(payload)
        assert combined == f"{build_writer_system()}\n\n{build_writer_user(payload)}"


# ── Section constraint reinforcement (2026-05-27) ────────────────────────


class TestSectionConstraints:
    def test_template_lead_in_mentions_eight_sections(self) -> None:
        assert "8" in TEMPLATE_LEAD_IN
        assert "ALT BÖLÜM EKLEME" in TEMPLATE_LEAD_IN

    def test_section_roster_lists_all_template_sections(self) -> None:
        for section in TEMPLATE_SECTIONS:
            assert section.marker in SECTION_ROSTER

    def test_section_roster_appears_before_template_skeleton(self) -> None:
        user = build_writer_user(_payload())
        roster_pos = user.index("GEÇERLİ BÖLÜMLER")
        title_pos = user.index(DOCUMENT_TITLE)
        assert roster_pos < title_pos

    def test_discipline_rules_mention_defcon_section_distinction(self) -> None:
        assert "şablon bölümlerini bölme" in DISCIPLINE_RULES


# ── Per-section prompt tests (2026-05-27 per-section refactor) ─────────


def _multi_defcon_payload() -> BriefingPayload:
    """Payload with clusters in multiple DEFCON buckets for isolation tests."""
    return BriefingPayload(
        date=date(2026, 5, 27),
        run_id="run_multi",
        clusters_by_defcon={
            int(DEFCON.SEVERE): [
                ClusterView(
                    id="cl_severe",
                    headline="Ciddi olay başlığı",
                    summary="Ciddi özet.",
                    category="POLİTİKA",
                    final_defcon=int(DEFCON.SEVERE),
                    confidence="YÜKSEK",
                    bands_present=["gov_aligned"],
                    arc_id=None,
                    sources=[{"source_id": "sabah", "band": "gov_aligned"}],
                    is_social_only=False,
                ),
            ],
            int(DEFCON.MATERIAL): [
                ClusterView(
                    id="cl_material",
                    headline="Materyal olay başlığı",
                    summary="Materyal özet.",
                    category="EKONOMİ",
                    final_defcon=int(DEFCON.MATERIAL),
                    confidence="ORTA",
                    bands_present=["centrist"],
                    arc_id=None,
                    sources=[{"source_id": "bianet", "band": "centrist"}],
                    is_social_only=False,
                ),
            ],
            int(DEFCON.ROUTINE): [
                ClusterView(
                    id="cl_routine",
                    headline="Gündem olay başlığı",
                    summary="Gündem özet.",
                    category="TOPLUM",
                    final_defcon=int(DEFCON.ROUTINE),
                    confidence="DÜŞÜK",
                    bands_present=["independent"],
                    arc_id=None,
                    sources=[{"source_id": "diken", "band": "independent"}],
                    is_social_only=False,
                ),
            ],
        },
        open_arc_updates=[
            ArcView(
                id="arc_open_001",
                headline="Açık hikaye başlığı",
                summary="Devam ediyor.",
                state="OPEN",
                peak_defcon=int(DEFCON.SEVERE),
                category="YARGI",
                last_update_at=None,
                created_at=None,
            ),
        ],
        resolved_arcs=[
            ArcView(
                id="arc_resolved_001",
                headline="Kapatılan hikaye başlığı",
                summary="Çözüldü.",
                state="RESOLVED",
                peak_defcon=int(DEFCON.MATERIAL),
                category="EKONOMİ",
                last_update_at=None,
                created_at=None,
            ),
        ],
        peak_defcon=int(DEFCON.SEVERE),
        cluster_count=3,
        arc_count=2,
        open_arc_count=1,
        ambient_count=0,
        failed_sources=[],
        stages_done=["ingest", "normalize", "cluster", "score", "arc-link"],
    )


class TestBuildSectionUser:
    def test_build_section_user_includes_only_target_bucket(self) -> None:
        user = build_section_user(_multi_defcon_payload(), 0)
        assert "Ciddi olay başlığı" in user
        assert "Materyal olay başlığı" not in user
        assert "Gündem olay başlığı" not in user

    def test_build_section_user_for_open_arcs(self) -> None:
        user = build_section_user(_multi_defcon_payload(), 2)
        assert "Açık hikaye başlığı" in user
        assert "Ciddi olay başlığı" not in user

    def test_build_section_user_for_resolved_arcs(self) -> None:
        user = build_section_user(_multi_defcon_payload(), 6)
        assert "Kapatılan hikaye başlığı" in user
        assert "Açık hikaye başlığı" not in user

    def test_build_section_user_includes_discipline_rules(self) -> None:
        payload = _multi_defcon_payload()
        for idx in range(7):
            user = build_section_user(payload, idx)
            assert DISCIPLINE_RULES in user, f"DISCIPLINE_RULES missing for idx={idx}"

    def test_build_section_user_omits_section_marker_from_text(self) -> None:
        payload = _multi_defcon_payload()
        for idx in range(7):
            user = build_section_user(payload, idx)
            for line in user.splitlines():
                assert not line.startswith("## ❯"), (
                    f"Section marker as standalone line in user message idx={idx}: {line}"
                )


class TestBuildSystemLogSection:
    def test_system_log_no_failures_omits_failure_line(self) -> None:
        output = build_system_log_section(_multi_defcon_payload(), [])
        assert "Başarısız bölüm üretimi" not in output

    def test_system_log_with_failures_includes_failure_line(self) -> None:
        output = build_system_log_section(_multi_defcon_payload(), [3, 5])
        assert "Başarısız bölüm üretimi" in output
        assert "DEFCON 4 · GÜNDEM" in output
        assert "AMBİYANS · DEFCON 5" in output
