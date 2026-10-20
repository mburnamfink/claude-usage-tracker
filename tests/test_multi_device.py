"""Multi-device ingest/harvest acceptance checks, against a throwaway DB.

Run: ~/work/bin/python -m unittest discover -s tests -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tracker"))

import alert   # noqa: E402
import db      # noqa: E402
import export  # noqa: E402
import ingest  # noqa: E402
import poll    # noqa: E402
import tokens  # noqa: E402

SAMPLE = json.loads((ROOT / "docs" / "sample-usage-response.json").read_text())


def turn_line(mid, ts="2026-10-19T12:00:00.000Z", out=100, session="s1"):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "sessionId": session,
        "cwd": "/home/michael/dev/x", "isSidechain": False,
        "message": {"id": mid, "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 10, "output_tokens": out,
                              "cache_read_input_tokens": 0}},
    }) + "\n"


class MultiDeviceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.inbox = self.tmp / "inbox"
        self.local = self.tmp / "local-projects"
        self.local.mkdir()
        cfg = self.tmp / "alert_config.json"
        cfg.write_text(json.dumps({"device_name": "piehole", "dashboard_health_url": None}))
        self._saved = (db.DB_PATH, poll.RAW_DIR, alert.CONFIG_PATH, tokens.PROJECTS)
        db.DB_PATH = self.tmp / "usage.db"
        poll.RAW_DIR = self.tmp / "raw"
        alert.CONFIG_PATH = cfg
        tokens.PROJECTS = self.local

    def tearDown(self):
        db.DB_PATH, poll.RAW_DIR, alert.CONFIG_PATH, tokens.PROJECTS = self._saved
        self._tmp.cleanup()

    # -- helpers --
    def transcript(self, device, name, text):
        p = self.inbox / device / "projects" / "-home-michael-dev-x" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def snap(self, device, captured_at, credits=None, http_code=200):
        resp = json.loads(json.dumps(SAMPLE))
        if credits is not None:
            resp.setdefault("extra_usage", {})["used_credits"] = credits
        out = self.inbox / device / "outbox"
        out.mkdir(parents=True, exist_ok=True)
        stamp = captured_at.replace(":", "").replace("-", "")[:15]
        (out / f"snap-{stamp}Z.json").write_text(json.dumps(
            {"captured_at": captured_at, "http_code": http_code, "neterr": None,
             "response": resp if http_code == 200 else None}))

    def q(self, sql, *args):
        conn = sqlite3.connect(db.DB_PATH)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def totals(self):
        return self.q("SELECT COUNT(*), SUM(output_tokens) FROM turns")[0]

    def raw_lines(self):
        return sum(len(p.read_text().splitlines()) for p in poll.RAW_DIR.glob("*.jsonl"))

    # -- acceptance checks --
    def test_reingest_is_idempotent(self):
        self.transcript("desktop", "a.jsonl",
                        turn_line("m1") + turn_line("m2") + turn_line("m1"))
        self.snap("desktop", "2026-10-19T12:00:00+00:00")
        self.snap("desktop", "2026-10-19T12:05:00+00:00")
        r1 = ingest.run(inbox=self.inbox)
        self.assertEqual(r1["snapshots_stored"], 2)
        first = (self.totals(), self.q("SELECT COUNT(*) FROM snapshots"), self.raw_lines())

        # an edge retry re-sends the same snapshots; a lost cursor forces a full re-read
        self.snap("desktop", "2026-10-19T12:00:00+00:00")
        self.snap("desktop", "2026-10-19T12:05:00Z")      # same instant, other spelling
        conn = sqlite3.connect(db.DB_PATH); conn.execute("DELETE FROM harvest_files")
        conn.commit(); conn.close()
        r2 = ingest.run(inbox=self.inbox)

        self.assertEqual(r2["snapshots_duplicate"], 2)
        self.assertEqual(first, (self.totals(), self.q("SELECT COUNT(*) FROM snapshots"),
                                 self.raw_lines()))
        self.assertEqual(self.totals(), (2, 200))
        self.assertEqual(list((self.inbox / "desktop" / "outbox").iterdir()), [])

    def test_same_transcript_on_two_devices_counts_once(self):
        text = turn_line("m1") + turn_line("m2", out=50)
        self.transcript("desktop", "a.jsonl", text)
        self.transcript("laptop", "a.jsonl", text)
        (self.local / "p").mkdir()
        (self.local / "p" / "a.jsonl").write_text(text)
        ingest.run(inbox=self.inbox)
        self.assertEqual(self.totals(), (2, 150))
        self.assertEqual(self.q("SELECT COUNT(*) FROM harvest_files")[0][0], 3)

    def test_partial_trailing_line_waits_for_completion(self):
        full, second = turn_line("m1"), turn_line("m2", out=7)
        p = self.transcript("desktop", "a.jsonl", full + second[:40])
        ingest.run(inbox=self.inbox)
        self.assertEqual(self.totals(), (1, 100))
        with open(p, "a") as f:                       # rsync --append-verify finishing the line
            f.write(second[40:])
        ingest.run(inbox=self.inbox)
        self.assertEqual(self.totals(), (2, 107))

    def test_credit_deltas_ignore_interleaved_stale_readings(self):
        t0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        readings = [(0, "desktop", 10.0), (30, "laptop", 9.9), (60, "desktop", 10.0),
                    (90, "laptop", 10.5),
                    (120, "desktop", 1.0),            # monthly rollover
                    (150, "desktop", 3.0)]
        for sec, dev, cr in readings:
            self.snap(dev, (t0 + timedelta(seconds=sec)).isoformat(), credits=cr)
        ingest.run(inbox=self.inbox)
        conn = tokens._connect()
        try:
            window = [{"start": t0 - timedelta(hours=1), "end": t0 + timedelta(hours=4)}]
            self.assertAlmostEqual(export._credit_deltas(conn, window)[0], 0.5 + 2.0)
        finally:
            conn.close()

    def test_malformed_outbox_file_is_quarantined(self):
        out = self.inbox / "desktop" / "outbox"
        out.mkdir(parents=True)
        (out / "snap-20261019T115900Z.json").write_text("{not json")
        self.snap("desktop", "2026-10-19T12:00:00+00:00")
        r = ingest.run(inbox=self.inbox)
        self.assertEqual((r["snapshots_rejected"], r["snapshots_stored"]), (1, 1))
        self.assertTrue((self.inbox / "desktop" / "rejected" /
                         "snap-20261019T115900Z.json").exists())
        self.assertEqual(list(out.iterdir()), [])

    def test_edge_network_error_is_stored_as_failure(self):
        out = self.inbox / "laptop" / "outbox"
        out.mkdir(parents=True)
        (out / "snap-20261019T120000Z.json").write_text(json.dumps(
            {"captured_at": "2026-10-19T12:00:00Z", "http_code": "000",
             "neterr": "Could not resolve host", "response": None}))
        ingest.run(inbox=self.inbox)
        self.assertEqual(self.q("SELECT device, status, http_code FROM snapshots"),
                         [("laptop", "network_error", None)])

    def test_migrates_single_device_db(self):
        here = self.local / "p" / "here.jsonl"
        here.parent.mkdir()
        here.write_text(turn_line("m1"))
        conn = sqlite3.connect(db.DB_PATH)
        conn.executescript(f"""
            CREATE TABLE snapshots (captured_at TEXT NOT NULL, status TEXT NOT NULL,
              http_code INTEGER, session_pct REAL, session_resets_at TEXT, weekly_pct REAL,
              weekly_resets_at TEXT, weekly_opus_pct REAL, extra_usage_pct REAL,
              extra_used_credits REAL, cc_weekly_share REAL, weekly_breakdown_json TEXT);
            INSERT INTO snapshots (captured_at, status) VALUES ('2026-10-01T00:00:00+00:00','ok');
            CREATE TABLE turns (message_id TEXT PRIMARY KEY, ts TEXT, model TEXT,
              input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
              cache_read_tokens INTEGER DEFAULT 0, cache_write_5m INTEGER DEFAULT 0,
              cache_write_1h INTEGER DEFAULT 0, cwd TEXT, is_sidechain INTEGER DEFAULT 0,
              session_id TEXT, source_file TEXT);
            INSERT INTO turns (message_id, source_file) VALUES ('m1', '{here}'),
                                                               ('m9', '/gone/old.jsonl');
            CREATE TABLE harvest_files (path TEXT PRIMARY KEY, dev_ino TEXT, size INTEGER,
              mtime_ns INTEGER, byte_offset INTEGER, n_turns INTEGER DEFAULT 0,
              updated_at TEXT);
            INSERT INTO harvest_files (path) VALUES ('/gone/old.jsonl'), ('{here}');
        """)
        conn.commit(); conn.close()

        tokens._connect().close()
        tokens._connect().close()                     # second open must be a no-op

        self.assertEqual(self.q("SELECT device FROM snapshots"), [("legacy",)])
        self.assertEqual(dict(self.q("SELECT message_id, device FROM turns")),
                         {"m1": "piehole", "m9": "legacy"})
        self.assertEqual(self.q("SELECT path FROM harvest_files"), [(str(here),)])
        with self.assertRaises(sqlite3.IntegrityError):
            c = sqlite3.connect(db.DB_PATH)
            c.execute("INSERT INTO snapshots (captured_at, status, device) "
                      "VALUES ('2026-10-01T00:00:00+00:00','ok','legacy')")

    def test_export_reports_devices(self):
        now = datetime.now(timezone.utc)
        self.transcript("desktop", "a.jsonl",
                        turn_line("m1", ts=now.strftime("%Y-%m-%dT%H:%M:%S.000Z")))
        self.snap("laptop", now.isoformat())
        ingest.run(inbox=self.inbox)
        conn = tokens._connect()
        try:
            devs = {d["name"]: d for d in export.build(conn)["devices"]}
        finally:
            conn.close()
        self.assertEqual(set(devs), {"desktop", "laptop"})
        self.assertEqual(devs["desktop"]["tokens_7d"], 110)


if __name__ == "__main__":
    unittest.main()
