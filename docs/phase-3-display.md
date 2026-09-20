# Phase 3 — Wire the display to real data (handoff)

Goal: replace the **seeded simulation** in the prototype dashboard with **real data
from `usage.db`**. The backend (capture + harvest + cost + categories, all on timers)
is done and committed; this task is only the display layer.

## Starting point

- **Prototype:** `display/quota-telemetry.html` — a self-contained page with two
  instruments. Live copy: https://claude.ai/code/artifact/affd55bb-e094-4468-a9e2-5a742cd5b814
  - **01 Session vs Week** — each session is a rectangle: width = % of its 5-hour
    window used, height = % of the weekly window it ate; stacked per week. Red cap =
    hit the 5-hour wall; red band = weekly ceiling; amber hatch = credit overage.
  - **02 Week Burn-up** — one small-multiple panel per week: cumulative weekly % over
    the 7-day window on a fixed 0–100% scale, with an even-pace diagonal, session
    dots, and an amber sliver for credit spend past 100%.
- **The simulation to replace** lives in one JS block: `display/quota-telemetry.html`
  lines ~172–230 (`/* seeded data model */` → `mulberry32` PRNG → `const weeks=[]` →
  the generation loop, ending just before `const allSessions =`). Everything downstream
  (`renderKPIs`, `renderStacks`, `renderRidge`, tooltips) consumes the `weeks[]` array
  and a few helpers — keep those; only change how `weeks[]` is produced.

## Delivery mechanism (read this first)

Artifacts run under a **strict CSP: no `fetch`/XHR/external requests**. The page
**cannot query SQLite at runtime.** Real data must be **inlined** into the HTML.

Recommended: write **`tracker/export.py`** that reads `usage.db` and prints one JSON
object, then a tiny generator (call it `tracker/report.py`) that substitutes it into
the template in place of the simulation block, emitting a finished self-contained HTML.
Keep the simulation available behind a flag (e.g. a `?demo=1` check) so the page still
demos when data is thin. Re-generate on demand or from a timer.

## Data sources — all in `data/usage.db`

Reuse the analysis functions in `tracker/tokens.py` (import it; `tokens._connect()`
opens the DB with WAL + seeds). Do **not** re-derive what these already compute.

| Need | Source |
|---|---|
| session-% and weekly-% trajectory, window ids, credits, surface split | `snapshots` (raw) |
| observed 5-hour windows `(start, end, key)` | `tokens._session_windows(conn)` |
| tokens per window × model | `window_tokens` table (rebuilt each harvest) |
| cost (derived, temporal) | `tokens.price_index(conn)`, `tokens.turn_cost(row, idx)`, `tokens.cost_summary(conn)` |
| Tier-B category per session | `tokens.load_rules(conn)` + `tokens.classify(cwd, title, rules)`; `v_sessions` view has `cwd`/`ai_title` |
| Tier-A surface split (exact weekly %) | `tokens.latest_surface_split(conn)` → `{claude_code, chat, cowork, other}` |
| per-session label | `session_titles` / `v_sessions.ai_title` |

Snapshot-derived queries you will likely write in `export.py`:

```sql
-- session windows: peak session-% and weekly contribution (Δ) per 5-hour window
SELECT substr(session_resets_at,1,16) AS key,
       MAX(session_pct)  AS peak_session_pct,
       MIN(weekly_pct)   AS weekly_at_start,
       MAX(weekly_pct)   AS weekly_at_end,        -- contribution = end - start
       MIN(captured_at)  AS first_seen, MAX(captured_at) AS last_seen
FROM snapshots WHERE status='ok' AND session_resets_at IS NOT NULL
GROUP BY key;

-- weekly burn-up trajectory: ordered points within one weekly window
SELECT captured_at, weekly_pct FROM snapshots
WHERE status='ok' AND substr(weekly_resets_at,1,16)=:weekly_key ORDER BY captured_at;

-- credit spend per interval = positive deltas of extra_used_credits, then bucket by
-- captured_at into the active session window (reset the baseline at the monthly rollover).
SELECT captured_at, extra_used_credits FROM snapshots WHERE status='ok' ORDER BY captured_at;
```

## Semantic rules — must not be violated (these were hard-won)

1. **session-% and weekly-% are independent channels** (model weighting) — never derive
   one from the other; show both.
2. **Weekly % is monotonic** within a window; a session's weekly contribution =
   `weekly_pct(end) − weekly_pct(start)`.
3. **Chat shares the same 5-hour and weekly windows as Claude Code**, but has **no local
   transcripts** — so `turns`/tokens/cost are **Claude Code only**. A window's `%` may
   include Chat you cannot see per-session (the surface breakdown is *weekly-only*).
   Label all token/cost figures "Claude Code"; present the Tier-A surface split
   separately as exact weekly %. Chat is often the **majority** of real usage.
4. **Cost is notional** (API list rates). On the subscription, marginal cost is $0 until
   limits; **credits (`extra_used_credits` deltas) are the only real dollars.** Frame the
   $ number as relative weight, not a bill.
5. **Session windows exist only while active** (`session_resets_at` is null at 0%); turns
   outside any observed window are `unassigned` in `window_tokens`.
6. **Tier-A surfaces are exact %; Tier-B categories are a token-share approximation**
   within Claude Code.
7. **Real data is thin and grows over time.** At handoff, snapshot coverage is only ~2
   session windows + 1 weekly window (the tracker started 2026-09-19); the transcript
   token history is ~30 days deep. The Burn-up needs several weeks to be interesting —
   expect sparse panels until coverage accrues. Keep the demo simulation reachable.

## Open mapping decision (make this call early)

The prototype's rectangle = one *transcript session*. The real quota unit is the
**5-hour window**, which may contain several transcript sessions **plus Chat**.
Recommended mapping: **one rectangle per observed 5-hour window** (matches quota
semantics) — width = `peak_session_pct`, height = weekly contribution Δ, color = the
window's dominant category by tokens, tooltip = per-category token/cost split + the
window's Chat share (from the nearest weekly breakdown). Credit overage per window =
the Δ-`extra_used_credits` bucketed into that window.

## Steps

1. Read `display/quota-telemetry.html`; locate the sim block and the shape of `weeks[]`
   (`weeks[i].sessions[j]` fields: `sessionPct, weeklyPct, cat, tokens, wall, nearWall,
   onCredits, creditUsd, durH, start, label`; plus `weeklyPeak`, `creditUsd`, `start`,
   `end`). Your JSON must produce the same shape (or adjust the render fns to match).
2. Write `tracker/export.py` → JSON, reusing the functions above. Verify numbers against
   `~/work/bin/python tracker/tokens.py` and direct SQL on a **copy** of `usage.db`.
3. Add `tracker/report.py` to inline the JSON into the template (sim behind `?demo=1`).
4. Re-publish the artifact (and optionally a timer to regenerate).

## Verify against the backend

Cross-check the exporter's totals against `tokens.cost_summary(conn)`,
`tokens.category_summary(conn)`, and `tokens.latest_surface_split(conn)` before
publishing.

## Key files

- Backend / analysis: `tracker/tokens.py` (functions above), `refresh_prices.py`,
  `poll.py`, `alert.py`, `db.py`.
- Display: `display/quota-telemetry.html`.
- Design & schema: `PLAN.md`, `docs/phase-2-analysis.md`.
- Data (on the tracker machine, gitignored): `data/usage.db`.
