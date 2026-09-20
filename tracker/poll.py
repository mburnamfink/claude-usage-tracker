#!/usr/bin/env python3
"""Poll the Claude usage endpoint once and store a snapshot.

Reads the live OAuth access token that Claude Code maintains in
~/.claude/.credentials.json. The token expires ~hourly and is refreshed only
while Claude Code runs, so when it is stale we record a 'token_expired' row and
exit cleanly rather than failing (see PLAN.md, "Authentication strategy").
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import alert
import db

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
TIMEOUT = 20

# The endpoint intermittently 429s (bursts against Claude Code's own calls);
# a single retry a bit later almost always succeeds. Env-overridable for tests.
RETRY_CODES = {429, 500, 502, 503, 504}
RETRIES = int(os.environ.get("USAGE_POLL_RETRIES", "2"))
BACKOFF = [float(x) for x in os.environ.get("USAGE_POLL_BACKOFF", "30,60").split(",")]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_token() -> tuple[str | None, bool]:
    """Return (access_token, expired). token is None if creds unreadable."""
    try:
        creds = json.loads(CREDENTIALS_PATH.read_text())["claudeAiOauth"]
    except (OSError, KeyError, ValueError):
        return None, True
    expired = creds.get("expiresAt", 0) / 1000 < datetime.now(timezone.utc).timestamp()
    return creds.get("accessToken"), expired


def fetch_usage(token: str) -> tuple[int | None, dict | None, str | None]:
    """Return (http_code, payload, network_error). Retries transient failures."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read()), None
        except urllib.error.HTTPError as e:
            if e.code in RETRY_CODES and attempt < RETRIES:
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                attempt += 1
                continue
            return e.code, None, None
        except urllib.error.URLError as e:
            if attempt < RETRIES:
                time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
                attempt += 1
                continue
            return None, None, str(e.reason)


def parse(payload: dict) -> dict:
    def util(key):
        node = payload.get(key)
        return node.get("utilization") if isinstance(node, dict) else None

    def resets(key):
        node = payload.get(key)
        return node.get("resets_at") if isinstance(node, dict) else None

    extra = payload.get("extra_usage") or {}
    breakdown = payload.get("seven_day_breakdown")
    cc_share = None
    for r in (breakdown or {}).get("rows", []):
        if r.get("key") == "claude_code":
            cc_share = r.get("percent")

    return {
        "session_pct": util("five_hour"),
        "session_resets_at": resets("five_hour"),
        "weekly_pct": util("seven_day"),
        "weekly_resets_at": resets("seven_day"),
        "weekly_opus_pct": util("seven_day_opus"),
        "extra_usage_pct": extra.get("utilization"),
        "extra_used_credits": extra.get("used_credits"),
        "cc_weekly_share": cc_share,
        "weekly_breakdown_json": json.dumps(breakdown) if breakdown else None,
    }


def append_raw(payload: dict, captured_at: str) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    day = captured_at[:10]
    line = json.dumps({"captured_at": captured_at, "response": payload})
    with open(RAW_DIR / f"{day}.jsonl", "a") as f:
        f.write(line + "\n")


def classify(token, expired) -> tuple[dict, dict | None]:
    """Return (row, payload). payload is kept only to append raw on success."""
    row = {"captured_at": now_iso(), "http_code": None}
    if not token or expired:
        row["status"] = "token_expired"
        return row, None

    code, payload, neterr = fetch_usage(token)
    row["http_code"] = code
    if neterr is not None:
        row["status"] = "network_error"
    elif code == 200 and payload is not None:
        row.update(parse(payload))
        # 200 but the percentage fields vanished => the undocumented schema changed
        if row["session_pct"] is None or row["weekly_pct"] is None:
            row["status"] = "schema_error"
        else:
            row["status"] = "ok"
    elif code == 401:
        row["status"] = "token_expired"
    else:
        row["status"] = "http_error"
    return row, payload


def main() -> int:
    token, expired = load_token()
    row, payload = classify(token, expired)

    # keep the raw body whenever we got one back — invaluable if the schema shifts
    if row["status"] in ("ok", "schema_error") and payload is not None:
        append_raw(payload, row["captured_at"])

    with db.connect() as conn:
        db.insert_snapshot(conn, row)
        try:
            action = alert.check_health(conn)
        except Exception as e:  # an alert failure must never break capture
            action = {"action": "error", "error": str(e)}

    if row["status"] == "ok":
        print(f"{row['captured_at']} ok — session {row['session_pct']}% "
              f"weekly {row['weekly_pct']}%")
    else:
        print(f"{row['captured_at']} {row['status']} — HTTP {row['http_code']}")
    if action.get("action") not in (None, "none"):
        print(f"  alert: {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
