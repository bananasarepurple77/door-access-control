#!/usr/bin/env python3
"""Denial webhook worker for dash-rpi; Python 3.9+, standard library only.

Reads events.db in read-only mode. Never imports controller/listener/dashboard,
uses GPIO, or changes the source database. Private state and secrets live beside
this script. Use `init`, `test`, then `run`; see README-webhook.md.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import getpass
import hashlib
import hmac
import http.client
import json
import logging
import math
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import ssl
import sys
import time
from typing import Any, Dict, Optional
from urllib import error, parse, request
import uuid

BASE = Path(__file__).resolve().parent
CONFIG = BASE / ".webhook-settings.json"
STATE = BASE / ".webhook-state.db"
LOCK = BASE / ".webhook-worker.lock"
MAX_ATTEMPTS = 5
RETRY_DELAYS = (2, 5, 15, 30)
POLL_SECONDS = 1.0
SOCKET_TIMEOUT = 5.0
REQUIRED = {"id", "ts", "received_ts", "card_uid", "decision", "reason", "similarity", "latency_s"}
log = logging.getLogger("denial_webhook")


def utc(epoch: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).isoformat(timespec="milliseconds")


def parse_time(value: Any) -> Optional[float]:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def safe_time(value: Any) -> Optional[str]:
    epoch = parse_time(value)
    return utc(epoch) if epoch is not None else None


def number(value: Any, upper: float) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return round(n, 3) if math.isfinite(n) and 0 <= n <= upper else None
    except (ValueError, TypeError, OverflowError):
        return None


def reason_code(value: Any) -> str:
    """Never send raw exception strings, snapshot URLs, paths, or credentials."""
    s = str(value or "").strip().lower()
    exact = {
        "unknown card": "unknown_card",
        "face did not match cardholder": "face_mismatch",
        "no face detected in snapshot": "no_face_detected",
        "revoked card": "revoked_card",
        "test notification": "test_notification",
    }
    if s in exact:
        return exact[s]
    if s.startswith("snapshot failure:"):
        return "camera_unavailable"
    if s.startswith("verification error:"):
        return "verification_error"
    return "access_denied"


def validate_url(url: str, mode: str) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("Invalid webhook URL; copy the receiving endpoint, with no spaces.")
    try:
        p = parse.urlsplit(url)
        _ = p.port
    except ValueError:
        raise ValueError("Invalid webhook URL.") from None
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment:
        raise ValueError("Use an HTTPS receiver URL without embedded user/password or a # fragment.")
    if mode not in {"generic", "discord", "slack"}:
        raise ValueError("Format must be generic, discord or slack.")
    if mode == "discord":
        if p.hostname not in {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"} or "/webhooks/" not in p.path:
            raise ValueError("Discord format needs the actual Discord webhook URL.")
        # wait=true requests server acknowledgement that a message was created.
        q = [(k, v) for k, v in parse.parse_qsl(p.query, keep_blank_values=True) if k != "wait"]
        q.append(("wait", "true"))
        return parse.urlunsplit((p.scheme, p.netloc, p.path, parse.urlencode(q), ""))
    if mode == "slack" and p.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"}:
        raise ValueError("Slack format needs a Slack incoming-webhook URL.")
    return url


def load_config(path: Path = CONFIG) -> Dict[str, Any]:
    if not path.is_file():
        raise ValueError("No webhook configured. Run: python3 webhook_worker.py init")
    if path.stat().st_mode & 0o077:
        raise ValueError("Protect settings first: chmod 600 .webhook-settings.json")
    try:
        c = json.loads(path.read_text(encoding="utf-8"))
        c["url"] = validate_url(c["url"], c["format"])
        if not isinstance(c.get("source_id"), str) or len(c["source_id"]) != 32:
            raise ValueError("Invalid source ID.")
        if len(bytes.fromhex(c["pseudonym_key"])) != 32:
            raise ValueError("Invalid pseudonym key.")
        if not isinstance(c.get("db_path"), str) or not Path(c["db_path"]).is_absolute():
            raise ValueError("Database path must be absolute.")
        bearer = c.get("bearer_token", "")
        if not isinstance(bearer, str) or len(bearer) > 4096 or any(ord(x) < 32 for x in bearer):
            raise ValueError("Invalid bearer token.")
        if c["format"] != "generic" and bearer:
            raise ValueError("Bearer authentication is only supported in generic mode.")
        return c
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ValueError("Invalid settings file; do not paste its contents into chat.") from None


def save_config(c: Dict[str, Any], path: Path = CONFIG) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@contextmanager
def single_worker(path: Path = LOCK):
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another worker is running. Stop it before this command.") from None
        yield
    finally:
        os.close(fd)


def source_connection(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise ValueError("events.db was not found; no database was created. Check the folder.")
    c = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    try:
        columns = {r[1] for r in c.execute("PRAGMA table_info(events)")}
        if not REQUIRED.issubset(columns):
            raise ValueError("events.db does not have the expected listener schema.")
    except Exception:
        c.close()
        raise
    return c


def source_max(db: Path) -> int:
    with closing(source_connection(db)) as c:
        return c.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]


def state_connection(path: Path = STATE, create: bool = False) -> sqlite3.Connection:
    if not path.exists():
        if not create:
            raise ValueError("Webhook state is missing. Restore it; do not re-initialise blindly.")
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)
    c = sqlite3.connect(str(path), timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def initialise_state(c: sqlite3.Connection, cfg: Dict[str, Any], cursor: int) -> None:
    c.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries (
            event_id TEXT PRIMARY KEY,
            source_row_id INTEGER UNIQUE NOT NULL,
            payload TEXT NOT NULL,
            event_ts TEXT,
            received_ts TEXT,
            queued_ts TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0,
            http_status INTEGER,
            last_result TEXT,
            accepted_ts TEXT,
            event_to_ack_s REAL,
            received_to_ack_s REAL
        );
        CREATE INDEX IF NOT EXISTS pending_due ON deliveries(status,next_attempt);
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES deliveries(event_id),
            attempt_no INTEGER NOT NULL,
            started_ts TEXT NOT NULL,
            finished_ts TEXT,
            http_status INTEGER,
            result TEXT NOT NULL
        );
    """)
    with c:
        c.execute("INSERT OR IGNORE INTO meta VALUES ('cursor',?)", (str(cursor),))
        c.execute("INSERT OR IGNORE INTO meta VALUES ('source_id',?)", (cfg["source_id"],))
        c.execute("INSERT OR IGNORE INTO meta VALUES ('db_path',?)", (cfg["db_path"],))
        c.execute("INSERT OR IGNORE INTO meta VALUES ('not_before','0')")


