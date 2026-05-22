# ADR-008 · Story arc model

**Status** · Accepted · 2026-05-22
**Author** · Mert Efe Şensoy
**Supersedes** · none
**Cross-references** · ADR-004 · ADR-006 · ADR-009

---

## ❯ Context

Turkish politics and economics are dominated by ongoing threads, not isolated events.
Court cases unfold over months. FX policy shifts persist for quarters. Cabinet reshuffles
trigger downstream resignations weeks later. A briefing that presents each day's news as
disconnected items loses the most important signal: which threads are active and how
they're evolving.

In *Person of Interest* terms, the Machine assigns numbers and tracks them over time.
MÜŞAHİT does the same with stories.

## ❯ Decision

A **story arc** is a long-lived container that groups clusters reporting on the same
underlying event thread. Arcs have lifecycle states. New clusters are linked to existing
arcs via embedding similarity plus entity overlap. The briefing surfaces arc updates
prominently, with new arcs and resolved arcs in their own sections.

### Arc data model

Defined in `src/arcs/models.py`:

```python
class ArcState(StrEnum):
    OPEN     = "OPEN"      # active in last 7 days
    WATCH    = "WATCH"     # dormant 7-30 days
    RESOLVED = "RESOLVED"  # closed by operator OR 30-day silence

@dataclass
class Arc:
    id: str                # arc_YYYYMMDD_NNNN
    created_at: datetime
    headline: str          # short title · evolves as the arc develops
    summary: str           # rolling summary, updated by writer each time arc gets a new
                           # cluster of significance
    state: ArcState
    last_update_at: datetime
    category: Category     # primary category · POLİTİKA · EKONOMİ · etc.
    peak_defcon: DEFCON    # highest DEFCON ever seen in this arc
    entity_set: set[str]   # core entities defining the arc (e.g., {"İmamoğlu",
                           # "İstanbul Büyükşehir", "Yargıtay"})
```

### Arc-cluster linking

When a new cluster is created, the arc-link stage tries to attach it to an existing arc:

```python
def find_matching_arc(cluster: Cluster) -> Arc | None:
    candidates = arcs_in_state([ArcState.OPEN, ArcState.WATCH])
    
    for arc in candidates:
        # Time window check
        if cluster.created_at - arc.last_update_at > timedelta(days=30):
            continue
        
        # Embedding similarity
        sim = cosine(cluster.centroid, arc.centroid)
        if sim < 0.55:
            continue
        
        # Entity overlap (Jaccard)
        cluster_entities = set(cluster.entities) - STOPWORD_ENTITIES
        if not cluster_entities:
            continue
        jaccard = len(cluster_entities & arc.entity_set) / len(
            cluster_entities | arc.entity_set
        )
        if jaccard < 0.4:
            continue
        
        return arc  # first match wins · arcs are returned by recency
    
    return None
```

Thresholds (`sim ≥ 0.55`, `jaccard ≥ 0.4`) are starting values. They are stored in
`config.toml` and may be tuned after the first month of operation.

`STOPWORD_ENTITIES` includes entities so common they don't disambiguate arcs · e.g.,
"Türkiye", "Cumhuriyet", "AKP", "CHP". An arc tagged only with these is meaningless.

### Arc creation

If no matching arc is found, a new arc is created with:

- `id` · `arc_YYYYMMDD_NNNN` (NNNN is a daily counter)
- `headline` · the cluster's headline
- `summary` · the cluster's summary
- `state` · `OPEN`
- `entity_set` · the cluster's entities minus stopwords
- `peak_defcon` · the cluster's final DEFCON
- `category` · the cluster's category

The cluster's `arc_id` is set to the new arc.

### Arc updates

When a cluster is linked to an existing arc:

