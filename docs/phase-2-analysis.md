# Phase 2 — Analysis Layer (Plan)

Turns the raw snapshot stream (Phase 1) into per-window answers to the original
question: **for each session (5-hour) window — what % of the session window did
I use, what % of the weekly window did it consume, how many tokens was that,
what did it cost, and what kind of work was it.** This phase produces the
analysis functions + SQL views + a CLI summary; the visual dashboard is Phase 3.

## Inputs

1. **`data/usage.db` → `snapshots`** (Phase 1): one row per ~5-min poll with
   `captured_at`, `status`, `session_pct`, `session_resets_at`, `weekly_pct`,
   `weekly_resets_at`, `weekly_opus_pct`, `extra_usage_pct`, `cc_weekly_share`.
   Only `status='ok'` rows carry percentages.
2. **`~/.claude/projects/**/*.jsonl`** (local transcripts): per-assistant-turn
   token usage + session titles. ~30-day retention.
3. **`prices`** table (new, in `data/usage.db`): a versioned price book, seeded
   and maintained separately (see Cost). Lets us price historical tokens.

## Grounded data facts (verified against captured data — do not re-derive)

1. **`resets_at` needs normalization.** Stable to the *second* but the
   *microseconds jitter* between polls of the same window
   (`20:30:00.440842` vs `20:30:00.808354`). Grouping on the raw string shatters
   one window. **Window key = `substr(resets_at, 1, 16)`** (minute truncation).
2. **Transcript turns must be de-duplicated.** One assistant turn writes several
   JSONL lines sharing a `message.id` (observed 70 lines → 28 unique). Summing
   raw lines overcounts ~2.5×. **Dedup by `message.id`; keep the line with the
   max `output_tokens`.** `requestId` is an equivalent fallback key.
3. **Percentages are not a linear function of tokens** (model weighting). Report
   both side by side; never derive one from the other.
4. Session window = 5h, `resets_at` = window *end*, `window_start = end − 5h`.
   Windows are activity-anchored; derive boundaries from observed `resets_at`.
5. Weekly window resets at a fixed instant (`07:00 UTC` = local midnight),
   7-day bucket. **Assumption to verify:** `weekly_pct` monotonic
   non-decreasing within one weekly key until reset. If true, a session's weekly
   *contribution* = weekly_pct(end) − weekly_pct(start).
6. Full comparison exists only where snapshot coverage and transcript retention
   overlap (tracker start onward). Earlier windows: tokens/cost but no %.
7. Raw usage splits cache creation into `ephemeral_5m_input_tokens` and
   `ephemeral_1h_input_tokens` — priced differently, so keep them separate.

## Design

### Window-key normalization
Derived in views, not stored: `session_key = substr(session_resets_at,1,16)`,
`weekly_key = substr(weekly_resets_at,1,16)`.