def meta(c: sqlite3.Connection, key: str) -> str:
    r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if r is None:
        raise ValueError("Webhook state is incomplete; preserve it for troubleshooting.")
    return r[0]


def check_state(c: sqlite3.Connection, cfg: Dict[str, Any]) -> None:
    if meta(c, "source_id") != cfg["source_id"] or meta(c, "db_path") != cfg["db_path"]:
        raise ValueError("Settings and state do not match. Restore the matching files.")


def make_payload(row: Dict[str, Any], cfg: Dict[str, Any], test: bool = False) -> Dict[str, Any]:
    rid = int(row["id"])
    uid = str(row.get("card_uid") or "")
    card_ref = None
    if uid and not test:
        card_ref = hmac.new(bytes.fromhex(cfg["pseudonym_key"]), uid.encode(), hashlib.sha256).hexdigest()[:16]
    return {
        "type": "access.denied",
        "test": test,
        "event_id": "test-" + uuid.uuid4().hex if test else cfg["source_id"] + ":" + str(rid),
        "event_row_id": None if test else rid,
        "source": "cabinet-01",
        "event_time_utc": safe_time(row.get("ts")),
        "received_time_utc": safe_time(row.get("received_ts")),
        "decision": "DENIED",
        "reason": reason_code(row.get("reason")),
        "card_ref": card_ref,
        "similarity": number(row.get("similarity"), 100),
        "decision_time_s": number(row.get("latency_s"), 86400),
    }


