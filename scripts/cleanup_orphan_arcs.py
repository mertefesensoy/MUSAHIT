"""One-off cleanup of the orphan arc row left by the 2026-05-23 cascade.

The first end-to-end smoke run on 2026-05-23 surfaced a bug in
``musahit/arcs/linker.py``: the FK workaround in
``_update_cluster_arc_id`` was missing ``promotion_log``, so every
cluster.arc_id UPDATE raised and the counter in ``ArcLinker.run``
never advanced. 240 clusters all retried the same arc_id
(``arc_20260523_0001``) · the first INSERT succeeded, every
subsequent one duplicated the PK. The surviving ``arcs`` row has no
linked clusters · it is an orphan.

Run once after pulling the fix (commit on 2026-05-24) and before the
next smoke run::

    python scripts/cleanup_orphan_arcs.py

Idempotent: rerunning after the cleanup is a no-op and prints
``0 / 0`` deletions.

The script targets the exact orphan id only · it does NOT scan for
arbitrary orphans. If the next smoke run leaves more orphans we will
generalise.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is on sys.path so ``import musahit`` works.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from musahit.common.config import get_settings  # noqa: E402
from musahit.common.db import open_connection  # noqa: E402
from musahit.common.logging import configure_logging, get_logger  # noqa: E402

ORPHAN_ARC_ID = "arc_20260523_0001"


def main() -> int:
    configure_logging()
    log = get_logger("cleanup_orphan_arcs")
    settings = get_settings()
    log.info(
        "cleanup_orphan_arcs_starting",
        db_path=str(settings.db_path),
        target=ORPHAN_ARC_ID,
    )

    # ``arc_centroids`` carries an HNSW index built by the VSS extension ·
    # DELETE on that table requires VSS to be loaded or DuckDB refuses
    # with "unknown index type 'HNSW'". Load it (non-fatal if missing ·
    # the script will then surface the underlying error to the operator).
    with open_connection(settings.db_path, load_vss=True) as conn:
        # Pre-flight: confirm the row has no linked clusters before we
        # delete · refuse to nuke an arc that is actually in use.
        linked = conn.execute(
            "SELECT COUNT(*) FROM clusters WHERE arc_id = ?",
            [ORPHAN_ARC_ID],
        ).fetchone()[0]
        if linked:
            log.error(
                "cleanup_orphan_arcs_refused",
                target=ORPHAN_ARC_ID,
                linked_clusters=linked,
            )
            print(
                f"REFUSED · {ORPHAN_ARC_ID} has {linked} linked cluster(s); "
                f"this is not an orphan."
            )
            return 1

        centroid_deleted = conn.execute(
            "DELETE FROM arc_centroids WHERE arc_id = ? RETURNING arc_id",
            [ORPHAN_ARC_ID],
        ).fetchall()
        arc_deleted = conn.execute(
            "DELETE FROM arcs WHERE id = ? RETURNING id",
            [ORPHAN_ARC_ID],
        ).fetchall()

    log.info(
        "cleanup_orphan_arcs_done",
        arc_centroids_deleted=len(centroid_deleted),
        arcs_deleted=len(arc_deleted),
    )
    print(
        f"deleted {len(arc_deleted)} arc row(s), "
        f"{len(centroid_deleted)} arc_centroids row(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
