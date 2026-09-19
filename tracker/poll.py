#!/usr/bin/env python3
"""Poll the Claude usage endpoint once and store a snapshot.

Reads the live OAuth access token that Claude Code maintains in
~/.claude/.credentials.json. The token expires ~hourly and is refreshed only
while Claude Code runs, so when it is stale we record a 'token_expired' row and
exit cleanly rather than failing (see PLAN.md, "Authentication strategy").
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import db

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
TIMEOUT = 20


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


def fetch_usage(token: str) -> tuple[int, dict | None]:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def parse(payload: dict) -> dict:
    def util(key):
        node = payload.get(key)
        return node.get("utilization") if isinstance(node, dict) else None

    def resets(key):
        node = payload.get(key)
        return node.get("resets_at") if isinstance(node, dict) else None

    extra = payload.get("extra_usage") or {}
    cc_share = None
    breakdown = payload.get("seven_day_breakdown") or {}
    for r in breakdown.get("rows", []):
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
    }


def append_raw(payload: dict, captured_at: str) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    day = captured_at[:10]
    line = json.dumps({"captured_at": captured_at, "response": payload})
    with open(RAW_DIR / f"{day}.jsonl", "a") as f:
        f.write(line + "\n")


def main() -> int:
    captured_at = now_iso()
    row = {"captured_at": captured_at, "http_code": None}

    token, expired = load_token()
    if not token or expired:
        row["status"] = "token_expired"
        with db.connect() as conn:
            db.insert_snapshot(conn, row)
        print(f"{captured_at} token_expired — skipped")
        return 0

    code, payload = fetch_usage(token)
    row["http_code"] = code
    if code == 200 and payload is not None:
        row["status"] = "ok"
        row.update(parse(payload))
        append_raw(payload, captured_at)
        with db.connect() as conn:
            db.insert_snapshot(conn, row)
        print(
            f"{captured_at} ok — session {row['session_pct']}% "
            f"weekly {row['weekly_pct']}%"
        )
    else:
        row["status"] = "token_expired" if code == 401 else "http_error"
        with db.connect() as conn:
            db.insert_snapshot(conn, row)
        print(f"{captured_at} {row['status']} — HTTP {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
