"""Export usage.db into the single JSON object the dashboard consumes.

The display runs inside an Artifact under a strict CSP (no fetch/XHR), so it cannot
read SQLite at runtime. This produces one JSON blob that report.py inlines into the
template. All analysis is delegated to tokens.py (prices, categories, windows,
surface split) so numbers match the backend exactly.

Quota unit = one observed 5-hour window (not one transcript session): a window may
hold several transcript sessions plus Chat. Session_resets_at jitters by ~1 minute
around the boundary, so raw window keys come in near-duplicate pairs; we merge any
whose end times fall within MERGE_EPS.

Run: ~/work/bin/python tracker/export.py [--pretty]
"""
import bisect
import json
import sys
from collections import defaultdict
from datetime import timedelta

import tokens

DAY = 86400.0
MERGE_EPS = timedelta(minutes=5)     # collapse minute-jitter window keys into one window
WEEK_MERGE_EPS = timedelta(minutes=30)

# Real category key -> (display name, CSS custom-property carrying its color).
# Keys not listed fall back to the misc slot. tokens.SEED_RULES emits:
#   coding, rpg-dev, newsletters, media, misc  (Tier-B, within Claude Code)
CAT_STYLE = {
    "coding":      ("Coding",      "--cat-coding"),
    "rpg-dev":     ("RPG-dev",     "--cat-rpg"),
    "newsletters": ("Newsletters", "--cat-news"),
    "media":       ("Media",       "--cat-media"),
    "chat":        ("Chats",       "--cat-chat"),
    "misc":        ("Misc",        "--cat-misc"),
}


def _cluster(ends, eps):
    """Assign each end-datetime to a cluster id; ends within eps of the running
    cluster's latest end join it. `ends` need not be sorted. Returns {idx: cid}."""
    order = sorted(range(len(ends)), key=lambda i: ends[i])
    cid_of = {}
    cid = -1
    last = None
    for i in order:
        if last is None or ends[i] - last > eps:
            cid += 1
        cid_of[i] = cid
        last = ends[i]
    return cid_of


def _merge_session_windows(conn):
    """Merged 5-hour windows with per-window snapshot aggregates.

    Returns list of dicts sorted by start:
      start, end (datetime), keys (raw substr keys folded in),
      peak_session_pct, weekly_at_start, weekly_at_end (contribution = end-start).
    """
    raw = tokens._session_windows(conn)          # (start, end, key) sorted by start
    if not raw:
        return []
    ends = [w[1] for w in raw]
    cid_of = _cluster(ends, MERGE_EPS)
    groups = defaultdict(list)
    for i, w in enumerate(raw):
        groups[cid_of[i]].append(w)

    # snapshot stats per raw key, then fold into the merged group
    key_stats = {}
    for r in conn.execute(
        """SELECT substr(session_resets_at,1,16) AS k,
                  MAX(session_pct) AS peak,
                  MIN(weekly_pct)  AS w0, MAX(weekly_pct) AS w1
             FROM snapshots
            WHERE status='ok' AND session_resets_at IS NOT NULL
            GROUP BY k"""
    ):
        key_stats[r["k"]] = r

    # weekly_pct resets to 0 at the weekly boundary, so a window straddling it must
    # only diff snapshots from one weekly key (see build()).
    weekly_by_key = defaultdict(dict)
    for r in conn.execute(
        """SELECT substr(session_resets_at,1,16) AS k, substr(weekly_resets_at,1,16) AS wk,
                  MIN(weekly_pct) AS w0, MAX(weekly_pct) AS w1
             FROM snapshots
            WHERE status='ok' AND session_resets_at IS NOT NULL
              AND weekly_resets_at IS NOT NULL
            GROUP BY k, wk"""
    ):
        weekly_by_key[r["k"]][r["wk"]] = (r["w0"], r["w1"])

    windows = []
    for cid, members in groups.items():
        start = min(m[0] for m in members)
        end = max(m[1] for m in members)
        keys = [m[2] for m in members]
        peak = max((key_stats[k]["peak"] for k in keys if k in key_stats), default=0.0)
        w0 = min((key_stats[k]["w0"] for k in keys if k in key_stats), default=0.0)
        w1 = max((key_stats[k]["w1"] for k in keys if k in key_stats), default=0.0)
        by_wk = {}
        for k in keys:
            for wk, (a, b) in weekly_by_key.get(k, {}).items():
                lo, hi = by_wk.get(wk, (a, b))
                by_wk[wk] = (min(lo, a), max(hi, b))
        windows.append({"start": start, "end": end, "keys": keys,
                        "peak_session_pct": peak,
                        "weekly_at_start": w0, "weekly_at_end": w1,
                        "weekly_by_wk": by_wk})
    windows.sort(key=lambda w: w["start"])
    return windows


