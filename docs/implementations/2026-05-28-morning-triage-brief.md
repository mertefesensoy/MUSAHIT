# MÜŞAHİT · Morning Triage Brief · 2026-05-28

> *Date · 2026-05-28 (Thu)*
> *Mode · AUTONOMOUS · operator is away · no questions can be asked*
> *Scope · three issues from run_20260528 · clusterer crash · embedder
> resilience · writer state and hardening*
> *Outcome target · all three issues resolved and test-verified, or
> blockers cleanly documented*

This brief is the **authoritative spec** for an unattended work session.
The operator is unreachable. Every decision is either made here or made
by the agent from diagnostics described here. Do not stall waiting for
input. When a decision point appears, follow the decision tree; if none
fits, take the safest option and record the choice in the run report.

---

## ❯ How to operate (read first)

1. **Work the issues in order · 1 → 2 → 3.** They have light coupling
   (1 and 2 both concern clustering/embedding; 3 is independent).
   Sequential, not parallel · do NOT fan out subagents that edit
   overlapping files unsupervised. Parallel diagnostics (read-only) are
   fine; parallel edits are not.

2. **Test-driven at every step.** No issue is "done" until its tests are
   written and green. Implement → run tests → if red, read the failure,
   fix, re-run → repeat until green. Then move on.

3. **Commit incrementally.** After each issue reaches green, commit with
   a clear message. This guarantees no work is lost if a later issue goes
   sideways. One commit per issue minimum.

4. **Never do destructive operations.** No `git reset --hard`, no
   `git push` (operator pushes), no force operations, no deleting files
   under `briefings/`, no dropping DB tables, no `rm` of anything not
   created during this session.

5. **FILE-PROTECTED · never edit** · `musahit/sources.py` ·
   `musahit/ingest/poller.py` · `musahit/score/defcon.py`. If a fix
   seems to require touching these, it is the wrong fix · find another
   way or document the blocker.

6. **Ollama may be flaky.** The 02:00 failure was an Ollama `/api/embed`
   500 under memory pressure. Unit tests with fakes are the PRIMARY gate.
   Live smokes (`--stage X --force`) are CONFIRMATORY · if a smoke 500s,
   that is data for Issue 2, not a reason to stop. Never let a flaky
   smoke block a unit-test-green fix from being committed.

7. **If blocked after ~5 honest retries on one issue**, write the blocker
   into the run report (see end), commit what works, and move to the next
   issue. Do not burn the whole session on one stuck problem.

8. **At the end**, write `docs/implementations/2026-05-28-morning-triage-report.md`
   summarizing what was done per issue, what passed, what's blocked, and
   what the operator should verify. This is the first thing the operator
   reads when back.

---

## ❯ Situation · what happened at 02:00

`run_20260528` fired on schedule, ingested and normalized 489 articles,
then the **cluster stage raised and was caught** by the orchestrator;
the pipeline continued on empty data:

```
stages_done: [ingest, normalize, score, arc-link, write, tts]   ← no "cluster"
failed_stages: [{"name": "cluster",
                 "reason": "ValueError: zip() argument 2 is shorter than argument 1"}]
clusters_scored: 0 · arcs_joined: 0 · arcs_seeded: 0
writer_used_fallback: true
```

Cluster-stage log sequence:

```
cluster_start · eligible: 707
cluster_embed_failed · count: 451 · "500 Internal Server Error" for /api/embed
stage_failed · ValueError: zip() argument 2 is shorter than argument 1
  File ".../clusterer.py", line 121, in run
```

`ollama ps` now shows nothing loaded; `ollama list` shows all four
models present (trendyol 4.4GB · qwen2.5 4.7GB · bge-m3 1.2GB ·
granite4 2.1GB). So Ollama is healthy now · the 500 was transient.

The briefing that shipped this morning is therefore empty of clustered
content · the writer fallback had nothing to summarize.

---

# Issue 1 · Clusterer converts a recoverable embed failure into a crash

## Diagnosis

In `musahit/cluster/clusterer.py`:

