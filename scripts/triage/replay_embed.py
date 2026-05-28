import asyncio, sys
import duckdb, httpx

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "run_20260528"
DB_PATH = "data/musahit.duckdb"
URL = "http://localhost:11434/api/embed"
MIN_WC = 10

def embedding_input(title, lead):
    return ((title or "") + "\n\n" + (lead or "")).strip()

async def main():
    conn = duckdb.connect(DB_PATH, read_only=True)
    rows = conn.execute(
        "SELECT a.id, a.title, a.lead, a.word_count "
        "FROM articles a "
        "JOIN ingest_log l ON l.source_id = a.source_id "
        "LEFT JOIN article_embeddings e ON e.article_id = a.id "
        "WHERE l.run_id = ? AND a.word_count >= ? AND e.article_id IS NULL "
        "ORDER BY a.id",
        [RUN_ID, MIN_WC],
    ).fetchall()
    conn.close()
    print("real eligible (lacking embedding, wc>=%d): %d\n" % (MIN_WC, len(rows)))
    failures = []
    async with httpx.AsyncClient(timeout=60) as client:
        for i, (aid, title, lead, wc) in enumerate(rows):
            text = embedding_input(title, lead)
            try:
                r = await client.post(URL, json={"model": "bge-m3", "input": [text]})
            except Exception as exc:
                print("[%d] id=%s EXCEPTION %s: %s" % (i, aid, type(exc).__name__, exc))
                failures.append((i, aid, title, len(text), "exc"))
                continue
            if r.status_code != 200:
                print("[%d] id=%s STATUS %d len=%d wc=%s" % (i, aid, r.status_code, len(text), wc))
                print("     title: %s" % (title or "")[:90])
                print("     lead : %s" % (lead or "")[:90])
                print("     resp : %s" % r.text[:160])
                failures.append((i, aid, title, len(text), r.status_code))
            elif i % 50 == 0:
                print("[%d] ok ..." % i)
    print("\n=== %d failing article(s) of %d ===" % (len(failures), len(rows)))
    for i, aid, title, n, status in failures:
        print("  idx=%d status=%s len=%d id=%s" % (i, status, n, aid))
        print("    title: %s" % (title or "")[:100])

asyncio.run(main())
