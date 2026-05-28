"""Find the poison article(s) crashing bge-m3 /api/embed for a given run.

Schema confirmed (articles): id, source_id, url, fetched_at,
published_at, title, lead, body, language, entities, word_count.
ingest_log links via source_id + run_id.

The embedder dies deterministically around the 11th batch of 16
(embed-order articles ~160-180). This script reproduces the eligible
selection order, then scans every eligible article for content
pathologies that can 500 an embedding endpoint:
  - non-Latin / mixed-script text (e.g. CJK leak)
  - control / non-printable characters
  - unusual length extremes
and prints the suspicious window in embed order.

Usage:
    python scripts/triage/find_poison_article.py [run_id]
"""

from __future__ import annotations

import sys
import unicodedata

import duckdb

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_20260528"
DB_PATH = "data/musahit.duckdb"

MIN_WORD_COUNT_FOR_EMBEDDING = 10  # matches clusterer floor


def script_summary(s: str) -> dict[str, int]:
    """Count characters by Unicode script-ish bucket."""
    buckets: dict[str, int] = {}
    for ch in s:
        if ch.isspace():
            continue
        cp = ord(ch)
        if cp < 0x80:
            key = "latin_ascii"
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            key = "cjk"
        elif 0x0600 <= cp <= 0x06FF:
            key = "arabic"
        elif 0x0400 <= cp <= 0x04FF:
            key = "cyrillic"
        elif unicodedata.category(ch).startswith("C"):
            key = "control_or_other"
        else:
            key = "other_latinish"
        buckets[key] = buckets.get(key, 0) + 1
    return buckets


def has_control_chars(s: str) -> list[str]:
    bad = []
    for ch in s:
        if ch in ("\n", "\r", "\t"):
            continue
        if unicodedata.category(ch).startswith("C"):
            bad.append(f"U+{ord(ch):04X}")
    return bad


def main() -> None:
    conn = duckdb.connect(DB_PATH, read_only=True)

    # Reproduce eligible selection. The clusterer's _select_eligible
    # returns run articles lacking embeddings; here we approximate the
    # set (all run articles >= word floor) in id order, then in
    # published_at order, so we can locate the ~160-180 window.
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.lead, a.language, a.word_count, a.published_at
        FROM articles a
        JOIN ingest_log l ON l.source_id = a.source_id
        WHERE l.run_id = ?
          AND a.word_count >= ?
        ORDER BY a.published_at
        """,
        [RUN_ID, MIN_WORD_COUNT_FOR_EMBEDDING],
    ).fetchall()

    print(f"eligible (word_count >= {MIN_WORD_COUNT_FOR_EMBEDDING}): {len(rows)}\n")

    print("=== CONTENT PATHOLOGY SCAN (all eligible) ===")
    flagged = 0
    for idx, (aid, title, lead, lang, wc, pub) in enumerate(rows):
        text = f"{title or ''}\n\n{lead or ''}".strip()
        scripts = script_summary(text)
        controls = has_control_chars(text)
        non_latin = sum(
            v for k, v in scripts.items() if k in ("cjk", "arabic", "cyrillic")
        )
        suspicious = bool(controls) or non_latin > 0 or len(text) == 0
        if suspicious:
            flagged += 1
            print(f"\n[embed_idx={idx}] id={aid!r} lang={lang!r} wc={wc} len={len(text)}")
            if controls:
                print(f"  CONTROL CHARS: {controls[:20]}")
            if non_latin > 0:
                print(f"  NON-LATIN: { {k: v for k, v in scripts.items() if k in ('cjk','arabic','cyrillic')} }")
            if len(text) == 0:
                print("  EMPTY TEXT after title+lead strip")
            print(f"  title: {(title or '')[:100]}")
            print(f"  lead : {(lead or '')[:100]}")

    print(f"\n=== {flagged} flagged of {len(rows)} eligible ===")

    print("\n=== EMBED-ORDER WINDOW 150-185 (around the failure batch) ===")
    for idx in range(150, min(185, len(rows))):
        aid, title, lead, lang, wc, pub = rows[idx]
        text = f"{title or ''}\n\n{lead or ''}".strip()
        scripts = script_summary(text)
        non_latin = sum(
            v for k, v in scripts.items() if k in ("cjk", "arabic", "cyrillic")
        )
        flag = ""
        if non_latin > 0:
            flag += " <NON-LATIN>"
        if has_control_chars(text):
            flag += " <CONTROL>"
        if len(text) == 0:
            flag += " <EMPTY>"
        print(f"  [{idx}] lang={lang!r} len={len(text)} wc={wc}{flag}  {(title or '')[:70]}")

    conn.close()


if __name__ == "__main__":
    main()
