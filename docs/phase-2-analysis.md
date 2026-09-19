# Phase 2 — Analysis Layer (Plan)

Turns the raw snapshot stream (Phase 1) into per-window answers to the original
question: **for each session (5-hour) window — what % of the session window did
I use, what % of the weekly window did it consume, and how many tokens was
that.** This phase produces the analysis functions + SQL views + a CLI summary;
the visual dashboard is Phase 3 and consumes what this phase exposes.

## Inputs

1. **`data/usage.db` → `snapshots`** (Phase 1): one row per ~5-min poll with
   `captured_at`, `status`, `session_pct`, `session_resets_at`, `weekly_pct`,
   `weekly_resets_at`, `weekly_opus_pct`, `extra_usage_pct`, `cc_weekly_share`.
   Only `status='ok'` rows carry percentages.
2. **`~/.claude/projects/**/*.jsonl`** (local transcripts): per-assistant-turn
   token usage, for the token join. ~30-day retention.

## Grounded data facts (verified against captured data — do not re-derive)

1. **`resets_at` needs normalization.** It is stable to the *second* but the
   *microseconds jitter* between polls of the same window
   (`20:30:00.440842` vs `20:30:00.808354`). Grouping on the raw string shatters
   one window into many. **Window key = `substr(resets_at, 1, 16)`** i.e.
   truncate to the minute (`YYYY-MM-DDTHH:MM`). Do this in SQL.
2. **Transcript turns must be de-duplicated.** A single assistant turn writes
   multiple JSONL lines sharing one `message.id` (observed: 70 lines → 28 unique
   ids). Summing raw lines overcounts ~2.5×. **Dedup by `message.id`; keep the
   line with the max `output_tokens`** (the final/cumulative one).
   `requestId` is an equivalent key if `message.id` is ever absent.
3. **Percentages are not a linear function of tokens** (Anthropic weights by
   model). Report both side by side; never derive one from the other.
4. Session window = 5h, `resets_at` is the window *end*, so
   `window_start = end − 5h`. Windows are activity-anchored (a window opens on
   first activity, not on a fixed grid), so derive boundaries from observed
   `resets_at`, not by extrapolating a grid.
5. Weekly window resets at a fixed wall-clock instant (`07:00 UTC` = local
   midnight), 7-day fixed bucket. **Assumption to verify (below):** `weekly_pct`
   is monotonic non-decreasing within one weekly key until it resets. If true,
   a session's weekly *contribution* = weekly_pct(end) − weekly_pct(start).
6. Full three-way comparison (session% / weekly% / tokens) only exists where
   snapshot coverage and transcript retention overlap — i.e. from tracker start
   onward. Earlier windows may have tokens but no percentages.

## Design

### Window-key normalization
Add derived keys in a view rather than storing them:
`session_key = substr(session_resets_at,1,16)`, `weekly_key = substr(weekly_resets_at,1,16)`.

### `v_session_windows` (GROUP BY session_key over status='ok')
Per session window:
- `session_key`, `window_end` (= max `session_resets_at`), `window_start` (= end − 5h)
- `peak_session_pct` = MAX(session_pct)
- `final_session_pct` = session_pct of the last sample (by captured_at)
- `first_seen`, `last_seen` (min/max captured_at), `n_samples`
- `weekly_at_start`, `weekly_at_end` (weekly_pct at first/last sample)
- `weekly_contribution` = weekly_at_end − weekly_at_start
- `partial` flag = TRUE when `first_seen − window_start` exceeds ~10 min OR the
  first observed session_pct is already well above 0 (window observed late →
  peak/contribution understated; mark it, don't hide it)

### `v_weekly_windows` (GROUP BY weekly_key over status='ok')
`peak_weekly_pct`, `final_weekly_pct`, `first_seen`, `last_seen`,
`avg_cc_weekly_share`, and count of distinct session_keys within the week.

### `tracker/tokens.py` — transcript token join
- Walk all `~/.claude/projects/**/*.jsonl`; keep `type=='assistant'` lines with
  `message.usage`.
- **Dedup** by `message.id` (fact 2), keeping max `output_tokens`.
- Per kept turn extract: `timestamp`, `model`, and usage components
  `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `thinking_tokens`. Include sidechain/sub-agent
  turns — they count toward the real limit.
- **Bucket into observed session windows:** a turn at time `t` belongs to
  window `W` iff `W.window_start ≤ t < W.window_end` (windows come from
  `v_session_windows`). Turns matching no observed window → `unassigned`
  (idle gaps or pre-tracking); summarized separately, not forced into a window.
- Output per session window: token totals + per-component + per-model breakdown.

### `tracker/analyze.py` — functions + CLI
- Functions returning plain dicts/lists (Phase 3 imports these):
  `session_windows()` (v_session_windows joined with token totals),
  `weekly_windows()`, `data_quality()`.
- CLI (`~/work/bin/python analyze.py`) prints:
  - recent session windows: `start(local) | dur | peak% | final% | weekly Δ% | tokens | top model`
  - a weekly summary line per weekly key
  - a data-quality footer.

### `data_quality()`
- capture coverage = ok / total polls; count + duration of gaps
  (runs of `token_expired`/`http_error`); longest gap.
- count of `partial` session windows.
- **assumption check:** within each weekly_key, is weekly_pct monotonic
  non-decreasing? Report violations (would mean weekly is rolling, not a fixed
  bucket → revisit `weekly_contribution`).

## Deliverables
- `tracker/tokens.py`, `tracker/analyze.py`
- SQL views (inline in `db.py` init, or `docs/views.sql` loaded at connect)
- CLI summary command
- Everything importable by Phase 3.

## Build-time validation checklist (run once enough data exists)
- [ ] weekly_pct monotonic within a weekly key (fact 5) — else revisit contribution.
- [ ] `window_end − 5h` lines up with the window's earliest activity (fact 4).
- [ ] token dedup ratio sane (~unique message.ids ≪ raw lines).
- [ ] a fully-covered window's peak/final look right vs. a `/usage` spot check.

## Open decisions (defaults chosen)
| Decision | Default |
|---|---|
| Weekly "% used" per session | report **contribution** (Δ) as the headline, keep level (weekly_at_end) as context |
| Sub-agent / sidechain tokens | include (count toward limits) |
| Transcript dedup key | `message.id`, fallback `requestId`; keep max `output_tokens` |
| Pre-tracking windows | tokens summarized separately; no invented percentages |

## Out of scope (→ Phase 3)
Charts, HTML dashboard, any rendering. Phase 2 stops at structured results + CLI.
