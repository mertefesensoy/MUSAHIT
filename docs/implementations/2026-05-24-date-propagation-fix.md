# Implementation: Date propagation fix · CLI target_date → writer briefing date

**Date** · 2026-05-24
**Author** · Mert Efe Şensoy
**ADR refs** · ADR-006 · ADR-007 · ADR-009

---

## ❯ Problem / Motivation

The 2026-05-23 smoke run was launched at 00:28 TR-local on
2026-05-24 (= 21:28 UTC on 2026-05-23). The operator's intent was
to produce the briefing for **today (TR-local) = 2026-05-24**.
The pipeline did the right thing for the run id · `run_20260524` ·
because the CLI computes that from `tr_local_date()`. But the
briefing landed under `briefings/2026/05/23/briefing.md` and the
`briefings` table row was dated 2026-05-23.

Two date-resolution paths that agreed during the daytime UTC window
diverged across the 21:00-24:00 UTC strip when TR is already on the
next day:

1. **CLI path** · `pipeline._cmd_run` → `_resolve_date("today")`
   → `tr_local_date()` → `date(2026, 5, 24)` → run_id
   `run_20260524`. **Correct.**
2. **Writer path** · `briefer.run` → `build_payload(conn, run_id)`
   → `pipeline_runs.started_at` (UTC, stamped by `utcnow()` in
   `_upsert_run_row`) → `.date()` → `date(2026, 5, 23)` →
   briefing path `briefings/2026/05/23/`. **Wrong.**

The on-disk briefing being one day behind every late-night run
breaks the operator's mental model and (if not fixed) would silently
overwrite the previous day's real briefing whenever the pipeline
ran late.

---

## ❯ Root cause

`musahit/writer/payload.py::build_payload` computed
`briefing_date = (started_at or datetime.utcnow()).date()`. There
was no path for the CLI's already-resolved TR-local date to reach
the writer · the orchestrator received the run_id (string) but
threw the target_date (which the CLI did have) away.

