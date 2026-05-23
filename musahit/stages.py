"""Stage Protocol + ADR-007 timing budgets + the canonical stage order.

The pipeline orchestrator (build step 15) binds the seven stages
(ingest · normalize · cluster · score · arc-link · write · tts) into
one nightly run. Each stage is independently buildable and tested
already — this module is the lightweight contract that lets the
orchestrator dispatch over them uniformly.

The :class:`Stage` Protocol matches the shape every step-13-and-earlier
orchestrator already exposes: an ``async run(run_id) -> object`` method
(stages return dict summaries; the protocol uses ``object`` so dict
returns satisfy the contract structurally).

The :data:`STAGE_BUDGETS` map encodes ADR-007 § Pipeline timing budget
as :class:`StageTimingBudget` records. ``soft_minutes`` is the
operator-visible deadline; the orchestrator hard-times-out a stage at
``soft_minutes × 2`` per the goal-spec (a stage running 2× over its
budget is the ADR's ``STAGE_SLOW`` threshold — once we hit it, abort
the stage and continue with the next).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# ── Stage Protocol ─────────────────────────────────────────────────────────


class Stage(Protocol):
    """The shape every pipeline stage exposes.

    Concrete stages in earlier build steps already match this protocol
    — :class:`IngestPoller`, :class:`Normalizer`, :class:`Clusterer`,
    :class:`Classifier`, :class:`ArcLinker`, :class:`Briefer`,
    :class:`Synthesizer`. They return per-stage summary dicts; the
    orchestrator does not look at the return value (stages persist
    their own state in DuckDB), so the protocol uses ``object`` to
    keep the structural match generous.
    """

    async def run(self, run_id: str) -> Any: ...


# ── Stage names (canonical strings) ────────────────────────────────────────
#
# Used by the orchestrator for pipeline_runs.stages_done bookkeeping, by
# CLI --stage NAME filtering, and by the briefing's SİSTEM LOG footer.
# Hyphenated form for arc-link matches what step 12 already writes to
# stages_done.

STAGE_INGEST: str = "ingest"
STAGE_NORMALIZE: str = "normalize"
STAGE_CLUSTER: str = "cluster"
STAGE_SCORE: str = "score"
STAGE_ARC_LINK: str = "arc-link"
STAGE_WRITE: str = "write"
STAGE_TTS: str = "tts"

# Canonical execution order per ADR-001 § Stage definitions + § Stage
# checkpoints. Order is load-bearing: cluster must run after normalize
# (it reads articles), score after cluster (it reads cluster_articles),
# arc-link after score (it reads final_defcon), write after arc-link
# (it reads open_arcs), tts after write (it reads briefings.markdown_path).
STAGE_ORDER: tuple[str, ...] = (
    STAGE_INGEST,
    STAGE_NORMALIZE,
    STAGE_CLUSTER,
    STAGE_SCORE,
    STAGE_ARC_LINK,
    STAGE_WRITE,
    STAGE_TTS,
)


# ── Timing budgets per ADR-007 ─────────────────────────────────────────────


@dataclass(frozen=True)
class StageTimingBudget:
    """Per-stage soft deadline.

    ``soft_minutes`` is the ADR-007 target — informational, the
    orchestrator logs a ``STAGE_SLOW`` event when actual runtime
    exceeds it. The hard timeout is ``soft_minutes × 2`` exposed
    via :attr:`timeout_seconds`; once a stage exceeds that the
    orchestrator cancels its task and records a TIMEOUT failure
    in ``failed_stages``.

    Stored as ``float`` so test rigs can use fractional minutes for
    fast timeout tests without scaling the budget API.
    """

    soft_minutes: float

    @property
    def soft_seconds(self) -> float:
        return self.soft_minutes * 60.0

    @property
    def timeout_seconds(self) -> float:
        # Per build-step-15 goal: hard timeout = 2 × soft deadline.
        # Matches the ADR-007 STAGE_SLOW threshold (a stage running
        # 2× over its budget is "slow enough to abort").
        return self.soft_minutes * 60.0 * 2.0


# Pulled directly from ADR-007 § Pipeline timing budget (the 01:00 →
# 07:00 schedule, minutes per stage). The 06:30 → 06:45 "artifact lock +
# backup" window is NOT a stage in this list — it lives in the post-tts
# liveness probe (step 18) and the dashboard-served backup script (step
# later).
STAGE_BUDGETS: dict[str, StageTimingBudget] = {
    STAGE_INGEST: StageTimingBudget(soft_minutes=60.0),
    STAGE_NORMALIZE: StageTimingBudget(soft_minutes=30.0),
    STAGE_CLUSTER: StageTimingBudget(soft_minutes=60.0),
    STAGE_SCORE: StageTimingBudget(soft_minutes=60.0),
    STAGE_ARC_LINK: StageTimingBudget(soft_minutes=30.0),
    STAGE_WRITE: StageTimingBudget(soft_minutes=60.0),
    STAGE_TTS: StageTimingBudget(soft_minutes=30.0),
}


__all__ = [
    "STAGE_ARC_LINK",
    "STAGE_BUDGETS",
    "STAGE_CLUSTER",
    "STAGE_INGEST",
    "STAGE_NORMALIZE",
    "STAGE_ORDER",
    "STAGE_SCORE",
    "STAGE_TTS",
    "STAGE_WRITE",
    "Stage",
    "StageTimingBudget",
]
