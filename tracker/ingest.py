"""Ingest what edge devices pushed, then harvest every device's transcripts. Stdlib only.

Edges rsync into `<inbox>/<device>/`:
  outbox/snap-<utc>.json   one usage-endpoint call made on the edge (its token never
                           leaves it): {"captured_at", "http_code", "neterr", "response"}
  projects/**.jsonl        an append-only mirror of the edge's ~/.claude/projects

Delivery is at-least-once (an edge retries until rsync succeeds), so every write here is
idempotent: snapshots are keyed (device, captured_at) and turns by message_id. An outbox
file is deleted only after its row is committed; a crash in between just re-ingests it
as a no-op. rsync writes to a dot-prefixed temp name and renames, so the snap-*.json glob
never sees a half-written file.

Run by usage-ingest.timer every 5 minutes.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import alert
import db
import poll
import tokens


def _norm_ts(s: str) -> str:
    """Edge clock ISO8601 -> the same UTC isoformat poll.py writes (ordering + dedup key)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"captured_at without timezone: {s!r}")
    return dt.astimezone(timezone.utc).isoformat()


def _load(path: Path) -> tuple[dict, dict | None]:
    o = json.loads(path.read_text())
    captured_at = _norm_ts(o["captured_at"])
    code = o.get("http_code")
    code = int(code) if code not in (None, "", 0, "0", "000") else None
    payload = o.get("response")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("response is not an object")
    row = poll.status_row(code, payload, o.get("neterr"))
    row["captured_at"] = captured_at
    return row, payload


def ingest_snapshots(conn, inbox: Path) -> dict:
    stored = duplicate = rejected = 0
    now = datetime.now(timezone.utc).isoformat()
    devices = sorted(d for d in inbox.iterdir() if d.is_dir()) if inbox.is_dir() else []
    for dev in devices:
        for f in sorted((dev / "outbox").glob("snap-*.json")):
            try:
                row, payload = _load(f)
            except (OSError, ValueError, KeyError, TypeError) as e:
                # keep it for inspection, out of the glob so it can't wedge every run
                (dev / "rejected").mkdir(exist_ok=True)
                f.rename(dev / "rejected" / f.name)
                print(f"rejected {f}: {e}", file=sys.stderr)
                rejected += 1
                continue
            row["device"] = dev.name
            row["received_at"] = now
            if db.insert_snapshot(conn, row):
                stored += 1
                if payload is not None and row["status"] in ("ok", "schema_error"):
                    poll.append_raw(payload, row["captured_at"], dev.name)
            else:
                duplicate += 1
            f.unlink()
    return {"snapshots_stored": stored, "snapshots_duplicate": duplicate,
            "snapshots_rejected": rejected}


def _ping(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=10).close()
    except Exception as e:  # a dead-man's-switch outage must never fail ingest
        print(f"healthcheck ping failed: {e}", file=sys.stderr)


def run(inbox: Path | None = None, roots=None, verbose: bool = False) -> dict:
    inbox = alert.inbox_dir() if inbox is None else inbox
    conn = tokens._connect()
    try:
        result = ingest_snapshots(conn, inbox)
    finally:
        conn.close()
    result.update(tokens.harvest(tokens.default_roots(inbox) if roots is None else roots))
    url = alert._config().get("healthcheck_url")
    if url:
        _ping(url)
    if verbose:
        print(result)
    return result


if __name__ == "__main__":
    run(verbose=True)