### `v_session_windows` (GROUP BY session_key over status='ok')
- `session_key`, `window_end` (max `session_resets_at`), `window_start` (end − 5h)
- `peak_session_pct` = MAX(session_pct); `final_session_pct` = last sample's
- `first_seen`, `last_seen`, `n_samples`
- `weekly_at_start`, `weekly_at_end`; `weekly_contribution` = end − start
- `partial` flag: first_seen − window_start > ~10 min, or first session_pct ≫ 0
  (window observed late → peak/contribution understated; mark, don't hide)

### `v_weekly_windows` (GROUP BY weekly_key over status='ok')
`peak_weekly_pct`, `final_weekly_pct`, `first_seen`, `last_seen`,
`avg_cc_weekly_share`, distinct session_keys in the week.

### `tracker/tokens.py` — transcript join (tokens + label)
- Walk `~/.claude/projects/**/*.jsonl`; keep `type=='assistant'` with
  `message.usage`. **Dedup** by `message.id`, keep max `output_tokens`.
- Extract per turn: `timestamp`, `model`, and token components mapped to price
  components (fact 7):
  | component | raw field |
  |---|---|
  | input | `input_tokens` |
  | output | `output_tokens` (includes thinking) |
  | cache_read | `cache_read_input_tokens` |
  | cache_write_5m | `cache_creation.ephemeral_5m_input_tokens` |
  | cache_write_1h | `cache_creation.ephemeral_1h_input_tokens` |
  Include sidechain/sub-agent turns (they count).
- **Bucket into observed session windows:** turn at `t` belongs to `W` iff
  `W.window_start ≤ t < W.window_end`. No-match turns → `unassigned`.
- Output per window: token totals, per-component, per-model breakdown.

### Cost — price-versioned, retrospective
**Cost is derived, never stored** on snapshots (keeps schema lean; a price
correction re-flows everywhere). It joins token counts to a small versioned
price book:

```sql
CREATE TABLE IF NOT EXISTS prices (
  model         TEXT NOT NULL,   -- e.g. 'claude-opus-4-8'
  component     TEXT NOT NULL,   -- input|output|cache_read|cache_write_5m|cache_write_1h
  usd_per_mtok  REAL NOT NULL,   -- USD per 1M tokens
  valid_from    TEXT NOT NULL,   -- UTC date the price took effect
  valid_to      TEXT,            -- NULL = currently in effect
  source        TEXT             -- 'ccusage'|'litellm'|'claude-api'|'manual'
);
```

- **Lookup** for a turn at time `T`: row matching model+component with
  `valid_from <= T AND (valid_to IS NULL OR T < valid_to)`.
  cost = tokens/1e6 × usd_per_mtok, summed over components and turns per window.
- **Temporal by design:** past tokens are priced at the price in effect *then*,
  even after Anthropic changes prices. A new price is added by closing the open
  row (`valid_to = change_date`) and inserting a new one
  (`valid_from = change_date`) — history is never edited.
- **`tracker/refresh_prices.py`** — read current prices from a source (ccusage
  bundles LiteLLM's price map; or the claude-api reference; or manual) and
  compare to the open rows; on any difference, version the row. Prices change
  rarely, so run this manually or via an occasional (~monthly) timer — **not**
  the 5-min poller.
- **Seed** once with today's prices for models seen in transcripts, `valid_from`
  set before the oldest transcript so existing token history is covered — safe
  because these models' prices have been stable across our ~30-day window.
- Approximate by design (user-accepted): component granularity above is the
  ceiling of precision we attempt.

### Categorization — "what kind of work" (two tiers)

The five categories live at two levels, because they come from two data sources:

**Tier A — Claude Code sub-categories** (from transcripts, per `cwd`): Coding,
RPG-dev, newsdeed, other. Derived from an **ordered, editable ruleset** in
`tracker/categories.py` (first match wins), keyed mainly on `cwd`:

| # | test | category |
|---|---|---|
| 1 | `cwd` contains `rpg` | RPG-dev |
| 2 | `cwd` contains `newsfeed` (= newsdeed → `~/dev/newsfeed-summary`) | newsdeed |
| 3 | `cwd` under `~/dev/` OR transcript has file-edit tool use | Coding |
| 4 | (default) | other |

Rules live in code so they stay transparent and tunable (add specific dirs to
Coding as needed). Category attaches **per transcript**, so tokens/cost roll up
by category exactly across any range.

**Tier B — the `chats` category is a different surface, not Claude Code.**
It is the `chat` row of the usage endpoint's `seven_day_breakdown` (claude.ai web
/ Claude app). It has **no local transcripts**, so no tokens/cost/windows —
only a weekly % share. `cowork` and `other` breakdown rows are handled the same
way. **Requires the Phase 1 tweak below** to be captured over time.

**Reporting the split:**
- weekly usage **by surface** (claude_code / chat / cowork / other): exact, from
  `seven_day_breakdown`.
- within Claude Code, **by sub-category** (Coding/RPG-dev/newsdeed/other): use
  each category's **token share** as the proxy for its % of usage (exact % per
  sub-category isn't exposed; token share is the accepted approximation).
- so e.g. "% of weekly that was RPG-dev" ≈ claude_code weekly share ×
  RPG-dev token share within Claude Code.

### Session labeling — "what specifically"
Distinct from category: a human-readable label per window — the transcript's
`ai-title` if present, else the first non-sidechain user message (~120 chars),
taking the label of the transcript contributing the most turns. Derived, not
stored heavily. Title-based; may not reflect exact contents (user-accepted).

### Phase 1 tweak required (small)
`poll.py` currently stores only `cc_weekly_share`. To support the chats/surface
split, capture the **full `seven_day_breakdown`** instead — recommended as one
nullable `weekly_breakdown_json` TEXT column on `snapshots` (flexible, survives
new surfaces; `cc_weekly_share` kept as a convenience). Backfill the handful of
existing rows from `data/raw/*.jsonl`. This is the only change outside Phase 2's
own modules.

### `tracker/analyze.py` — functions + CLI
- Functions returning plain dicts/lists (Phase 3 imports these):
  `session_windows()` (windows + tokens + cost + label), `weekly_windows()`,
  `data_quality()`.
- CLI (`~/work/bin/python analyze.py`) prints:
  - recent session windows:
    `start(local) | dur | peak% | final% | weekly Δ% | tokens | $cost | category | label`
  - a weekly summary per weekly key, with the surface split (claude_code / chat
    / cowork / other) and the within-Claude-Code sub-category breakdown.
  - a data-quality footer.

### `data_quality()`
- capture coverage = ok / total polls; count + duration of gaps; longest gap.
- count of `partial` windows.
- **assumption check:** weekly_pct monotonic within each weekly key (fact 5).
- **price coverage check:** every model present in transcripts has a price row
  covering its turns (else cost is undercounted — fail loudly).

## Deliverables
- `tracker/tokens.py`, `tracker/analyze.py`, `tracker/refresh_prices.py`,
  `tracker/categories.py` (the editable ruleset)
- `prices` table + one-time seed; SQL views (inline in `db.py` or `docs/views.sql`)
- Phase 1 tweak: capture full `seven_day_breakdown` in `poll.py` + backfill
- CLI summary; everything importable by Phase 3.

## Build-time validation checklist (run once enough data exists)
- [ ] weekly_pct monotonic within a weekly key (fact 5) — else revisit contribution.
- [ ] `window_end − 5h` lines up with the window's earliest activity (fact 4).
- [ ] token dedup ratio sane (unique message.ids ≪ raw lines).
- [ ] price book covers every model in the transcripts.
- [ ] a fully-covered window's peak/final look right vs. a `/usage` spot check.

## Open decisions (defaults chosen)
| Decision | Default |
|---|---|
| Weekly "% used" per session | headline = contribution (Δ); keep level as context |
| Sub-agent / sidechain tokens | include (count toward limits) |
| Transcript dedup key | `message.id`, fallback `requestId`; keep max `output_tokens` |
| Cost storage | derived at query time, not stored (price-versioned join) |
| Price source | ccusage/LiteLLM map for seed + change detection; manual fallback |
| Categories | Coding/RPG-dev/newsdeed/other = per-transcript cwd rules in `categories.py`; chats = `chat` surface from the endpoint breakdown (weekly % only) |
| Sub-category % of weekly | approximated by token share within Claude Code |
| Session label | `ai-title` else first user message (separate from category) |
| Pre-tracking windows | tokens/cost summarized separately; no invented percentages |

## Out of scope (→ Phase 3)
Charts, HTML dashboard, any rendering. Phase 2 stops at structured results + CLI.
