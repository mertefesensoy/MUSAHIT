# Implementation: Arc-link cascade fix

**Date** · 2026-05-24
**Author** · Mert Efe Şensoy
**ADR refs** · ADR-008 · ADR-006 (FK pattern)

---

## ❯ Problem / Motivation

The first end-to-end smoke run on 2026-05-23 reached the arc-link
stage and emitted **240 `arc_link_cluster_failed` warnings** · one
per cluster · plus a single orphan row in the `arcs` /
`arc_centroids` tables (`id = arc_20260523_0001`). No cluster was
ever linked to an arc on that run.

The structured-log tail showed every cluster failing on a
constraint check against `clusters.id`, then failing again on
`PRIMARY KEY constraint failed: arcs.id` when the linker tried to
seed the same arc id for the next cluster. The cascade buried the
real diagnostic.

The arc-link stage is on the always-ships critical path (per
ADR-012) but the briefing fell through to fall-back content for
2026-05-23 with zero linked arcs. Without a fix, every subsequent
smoke run reproduces the same cascade.

---

## ❯ Root cause

Two compounding bugs in `musahit/arcs/linker.py`:

1. **Missed FK in the workaround.** The
   `_update_cluster_arc_id` helper performed the
   DELETE-child → UPDATE-parent → re-INSERT-child dance for
   `cluster_articles` and `cluster_embeddings` but did NOT handle
   `promotion_log`. `promotion_log.cluster_id REFERENCES clusters(id)`
   was added during step 11 (score stage), and DuckDB enforces FK
   checks on UPDATE statements regardless of whether the referenced
   column is the one being changed. Every
   `UPDATE clusters SET arc_id = ? WHERE id = ?` raised a
   constraint error · the cluster had a `promotion_log` row from
   the just-completed score stage.

2. **Counter trapped inside the success branch.** The original
   `ArcLinker.run` loop incremented the daily arc-id counter only
   on the success path of `_seed_arc`. The outer try/except caught
   `_seed_arc`'s exception · the counter sat still. The next
   cluster then called `_seed_arc(counter=1)` again and tried to
   re-INSERT `arc_20260523_0001`. The first cluster's INSERT had
   succeeded (arcs + arc_centroids both committed before the
   cluster UPDATE raised) · so the next cluster hit duplicate-PK,
   which raised, which the outer except caught, which sat the
   counter still, which · cascade.

Bug 1 alone would have produced 240 single-error warnings with no
orphans. Bug 2 alone would have advanced the counter cleanly with
no failures. Together they produced the 240-clusters-on-one-arc_id
pattern observed.

A third subtle point: bug 1 left the just-inserted arc row + its
arc_centroids row behind every time the cluster UPDATE raised.
DuckDB auto-commits per statement so the INSERTs were already
durable when the UPDATE raised. Of the 240 attempts, the first
left a real orphan; the other 239 each hit duplicate-PK on the
INSERT itself, so no further orphans were created · but if the
duplicate-PK protection hadn't existed (e.g. if we'd used a
different id scheme) we'd have 240 orphans.

---

## ❯ What Changed

| File | Description |
|---|---|
| `musahit/arcs/linker.py` | `_update_cluster_arc_id` and `_update_cluster_arc_id_to_value` extended to snapshot · DELETE · re-INSERT `promotion_log` rows around the cluster UPDATE; `ArcLinker.run` advances the arc-id counter in a `finally` block whenever a seed was attempted; `_seed_arc` adds manual rollback of arcs + arc_centroids if the cluster update raises. Module docstring updated to reflect the new FK list. |
| `tests/test_linker.py` | Three regression test classes: `TestPromotionLogPreservedAcrossArcUpdate` round-trips a populated promotion_log row through a link; `TestSeedArcRollback` injects a failing `_update_cluster_arc_id` and asserts no orphan arc / arc_centroids row survives; `TestCounterAdvancesOnSeedFailure` injects two failures and one success and asserts the surviving arc id ends in `_0003`, not `_0001`. |
| `scripts/cleanup_orphan_arcs.py` | One-off operator script. DELETEs `arc_centroids` and `arcs` rows for the specific orphan `arc_20260523_0001` only. Refuses to delete if the row has any linked clusters. Idempotent. |
| `memory/MEMORY.md` | "DuckDB FK Update Pattern" entry extended with `promotion_log` in the "Applies to" list, plus a Maintenance rule + 4-step checklist for adding a new child FK to a parent already covered by the workaround. |
| `memory/build-progress.md` | Between-step entry "Step 16 follow-up · arc-link cascade fix · 2026-05-24" documenting the bug, the fix scope, and the operator cleanup action. |
| `docs/implementations/2026-05-24-arc-link-bug-fix.md` | This file. |

