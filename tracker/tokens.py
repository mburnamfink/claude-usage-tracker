"""Incremental token harvester.

Walks Claude Code transcripts (`~/.claude/projects/**/*.jsonl`), dedups assistant
turns, and stores per-turn token usage in usage.db so the token history OUTLIVES the
transcripts' ~30-day retention. Stdlib only. Safe to run every few minutes: unchanged
files cost one stat(); a growing file is read only from where we left off.

Transcript facts this relies on (verified against the local corpus, 458 files/462 MB):
  * One file == one session (a single sessionId); files are append-only while active.
  * The SAME message.id is written on several lines within a file. Of 4240 duplicate
    groups, 4239 are byte-identical and 1 is a streaming partial that GROWS
    (output_tokens 3 -> 350). So dedup by message.id keeping MAX per component: exact
    dupes are a no-op, the rare partial cannot undercount, and re-reading is harmless.
  * Cross-file duplicate ids are rare (5) -> dedup is GLOBAL (message_id PRIMARY KEY).
  * Timestamps are NOT monotonic within a file -> never infer order from line position;
    a turn is bucketed into a session window by its own timestamp, at read time
    (see refresh_window_tokens), so windows observed later retroactively claim old turns.
  * usage carries both cache_creation_input_tokens (total) and a cache_creation object
    {ephemeral_5m,ephemeral_1h} that price differently -> the split is kept.

Footguns and how each is defused:
  * Re-reading an active file could double-count      -> message_id UPSERT keep-max (idempotent).
  * mtime granularity / clock skew missing a write    -> correctness is per-file (dev_ino,size,offset),
                                                          not a global last-run clock; re-reads are safe.
  * A 462 MB corpus / a 120 MB active file being slow -> stat-skip unchanged files; resume changed
                                                          files from a stored byte offset (append only).
  * A half-written final line (mid-append)            -> consume only up to the last '\n'; the partial
                                                          line is left for next run.
  * A file compacted/truncated/replaced in place      -> detect via (dev_ino change) or (size shrank)
                                                          and re-read from offset 0.
  * Files vanishing at retention                      -> harvested turns are kept; a missing file is skipped.
  * Concurrent poller writes                          -> WAL + busy_timeout + short per-file transactions.
"""
import bisect
import glob
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db

PROJECTS = Path.home() / ".claude" / "projects"
MAX_LINE = 1 << 20            # assistant-usage lines are <10 KB; skip anything absurd
SESSION_WINDOW = timedelta(hours=5)

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
  message_id        TEXT PRIMARY KEY,   -- message.id, else requestId
  ts                TEXT,               -- assistant timestamp, ISO8601 UTC
  model             TEXT,
  input_tokens      INTEGER DEFAULT 0,
  output_tokens     INTEGER DEFAULT 0,  -- includes thinking
  cache_read_tokens INTEGER DEFAULT 0,
  cache_write_5m    INTEGER DEFAULT 0,  -- cache_creation.ephemeral_5m_input_tokens
  cache_write_1h    INTEGER DEFAULT 0,  -- cache_creation.ephemeral_1h_input_tokens
  cwd               TEXT,               -- category signal (derived at read time)
  is_sidechain      INTEGER DEFAULT 0,  -- sub-agent turns; they still count
  session_id        TEXT,
  source_file       TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);

-- per-file resume state; the harvester's fast path lives here
CREATE TABLE IF NOT EXISTS harvest_files (
  path        TEXT PRIMARY KEY,
  dev_ino     TEXT,          -- st_dev:st_ino, detects a same-path replacement
  size        INTEGER,       -- file size observed at last consume
  mtime_ns    INTEGER,       -- exact mtime; float equality is unreliable
  byte_offset INTEGER,       -- end of the last COMPLETE line we consumed
  n_turns     INTEGER DEFAULT 0,
  updated_at  TEXT
);

-- derived rollup, recomputed from turns each run (turns is the source of truth)
CREATE TABLE IF NOT EXISTS window_tokens (
  session_key       TEXT,    -- substr(session_resets_at,1,16); 'unassigned' if no window covers the turn
  model             TEXT,
  input_tokens      INTEGER DEFAULT 0,
  output_tokens     INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_write_5m    INTEGER DEFAULT 0,
  cache_write_1h    INTEGER DEFAULT 0,
  n_turns           INTEGER DEFAULT 0,
  PRIMARY KEY (session_key, model)
);

-- heartbeat + last-run stats, so the 5-minute poller can tell if the hourly
-- harvest silently stopped or started recognizing nothing (format drift).
CREATE TABLE IF NOT EXISTS harvest_meta (k TEXT PRIMARY KEY, v TEXT);