```python
async def _embed_articles(self, articles):
    texts = [self._embedding_input(a) for a in articles]
    if not texts:
        return []
    try:
        return await self._embedder.embed(texts)
    except Exception as exc:
        _log.warning("cluster_embed_failed", count=len(texts), error=...)
        return []                       # ← swallows failure, returns empty
```

Then in `run()` around line 121:

```python
vectors = await self._embed_articles(embeddable)   # [] when embed failed
self._persist_embeddings(embeddable, vectors)       # no-op on empty
buckets = {}
for a, v in zip(embeddable, vectors, strict=True):  # ← 451 vs 0 → ValueError
    ...
```

The `except` returns `[]`; the `strict=True` zip then raises because
`embeddable` (451) and `vectors` (0) mismatch. The docstring on
`_embed_articles` claims "Per-batch failures bubble up · see the
try/except in run()" but **there is no try/except in run()** · the
comment is stale and the actual behavior is swallow-then-crash.

Two facts to keep in mind:
- The `strict=True` zip is CORRECT · it is the guard that refuses to
  silently misalign articles with wrong vectors. Do not remove it.
- The orchestrator already catches stage exceptions and marks the stage
  failed without killing the pipeline · so the pipeline "completing" is
  expected. The problems are (a) the failure is reported as a cryptic
  zip error instead of "embedding failed", and (b) downstream stages run
  on empty data producing a misleading briefing.

## Decision tree

Run this diagnostic first:

```
# Confirm the exact line and surrounding logic
view musahit/cluster/clusterer.py   (read run() and _embed_articles fully)
# Confirm the embedder's contract · all-or-nothing or per-item?
view musahit/cluster/embedder.py
```

Then decide the fix scope:

- **If `embedder.embed()` is all-or-nothing** (returns full list or
  raises/empties) → implement **Solution A** (mandatory) and the embed
  failure becomes a clean, explicit, RE-RUNNABLE stage failure. Partial
  alignment (Solution B) is not possible without embedder changes, which
  belong to Issue 2.

- **If `embedder.embed()` can be made to return aligned per-item results
  cleanly** (e.g. it already batches internally and could return
  `list[list[float] | None]`) → implement **Solution A** AND **Solution B**
  (partial alignment) so a partial embed still clusters the successful
  articles. Only do B if it does not require touching FILE-PROTECTED
  files and the change is contained to `embedder.py` + `clusterer.py`.

## Solution A · mandatory · explicit re-runnable failure (no cryptic crash)

Goal: when embeddings are unavailable or incomplete, the cluster stage
fails with a CLEAR reason and remains re-runnable · never a raw zip
ValueError, never silently marking itself done on empty data.

Implementation:

1. In `run()`, immediately after `vectors = await self._embed_articles(embeddable)`,
   add a guard BEFORE `_persist_embeddings` and the zip:

   ```python
   if len(vectors) != len(embeddable):
       log.warning(
           "cluster_embed_incomplete",
           expected=len(embeddable),
           got=len(vectors),
       )
       raise EmbeddingUnavailableError(
           f"embedding returned {len(vectors)} vectors for "
           f"{len(embeddable)} articles · cluster stage cannot proceed"
       )
   ```

2. Define `EmbeddingUnavailableError(RuntimeError)` at module level in
   `clusterer.py` (or in a small `musahit/cluster/errors.py` if cleaner).
   This makes the orchestrator's `failed_stages` reason read
   `EmbeddingUnavailableError: embedding returned 0 vectors for 451
   articles · cluster stage cannot proceed` instead of the cryptic zip
   error.

3. Keep `_embed_articles` returning `[]` on total failure (so the guard
   catches it) BUT change its warning to make the downstream impact
   explicit:

   ```python
   _log.warning("cluster_embed_failed", count=len(texts),
                error=f"{type(exc).__name__}: {exc}",
                stage_impact="cluster_will_fail_and_be_rerunnable")
   ```

4. **Re-runnability** · because the stage raises (rather than marking
   itself done), `cluster` stays out of `stages_done`. A later
   `python -m musahit.pipeline run --date 2026-05-28 --stage cluster --force`
   will retry it. Confirm by reading how `_mark_stage_done` is gated ·
   it must NOT be called on the failure path.

