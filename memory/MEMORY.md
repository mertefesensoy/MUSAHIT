# MÜŞAHİT · project memory index

Per global CLAUDE.md: each project maintains this file. Summaries are in the
linked files; this index gives the one-line hook.

| File | Contents |
|---|---|
| [build-progress.md](build-progress.md) | Build order completion status and key decisions per step |

---

## Conventions

### Enum expansion is an ADR amendment, never a silent code change

Closed enumeration sets in this project — `Band`, `Tier`, `Category`,
`DEFCON` (when defined in step 11), `ArcState`, `GazetteSection`,
`GazetteItemType`, `OverrideAction`, and any future closed set — are
load-bearing for downstream stages: promotion rules, briefing template
slots, dashboard tabs, audit logs. Adding a value silently breaks
exhaustiveness checks somewhere else in the pipeline.

**Rule:** if real data surfaces a gap, the gap is reported (via
`ingest_log` for ingest gaps, via a new ADR amendment for taxonomy
gaps). The enum is expanded only by an ADR that names the new member
and lists the downstream code paths that must update with it. Never
add a member in the same PR that introduces the use case for it
without the matching ADR.

This applies symmetrically: removing or renaming a member is also an
ADR amendment.