def formatted(payload: Dict[str, Any], mode: str) -> Dict[str, Any]:
    if mode == "generic":
        return payload
    text = ("[TEST] " if payload["test"] else "") + "Cabinet access DENIED\n"
    text += "Reason: {reason}\nUTC time: {event_time_utc}\n".format(**payload)
    text += "Event ID: " + payload["event_id"]
    if payload.get("event_row_id") is not None:
        text += "\nDashboard row: " + str(payload["event_row_id"])
    if payload.get("card_ref"):
        text += "\nCard reference: " + payload["card_ref"]
    if payload.get("similarity") is not None:
        text += "\nSimilarity: " + str(payload["similarity"])
    if mode == "discord":
        return {"content": text[:1900], "allowed_mentions": {"parse": []}}
    return {"text": text, "mrkdwn": False}


def ingest(c: sqlite3.Connection, cfg: Dict[str, Any]) -> int:
    cursor = int(meta(c, "cursor"))
    with closing(source_connection(Path(cfg["db_path"]))) as src:
        maximum = src.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        if maximum < cursor:
            raise ValueError("Source event IDs moved backwards. Stop and reconcile restored/replaced events.db.")
        rows = src.execute("""SELECT id, ts, received_ts, card_uid, decision, reason, similarity, latency_s
                              FROM events WHERE id>? ORDER BY id LIMIT 100""", (cursor,)).fetchall()
    added = 0
    if rows:
        with c:
            for r in rows:
                if str(r["decision"] or "").strip().upper() != "DENIED":
                    continue
                payload = make_payload(dict(r), cfg)
                result = c.execute("""INSERT OR IGNORE INTO deliveries
                    (event_id, source_row_id, payload, event_ts, received_ts, queued_ts)
                    VALUES (?,?,?,?,?,?)""",
                    (payload["event_id"], r["id"], json.dumps(payload, allow_nan=False),
                     payload["event_time_utc"], payload["received_time_utc"], utc()))
                added += result.rowcount
            c.execute("UPDATE meta SET value=? WHERE key='cursor'", (str(rows[-1]["id"]),))
    return added


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def retry_after(headers: Any) -> Optional[float]:
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (ValueError, TypeError):
        try:
            value = parsedate_to_datetime(raw).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    if not math.isfinite(value):
        return None
    return max(1.0, value)  # Honour server delays; no automatic rapid retries.


def classify(status: Optional[int], code: str, retry: Optional[float] = None) -> Dict[str, Any]:
    return {"ok": status is not None and 200 <= status < 300,
            "http_status": status, "code": code,
            "retryable": status is None or status in (408, 429) or (status is not None and 500 <= status < 600),
            "retry_after": retry}


