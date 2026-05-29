"""Tier-2 integration · the arc-freshness signal flows arcs → writer → TTS.

Wires the REAL arc lifecycle pass (:func:`musahit.arcs.transitions.
transition_states`), the REAL writer (:class:`musahit.writer.briefer.
Briefer` over a real DB payload), and the REAL TTS preprocessor
(:func:`musahit.tts.preprocessor.preprocess_for_tts`) against a seeded
DuckDB with arcs at varied last-update ages (today / 1d / 3d / 8d) and
asserts the whole contract end-to-end on production code:

* the 8-day arc EXPIRES → the lifecycle resolves it (FK-safe) and it is
  absent from every active section (silent backlog drain · open-arc count
  drops);
* the 3-day arc is DORMANT → present in the markdown with "· 3 gün önce"
  but ABSENT from the spoken (TTS) text;
* the today/1-day arcs are FRESH → present in both, marked "bugün"/"dün";
* AÇIK GELİŞMELER + AMBİYANS (and DEFCON 4) are itemized data lists with
  NO LLM call · a fabrication marker fed through the fake LLM never lands
  in them.

The fake LLM is wired to emit a fabrication marker on every call; if any
itemized section were routed through it, the marker would surface there.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from musahit.arcs.transitions import transition_states
from musahit.common.migrations import init_db
from musahit.common.types import ArcState
from musahit.ingest.sources import seed_sources
from musahit.score.defcon import DEFCON
from musahit.score.llm_client import FakeLlmClient
from musahit.tts.extractor import extract_voiced_briefing
from musahit.tts.preprocessor import preprocess_for_tts
from musahit.writer.briefer import Briefer

RUN_ID = "run_freshness"
NOW = datetime(2026, 5, 29, 12, 0, 0)
BRIEFING_DATE = NOW.date()
DIM = 1024

# Fed through the fake LLM on every call · stands in for the İş Bankası /
# Guantanamo Mode-4 fabrications. Must never appear in a deterministic
# (itemized) section.
FABRICATION = "İş Bankası kredi paketi · Guantanamo denemesi"

ARC_HEADLINES = {
    "arc_20260529_0001": "Bugünkü Hikaye",  # 0 days → FRESH
    "arc_20260528_0001": "Dünkü Hikaye",  # 1 day  → FRESH
    "arc_20260526_0001": "Üç Günlük Hikaye",  # 3 days → DORMANT
    "arc_20260521_0001": "Sekiz Günlük Hikaye",  # 8 days → EXPIRED
}


@pytest.fixture()
def db(tmp_path: Path) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    db_path = tmp_path / "x.duckdb"
    init_db(db_path, load_vss=False)
    conn = duckdb.connect(str(db_path))
    seed_sources(conn)
    conn.execute(
        "INSERT INTO pipeline_runs (run_id, started_at, status, stages_done, counts) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            RUN_ID,
            NOW,
            "RUNNING",
            json.dumps(["ingest", "normalize", "cluster", "score", "arc-link"]),
            json.dumps({}),
        ],
    )
    try:
        yield conn
    finally:
        conn.close()


def _vec(idx: int) -> list[float]:
    v = [0.0] * DIM
    v[idx % DIM] = 1.0
    return v


def _insert_arc(
    conn: duckdb.DuckDBPyConnection, arc_id: str, age_days: int, peak: int
) -> None:
    last_update = NOW - timedelta(days=age_days)
    headline = ARC_HEADLINES[arc_id]
    conn.execute(
        "INSERT INTO arcs (id, created_at, headline, summary, state, last_update_at, "
        "category, peak_defcon, entity_set, last_update_summary, "
        "last_update_headline, last_update_cluster_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        [
            arc_id,
            last_update - timedelta(days=1),
            headline,
            f"{headline} özeti.",
            ArcState.OPEN.value,
            last_update,
            "POLİTİKA",
            peak,
            json.dumps(["Entity"]),
            f"{headline} özeti.",
            headline,
        ],
    )
    # Every arc owns a centroid · exercises the FK-safe resolution path
    # when the 8-day arc is auto-resolved.
    conn.execute(
        "INSERT INTO arc_centroids (arc_id, centroid, updated_at) VALUES (?, ?, ?)",
        [arc_id, _vec(hash(arc_id) % DIM), last_update],
    )


def _insert_cluster(
    conn: duckdb.DuckDBPyConnection,
    cluster_id: str,
    defcon: int,
    source_id: str,
    *,
    headline: str,
    arc_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ingest_log (run_id, source_id, started_at, "
        "completed_at, status, articles_fetched) VALUES (?, ?, ?, ?, ?, ?)",
        [RUN_ID, source_id, NOW, NOW, "OK", 1],
    )
    aid = f"{cluster_id}_a"
    conn.execute(
        "INSERT INTO articles (id, source_id, url, fetched_at, published_at, "
        "title, lead, body, language, entities, word_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [aid, source_id, f"u/{aid}", NOW, NOW, "t", "l", "b", "tr", "[]", 40],
    )
    conn.execute(
        "INSERT INTO clusters (id, created_at, headline, summary, category, "
        "raw_defcon, ceiling_defcon, final_defcon, confidence, bands_present, "
        "arc_id, operator_override) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        [
            cluster_id,
            NOW,
            headline,
            f"{headline} özeti.",
            "POLİTİKA",
            defcon,
            defcon,
            defcon,
            "ORTA",
            json.dumps(["independent"]),
            arc_id,
        ],
    )
    conn.execute(
        "INSERT INTO cluster_articles (cluster_id, article_id) VALUES (?, ?)",
        [cluster_id, aid],
    )


def _seed(conn: duckdb.DuckDBPyConnection) -> None:
    _insert_arc(conn, "arc_20260529_0001", 0, int(DEFCON.SEVERE))
    _insert_arc(conn, "arc_20260528_0001", 1, int(DEFCON.MATERIAL))
    _insert_arc(conn, "arc_20260526_0001", 3, int(DEFCON.MATERIAL))
    _insert_arc(conn, "arc_20260521_0001", 8, int(DEFCON.MATERIAL))
    # A DEFCON-1-2 cluster so the LLM is actually invoked (proves the
    # fabrication marker can leak — and that it only leaks into the LLM
    # section, never the itemized ones).
    _insert_cluster(
        conn, "cl_priority", int(DEFCON.SEVERE), "bianet", headline="Öncelikli olay"
    )
    # A DEFCON-4 routine cluster linked to the 3-day arc · its recency
    # anchor is the arc's last_update (3 days), so the DEFCON-4 line shows
    # "3 gün önce" — the brief's recovered-stale-thread scenario.
    _insert_cluster(
        conn,
        "cl_routine",
        int(DEFCON.ROUTINE),
        "cumhuriyet",
        headline="Rutin gelişme",
        arc_id="arc_20260526_0001",
    )
    # A DEFCON-5 ambient cluster (today) → AMBİYANS itemized list, "bugün".
    _insert_cluster(
        conn, "cl_ambient", int(DEFCON.AMBIENT), "diken", headline="Ambiyans başlığı"
    )


async def _run_writer(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> tuple[str, FakeLlmClient]:
    llm = FakeLlmClient(responder=lambda _p, _a: f"{FABRICATION}\n")
    briefer = Briefer(
        conn, llm, briefings_root=tmp_path / "briefings", target_date=BRIEFING_DATE
    )
    result = await briefer.run(RUN_ID)
    markdown = Path(result["path"]).read_text(encoding="utf-8")
    return markdown, llm


def _section(markdown: str, marker: str) -> str:
    """Return the body lines under ``marker`` (up to the next ## ❯)."""
    lines = markdown.splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if line.startswith("## ❯"):
            if capturing:
                break
            capturing = line.strip() == marker
            continue
        if capturing:
            out.append(line)
    return "\n".join(out)


# ── The integration assertions ─────────────────────────────────────────────


class TestArcFreshnessFlow:
    async def test_eight_day_arc_resolves_and_drains_backlog(
        self, db: duckdb.DuckDBPyConnection
    ) -> None:
        _seed(db)
        open_before = db.execute(
            "SELECT COUNT(*) FROM arcs WHERE state = ?", [ArcState.OPEN.value]
        ).fetchone()[0]
        assert open_before == 4

        result = transition_states(db, NOW)
        assert result["expired_to_resolved"] == 1

        open_after = db.execute(
            "SELECT COUNT(*) FROM arcs WHERE state = ?", [ArcState.OPEN.value]
        ).fetchone()[0]
        assert open_after == 3  # backlog drained by one
        assert (
            db.execute(
                "SELECT state FROM arcs WHERE id = 'arc_20260521_0001'"
            ).fetchone()[0]
            == ArcState.RESOLVED.value
        )

    async def test_markdown_surfacing_by_freshness(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed(db)
        transition_states(db, NOW)
        markdown, llm = await _run_writer(db, tmp_path)

        açık = _section(markdown, "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP")
        # FRESH arcs present and marked.
        assert "Bugünkü Hikaye" in açık and "· bugün" in açık
        assert "Dünkü Hikaye" in açık and "· dün" in açık
        # DORMANT arc present in markdown with its recency.
        assert "Üç Günlük Hikaye" in açık and "· 3 gün önce" in açık
        # EXPIRED arc resolved → absent everywhere (silent drain, not KAPATILAN).
        assert "Sekiz Günlük Hikaye" not in markdown

        # Itemized sections are deterministic data · no fabrication leak.
        ambiyans = _section(markdown, "## ❯ AMBİYANS · DEFCON 5")
        assert FABRICATION not in açık
        assert FABRICATION not in ambiyans
        assert FABRICATION not in _section(markdown, "## ❯ DEFCON 4 · GÜNDEM")
        # AMBİYANS is the itemized ambient list.
        assert "Ambiyans başlığı" in ambiyans and "· bugün" in ambiyans
        # DEFCON 4 line shows the linked arc's true age.
        defcon4 = _section(markdown, "## ❯ DEFCON 4 · GÜNDEM")
        assert "Rutin gelişme" in defcon4 and "· 3 gün önce" in defcon4

        # The LLM was only called for DEFCON 1-2 / DEFCON 3 (priority seeded,
        # material empty → exactly one call), and its fabrication landed there.
        assert llm.call_count == 1
        assert FABRICATION in _section(markdown, "## ❯ DEFCON 1-2 · ÖNCELİKLİ")

    async def test_open_arc_count_drops_in_payload(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed(db)
        transition_states(db, NOW)
        markdown, _ = await _run_writer(db, tmp_path)
        # SİSTEM LOG reflects the drained open-arc count (3, not 4).
        syslog = _section(markdown, "## ❯ SİSTEM LOG")
        assert "**Açık hikaye** · 3" in syslog

    async def test_dormant_dropped_from_voice_fresh_kept(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        _seed(db)
        transition_states(db, NOW)
        markdown, _ = await _run_writer(db, tmp_path)

        voiced = extract_voiced_briefing(markdown)
        spoken_open_arcs = preprocess_for_tts(voiced.open_arcs)

        # FRESH arcs are voiced.
        assert "Bugünkü Hikaye" in spoken_open_arcs
        assert "Dünkü Hikaye" in spoken_open_arcs
        # DORMANT (3-day) arc is dropped from the spoken text …
        assert "Üç Günlük Hikaye" not in spoken_open_arcs
        # … but remains in the on-disk markdown (TTS never rewrites it).
        assert "Üç Günlük Hikaye" in markdown
        # Arc-id rewrite still works on the surviving fresh lines.
        assert "hikaye 1" in spoken_open_arcs
        assert "arc_2026" not in spoken_open_arcs

    async def test_all_dormant_open_arcs_voice_note(
        self, db: duckdb.DuckDBPyConnection, tmp_path: Path
    ) -> None:
        # Only DORMANT open arcs (no fresh) → spoken AÇIK GELİŞMELER becomes
        # the brief "no fresh updates" note, while markdown keeps them.
        _insert_arc(db, "arc_20260526_0001", 3, int(DEFCON.MATERIAL))
        _insert_arc(db, "arc_20260521_0001", 8, int(DEFCON.MATERIAL))  # will resolve
        transition_states(db, NOW)
        markdown, _ = await _run_writer(db, tmp_path)

        voiced = extract_voiced_briefing(markdown)
        spoken = preprocess_for_tts(voiced.open_arcs)
        from musahit.tts.preprocessor import ALL_DORMANT_VOICE_NOTE

        assert ALL_DORMANT_VOICE_NOTE in spoken
        assert "Üç Günlük Hikaye" not in spoken
        # Markdown still lists the dormant arc with its recency.
        açık = _section(markdown, "## ❯ AÇIK GELİŞMELER · DEVAM EDEN TAKİP")
        assert "Üç Günlük Hikaye" in açık and "· 3 gün önce" in açık