---

## ❯ Implementation Approach

### FK workaround extension

`promotion_log` has a one-row-per-cluster shape
(`cluster_id TEXT PRIMARY KEY REFERENCES clusters(id)`), so the
snapshot is a single `fetchone()` of all eight non-key columns
(`raw_defcon, ceiling_defcon, final_defcon, bands_present,
sides_present, confidence, rule_applied, computed_at`). After
the UPDATE re-inserts cluster_articles + cluster_embeddings, the
re-INSERT of promotion_log uses `ON CONFLICT (cluster_id) DO
NOTHING` so a no-op rerun is safe.

Both helpers (`_update_cluster_arc_id` and
`_update_cluster_arc_id_to_value`) get the same extension to keep
them in lock-step · the existing module docstring already warned
that they must stay in sync, and now the inline docstrings restate
this so the next reader sees it without crawling the module
header.

### Counter advance in `finally`

```python
for cluster in clusters:
    seed_attempted = False
    try:
        ...
        if arc_id is not None:
            self._attach_cluster(...)
            joined += 1
        else:
            seed_attempted = True
            new_arc = self._seed_arc(cache, cluster, ...)
            seeded += 1
    except Exception as exc:
        log.warning("arc_link_cluster_failed", ...)
        errors += 1
    finally:
        if seed_attempted:
            counter += 1
```

`seed_attempted` is set BEFORE `_seed_arc` is called so a raise
inside the seeder still triggers the counter advance. The flag is
needed because a successful `_attach_cluster` should NOT advance
the counter (no arc was seeded).

### Manual rollback in `_seed_arc`

DuckDB auto-commits per statement. There is no `BEGIN`/`COMMIT`
pair we can rely on. The seeder INSERTs the arc row + arc_centroids
row FIRST (required because `clusters.arc_id REFERENCES arcs.id` ·
the cluster UPDATE must see the arc id as a valid FK target),
then attempts the cluster UPDATE inside a try/except. If the
cluster UPDATE raises, the handler does the rollback by hand:

```python
try:
    self._update_cluster_arc_id(cluster.id, arc_id)
except Exception:
    self._conn.execute("DELETE FROM arc_centroids WHERE arc_id = ?", [arc_id])
    self._conn.execute("DELETE FROM arcs WHERE id = ?", [arc_id])
    raise
```

Order matters: arc_centroids FIRST (because its FK targets arcs),
then arcs. The `raise` propagates to the outer loop's except,
which logs the warning and increments the error counter. The
`finally` block then advances the counter as usual.

---

## ❯ Mathematical / Statistical Details

Not applicable · this is a structural fix.

---

## ❯ Design Decisions

### Why extend the workaround rather than drop the FK?

`promotion_log.cluster_id` is a FK on `clusters.id` so that an
audit walking `promotion_log` can join back to the cluster's
current state without orphan handling. Dropping the FK would
remove the source of pain but also remove a real invariant ·
score-stage outputs that don't correspond to a current cluster
would silently accumulate. The cost of the FK is one snapshot
+ DELETE + re-INSERT per arc link; cheap.

### Why move the counter to `finally` rather than only fixing the
FK?

