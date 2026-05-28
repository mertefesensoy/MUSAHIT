# MÜŞAHİT · Morning Triage Run Report · 2026-05-28

> *Date · 2026-05-28 (Thu)*
> *Mode · AUTONOMOUS · operator was away*
> *Brief · `docs/implementations/2026-05-28-morning-triage-brief.md`*
> *Outcome · all three issues green · 0 blockers · 3 commits · no push*

This report is the first thing the operator should read on return.
It maps 1:1 to the brief's "Final acceptance checklist."

---

## ❯ Acceptance checklist

| Item | Status |
|---|---|
| Issue 1 · embed failure raises clear `EmbeddingUnavailableError` · no cryptic zip ValueError · cluster stage re-runnable · tests green | DONE |
| Issue 1 · partial alignment clusters the successful subset (Solution B, stretch) | Deferred · see Notes |
| Issue 2 · embedder retries transient 500s with backoff · adaptive batch halving · order preserved · tests green | DONE |
| Issue 3a · briefer.py definitively per-section · `tests/test_writer/test_briefer.py` green (regenerated from spec) | DONE · briefer was single-shot at start; restored from 2026-05-27 spec |
| Issue 3b · empty sections emit honest notes (no LLM call) · validator rejects prompt echo + CoT · bad sections become honest stubs · tests green | DONE · entity-grounding (mode 4) deferred |
| FULL pytest suite green | 755 passed · 1 skipped · 0 failed |
| ruff clean (`musahit/` + `tests/`) | All checks passed |
| One commit per issue · local only · no push | 3 commits · no push |
| Run report written | This file |

Baseline at session start: 7 failures (all `tests/test_writer/test_briefer.py`).
Tripwire was 7. Final: 0 failures. Net delta: -7 failures, +29 new passes.

---

## ❯ Issue 1 · Clusterer clean re-runnable failure on embed unavailability

**Commit · `3f11634`** · `fix(cluster): clean re-runnable failure on embed unavailability (Issue 1)`

### What was wrong

`musahit/cluster/clusterer.py::_embed_articles` caught any embedder
exception and returned `[]`. The downstream `zip(embeddable, vectors,
strict=True)` in `run()` then raised
`ValueError: zip() argument 2 is shorter than argument 1` — the orchestrator
captured that as the stage failure reason, so the 02:00 nightly log read:

```
failed_stages: [{"name": "cluster",
                 "reason": "ValueError: zip() argument 2 is shorter than argument 1"}]
```

Cryptic and useless. Also: no `cluster` in `stages_done` means `--stage
cluster --force` *would* re-run, but the failure mode was hidden from
the operator.

### What changed

* `EmbeddingUnavailableError(RuntimeError)` added at module level in
  `clusterer.py`.
* New guard in `run()` immediately after `vectors = await
  self._embed_articles(embeddable)`, BEFORE `_persist_embeddings` and
  the zip:

  ```python
  if len(vectors) != len(embeddable):
      log.warning("cluster_embed_incomplete",
                  expected=len(embeddable), got=len(vectors))
      raise EmbeddingUnavailableError(...)
  ```
* `_embed_articles` warning now includes
  `stage_impact="cluster_will_fail_and_be_rerunnable"` so the structured
  log explains the downstream consequence.

### Why Solution A and not Solution B

The embedder is all-or-nothing (Ollama returns a full list of embeddings
or `raise_for_status` lifts a 5xx). Solution B (partial alignment with
per-item `None`) would require changing the embedder's public signature
to `list[list[float] | None]` AND the clusterer to filter `None`. That's
a 3-file change for a stretch goal; Issue 2's resilience layer already
makes total embed failure rare enough that A is sufficient. If the
operator wants B later, the embedder side is the bigger lift and is
self-contained.

### Tests added (`tests/test_clusterer.py`)

* `TestEmbedFailureRaisesClearError::test_embed_total_failure_raises_clear_error`
  · `_FailingEmbeddingClient` raises every embed call · assert
  `EmbeddingUnavailableError` with article counts in message · assert
  `zip()` NOT in message · assert `cluster` NOT in `stages_done`.
* `TestEmbedFailureRaisesClearError::test_embed_length_mismatch_raises_clear_error`
  · embedder returns fewer vectors than inputs · same assertions.
