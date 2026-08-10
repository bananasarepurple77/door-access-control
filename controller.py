#!/usr/bin/env python3
"""
Door controller — multi-factor access control (RFID + facial verification).

STUB MODE: hardware calls are no-ops and the card reader is faked.
When the RC522/LED/relay arrive, set HARDWARE = True and implement the
three functions marked  # >>> HARDWARE  below. Nothing else changes.

Flow:  card tap -> lookup user -> Meraki snapshot -> Rekognition 1:1 verify
       -> grant (green + unlock) or deny (red + alert) -> log event
"""

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

import mqtt_pub

# ---------------------------------------------------------------- config
BASE = Path("/home/cqlm/door")
load_dotenv(BASE / ".env")

HARDWARE = False              # flip to True once RC522 / LED / relay are wired

MERAKI_KEY    = os.environ["MERAKI_API_KEY"]
MERAKI_SERIAL = os.environ["MERAKI_SERIAL"]
AWS_REGION    = os.getenv("AWS_REGION", "ap-southeast-2")

SIMILARITY_THRESHOLD = 95     # SC1/SC2
UNLOCK_SECONDS       = 5
SNAPSHOT_POLL_LIMIT  = 15     # seconds

DB_PATH   = BASE / "door.db"
PHOTO_DIR = BASE / "photos"
LOG_PATH  = BASE / "door.log"

# GPIO pins (BCM) — used only when HARDWARE = True
PIN_LED_GREEN = 17
PIN_LED_RED   = 27
PIN_RELAY     = 22

MERAKI_HEADERS = {
    "Authorization": f"Bearer {MERAKI_KEY}",
    "Content-Type": "application/json",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("door")

rekognition = boto3.client("rekognition", region_name=AWS_REGION)


# ------------------------------------------------------------- database
def db_init():
    """Create the user table and event log if they don't exist."""
    with sqlite3.connect(DB_PATH) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                card_uid   TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                photo_path TEXT NOT NULL,
                active     INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                card_uid   TEXT,
                name       TEXT,
                decision   TEXT NOT NULL,
                reason     TEXT,
                similarity REAL,
                snapshot   TEXT,
                latency_s  REAL
            );
        """)


def lookup_user(card_uid):
    """Return (name, photo_path) for an active card, or None."""
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            "SELECT name, photo_path FROM users WHERE card_uid = ? AND active = 1",
            (str(card_uid),),
        ).fetchone()
    return row


def log_event(**kw):
    """Append one decision to the audit log (SC6)."""
    kw.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    cols = ", ".join(kw)
    marks = ", ".join("?" for _ in kw)
    with sqlite3.connect(DB_PATH) as c:
        c.execute(f"INSERT INTO events ({cols}) VALUES ({marks})", tuple(kw.values()))
    mqtt_pub.publish(kw)

# ------------------------------------------------------- hardware layer
# >>> HARDWARE: replace the three functions below when parts arrive.

def read_card():
    """Block until a card is presented; return its UID as a string.

    STUB: prompts on stdin so the whole flow can be exercised without an RC522.
    REAL:
        from mfrc522 import SimpleMFRC522
        uid, _ = _reader.read()
        return str(uid)
    """
    if HARDWARE:
        uid, _ = _reader.read()          # noqa: F821  (defined in setup_hardware)
        return str(uid)
    try:
        return input("\n[stub] tap a card (enter UID, or 'q' to quit): ").strip()
    except EOFError:
        return "q"


def set_led(colour):
    """colour: 'green' | 'red' | 'off'.

    REAL: drive PIN_LED_GREEN / PIN_LED_RED via gpiozero LED objects.
    """
    if not HARDWARE:
        log.info("[stub] LED -> %s", colour)
        return
    _led_green.value = (colour == "green")   # noqa: F821
    _led_red.value   = (colour == "red")     # noqa: F821


def unlock(seconds=UNLOCK_SECONDS):
    """Release the maglock for `seconds`, then re-lock.

    NOTE fail-safe wiring: relay NC keeps the magnet energised (locked) at rest,
    so firing the relay DROPS the magnet and opens the box.
    REAL: _relay.on() / sleep / _relay.off() with gpiozero OutputDevice.
    """
    if not HARDWARE:
        log.info("[stub] maglock released for %ss, then re-locked", seconds)
        return
    _relay.on()                              # noqa: F821
    time.sleep(seconds)
    _relay.off()                             # noqa: F821


def setup_hardware():
    """Instantiate reader/LED/relay objects. Called once at startup."""
    if not HARDWARE:
        log.info("running in STUB mode — no GPIO access")
        return
    global _reader, _led_green, _led_red, _relay
    from gpiozero import LED, OutputDevice
    from mfrc522 import SimpleMFRC522
    _reader    = SimpleMFRC522()
    _led_green = LED(PIN_LED_GREEN)
    _led_red   = LED(PIN_LED_RED)
    # active_high=False suits most opto-isolated relay boards set to LOW trigger;
    # if the board's jumper is on HIGH trigger, use active_high=True.
    _relay     = OutputDevice(PIN_RELAY, active_high=False, initial_value=False)


# --------------------------------------------------------- camera + AWS
def get_snapshot():
    """Trigger a Meraki snapshot and return the JPEG bytes."""
    r = requests.post(
        f"https://api.meraki.com/api/v1/devices/{MERAKI_SERIAL}"
        "/camera/generateSnapshot",
        headers=MERAKI_HEADERS, timeout=10,
    )
    r.raise_for_status()
    url = r.json()["url"]
    for _ in range(SNAPSHOT_POLL_LIMIT):
        img = requests.get(url, timeout=10)
        if img.status_code == 200:
            return img.content
        time.sleep(1)
    raise TimeoutError("snapshot did not become available in time")


def verify_face(enrolment_path, snapshot_bytes):
    """1:1 verification. Returns (matched: bool, similarity: float|None, reason)."""
    try:
        with open(enrolment_path, "rb") as f:
            resp = rekognition.compare_faces(
                SourceImage={"Bytes": f.read()},
                TargetImage={"Bytes": snapshot_bytes},
                SimilarityThreshold=0,
            )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        # No detectable face in the snapshot: empty doorway, motion blur, etc.
        if code == "InvalidParameterException":
            return False, None, "no face detected in snapshot"
        raise

    matches = resp.get("FaceMatches", [])
    best = max((m["Similarity"] for m in matches), default=None)
    if best is None:
        return False, None, "no face detected in snapshot"
    if best < SIMILARITY_THRESHOLD:
        return False, round(best, 2), "face did not match cardholder"
    return True, round(best, 2), "verified"



def save_snapshot(jpeg, card_uid):
    """Persist the frame for the audit trail; returns the filename."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"attempt_{stamp}_{card_uid}.jpg"
    (PHOTO_DIR / name).write_bytes(jpeg)
    return name