def _bucket_index(starts, ends, t):
    """Index of the window [start,end) containing datetime t, else None."""
    if not starts or t is None:
        return None
    i = bisect.bisect_right(starts, t) - 1
    if 0 <= i < len(starts) and starts[i] <= t < ends[i]:
        return i
    return None


def _per_window_tokens_cost(conn, windows):
    """Per merged window: category token-share, total tokens, notional cost, and
    the dominant transcript session's title. Claude Code only (transcripts)."""
    starts = [w["start"] for w in windows]
    ends = [w["end"] for w in windows]
    idx = tokens.price_index(conn)
    sess_cat = tokens._session_category(conn)     # session_id -> category
    titles = {r["session_id"]: r["ai_title"]
              for r in conn.execute("SELECT session_id, ai_title FROM v_sessions")}

    n = len(windows)
    cat_tok = [defaultdict(int) for _ in range(n)]   # tokens by category
    cat_cost = [defaultdict(float) for _ in range(n)]
    sess_tok = [defaultdict(int) for _ in range(n)]  # tokens by session (for label)
    tot_tok = [0] * n
    tot_cost = [0.0] * n

    for r in conn.execute(
        """SELECT session_id, model, ts, input_tokens, output_tokens,
                  cache_read_tokens, cache_write_5m, cache_write_1h FROM turns"""
    ):
        t = tokens._parse_iso(r["ts"])
        wi = _bucket_index(starts, ends, t)
        if wi is None:
            continue
        weight = (r["input_tokens"] + r["output_tokens"] + r["cache_read_tokens"]
                  + r["cache_write_5m"] + r["cache_write_1h"])
        cat = sess_cat.get(r["session_id"], "misc")
        cost = tokens.turn_cost(r, idx) or 0.0
        cat_tok[wi][cat] += weight
        cat_cost[wi][cat] += cost
        sess_tok[wi][r["session_id"]] += weight
        tot_tok[wi] += weight
        tot_cost[wi] += cost

    labels = []
    for wi in range(n):
        if sess_tok[wi]:
            top_sid = max(sess_tok[wi], key=sess_tok[wi].get)
            lab = titles.get(top_sid) or CAT_STYLE.get(
                sess_cat.get(top_sid, "misc"), ("Misc",))[0]
        else:
            lab = "(no local transcript)"
        labels.append(lab)
    return cat_tok, cat_cost, tot_tok, tot_cost, labels


def _turn_span_hours(conn, windows):
    """Active duration proxy per window = span of turn timestamps within it."""
    starts = [w["start"] for w in windows]
    ends = [w["end"] for w in windows]
    lo = [None] * len(windows)
    hi = [None] * len(windows)
    for r in conn.execute("SELECT ts FROM turns"):
        t = tokens._parse_iso(r["ts"])
        wi = _bucket_index(starts, ends, t)
        if wi is None:
            continue
        if lo[wi] is None or t < lo[wi]:
            lo[wi] = t
        if hi[wi] is None or t > hi[wi]:
            hi[wi] = t
    out = []
    for wi in range(len(windows)):
        if lo[wi] and hi[wi]:
            out.append(max(0.1, min(5.0, (hi[wi] - lo[wi]).total_seconds() / 3600.0)))
        else:
            out.append(0.5)
    return out


