"""Generate the self-contained dashboard by inlining usage.db data into the template.

Artifacts run under a strict CSP (no fetch/XHR), so the page cannot read SQLite at
runtime. This reads display/quota-telemetry.html, inlines export.build()'s JSON as
window.__QUOTA_DATA__ just before the page script, and writes a finished HTML file.
The seeded simulation stays reachable at ?demo=1 (and when no data is inlined).

Run: ~/work/bin/python tracker/report.py [-o display/quota-telemetry.generated.html]
"""
import argparse
import json
from pathlib import Path

import export
import tokens

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "display" / "quota-telemetry.html"
DEFAULT_OUT = ROOT / "display" / "quota-telemetry.generated.html"
ANCHOR = '<script>\n"use strict";'


def _embed(data: dict) -> str:
    # escape '<' so a stray "</script>" in any string can't close the tag early
    blob = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    return f"<script>window.__QUOTA_DATA__={blob};</script>\n"


def generate(out: Path) -> Path:
    template = TEMPLATE.read_text(encoding="utf-8")
    if ANCHOR not in template:
        raise SystemExit(f"anchor not found in {TEMPLATE}; template layout changed")
    conn = tokens._connect()
    data = export.build(conn)
    conn.close()
    html = template.replace(ANCHOR, _embed(data) + ANCHOR, 1)
    out.write_text(html, encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT,
                    help=f"output HTML path (default: {DEFAULT_OUT})")
    args = ap.parse_args()
    path = generate(args.out)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