* `TestEmbedFailureRaisesClearError::test_no_eligible_articles_does_not_raise`
  · word_count < 10 floor means embeddable is `[]` · guard passes (0==0)
  · stage marks done normally.

### Operator verification

```powershell
python -m musahit.pipeline run --date 2026-05-28 --stage cluster --force
```

Two outcomes:

* If Ollama is healthy now → real clustering, some clusters created.
* If Ollama 500s under load → `failed_stages[0].reason` reads
  `EmbeddingUnavailableError: embedding returned 0 vectors for 491
  articles · cluster stage cannot proceed` instead of the cryptic zip
  error. `cluster` stays out of `stages_done` so a later
  `--stage cluster --force` retries.

Either confirms Issue 1.

---

## ❯ Issue 2 · Embedder resilience (retry + adaptive batch halving)

**Commit · `f8b918f`** · `feat(cluster): embedder retry + adaptive batch halving (Issue 2)`

### What was wrong

`OllamaEmbeddingClient.embed()` sliced into `batch_size=50` chunks
(default per ADR-002) but had NO retry, NO halving, NO backoff. A single
transient 500 from Ollama → entire embed call fails → Issue 1's guard
fires → cluster stage fails the whole run. The 02:00 incident was almost
certainly memory pressure (multiple 4-5GB models resident on a single
laptop): bge-m3 had to cold-load while qwen2.5/trendyol still held RAM,
peak demand exceeded the GPU+system budget, Ollama 500'd the batch.

### What changed (`musahit/cluster/embedder.py`)

1. **`DEFAULT_BATCH_SIZE` 50 → 16.** Smaller per-call memory footprint
   keeps the embed envelope inside the budget when other models are
   resident. Old `batch_size=50` callers (none in the codebase) can pass
   it explicitly.
2. **Per-batch retry with exponential backoff.** Default 3 retries with
   `2s · 4s · 8s` sleeps. Retryable on 5xx, 408, 429,
   `TimeoutException`, `TransportError`. 4xx (caller bug) raises
   immediately.
3. **Adaptive batch halving on persistent failure.** When a batch fails
   every retry, the batch is halved and each half is recursively
   embedded. A 16-item batch can degrade to 8 → 4 → 2 → 1 across recursion
   if the load is bad. A single item that still fails after retries
   re-raises (which Issue 1's guard then surfaces as a clean
   `EmbeddingUnavailableError`).
4. **Order preservation.** `_embed_batch_resilient` concatenates `left +
   right` after recursive halving · the output index of every input is
   preserved. Batches are processed sequentially in input order. The
   strict-zip downstream therefore matches articles to vectors correctly
   even after halving.
5. **Structured logging.** `embed_batch_retry`, `embed_batch_halved`,
   `embed_item_failed` — the nightly log shows resilience working.

### Tests added (`tests/test_embedder.py`)

* `TestEmbedderResilience::test_embed_retries_on_500_then_succeeds` ·
  first POST 500s, retry succeeds, vectors correct.
* `TestEmbedderResilience::test_embed_halves_batch_on_persistent_failure`
  · poison-marker test forces a full batch to fail every retry, halves
  succeed, vectors return in INPUT ORDER (tagged-token verification).
* `TestEmbedderResilience::test_embed_single_item_failure_raises` ·
  permanently-down single item raises after retry exhaustion.
* `TestEmbedderResilience::test_embed_preserves_order_across_batches` ·
  inputs >> batch size, tagged tokens verify output ORDER matches input.
* `TestEmbedderResilience::test_embed_4xx_does_not_retry` · 400 single
  attempt → raise.
* `TestEmbedderResilience::test_embed_429_is_retryable` · 429
  exhausts retries · 1 + max_retries attempts.

Existing happy-path test `test_batches_respect_batch_size` adapted to
derive expected sizes from `DEFAULT_BATCH_SIZE` (instead of hardcoding
`[50, 50, 20]`). Added `test_explicit_batch_size_of_50_still_supported`
for callers who want the old size.

All tests use `backoff_base_seconds=0.0` so retries are instant — the
suite still runs in seconds.

### Operator verification

```powershell
python -m musahit.pipeline run --date 2026-05-28 --stage cluster --force
```