def send_alert(subject, message):
    """Denial alert. Stubbed until SNS is configured in Sprint 4."""
    log.warning("[alert] %s | %s", subject, message)
    # Sprint 4:
    # boto3.client("sns", region_name=AWS_REGION).publish(
    #     TopicArn=os.environ["SNS_TOPIC_ARN"], Subject=subject, Message=message)


# ------------------------------------------------------------ main flow
def handle_tap(card_uid):
    """One complete authentication attempt."""
    t0 = time.time()
    set_led("off")
    log.info("card presented: %s", card_uid)

    user = lookup_user(card_uid)
    if user is None:
        set_led("red")
        log_event(card_uid=card_uid, decision="DENIED",
                  reason="unknown card", latency_s=round(time.time() - t0, 2))
        send_alert("Access denied: unknown card", f"UID {card_uid} is not enrolled.")
        log.warning("DENIED — unknown card (no cloud call made)")
        time.sleep(2)
        set_led("off")
        return

    name, photo_path = user
    log.info("card belongs to %s — capturing snapshot", name)

    try:
        jpeg = get_snapshot()
    except (requests.RequestException, TimeoutError) as e:
        set_led("red")
        log_event(card_uid=card_uid, name=name, decision="DENIED",
                  reason=f"snapshot failure: {e}",
                  latency_s=round(time.time() - t0, 2))
        send_alert("Access denied: camera unavailable", str(e))
        log.error("DENIED — snapshot failed: %s", e)
        time.sleep(2)
        set_led("off")
        return

    snap_name = save_snapshot(jpeg, card_uid)

    try:
        matched, similarity, reason = verify_face(photo_path, jpeg)
    except (BotoCoreError, ClientError) as e:
        set_led("red")
        log_event(card_uid=card_uid, name=name, decision="DENIED",
                  reason=f"verification error: {e}", snapshot=snap_name,
                  latency_s=round(time.time() - t0, 2))
        send_alert("Access denied: verification error", str(e))
        log.error("DENIED — verification error: %s", e)
        time.sleep(2)
        set_led("off")
        return

    latency = round(time.time() - t0, 2)

    if matched:
        log.info("GRANTED — %s at %.2f%% (%.1fs)", name, similarity, latency)
        log_event(card_uid=card_uid, name=name, decision="GRANTED",
                  reason=reason, similarity=similarity,
                  snapshot=snap_name, latency_s=latency)
        set_led("green")
        unlock()
        set_led("off")
    else:
        log.warning("DENIED — %s (%.1fs)", reason, latency)
        log_event(card_uid=card_uid, name=name, decision="DENIED",
                  reason=reason, similarity=similarity,
                  snapshot=snap_name, latency_s=latency)
        send_alert(
            "Access denied: face mismatch",
            f"Card {card_uid} ({name}) presented but the face did not match "
            f"({reason}). Snapshot: {snap_name}",
        )
        set_led("red")
        time.sleep(3)
        set_led("off")


def main():
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    db_init()
    setup_hardware()
    set_led("off")
    log.info("door controller ready (threshold %s%%, hardware=%s)",
             SIMILARITY_THRESHOLD, HARDWARE)
    try:
        while True:
            uid = read_card()
            if not uid or uid.lower() == "q":
                break
            handle_tap(uid)
    except KeyboardInterrupt:
        pass
    finally:
        set_led("off")
        log.info("controller stopped")


if __name__ == "__main__":
    main()