-- per-session label (Claude Code's own ai-title); harvested, latest wins
CREATE TABLE IF NOT EXISTS session_titles (
  session_id TEXT PRIMARY KEY,
  ai_title   TEXT,
  updated_at TEXT
);

-- versioned price book. Cost is DERIVED at query time (never stored on a turn), so a
-- price correction re-flows through all history. A price change = close the open row
-- (set valid_to) and insert a new one (valid_from = change date); history is never edited.
CREATE TABLE IF NOT EXISTS prices (
  model        TEXT NOT NULL,   -- exact model string as it appears in turns.model
  component    TEXT NOT NULL,   -- input | output | cache_read | cache_write_5m | cache_write_1h
  usd_per_mtok REAL NOT NULL,   -- USD per 1,000,000 tokens
  valid_from   TEXT NOT NULL,   -- ISO date the price took effect (UTC)
  valid_to     TEXT,            -- NULL = currently in effect
  source       TEXT
);
CREATE INDEX IF NOT EXISTS idx_prices_model ON prices(model, component);

-- editable, ordered category ruleset (Tier B, within Claude Code). First match wins.
-- Extend by INSERTing a row with a seq that orders it correctly; nothing else changes.
CREATE TABLE IF NOT EXISTS category_rules (
  seq      INTEGER PRIMARY KEY,   -- lower = checked first
  field    TEXT,                  -- 'cwd' | 'title' | '' (for default)
  op       TEXT,                  -- 'contains' | 'startswith' | 'regex' | 'default'
  pattern  TEXT,
  category TEXT
);

-- one row per transcript session: aggregates from turns + the harvested label.
-- A VIEW, so it can never go stale relative to turns/labels. category is applied
-- at read time by classify() (rules can change without touching stored data).
CREATE VIEW IF NOT EXISTS v_sessions AS
SELECT t.session_id,
       MIN(t.ts)  AS first_ts,
       MAX(t.ts)  AS last_ts,
       COUNT(*)   AS n_turns,
       MAX(t.cwd) AS cwd,            -- cwd is stable within a session
       st.ai_title AS ai_title
FROM turns t
LEFT JOIN session_titles st ON st.session_id = t.session_id
GROUP BY t.session_id;
"""

# Seeded once (only if category_rules is empty). Ordered: specific dirs and the
# title-based newsletter signal BEFORE the broad ~/dev -> coding fallback, so
# rpg/media/newsletters under ~/dev win. Newsletters run from ~/Downloads with
# titles like "Score these articles", so title rules matter (verified in corpus).
SEED_RULES = [
    (10, "cwd",   "contains",   "rpg-adventure",        "rpg-dev"),
    (15, "cwd",   "contains",   "rpg-dev",              "rpg-dev"),
    (20, "cwd",   "contains",   "media-management",     "media"),
    (25, "cwd",   "contains",   "/pictures/",           "media"),
    (30, "cwd",   "contains",   "newsfeed",             "newsletters"),
    (35, "title", "contains",   "newsletter",           "newsletters"),
    (40, "title", "contains",   "article",              "newsletters"),
    (50, "cwd",   "startswith", "/home/michael/dev",    "coding"),
    (55, "cwd",   "contains",   "webscraper",           "coding"),
    (99, "",      "default",    "",                     "misc"),
]

# Tier A — surfaces from the endpoint breakdown; not derivable from transcripts.
SURFACES = {"claude_code": "Claude Code", "chat": "Chats",
            "cowork": "Cowork", "other": "Other"}

# First-party Anthropic API rates, $/MTok (verified against the LiteLLM price map,
# raw.githubusercontent.com/BerriAI/litellm, 2026-09). Per component:
#   input, output, cache_read (=0.10x input), cache_write_5m (=1.25x input, from LiteLLM),
#   cache_write_1h (=2.0x input; Anthropic's 1-hour-cache rate, not carried separately by LiteLLM).
# valid_from is set safely before the oldest transcript so all history is priced.
PRICE_VALID_FROM = "2025-01-01"
PRICE_SOURCE = "litellm+anthropic-1h-2x@2026-09"
# model: (input, output)  -> cache rates derived below
_BASE = {
    "claude-opus-5":               (5.0, 25.0),
    "claude-opus-4-8":             (5.0, 25.0),
    "claude-sonnet-5":             (2.0, 10.0),
    "claude-sonnet-4-6":           (3.0, 15.0),
    "claude-haiku-4-5-20251001":   (1.0,  5.0),
}


def _seed_price_rows():
    rows = []
    for model, (inp, out) in _BASE.items():
        comps = {
            "input": inp, "output": out,
            "cache_read": round(inp * 0.10, 6),
            "cache_write_5m": round(inp * 1.25, 6),
            "cache_write_1h": round(inp * 2.00, 6),
        }
        for comp, usd in comps.items():
            rows.append((model, comp, usd, PRICE_VALID_FROM, None, PRICE_SOURCE))
    # synthetic turns carry no cost
    for comp in ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h"):
        rows.append(("<synthetic>", comp, 0.0, PRICE_VALID_FROM, None, "n/a"))
    return rows

UPSERT = """
INSERT INTO turns (message_id, ts, model, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_5m, cache_write_1h,
                   cwd, is_sidechain, session_id, source_file)
VALUES (:message_id,:ts,:model,:input_tokens,:output_tokens,
        :cache_read_tokens,:cache_write_5m,:cache_write_1h,
        :cwd,:is_sidechain,:session_id,:source_file)
ON CONFLICT(message_id) DO UPDATE SET
  output_tokens     = MAX(turns.output_tokens,     excluded.output_tokens),
  input_tokens      = MAX(turns.input_tokens,      excluded.input_tokens),
  cache_read_tokens = MAX(turns.cache_read_tokens, excluded.cache_read_tokens),
  cache_write_5m    = MAX(turns.cache_write_5m,    excluded.cache_write_5m),
  cache_write_1h    = MAX(turns.cache_write_1h,    excluded.cache_write_1h),
  ts=excluded.ts, model=excluded.model, cwd=excluded.cwd,
  is_sidechain=excluded.is_sidechain, session_id=excluded.session_id,
  source_file=excluded.source_file
"""


def _connect() -> sqlite3.Connection:
    conn = db.connect(db.DB_PATH)             # read module attr at call time (overridable in tests)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # let the poller write while we read
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM category_rules").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO category_rules (seq, field, op, pattern, category) VALUES (?,?,?,?,?)",
            SEED_RULES,
        )
        conn.commit()
    if conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO prices (model, component, usd_per_mtok, valid_from, valid_to, source) "
            "VALUES (?,?,?,?,?,?)",
            _seed_price_rows(),
        )
        conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _parse_line(raw: bytes, source_file: str, session_hint: str):
    # cheap reject before JSON: an assistant-usage line contains both markers
    if b'"assistant"' not in raw or b'"usage"' not in raw or len(raw) > MAX_LINE:
        return None
    try:
        o = json.loads(raw)
    except ValueError:
        return None
    if o.get("type") != "assistant":
        return None
    m = o.get("message") or {}
    u = m.get("usage")
    if not u:
        return None
    mid = m.get("id") or o.get("requestId")
    if not mid:
        return None
    cc = u.get("cache_creation") or {}
    return {
        "message_id": mid,
        "ts": o.get("timestamp"),
        "model": m.get("model"),
        "input_tokens": u.get("input_tokens") or 0,
        "output_tokens": u.get("output_tokens") or 0,
        "cache_read_tokens": u.get("cache_read_input_tokens") or 0,
        "cache_write_5m": cc.get("ephemeral_5m_input_tokens") or 0,
        "cache_write_1h": cc.get("ephemeral_1h_input_tokens") or 0,
        "cwd": o.get("cwd"),
        "is_sidechain": 1 if o.get("isSidechain") else 0,
        "session_id": o.get("sessionId") or session_hint,
        "source_file": source_file,
    }


def _read_appended(path: str, start_offset: int):
    """Parse turns from the bytes after start_offset.

    Returns (turns, new_offset). Only complete (newline-terminated) lines are
    consumed; a trailing partial line leaves the offset before it, so the next run
    picks it up once finished.
    """
    turns = []
    latest_title = None
    name = os.path.basename(path)
    session_hint = name[:-6] if name.endswith(".jsonl") else name
    consumed = 0
    with open(path, "rb") as fh:
        fh.seek(start_offset)
        for raw in fh:                     # binary iteration splits on b'\n'
            if not raw.endswith(b"\n"):    # final partial line: stop, don't consume
                break
            consumed += len(raw)
            if b'"ai-title"' in raw:       # session label line; latest in file wins
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                if o.get("type") == "ai-title":
                    title = o.get("aiTitle") or o.get("title")
                    if title:
                        latest_title = title
                continue
            t = _parse_line(raw[:-1], path, session_hint)
            if t:
                turns.append(t)
    return turns, latest_title, start_offset + consumed


def harvest(verbose: bool = False) -> dict:
    conn = _connect()
    state = {r["path"]: r for r in conn.execute("SELECT * FROM harvest_files")}
    files = glob.glob(str(PROJECTS / "**" / "*.jsonl"), recursive=True)
    changed = new_turns = appended_bytes = titles = 0

    for path in files:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue                        # vanished between glob and stat
        dev_ino = f"{st.st_dev}:{st.st_ino}"
        prev = state.get(path)

        # fast path: identical identity, size and mtime -> nothing to do
        if (prev and prev["dev_ino"] == dev_ino
                and prev["size"] == st.st_size and prev["mtime_ns"] == st.st_mtime_ns):
            continue

        # append -> resume from stored offset; replaced/truncated -> full re-read
        if prev and prev["dev_ino"] == dev_ino and st.st_size >= prev["size"]:
            offset = prev["byte_offset"]
        else:
            offset = 0

        turns, latest_title, new_offset = _read_appended(path, offset)
        changed += 1
        new_turns += len(turns)
        appended_bytes += new_offset - offset
        titles += 1 if latest_title else 0
        session_id = os.path.basename(path)[:-6] if path.endswith(".jsonl") else os.path.basename(path)
        with conn:                          # one short transaction per file
            for t in turns:
                conn.execute(UPSERT, t)
            if latest_title:
                conn.execute(
                    """INSERT INTO session_titles (session_id, ai_title, updated_at)
                       VALUES (?,?,?)
                       ON CONFLICT(session_id) DO UPDATE SET
                         ai_title=excluded.ai_title, updated_at=excluded.updated_at""",
                    (session_id, latest_title, _now()),
                )
            conn.execute(
                """INSERT INTO harvest_files
                     (path, dev_ino, size, mtime_ns, byte_offset, n_turns, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     dev_ino=excluded.dev_ino, size=excluded.size, mtime_ns=excluded.mtime_ns,
                     byte_offset=excluded.byte_offset,
                     n_turns=harvest_files.n_turns+excluded.n_turns,
                     updated_at=excluded.updated_at""",
                (path, dev_ino, st.st_size, st.st_mtime_ns, new_offset, len(turns), _now()),
            )

    refresh_window_tokens(conn)
    total = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    # heartbeat + last-run stats for the poller's pipeline health check
    meta = {"last_run": _now(), "last_files_changed": changed, "last_turns_written": new_turns,
            "last_appended_bytes": appended_bytes, "last_titles": titles}
    with conn:
        conn.executemany(
            "INSERT INTO harvest_meta (k, v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            [(k, str(v)) for k, v in meta.items()])
    conn.close()
    result = {"files_seen": len(files), "files_changed": changed,
              "turns_written": new_turns, "turns_total": total}
    if verbose:
        print(result)
    return result


def _session_windows(conn):
    """Observed session windows as (start, end, key), sorted by start.

    end = latest session_resets_at in the minute-normalized group; start = end - 5h.
    """
    rows = conn.execute(
        """SELECT substr(session_resets_at,1,16) AS k, MAX(session_resets_at) AS win_end
             FROM snapshots
            WHERE status='ok' AND session_resets_at IS NOT NULL
            GROUP BY k"""
    ).fetchall()
    wins = []
    for r in rows:
        end = _parse_iso(r["win_end"])
        if end:
            wins.append((end - SESSION_WINDOW, end, r["k"]))
    wins.sort()
    return wins


def refresh_window_tokens(conn) -> None:
    """Rebuild window_tokens from turns against the CURRENT set of windows.

    Assignment is done here, not at harvest, so a window observed after a turn was
    harvested still claims it. Cheap: a few thousand turns against a few hundred windows.
    """
    wins = _session_windows(conn)
    starts = [w[0] for w in wins]
    agg = defaultdict(lambda: [0, 0, 0, 0, 0, 0])   # (key, model) -> 5 components + n

    for r in conn.execute(
        """SELECT ts, model, input_tokens, output_tokens, cache_read_tokens,
                  cache_write_5m, cache_write_1h FROM turns"""
    ):
        key = "unassigned"
        t = _parse_iso(r["ts"])
        if t and wins:
            i = bisect.bisect_right(starts, t) - 1   # latest window whose start <= t
            if 0 <= i < len(wins) and wins[i][0] <= t < wins[i][1]:
                key = wins[i][2]
        a = agg[(key, r["model"])]
        a[0] += r["input_tokens"]; a[1] += r["output_tokens"]; a[2] += r["cache_read_tokens"]
        a[3] += r["cache_write_5m"]; a[4] += r["cache_write_1h"]; a[5] += 1

    with conn:
        conn.execute("DELETE FROM window_tokens")
        conn.executemany(
            """INSERT INTO window_tokens
                 (session_key, model, input_tokens, output_tokens, cache_read_tokens,
                  cache_write_5m, cache_write_1h, n_turns)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(k, model, *a) for (k, model), a in agg.items()],
        )


