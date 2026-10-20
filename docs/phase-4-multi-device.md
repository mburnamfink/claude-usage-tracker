# Phase 4 — Multi-device collection (design + implementation plan)

Goal: **piehole** (home server) is the single tracker — one `usage.db`, one dashboard,
all alerting. Every other machine that runs Claude Code (**edges**: `desktop`, `laptop`,
future machines) ships its data to piehole. Edges stay dumb (a shell script + two
timers); piehole does all parsing, storage and alerting.

## Why this is needed

| | Usage % (`/api/oauth/usage`) | Tokens (transcripts) |
|---|---|---|
| Scope | **Account-wide** — any device reads the same numbers | **Per device** — each machine has only its own `~/.claude/projects/**/*.jsonl` |
| Requires | A fresh OAuth access token (~1 h life, refreshed only while Claude Code runs *on that machine*) | Filesystem access |
| Nature | Point-in-time reading | Additive counter |

So: tokens must be collected from every device; the % can come from whichever device
currently has a fresh token. piehole runs Claude Code often and already polls (≈600 `ok`
vs 18 `token_expired` over 2 days), so edge polling only fills gaps.

Android: the Claude app has no transcripts; its usage appears only in the account-wide %
(`seven_day_breakdown` surface `chat`). Android is not an edge.

## Current state (as of 2026-10-19)

- **piehole**: Debian 13, x86_64 (old Lenovo laptop), 4 cores, 7.5 GB RAM, 120 GB SSD
  (96 GB free). Repo at `~/dev/claude-usage-tracker` (commit `775b80d`), `~/work` venv,
  linger on, Tailscale SSH on (`RunSSH: true`). Timers running: `usage-tracker`
  (5 min poll), `usage-harvest` (hourly), `usage-maintenance`, `usage-prices`.
  `rsync`/`curl`/`sqlite3` present, **no `jq`** (piehole doesn't need it).
- `usage.db` on piehole was copied from desktop: `turns` 8661 rows (2026-08-17 →);
  `harvest_files` holds ~666 desktop paths (`/home/michael/.claude/projects/...`) that
  don't exist on piehole (piehole's own corpus is 7 MB).
- **desktop**: usage timers already disabled. Transcripts 413 MB. jq/rsync 3.2.7/curl present.
- **laptop**: transcripts 1.4 MB. jq/rsync/curl present.
- Tailnet node names: `piehole`, `desktop`, `laptop`. `ssh -o BatchMode=yes piehole`
  works from desktop.
- No edge has `cleanupPeriodDays` set (Claude Code default: transcripts deleted after 30 days).

## Architecture

```
edge (desktop, laptop, …)                          piehole
──────────────────────────                         ────────────────────────────────────────
~/.local/bin/usage-edge.sh   (bash, curl, jq, rsync — no Python, no repo checkout)
 snap  (every 5 min, jittered):
   token valid? → curl usage endpoint
   → ~/.local/state/usage-edge/outbox/snap-<utc>.json
   → rsync --remove-source-files ──────────────→ data/inbox/<device>/outbox/
 sync  (every 15 min):
   rsync --append-verify ~/.claude/projects/ ──→ data/inbox/<device>/projects/
 both: on push failure, self-alert via ntfy        usage-ingest.timer (5 min):
                                                     outbox → snapshots (+device), raw log
                                                     tokens.harvest over all roots
                                                   usage-tracker.timer: poll.py as device "piehole"
                                                   dashboard, alerts, prices, backups
```

Every transfer is **at-least-once** and every write **idempotent**, so retries can
never double-count. The edge→server contract is only "files appear in a directory".

