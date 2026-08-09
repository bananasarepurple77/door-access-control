#!/usr/bin/env python3
"""
Enrol, list, or revoke users.

    python enrol.py add 123456789 "Otto Schultz" photos/me_enrolled.jpg
    python enrol.py list
    python enrol.py revoke 123456789

Validates the photo with Rekognition before saving, so a bad enrolment
image is caught at enrolment time rather than at the door.
"""

import sqlite3
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

BASE = Path("/home/cqlm/door")
DB_PATH = BASE / "door.db"
REGION = "ap-southeast-2"


def validate_photo(path):
    """Confirm the image contains exactly one clearly detectable face."""
    rek = boto3.client("rekognition", region_name=REGION)
    try:
        with open(path, "rb") as f:
            resp = rek.detect_faces(Image={"Bytes": f.read()}, Attributes=["DEFAULT"])
    except ClientError as e:
        print(f"  rejected: Rekognition error - {e.response['Error']['Code']}")
        return False

    faces = resp.get("FaceDetails", [])
    if len(faces) == 0:
        print("  rejected: no face detected in this image")
        return False
    if len(faces) > 1:
        print(f"  rejected: {len(faces)} faces detected - enrolment photo must "
              "contain exactly one person")
        return False

    conf = faces[0]["Confidence"]
    print(f"  one face detected, confidence {conf:.1f}%")
    if conf < 99:
        print("  warning: low detection confidence - consider a clearer photo")
    return True


def add(card_uid, name, photo):
    photo_path = Path(photo)
    if not photo_path.is_absolute():
        photo_path = BASE / photo_path
    if not photo_path.exists():
        sys.exit(f"photo not found: {photo_path}")
    if photo_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        sys.exit("photo must be .jpg or .png (Rekognition accepts these only)")

    print(f"validating {photo_path.name}...")
    if not validate_photo(photo_path):
        sys.exit("enrolment aborted")

    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            "INSERT OR REPLACE INTO users (card_uid, name, photo_path, active) "
            "VALUES (?, ?, ?, 1)",
            (str(card_uid), name, str(photo_path)),
        )
    print(f"enrolled: {name} -> card {card_uid}")


def listing():
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT card_uid, name, photo_path, active FROM users ORDER BY name"
        ).fetchall()
    if not rows:
        print("no users enrolled")
        return
    print(f"{'CARD UID':<16} {'NAME':<22} {'ACTIVE':<7} PHOTO")
    for uid, name, photo, active in rows:
        print(f"{uid:<16} {name:<22} {'yes' if active else 'NO':<7} "
              f"{Path(photo).name}")


def revoke(card_uid):
    with sqlite3.connect(DB_PATH) as c:
        n = c.execute("UPDATE users SET active = 0 WHERE card_uid = ?",
                      (str(card_uid),)).rowcount
    print(f"revoked card {card_uid}" if n else f"no such card: {card_uid}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "add" and len(sys.argv) == 5:
        add(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "list":
        listing()
    elif cmd == "revoke" and len(sys.argv) == 3:
        revoke(sys.argv[2])
    else:
        sys.exit(__doc__)
