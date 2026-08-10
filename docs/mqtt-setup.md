# MQTT Broker Setup (dash-rpi)

Mosquitto runs on **dash-rpi** and carries door events from the controller
(os-rpi) to the dashboard. Authentication is enabled; TLS is not (see
Limitations).

## Install

    sudo apt update && sudo apt install -y mosquitto mosquitto-clients
    sudo systemctl enable --now mosquitto

## Authentication

Create the password file (first user only uses `-c`):

    sudo mosquitto_passwd -c /etc/mosquitto/passwd doorpi

Add further users without `-c`:

    sudo mosquitto_passwd /etc/mosquitto/passwd dashpi

## Configuration

`/etc/mosquitto/conf.d/local.conf`:

    listener 1883 0.0.0.0
    allow_anonymous false
    password_file /etc/mosquitto/passwd

Mosquitto 2.x binds to localhost only by default — the explicit
`listener 0.0.0.0` is required for the controller to reach it over the LAN.

    sudo systemctl restart mosquitto
    sudo systemctl status mosquitto
    ss -tlnp | grep 1883          # expect 0.0.0.0:1883

## Topics

| Topic | Publisher | Payload |
|---|---|---|
| `door/events` | os-rpi controller | JSON: ts, card_uid, name, decision, reason, similarity, snapshot, latency_s |

## Verify

Subscribe on dash-rpi:

    mosquitto_sub -t 'door/#' -v -u doorpi -P '<password>'

Publish from os-rpi:

    mosquitto_pub -h dash-rpi.local -t 'door/events' -m 'test' -u doorpi -P '<password>'

An unauthenticated publish must be refused — this confirms hardening is active.

## Credentials

Broker credentials are stored in `.env` on each host (see `.env.example`):
`MQTT_HOST`, `MQTT_USER`, `MQTT_PASS`. Never committed to version control.

## Limitations

- **No TLS.** Traffic is plaintext on the LAN. Out of scope for this project;
  recommended for production deployment.
- **Single broker, no clustering.** If dash-rpi is unavailable the controller
  must continue to operate and queue events locally — door decisions must never
  depend on broker availability (see failure-mode testing, Sprint 5).