The FK fix alone restores correct behaviour for the specific bug.
But "any future child table referencing clusters could regress
this in the same way" is a structural risk. Moving the counter
into `finally` converts that risk from cascade-class into
single-failure-class · an unhandled raise still produces one
orphan but does NOT trip the next 239 clusters. Cheap defence in
depth that costs one boolean and one finally clause.

### Why manual rollback in `_seed_arc` instead of "INSERT cluster
update first"?

The seemingly cleaner option would be: UPDATE the cluster's
arc_id to a placeholder, then INSERT arcs + arc_centroids. But
`clusters.arc_id REFERENCES arcs(id)`, so the placeholder would
have to be either NULL (then the linker's idempotence check
"don't re-link clusters with arc_id IS NOT NULL" stops being
true mid-link) or a real arc id (which doesn't exist yet, so we
still need arcs to be inserted first). Manual rollback is the
only path that preserves the FK ordering AND survives a mid-link
failure.

### Why a targeted cleanup script, not a generic
"delete-orphan-arcs" script?

Until the fix lands, there's exactly one orphan (`arc_20260523_0001`)
left by the 2026-05-23 run. A generic "delete arcs with no linked
clusters" script would also delete legitimate-but-empty arcs (e.g.
arcs that just resolved or arcs created from edge-case clusters
that subsequently lost all members to dedup). The targeted script
is safer for a one-off; if a future smoke run leaves multiple
orphans we'll generalise then.

### Why no ADR-008 amendment?

The /goal tripwire on "ADR-008 needs amendment for the
orphan-on-failure rollback semantics" was checked. ADR-008
describes the arc-cluster linking semantics (cosine + jaccard
thresholds, state transitions, peak_defcon direction, operator
overrides) and is silent on transaction / rollback details. The
FK workaround and the manual rollback are implementation safety
measures for DuckDB's per-statement auto-commit · not semantic
decisions about the arc model. The pattern belongs in
`memory/MEMORY.md` (where it now lives), not in the ADR.

---

## ❯ Verification

```powershell
# Linter clean.
python -m ruff check .

# Linker tests grow from 10 to 13.
python -m pytest tests/test_linker.py -q

# Full suite stays green.
python -m pytest tests/ -q
```

The three new tests assert:

1. `TestPromotionLogPreservedAcrossArcUpdate` · insert a
   promotion_log row for the cluster, run the linker, and read the
   row back · all eight non-key columns match the originals.
2. `TestSeedArcRollback` · monkey-patch the linker's
   `_update_cluster_arc_id` to a function that always raises.
   `result["errors"] == 1` and both `arcs` and `arc_centroids`
   tables stay at zero rows.
3. `TestCounterAdvancesOnSeedFailure` · monkey-patch
   `_update_cluster_arc_id` to fail on the first two calls and
   succeed on the third. Three disjoint clusters · the surviving
   `arcs.id` ends in `_0003`, not `_0001`. This is the direct
   regression test for the cascade.

---

## ❯ Operator caveats

Before the next smoke run, drop the orphan row left by 2026-05-23:

```powershell
python scripts/cleanup_orphan_arcs.py
```

Expected output:

```
deleted 1 arc row(s), 1 arc_centroids row(s)
```

The script refuses if `arc_20260523_0001` has any linked clusters
(it shouldn't, but defence in depth · prints `REFUSED · ...` and
exits 1). The script is idempotent: rerunning after success prints
`deleted 0 arc row(s), 0 arc_centroids row(s)`.

If the next smoke run surfaces a different cluster→cluster FK
miss, the fix here keeps it from cascading · expect a single
failed cluster + a single warning. File the finding in
`memory/operator-tasks.md` with the missing FK name and the
helper that needs extending.

---

## ❯ Related Docs

- ADR-008 · story arc model
- `docs/implementations/2026-05-23-arcs.md` · original arc-link build
- `docs/implementations/2026-05-23-score.md` · original FK pattern discovery
- `memory/MEMORY.md` § "DuckDB FK Update Pattern"
- `memory/build-progress.md` · "Step 16 follow-up" entry
