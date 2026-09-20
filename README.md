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

## Phase 2 — token & cost harvest

`tracker/tokens.py` walks the Claude Code transcripts
(`~/.claude/projects/**/*.jsonl`), dedups assistant turns, and stores per-turn
token usage in `usage.db` (`turns`) so the token history **outlives the ~30-day
transcript retention**. It also captures each session's `ai-title` label
(`session_titles`) and rolls tokens into observed session windows
(`window_tokens`). Cost is **derived at query time** from a versioned price book
(`prices`); categories come from an editable, ordered ruleset (`category_rules`,
first match wins — extend by inserting a row). The harvest is incremental:
unchanged files cost one `stat()`, a growing file is read only from where it left
off, and re-reading can never double-count (`message_id` UPSERT keep-max).

`tracker/refresh_prices.py` reconciles `prices` against the LiteLLM price map and
versions any changed rate (closes the open row, inserts a new one); a failed fetch
never changes anything.

```sh
cd tracker && ~/work/bin/python tokens.py            # harvest now
cd tracker && ~/work/bin/python refresh_prices.py --dry-run   # preview price changes
```

Install the timers (harvest hourly, prices monthly, log compaction weekly) plus
the shared failure notifier:

```sh
cp deploy/usage-harvest.* deploy/usage-prices.* deploy/usage-maintenance.* \
   deploy/usage-alert@.service  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now usage-harvest.timer usage-prices.timer usage-maintenance.timer
```

The harvest opens the DB in WAL mode so it can run while the 5-minute poller
writes; both are short, idempotent transactions.

## What can silently break — and what catches it

Three layers, all pushing to the same ntfy topic:

- **Endpoint outage** — the poller's own health check (sustained HTTP/schema/network
  failures). Original behavior.
- **A service crashes** (broken `~/work` venv, disk full, DB locked, an exception in
  any unit) — every unit has `OnFailure=usage-alert@…`, which fires a notification.
- **A timer silently stops or drifts** — the 5-minute poller is the watchdog for the
  hourly/monthly timers (`alert.check_pipeline`): it alerts if the harvest heartbeat
  goes stale (timer dead), if a harvest read new transcript bytes but parsed **zero**
  turns (Claude Code changed the transcript format), or if a model in `turns` has no
  price row (cost undercounting).

Residual gap: if the **poller itself** stops firing while you're logged in, nothing
external notices (its watchdog can't run). `enable-linger` keeps the timers running
when logged out; for a true dead-man's switch, add an external ping (e.g.
healthchecks.io) to the poller.

## Storage

The tool adds ~0.2 GB/year (journal + raw logs + DB); the DB and gzipped raw logs are
the durable parts. `usage-maintenance` gzips raw poll logs older than 7 days (~10×) and
drops them after 90 days — the parsed data is in `usage.db` regardless.

The **systemd journal is capped globally, not per-user** — set it once (needs sudo):

```sh
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' | sudo tee /etc/systemd/journald.conf.d/cap.conf
sudo systemctl restart systemd-journald          # or: journalctl --vacuum-size=500M
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