1. `arc.last_update_at` is updated to `cluster.created_at`
2. `arc.peak_defcon` is updated to `max(arc.peak_defcon, cluster.final_defcon)`
3. `arc.entity_set` is extended with new entities from the cluster
4. `arc.centroid` is recomputed as the average of all member cluster centroids
5. If the cluster's DEFCON is ≥ 3 OR the cluster represents a significant change, the
   arc is flagged for writer attention · the writer regenerates `arc.summary` to
   incorporate the new development

### State transitions

```
OPEN ──(7 days without update)──→ WATCH
WATCH ──(any new cluster linked)──→ OPEN  
WATCH ──(30 days without update)──→ RESOLVED
OPEN ──(operator dashboard action)──→ RESOLVED
RESOLVED ──(new cluster matches via override)──→ OPEN
```

The transition `WATCH → RESOLVED` after 30 days of silence is automatic but reversible:
if a matching cluster arrives later, the arc is reopened (operator confirms via dashboard).

### Operator resolution actions

From the dashboard, the operator can:

- **RESOLVE** · explicitly close an arc · adds a `RESOLVED` entry in `manual_overrides`
- **MERGE** · combine two arcs that should have been linked but weren't (the cluster
  history of the absorbed arc transfers · the absorbed arc is marked `RESOLVED` with
  reason `MERGED_INTO_<arc_id>`)
- **SPLIT** · separate clusters from an arc that shouldn't have been linked (creates a
  new arc with those clusters)
- **RENAME** · change the arc's headline

All operator actions are logged in `manual_overrides`.

### Bootstrap behavior

On the first night of operation, no arcs exist. Every cluster creates a new arc. After
~30 days, the arc set stabilizes and most clusters link to existing arcs rather than
creating new ones.

The bootstrap period also means that the **same underlying event** reported across
several days produces several arcs in week 1 (because there's no history to link to).
Week 2 onward, the operator uses MERGE actions to consolidate. Manual MERGE is expected
to be common in the first month.

### Arc in the briefing

Per ADR-009, the briefing template surfaces arcs in two ways:

- `AÇIK GELİŞMELER (devam eden takip)` · arcs in `OPEN` state with updates today
- `KAPATILAN HİKAYELER` · arcs transitioning to `RESOLVED` today

`WATCH` state arcs are not in the briefing by default. The dashboard has a `WATCH` tab
where the operator can browse.

### Arc embedding maintenance

Arc centroids are recomputed after every linked cluster. The HNSW index in DuckDB VSS
is rebuilt nightly (cheap at our volume).

## ❯ Consequences

**Positive**
- The operator sees continuity, not just daily snapshots · this matches how Turkish
  political/economic events actually unfold
- The arc model is the most distinctly *Person of Interest* component · each arc is a
  "Number" being tracked
- Operator overrides (RESOLVE, MERGE, SPLIT) give human judgment a clean interface
  without requiring code changes
- The 30-day silence auto-RESOLVED rule prevents arc bloat

**Negative**
- Embedding + entity Jaccard linking will misfire occasionally · MERGE and SPLIT actions
  are expected weekly · this is acceptable
- The thresholds (0.55 cosine, 0.4 Jaccard) are guesses · expect tuning
- Bootstrap period produces noisy arc set · operator must invest time consolidating in
  the first month

## ❯ Alternatives considered

- **No arc model · daily snapshots only** · rejected · loses the most important signal in
  Turkish news
- **LLM-driven arc detection (worker asks "does this match arc X")** · rejected · too
  many LLM calls (one per cluster per existing arc) · the embedding + Jaccard rule is
  cheaper and auditable
- **Graph-based event linking (Neo4j)** · over-engineered for v0.1 · revisit if arc
  relationships become important

## ❯ Open questions

- Whether the 0.4 Jaccard threshold should adapt to entity set size · a 2-entity overlap
  out of 3 (0.66 Jaccard) is different from 10 out of 25 (0.4) · revisit if precision
  is poor
- Whether arcs should have explicit relationships to each other (e.g., "this arc spawned
  from that arc") · deferred · revisit if it would aid the briefing
