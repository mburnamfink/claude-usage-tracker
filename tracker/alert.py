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
DEFAULTS = {"warn_minutes": 30, "realert_minutes": 60, "stale_minutes": 15,
            # null disables the dashboard liveness check
            "dashboard_health_url": "http://127.0.0.1:8787/api/health"}


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


# ---- pipeline health: the poller watches the two hourly/monthly timers ----
# The poller runs every 5 min and already has ntfy plumbing, so it is the natural
# watchdog for the harvest + price timers, which have no alerting of their own.
# Breadcrumbs live in harvest_meta (written by tokens.harvest / refresh_prices).

PIPELINE_STATE = ROOT / "data" / "pipeline_state.json"
# thresholds in hours; realert once per HARVEST_STALE window to avoid spam
HARVEST_STALE_H = 3          # hourly timer: 3 missed runs = something's wrong
PRICE_STALE_H = 24 * 40      # monthly timer: ~40 days
CANARY_MIN_BYTES = 50_000    # a substantial append that yields nothing = format drift


def _load_pipeline() -> dict:
    try:
        return json.loads(PIPELINE_STATE.read_text())
    except (OSError, ValueError):
        return {}


def _dashboard_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _meta(conn) -> dict:
    return {k: v for k, v in conn.execute("SELECT k, v FROM harvest_meta")}


def check_pipeline(conn, now: datetime | None = None) -> dict:
    """Detect the silent failures OnFailure can't: a timer that stopped firing, a
    harvest that recognizes nothing (transcript format drift), an unpriced model,
    or a dashboard that isn't serving.
    No-op (and never raises) if the Phase-2 tables aren't present yet."""
    now = now or datetime.now(timezone.utc)
    cfg = _config()
    topic = cfg.get("ntfy_topic")
    try:
        meta = _meta(conn)
    except Exception:
        return {"action": "none", "reason": "no harvest_meta yet"}

    issues = []  # (key, title, message)

    lr = meta.get("last_run")
    if lr:
        age_h = (now - _parse(lr)).total_seconds() / 3600
        if age_h > HARVEST_STALE_H:
            issues.append(("harvest_stale", "Token harvest stalled",
                           f"No harvest run in {age_h:.0f}h (timer dead?)."))
        elif (int(meta.get("last_appended_bytes", "0")) > CANARY_MIN_BYTES
              and int(meta.get("last_turns_written", "0")) == 0
              and int(meta.get("last_titles", "0")) == 0):
            issues.append(("harvest_canary", "Harvest recognizes nothing",
                           "Read new transcript bytes but parsed 0 turns — format drift?"))

    pr = meta.get("last_price_refresh")
    if pr and (now - _parse(pr)).total_seconds() / 3600 > PRICE_STALE_H:
        issues.append(("price_stale", "Price refresh stalled",
                       "No price reconcile in >40 days (timer dead?)."))

    try:
        priced = {r[0] for r in conn.execute("SELECT DISTINCT model FROM prices")}
        used = {r[0] for r in conn.execute("SELECT DISTINCT model FROM turns")}
        gaps = sorted(used - priced - {"<synthetic>"})
    except Exception:
        gaps = []
    if gaps:
        issues.append(("price_coverage", "Unpriced model(s)",
                       f"Cost undercounts: {', '.join(gaps)}"))

    # OnFailure only fires on a crash; a dashboard unit that was stopped or never
    # came back sits "inactive" with nothing to alert on.
    dash_url = cfg.get("dashboard_health_url")
    if dash_url and not _dashboard_up(dash_url):
        issues.append(("dashboard_down", "Dashboard down",
                       f"{dash_url} not responding — `systemctl --user status usage-dashboard`."))

    state = _load_pipeline()
    fired = []
    for key, title, message in issues:
        last = state.get(key)
        due = last is None or (now - _parse(last)).total_seconds() / 60 >= cfg["realert_minutes"]
        if due and topic:
            _send_ntfy(topic, f"Usage tracker: {title}", message, "high", "warning")
            state[key] = now.isoformat()
            fired.append(key)
    # clear resolved issues so they can alert again if they recur
    for key in list(state):
        if key not in {k for k, _, _ in issues}:
            state.pop(key)
    PIPELINE_STATE.parent.mkdir(parents=True, exist_ok=True)
    PIPELINE_STATE.write_text(json.dumps(state))
    return {"action": "alert" if fired else "none",
            "issues": [k for k, _, _ in issues], "fired": fired}


def notify_failure(unit: str) -> None:
    """OnFailure= handler: a service (poller/harvest/prices) exited non-zero."""
    cfg = _config()
    topic = cfg.get("ntfy_topic")
    if topic:
        _send_ntfy(topic, "Usage tracker: service failed",
                   f"{unit} exited with failure — check `journalctl --user -u {unit}`.",
                   "high", "rotating_light")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--failed":
        notify_failure(sys.argv[2])
