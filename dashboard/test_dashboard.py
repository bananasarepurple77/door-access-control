"""Standard-library tests. Uses ONLY a temporary sample database, not events.db.
Run: python -m unittest -v test_dashboard.py
"""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

import dashboard_app as app


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "sample.db"
        self.zone = ZoneInfo("Australia/Sydney")
        with sqlite3.connect(self.db) as connection:
            connection.execute("""CREATE TABLE events (
                id INTEGER PRIMARY KEY, received_ts TEXT NOT NULL, ts TEXT,
                card_uid TEXT, name TEXT, decision TEXT, reason TEXT,
                similarity REAL, snapshot TEXT, latency_s REAL, raw TEXT NOT NULL
            )""")
            rows = [
                (21, "2026-10-29T21:56:08+00:00", "2026-10-29T21:56:08+00:00", "111", "Demo user", "GRANTED", "verified", 99.89, "secret.jpg", 8.88, "private payload"),
                (22, "2026-10-29T21:56:36+00:00", "2026-10-29T21:56:36+00:00", "222", None, "DENIED", "unknown card", None, None, 0.0, "private payload"),
                (23, "2026-10-29T21:57:03+00:00", "2026-10-29T21:57:03+00:00", "111", "Demo user", "GRANTED", "verified", 99.98, "secret.jpg", 8.97, "private payload"),
                (24, "2026-10-29T21:57:23+00:00", "2026-10-29T21:57:22+00:00", "111", "Demo user", "DENIED", "no face detected in snapshot", None, "secret.jpg", 8.3, "private payload"),
                (25, "2026-10-29T21:59:18+00:00", "2026-10-29T21:59:18+00:00", "111", "Demo user", "GRANTED", "verified", 99.19, "secret.jpg", 9.06, "private payload"),
            ]
            connection.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, args=None):
        return app.read_page(self.db, self.zone, args or {})

    def test_existing_schema_and_count(self):
        self.assertEqual(app.check_database(self.db), 5)

    def test_missing_db_is_not_created(self):
        missing = Path(self.tmp.name) / "missing.db"
        with self.assertRaises(ValueError):
            app.open_database(missing)
        self.assertFalse(missing.exists())

    def test_readonly_blocks_writes(self):
        connection = app.open_database(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM events")
        finally:
            connection.close()
        self.assertEqual(app.check_database(self.db), 5)

    def test_read_does_not_change_db_bytes(self):
        before = self.db.read_bytes()
        self.read()
        app.read_csv(self.db, self.zone, {})
        self.assertEqual(before, self.db.read_bytes())

    def test_counts_and_newest_first(self):
        result = self.read()
        self.assertEqual(result["stats"]["total"], 5)
        self.assertEqual(result["stats"]["granted"], 3)
        self.assertEqual(result["stats"]["denied"], 2)
        self.assertEqual(result["stats"]["other"], 0)
        self.assertEqual([e["id"] for e in result["events"]], [25,24,23,22,21])
        self.assertAlmostEqual(result["stats"]["mean_duration"], 7.042)

    def test_sydney_times(self):
        self.assertIn("30 Oct 2026, 08:59:18 AEDT", self.read()["events"][0]["time_local"])

    def test_denied_filter(self):
        result = self.read({"decision": "DENIED"})
        self.assertEqual([e["id"] for e in result["events"]], [24,22])
        self.assertEqual(result["stats"]["total"], 2)

    def test_name_filter_case_insensitive(self):
        self.assertEqual(self.read({"q": "DEMO"})["stats"]["total"], 4)

    def test_uid_filter(self):
        self.assertEqual(self.read({"q": "222"})["stats"]["total"], 1)

    def test_search_not_sql_injection(self):
        self.assertEqual(self.read({"q": "' OR 1=1 --"})["stats"]["total"], 0)

    def test_literal_wildcard_is_not_pattern(self):
        self.assertEqual(self.read({"q": "%"})["stats"]["total"], 0)

    def test_local_dates_not_utc_dates(self):
        self.assertEqual(self.read({"from": "2026-10-30", "to": "2026-10-30"})["stats"]["total"],5)
        self.assertEqual(self.read({"from": "2026-10-29", "to": "2026-10-29"})["stats"]["total"],0)

    def test_dst_day_is_23_hours(self):
        from datetime import datetime
        _, params = app.build_filter({"from":"2026-10-04","to":"2026-10-04"}, self.zone)
        self.assertEqual((datetime.fromisoformat(params[1])-datetime.fromisoformat(params[0])).total_seconds(),23*3600)

    def test_bad_parameters_rejected(self):
        for query in ({"decision":"HACK"},{"from":"yesterday"},{"from":"2026-11-01","to":"2026-10-01"},{"page":"-1"},{"page":"x"},{"q":"x"*129}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.read(query)

    def test_zero_latency_not_missing(self):
        event=self.read({"q":"222"})["events"][0]
        self.assertEqual(event["latency_s"],0.0)
        self.assertIsNone(event["similarity"])

    def test_raw_and_images_not_returned(self):
        data=json.dumps(self.read())
        self.assertNotIn('secret.jpg',data)
        self.assertNotIn('private payload',data)
        self.assertNotIn('"raw":',data)
        self.assertNotIn('"snapshot":',data)

    def test_unrecognised_record_and_receipt_fallback(self):
        with sqlite3.connect(self.db) as c:
            c.execute("INSERT INTO events (id,received_ts,raw) VALUES (26,?,?)",("2026-10-29T22:00:00+00:00","broken"))
        result=self.read({"decision":"OTHER"})
        self.assertEqual(result["stats"]["other"],1)
        self.assertEqual(result["events"][0]["time_source"],"receipt")
        self.assertEqual(result["events"][0]["decision"],"UNKNOWN")

    def test_nonfinite_numeric_values(self):
        self.assertIsNone(app.finite_number(float('nan')))
        self.assertIsNone(app.finite_number(float('inf')))
        self.assertIsNone(app.finite_number('not-a-number'))

    def test_cloud_error_redaction(self):
        self.assertNotIn("SECRET", app.safe_reason("snapshot failure: https://secret.example/SECRET"))
        self.assertNotIn("SECRET", app.safe_reason("verification error: token=SECRET"))
        self.assertNotIn("SECRET", app.safe_reason("failure https://example.com/SECRET"))

    def test_csv_has_filters_no_photos_or_raw(self):
        text=app.read_csv(self.db,self.zone,{"decision":"DENIED"})
        self.assertEqual(len(text.splitlines()),3)
        self.assertNotIn("GRANTED",text)
        self.assertNotIn("secret.jpg",text)
        self.assertNotIn("private payload",text)

    def test_csv_formula_neutralisation(self):
        for value in ('=1+1','+1','-1','@foo','\t=1','   =1'):
            self.assertTrue(app.csv_cell(value).startswith("'"))

    def test_password_roundtrip(self):
        hashed=app.password_hash("example-test-only-password")
        self.assertTrue(app.password_matches("example-test-only-password",hashed))
        self.assertFalse(app.password_matches("incorrect",hashed))
        self.assertFalse(app.password_matches("incorrect","invalid"))
        self.assertNotEqual(hashed,app.password_hash("example-test-only-password"))

    def test_bad_schema(self):
        path=Path(self.tmp.name)/"wrong.db"
        with sqlite3.connect(path) as c:
            c.execute("CREATE TABLE events (id INTEGER)")
        with self.assertRaises(ValueError):
            app.check_database(path)

    def test_json_settings_permissions(self):
        import os
        path=Path(self.tmp.name)/"settings.json"
        path.write_text(json.dumps({"username":"admin","password_hash":"abc","secret_key":"x"*64,"db_path":str(self.db),"timezone":"Australia/Sydney"}))
        path.chmod(0o600)
        self.assertEqual(app.load_settings(path)["username"],"admin")
        if os.name=="posix":
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                app.load_settings(path)

    def test_pagination(self):
        with sqlite3.connect(self.db) as c:
            for number in range(26,84):
                c.execute("INSERT INTO events (id,received_ts,decision,raw) VALUES (?,?,?,?)",(number,"2026-10-29T22:00:00+00:00","DENIED","{}"))
        first=self.read()
        second=self.read({"page":"2"})
        self.assertEqual(len(first["events"]),50)
        self.assertEqual(len(second["events"]),13)
        self.assertEqual(self.read({"page":"999"})["page"],2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
