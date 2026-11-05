"""Offline tests only: temporary databases and mocked HTTPS requests."""
import io
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import ssl
import tempfile
import time
import unittest
from unittest import mock
from urllib import error

import webhook_worker as w


def cfg(db):
    return {"version": 1, "url": "https://example.invalid/webhook/SECRET", "format": "generic",
            "bearer_token": "", "source_id": "a" * 32, "pseudonym_key": "b" * 64, "db_path": str(db)}


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg(Path('/tmp/events.db'))
        self.row = {"id": 27, "ts": "2026-10-30T00:00:00+00:00", "received_ts": "2026-10-30T00:00:01+00:00",
                    "card_uid": "900000000001", "name": "Private Name", "decision": "DENIED",
                    "reason": "no face detected in snapshot", "similarity": 43.34, "latency_s": 8.3,
                    "raw": "secret", "snapshot": "secret.jpg"}

    def test_private_fields_omitted(self):
        text = json.dumps(w.make_payload(self.row, self.cfg))
        for sensitive in ('Private Name', '900000000001', 'secret.jpg', 'secret'):
            self.assertNotIn(sensitive, text)

    def test_card_reference_is_stable_but_keyed(self):
        p = w.make_payload(self.row, self.cfg)
        self.assertEqual(p['card_ref'], w.make_payload(self.row, self.cfg)['card_ref'])
        changed = dict(self.cfg, pseudonym_key='c' * 64)
        self.assertNotEqual(p['card_ref'], w.make_payload(self.row, changed)['card_ref'])
        self.assertEqual(len(p['card_ref']), 16)

    def test_stable_event_id(self):
        p = w.make_payload(self.row, self.cfg)
        self.assertEqual(p['event_id'], 'a' * 32 + ':27')
        self.assertEqual(p['event_row_id'], 27)

    def test_synthetic_test_has_no_identity(self):
        p = w.sample(self.cfg)
        self.assertTrue(p['test'])
        self.assertIsNone(p['card_ref'])
        self.assertIsNone(p['event_row_id'])
        self.assertTrue(p['event_id'].startswith('test-'))

    def test_exception_reason_is_sanitised(self):
        for reason, expected in [('snapshot failure: https://secret.invalid?key=secret', 'camera_unavailable'),
                                 ('verification error: password=secret', 'verification_error'),
                                 ('unexpected private detail', 'access_denied')]:
            p = w.make_payload(dict(self.row, reason=reason), self.cfg)
            self.assertEqual(p['reason'], expected)
            self.assertNotIn('secret', json.dumps(p))

    def test_reason_mapping(self):
        for value, code in [('unknown card','unknown_card'), ('face did not match cardholder','face_mismatch'),
                            ('revoked card','revoked_card')]:
            self.assertEqual(w.reason_code(value), code)

    def test_bad_numbers_become_null(self):
        for value in (None, 'nan', float('inf'), -1, 'nonsense', True):
            self.assertIsNone(w.number(value, 100))
        self.assertIsNone(w.number(101, 100))
        self.assertEqual(w.number('42.5', 100), 42.5)

    def test_timestamps_are_normalised(self):
        self.assertEqual(w.safe_time('2026-10-30T11:00:00+11:00'), '2026-10-30T00:00:00.000+00:00')
        self.assertIsNone(w.safe_time('https://secret'))
        self.assertIsNone(w.safe_time('2026-10-30T00:00:00'))

    def test_generic_payload(self):
        p = w.make_payload(self.row, self.cfg)
        self.assertEqual(w.formatted(p, 'generic'), p)

    def test_discord_format(self):
        p = w.formatted(w.make_payload(self.row, self.cfg), 'discord')
        self.assertIn('Cabinet access DENIED', p['content'])
        self.assertEqual(p['allowed_mentions'], {'parse': []})
        self.assertLess(len(p['content']), 2000)

    def test_slack_format(self):
        p = w.formatted(w.sample(self.cfg), 'slack')
        self.assertIn('[TEST]', p['text'])
        self.assertFalse(p['mrkdwn'])


