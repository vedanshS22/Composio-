from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pipeline.schema import AppFinding, AppSeed, VerificationResult


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS apps (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, category TEXT NOT NULL, hint_url TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, pass_number INTEGER NOT NULL, notes TEXT);
        CREATE TABLE IF NOT EXISTS findings (id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL REFERENCES apps(id), run_id INTEGER NOT NULL REFERENCES runs(id), pass_number INTEGER NOT NULL, payload TEXT NOT NULL, extracted_at TEXT NOT NULL, UNIQUE(app_id, run_id, pass_number));
        CREATE TABLE IF NOT EXISTS verification (id INTEGER PRIMARY KEY AUTOINCREMENT, finding_id INTEGER NOT NULL REFERENCES findings(id), verifier_source TEXT NOT NULL, field_name TEXT NOT NULL, pipeline_value TEXT NOT NULL, verified_value TEXT NOT NULL, match INTEGER NOT NULL, notes TEXT, verified_at TEXT NOT NULL);
        """)


def seed_apps(db_path: str | Path, seeds: Iterable[AppSeed]) -> int:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.executemany("INSERT OR IGNORE INTO apps(id,name,category,hint_url) VALUES(?,?,?,?)", [(s.id, s.name, s.category, str(s.hint_url)) for s in seeds])
        return conn.execute("SELECT count(*) FROM apps").fetchone()[0]


def create_run(conn: sqlite3.Connection, pass_number: int, notes: str = "") -> int:
    now = datetime.now(timezone.utc).isoformat()
    return conn.execute("INSERT INTO runs(started_at,pass_number,notes) VALUES(?,?,?)", (now, pass_number, notes)).lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute("UPDATE runs SET finished_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), run_id))


def save_finding(conn: sqlite3.Connection, run_id: int, pass_number: int, finding: AppFinding) -> int:
    now = datetime.now(timezone.utc).isoformat()
    payload = finding.model_dump(mode="json")
    return conn.execute("INSERT INTO findings(app_id,run_id,pass_number,payload,extracted_at) VALUES(?,?,?,?,?)", (finding.app_id, run_id, pass_number, json.dumps(payload), now)).lastrowid


def list_seeds(db_path: str | Path, names: set[str] | None = None) -> list[AppSeed]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM apps ORDER BY id").fetchall()
    seeds = [AppSeed(id=row["id"], name=row["name"], category=row["category"], hint_url=row["hint_url"]) for row in rows]
    return [seed for seed in seeds if names is None or seed.name.lower() in names]


def latest_findings(db_path: str | Path) -> list[tuple[int, AppFinding]]:
    query = """SELECT id, payload FROM (SELECT f.id, f.app_id, f.payload, ROW_NUMBER() OVER (PARTITION BY f.app_id ORDER BY f.pass_number DESC, f.id DESC) AS rank FROM findings f) WHERE rank=1 ORDER BY app_id"""
    with connect(db_path) as conn:
        return [(row["id"], AppFinding.model_validate_json(row["payload"])) for row in conn.execute(query)]


def all_findings(db_path: str | Path) -> list[tuple[int, int, AppFinding]]:
    with connect(db_path) as conn:
        return [(row["id"], row["pass_number"], AppFinding.model_validate_json(row["payload"])) for row in conn.execute("SELECT id,pass_number,payload FROM findings ORDER BY app_id,pass_number")]


def save_verification(conn: sqlite3.Connection, finding_id: int, source: str, result: VerificationResult) -> None:
    conn.execute("INSERT INTO verification(finding_id,verifier_source,field_name,pipeline_value,verified_value,match,notes,verified_at) VALUES(?,?,?,?,?,?,?,?)", (finding_id, source, result.field_name, result.pipeline_value, result.verified_value, int(result.match), result.notes, datetime.now(timezone.utc).isoformat()))


def verification_rows(db_path: str | Path) -> list[dict]:
    with connect(db_path) as conn:
        return [dict(row) for row in conn.execute("""SELECT a.name app, v.* FROM verification v JOIN findings f ON f.id=v.finding_id JOIN apps a ON a.id=f.app_id ORDER BY a.id,v.id""")]