Design decisions (settled; don't re-litigate):
- **Raw transcripts are shipped, not parsed results.** No parser on edges → no version
  skew; piehole keeps raw history so a parser fix can re-harvest. Full conversation
  content living on piehole is accepted by the owner.
- **The OAuth token never leaves its device.** Edges call the endpoint themselves and
  ship the response.
- **Server never connects to edges** (laptops sleep). All traffic is edge-initiated.
- **Transport: ssh over Tailscale as user `michael`.** A dedicated restricted ingest user
  is a possible later hardening, out of scope here.
- **Device id = Tailscale node name**, captured once at install into the edge config
  (not read live, so a later rename doesn't silently start a new inbox). Tailscale
  enforces uniqueness; a reinstalled machine that becomes `laptop-1` just gets a new
  inbox and dedups on `message_id`.
- **No server-side raw-transcript pruning.** ~5 GB/yr at desktop's current rate vs 96 GB
  free. Add a disk-usage alert instead.

## Foot-guns and their mitigations

| Risk | Mitigation |
|---|---|
| Same transcript on two devices (copied `~/.claude` to a new machine) | `turns.message_id` PK + keep-max UPSERT already dedups globally |
| Re-harvest of desktop history under new inbox paths | Same dedup; verified by the token-total check in Rollout step 3 |
| `harvest_files` cursor collision between devices | Server paths include `inbox/<device>/`, so they're distinct |
| Duplicate snapshot upload (retry) | `UNIQUE(device, captured_at)` + `INSERT OR IGNORE` |
| **`export._credit_deltas` overcounts** with interleaved multi-device snapshots: out-of-order readings 10.0, 9.9, 10.0 count +0.1 spurious | Delta against **running max**, reset only on a large drop (monthly rollover) |
| piehole down | Edge outbox spools snapshots; transcripts stay on the edge as the buffer. Set `cleanupPeriodDays: 365` on every edge so the buffer is a year, not 30 days |
| Push silently broken (piehole down, tailscale logged out, ssh failing) and transcripts age out | Edge self-alerts via ntfy when it has unpushed changes and last success > 6 h. Idle machine = no changes = silence |
| piehole itself dead (its own alerting is dead too) | Optional `healthcheck_url` (healthchecks.io) pinged by every ingest run; external service alerts on silence |
| Tailscale SSH ACL in `check` mode needs periodic browser re-auth → unattended rsync fails | ACL rule for edge→piehole must be `"action": "accept"`. Verify per install |
| ssh prompt hangs a timer | `-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new` |
| harvester reads a file mid-rsync | `--append-verify` writes in place; harvester already consumes only up to the last `\n`. If verify fails rsync re-sends whole file → new inode → harvester re-reads from 0 (existing logic) |
| Outbox file read half-written | rsync writes to a dot-temp then renames; ingest globs `snap-*.json` (no dotfiles) |
| Late-arriving (spooled) snapshots trigger stale outage alerts | `check_health` already ignores failures older than `stale_minutes`; snapshots are ordered by `captured_at` not arrival |
| Laptop resumes from suspend with no network | Units `Persistent=true`; network failure → exit 0 and retry next run (self-alert covers persistence) |
| Endpoint 429s get worse with N pollers | `RandomizedDelaySec=60` on edge snap timer; edge polls only when its token is valid |
| SSD failure loses the single source of truth | Nightly `sqlite3 usage.db ".backup ..."` (never `cp` a WAL db); copy off-box |
| Edge clock skew | Window identity comes from server `resets_at`, not our clocks; store `received_at` alongside edge `captured_at` |

## Server changes (piehole, Python, stdlib only, `~/work/bin/python`)

1. **Config** (`alert_config.json` / `alert.DEFAULTS`): add `device_name` (default
   `socket.gethostname()`), `inbox_dir` (default `data/inbox`), `healthcheck_url`
   (default `null`), `disk_warn_pct` (default 85). Add `data/inbox/` to `.gitignore`.

2. **Schema migrations** (idempotent `ALTER TABLE … ADD COLUMN`, same pattern as
   `db._migrate`):
   - `snapshots`: `device TEXT`, `received_at TEXT`;
     `CREATE UNIQUE INDEX IF NOT EXISTS uq_snap_device_ts ON snapshots(device, captured_at)`.
   - `turns`: `device TEXT` (last sighting; display only, not part of the key). Set it
     in `tokens.UPSERT` (`device=excluded.device`).
   - New `devices(name TEXT PRIMARY KEY, first_seen TEXT, last_seen_at TEXT)` —
     newest activity observed (transcript mtime or snapshot `captured_at`).
   - One-time backfill: existing snapshots `device='legacy'`; existing turns
     `device='piehole'` if `source_file` exists on piehole else `'legacy'` (an edge's
     push re-sights those still on disk and overwrites it); delete `harvest_files`
     rows whose path doesn't exist locally (stale desktop cursors).

3. **`poll.py`**: write `device=<device_name>`, `received_at=captured_at`. Factor the
   status logic so ingest can reuse it: a function taking `(http_code, payload,
   neterr)` → row (the tail of current `classify`). Keep `check_health` /
   `check_pipeline` calls here.

4. **`tokens.harvest(roots)`**: accept `[(device, path), …]`. Default roots =
   `[(device_name, ~/.claude/projects)] + [(d.name, d/"projects") for d in inbox/*]`.
   Each turn row carries `device`. Everything else (stat-skip, offsets, inode/shrink
   detection) is unchanged. A missing root is skipped, not an error.

5. **New `tracker/ingest.py`** (one run = one pass, safe to re-run):
   - For each `outbox/snap-*.json` sorted by name: parse `{captured_at, http_code,
     neterr, response}` (format below) → status row via the poll.py function →
     `INSERT OR IGNORE` with `device`, `received_at=now`; append
     `{captured_at, device, response}` to `data/raw/<day>.jsonl` when a body exists;
     **delete the file only after the DB commit**. Malformed file → move to
     `inbox/<device>/rejected/`, log, continue.
   - Run `tokens.harvest(default roots)` (writes the existing heartbeat in `harvest_meta`).
   - Ping `healthcheck_url` if set (failure to ping never fails the run).
   - Units: `deploy/usage-ingest.service` + `.timer` (`OnCalendar=*:0/5`, `Persistent=true`,
     `OnFailure=usage-alert@%N.service`). **Retire `usage-harvest.*`** (ingest replaces
     it; heartbeat threshold `HARVEST_STALE_H=3` still applies).

6. **`alert.check_pipeline`**: add `disk_high` issue when the filesystem holding
   `data/` exceeds `disk_warn_pct` (`shutil.disk_usage`).

7. **`export._credit_deltas`**: running-max delta as described above. Pick the rollover
   threshold so a monthly reset (counter drops to ~0) resets the max but a small
   out-of-order dip does not, e.g. drop > 50% of the running max.

8. **`export.build`**: add `devices: [{name, last_seen_at, tokens_7d}]`; render it in
   the dashboard footnote (name · last seen · tokens/7d).

9. **Backup**: `deploy/usage-backup.service` + daily timer running
   `sqlite3 data/usage.db ".backup data/backup/usage-$(date +%F).db"`, keep 14. Copying
   off-box (e.g. to desktop) is a follow-up the owner decides.

## Edge (new `edge/` directory in this repo)

`edge/usage-edge.sh` (bash, `set -euo pipefail`), config at
`~/.config/usage-edge/env`:

```sh
DEVICE=desktop                    # tailscale node name, fixed at install
SERVER=piehole
REMOTE_INBOX=dev/claude-usage-tracker/data/inbox   # relative to remote $HOME
NTFY_TOPIC=…                      # same topic as server alert_config.json
STALE_HOURS=6
```

State dir `~/.local/state/usage-edge/` (`outbox/`, `last_push_ok`, `last_alert`).
SSH opts for every call: `-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new`.

- **`usage-edge.sh snap`**
  1. Read `~/.claude/.credentials.json` with jq: `.claudeAiOauth.accessToken`,
     `.claudeAiOauth.expiresAt` (ms). If missing or expiring within 60 s → skip poll
     (no file; piehole/other devices cover it).
  2. `curl -sS --max-time 20 -w '%{http_code}' -H "Authorization: Bearer …"
     -H "anthropic-beta: oauth-2025-04-20" https://api.anthropic.com/api/oauth/usage`.
     Keep the token out of argv (visible in `ps`): feed the auth header via
     `--config -` on stdin.
  3. Write outbox file atomically (tmp in same dir + `mv`), name
     `snap-<YYYYmmddTHHMMSSZ>.json`:
     `{"captured_at": "<ISO UTC>", "http_code": 200|null, "neterr": null|"<curl error>", "response": {…}|null}`.
  4. Push: `rsync -a --mkpath --remove-source-files outbox/ $SERVER:$REMOTE_INBOX/$DEVICE/outbox/`.
- **`usage-edge.sh sync`**:
  `rsync -a --mkpath --append-verify --prune-empty-dirs --include='*/' --include='*.jsonl' --exclude='*' ~/.claude/projects/ $SERVER:$REMOTE_INBOX/$DEVICE/projects/`
  (no `--delete`: piehole keeps history after edge cleanup; transcripts include
  `<session>/subagents/*.jsonl`, hence the `*/` include).
- **Both**: on push success `touch last_push_ok`. On failure: if anything under
  `~/.claude/projects` or `outbox/` is newer than `last_push_ok` and `last_push_ok` is
  older than `STALE_HOURS`, POST to `https://ntfy.sh/$NTFY_TOPIC` (title
  "Usage edge <device>: can't reach <server>"), at most once per `STALE_HOURS`
  (`last_alert`). Then **exit 0** so systemd's failure path isn't the alert path.
- Units: `edge/usage-edge-snap.{service,timer}` (`*:0/5`, `RandomizedDelaySec=60`) and
  `edge/usage-edge-sync.{service,timer}` (`*:0/15`), `Persistent=true`, user units.
- **`edge/install.sh`** (idempotent): check `jq rsync curl tailscale` exist; derive
  `DEVICE` from `tailscale status --json | jq -r .Self.HostName` unless config exists;
  write config (prompt for ntfy topic or copy from arg); install script to
  `~/.local/bin`; install + enable units; set `cleanupPeriodDays: 365` in
  `~/.claude/settings.json` via jq merge (back up the file first; preserve other keys);
  run `ssh $SERVER true` in BatchMode and fail loudly if it prompts/fails; run one
  `sync` + `snap`.

## Rollout

Status 2026-10-19: step 1 done (server code + `tests/test_multi_device.py`).

1. **Server code** on desktop checkout; verify against a temp DB (override
   `db.DB_PATH`) with these acceptance checks:
   - ingesting the same inbox twice → identical row counts and token sums;
   - same transcript under two device dirs → counted once;
   - trailing partial line ignored, picked up after it completes;
   - two devices' snapshots with clocks 30 s apart → credit delta equals true delta;
   - malformed outbox file → `rejected/`, run continues.
2. **Deploy to piehole** (pull the commit there), run migrations, install
   `usage-ingest` + backup units, disable/remove `usage-harvest`.
3. **Desktop as first edge.** Before first sync, record on piehole
   `SELECT substr(ts,1,10), SUM(input_tokens+output_tokens) FROM turns GROUP BY 1`.
   Run `install.sh`. First sync is 413 MB, possibly via a DERP relay (slow; resumable).
   After ingest, the per-day sums must be **unchanged** for days already harvested
   (newer days may grow). Any increase = double-count bug; stop.
4. **Outage drill**: `sudo tailscale down` on desktop (or stop ssh on piehole) for > 6 h
   of simulated staleness (lower `STALE_HOURS` temporarily), confirm the edge ntfy
   alert, restore, confirm the outbox drains and no duplicate snapshots.
5. **Laptop**: `install.sh`; check its device row appears on the dashboard.
6. **Tailscale ACL**: confirm the SSH rule covering edges→piehole is `accept`, not `check`.
7. Update README: server install (ingest/backup units), edge install, new alert keys,
   and remove the single-machine harvest instructions.