The fix is to make the date a first-class parameter that travels
the full call chain · CLI → Orchestrator → DefaultStageFactory →
Briefer → build_payload · instead of being recomputed from UTC at
the bottom.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/writer/payload.py` | `build_payload(conn, run_id, *, target_date=None)`. When provided, drives `BriefingPayload.date` directly. Fallback to `started_at.date()` preserved for legacy callers. |
| `musahit/writer/briefer.py` | `Briefer.__init__(*, target_date=None)` stores `self._target_date`. `Briefer.run` passes it to `build_payload`. Stage Protocol `run(run_id)` signature unchanged. |
| `musahit/orchestrator.py` | `Orchestrator.run(*, target_date=None, ...)` stores `self._current_target_date` for the duration of the run. `DefaultStageFactory.__init__(*, get_target_date=None)` accepts a closure that the factory invokes at Briefer-construction time. Orchestrator wires the default factory with `get_target_date=lambda: self._current_target_date`. |
| `musahit/pipeline.py` | `_cmd_run` passes the already-computed `target_date` to `orchestrator.run(target_date=target_date, ...)`. |
| `tests/test_writer/test_payload.py` | `TestTargetDatePropagation` (3 tests): target_date overrides started_at; legacy fallback preserved; midnight-crossing simulation. |
| `tests/test_writer/test_briefer.py` | `TestTargetDateInBriefer` (3 tests): markdown path uses target_date; briefings row date uses target_date; legacy fallback preserved. |
| `tests/test_orchestrator.py` | `TestTargetDatePropagation` (3 tests): orchestrator stores target_date for factory readback; DefaultStageFactory passes it to Briefer; omitting leaves it None. |
| `memory/build-progress.md` | "Step 16 follow-up · date propagation fix · 2026-05-24" entry. |
| `docs/implementations/2026-05-24-date-propagation-fix.md` | This file. |

---

## ❯ Implementation Approach

### Bind target_date at construction time, not dispatch time

The Stage Protocol is `async def run(self, run_id: str) -> Any`.
Adding `target_date` as a positional parameter would diverge every
stage from the protocol and force the orchestrator's
`asyncio.wait_for(stage.run(run_id), timeout=...)` dispatch to
special-case the writer.

Cleaner: bind `target_date` in `Briefer.__init__` and let
`Briefer.run(run_id)` read `self._target_date`. The factory builds
the Briefer right before the writer stage runs, so the binding
is fresh per run.

### Closure-based factory wiring

The orchestrator's `DefaultStageFactory` is constructed once at
`Orchestrator.__init__` time but called once per stage per run.
A single orchestrator instance might be re-used across multiple
`.run()` calls (some test fixtures do this, and the CLI does it
when looping over `resume` flows). Storing `target_date` directly
on the factory would cache stale values.

The closure indirection:

```python
self._current_target_date: date | None = None
self._stage_factory = stage_factory or DefaultStageFactory(
    conn, settings, get_target_date=lambda: self._current_target_date
)
```

means the factory reads the orchestrator's current value at the
moment it builds the Briefer · always fresh, never cached.

### TTS picks up the right date automatically

`musahit/tts/synthesizer.py::_resolve_briefing_date` queries
`SELECT date FROM briefings ORDER BY generated_at DESC LIMIT 1`.
After the writer writes the briefings row with the corrected
date, TTS reads back the same date. No synthesizer change
needed.

This is the same architectural decision that the always-ships
TTS placeholder relies on · TTS treats the briefings table as
the source of truth for "which date am I voicing?" rather than
recomputing from UTC. Once the writer is right, TTS is right.

### Cluster IDs untouched

`cl_YYYYMMDD_NNNN` ids are stamped from `cluster.created_at`
(article-publication time) at the cluster stage. That's a
deliberate identity tied to the news date, NOT the briefing
date. A cluster about an event from 2026-05-23 keeps its
`cl_20260523_*` id even when surfaced in the 2026-05-24
briefing.

### Backward-compat fallback in build_payload

```python
if target_date is not None:
    briefing_date = target_date
else:
    briefing_date = (started_at or datetime.utcnow()).date()
```

The legacy path stays · existing tests that don't pass
`target_date` produce the same briefing as before. New tests
exercise both branches explicitly so a future refactor that
removes the fallback won't break silently.

---

## ❯ Mathematical / Statistical Details

Not applicable · structural fix.

---

## ❯ Design Decisions

### Why a first-class target_date rather than UTC → TR conversion?

Alternative: leave `build_payload` driving from `started_at` but
convert UTC → TR-local inside the function. Rejected because:

- The function would need to know the TR timezone offset · 3
  hours fixed, no DST in Turkey since 2016, but encoding "TR has
  no DST" inline in the writer is brittle. Better to keep the
  timezone resolution at the CLI boundary where `tr_local_date()`
  already lives.
- The CLI takes `--date 2026-05-20` as an explicit date · with
  the UTC-conversion approach, that explicit date would still
  need a separate code path. First-class target_date handles
  both `today` and explicit dates uniformly.
- `started_at` legitimately needs to stay in UTC for arc-loading
  windows (`cutoff = now - timedelta(days=30)` etc.). Forcing it
  to TR-local would create new conversion bugs elsewhere.

### Why closure-based factory wiring rather than stored state?

Alternative: store `target_date` directly on the factory
instance, mutate it before each run. Rejected because:

- The orchestrator's responsibility for the run is clearer: it
  owns `_current_target_date` for the duration of `run()`. The
  factory just reads.
- A test-supplied factory (StageFactory injected via
  `Orchestrator.__init__`) bypasses DefaultStageFactory entirely.
  The closure pattern keeps the orchestrator's state available
  to whoever needs it (the `_spy_factory` in the new test reads
  `orchestrator._current_target_date` directly).
- Mutating shared state on a factory imported as a module-level
  singleton would be a footgun; the closure scopes the
  read-through to the specific orchestrator instance.

### Why no changes to Stage Protocol?

The Protocol is `async def run(self, run_id: str) -> Any`.
Adding `target_date` would require updating every stage
implementation (IngestPoller, Normalizer, Clusterer, Classifier,
ArcLinker, Briefer, Synthesizer) and every test fake
(`_SuccessStage`, `_RaisingStage`, `_SlowStage`, `_NoOpStage`,
`FakePiper`, etc.). Six of those seven stages don't need the
date. Construction-time binding scopes the change to the one
stage that does.

### Why the legacy fallback in build_payload?

Removing the `(started_at or datetime.utcnow()).date()` branch
would force every legacy test fixture to migrate at once. The
fallback lets us add the kwarg now, migrate the production
path immediately (CLI → Orchestrator → Briefer all pass
target_date), and migrate test fixtures over time. The new
test `test_no_target_date_falls_back_to_started_at` pins the
fallback's behavior so a future cleanup can find and intentionally
remove it.

---

## ❯ Verification

```powershell
# Linter clean.
python -m ruff check .

