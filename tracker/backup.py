"""Daily consistent copy of usage.db. Stdlib only.

Uses SQLite's online backup API: a plain `cp` of a WAL-mode database can capture the
main file and -wal out of step and restore as corrupt. Keeps the newest KEEP copies.
These sit on the same disk, so they guard against a bad write or migration, not disk
loss; copy data/backup/ off the machine for that.
"""
import sqlite3
from datetime import datetime, timezone

import db

BACKUP_DIR = db.DB_PATH.parent / "backup"
KEEP = 14


def run(verbose: bool = True) -> dict:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"usage-{datetime.now(timezone.utc):%Y-%m-%d}.db"
    src = sqlite3.connect(db.DB_PATH)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    old = sorted(BACKUP_DIR.glob("usage-*.db"))[:-KEEP]
    for p in old:
        p.unlink()
    result = {"backup": str(dest), "pruned": len(old)}
    if verbose:
        print(result)
    return result


if __name__ == "__main__":
    run()
