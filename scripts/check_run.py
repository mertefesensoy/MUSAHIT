import duckdb

c = duckdb.connect("data/musahit.duckdb", read_only=True)
row = c.execute("""
    SELECT run_id, status, started_at, completed_at, stages_done, failed_stages
    FROM pipeline_runs ORDER BY started_at DESC LIMIT 1
""").fetchone()
print(row)
