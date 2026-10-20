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
  weekly_breakdown_json TEXT,         -- full seven_day_breakdown rows (per-surface split)
  device             TEXT,            -- who polled; the reading itself is account-wide
  received_at        TEXT             -- when it reached this DB (edges spool while offline)
);
CREATE INDEX IF NOT EXISTS idx_session_window ON snapshots(session_resets_at);
CREATE INDEX IF NOT EXISTS idx_weekly_window  ON snapshots(weekly_resets_at);

-- every machine whose data reaches this DB; last_seen_at = newest activity observed
-- from it (transcript mtime or snapshot captured_at), not the time of the push
CREATE TABLE IF NOT EXISTS devices (
  name         TEXT PRIMARY KEY,
  first_seen   TEXT,
  last_seen_at TEXT
);
"""

COLUMNS = [
    "captured_at", "status", "http_code",
    "session_pct", "session_resets_at",
    "weekly_pct", "weekly_resets_at", "weekly_opus_pct",
    "extra_usage_pct", "extra_used_credits", "cc_weekly_share",
    "weekly_breakdown_json", "device", "received_at",
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
    for col, decl in [("weekly_breakdown_json", "TEXT"), ("device", "TEXT"),
                      ("received_at", "TEXT")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {decl}")
    if "device" not in existing:
        # rows from before multi-device capture; which machine polled is unknown
        conn.execute("UPDATE snapshots SET device='legacy' WHERE device IS NULL")
    # a retried upload of the same edge snapshot must be a no-op
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_snap_device_ts "
                 "ON snapshots(device, captured_at)")
    conn.commit()


def insert_snapshot(conn: sqlite3.Connection, row: dict) -> bool:
    """Insert one snapshot; False if (device, captured_at) was already stored."""
    values = [row.get(c) for c in COLUMNS]
    placeholders = ",".join("?" * len(COLUMNS))
    cur = conn.execute(
        f"INSERT OR IGNORE INTO snapshots ({','.join(COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    if row.get("device"):
        touch_device(conn, row["device"], row["captured_at"])
    conn.commit()
    return cur.rowcount == 1


def touch_device(conn: sqlite3.Connection, name: str, seen_at: str) -> None:
    """Record activity from a device. ISO8601 UTC strings compare chronologically."""
    conn.execute(
        """INSERT INTO devices (name, first_seen, last_seen_at) VALUES (?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             first_seen   = MIN(devices.first_seen, excluded.first_seen),
             last_seen_at = MAX(devices.last_seen_at, excluded.last_seen_at)""",
        (name, seen_at, seen_at),
    )
