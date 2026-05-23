import duckdb

conn = duckdb.connect("data/musahit.duckdb", read_only=True)

print("=== Foreign keys touching clusters ===")
rows = conn.execute("""
    SELECT table_name, constraint_text
    FROM duckdb_constraints()
    WHERE constraint_type = 'FOREIGN KEY'
""").fetchall()
for r in rows:
    print(r)
