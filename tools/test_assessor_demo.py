"""Tests for additive synthetic demo; only temporary directories are touched."""
# SQLite's transaction context does not close the connection.
# Explicit closure is required before Windows can delete temporary databases.
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

import assessor_demo as demo


class AssessorDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / '.assessor-demo'

    def tearDown(self):
        self.tmp.cleanup()

    def create(self):
        demo.create_demo(self.path, 'Only-for-local-testing-123')

    def test_create_has_four_rows(self):
        self.create()
        self.assertEqual(demo.dashboard.check_database(self.path / 'events.db'), 4)
        self.assertTrue(demo.dashboard.password_matches('Only-for-local-testing-123', demo.load_demo(self.path)['password_hash']))

    def test_demo_counts(self):
        self.create()
        data = demo.dashboard.read_page(self.path / 'events.db', ZoneInfo('Australia/Sydney'), {})
        self.assertEqual(data['stats']['granted'], 1)
        self.assertEqual(data['stats']['denied'], 3)

    def test_existing_folder_not_overwritten(self):
        self.create()
        before = (self.path / 'events.db').read_bytes()
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual(before, (self.path / 'events.db').read_bytes())

    def test_invalid_password_makes_no_folder(self):
        with self.assertRaises(ValueError):
            demo.create_demo(self.path, 'tiny')
        self.assertFalse(self.path.exists())

    def test_add_new_event(self):
        self.create()
        self.assertEqual(demo.add_scenario(self.path, 'mismatch'), 5)
        with closing(sqlite3.connect(str(self.path / 'events.db'))) as c, c:
            row=c.execute('SELECT decision,raw FROM events WHERE id=5').fetchone()
        self.assertEqual(row[0], 'DENIED')
        self.assertTrue(json.loads(row[1])['synthetic'])

    def test_reject_unknown_scenario(self):
        self.create()
        with self.assertRaises(ValueError):
            demo.add_scenario(self.path, 'unknown-setting')
        self.assertEqual(demo.dashboard.check_database(self.path / 'events.db'), 4)

    def test_no_marker_refuses_write(self):
        self.create()
        (self.path / 'DEMO-ONLY.json').unlink()
        with self.assertRaises(ValueError):
            demo.add_scenario(self.path, 'granted')

    def test_settings_cannot_point_to_live_db(self):
        self.create()
        settings = self.path / 'settings.json'
        values = json.loads(settings.read_text())
        values['db_path'] = str(Path(self.tmp.name) / 'live.db')
        settings.write_text(json.dumps(values))
        with self.assertRaises(ValueError):
            demo.load_demo(self.path)
        self.assertFalse(Path(values['db_path']).exists())

    def test_fixture_contains_no_photo_or_real_uid(self):
        self.create()
        with closing(sqlite3.connect(str(self.path / 'events.db'))) as c, c:
            rows=c.execute('SELECT card_uid,snapshot,raw FROM events').fetchall()
        self.assertTrue(all(uid.startswith('DEMO-') and image is None and json.loads(raw)['synthetic'] for uid,image,raw in rows))


if __name__ == '__main__':
    unittest.main(verbosity=2)
