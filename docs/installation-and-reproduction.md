# Installation and reproduction

## 1. Reproduction scope

Use the main README's computer-only quick start for assessment on a laptop. It runs
`dashboard/dashboard_app.py` with synthetic data via `tools/assessor_demo.py` on
127.0.0.1:8081. It does not emulate actual camera, biometric or lock performance.
The 9 demo-helper tests, 37 dashboard tests and 42 Unix webhook tests use temporary
fixtures; webhook HTTP responses are mocked. Do not use synthetic scores as report results.

For the recorded physical demonstration, see the video in the separate assessment
submission. No video is bundled into this documentation update.

## 2. Preserve a working deployment

Do not execute fresh-install/configuration steps on a working Pi merely to update GitHub.
Do not overwrite `.env`, private JSON settings, databases, photos or either working venv.
Do not start a second listener, webhook worker or GPIO controller alongside an existing one.
Only the source copy in the Git repository and new documentation/demo files need publishing.

## 3. Hardware prerequisites and as-built connections

The original prototype used a Raspberry Pi controller, RC522, green/red LEDs with
330-ohm resistors, a HIGH-trigger relay module, separate 12 V lock supply and maglock.
The monitoring Pi runs the broker and monitoring software. A compatible Meraki camera
with snapshot API access and the operator's own AWS credentials are required.

**As-built, not an electrical safety certification.** The direct 3.3 V GPIO interface
of the original 5 V Songle-type module remains unverified. Use a verified compatible
interface before reproducing powered hardware. Work with both power supplies disconnected
and do not install this prototype on an emergency exit or occupied-room door.

| Device connection | Pi physical/BOARD pin |
|---|---|
| RC522 3.3 V / GND | 1 / 6 |
| RC522 SDA (SPI chip select) | 24 (GPIO8/CE0) |
| RC522 SCK / MOSI / MISO | 23 / 19 / 21 (GPIO11 / GPIO10 / GPIO9) |
| RC522 RST | 22 (GPIO25) |
| RC522 IRQ | Not connected |
| Green LED through 330-ohm resistor / return | 11 (GPIO17) / 9 (GND) |
| Red LED through 330-ohm resistor / return | 13 (GPIO27) / 14 (GND) |
| Relay DC+ / DC- / IN | 2 (5 V) / 20 (GND) / 15 (GPIO22) |

The relay selector is **H**; HIGH activates it. Lock-side wiring is:

```text
12 V adapter + → COM
NC → maglock red +
Maglock black - → 12 V adapter -
NO unused
```

Never connect 12 V to a Pi pin or relay DC+/DC-/IN control terminal. The lock supply
is switched by the relay contacts. GPIO LOW leaves COM–NC closed, powering the lock;
HIGH opens it for the release period. Unplug the 12 V adapter to remove holding power.
The original controller's cleanup returns GPIOs to inputs; reboot/crash behaviour
needs verification, not inference from a normal test.

## 4. Controller — a NEW Raspberry Pi deployment only

The preserved controller and `enrol.py` use `/home/cqlm/door`. Do not assume they
will run from an arbitrary laptop clone. Use the recorded user/path on a new Pi or
review and test path changes in a separate branch. Configure Raspberry Pi SPI and
appropriate GPIO/SPI device permissions. The current code uses BOARD numbering.

The as-built system ran Python 3.9.2. Do not upgrade a working demonstration in place.
For a clean installation, choose a compatible supported OS/Python and validate package
installation and hardware behaviour. A cross-version recreation has not been proven here.

Example layout and environment creation on a NEW Pi with user `cqlm`:

```sh
git clone https://github.com/bananasarepurple77/door-access-control.git /home/cqlm/door
cd /home/cqlm/door
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m pip install 'paho-mqtt==1.6.1' RPi.GPIO spidev mfrc522
```

The second install line identifies dependencies missing from the original root
requirements. These are not a reconstructed exact freeze of the deployed Pi. Stop
on installation errors; do not install them into a working environment blindly.
`paho-mqtt==1.6.1` matches the callback API style used by the source.

On a NEW deployment only, create `.env` from `.env.example` and enter your own values.
Keep it readable only by the owner. The controller expects `MERAKI_API_KEY`,
`MERAKI_SERIAL`, optional `AWS_REGION` (default `ap-southeast-2`) and MQTT variables.
Do not overwrite an existing `.env`. No secrets are supplied with the project.

Boto3 uses its normal AWS credential chain. Configure an account/profile permitted to
call `rekognition:DetectFaces` and `rekognition:CompareFaces`. Do not put working keys
in the repository. `enrol.py` fixes its region to `ap-southeast-2`; keep it consistent
with the controller or explicitly review this setting. Image processing can incur charges.

After credentials/configuration are ready, initialise tables without running GPIO main():

```sh
cd /home/cqlm/door
mkdir -p photos
./venv/bin/python -c "import controller; controller.db_init(); print('Controller tables ready')"
```

This imports the controller and initialises its AWS client/logging, but does not start
its main GPIO loop. It does not call CompareFaces. The database schema is created only
if absent; it is not a reset operation.

Place a consenting test participant's reference image in `photos/`, then use the actual
card UID rather than the placeholder below:

```sh
./venv/bin/python enrol.py add ACTUAL_CARD_UID 'Test User' photos/reference.jpg
./venv/bin/python enrol.py list
# To revoke that credential:
# ./venv/bin/python enrol.py revoke ACTUAL_CARD_UID
```

Enrolment sends image bytes to DetectFaces and requires one detected face. It does not
write anything to the RFID card. Start the controller only after the hardware checks:

