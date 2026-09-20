# Claude Code Usage Tracker

Records exact Claude subscription rate-limit usage over time by polling the
endpoint that backs `/usage`, so you can see how much of each session (5-hour)
and weekly window you consume. See [PLAN.md](PLAN.md) for the full design.

## How it works

`tracker/poll.py` reads the live OAuth token from `~/.claude/.credentials.json`,
calls `GET https://api.anthropic.com/api/oauth/usage`, and stores a snapshot in
`data/usage.db` (SQLite) plus a raw copy in `data/raw/`. A systemd user timer
runs it every 5 minutes. When the token is stale (Claude Code not running) it
records a `token_expired` row and exits cleanly.

## Run once manually

```sh
cd tracker && PYTHONPATH=. ~/work/bin/python poll.py
```

## Install the 5-minute timer

```sh
mkdir -p ~/.config/systemd/user
cp deploy/usage-tracker.service deploy/usage-tracker.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usage-tracker.timer
```

Check it:

```sh
systemctl --user list-timers usage-tracker.timer
journalctl --user -u usage-tracker.service -n 20
```

Optional — to keep the timer running when you are not logged in
(requires sudo, modifies system state):

```sh
sudo loginctl enable-linger $USER
```

## Outage alerts (ntfy.sh)

Because the endpoint is undocumented and could break, each poll runs a health
check and pushes a phone notification if *real* failures (HTTP errors, network
errors, or a silent schema change) persist past a threshold. Transient 429s and
`token_expired` (Claude Code not running) never alert.

Setup: copy `alert_config.example.json` to `alert_config.json` (gitignored),
pick a random `ntfy_topic`, then subscribe to that topic in the
[ntfy](https://ntfy.sh) app (or open `https://ntfy.sh/<topic>` in a browser).
Thresholds: `warn_minutes` (first alert, default 30), `realert_minutes` (re-alert
cadence, default 60), `stale_minutes` (stop alerting once idle, default 15).

```json
{ "ntfy_topic": "pick-a-random-topic", "warn_minutes": 30, "realert_minutes": 60, "stale_minutes": 15 }
```

The endpoint intermittently 429s (bursts against Claude Code's own calls to it);
`poll.py` retries with backoff, and isolated 429s recover on the next 5-min poll.

## Inspect data

```sh
sqlite3 data/usage.db 'SELECT captured_at, status, session_pct, weekly_pct FROM snapshots ORDER BY captured_at DESC LIMIT 10;'
```
