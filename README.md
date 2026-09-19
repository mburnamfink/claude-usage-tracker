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

## Inspect data

```sh
sqlite3 data/usage.db 'SELECT captured_at, status, session_pct, weekly_pct FROM snapshots ORDER BY captured_at DESC LIMIT 10;'
```
