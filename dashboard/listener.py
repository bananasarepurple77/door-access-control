#!/usr/bin/env python3
"""
Event listener — dash-rpi side.

Subscribes to door/events on the local Mosquitto broker and writes every
message into events.db. This is the durable event store the dashboard
reads from.

Run:  python listener.py
Test: mosquitto_pub -h localhost -t door/events -u doorpi -P '<pass>' \
        -m '{"ts":"2026-08-10T05:00:00+00:00","card_uid":"123","name":"Test",
             "decision":"DENIED","reason":"test event","similarity":12.3,
             "snapshot":"none.jpg","latency_s":1.0}'
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.environ["MQTT_USER"]
MQTT_PASS = os.environ["MQTT_PASS"]
TOPIC     = "door/events"

DB_PATH = BASE / "events.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.FileHandler(BASE / "listener.log"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("listener")

EXPECTED = {"ts", "card_uid", "name", "decision", "reason",
            "similarity", "snapshot", "latency_s"}


def db_init():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                received_ts TEXT NOT NULL,
                ts          TEXT,
                card_uid    TEXT,
                name        TEXT,
                decision    TEXT,
                reason      TEXT,
                similarity  REAL,
                snapshot    TEXT,
                latency_s   REAL,
                raw         TEXT NOT NULL
            )
        """)


def store(payload_bytes):
    raw = payload_bytes.decode("utf-8", errors="replace")
    received = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("payload is not a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        # Store malformed messages too — losing events silently would
        # undermine the audit trail. They land with NULL fields + raw text.
        log.warning("malformed event stored raw: %s", e)
        d = {}

    row = {k: d.get(k) for k in EXPECTED}
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """INSERT INTO events
               (received_ts, ts, card_uid, name, decision, reason,
                similarity, snapshot, latency_s, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (received, row["ts"], row["card_uid"], row["name"],
             row["decision"], row["reason"], row["similarity"],
             row["snapshot"], row["latency_s"], raw),
        )
    log.info("stored: %s %s (%s)",
             row.get("decision"), row.get("name") or row.get("card_uid"),
             row.get("reason"))


# ------------------------------------------------------------- mqtt glue
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        client.subscribe(TOPIC, qos=1)
        log.info("connected to %s:%s, subscribed to %s",
                 MQTT_HOST, MQTT_PORT, TOPIC)
    else:
        log.error("MQTT connect failed rc=%s", rc)


def on_message(client, userdata, msg):
    try:
        store(msg.payload)
    except Exception:
        log.exception("failed to store event")   # never let one bad event kill us


def on_disconnect(client, userdata, rc, properties=None):
    log.warning("disconnected (rc=%s) — paho will auto-reconnect", rc)


def main():
    db_init()
    client = mqtt.Client(client_id="dash-listener", clean_session=False)
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    log.info("listener starting")
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