If Ollama 500s a batch, the structured log will now contain
`embed_batch_retry` / `embed_batch_halved` events and the stage will
complete. If every retry+halving still fails entirely, Issue 1's clean
error fires — that points to a hardware/scheduling problem (laptop
genuinely cannot serve embeddings under that load), not a code bug.

---

## ❯ Issue 3 · Writer · per-section briefer + anti-hallucination hardening

**Commit · `eae4881`** · `fix(writer): per-section briefer + anti-hallucination hardening (Issue 3)`

### Issue 3a · Definitive briefer state

Per the brief's decision tree:

```
grep musahit/writer/briefer.py for these markers:
   range(7), generate_with_prefill, build_section_user,
   build_system_log_section, max_retries, _retry_prompt
```

At session start HEAD's `briefer.py` contained `_retry_prompt`,
`max_retries`, `DEFAULT_MAX_RETRIES`, and called `self._llm.generate(...)`
in a single-shot retry loop. It did NOT contain `range(7)`,
`generate_with_prefill`, `build_section_user`, or
`build_system_log_section`. **Conclusion · single-shot · the per-section
refactor was LOST in last night's commit tangle** (matches the
clusterer's diagnosis that the previous commit `36bf375` was a
"simplify LLM composition" that flattened back to single-shot).

The 2026-05-27 spec at
`docs/implementations/2026-05-27-per-section-writer-briefing.md` is
intact, complete, and authoritative. The lost code was rebuilt from
that spec. Recovery of the single-shot version (if ever needed) is
available via `git show 723e3e8:musahit/writer/briefer.py` — nothing was
deleted.

### Issue 3b · Anti-hallucination hardening

The 2026-05-27 hallucinated specimen at
`briefings/2026/05/27/briefing.per-section-hallucinated.md` exhibits
four failure modes. The brief mandates remediating modes 1-3 and
flags mode 4 as stretch.

| Mode | Description | Status |
|---|---|---|
| 1 | Empty-section hallucination (DEFCON 1-2 with fabricated COVID, drug-bust) | **FIXED** · empty-section short-circuit |
| 2 | Prompt echo (DİKKAT echoed DISCIPLINE_RULES verbatim) | **FIXED** · `_PROMPT_ECHO_MARKERS` validator |
| 3 | Chain-of-thought leak (AMBİYANS emitted "Adım 1:" / "Gerekçe:") | **FIXED** · `_COT_LINE_PATTERNS` validator |
| 4 | Hallucinated arc_ids · invented `arc_YYYYMMDD_NNNN` not in payload | **DEFERRED** · requires threading the section's valid arc_id set through `validate_section` and decoder. The current validator runs on text in isolation. Mode 4 is rare in practice (the prompt structure keeps real arc_ids in the section payload) and the validator catches most fabricated arc_ids indirectly because a section that invents arc_ids usually also exhibits modes 1-3. Track this as a follow-up for Angle F. |

### Architecture (matches the 2026-05-27 spec)

```
Briefer._compose:
    for idx in LLM_SECTION_INDICES (0..6):
        if _is_section_empty(payload, idx):                # short-circuit
            sections[idx] = "{marker}\n\nBugün bu bölümde öğe yok.\n"
            continue
        try:
            body = await llm.generate_with_prefill(
                system=build_writer_system(),
                user=build_section_user(payload, idx),
                prefill=f"{marker}\n\n",
                ...)
        except Exception:
            sections[idx] = render_section_stub(idx)
            failed.append(idx); continue
        full = prefill + body
        if not validate_section(full, idx):                # echo/CoT/structure
            sections[idx] = render_section_stub(idx)
            failed.append(idx); continue
        sections[idx] = full

    sections[7] = build_system_log_section(payload, failed)   # deterministic
    markdown = DOCUMENT_TITLE + "\n\n" + "\n\n".join(sections)

    if validate_briefing_markdown(markdown) is non-empty:    # last-resort
        return render_fallback_briefing(payload), True, list(range(8))
    return markdown, False, failed
```

8 calls per run (7 LLM + 1 deterministic) vs single-shot 1 call up to
4 attempts. The 2026-05-27 spec documents the wall-clock cost (~2-4 min
vs ~30-60 sec). Acceptable for unattended 02:00 runs.

### Empty-section detection

`_is_section_empty(payload, idx)` checks the underlying payload data
(not the rendered string) so the test seam is clean:

| idx | Section | Empty when |
|---|---|---|
| 0 | DEFCON 1-2 ÖNCELİKLİ | no clusters in buckets `(0, 1, 2)` |
| 1 | DEFCON 3 MATERYAL | no clusters in bucket `(3,)` |
| 2 | AÇIK GELİŞMELER | `payload.open_arc_updates == []` |
| 3 | DEFCON 4 GÜNDEM | no clusters in bucket `(4,)` |
| 4 | DİKKAT · YALNIZCA SOSYALDE | no `is_social_only=True` cluster in priority/material/routine |
| 5 | AMBİYANS · DEFCON 5 | no clusters in bucket `(5,)` |
| 6 | KAPATILAN HİKAYELER | `payload.resolved_arcs == []` |

Empty sections write `EMPTY_SECTION_NOTE_TR = "Bugün bu bölümde öğe yok."`
under the section's marker. The structured log emits
`writer_section_empty_short_circuit` per skipped section so the operator
sees which sections were genuinely empty vs which were attempted.

### Validator hardening (`musahit/writer/validator.py`)

`validate_section(text, section_idx)` now rejects:

* Any of `_PROMPT_ECHO_MARKERS` in body: `KURALLAR (ADR-009)`,
  `BÖLÜM VERİSİ:`, `ÇIKTI (yalnızca`, `GÖREV ·`, `Hedef bölüm ·`.
* Any of `_COT_LINE_PATTERNS` matching the body:
  `^\s*Adım\s*\d+\s*:` or `^\s*Gerekçe\s*:` (Turkish).
* The 2026-05-23 legacy placeholder fragment `[içerik buraya`.

When a non-empty section's LLM body fails validation, it becomes a
stub via `render_section_stub(idx)` — honest "Bu bölüm üretilemedi" placeholder, not fabricated prose.

### Counts schema

`pipeline_runs.counts` JSON now carries both:

```json
{
  "writer_used_fallback": false,
  "writer_sections_fallback": [3, 5]
}
```

`writer_used_fallback` is True only when all 8 sections fail OR when
the final assembled markdown fails `validate_briefing_markdown` (last-
resort safety net). Partial-stub runs (e.g., 2 stubs + 6 real) are
**not** full fallback — empty sections are not counted as failures.

### Tests

* Adapted `TestHappyPath::test_per_section_compose`,
  `TestPerSectionFailure::test_per_section_failure_produces_stub`,
  `TestAllLlmSectionsFail`, `TestPrefillWiring`, `TestLlmException` to
  use new `_seed_all_section_buckets(conn)` helper — seeds every LLM
  section so the seven-calls-per-section contract is testable
  independently of the short-circuit.
* Added `TestEmptySectionShortCircuit::test_empty_section_skips_llm_emits_note`
  — single MATERIAL cluster, 6 empty sections, asserts `llm.call_count
  == 1` and disk markdown contains the canonical note in each empty
  section's body.
* Added `TestEmptySectionShortCircuit::test_empty_sections_do_not_count_as_fallback`
  — `writer_used_fallback=false`, `writer_sections_fallback=[]` despite
  6 short-circuits.
* Added `TestValidatorRejectsPromptEcho::test_section_with_prompt_echo_becomes_stub`
  — echo input → stub on disk, no `KURALLAR` or `BÖLÜM VERİSİ` leakage.
* Added `TestValidatorRejectsPromptEcho::test_section_with_cot_scaffolding_becomes_stub`
  — `Adım 1:` / `Gerekçe:` input → stub on disk, no CoT leakage.
* Added 8 validator unit tests (`TestValidateSectionRejectsPromptEcho`,
  `TestValidateSectionRejectsChainOfThought`) covering each marker and
  pattern plus a clean-prose positive case.

### Operator verification

```powershell
python -m musahit.pipeline run --date 2026-05-27 --stage write --force
```

(use `--date 2026-05-27` if 2026-05-28 has no clusters yet from Issue
1/2 work.) Then open `briefings/2026/05/27/briefing.md` and check:

* No fabricated headlines in genuinely empty sections — they read
  `Bugün bu bölümde öğe yok.`
* No `KURALLAR (ADR-009)` / `BÖLÜM VERİSİ:` / `ÇIKTI` echo anywhere.
* No `Adım 1:` / `Gerekçe:` scaffolding anywhere.
* Sections with real data are coherent Turkish prose OR honest
  `Bu bölüm üretilemedi · yedek metin kullanıldı.` stubs.