def load_rules(conn):
    return list(conn.execute(
        "SELECT seq, field, op, pattern, category FROM category_rules ORDER BY seq"))


def classify(cwd, title, rules):
    """Tier-B category for a Claude Code session (first matching rule wins)."""
    cwd_l = (cwd or "").lower()
    title_l = (title or "").lower()
    for r in rules:
        op = r["op"]
        if op == "default":
            return r["category"]
        val = cwd_l if r["field"] == "cwd" else title_l if r["field"] == "title" else ""
        pat = (r["pattern"] or "").lower()
        if op == "contains" and pat in val:
            return r["category"]
        if op == "startswith" and val.startswith(pat):
            return r["category"]
        if op == "regex" and re.search(r["pattern"], val):
            return r["category"]
    return "misc"


def category_summary(conn):
    """Tier-B split within Claude Code: {category: [sessions, turns]}."""
    rules = load_rules(conn)
    agg = defaultdict(lambda: [0, 0])
    for s in conn.execute("SELECT cwd, ai_title, n_turns FROM v_sessions"):
        cat = classify(s["cwd"], s["ai_title"], rules)
        agg[cat][0] += 1
        agg[cat][1] += s["n_turns"]
    return agg


def latest_surface_split(conn):
    """Tier-A surfaces (exact weekly %) from the newest snapshot breakdown."""
    row = conn.execute(
        """SELECT weekly_breakdown_json FROM snapshots
           WHERE weekly_breakdown_json IS NOT NULL
           ORDER BY captured_at DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return {}
    return {r["key"]: r["percent"] for r in json.loads(row[0])["rows"]}


_COST_COMPONENTS = {
    "input_tokens": "input", "output_tokens": "output",
    "cache_read_tokens": "cache_read", "cache_write_5m": "cache_write_5m",
    "cache_write_1h": "cache_write_1h",
}


def price_index(conn):
    """{model: {component: [(valid_from, valid_to, usd_per_mtok), ...]}} for temporal lookup."""
    idx = defaultdict(lambda: defaultdict(list))
    for r in conn.execute("SELECT model, component, usd_per_mtok, valid_from, valid_to FROM prices"):
        idx[r["model"]][r["component"]].append((r["valid_from"], r["valid_to"], r["usd_per_mtok"]))
    return idx


def _price_at(rows, ts):
    """Price effective at ts (ISO strings sort correctly). None if uncovered."""
    ts = ts or ""
    for vf, vt, usd in rows:
        if vf <= ts and (vt is None or ts < vt):
            return usd
    return None


def turn_cost(row, idx):
    """USD for one turn row. None if the model has no price rows at all (undercount risk)."""
    model_prices = idx.get(row["model"])
    if model_prices is None:
        return None
    total = 0.0
    for col, comp in _COST_COMPONENTS.items():
        tok = row[col] or 0
        if not tok:
            continue
        usd = _price_at(model_prices.get(comp, []), row["ts"])
        if usd:
            total += tok / 1e6 * usd
    return total


def price_coverage(conn):
    """Models in turns with NO price row — cost silently undercounts these. Should be empty."""
    priced = {r[0] for r in conn.execute("SELECT DISTINCT model FROM prices")}
    used = {r[0] for r in conn.execute("SELECT DISTINCT model FROM turns")}
    return sorted(used - priced)


def _session_category(conn):
    rules = load_rules(conn)
    return {s["session_id"]: classify(s["cwd"], s["ai_title"], rules)
            for s in conn.execute("SELECT session_id, cwd, ai_title FROM v_sessions")}


def cost_summary(conn):
    """Derived cost rolled up by category and by model. Returns (by_category, by_model, total)."""
    idx = price_index(conn)
    cat = _session_category(conn)
    by_cat = defaultdict(float)
    by_model = defaultdict(float)
    total = 0.0
    for r in conn.execute(
        """SELECT session_id, model, ts, input_tokens, output_tokens,
                  cache_read_tokens, cache_write_5m, cache_write_1h FROM turns"""
    ):
        c = turn_cost(r, idx) or 0.0
        by_cat[cat.get(r["session_id"], "misc")] += c
        by_model[r["model"]] += c
        total += c
    return dict(by_cat), dict(by_model), total


if __name__ == "__main__":
    harvest(verbose=True)
