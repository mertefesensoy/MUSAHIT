from __future__ import annotations

import sys
import unicodedata

import duckdb

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_20260528"
DB_PATH = "data/musahit.duckdb"

MIN_WORD_COUNT_FOR_EMBEDDING = 10


def script_summary(s):
    buckets = {}
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


def has_control_chars(s):
    bad = []
    for ch in s:
        if ch in ("\n", "\r", "\t"):
            continue
        if unicodedata.category(ch).startswith("C"):
            bad.append("U+%04X" % ord(ch))
    return bad


def main():
    conn = duckdb.connect(DB_PATH, read_only=True)
    rows = conn.execute(
        "SELECT a.id, a.title, a.lead, a.language, a.word_count, a.published_at "
        "FROM articles a JOIN ingest_log l ON l.source_id = a.source_id "
        "WHERE l.run_id = ? AND a.word_count >= ? ORDER BY a.published_at",
        [RUN_ID, MIN_WORD_COUNT_FOR_EMBEDDING],
    ).fetchall()

    print("eligible (word_count >= %d): %d\n" % (MIN_WORD_COUNT_FOR_EMBEDDING, len(rows)))

    print("=== CONTENT PATHOLOGY SCAN (all eligible) ===")
    flagged = 0
    for idx, (aid, title, lead, lang, wc, pub) in enumerate(rows):
        text = ((title or "") + "\n\n" + (lead or "")).strip()
        scripts = script_summary(text)
        controls = has_control_chars(text)
        non_latin = sum(v for k, v in scripts.items() if k in ("cjk", "arabic", "cyrillic"))
        if controls or non_latin > 0 or len(text) == 0:
            flagged += 1
            print("\n[embed_idx=%d] id=%r lang=%r wc=%s len=%d" % (idx, aid, lang, wc, len(text)))
            if controls:
                print("  CONTROL CHARS: %s" % controls[:20])
            if non_latin > 0:
                print("  NON-LATIN: %s" % {k: v for k, v in scripts.items() if k in ("cjk", "arabic", "cyrillic")})
            if len(text) == 0:
                print("  EMPTY TEXT after title+lead strip")
            print("  title: %s" % (title or "")[:100])
            print("  lead : %s" % (lead or "")[:100])

    print("\n=== %d flagged of %d eligible ===" % (flagged, len(rows)))

    print("\n=== EMBED-ORDER WINDOW 150-185 ===")
    for idx in range(150, min(185, len(rows))):
        aid, title, lead, lang, wc, pub = rows[idx]
        text = ((title or "") + "\n\n" + (lead or "")).strip()
        scripts = script_summary(text)
        non_latin = sum(v for k, v in scripts.items() if k in ("cjk", "arabic", "cyrillic"))
        flag = ""
        if non_latin > 0:
            flag += " <NON-LATIN>"
        if has_control_chars(text):
            flag += " <CONTROL>"
        if len(text) == 0:
            flag += " <EMPTY>"
        print("  [%d] lang=%r len=%d wc=%s%s  %s" % (idx, lang, len(text), wc, flag, (title or "")[:70]))

    conn.close()


if __name__ == "__main__":
    main()
