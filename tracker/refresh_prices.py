"""Price-book reconciler.

Fetches the LiteLLM price map (the source the price book is seeded from) and versions
the `prices` table when a rate has changed: the open row is closed (valid_to = today) and
a new open row is inserted (valid_from = today). Past turns keep the price in effect then;
history is never edited. Stdlib only. Run occasionally (a monthly timer) — prices change
rarely and this makes one network call.

Model strings in `turns` (e.g. `claude-opus-5`, `claude-haiku-4-5-20251001`) are used as-is
as the bare first-party LiteLLM keys. Cache rates: cache_read and cache_write_5m come from
LiteLLM; cache_write_1h is derived as 2x input (Anthropic's 1-hour-cache rate, which LiteLLM
does not carry separately).

Footguns defused:
  * a failed/partial fetch must never trigger versioning -> validate the payload resolves at
    least one model before touching the DB; on any network/parse error, exit 0 with no change.
  * float noise in the source -> compare $/MTok rounded to 6 dp.
  * only bare first-party keys are used (never us./eu./bedrock partner-priced variants).
  * a model we price but LiteLLM dropped -> left untouched (keep its price), warned.
  * re-running is a no-op once the open row already matches.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone

import db
import tokens

LITELLM_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
               "model_prices_and_context_window.json")
COMPONENTS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def fetch_litellm(url=LITELLM_URL, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "usage-tracker/refresh_prices"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def target_prices(litellm, model):
    """Current $/MTok per component for one model from the LiteLLM map, or None if absent."""
    m = litellm.get(model)
    if not m or m.get("input_cost_per_token") is None or m.get("output_cost_per_token") is None:
        return None
    inp = m["input_cost_per_token"]
    out = m["output_cost_per_token"]
    cache_read = m.get("cache_read_input_token_cost")
    cache_5m = m.get("cache_creation_input_token_cost")
    per_tok = {
        "input": inp,
        "output": out,
        "cache_read": cache_read if cache_read is not None else inp * 0.10,
        "cache_write_5m": cache_5m if cache_5m is not None else inp * 1.25,
        "cache_write_1h": inp * 2.00,          # not in LiteLLM; Anthropic 1-hour rate
    }
    return {c: round(v * 1e6, 6) for c, v in per_tok.items()}


def models_to_price(conn):
    """Priced models + any real model seen in transcripts (so new models get added)."""
    seeded = set(tokens._BASE)
    used = {r[0] for r in conn.execute("SELECT DISTINCT model FROM turns")
            if r[0] and r[0] != "<synthetic>"}
    return sorted(seeded | used)


def reconcile(conn, litellm, change_date, dry_run=False):
    changes, missing = [], []
    open_rows = {(r["model"], r["component"]): r["usd_per_mtok"]
                 for r in conn.execute(
                     "SELECT model, component, usd_per_mtok FROM prices WHERE valid_to IS NULL")}
    for model in models_to_price(conn):
        target = target_prices(litellm, model)
        if target is None:
            missing.append(model)
            continue
        for comp in COMPONENTS:
            want = target[comp]
            have = open_rows.get((model, comp))
            if have is None:
                changes.append((model, comp, None, want, "add"))
                if not dry_run:
                    conn.execute(
                        "INSERT INTO prices (model, component, usd_per_mtok, valid_from, valid_to, source) "
                        "VALUES (?,?,?,?,?,?)",
                        (model, comp, want, change_date, None, tokens.PRICE_SOURCE))
            elif round(have, 6) != want:
                changes.append((model, comp, have, want, "version"))
                if not dry_run:
                    conn.execute(
                        "UPDATE prices SET valid_to=? WHERE model=? AND component=? AND valid_to IS NULL",
                        (change_date, model, comp))
                    conn.execute(
                        "INSERT INTO prices (model, component, usd_per_mtok, valid_from, valid_to, source) "
                        "VALUES (?,?,?,?,?,?)",
                        (model, comp, want, change_date, None, tokens.PRICE_SOURCE))
    if not dry_run:
        conn.commit()
    return changes, missing


def refresh(dry_run=False, verbose=True):
    try:
        litellm = fetch_litellm()
    except Exception as e:                       # network/parse — never version on a bad fetch
        if verbose:
            print(f"refresh_prices: fetch failed ({e}); no changes.")
        return {"ok": False, "changes": [], "missing": []}
    conn = tokens._connect()                     # ensures schema + seeds if empty
    # sanity gate: at least one of our models must resolve, else treat as a bad payload
    if not any(target_prices(litellm, m) for m in tokens._BASE):
        conn.close()
        if verbose:
            print("refresh_prices: payload resolved no known models; no changes.")
        return {"ok": False, "changes": [], "missing": []}
    change_date = datetime.now(timezone.utc).date().isoformat()
    changes, missing = reconcile(conn, litellm, change_date, dry_run=dry_run)
    if not dry_run:                          # heartbeat for the poller's pipeline check
        with conn:
            conn.execute(
                "INSERT INTO harvest_meta (k, v) VALUES ('last_price_refresh', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (datetime.now(timezone.utc).isoformat(),))
    conn.close()
    if verbose:
        tag = "(dry-run) " if dry_run else ""
        if changes:
            print(f"refresh_prices: {tag}{len(changes)} change(s) on {change_date}:")
            for model, comp, old, new, kind in changes:
                print(f"  {kind:8s} {model} {comp}: {old} -> {new} $/MTok")
        else:
            print(f"refresh_prices: {tag}prices up to date.")
        if missing:
            print(f"  warning: priced but absent from LiteLLM (left unchanged): {missing}")
    return {"ok": True, "changes": changes, "missing": missing}


if __name__ == "__main__":
    refresh(dry_run="--dry-run" in sys.argv)
