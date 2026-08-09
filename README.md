# Multi-Factor Door Access Control

RFID card identifies the user; AWS Rekognition verifies their face 1:1 against
the enrolled photo. Both must agree or access is denied and an alert is raised.

ITNET312A Major Individual Project - Otto Schultz

## Hardware
- Raspberry Pi 4B (door controller), RC522 RFID reader, bi-colour LED,
  5V opto-isolated relay, Autens 12V 60kg maglock (fail-safe)
- Cisco Meraki MV camera (snapshot API)

## Setup
1. `python3 -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and add MERAKI_API_KEY, MERAKI_SERIAL
4. AWS credentials in `~/.aws/credentials`; IAM user needs
   `rekognition:CompareFaces` and `rekognition:DetectFaces`

## Usage
- Enrol: `python enrol.py add <card_uid> "Name" photos/photo.jpg`
- Run:   `python controller.py`
- Logs:  `sqlite3 door.db "SELECT * FROM events;"`

## Status
Sprint 1 - Pipeline verified in stub mode. `HARDWARE = False` in
controller.py until RC522/LED/relay are wired.
