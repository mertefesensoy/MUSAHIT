# MÜŞAHİT · Embedder Solution B Brief · 2026-05-28

> *Date · 2026-05-28 (Thu)*
> *Component · `musahit/cluster/embedder.py` + `musahit/cluster/clusterer.py`*
> *Decision · Per-item None partial alignment · skip un-embeddable articles*
> *Motivating incident · bge-m3 NaN poison article (isolated below)*
> *Verification · MANDATORY three tiers · unit + integration + live smoke*

This is the authoritative spec for the deferred "Solution B" from the
2026-05-28 morning triage. Unlike a vague "make it resilient" task, the
target is now precise: one specific article makes bge-m3 emit a NaN
embedding, and the only robust fix is to let the embedder skip that
single item and cluster the rest.

---

## ❯ The diagnosis (motivating incident · do not re-investigate, it is solved)

The cluster stage failed at 02:00 and on two daytime smokes, always at
the same point, surviving a model re-pull AND a full Ollama process
restart. A 60-call benign-input probe ran clean (so not memory/state),
which proved the failure is content-dependent. A one-at-a-time replay of
the real eligible set (451 articles lacking embeddings, `title + "\n\n" +
lead` input) isolated it to exactly ONE article:

```
[idx 117] id=3efe17cd13b367cc067a12471c83c12eccf5adb79d1b34112d2c93e53bfde736
          title: "Sezon açıldı"
          lead:  "Ünlü isimler bayramda yaz tatili sezonunu açtı..."
          len=140  wc=17  status=500
          resp: {"error":"failed to encode response: json: unsupported value: NaN"}
```

Root cause · bge-m3 computes an embedding **containing NaN** for this
specific (benign, clean) input. Ollama's JSON serializer then cannot
encode NaN and returns 500. This is a model/runtime bug, NOT a
data-quality problem · the text is fine, there is nothing to sanitize.
It is deterministic (same input → same NaN), affects 1 of 451, and is
irreducibly per-item.

Implication · input sanitization cannot fix this. The ONLY robust fix is
per-item partial alignment: when bge-m3 fails on one article, the
embedder returns `None` for that position and the clusterer clusters the
rest. Solution A (the shipped behavior) fails the whole stage because one
item is un-embeddable · that is why every run dies. Solution B makes the
stage succeed on 450 and drop the 1.

---

## ❯ The fix contract

### File · `musahit/cluster/embedder.py`

1. **Return type change** · `embed()` (and any internal batch helpers)
   return `list[list[float] | None]` · same length as input, `None` at
   positions that could not be embedded. Update the type hints and any
   `EmbeddingClient` Protocol / ABC accordingly.

2. **Per-item failure → None, not raise** · the existing adaptive halving
   already recurses down to a single item (confirmed in the live logs).
   Today, a single item that fails all retries RE-RAISES. Change it so a
   single-item permanent failure returns `[None]` for that position.
   Order is preserved through the halving reassembly (`left + right`),
   so the `None` lands at the correct index.

3. **Validate successful vectors → `_is_valid_vector(vec)`** · defends
   against a future Ollama returning HTTP 200 with NaN coerced to null/0.
   A vector is INVALID (mapped to `None`) if any of:
   - it is `None` or empty
   - its length != `DIMENSION`
   - it contains NaN or Inf (`any(x != x or x in (float("inf"), float("-inf")) for x in vec)`, or use `math.isnan`/`math.isinf`)
   - it is all-zeros (degenerate · `not any(vec)`)
   Run every returned vector through this guard before accepting it.

4. **Logging** · keep `embed_batch_retry`, `embed_batch_halved`. Replace
   the terminal `embed_item_failed` re-raise with
   `embed_item_skipped` carrying `reason` (`http_500_nan`, `nan_in_vector`,
   `inf_in_vector`, `wrong_dim`, `all_zeros`, `exhausted_retries`) and the
   batch-relative index. This makes the nightly log show exactly which
   articles were dropped and why.

5. **Total failure still possible** · if the entire embed call raises at
   the transport level (Ollama process down, connection refused), let
   that propagate / surface as today (the clusterer's `_embed_articles`
   wrapper and Issue-1 guard handle it · see reconciliation below). Per-
   item None is for per-item model failures; total connection failure is
   still a stage failure.

### File · `musahit/cluster/clusterer.py`

