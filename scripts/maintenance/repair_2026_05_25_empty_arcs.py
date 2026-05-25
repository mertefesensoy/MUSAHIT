"""One-off repair · 2026-05-25 empty-headline arcs.

Background
----------
The 2026-05-25 investigation
(``docs/investigations/2026-05-25-empty-headlines.md``) traced two
arcs that render as ``(başlıksız)`` to score-stage fallback rows whose
empty ``headline``/``summary`` propagated through ``_seed_arc`` into the
arcs table. Going forward the classifier writes a placeholder and the
arc-link filter excludes empty-headline clusters, but the two existing
arcs need a manual fix.

Targets
-------
* ``arc_20260523_0146`` — recoverable. ``cl_20260525_0076`` joined this
  arc on 2026-05-25 with real Bilgi Üniversitesi content (headline + summary
  from a successful classification). Copy that cluster's headline/summary
  into the arc's headline / summary / last_update_headline /
  last_update_summary columns.
* ``arc_20260523_0036`` — not recoverable. Its only linked cluster
  (``cl_20260523_0011``) also has empty headline + summary. Set the four
  columns to the same Turkish operator-triage placeholder the classifier
  fallback now writes.

Invariants
----------
* Idempotent — running twice changes nothing the second time. Each arc's
  current state is read before any UPDATE; if it already matches the
  desired state the script skips the write.
* Touches ONLY ``headline``, ``summary``, ``last_update_headline``, and
  ``last_update_summary``. Specifically leaves ``last_update_at`` and
  ``last_update_cluster_id`` unchanged so the briefing's "active-today"
  determination keeps the same shape it had pre-repair.
* Uses the DuckDB FK workaround pattern from
  ``musahit/arcs/linker.py::_update_arc`` because DuckDB 1.4.2 fires
  FK checks on any UPDATE to a parent row (``arcs``) even when the
  updated column isn't referenced by ``arc_centroids`` or ``clusters``.
  We snapshot + DELETE arc_centroids, null out member clusters' arc_id
  via the cluster-level workaround, UPDATE the arc, then restore.

Operator usage
--------------
The script never auto-runs. Operator invokes::

    python scripts/maintenance/repair_2026_05_25_empty_arcs.py

It prints what it would change and applies the change. Pass ``--dry-run``
to inspect the planned changes without writing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

# Bootstrap import path so the script runs from anywhere (e.g. Task Scheduler).
# Must happen BEFORE musahit imports · the noqa is intentional.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from musahit.common.config import get_settings  # noqa: E402,I001


DEFAULT_DB_PATH: Path = REPO_ROOT / "data" / "musahit.duckdb"

# The two arcs the investigation identified. Hardcoded — this script
# repairs exactly these rows, no fewer, no more.
ARC_RECOVERABLE: str = "arc_20260523_0146"
ARC_RECOVERABLE_SOURCE_CLUSTER: str = "cl_20260525_0076"

ARC_UNRECOVERABLE: str = "arc_20260523_0036"

# Same placeholder text the classifier fallback now writes (see
# musahit/score/classifier.py::_FALLBACK_RESPONSE). Kept as a literal here
# rather than imported to keep this one-off script self-contained · the
# strings are pinned by the test suite, not by runtime composition.
PLACEHOLDER_HEADLINE: str = "(sınıflandırılamadı)"
PLACEHOLDER_SUMMARY: str = (
    "Skorlama modeli bu kümede geçerli yanıt üretemedi. "
    "Operatör incelemesi bekliyor."
)


def _read_arc(
    conn: duckdb.DuckDBPyConnection, arc_id: str
) -> tuple[str, str, str, str] | None:
    """Return (headline, summary, last_update_headline, last_update_summary)."""
    row = conn.execute(
        """
        SELECT coalesce(headline, ''),
               coalesce(summary, ''),
               coalesce(last_update_headline, ''),
               coalesce(last_update_summary, '')
          FROM arcs
         WHERE id = ?
        """,
        [arc_id],
    ).fetchone()
    return row  # type: ignore[return-value]


def _read_cluster(
    conn: duckdb.DuckDBPyConnection, cluster_id: str
) -> tuple[str, str] | None:
    """Return (headline, summary) for the named cluster."""
    row = conn.execute(
        "SELECT coalesce(headline, ''), coalesce(summary, '') "
        "FROM clusters WHERE id = ?",
        [cluster_id],
    ).fetchone()
    return row  # type: ignore[return-value]


def _apply_update_with_fk_workaround(
    conn: duckdb.DuckDBPyConnection,
    arc_id: str,
    *,
    headline: str,
    summary: str,
    last_update_headline: str,
    last_update_summary: str,
) -> None:
    """UPDATE arcs SET … with the DuckDB FK-workaround dance.

    Mirrors the snapshot/DELETE/UPDATE/re-INSERT pattern used by
    ``ArcLinker._update_arc`` (line ~523 of musahit/arcs/linker.py). The
    workaround is necessary because an UPDATE on a parent row in DuckDB
    1.4.2 fires the FK check for every referencing table — even when the
    column being updated isn't itself a referenced column.

    Children of ``arcs`` to handle:

    * ``arc_centroids`` — DELETE + re-INSERT (centroid + updated_at
      snapshotted first).
    * ``clusters`` (via ``clusters.arc_id``) — temporarily null each
      member cluster's ``arc_id`` using the cluster-level workaround
      (``cluster_articles`` / ``cluster_embeddings`` / ``promotion_log``
      are children of clusters), then restore.

    Per-statement auto-commit · no explicit transaction.
    """
    # Snapshot arc_centroid.
    centroid_row = conn.execute(
        "SELECT centroid, updated_at FROM arc_centroids WHERE arc_id = ?",
        [arc_id],
    ).fetchone()

    # Snapshot member cluster ids.
    member_cluster_ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM clusters WHERE arc_id = ?", [arc_id]
        ).fetchall()
    ]

    # Clear arc_centroid first.
    conn.execute("DELETE FROM arc_centroids WHERE arc_id = ?", [arc_id])

    # Null out each cluster's arc_id via the cluster-level workaround.
    cluster_snapshots: list[
        tuple[
            str,  # cluster_id
            list[str],  # member article ids
            tuple | None,  # embedding row (centroid, embedded_at)
            tuple | None,  # promotion_log row
        ]
    ] = []
    for cid in member_cluster_ids:
        article_ids = [
            r[0]
            for r in conn.execute(
                "SELECT article_id FROM cluster_articles WHERE cluster_id = ?",
                [cid],
            ).fetchall()
        ]
        embedding_row = conn.execute(
            "SELECT centroid, embedded_at FROM cluster_embeddings "
            "WHERE cluster_id = ?",
            [cid],
        ).fetchone()
        promotion_row = conn.execute(
            """
            SELECT raw_defcon, ceiling_defcon, final_defcon,
                   bands_present, sides_present, confidence,
                   rule_applied, computed_at
              FROM promotion_log
             WHERE cluster_id = ?
            """,
            [cid],
        ).fetchone()
        cluster_snapshots.append(
            (cid, article_ids, embedding_row, promotion_row)
        )

        conn.execute(
            "DELETE FROM cluster_articles WHERE cluster_id = ?", [cid]
        )
        conn.execute(
            "DELETE FROM cluster_embeddings WHERE cluster_id = ?", [cid]
        )
        conn.execute(
            "DELETE FROM promotion_log WHERE cluster_id = ?", [cid]
        )
        conn.execute(
            "UPDATE clusters SET arc_id = NULL WHERE id = ?", [cid]
        )
        # Restore cluster's children (the cluster row itself still
        # exists, just with arc_id=NULL for the moment).
        for aid in article_ids:
            conn.execute(
                "INSERT INTO cluster_articles (cluster_id, article_id) "
                "VALUES (?, ?) ON CONFLICT DO NOTHING",
                [cid, aid],
            )
        if embedding_row is not None:
            conn.execute(
                "INSERT INTO cluster_embeddings (cluster_id, centroid, embedded_at) "
                "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                [cid, embedding_row[0], embedding_row[1]],
            )
        if promotion_row is not None:
            conn.execute(
                """
                INSERT INTO promotion_log (
                    cluster_id, raw_defcon, ceiling_defcon, final_defcon,
                    bands_present, sides_present, confidence,
                    rule_applied, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cluster_id) DO NOTHING
                """,
                [cid, *promotion_row],
            )

    # The actual UPDATE we're here for.
    conn.execute(
        """
        UPDATE arcs
           SET headline             = ?,
               summary              = ?,
               last_update_headline = ?,
               last_update_summary  = ?
         WHERE id = ?
        """,
        [
            headline,
            summary,
            last_update_headline,
            last_update_summary,
            arc_id,
        ],
    )

    # Restore each cluster's arc_id via the same workaround.
    for cid, article_ids, embedding_row, promotion_row in cluster_snapshots:
        conn.execute(
            "DELETE FROM cluster_articles WHERE cluster_id = ?", [cid]
        )
        conn.execute(
            "DELETE FROM cluster_embeddings WHERE cluster_id = ?", [cid]
        )
        conn.execute(
            "DELETE FROM promotion_log WHERE cluster_id = ?", [cid]
        )
        conn.execute(
            "UPDATE clusters SET arc_id = ? WHERE id = ?", [arc_id, cid]
        )
        for aid in article_ids:
            conn.execute(
                "INSERT INTO cluster_articles (cluster_id, article_id) "
                "VALUES (?, ?) ON CONFLICT DO NOTHING",
                [cid, aid],
            )
        if embedding_row is not None:
            conn.execute(
                "INSERT INTO cluster_embeddings (cluster_id, centroid, embedded_at) "
                "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                [cid, embedding_row[0], embedding_row[1]],
            )
        if promotion_row is not None:
            conn.execute(
                """
                INSERT INTO promotion_log (
                    cluster_id, raw_defcon, ceiling_defcon, final_defcon,
                    bands_present, sides_present, confidence,
                    rule_applied, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (cluster_id) DO NOTHING
                """,
                [cid, *promotion_row],
            )

    # Restore arc_centroid.
    if centroid_row is not None:
        conn.execute(
            "INSERT INTO arc_centroids (arc_id, centroid, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT (arc_id) DO NOTHING",
            [arc_id, centroid_row[0], centroid_row[1]],
        )


def _plan_recoverable(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[bool, str, str, str, str, str, str]:
    """Compute (needs_update, current_…, desired_…) for arc_20260523_0146."""
    arc = _read_arc(conn, ARC_RECOVERABLE)
    if arc is None:
        raise SystemExit(
            f"ERROR · {ARC_RECOVERABLE} not present in arcs · cannot repair"
        )
    cluster = _read_cluster(conn, ARC_RECOVERABLE_SOURCE_CLUSTER)
    if cluster is None:
        raise SystemExit(
            f"ERROR · source cluster {ARC_RECOVERABLE_SOURCE_CLUSTER} "
            "not present in clusters · cannot recover headline/summary"
        )
    cur_hl, cur_sum, cur_lu_hl, cur_lu_sum = arc
    new_hl, new_sum = cluster
    if not new_hl or not new_sum:
        raise SystemExit(
            f"ERROR · source cluster {ARC_RECOVERABLE_SOURCE_CLUSTER} "
            "has empty headline or summary itself · cannot recover"
        )
    needs = (
        cur_hl != new_hl
        or cur_sum != new_sum
        or cur_lu_hl != new_hl
        or cur_lu_sum != new_sum
    )
    return needs, cur_hl, cur_sum, cur_lu_hl, cur_lu_sum, new_hl, new_sum


def _plan_unrecoverable(
    conn: duckdb.DuckDBPyConnection,
) -> tuple[bool, str, str, str, str]:
    """Compute (needs_update, current_headline, current_summary, current_lu_h, current_lu_s)."""
    arc = _read_arc(conn, ARC_UNRECOVERABLE)
    if arc is None:
        raise SystemExit(
            f"ERROR · {ARC_UNRECOVERABLE} not present in arcs · cannot repair"
        )
    cur_hl, cur_sum, cur_lu_hl, cur_lu_sum = arc
    needs = (
        cur_hl != PLACEHOLDER_HEADLINE
        or cur_sum != PLACEHOLDER_SUMMARY
        or cur_lu_hl != PLACEHOLDER_HEADLINE
        or cur_lu_sum != PLACEHOLDER_SUMMARY
    )
    return needs, cur_hl, cur_sum, cur_lu_hl, cur_lu_sum


def _readonly_preview(arc_id: str, fields: tuple[str, ...]) -> None:
    for label, val in zip(
        ("headline", "summary", "last_update_headline", "last_update_summary"),
        fields,
        strict=True,
    ):
        truncated = val if len(val) <= 100 else val[:97] + "…"
        print(f"    {label:22s}: {truncated!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="DuckDB path · defaults to settings.db_path (data/musahit.duckdb).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without modifying the DB.",
    )
    args = parser.parse_args()

    db_path = args.db
    if db_path is None:
        try:
            db_path = Path(get_settings().db_path)
        except Exception:  # pragma: no cover · defensive
            db_path = DEFAULT_DB_PATH
    if not db_path.is_absolute():
        db_path = (REPO_ROOT / db_path).resolve()

    if not db_path.exists():
        print(f"ERROR · DB not found at {db_path}", file=sys.stderr)
        return 2

    print(f"Opening DB at {db_path} (dry_run={args.dry_run})")
    conn = duckdb.connect(str(db_path), read_only=args.dry_run)
    try:
        # ── arc_20260523_0146 (recoverable) ────────────────────────────
        print()
        print(f"=== {ARC_RECOVERABLE} (recoverable from {ARC_RECOVERABLE_SOURCE_CLUSTER}) ===")
        (
            needs_r,
            cur_hl_r,
            cur_sum_r,
            cur_lu_hl_r,
            cur_lu_sum_r,
            new_hl_r,
            new_sum_r,
        ) = _plan_recoverable(conn)

        print("  current state:")
        _readonly_preview(
            ARC_RECOVERABLE,
            (cur_hl_r, cur_sum_r, cur_lu_hl_r, cur_lu_sum_r),
        )
        print("  desired state:")
        _readonly_preview(
            ARC_RECOVERABLE,
            (new_hl_r, new_sum_r, new_hl_r, new_sum_r),
        )

        if not needs_r:
            print(f"  → already at desired state · skipping {ARC_RECOVERABLE}")
        elif args.dry_run:
            print(f"  → DRY RUN · would UPDATE {ARC_RECOVERABLE}")
        else:
            _apply_update_with_fk_workaround(
                conn,
                ARC_RECOVERABLE,
                headline=new_hl_r,
                summary=new_sum_r,
                last_update_headline=new_hl_r,
                last_update_summary=new_sum_r,
            )
            print(f"  → APPLIED · updated {ARC_RECOVERABLE}")

        # ── arc_20260523_0036 (placeholder) ────────────────────────────
        print()
        print(f"=== {ARC_UNRECOVERABLE} (placeholder · no recoverable source) ===")
        (
            needs_u,
            cur_hl_u,
            cur_sum_u,
            cur_lu_hl_u,
            cur_lu_sum_u,
        ) = _plan_unrecoverable(conn)

        print("  current state:")
        _readonly_preview(
            ARC_UNRECOVERABLE,
            (cur_hl_u, cur_sum_u, cur_lu_hl_u, cur_lu_sum_u),
        )
        print("  desired state:")
        _readonly_preview(
            ARC_UNRECOVERABLE,
            (
                PLACEHOLDER_HEADLINE,
                PLACEHOLDER_SUMMARY,
                PLACEHOLDER_HEADLINE,
                PLACEHOLDER_SUMMARY,
            ),
        )

        if not needs_u:
            print(f"  → already at desired state · skipping {ARC_UNRECOVERABLE}")
        elif args.dry_run:
            print(f"  → DRY RUN · would UPDATE {ARC_UNRECOVERABLE}")
        else:
            _apply_update_with_fk_workaround(
                conn,
                ARC_UNRECOVERABLE,
                headline=PLACEHOLDER_HEADLINE,
                summary=PLACEHOLDER_SUMMARY,
                last_update_headline=PLACEHOLDER_HEADLINE,
                last_update_summary=PLACEHOLDER_SUMMARY,
            )
            print(f"  → APPLIED · updated {ARC_UNRECOVERABLE}")

        # ── Post-condition checks (idempotence + tripwire guard) ───────
        if not args.dry_run:
            print()
            print("=== Post-condition checks ===")
            # Confirm last_update_at and last_update_cluster_id stayed put.
            for arc_id in (ARC_RECOVERABLE, ARC_UNRECOVERABLE):
                row = conn.execute(
                    "SELECT last_update_at, last_update_cluster_id "
                    "FROM arcs WHERE id = ?",
                    [arc_id],
                ).fetchone()
                if row is not None:
                    print(
                        f"  {arc_id}: last_update_at={row[0]}, "
                        f"last_update_cluster_id={row[1]!r}"
                    )
    finally:
        conn.close()
    print()
    print("Done.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
