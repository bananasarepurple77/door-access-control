#!/usr/bin/env python3
"""
mqtt_pub.py — fire-and-forget event publishing for the door controller.

Design rule: the door decision must NEVER depend on the broker.
Every failure here is logged and swallowed; the controller carries on.

Usage (two lines in controller.py):

    import mqtt_pub
    ...inside log_event(), after the DB insert:
    mqtt_pub.publish(kw)
"""

import json
import logging
import os

log = logging.getLogger("door.mqtt")

_client = None
_TOPIC = "door/events"


def _get_client():
    """Lazy-connect once; reuse the connection afterwards."""
    global _client
    if _client is not None:
        return _client
    import paho.mqtt.client as mqtt
    host = os.getenv("MQTT_HOST")
    user = os.getenv("MQTT_USER")
    pw   = os.getenv("MQTT_PASS")
    if not (host and user and pw):
        raise RuntimeError("MQTT_* not configured in .env")
    c = mqtt.Client(client_id="door-controller")
    c.username_pw_set(user, pw)
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    c.connect(host, int(os.getenv("MQTT_PORT", "1883")), keepalive=60)
    c.loop_start()          # background thread handles network + reconnects
    _client = c
    return c


def publish(event: dict):
    """Publish one event dict as JSON. Never raises."""
    try:
        payload = json.dumps(event, default=str)
        _get_client().publish(_TOPIC, payload, qos=1)
    except Exception as e:
        # Broker down, DNS failure, bad config — log it, keep the door working.
        log.warning("event publish failed (door unaffected): %s", e)
