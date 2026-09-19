# Claude Code Usage Tracker — Plan

## Goal

Record my exact Claude subscription rate-limit usage over time so I can see, for any moment:
- **% of the current session window** (the rolling 5-hour limit) consumed
- **% of the current weekly window** consumed
- how those evolve within and across windows

These percentages are the *official server-side* figures (identical to what `/usage` shows), not estimates. The token counts in local transcripts are a separate, optional enrichment (Phase 2).

## Key discovery — where the numbers come from

The `/usage` screen is backed by a single authenticated endpoint:

```
GET https://api.anthropic.com/api/oauth/usage
  Authorization: Bearer <access_token>
  anthropic-beta: oauth-2025-04-20
```

Returns HTTP 200 with JSON. The fields we care about (see `docs/sample-usage-response.json` for a full real capture):

| Field | Meaning |
|---|---|
| `five_hour.utilization` | session-window %, e.g. `29.0` |
| `five_hour.resets_at` | when this session window resets (ISO, UTC) |
| `seven_day.utilization` | weekly-window %, e.g. `28.0` |
| `seven_day.resets_at` | when the weekly window resets |
| `seven_day_opus.utilization` | Opus-specific weekly % (null on Pro plan) |
| `extra_usage.utilization` / `used_credits` | pay-as-you-go credit spend after limits |
| `limits[]` | normalized list: `{kind, group, percent, severity, resets_at, is_active}` |
| `seven_day_breakdown.rows[]` | share of weekly usage per surface (Claude Code vs Chats vs …) |

**Window identity:** two readings belong to the *same* session window iff they share the same `five_hour.resets_at`; likewise weekly via `seven_day.resets_at`. This is how we group readings to compute peak/final utilization per window — the "how much of the window I used" number.

There is no historical feed — the endpoint only reports *now*. So the tool works by **polling and storing snapshots going forward**. No retrospective percentages exist.

## Authentication strategy (v1: piggyback)

The access token lives in `~/.claude/.credentials.json` under `claudeAiOauth.accessToken` and **expires roughly hourly**. Claude Code refreshes that file automatically whenever it is running.

v1 reads the live token from that file on each poll:
- token valid → poll succeeds, row stored.
- token expired (HTTP 401) → store a `token_expired` status row and move on; no crash.

This means captures land reliably during active Claude Code use (exactly when the session window is moving) and may have gaps during long idle stretches when Claude Code is closed. That is acceptable: the session window is only interesting while active, and the weekly window barely moves while idle.

**Deliberately NOT doing in v1:** an independent `refresh_token` grant. It would close idle gaps but risks racing with / invalidating Claude Code's own credential refresh, and depends on an undocumented client_id. Documented here as a future option only.

## Architecture

```
tracker/
  poll.py       # read creds -> GET usage -> parse -> write SQLite + raw JSONL. Idempotent.
  db.py         # schema init + insert helpers (sqlite3, stdlib)
  analyze.py    # (Phase 2) queries/views: per-window peak, trends
  report.py     # (Phase 3) render an HTML dashboard from the DB
data/
  usage.db      # SQLite store (gitignored)
  raw/*.jsonl   # append-only raw responses, one per poll (gitignored, audit/reprocess)
docs/
  sample-usage-response.json   # real response, schema reference
deploy/
  usage-tracker.service / .timer   # systemd user units (Phase 1)
```

- **Runtime:** Python via `~/work/bin/python` (per environment rule). **Stdlib only** (`urllib`, `sqlite3`, `json`, `datetime`) so there is nothing to `uv pip install`.
- **Scheduler:** a **systemd user timer** firing every **5 minutes**. Rationale: the session % changes fast during active use, so 5-min sampling catches peaks; 288 polls/day is negligible; the endpoint is metadata and does **not** consume model quota. `cron` is a drop-in alternative.

### SQLite schema (one row per poll attempt)

```sql
CREATE TABLE IF NOT EXISTS snapshots (
  captured_at        TEXT NOT NULL,   -- our UTC clock, ISO8601
  status             TEXT NOT NULL,   -- 'ok' | 'token_expired' | 'http_error'
  session_pct        REAL,            -- five_hour.utilization
  session_resets_at  TEXT,            -- five_hour.resets_at  (== session window id)
  weekly_pct         REAL,            -- seven_day.utilization
  weekly_resets_at   TEXT,            -- seven_day.resets_at  (== weekly window id)
  weekly_opus_pct    REAL,            -- seven_day_opus.utilization (nullable)
  extra_usage_pct    REAL,            -- extra_usage.utilization (nullable)
  extra_used_credits REAL,            -- extra_usage.used_credits (nullable)
  cc_weekly_share    REAL,            -- seven_day_breakdown row where key='claude_code'
  http_code          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_session_window ON snapshots(session_resets_at);
CREATE INDEX IF NOT EXISTS idx_weekly_window  ON snapshots(weekly_resets_at);
```

Full raw JSON is also appended to `data/raw/YYYY-MM-DD.jsonl` so we can re-derive anything later without re-polling.

## Phases

**Phase 0 — Discovery (DONE).** Endpoint confirmed live (HTTP 200), repo scaffolded, real response saved as fixture.

**Phase 1 — Capture pipeline (build first).**
1. `db.py` — init schema.
2. `poll.py` — read creds, GET, parse fields above, insert row + append raw. Graceful on expired token / non-200.
3. `deploy/` systemd user `.service` + `.timer` (every 5 min) and install notes in README.
4. Verify: run `poll.py` manually several times over a work session; confirm rows accumulate and window ids group correctly.

**Phase 2 — Analysis.**
- SQL views / `analyze.py`:
  - per **session window** (`GROUP BY session_resets_at`): peak %, final %, first/last seen, duration covered.
  - per **weekly window**: % trajectory and end-of-week peak.
  - detect windows that hit high severity / approached 100%.
- Optional enrichment: join token totals from `~/.claude/projects/**/*.jsonl` (per-turn `usage`, model, timestamp) bucketed into the same 5-hour windows, to answer the original "how many tokens is that" alongside the percentage. Cheap; adds the token dimension back.

**Phase 3 — Display.**
- `report.py` generates a self-contained HTML dashboard from the DB: session-% within-window sparklines, weekly-% trend line, distribution of per-window peak utilization, and current status. Can be published as an Artifact for at-a-glance viewing.

## Open decisions (defaults chosen; change if you disagree)

| Decision | Default | Alternative |
|---|---|---|
| Auth | piggyback on live creds file | self-refreshing token (more coverage, more risk) |
| Scheduler | systemd user timer, 5-min | cron; or hook that fires on Claude Code activity |
| Poll cadence | every 5 min | 1 min (finer peaks) / 15 min (leaner) |
| Token-count join (Phase 2) | include it | percentages only |
| Storage | SQLite + raw JSONL | JSONL only |
