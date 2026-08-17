import sqlite3
from pathlib import Path

conn = sqlite3.connect(Path(__file__).resolve().parents[1] / "data/research.db")
rows = conn.execute(
    "SELECT DISTINCT a.name FROM findings f JOIN apps a ON a.id=f.app_id WHERE f.pass_number=1 ORDER BY a.id"
).fetchall()
print(",".join(r[0] for r in rows))
