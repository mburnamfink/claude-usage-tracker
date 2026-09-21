"""Local live dashboard server. Stdlib only.

Serves the quota-telemetry page and a JSON endpoint the page polls, so the display
updates as the poller writes new snapshots and as windows reset. This is the live
counterpart to report.py (which inlines a static snapshot for a Claude Artifact).
The same template drives both: with no inlined data it fetches /api/data and polls.

Binds to localhost only — usage data never leaves the machine.

Run: ~/work/bin/python tracker/serve.py [--port 8787] [--open]
"""
import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import export
import tokens

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "display" / "quota-telemetry.html"


def _build_data() -> bytes:
    conn = tokens._connect()
    try:
        data = export.build(conn)
    finally:
        conn.close()
    return json.dumps(data).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/quota-telemetry.html"):
            try:
                body = TEMPLATE.read_bytes()
            except FileNotFoundError:
                self._send(500, b"template missing", "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/api/data":
            try:
                body = _build_data()
            except Exception as exc:                       # surface build errors to the page
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self._send(500, body, "application/json")
                return
            self._send(200, body, "application/json")
        elif path == "/api/health":
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def log_message(self, fmt, *args):                     # quiet; one line per request
        print(f"  {self.address_string()} {fmt % args}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"quota telemetry live at {url}  (Ctrl-C to stop)")
    print(f"  page:  {TEMPLATE}")
    print(f"  data:  {tokens.db.DB_PATH}")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        httpd.shutdown()


if __name__ == "__main__":
    main()