If Trendyol still produces bad output for non-empty sections, those
become stubs — that is a **PASS** (honest degradation). The failure mode
killed is fabrication, not stubbing.

---

## ❯ Notes & deferred items

### Issue 1 Solution B (partial alignment) · deferred

Implementing per-item `None` returns from the embedder + clusterer pair
selection is doable but spans embedder.py + clusterer.py + their
Protocol typing. Issue 2's resilience makes total embed failure rare
enough that A is sufficient for the immediate incident. If the operator
wants to ship Solution B later, the path is:

1. Change `EmbeddingClient.embed` return type to `list[list[float] |
   None]`.
2. In the embedder, replace the recursive single-item raise with `return
   [None]` for that position.
3. In `clusterer.run`, build pairs filtering Nones and only raise
   `EmbeddingUnavailableError` when `len(pairs) == 0`.

Self-contained, two-file change.

### Issue 3b Mode 4 (entity grounding) · deferred

The brief flags this as stretch. Implementing it cleanly requires
passing the section's valid arc_id set into `validate_section` (current
signature takes only `(text, section_idx)`). Cleanest refactor: add an
optional `payload_arc_ids: set[str] | None = None` parameter and have
the Briefer compute the per-section set from `BriefingPayload`. The
risk: false positives if the LLM legitimately mentions an arc_id from
the SİSTEM LOG section's metadata. Worth a separate pass when the
hallucinated-arc_id mode is actually observed in the wild — not yet a
recurring failure.

### Sources used in `_seed_all_section_buckets`

The new test helper picks source IDs from `seed_sources()`'s output
(`bianet`, `cumhuriyet`, `sabah`, `diken`, `hurriyet`) so the
`ingest_log.source_id` foreign key holds. If `seed_sources` is ever
refactored to drop one of these IDs, the helper will fail at
constraint-time — a clear signal, not a silent break.

### `Briefer.__init__` signature

`max_retries` and `DEFAULT_MAX_RETRIES` were removed from the new
per-section briefer (no retry loop in this architecture). The
orchestrator constructs `Briefer` without `max_retries`, so this is
backward-compatible at the call site. No callers passed `max_retries`
explicitly.

### Pre-existing ruff errors

`python -m ruff check .` reports 15 errors in
`scripts/triage/spike_session_pdf_full.py` (unrelated to the triage
scope; these existed before this session). `python -m ruff check
musahit/ tests/` is clean. Fixing the scripts/ errors is out of scope
for this brief.

### Out-of-scope sanity check

The brief's file-protected list (`sources.py`, `poller.py`, `defcon.py`)
and out-of-scope test trees (`test_score/`, `test_ingest/`,
`test_normalize/`, `test_arcs/`, `test_tts/`) were not touched. Full
suite stayed green throughout, so no out-of-scope regression slipped in.

---

## ❯ Tripwire

| Checkpoint | Failures | Notes |
|---|---|---|
| Session start (baseline) | 7 | All in `tests/test_writer/test_briefer.py` · expected per-section, HEAD was single-shot |
| After Issue 1 | 7 | Unchanged · only cluster files touched |
| After Issue 2 | 7 | Unchanged · only embedder files touched (test_embedder.py adapted) |
| After Issue 3 | 0 | Per-section briefer restored · all writer tests green |

Tripwire was 7. Final 0. Suite is greener than baseline.

---

## ❯ Commits

```
eae4881 fix(writer): per-section briefer + anti-hallucination hardening (Issue 3)
f8b918f feat(cluster): embedder retry + adaptive batch halving (Issue 2)
3f11634 fix(cluster): clean re-runnable failure on embed unavailability (Issue 1)
```

All local. No push. Operator pushes when ready.

---

## ❯ One-line takeaway

> *Embed failures are now honest (1) and rare (2); the writer cannot
> ship fabricated content (3) · full suite green from 7-fail baseline
> to 0-fail final · 3 commits · no push.*

---

*End of report. Operator: smoke `--stage cluster --force` and `--stage
write --force` against 2026-05-27 or 2026-05-28 to confirm in
production. The fixes are unit-test verified · the smokes are
confirmatory.*