```sh
./venv/bin/python controller.py
```

Expected ready message includes `threshold 95%` and `hardware=True`. Verify real positive
and negative cases, LED behaviour and physical hold/release separately. This README
records the prototype; it does not establish deployment safety or biometric accuracy.

## 5. Monitoring Pi — NEW deployment

Recorded paths: `/home/cqlm2/dashboard`, listener venv `/home/cqlm2/venv`, web venv
`/home/cqlm2/dashboard/dashboard-venv`. Copy only the repository's `dashboard/` source
to the deployment directory, preserving it as a separate live configuration/data location.
Do not copy another installation's settings/databases unless doing an authorised private
backup/restore. Never publish those backups.

Install/configure Mosquitto using `docs/mqtt-setup.md`. Its `mosquitto_passwd -c` example
is for creating a NEW password file only; it overwrites an existing one. Use account
passwords chosen locally, not documentation placeholders. The broker uses authenticated
LAN MQTT/1883 without TLS in the recorded design; restrict it to a trusted lab network.
Credentials differ from Pi SSH and dashboard login credentials.

```sh
python3 -m venv /home/cqlm2/venv
/home/cqlm2/venv/bin/python -m pip install 'paho-mqtt==1.6.1' python-dotenv
```

Create `/home/cqlm2/dashboard/.env` from its `.env.example` on a new installation.
Set `MQTT_HOST=localhost` and that listener's broker username/password. Protect it
with `chmod 600`. Start exactly one listener:

```sh
cd /home/cqlm2/dashboard
/home/cqlm2/venv/bin/python listener.py
```

The listener creates the events table in `events.db` and subscribes to `door/events`.
Use a second terminal for the dashboard. Events must already reach this store before
claiming end-to-end monitoring works.

```sh
cd /home/cqlm2/dashboard
python3 -m venv dashboard-venv
./dashboard-venv/bin/python -m pip install -r requirements-dashboard.txt
./dashboard-venv/bin/python -m unittest -v test_dashboard.py test_dashboard_web.py
./dashboard-venv/bin/python dashboard_app.py init
./dashboard-venv/bin/python dashboard_app.py check
./dashboard-venv/bin/python dashboard_app.py
```

`init` requires an existing event database and creates a private administrator account;
it does not enrol access-control users. Do not run `init --reset` unless replacing that
login intentionally. The application binds to **127.0.0.1:8080 on the Pi**.
On the assessor/administrator computer, use a separate terminal:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8080:127.0.0.1:8080 cqlm2@dash-rpi.local
```

Replace the hostname with the monitoring Pi's address where necessary. Browse to
`http://127.0.0.1:8080` on the computer. Keep both server and tunnel running.

## 6. Webhook worker — Pi/Linux/WSL only

Use the existing configuration on a working deployment. For a NEW receiver setup:

```sh
cd /home/cqlm2/dashboard
python3 -m unittest -v test_webhook_worker.py
python3 webhook_worker.py init
python3 webhook_worker.py test
python3 webhook_worker.py run
```

`init` needs the database, prompts for your own HTTPS endpoint, saves private settings,
and skips records existing at initialisation. `test` sends ONE synthetic message to
that external endpoint. `run` sends eligible new DENIED events; it does not send GRANTED
records. These are **real network operations**, unlike the mocked test suite.
Use a private Discord channel for a controlled demonstration. Never share the URL/token.

From a separate terminal:

```sh
cd /home/cqlm2/dashboard
python3 webhook_worker.py status
```

`accepted` means HTTP success, not proof a person read the notification. Unknown network
outcomes can cause duplicate deliveries despite stable event IDs. No claim of exactly-once
physical-event delivery is made.

After foreground validation, install services **only if not already installed**. Stop
foreground copies first. Inspect the examples before installation; they contain the
specific `cqlm2` deployment paths:

```sh
cd /home/cqlm2/dashboard
sudo install -m 644 dashboard-web.service.example /etc/systemd/system/door-dashboard.service
sudo install -m 644 denial-webhook.service.example /etc/systemd/system/denial-webhook.service
sudo systemctl daemon-reload
sudo systemctl enable --now door-dashboard.service denial-webhook.service
```

## 7. Restart checks for the EXISTING deployment

```sh
cd /home/cqlm2/dashboard
systemctl is-active mosquitto
pgrep -af '[l]istener.py'
ss -ltnp | grep ':8080'
pgrep -af '[w]ebhook_worker.py'
```

Start a missing process using its documented command or installed service, not both.
The supplied package does not establish the name of any existing listener service.

## 8. Assessment evidence and secrets

Record command, actual output and interpretation in the report appendix where required.
Export only consented/redacted evidence, with trial conditions and event IDs. The synthetic
demo cannot be cited as evidence of successful facial matching, physical locking, latency
or actual Discord delivery. Use the real project's report/video for those claims.

Keep out of Git and the public/source ZIP: real `.env`, credential files, private keys,
dashboard/webhook JSON settings, databases, logs and personal photographs. Ignoring a file
does not remove it from old commits. Review history separately if credentials were ever tracked.

Before final submission: run the instructions in a fresh clone, record the commit hash,
verify teacher access to the repository, inspect the ZIP contents and confirm the reported
video/diagrams are included in the final assessment archive.

## Primary documentation

- Python fcntl platform availability: https://docs.python.org/3/library/fcntl.html
- Python timezone data on Windows: https://docs.python.org/3/library/zoneinfo.html
- Flask/Waitress deployment: https://flask.palletsprojects.com/en/stable/deploying/waitress/
