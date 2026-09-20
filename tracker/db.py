"""SQLite store for usage snapshots. Stdlib only."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "usage.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  captured_at        TEXT NOT NULL,   -- our UTC clock, ISO8601
  status             TEXT NOT NULL,   -- 'ok' | 'token_expired' | 'http_error'
  http_code          INTEGER,
  session_pct        REAL,
  session_resets_at  TEXT,            -- also the session-window identity
  weekly_pct         REAL,
  weekly_resets_at   TEXT,            -- also the weekly-window identity
  weekly_opus_pct    REAL,
  extra_usage_pct    REAL,
  extra_used_credits REAL,
  cc_weekly_share    REAL,            -- Claude Code's share of weekly usage
  weekly_breakdown_json TEXT          -- full seven_day_breakdown rows (per-surface split)
);
CREATE INDEX IF NOT EXISTS idx_session_window ON snapshots(session_resets_at);
CREATE INDEX IF NOT EXISTS idx_weekly_window  ON snapshots(weekly_resets_at);
"""

COLUMNS = [
    "captured_at", "status", "http_code",
    "session_pct", "session_resets_at",
    "weekly_pct", "weekly_resets_at", "weekly_opus_pct",
    "extra_usage_pct", "extra_used_credits", "cc_weekly_share",
    "weekly_breakdown_json",
]


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to a snapshots table created by an older schema."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
    for col, decl in [("weekly_breakdown_json", "TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {decl}")
    conn.commit()


def insert_snapshot(conn: sqlite3.Connection, row: dict) -> None:
    values = [row.get(c) for c in COLUMNS]
    placeholders = ",".join("?" * len(COLUMNS))
    conn.execute(
        f"INSERT INTO snapshots ({','.join(COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