1. **`_embed_articles`** · returns the embedder's aligned
   `list[list[float] | None]` directly. Keep the existing try/except that
   returns `[]` on a TOTAL embed exception (Ollama fully down) · that
   feeds the Issue-1 guard. Do NOT swallow per-item Nones · they are the
   normal Solution-B path now and must reach `run()`.

2. **`run()` · partial alignment** · reconcile with the Issue-1 guard:

   ```python
   vectors = await self._embed_articles(embeddable)

   # Total-failure guard (Issue 1) · embed call raised, wrapper returned [].
   if not vectors and embeddable:
       log.warning("cluster_embed_incomplete", expected=len(embeddable), got=0)
       raise EmbeddingUnavailableError(...)

   # Length contract · embedder returns one slot per input (value or None).
   # strict=True still valid because lengths now match by construction.
   pairs = [(a, v) for a, v in zip(embeddable, vectors, strict=True) if v is not None]
   dropped = len(embeddable) - len(pairs)
   if dropped:
       log.warning(
           "cluster_embed_partial",
           dropped=dropped,
           clustered=len(pairs),
           dropped_ids=[a.id for a, v in zip(embeddable, vectors, strict=True) if v is None][:20],
       )
   if not pairs:
       raise EmbeddingUnavailableError(
           f"all {len(embeddable)} articles failed to embed · cluster cannot proceed"
       )

   # Persist + bucket ONLY the successful pairs.
   self._persist_embeddings([a for a, _ in pairs], [v for _, v in pairs])
   buckets = {}
   for a, v in pairs:
       buckets.setdefault(a.language, []).append((a, v))
   ...
   ```

3. **`_persist_embeddings`** · must only receive successful (article,
   vector) pairs · no None vectors written to `article_embeddings`.
   Articles dropped this run remain without an embedding, so a future
   `--stage cluster --force` will retry them (and likely drop them again
   until the bge-m3 bug is fixed upstream · acceptable, they are logged).

4. **Keep `EmbeddingUnavailableError`** · now raised ONLY when zero
   articles embedded (total failure), never for partial. The Issue-1
   tests that assert the clean error on total failure must still pass.

### Callers / typing ripple

Grep for every caller of `.embed(` and the `EmbeddingClient` type. Update
signatures and any fakes/mocks in tests to the `list[list[float] | None]`
contract. The clusterer is the main caller; there may be test fakes.

---

## ❯ MANDATORY verification · three tiers (all are acceptance gates)

The last autonomous run was unit-test-only and shipped a partial-discard
bug (`got: 0` despite successful embeds) that only a live run exposed.
This time, ALL THREE tiers must pass before declaring done. Tier 2 and
Tier 3 are not optional.

### Tier 1 · Unit tests (fakes) · `tests/test_embedder.py`, `tests/test_clusterer.py`

Embedder:
- `test_embed_returns_none_for_persistent_item_failure` · fake transport
  500s on one specific input every time · `embed()` returns a list with
  `None` at that index and valid vectors elsewhere · order preserved.
- `test_is_valid_vector_rejects_nan` · a vector with `float("nan")` → None.
- `test_is_valid_vector_rejects_inf` · `float("inf")` → None.
- `test_is_valid_vector_rejects_wrong_dim` · length != DIMENSION → None.
- `test_is_valid_vector_rejects_all_zeros` · `[0.0]*DIMENSION` → None.
- `test_valid_vector_accepted` · a normal vector passes.
- order-preservation test across batches still passes with a None in the mix.

Clusterer:
- `test_partial_embed_clusters_successful_subset` · mock embedder returns
  one None · clustering proceeds on the rest · `cluster_embed_partial`
  logged with `dropped=1` · stage marked done (cluster in stages_done).
- `test_all_none_raises_embedding_unavailable` · mock returns all None →
  `EmbeddingUnavailableError`.
- `test_total_failure_returns_empty_raises` · mock embed raises / wrapper
  returns [] → `EmbeddingUnavailableError` (Issue-1 behavior preserved).
- existing Issue-1 tests still green.

### Tier 2 · Integration test (real code, mocked HTTP boundary) · `tests/test_cluster_embed_integration.py` (new)

Wire the REAL `OllamaEmbeddingClient` and the REAL `Clusterer` together,
with ONLY the httpx boundary stubbed via `httpx.MockTransport`. The mock
returns:
- HTTP 500 with body `{"error":"...json: unsupported value: NaN"}` for a
  designated poison input string,
- HTTP 200 with a valid embedding for all other inputs.