def post(cfg: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(formatted(payload, cfg["format"]), ensure_ascii=False, allow_nan=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "MFA-Cabinet-Webhook/1.0",
               "X-Event-ID": payload["event_id"], "Idempotency-Key": payload["event_id"]}
    if cfg.get("bearer_token"):
        headers["Authorization"] = "Bearer " + cfg["bearer_token"]
    req = request.Request(cfg["url"], data=body, headers=headers, method="POST")
    # No automatic redirect: do not move secrets/payload to an unexpected host.
    opener = request.build_opener(NoRedirect(), request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(req, timeout=SOCKET_TIMEOUT) as response:
            status = response.status
            # No response body is logged or stored; receiver acceptance is not human receipt.
            return classify(status, "http_" + str(status), retry_after(response.headers))
    except error.HTTPError as exc:
        result = classify(exc.code, "http_" + str(exc.code), retry_after(exc.headers))
        exc.close()
        return result
    except error.URLError as exc:
        result = classify(None, "tls_error" if isinstance(exc.reason, ssl.SSLError) else "network_error")
        return result
    except (TimeoutError, OSError, http.client.HTTPException):
        return classify(None, "network_error")


def recover(c: sqlite3.Connection) -> None:
    with c:
        c.execute("UPDATE attempts SET finished_ts=?,result='interrupted_unknown_outcome' WHERE finished_ts IS NULL", (utc(),))
        c.execute("""UPDATE deliveries SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'pending' END,
                     next_attempt=?,last_result='interrupted_unknown_outcome' WHERE status='sending'""",
                  (MAX_ATTEMPTS, time.time() + 2))


def difference(end: float, start: Any) -> Optional[float]:
    start_t = parse_time(start)
    return round(end - start_t, 3) if start_t is not None and end >= start_t else None


def deliver_one(c: sqlite3.Connection, cfg: Dict[str, Any], sender=post) -> bool:
    now = time.time()
    if float(meta(c, "not_before")) > now:
        return False
    row = c.execute("""SELECT * FROM deliveries WHERE status='pending' AND next_attempt<=?
                       ORDER BY next_attempt, source_row_id LIMIT 1""", (now,)).fetchone()
    if row is None:
        return False
    attempt = row["attempts"] + 1
    with c:
        c.execute("UPDATE deliveries SET status='sending',attempts=? WHERE event_id=?", (attempt, row["event_id"]))
        a = c.execute("INSERT INTO attempts(event_id,attempt_no,started_ts,result) VALUES (?,?,?,'sending')",
                      (row["event_id"], attempt, utc(now)))
        aid = a.lastrowid
    result = sender(cfg, json.loads(row["payload"]))
    finished = time.time()
    status = "accepted" if result["ok"] else "failed"
    next_due = 0.0
    if not result["ok"] and result["retryable"] and attempt < MAX_ATTEMPTS:
        status = "pending"
        delay = max(float(RETRY_DELAYS[attempt - 1]), result.get("retry_after") or 0)
        next_due = finished + delay
    with c:
        c.execute("UPDATE attempts SET finished_ts=?,http_status=?,result=? WHERE id=?",
                  (utc(finished), result["http_status"], result["code"], aid))
        c.execute("""UPDATE deliveries SET status=?, next_attempt=?, http_status=?,last_result=?,
                     accepted_ts=?,event_to_ack_s=?,received_to_ack_s=? WHERE event_id=?""",
                  (status, next_due, result["http_status"], result["code"],
                   utc(finished) if result["ok"] else None,
                   difference(finished, row["event_ts"]) if result["ok"] else None,
                   difference(finished, row["received_ts"]) if result["ok"] else None, row["event_id"]))
        if result["http_status"] == 429:
            hold = finished + max(2.0, result.get("retry_after") or 30.0)
            c.execute("UPDATE meta SET value=? WHERE key='not_before'", (str(hold),))
    log.info("row=%s attempt=%s status=%s result=%s", row["source_row_id"], attempt, status, result["code"])
    return True


def read_secret(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise ValueError("Run setup interactively through SSH; secrets are entered at hidden prompts.")
    return getpass.getpass(prompt).strip()


def configure(update: bool = False) -> None:
    if CONFIG.exists() and not update:
        raise ValueError("Already configured. Use configure to change the endpoint; it preserves the cursor.")
    old = load_config() if update else None
    db = Path(old["db_path"]) if old else BASE / "events.db"
    latest = source_max(db)
    cfg = dict(old or {"version": 1, "db_path": str(db.resolve()),
                      "source_id": uuid.uuid4().hex, "pseudonym_key": secrets.token_hex(32)})
    print("Choose only a receiver you control. Requests leave this Pi.")
    print("Payloads omit names, full card UIDs, photos, and raw exception text.")
    mode = input("Payload format [generic/discord/slack] (generic): ").strip().lower() or "generic"
    cfg["format"] = mode
    cfg["url"] = validate_url(read_secret("Receiving HTTPS webhook URL (hidden): "), mode)
    cfg["bearer_token"] = read_secret("Optional bearer token (Enter for none): ") if mode == "generic" else ""
    with single_worker():
        if update:
            with closing(state_connection()) as c:
                check_state(c, cfg)
                outstanding = c.execute("SELECT COUNT(*) FROM deliveries WHERE status IN ('pending','sending')").fetchone()[0]
                if outstanding:
                    raise ValueError("Pending alerts exist; deliver or resolve them before changing destination.")
        elif STATE.exists():
            raise ValueError("A state file already exists. Preserve it and its matching settings rather than resetting.")
        else:
            with closing(state_connection(create=True)) as c:
                initialise_state(c, cfg, latest)
        save_config(cfg)
    if update:
        print("Endpoint updated; historical cursor and delivery records preserved. Restart the worker.")
    else:
        print("Configured. Existing rows through ID %s are NOT sent. New denials from now on will be queued." % latest)
    print("No request has been sent. Next: python3 webhook_worker.py test")


def sample(cfg: Dict[str, Any]) -> Dict[str, Any]:
    stamp = utc()
    return make_payload({"id": 0, "ts": stamp, "received_ts": stamp,
                         "reason": "test notification", "latency_s": 0}, cfg, test=True)


def run_worker(once: bool = False) -> None:
    cfg = load_config()
    stopping = [False]
    def stop(signum, frame):
        stopping[0] = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with single_worker(), closing(state_connection()) as c:
        check_state(c, cfg)
        recover(c)
        log.info("Webhook worker started; format=%s; scanning after row=%s; DENIED only", cfg["format"], meta(c, "cursor"))
        last_db_error = 0.0
        while not stopping[0]:
            try:
                count = ingest(c, cfg)
                if count:
                    log.info("Queued %s new denial(s)", count)
                deliver_one(c, cfg)
            except sqlite3.OperationalError:
                if time.time() - last_db_error > 30:
                    log.warning("Database temporarily unavailable; retrying without changing the source database.")
                    last_db_error = time.time()
                # A state commit may have failed after a POST. Recover as unknown
                # outcome; stable event IDs let capable receivers deduplicate.
                recover(c)
            if once:
                break
            time.sleep(POLL_SECONDS)
    log.info("Worker stopped. Unaccepted alerts remain in its local state database.")


def status_report() -> None:
    cfg = load_config()
    latest = source_max(Path(cfg["db_path"]))
    with closing(state_connection()) as c:
        check_state(c, cfg)
        print("Format:", cfg["format"], "| source rows:", latest, "| scanned through:", meta(c, "cursor"))
        counts = dict(c.execute("SELECT status,COUNT(*) FROM deliveries GROUP BY status").fetchall())
        print(" | ".join("%s=%s" % (k, counts.get(k, 0)) for k in ("pending", "sending", "accepted", "failed")))
        print("ROW | STATUS | ATTEMPTS | HTTP | EVENT-TO-ACK(s) | RECEIVED-TO-ACK(s) | RESULT")
        for r in c.execute("""SELECT source_row_id,status,attempts,http_status,event_to_ack_s,received_to_ack_s,last_result
                              FROM deliveries ORDER BY source_row_id DESC LIMIT 10"""):
            print(" | ".join("-" if v is None else str(v) for v in r))
    print("accepted = receiver returned HTTP 2xx; not proof a person saw an alert.")


def main() -> int:
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("init", "configure", "test", "run", "once", "status", "retry-failed", "export"))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command in {"init", "configure"}:
            configure(args.command == "configure")
        elif args.command == "test":
            cfg = load_config()
            print("Sending ONE synthetic test message; no real card/user data or database rows.", flush=True)
            result = post(cfg, sample(cfg))
            print("Test result:", result["code"])
            if not result["ok"]:
                print("Not accepted. Check the receiving URL, payload format, authentication and network.")
                return 1
            print("Receiver accepted the test. Verify it at the receiving service, then run the worker.")
        elif args.command in {"run", "once"}:
            run_worker(args.command == "once")
        elif args.command == "status":
            status_report()
        elif args.command == "retry-failed":
            cfg = load_config()
            with single_worker(), closing(state_connection()) as c:
                check_state(c, cfg)
                with c:
                    n = c.execute("UPDATE deliveries SET status='pending',attempts=0,next_attempt=0 WHERE status='failed'").rowcount
                    c.execute("UPDATE meta SET value='0' WHERE key='not_before'")
                print("Requeued %s failed alerts. Prior attempt history retained. Retries can duplicate remote notifications." % n)
        else:
            cfg = load_config()
            with closing(state_connection()) as c:
                check_state(c, cfg)
                columns = ("source_row_id,event_id,event_ts,received_ts,queued_ts,status,attempts,http_status,"
                           "last_result,accepted_ts,event_to_ack_s,received_to_ack_s")
                out = csv.writer(sys.stdout)
                out.writerow(columns.split(","))
                out.writerows(c.execute("SELECT " + columns + " FROM deliveries ORDER BY source_row_id"))
        return 0
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except (ValueError, OSError, sqlite3.Error) as exc:
        # Do not print transport exception strings: URLs can contain credentials.
        if isinstance(exc, ValueError):
            print("Error:", str(exc), file=sys.stderr)
        else:
            print("Error: local file/database operation failed (%s). Check permissions and free space." % type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
