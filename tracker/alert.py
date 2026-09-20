"""Detect a sustained usage-endpoint outage and push a notification via ntfy.sh.

Called at the end of each poll. Alerts only on *real* failures
(http_error / schema_error / network_error) sustained past a threshold — never
on token_expired, which just means Claude Code isn't running and we have no
valid token to poll with (i.e. "not monitoring", not "broken").
"""
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "alert_config.json"
STATE_PATH = ROOT / "data" / "alert_state.json"

FAIL_STATUSES = {"http_error", "schema_error", "network_error"}
DEFAULTS = {"warn_minutes": 30, "realert_minutes": 60, "stale_minutes": 15}


def _config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def _send_ntfy(topic: str, title: str, message: str, priority: str, tags: str) -> None:
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode(),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def check_health(conn, now: datetime | None = None) -> dict:
    """Evaluate recent poll outcomes; (re)alert or recover as needed.

    Returns a dict describing the action taken (for logging / testing).
    """
    cfg = _config()
    topic = cfg.get("ntfy_topic")
    now = now or datetime.now(timezone.utc)

    cutoff = now.timestamp() - 6 * 3600
    rows = []
    for t, s, c in conn.execute(
        "SELECT captured_at, status, http_code FROM snapshots ORDER BY captured_at DESC LIMIT 200"
    ):
        dt = _parse(t)
        if dt.timestamp() < cutoff:
            break
        rows.append((dt, s, c))
    rows.reverse()

    state = _load_state()

    ok_times = [t for t, s, _ in rows if s == "ok"]
    last_ok = max(ok_times) if ok_times else None
    real_fails = [
        (t, c) for t, s, c in rows
        if s in FAIL_STATUSES and (last_ok is None or t > last_ok)
    ]

    # healthy (no failures since the last success)
    if not real_fails:
        if state.get("active") and topic:
            _send_ntfy(topic, "Usage tracker recovered",
                       "Endpoint responding again.", "default", "white_check_mark")
        if state:
            _save_state({})
        return {"action": "recover" if state.get("active") else "none"}

    most_recent = max(t for t, _ in real_fails)
    outage_start = min(t for t, _ in real_fails)
    stale_min = (now - most_recent).total_seconds() / 60
    if stale_min > cfg["stale_minutes"]:
        return {"action": "none", "reason": "stale (idle after failure)"}

    dur_min = (now - outage_start).total_seconds() / 60
    if dur_min < cfg["warn_minutes"]:
        return {"action": "none", "reason": f"below threshold ({dur_min:.0f}m)"}

    last_code = real_fails[-1][1]
    msg = (f"/api/oauth/usage failing for {dur_min:.0f} min "
           f"({len(real_fails)} polls, last HTTP {last_code}).")

    if not state.get("active"):
        if topic:
            _send_ntfy(topic, "Usage tracker: endpoint down", msg, "high", "warning")
        _save_state({"active": True, "outage_start": outage_start.isoformat(),
                     "last_alert_at": now.isoformat()})
        return {"action": "alert", "outage_min": round(dur_min)}

    last_alert = _parse(state["last_alert_at"])
    if (now - last_alert).total_seconds() / 60 >= cfg["realert_minutes"]:
        if topic:
            _send_ntfy(topic, "Usage tracker: still down", msg, "high", "warning")
        state["last_alert_at"] = now.isoformat()
        _save_state(state)
        return {"action": "escalate", "outage_min": round(dur_min)}

    return {"action": "none", "reason": "already alerted"}