class ConfigTests(unittest.TestCase):
    def test_https_required(self):
        for url in ('http://example.com/hook', 'file:///etc/passwd', 'https://a:b@example.com/hook',
                    'https://example.com/#fragment', 'https://example.com/\nsecret', 'https://example.com:bad'):
            with self.assertRaises(ValueError):
                w.validate_url(url, 'generic')

    def test_generic_valid_url(self):
        self.assertEqual(w.validate_url('https://example.com/hook?token=a', 'generic'), 'https://example.com/hook?token=a')

    def test_unknown_format(self):
        with self.assertRaises(ValueError):
            w.validate_url('https://example.com/hook', 'teams')

    def test_discord_wait_added(self):
        url = w.validate_url('https://discord.com/api/webhooks/123/abc?wait=false&thread_id=456', 'discord')
        self.assertIn('wait=true', url)
        self.assertIn('thread_id=456', url)
        self.assertNotIn('wait=false', url)

    def test_discord_and_slack_host_validation(self):
        for mode in ('discord', 'slack'):
            with self.assertRaises(ValueError):
                w.validate_url('https://example.com/webhooks/123', mode)
        self.assertEqual(w.validate_url('https://hooks.slack.com/services/test', 'slack'),
                         'https://hooks.slack.com/services/test')

    def test_config_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'cfg.json'
            c = cfg(Path(d) / 'events.db')
            w.save_config(c, p)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(w.load_config(p), c)
            os.chmod(p, 0o644)
            with self.assertRaises(ValueError):
                w.load_config(p)

    def test_double_worker_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'lock'
            with w.single_worker(p):
                with self.assertRaises(ValueError):
                    with w.single_worker(p):
                        pass
            with w.single_worker(p):
                pass


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = self.base / 'events.db'
        self.state = self.base / 'state.db'
        self.cfg = cfg(self.db)
        with closing(sqlite3.connect(self.db)) as s, s:
            s.execute('''CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, received_ts TEXT, ts TEXT,
                card_uid TEXT,name TEXT,decision TEXT,reason TEXT,similarity REAL,snapshot TEXT,latency_s REAL,raw TEXT)''')
        self.add('DENIED')  # An old denial must be skipped on initial configuration.
        self.c = w.state_connection(self.state, create=True)
        w.initialise_state(self.c, self.cfg, w.source_max(self.db))

    def tearDown(self):
        self.c.close()
        self.temp.cleanup()

    def add(self, decision, reason='unknown card'):
        with closing(sqlite3.connect(self.db)) as s, s:
            return s.execute('''INSERT INTO events(ts,received_ts,card_uid,name,decision,reason,latency_s,raw)
                VALUES (?,?,?,?,?,?,?,?)''', (w.utc(time.time()-2), w.utc(time.time()-1), '123456789',
                                             'Private Name', decision, reason, 1.0, '{}')).lastrowid

    def queued(self):
        return self.c.execute('SELECT * FROM deliveries ORDER BY source_row_id').fetchall()

    def test_old_events_skipped(self):
        self.assertEqual(w.ingest(self.c, self.cfg), 0)
        self.assertEqual(len(self.queued()), 0)

    def test_denials_only(self):
        self.add('GRANTED')
        self.add('DENIED')
        self.add(None)
        self.assertEqual(w.ingest(self.c, self.cfg), 1)
        self.assertEqual(self.queued()[0]['source_row_id'], 3)
        self.assertEqual(w.meta(self.c, 'cursor'), '4')

    def test_no_duplicate_when_ingested_twice(self):
        self.add('DENIED')
        w.ingest(self.c, self.cfg)
        w.ingest(self.c, self.cfg)
        self.assertEqual(len(self.queued()), 1)

    def test_source_database_unchanged(self):
        self.add('DENIED')
        before = self.db.read_bytes()
        w.ingest(self.c, self.cfg)
        self.assertEqual(self.db.read_bytes(), before)
        with closing(w.source_connection(self.db)) as src:
            with self.assertRaises(sqlite3.OperationalError):
                src.execute("DELETE FROM events")

    def test_events_after_downtime_caught_up(self):
        self.c.close()
        self.add('DENIED')
        self.add('GRANTED')
        self.add('DENIED')
        self.c = w.state_connection(self.state)
        self.assertEqual(w.ingest(self.c, self.cfg), 2)

    def test_accepted_saved_and_not_repeated(self):
        self.add('DENIED')
        w.ingest(self.c, self.cfg)
        sender = mock.Mock(return_value=w.classify(204, 'http_204'))
        self.assertTrue(w.deliver_one(self.c, self.cfg, sender))
        self.assertFalse(w.deliver_one(self.c, self.cfg, sender))
        self.assertEqual(sender.call_count, 1)
        r = self.queued()[0]
        self.assertEqual(r['status'], 'accepted')
        self.assertGreaterEqual(r['event_to_ack_s'], 2)
        self.assertGreaterEqual(r['received_to_ack_s'], 1)

    def test_transient_failure_retries_same_id(self):
        self.add('DENIED')
        w.ingest(self.c, self.cfg)
        sender = mock.Mock(side_effect=[w.classify(503, 'http_503'), w.classify(200, 'http_200')])
        w.deliver_one(self.c, self.cfg, sender)
        self.assertEqual(self.queued()[0]['status'], 'pending')
        self.assertFalse(w.deliver_one(self.c, self.cfg, sender))
        with self.c:
            self.c.execute('UPDATE deliveries SET next_attempt=0')
        w.deliver_one(self.c, self.cfg, sender)
        self.assertEqual(self.queued()[0]['status'], 'accepted')
        self.assertEqual(sender.call_args_list[0].args[1]['event_id'], sender.call_args_list[1].args[1]['event_id'])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 2)

    def test_permanent_failure_not_retried(self):
        self.add('DENIED')
        w.ingest(self.c, self.cfg)
        sender = mock.Mock(return_value=w.classify(401, 'http_401'))
        w.deliver_one(self.c, self.cfg, sender)
        self.assertEqual(self.queued()[0]['status'], 'failed')
        self.assertFalse(w.deliver_one(self.c, self.cfg, sender))

    def test_five_attempt_limit(self):
        self.add('DENIED')
        w.ingest(self.c, self.cfg)
        sender = mock.Mock(return_value=w.classify(None, 'network_error'))
        for _ in range(5):
            with self.c:
                self.c.execute('UPDATE deliveries SET next_attempt=0')
            w.deliver_one(self.c, self.cfg, sender)
        self.assertEqual(self.queued()[0]['status'], 'failed')
        self.assertEqual(self.queued()[0]['attempts'], 5)
        self.assertFalse(w.deliver_one(self.c, self.cfg, sender))

    def test_429_pauses_destination(self):
        self.add('DENIED')
        self.add('DENIED')
        w.ingest(self.c, self.cfg)
        sender = mock.Mock(return_value=w.classify(429, 'http_429', 60))
        w.deliver_one(self.c, self.cfg, sender)
        self.assertGreater(float(w.meta(self.c, 'not_before')), time.time()+59)
        self.assertFalse(w.deliver_one(self.c, self.cfg, sender))

    def test_interrupted_send_recovered(self):
        self.add('DENIED')
        w.ingest(self.c, self.cfg)
        with self.c:
            self.c.execute("UPDATE deliveries SET status='sending',attempts=1")
        w.recover(self.c)
        self.assertEqual(self.queued()[0]['status'], 'pending')
        self.assertEqual(self.queued()[0]['last_result'], 'interrupted_unknown_outcome')

    def test_interrupted_last_attempt_becomes_failed(self):
        self.add('DENIED')
        w.ingest(self.c, self.cfg)
        with self.c:
            self.c.execute("UPDATE deliveries SET status='sending',attempts=5")
        w.recover(self.c)
        self.assertEqual(self.queued()[0]['status'], 'failed')

    def test_reset_database_detected(self):
        with closing(sqlite3.connect(self.db)) as s, s:
            s.execute('DELETE FROM events')
        with self.assertRaises(ValueError):
            w.ingest(self.c, self.cfg)

    def test_state_mismatch_detected(self):
        with self.assertRaises(ValueError):
            w.check_state(self.c, dict(self.cfg, source_id='c'*32))

    def test_missing_source_not_created(self):
        p = self.base / 'missing.db'
        with self.assertRaises(ValueError):
            w.source_connection(p)
        self.assertFalse(p.exists())

    def test_missing_state_not_silently_reset(self):
        with self.assertRaises(ValueError):
            w.state_connection(self.base / 'missing-state.db')

    def test_batch_cursor(self):
        for _ in range(105):
            self.add('DENIED')
        self.assertEqual(w.ingest(self.c, self.cfg), 100)
        self.assertEqual(w.ingest(self.c, self.cfg), 5)
        self.assertEqual(len(self.queued()), 105)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg(Path('/tmp/events.db'))
        self.payload = w.sample(self.cfg)

    @mock.patch.object(w.request, 'build_opener')
    def test_request_headers_and_tls(self, build):
        response = mock.MagicMock()
        response.status = 200
        response.headers = {}
        build.return_value.open.return_value.__enter__.return_value = response
        r = w.post(dict(self.cfg, bearer_token='test-token'), self.payload)
        self.assertTrue(r['ok'])
        req = build.return_value.open.call_args.args[0]
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-token')
        self.assertEqual(req.get_header('Idempotency-key'), self.payload['event_id'])
        self.assertEqual(req.get_header('Content-type'), 'application/json')
        self.assertEqual(build.return_value.open.call_args.kwargs['timeout'], 5.0)
        tls_handler = build.call_args.args[1]
        self.assertEqual(tls_handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(tls_handler._context.check_hostname)

    @mock.patch.object(w.request, 'build_opener')
    def test_http_error_classification(self, build):
        for status, retry in [(400, False), (403, False), (404, False), (302, False), (429, True), (500, True), (408, True)]:
            build.return_value.open.side_effect = error.HTTPError(self.cfg['url'], status, 'secret', {'Retry-After': '12'}, io.BytesIO(b'secret body'))
            result = w.post(self.cfg, self.payload)
            self.assertFalse(result['ok'])
            self.assertEqual(result['retryable'], retry)
            self.assertNotIn('SECRET', json.dumps(result))
            self.assertNotIn('secret body', json.dumps(result))

    @mock.patch.object(w.request, 'build_opener')
    def test_network_error_redacted(self, build):
        build.return_value.open.side_effect = error.URLError('SECRET_URL')
        r = w.post(self.cfg, self.payload)
        self.assertEqual(r['code'], 'network_error')
        self.assertNotIn('SECRET_URL', str(r))

    @mock.patch.object(w.request, 'build_opener')
    def test_tls_error_redacted(self, build):
        build.return_value.open.side_effect = error.URLError(ssl.SSLError('PRIVATE_HOST'))
        self.assertEqual(w.post(self.cfg, self.payload)['code'], 'tls_error')

    def test_redirect_refused(self):
        self.assertIsNone(w.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://bad.invalid'))

    def test_retry_after(self):
        self.assertEqual(w.retry_after({'Retry-After':'12'}), 12)
        self.assertEqual(w.retry_after({'Retry-After':'0.2'}), 1)
        self.assertIsNone(w.retry_after({'Retry-After':'nan'}))
        self.assertIsNone(w.retry_after({'Retry-After':'not a time'}))
        self.assertIsNone(w.retry_after({}))

    def test_negative_clock_difference_not_falsified(self):
        self.assertIsNone(w.difference(0, '2026-10-30T00:00:00+00:00'))


if __name__ == '__main__':
    unittest.main()