Rationale for raising vs. skipping-gracefully: an OSINT briefing that
ships looking complete but is actually empty is worse than a briefing
that is visibly missing because a stage failed. Raising keeps the
failure honest and the work re-runnable. Issue 2 makes the raise rare.

## Solution B · stretch · partial alignment (only if embedder permits cleanly)

If `embedder.embed()` can return `list[list[float] | None]` (None for
items that failed even after Issue 2's retries), then:

1. `_embed_articles` returns the aligned list (same length as input,
   Nones for failures).
2. In `run()`, build pairs filtering Nones:
   ```python
   pairs = [(a, v) for a, v in zip(embeddable, vectors, strict=True)
            if v is not None]
   embed_failed = len(embeddable) - len(pairs)
   if embed_failed:
       log.warning("cluster_embed_partial", failed=embed_failed,
                   clustered=len(pairs))
   ```
   Now `strict=True` is satisfied (both full length, Nones included) and
   clustering proceeds on the successful subset.
3. Only raise `EmbeddingUnavailableError` if `len(pairs) == 0` (total
   failure · nothing to cluster).

B is strictly better than A but more invasive. Implement A always; add B
only if the embedder change is clean and contained.

## Tests (tests/test_cluster/test_clusterer.py)

- `test_embed_total_failure_raises_clear_error` · mock embedder returns
  `[]` (or raises) · assert `EmbeddingUnavailableError` propagates with a
  message mentioning vector/article counts · assert NO raw ValueError ·
  assert `cluster` NOT in stages_done (re-runnable).
- `test_embed_length_mismatch_raises_clear_error` · mock returns fewer
  vectors than articles · same assertions.
- If Solution B implemented · `test_embed_partial_clusters_successful_subset`
  · mock returns some Nones · assert clustering proceeds on non-None
  pairs · assert `cluster_embed_partial` logged · assert stage marked done.
- Existing happy-path clustering tests must still pass.

## Verification (confirmatory, Ollama-dependent)

```
python -m musahit.pipeline run --date 2026-05-28 --stage cluster --force
```

- If Ollama healthy · expect real clustering (some clusters created).
- If Ollama 500s again · expect a clean `EmbeddingUnavailableError` in
  `failed_stages` (NOT a zip ValueError), and `cluster` re-runnable.
Either outcome confirms Issue 1 fixed. Do not block on getting clusters
· the fix is about the failure being clean and re-runnable.

---

# Issue 2 · Embedder is not resilient to transient Ollama 500s

## Diagnosis

The 02:00 `/api/embed` 500 was almost certainly memory pressure. The
pipeline holds multiple 4-5GB models across stages on a single laptop.
Clustering runs early (stage 3) and needs bge-m3 (1.2GB), but if prior
models are resident or keep-alive holds them, a large embed batch can
OOM Ollama → 500. The 442-second total runtime (vs ~15 min normal)
corroborates heavy memory thrash.

`EMBED_BATCH_LIMIT: int = 50` exists in `clusterer.py`, and the docstring
says the embedder batches in 50s internally · but the failure log shows
`count: 451`, which is the total text count, not a batch · so confirm
whether batching actually happens in the embedder.

## Decision tree

```
view musahit/cluster/embedder.py   (read embed() fully · how it batches,
                                     whether it retries, how it calls Ollama)
```

Determine:
- Does `embed()` actually slice into batches, or send all texts at once?
- Is there any retry / backoff today? (Likely none.)
- What's the Ollama call shape · `/api/embed` with a list, or per-text?

Then implement the resilience layer that fits the current structure.

## Solution · retry with backoff + adaptive batch halving

Add to `embedder.py` (the production `OllamaEmbeddingClient` or
equivalent · NOT the fake, NOT the Protocol):

1. **Explicit batching** · if not already, slice inputs into batches of
   a configurable size (default 16 · smaller than 50 to reduce per-call
   memory). Make the batch size a module constant
   `DEFAULT_EMBED_BATCH_SIZE: int = 16`.

2. **Per-batch retry with exponential backoff** · on a transient error
   (HTTP 5xx, timeout, connection error), retry the batch up to N times
   (default 3) with backoff (e.g. 2s, 4s, 8s). Use `asyncio.sleep`.

3. **Adaptive batch halving on persistent failure** · if a batch still
   fails after retries, split it in half and retry each half once
   (recursively, down to a single item). A single item that still fails
   after retries is treated as a hard per-item failure:
   - If Solution B (Issue 1) is implemented · return `None` for that
     item's position (aligned partial result).
   - If only Solution A · raising is acceptable, but PREFER to return a
     partial-with-None list if the embedder signature allows, so Issue 1
     Solution B can be layered later. At minimum, do not let one bad item
     fail the entire batch silently.

4. **Warm the model** · optionally, before the first batch, issue a tiny
   warmup embed (single short string) to force bge-m3 to load, so the
   first real batch does not pay cold-load + large-batch cost
   simultaneously. Low priority · add only if clean.

5. **Logging** · log `embed_batch_retry` (with attempt, batch size),
   `embed_batch_halved`, `embed_item_failed` (with index) so the operator
   can see resilience working in the nightly log.

Keep the embedder's public `embed()` signature compatible with existing
callers. If adding per-item None returns, update the type hint to
`list[list[float] | None]` and ensure the clusterer (Issue 1 Solution B)
consumes it; if staying all-or-nothing, retries/halving still reduce the
chance of total failure.

## Tests (tests/test_cluster/test_embedder.py · create if absent)

Use a fake httpx client or mock the Ollama call:

- `test_embed_retries_on_500_then_succeeds` · first call 500s, second
  succeeds · assert final result correct · assert retry logged.
- `test_embed_halves_batch_on_persistent_failure` · a batch of N fails,
  halves succeed · assert all vectors returned in order.
- `test_embed_single_item_failure_handling` · one item fails all retries
  · assert it becomes None (if partial-aligned) or raises cleanly (if
  all-or-nothing) per the chosen design.
- `test_embed_happy_path_batches_correctly` · inputs > batch size · assert
  correct number of batch calls · assert output length == input length
  and order preserved.
- `test_embed_empty_input_returns_empty` · `embed([])` returns `[]`.

Order preservation is critical · the clusterer zips by position. Any
batching/halving MUST reassemble results in original input order.

## Verification (confirmatory)

```
python -m musahit.pipeline run --date 2026-05-28 --stage cluster --force
```

If Ollama is under load and 500s a batch, the retry/halving should now
recover and produce embeddings rather than failing the stage. If it
still fails entirely, Issue 1's clean error fires · acceptable, and
documents that the laptop genuinely cannot serve embeddings under that
load (a hardware/scheduling issue, not a code bug · note in report).

---

# Issue 3 · Writer state confusion + per-section hallucination hardening

This issue has two parts. Do 3a first (establish ground truth), then 3b
(harden so the writer never ships hallucinated content).

## Issue 3a · Establish the definitive briefer state

### Diagnosis

Last night's commit sequence tangled. `git diff HEAD -- musahit/writer/briefer.py`
returns clean (working tree matches HEAD), but
`pytest tests/test_writer/test_briefer.py::TestHappyPath::test_per_section_compose`
fails with `assert 4 == 7` and the captured log shows SINGLE-SHOT
behavior (`writer_validator_failed attempt=0..3`, `writer_fallback`).

Per-section `_compose` would call `generate_with_prefill` 7 times
(SİSTEM LOG is deterministic, not an LLM call) · so `call_count == 7`.
Single-shot `_compose` calls `generate` up to 4 times (1 + 3 retries) ·
`call_count == 4`. The test sees 4 · so at test runtime the briefer was
single-shot. This contradicts an earlier diff that showed HEAD as
per-section. Resolve the contradiction definitively.

### Decision tree

```
# Fresh restore from HEAD, then inspect the actual file
git checkout HEAD -- musahit/writer/briefer.py
grep/Select-String musahit/writer/briefer.py for these markers:
   "range(7)"
   "generate_with_prefill"
   "build_section_user"
   "build_system_log_section"
   "max_retries"
   "_retry_prompt"
```

- **If per-section markers present** (`range(7)`, `generate_with_prefill`,
  `build_section_user`) AND single-shot markers absent (`_retry_prompt`,
  `max_retries` retry loop) → HEAD IS per-section. The test failure is a
  fixture/cache/fake issue. Investigate:
  - Is there a stale `__pycache__` for briefer? Delete `__pycache__`
    dirs under `musahit/writer/` and `tests/test_writer/` and re-run.
  - Does the test's `FakeLlmClient` implement `generate_with_prefill`?
    If the fake only has `generate`, the per-section briefer calling
    `generate_with_prefill` would error or behave oddly. Read
    `musahit/score/llm_client.py` FakeLlmClient and the test's fake.
  - Reconcile and make `test_per_section_compose` pass against the
    per-section briefer.

- **If per-section markers ABSENT** and single-shot markers present →
  HEAD's briefer is single-shot · the per-section refactor was LOST in
  last night's commit tangle. The CODE is gone but the SPEC survives at
  `docs/implementations/2026-05-27-per-section-writer-briefing.md`.
  Re-implement the per-section briefer from that spec (the spec is
  complete and authoritative · sections "Implementation requirements >
  File · musahit/writer/briefer.py" and the architecture pseudocode).
  Then proceed to 3b. Note in the report that briefer was regenerated
  from spec due to the commit tangle.

Either way, the end state of 3a is: briefer.py is definitively
per-section, and `tests/test_writer/test_briefer.py` passes.

### Important · do not lose the single-shot version

Before any 3a surgery, the single-shot briefer is the version that ran
safely at 02:00. It lives at commit `723e3e8`. If you need it later, it
is recoverable via `git show 723e3e8:musahit/writer/briefer.py`. Do not
delete history.

## Issue 3b · Harden so the writer never ships hallucinated content

### Diagnosis · the four failure modes from the 2026-05-27 specimen

The per-section smoke produced a STRUCTURALLY valid briefing
(`writer_used_fallback: false`) that was CONTENT garbage. Saved at
`briefings/2026/05/27/briefing.per-section-hallucinated.md`. Read it for
ground truth. The four modes:

1. **Empty-section hallucination** · DEFCON 1-2 section had no clusters
   (peak DEFCON was 3) yet the model invented COVID and drug-bust
   headlines with no arc_id, no sources. Sections 0 (DEFCON 1-2),
   4 (DİKKAT), 5 (AMBİYANS) were empty and fabricated.
2. **Prompt echo** · DİKKAT section returned the user-message text
   verbatim · DISCIPLINE_RULES bullets, "BÖLÜM VERİSİ:", "(bugün öğe
   yok)", "ÇIKTI (yalnızca bu bölümün içeriği · marker hazır
   verilmiştir):".
3. **Chain-of-thought leak** · AMBİYANS section emitted "Adım 1: ..." /
   "Gerekçe: ..." reasoning scaffolding as if showing work.
4. **Hallucinated entities** · invented content with no grounding in the
   section payload.

Sections that were FAITHFUL · DEFCON 3 (MATERYAL), DEFCON 4 (GÜNDEM),
KAPATILAN HİKAYELER, SİSTEM LOG. Preserve whatever lets these succeed.

### Success criterion (revised · important)

The writer is NOT required to produce real LLM prose for every section.
Trendyol 7B is genuinely unreliable. The success criterion is:

> **The writer must never ship hallucinated, echoed, or CoT-scaffolding
> content. Every section is either (a) real grounded LLM prose, (b) an
> honest "bugün öğe yok" note for genuinely empty sections, or (c) an
> honest stub for sections where the LLM produced bad output.**

A briefing that is half real prose and half honest stubs is a SUCCESS.
A briefing with one fabricated headline is a FAILURE.

### Solution · empty-section short-circuit + validator hardening

**Part 1 · Empty-section short-circuit (highest leverage)**

Sections with no payload data must NOT call the LLM at all. They emit a
canonical empty-note deterministically.

In `_compose` (or `build_section_user`), determine per-section emptiness:
- Section 0 (DEFCON 1-2) · empty if no clusters in DEFCON 1-2 buckets
- Section 1 (DEFCON 3) · empty if no DEFCON 3 clusters
- Section 2 (AÇIK GELİŞMELER) · empty if no open_arc_updates
- Section 3 (DEFCON 4) · empty if no routine clusters
- Section 4 (DİKKAT) · empty if no social-only clusters
- Section 5 (AMBİYANS) · empty if no ambient clusters
- Section 6 (KAPATILAN HİKAYELER) · empty if no resolved_arcs
- Section 7 (SİSTEM LOG) · never empty · always deterministic (existing)

For an empty section, skip the LLM and emit:
```
{marker}

Bugün bu bölümde öğe yok.
```
This single change eliminates failure modes 1 and 2 for empty sections
(the most common case · empties caused the worst hallucinations).

**Part 2 · Validator hardening for non-empty sections**

Extend `validate_section(text, section_idx)` (or add a
`validate_section_content` layer) to REJECT a section whose text:

- Contains any DISCIPLINE_RULES marker substring · reject prompt echo.
  Check for literal substrings like `"KURALLAR (ADR-009)"`,
  `"BÖLÜM VERİSİ:"`, `"ÇIKTI (yalnızca"`, `"GÖREV ·"`. Maintain a small
  `_PROMPT_ECHO_MARKERS` list. Any hit → invalid → section becomes stub.
- Contains chain-of-thought scaffolding · lines matching
  `^\s*Adım\s*\d+\s*:` or `^\s*Gerekçe\s*:` (Turkish CoT). Maintain
  `_COT_PATTERNS`. Any hit → invalid → stub.
- (Stretch · entity grounding) For sections tied to arcs/clusters, any
  `arc_YYYYMMDD_NNNN` in the output must appear in that section's
  payload. Fabricated arc_ids → invalid → stub. This requires passing
  the section's valid arc_id set into the validator. Implement only if
  clean; modes 1-3 are mandatory, mode 4 is stretch.

When a non-empty section fails the hardened validator, it becomes a
`render_section_stub(idx)` per the existing per-section fallback path
(Decision 1 in the original spec) · honest stub, not hallucination.

**Part 3 · Reduce CoT/echo at the source (optional, low risk)**

The per-section user message currently includes the full DISCIPLINE_RULES
and a verbose "ÇIKTI (...)" trailer · which the model sometimes echoes.
Consider trimming the per-section user message to the minimum the model
needs · the section's data plus a one-line instruction. Keep
DISCIPLINE_RULES in the SYSTEM message (where it belongs) rather than the
user message, so it cannot be echoed into the section body. Do this only
if it does not regress the faithful sections (DEFCON 3/4); verify by
smoke. If risky, skip · the validator catches echo anyway.

### Tests (tests/test_writer/test_validator.py and test_briefer.py)

- `test_validate_section_rejects_prompt_echo` · text containing
  "KURALLAR (ADR-009)" → invalid.
- `test_validate_section_rejects_cot_adim` · text with "Adım 1:" →
  invalid.
- `test_validate_section_rejects_cot_gerekce` · text with "Gerekçe:" →
  invalid.
- `test_validate_section_accepts_clean_prose` · normal section content →
  valid.
- (If mode 4) `test_validate_section_rejects_unknown_arc_id` · output
  cites an arc_id not in the section payload → invalid.
- `test_empty_section_skips_llm_emits_note` · section with empty payload
  → LLM NOT called for that section → output is the canonical
  "Bugün bu bölümde öğe yok." note.
- `test_empty_sections_do_not_count_as_fallback` · a run where some
  sections are empty (notes) and others are real → `writer_used_fallback`
  is false · `writer_sections_fallback` does NOT include the empty-note
  sections (empties are not failures, they are correct).
- Existing per-section tests adapted to the new empty-short-circuit
  (call counts change · a run with E empty sections calls the LLM
  `7 - E` times, not 7).

### Verification (confirmatory, Ollama-dependent)

```
python -m musahit.pipeline run --date 2026-05-28 --stage write --force
```
Then open `briefings/2026/05/28/briefing.md` and check:
- No fabricated headlines in empty sections (they read "Bugün bu bölümde
  öğe yok.").
- No DISCIPLINE_RULES / "BÖLÜM VERİSİ" / "ÇIKTI" echo anywhere.
- No "Adım 1:" / "Gerekçe:" scaffolding.
- Sections with real data read as coherent Turkish prose OR honest stubs.

If Trendyol still produces bad output for non-empty sections, those
become stubs · that is a PASS (honest degradation). The failure mode we
are killing is fabrication, not stubbing.

NOTE · this smoke needs clustered data for 2026-05-28. If Issue 1/2 left
2026-05-28 without clusters, run against 2026-05-27 instead (which has
real clusters in the DB) · `--date 2026-05-27 --stage write --force`.
Either date exercises the writer hardening.

---

## ❯ Cross-cutting rules

- **Scope of edits** · Issues 1-2 touch `musahit/cluster/clusterer.py`,
  `musahit/cluster/embedder.py`, optionally a new `musahit/cluster/errors.py`,
  and their tests under `tests/test_cluster/`. Issue 3 touches
  `musahit/writer/briefer.py`, `musahit/writer/prompt.py`,
  `musahit/writer/validator.py`, `musahit/writer/fallback.py`, and tests
  under `tests/test_writer/`. Nothing else.
- **Never touch** FILE-PROTECTED (`sources.py`, `poller.py`, `defcon.py`)
  or any stage outside the two above (`normalize`, `score`, `arcs`,
  `tts`, `ingest`).
- **Full suite must stay green** · after each issue, run the FULL
  `pytest` (not just the issue's tests) to confirm no cross-stage
  regression. If an unrelated test breaks, you changed something out of
  scope · revert that part.
- **ruff clean** after every issue.
- **Commit messages** · one per issue, e.g.
  `fix(cluster): clean re-runnable failure on embed unavailability (Issue 1)`,
  `feat(cluster): embedder retry + adaptive batch halving (Issue 2)`,
  `fix(writer): empty-section short-circuit + anti-hallucination validator (Issue 3)`.
- **Do not push.** Commit locally only.

---

## ❯ Final acceptance checklist

Mark each in the run report:

- [ ] Issue 1 · embed failure raises a clear `EmbeddingUnavailableError`
      (no cryptic zip ValueError) · cluster stage re-runnable · tests
      green.
- [ ] Issue 1 · (if done) partial alignment clusters the successful
      subset.
- [ ] Issue 2 · embedder retries transient 500s with backoff · adaptive
      batch halving · order preserved · tests green.
- [ ] Issue 3a · briefer.py definitively per-section ·
      `tests/test_writer/test_briefer.py` green (regenerated from spec if
      the code was lost).
- [ ] Issue 3b · empty sections emit honest notes (no LLM call) ·
      validator rejects prompt echo + CoT (+ entity grounding if done) ·
      bad sections become honest stubs · tests green.
- [ ] FULL pytest suite green.
- [ ] ruff clean.
- [ ] One commit per issue, local only, no push.
- [ ] Run report written to
      `docs/implementations/2026-05-28-morning-triage-report.md`.

---

## ❯ Recommended order of operations

1. Read this brief fully.
2. Read `clusterer.py`, `embedder.py`, `briefer.py`, `validator.py`,
   `prompt.py`, `fallback.py`, the FakeLlmClient, and the 2026-05-27
   spec + hallucinated specimen. Build the mental model before editing.
3. Issue 1 · implement → test → full suite → ruff → commit.
4. Issue 2 · implement → test → full suite → ruff → commit.
5. Issue 3a · resolve state → test → commit (may fold into 3b commit).
6. Issue 3b · implement → test → full suite → ruff → commit.
7. Confirmatory smokes where Ollama permits.
8. Write the run report. List anything blocked or deferred, with enough
   detail that the operator can pick it up cold.

---

## ❯ One-line takeaway

> *Make the embed failure honest and re-runnable (1), make embedding
> resilient so it rarely fails (2), and make the writer incapable of
> shipping fabricated content (3) · test every step, commit per issue,
> never push, document blockers, do not stall.*

---

*End of brief. Operator is at the sea. Work autonomously and leave the
repo greener than you found it.*