def _credit_deltas(conn, windows):
    """Positive deltas of extra_used_credits, bucketed into the active window.

    A monthly rollover resets the counter (a large drop); we ignore negative deltas
    so the reset never registers as spend. Returns per-window summed credit units."""
    starts = [w["start"] for w in windows]
    ends = [w["end"] for w in windows]
    per = [0.0] * len(windows)
    prev = None
    for r in conn.execute(
        """SELECT captured_at, extra_used_credits FROM snapshots
            WHERE status='ok' AND extra_used_credits IS NOT NULL
            ORDER BY captured_at"""
    ):
        cur = r["extra_used_credits"]
        if prev is not None and cur > prev:
            t = tokens._parse_iso(r["captured_at"])
            wi = _bucket_index(starts, ends, t)
            if wi is not None:
                per[wi] += cur - prev
        prev = cur
    return per


def _weekly_windows(conn):
    """Merged weekly windows: {start, end, keys}. end = reset instant."""
    rows = conn.execute(
        """SELECT substr(weekly_resets_at,1,16) AS k, MAX(weekly_resets_at) AS reset
             FROM snapshots
            WHERE status='ok' AND weekly_resets_at IS NOT NULL
            GROUP BY k"""
    ).fetchall()
    ends = []
    keys = []
    for r in rows:
        e = tokens._parse_iso(r["reset"])
        if e:
            ends.append(e)
            keys.append(r["k"])
    if not ends:
        return []
    cid_of = _cluster(ends, WEEK_MERGE_EPS)
    groups = defaultdict(list)
    for i in range(len(ends)):
        groups[cid_of[i]].append(i)
    weeks = []
    for members in groups.values():
        end = max(ends[i] for i in members)
        weeks.append({"start": end - timedelta(days=7), "end": end,
                      "keys": [keys[i] for i in members]})
    weeks.sort(key=lambda w: w["start"])
    return weeks


def _weekly_trajectory(conn, wk):
    """Real (dayFrac, pct) samples across a weekly window, thinned to changes."""
    rows = conn.execute(
        """SELECT captured_at, weekly_pct FROM snapshots
            WHERE status='ok' AND weekly_pct IS NOT NULL
              AND substr(weekly_resets_at,1,16) IN ({})
            ORDER BY captured_at""".format(
            ",".join("?" * len(wk["keys"])))
        , wk["keys"]
    ).fetchall()
    pts = []
    last_pct = None
    for r in rows:
        t = tokens._parse_iso(r["captured_at"])
        if not t:
            continue
        frac = max(0.0, min(7.0, (t - wk["start"]).total_seconds() / DAY))
        pct = r["weekly_pct"]
        if last_pct is None or pct != last_pct:
            pts.append({"d": round(frac, 3), "pct": pct})
            last_pct = pct
    if rows:                                   # always pin the final observed level
        t = tokens._parse_iso(rows[-1]["captured_at"])
        frac = max(0.0, min(7.0, (t - wk["start"]).total_seconds() / DAY))
        if not pts or pts[-1]["d"] != round(frac, 3):
            pts.append({"d": round(frac, 3), "pct": rows[-1]["weekly_pct"]})
    return pts


