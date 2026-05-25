"""Verify DB state after the repair script's mid-execution failure.

The repair script errored on `DELETE FROM arc_centroids` because the VSS
extension wasn't loaded. We need to know:

1. Did the arc row for 0146 actually get UPDATED before the error?
2. Did the arc_centroids row for 0146 get DELETED before the error?
3. Did 0036 get processed at all (it came second in script order)?

This script is READ-ONLY. It only inspects state.
"""
import duckdb


def main() -> None:
    conn = duckdb.connect("data/musahit.duckdb", read_only=True)

    print("=== ARC ROWS ===\n")
    for arc_id in ["arc_20260523_0146", "arc_20260523_0036"]:
        row = conn.execute(
            """
            SELECT headline, summary, last_update_headline, last_update_summary,
                   last_update_at, last_update_cluster_id
            FROM arcs
            WHERE id = ?
            """,
            [arc_id],
        ).fetchone()

        print(f"--- {arc_id} ---")
        if row is None:
            print("  !!! ARC ROW MISSING !!!")
        else:
            headline = row[0] if row[0] is not None else "(NULL)"
            summary = row[1] if row[1] is not None else "(NULL)"
            lu_headline = row[2] if row[2] is not None else "(NULL)"
            lu_summary = row[3] if row[3] is not None else "(NULL)"

            print(f"  headline:             {headline[:80]!r}")
            print(f"  summary:              {summary[:80]!r}")
            print(f"  last_update_headline: {lu_headline[:80]!r}")
            print(f"  last_update_summary:  {lu_summary[:80]!r}")
            print(f"  last_update_at:       {row[4]}")
            print(f"  last_update_cluster_id: {row[5]}")
        print()

    print("\n=== ARC_CENTROIDS ROWS ===\n")
    # Try to query arc_centroids · this might also fail if VSS isn't loaded
    # but DuckDB read-only might still let us SELECT
    try:
        for arc_id in ["arc_20260523_0146", "arc_20260523_0036"]:
            try:
                row = conn.execute(
                    "SELECT arc_id, length(centroid) FROM arc_centroids WHERE arc_id = ?",
                    [arc_id],
                ).fetchone()
                if row is None:
                    print(f"  {arc_id}: !!! CENTROID ROW MISSING !!!")
                else:
                    print(f"  {arc_id}: centroid exists, length={row[1]}")
            except Exception as e:
                print(f"  {arc_id}: error querying centroid: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"  arc_centroids query failed: {type(e).__name__}: {e}")
        print("  This might mean VSS isn't loaded · try loading it first")

    # Also check: how many arc_centroids rows total
    print()
    try:
        count = conn.execute("SELECT COUNT(*) FROM arc_centroids").fetchone()[0]
        print(f"  Total arc_centroids rows in DB: {count}")
        arcs_count = conn.execute("SELECT COUNT(*) FROM arcs").fetchone()[0]
        print(f"  Total arcs rows in DB: {arcs_count}")
        if count != arcs_count:
            print(f"  !!! MISMATCH · {arcs_count - count} arcs missing centroids !!!")
    except Exception as e:
        print(f"  Count query failed: {type(e).__name__}: {e}")

    conn.close()


if __name__ == "__main__":
    main()
