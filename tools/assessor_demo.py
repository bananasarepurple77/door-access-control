#!/usr/bin/env python3
"""Synthetic, local-only dashboard demonstration. Does not run the controller.

Run from the repository: python tools/assessor_demo.py create
Then: python tools/assessor_demo.py serve
All runtime data stays in .assessor-demo, never dashboard/events.db.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import getpass
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))
import dashboard_app as dashboard  # No GPIO, MQTT or network calls on import.

DEMO = ROOT / ".assessor-demo"
MARKER = "mfa-synthetic-assessor-demo-v1"
SCHEMA = """CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_ts TEXT NOT NULL, ts TEXT, card_uid TEXT, name TEXT,
    decision TEXT, reason TEXT, similarity REAL, snapshot TEXT,
    latency_s REAL, raw TEXT NOT NULL
)"""
SCENARIOS = {
    "granted": ("DEMO-CARD-A", "Synthetic User A", "GRANTED", "verified", 99.0, 1.0),
    "unknown-card": ("DEMO-UNKNOWN", None, "DENIED", "unknown card", None, 0.0),
    "no-face": ("DEMO-CARD-A", "Synthetic User A", "DENIED", "no face detected in snapshot", None, 2.0),
    "mismatch": ("DEMO-CARD-B", "Synthetic User B", "DENIED", "face did not match cardholder", 30.0, 3.0),
}
BANNER = ('<section class="notice" role="status"><strong>ASSESSOR DEMO — SYNTHETIC DATA.</strong> '
          'No camera, AWS, MQTT, GPIO, lock or real webhook is being used. '
          'Scores and timings are invented fixtures, not project results.</section>')


def write_private_json(path: Path, value: Dict[str, Any]) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def check_marker(base: Path) -> None:
    marker = base / "DEMO-ONLY.json"
    if base.is_symlink() or marker.is_symlink() or not marker.is_file():
        raise ValueError("No owned demo folder. Run the create command; do not use a live database.")
    if json.loads(marker.read_text(encoding="utf-8")).get("kind") != MARKER:
        raise ValueError("Unrecognised demo marker. Nothing was changed.")
    for name in ("events.db", "settings.json"):
        path = base / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Demo is incomplete or contains a symbolic link. Nothing was changed.")


def insert_scenario(connection: sqlite3.Connection, scenario: str,
                    stamp: str) -> int:
    if scenario not in SCENARIOS:
        raise ValueError("Unknown synthetic scenario.")
    uid, name, decision, reason, similarity, duration = SCENARIOS[scenario]
    raw = json.dumps({"synthetic": True, "scenario": scenario,
                      "note": "Invented assessor fixture, not a real access event"})
    row = connection.execute(
        """INSERT INTO events
           (received_ts,ts,card_uid,name,decision,reason,similarity,snapshot,latency_s,raw)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (stamp, stamp, uid, name, decision, reason, similarity, None, duration, raw),
    )
    return int(row.lastrowid)


def create_demo(base: Path, password: str) -> None:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Choose a password between 12 and 1024 characters.")
    if base.exists() or base.is_symlink():
        raise ValueError("Demo folder already exists; it will not be overwritten. Use check, serve or add.")
    base.mkdir(mode=0o700)
    write_private_json(base / "DEMO-ONLY.json", {"kind": MARKER, "synthetic": True})
    db = base / "events.db"
    with closing(sqlite3.connect(str(db))) as connection:
        with connection:
            connection.execute(SCHEMA)
            now = datetime.now(timezone.utc)
            for index, scenario in enumerate(SCENARIOS):
                stamp = (now - timedelta(seconds=(3-index)*30)).isoformat(timespec="seconds")
                insert_scenario(connection, scenario, stamp)
    db.chmod(0o600)
    write_private_json(base / "settings.json", {
        "username": "assessor", "password_hash": dashboard.password_hash(password),
        "secret_key": secrets.token_hex(32), "db_path": str(db.resolve()),
        "timezone": "Australia/Sydney",
    })


def load_demo(base: Path) -> Dict[str, Any]:
    check_marker(base)
    settings = dashboard.load_settings(base / "settings.json")
    if Path(settings["db_path"]).resolve() != (base / "events.db").resolve():
        raise ValueError("Demo settings point outside the demo folder. Refusing to continue.")
    dashboard.check_database(base / "events.db")
    return settings


def add_scenario(base: Path, scenario: str) -> int:
    load_demo(base)
    if scenario not in SCENARIOS:
        raise ValueError("Unknown synthetic scenario.")
    uri = (base / "events.db").resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
        with connection:
            return insert_scenario(connection, scenario, datetime.now(timezone.utc).isoformat(timespec="seconds"))


def serve_demo(base: Path, port: int) -> None:
    if not 1024 <= port <= 65535:
        raise ValueError("Choose a port from 1024 to 65535.")
    settings = load_demo(base)
    from waitress import serve
    # Add a visible banner in this process only; original source stays unchanged.
    dashboard.DASHBOARD_HTML = dashboard.DASHBOARD_HTML.replace("<main>", "<main>" + BANNER, 1)
    dashboard.LOGIN_HTML = dashboard.LOGIN_HTML.replace('<main class="login">', '<main class="login">' + BANNER, 1)
    app = dashboard.create_app(settings)
    print("SYNTHETIC ASSESSOR DEMO — not the physical cabinet.", flush=True)
    print("Open http://127.0.0.1:%d on THIS computer. Username: assessor" % port, flush=True)
    print("Use the password you chose at create. Ctrl+C stops this demo only.", flush=True)
    serve(app, host="127.0.0.1", port=port, threads=4, max_request_body_size=8192,
          channel_timeout=30, clear_untrusted_proxy_headers=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="Create four synthetic events and a private local login; never overwrite")
    sub.add_parser("check", help="Check the demo database and settings")
    serve = sub.add_parser("serve", help="Serve the existing dashboard using demo-only settings")
    serve.add_argument("--port", type=int, default=8081)
    add = sub.add_parser("add", help="Append one synthetic event for the live-refresh demonstration")
    add.add_argument("scenario", choices=tuple(SCENARIOS))
    args = parser.parse_args()
    try:
        if args.command == "create":
            if DEMO.exists() or DEMO.is_symlink():
                raise ValueError("Demo already exists and will not be overwritten. Use check or serve.")
            password = getpass.getpass("Choose a LOCAL DEMO password (12+ characters): ")
            if password != getpass.getpass("Repeat password: "):
                raise ValueError("Passwords did not match. Nothing was created.")
            create_demo(DEMO, password)
            print("Created 4 synthetic rows in .assessor-demo. Username: assessor.")
            print("Next: python tools/assessor_demo.py serve")
        elif args.command == "check":
            load_demo(DEMO)
            print("Synthetic demo ready: %s rows; no live configuration was used." % dashboard.check_database(DEMO / "events.db"))
        elif args.command == "add":
            row = add_scenario(DEMO, args.scenario)
            print("Added SYNTHETIC %s event, row %d. No MQTT or external webhook was sent." % (args.scenario, row))
        else:
            serve_demo(DEMO, args.port)
        return 0
    except KeyboardInterrupt:
        print("\nDemo stopped. No live service was stopped.")
        return 0
    except (ValueError, OSError, sqlite3.Error, ImportError) as exc:
        print("Demo error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