Seed a small DB (use the existing test DB fixtures) with, say, 5 articles
where one has the poison input. Run `clusterer.run(run_id)`. Assert:
- the full chain (retry → halving → single-item None → partial alignment)
  executes against real code, not fakes,
- the poison article is dropped (its id in `dropped_ids`),
- the other 4 cluster,
- the stage succeeds (cluster in stages_done), NO `EmbeddingUnavailableError`,
- `article_embeddings` has 4 rows, not 5, and none are NaN.

This is the test that would have caught the `got: 0` discard bug. It
exercises the real embedder reassembly + real clusterer pairing.

### Tier 3 · Live production smoke (real Ollama) · the agent runs this

Run against the real run that has the real poison article:

```powershell
python -m musahit.pipeline run --date 2026-05-28 --stage cluster --force
```

This embeds the actual 451 articles against the live bge-m3, including
the NaN article `id=3efe17cd...` ("Sezon açıldı"). Acceptance · the stage
must now SUCCEED:
- `status COMPLETED` with `cluster` in `stages_done` (NOT failed),
- a `cluster_embed_partial` event with `dropped=1` (or a small number)
  and the NaN article id in `dropped_ids`,
- ~450 articles clustered, real `clusters` rows written,
- NO `EmbeddingUnavailableError`, NO zip ValueError.

Capture the relevant log lines and the final run-state row into the run
report. If Ollama is genuinely unreachable (connection refused, not a
per-item 500), that is an environment problem · retry once, and if still
down, document it and mark Tier 3 BLOCKED with the reason (but Tiers 1-2
must still be green). A per-item NaN 500 being skipped while the run
succeeds is the definitive proof Solution B works.

Note · this live run WILL write embeddings + clusters for run_20260528 to
the DB (recovering the run). That is desirable. The operator should know
the 28th's data is now populated.

---

## ❯ Acceptance checklist

- [ ] Embedder returns `list[list[float] | None]`; per-item failure → None.
- [ ] `_is_valid_vector` rejects None/empty/wrong-dim/NaN/Inf/all-zeros → None.
- [ ] Clusterer partial-alignment: clusters non-None, logs `cluster_embed_partial` with dropped ids, raises `EmbeddingUnavailableError` only when zero embedded.
- [ ] `_persist_embeddings` writes only successful pairs (no None).
- [ ] Issue-1 total-failure behavior preserved (clean error, re-runnable).
- [ ] All callers / fakes / Protocol updated to the new return type.
- [ ] Tier 1 unit tests green.
- [ ] Tier 2 integration test green (real embedder + clusterer, mocked httpx, NaN-500 for poison input).
- [ ] Tier 3 live smoke: `--stage cluster --force` for 2026-05-28 SUCCEEDS with `cluster_embed_partial dropped>=1` including id=3efe17cd, ~450 clustered, no EmbeddingUnavailableError. (Or BLOCKED-with-reason only if Ollama is fully unreachable.)
- [ ] FULL pytest suite green · ruff clean (`musahit/` + `tests/`).
- [ ] One commit · local only · no push.
- [ ] Run report written: `docs/implementations/2026-05-28-embedder-solution-b-report.md`.

---

## ❯ Scope & safety

- **Edit only** · `musahit/cluster/embedder.py`, `musahit/cluster/clusterer.py`,
  their tests, and the new integration test file. Plus the run report.
- **FILE-PROTECTED · never edit** · `musahit/sources.py`,
  `musahit/ingest/poller.py`, `musahit/score/defcon.py`.
- **Do not touch** the writer, score, arcs, tts, normalize, ingest stages.
- **No destructive ops** · no `git reset --hard`, no force, no push, no
  dropping DB tables. The live smoke writes clusters (expected); it does
  not delete anything.
- **No new dependencies** · use `math` for NaN/Inf checks; numpy only if
  already imported in embedder.py.
- **Commit message** · `feat(cluster): per-item None partial alignment for un-embeddable articles (Solution B)`.

---

## ❯ One-line takeaway

> *bge-m3 emits NaN on one specific clean article and Ollama 500s on
> serialization · sanitizing is impossible · so the embedder returns None
> for the poison item and the clusterer clusters the other 450 · proven
> by unit + integration + a live smoke that must SUCCEED, not fail clean.*

---

*End of brief. The poison article is id=3efe17cd... "Sezon açıldı". The
fix is partial alignment. The proof is a green live cluster smoke.*