def build(conn) -> dict:
    windows = _merge_session_windows(conn)
    cat_tok, cat_cost, tot_tok, tot_cost, labels = _per_window_tokens_cost(conn, windows)
    durs = _turn_span_hours(conn, windows)
    credits = _credit_deltas(conn, windows)
    weeks_meta = _weekly_windows(conn)

    # map each session window to the weekly window containing its start: a window
    # straddling the weekly reset is filed under the week its work began in
    wk_starts = [w["start"] for w in weeks_meta]
    wk_ends = [w["end"] for w in weeks_meta]

    week_sessions = defaultdict(list)
    for wi, w in enumerate(windows):
        peak = w["peak_session_pct"]
        # dominant category by token share
        if cat_tok[wi]:
            dom = max(cat_tok[wi], key=cat_tok[wi].get)
        else:
            dom = "misc"
        split = sorted(
            ({"cat": c, "tokens": cat_tok[wi][c], "cost": round(cat_cost[wi][c], 2)}
             for c in cat_tok[wi]),
            key=lambda x: -x["tokens"])
        wkidx = _bucket_index(wk_starts, wk_ends, w["start"])
        if wkidx is None:
            wkidx = len(weeks_meta) - 1 if weeks_meta else 0
        wk = weeks_meta[wkidx] if weeks_meta else None
        day_frac = (max(0.0, min(7.0, (w["end"] - wk["start"]).total_seconds() / DAY))
                    if wk else 0.0)
        seg = [v for k, v in w["weekly_by_wk"].items() if wk and k in wk["keys"]]
        w0 = min((a for a, _ in seg), default=w["weekly_at_start"])
        w1 = max((b for _, b in seg), default=w["weekly_at_end"])
        weekly_delta = max(0.0, w1 - w0)
        cred = credits[wi]
        sess = {
            "weekIdx": wkidx,
            "dayIdx": min(6, int(day_frac)),
            "dayFrac": round(day_frac, 3),
            "start": w["start"].isoformat(),
            "end": w["end"].isoformat(),
            "durH": round(durs[wi], 2),
            "sessionPct": round(peak, 1),
            "weeklyPct": round(weekly_delta, 1),
            "weeklyAtStart": round(w0, 1),
            "weeklyAtEnd": round(w1, 1),
            "catKey": dom,
            "tokens": tot_tok[wi],
            "cost": round(tot_cost[wi], 2),
            "catSplit": split,
            "label": labels[wi],
            "wall": peak >= 100,
            "nearWall": 90 <= peak < 100,
            "onCredits": cred > 0,
            "creditUsd": round(cred, 2),
        }
        week_sessions[wkidx].append(sess)

    split = tokens.latest_surface_split(conn)
    cc_share = split.get("claude_code", 0)
    chat_share = split.get("chat", 0)

    weeks = []
    for wi, wk in enumerate(weeks_meta):
        sess = sorted(week_sessions.get(wi, []), key=lambda s: s["start"])
        traj = _weekly_trajectory(conn, wk)
        weekly_peak = max((p["pct"] for p in traj), default=0.0)
        # every observed window contributes its weekly delta (credit spend is 5-hour
        # wall overage, not a separate weekly-ceiling entry); the remainder is
        # pre-capture baseline + growth outside any tracked window (incl. Chat).
        observed = sum(s["weeklyPct"] for s in sess)
        weeks.append({
            "idx": wi,
            "start": wk["start"].isoformat(),
            "end": wk["end"].isoformat(),
            "weeklyPeak": round(weekly_peak, 1),
            "unobserved": round(max(0.0, weekly_peak - observed), 1),
            "creditUsd": round(sum(s["creditUsd"] for s in sess), 2),
            "trajectory": traj,
            "ccShare": cc_share,
            "chatShare": chat_share,
            "sessions": sess,
        })

    # categories actually present in the data (for legend + colors)
    catsum = tokens.category_summary(conn)      # {cat: [sessions, turns]}
    cats = []
    for key in [k for k in CAT_STYLE if k in catsum] + \
              [k for k in catsum if k not in CAT_STYLE]:
        name, var = CAT_STYLE.get(key, (key.title(), "--cat-misc"))
        s = catsum.get(key, [0, 0])
        cats.append({"key": key, "name": name, "varname": var,
                     "sessions": s[0], "turns": s[1]})

    by_cat, by_model, total = tokens.cost_summary(conn)
    n_snaps = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE status='ok'").fetchone()[0]

    # newest reading, for the live status strip + reset countdowns
    cur = conn.execute(
        """SELECT captured_at, session_pct, session_resets_at,
                  weekly_pct, weekly_resets_at, extra_used_credits
             FROM snapshots WHERE status='ok'
            ORDER BY captured_at DESC LIMIT 1"""
    ).fetchone()
    current = dict(cur) if cur else None

    return {
        "generated_at": tokens._now(),
        "demo": False,
        "current": current,
        "surface_split": split,
        "credit_unit": "cr",
        "cats": cats,
        "cost": {
            "total": round(total, 2),
            "by_category": {k: round(v, 2) for k, v in by_cat.items()},
            "by_model": {k: round(v, 2) for k, v in by_model.items()},
        },
        "weeks": weeks,
        "meta": {
            "n_windows": len(windows),
            "n_weeks": len(weeks),
            "n_snapshots_ok": n_snaps,
            "weekly_start": weeks[0]["start"] if weeks else None,
            "weekly_reset": weeks[-1]["end"] if weeks else None,
        },
    }


def main():
    conn = tokens._connect()
    data = build(conn)
    conn.close()
    pretty = "--pretty" in sys.argv
    print(json.dumps(data, indent=2 if pretty else None))


if __name__ == "__main__":
    main()