# Targeted subsuite · expected: 90 passed (81 prior + 9 new).
$env:PYTHONIOENCODING = "utf-8"
python -m pytest tests/test_writer/ tests/test_orchestrator.py -q

# Full suite · expected: 622 passed, 2 skipped (613 prior + 9 new).
python -m pytest tests/ -q
```

The 9 new tests pin:

1. **`TestTargetDatePropagation` in test_payload** · target_date
   overrides started_at-derived date; legacy fallback preserved;
   the "23:59 UTC + next-TR-day target" scenario produces the
   correct briefing date.
2. **`TestTargetDateInBriefer`** · Briefer with target_date
   writes to `YYYY/MM/DD` of the target date; briefings row date
   matches target_date; legacy fallback preserved.
3. **`TestTargetDatePropagation` in test_orchestrator** ·
   `Orchestrator.run(target_date=X)` stores X so a spy factory
   can read it; `DefaultStageFactory` reads from the closure on
   each call (no caching across runs with different dates);
   omitting target_date leaves `_current_target_date` None for
   legacy fallback.

---

## ❯ Operator caveats

- The 2026-05-23 `briefing.md` (and `briefing.mp3`) on disk are
  the buggy outputs from the smoke run. They stay as-is · audit
  trail of the bug is preserved per the convention established
  in the sibling 2026-05-24 fixes (arc cascade, category
  normalization, template placeholder, TTS silent MP3).
- The next smoke run launched after 21:00 UTC will produce a
  briefing dated for the TR-local day, not the UTC day · this
  is the operator-visible fix.
- `pipeline status --date 2026-05-24` reads the date from
  `pipeline_runs` not `briefings`, so its behavior is unchanged.
  The status display always agreed with the CLI's resolved
  date; the briefing path is what was drifting.
- Future stages that need TR-local date awareness should accept
  `target_date` via constructor, never recompute from `started_at`.
  The orchestrator's `_current_target_date` is the canonical
  per-run value.
- Test fixtures still using the `build_payload(conn, run_id)`
  signature (without `target_date`) keep working via the
  fallback path. New fixtures should pass `target_date` to make
  the intent explicit.

---

## ❯ Related Docs

- ADR-006 · storage
- ADR-007 · pipeline timing budget
- ADR-009 · briefing template
- `docs/implementations/2026-05-23-write.md` · original writer build
- `docs/implementations/2026-05-23-pipeline.md` · orchestrator build
- `docs/implementations/2026-05-24-arc-link-bug-fix.md` · sibling smoke-run fix
- `docs/implementations/2026-05-24-category-normalization.md` · sibling smoke-run fix
- `docs/implementations/2026-05-24-template-placeholder-fix.md` · sibling smoke-run fix
- `docs/implementations/2026-05-24-tts-silent-mp3-fix.md` · sibling smoke-run fix
