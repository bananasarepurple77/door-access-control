# Multi-Factor Physical Access-Control Prototype

**ITNET312A — Otto Schultz, student 806005002**

A supervised cabinet prototype uses an RFID credential to select an enrolled user,
a Cisco Meraki MV13M camera snapshot and AWS Rekognition `CompareFaces` to verify
the face, and Raspberry Pi GPIO to control status LEDs and a relay/maglock.
A second Pi stores MQTT access events, serves an authenticated read-only dashboard,
and sends denied-event notifications through a separate webhook worker.

**Assessment prototype, not a production access-control product.** A smartphone
portrait exceeded the implemented face-comparison threshold during project testing.
The system has no liveness detection. Other limitations are listed below.

## Start here: choose the correct reproduction path

| Path | Requirements | What is reproduced |
|---|---|---|
| **Computer-only assessor demo** (below) | Python, the dashboard packages, a local browser | The actual dashboard code, login, filters, CSV export and live refresh, with clearly labelled synthetic records |
| **Offline automated tests** | Python; Flask for web-route tests | Dashboard/database behaviour and, on Unix, webhook queue/payload/transport handling with mocked HTTP |
| **Full hardware deployment** | Two Pis, correctly interfaced hardware, camera, network, own cloud credentials | The real RFID → image → comparison → GPIO workflow, MQTT and optionally real alerts |

The assessor demo does **not** authenticate real faces, reproduce the physical lock,
connect to MQTT, call AWS/Meraki, or send Discord messages. It is not new experimental
evidence. The report, test records and separately submitted video document the actual
hardware tests. `controller.py`'s old `HARDWARE=False` branch is **not** a fully offline
mode: the file still loads Meraki configuration and initialises an AWS client.

## Computer-only quick start

Use a **fresh clone or clean copy**, not either live Pi deployment. New computer setups
should use Python 3.12 or 3.13; the dashboard source is written for Python 3.9+.
Do not replace the working Pi's interpreter or packages just to run this demonstration.
Installation needs package-download access. Once installed, the local demo makes no
cloud-service calls.

### Windows PowerShell

```powershell
git clone https://github.com/bananasarepurple77/door-access-control.git
cd door-access-control
py -3 -m venv .assessor-venv
.\.assessor-venv\Scripts\python.exe -m pip install -r requirements-assessor.txt
.\.assessor-venv\Scripts\python.exe -m unittest discover -s tools -p test_assessor_demo.py -v
```

No PowerShell activation-policy change is necessary: invoke the environment's Python directly.
Then run the original dashboard tests:

```powershell
Push-Location dashboard
..\.assessor-venv\Scripts\python.exe -m unittest -v test_dashboard.py test_dashboard_web.py
Pop-Location
```

Create an isolated demo (choose a password of at least 12 characters), then start it:

```powershell
.\.assessor-venv\Scripts\python.exe tools\assessor_demo.py create
.\.assessor-venv\Scripts\python.exe tools\assessor_demo.py check
.\.assessor-venv\Scripts\python.exe tools\assessor_demo.py serve
```

Open **http://127.0.0.1:8081** in a browser on this computer. Sign in as **assessor**
using the password just chosen. Keep the server terminal open. The demo uses 8081 to
avoid a typical 8080 dashboard tunnel; it never binds to the public/LAN interface.

In a second PowerShell window, in the same repository folder:

```powershell
.\.assessor-venv\Scripts\python.exe tools\assessor_demo.py add unknown-card
.\.assessor-venv\Scripts\python.exe tools\assessor_demo.py add granted
```

Watch the next dashboard refresh. Test the decision filter and CSV export.
Additional scenarios are `no-face` and `mismatch`. They append invented records,
not measured facial comparisons. Stop the demo with Ctrl+C.

### Linux, WSL or macOS

```sh
git clone https://github.com/bananasarepurple77/door-access-control.git
cd door-access-control
python3 -m venv .assessor-venv
./.assessor-venv/bin/python -m pip install -r requirements-assessor.txt
./.assessor-venv/bin/python -m unittest discover -s tools -p test_assessor_demo.py -v
(cd dashboard && ../.assessor-venv/bin/python -m unittest -v test_dashboard.py test_dashboard_web.py test_webhook_worker.py)
./.assessor-venv/bin/python tools/assessor_demo.py create
./.assessor-venv/bin/python tools/assessor_demo.py serve
```

Browse to http://127.0.0.1:8081. In a second terminal in the repository:

```sh
./.assessor-venv/bin/python tools/assessor_demo.py add mismatch
```

The shipped webhook worker imports Unix-only `fcntl`; run its tests and deployment
under Linux/WSL/macOS, not native Windows Python. The dashboard and synthetic demo
are separate from that module. Systemd service examples are Linux/Pi-specific.

### Expected evidence, and limits

- Demo starts with **4 synthetic rows: 1 GRANTED and 3 DENIED**.
- A conspicuous banner identifies synthetic data on the login and dashboard pages.
- Adding a scenario creates one more synthetic row; dashboard refresh is every 3 seconds.
- Files stay in `.assessor-demo/`. Existing demo data is never silently overwritten.
- Dashboard tests: **37**. Unix webhook tests: **42**. Demo-helper tests: **9**.
- With all dependencies installed on Unix, the two test commands above cover **88 tests**.
- The web tests deliberately skip if Flask is absent. **A result containing skipped web
  tests is not verification of the web routes.** See `docs/source-review.md` for what
  was actually executed during packaging.
- Webhook tests mock HTTP; they do not demonstrate live Discord delivery.
- Do not run blanket test discovery from the root: legacy `test_verify.py` is a
  real-service script, not one of the offline test suites documented here.

## Architecture

```text
Card → RC522 → Controller Pi → local door.db user lookup
                       │
                       ├─ Meraki API → snapshot URL → download image
                       ├─ reference image + snapshot bytes → AWS CompareFaces
                       ├─ decision → LEDs + relay → separate 12 V maglock circuit
                       └─ local event record + MQTT publish
                                                  ↓
                            Monitoring Pi: Mosquitto → listener → events.db
                                                                     ├─ authenticated web dashboard
                                                                     └─ webhook worker → Discord
```

`door.db` includes user records and local controller events. Reference and attempt
images are files under `photos/`; the database records reference paths/filenames.
The monitoring database is `events.db`. The dashboard and webhook worker independently
read it. The browser dashboard does not trigger the webhook.

## Repository guide

| File/directory | Purpose |
|---|---|
| `controller.py` | Real controller, GPIO, snapshot acquisition, comparison and local events |
| `enrol.py` | Add/list/revoke user credentials; checks enrolment images with DetectFaces |
| `mqtt_pub.py` | MQTT event publisher; publish errors are logged, not deliberately raised |
| `snapshot.py`, `test_verify.py` | Legacy live-service helpers; not offline tests |
| `.env.example` | Controller configuration placeholders; never contains working secrets |
| `dashboard/listener.py` | MQTT subscriber and event-store writer |
| `dashboard/dashboard_app.py` | Read-only authenticated Flask dashboard served by Waitress |
| `dashboard/webhook_worker.py` | Denial-only sender with separate state database and retries |
| `dashboard/README-dashboard.md`, `README-webhook.md` | Detailed Pi 2 operation guides |
| `dashboard/.env.example` | Listener configuration placeholders |
| `tools/assessor_demo.py` | Isolated synthetic-data demonstration of the real dashboard |
| `docs/installation-and-reproduction.md` | Full setup, operating commands and expected checks |
| `docs/appendix-c-draft.md` | Report appendix wording and reproduction commands |

## Real-system configuration and installation

See **[Installation and reproduction](docs/installation-and-reproduction.md)**.
The as-built paths are `/home/cqlm/door` on `os-rpi` and `/home/cqlm2/dashboard` on
`dash-rpi`; the controller and enrolment script contain a fixed base directory.
Teacher computer-only checks do not require those users or paths.

The original root `requirements.txt` does not list every required hardware/MQTT
package. The installation guide identifies those missing direct dependencies. An
exact reconstruction of the deployed Pi environment additionally requires a reviewed
package inventory from that Pi; the source export was not a full operating-system image.

## Security, privacy and operational limitations

- Use only a supervised test cabinet, not an emergency-exit or occupied-room locking system.
- The tested relay interface is recorded as built, **not certified as GPIO-voltage-compatible**.
  Select a properly rated/verified interface before reproducing powered hardware.
- The maglock is power-to-lock. Loss of its 12 V supply releases it. Ctrl+C or removal of
  Pi power is not an emergency release: COM–NC may still power it from its separate adapter.
- No liveness or presentation-attack detection. A photo may pass the similarity threshold.
- The controller takes the **highest matching face score** from a target snapshot. It does
  not establish that the matching person, when multiple people are visible, presented the card.
- MQTT uses username/password and port 1883 without TLS in this prototype. It is not
  suitable for untrusted networks as configured. Credentials and events need stronger protection.
- Controller events are saved locally before publishing. The current publisher does not
  implement a durable automatic replay of missed events from `door.db` to the monitoring Pi.
- External alerts come from the Pi 2 worker. The controller's `send_alert()` only logs;
  its SNS/email/SMS implementation is not completed.
- `.env` and generated JSON settings are plaintext configurations, not encrypted secret vaults.
  Keep them, credentials, keys, images, live databases and access-log exports out of Git/ZIPs.
- Webhooks use a keyed pseudonymous card reference, not anonymous data. Dashboard records
  include names/UIDs for local authorised review; restrict access accordingly.
- The application does not create S3 storage or a Rekognition collection. This does not
  establish that cloud providers retain no image data. Check provider policies/account settings.
- `latency_s` is measured before subsequent event publishing and lock actuation. It is
  not a measured physical lock-release duration. Audit row counts are not deduplicated trials.
- The legacy Pi runtime is Python 3.9.2; treat it as a preserved prototype environment,
  not a recommendation for a new long-lived deployment.

## Submission and access

At the 5 November 2026 review, the GitHub connector reported this repository as
**private**. Invite the teachers and confirm they can access it, or provide the
reviewed source archive through the submission system. A repository URL alone does
not grant access. Clone commands above require authorised access while it is private.
This update does not change visibility or invite collaborators. Record the submitted commit with `git rev-parse HEAD`.

A source ZIP can be produced from a reviewed commit using `git archive`; it contains
tracked files at that commit, not the untracked live data. Inspect its contents before
submission. Report, final diagrams, evidence and demonstration video must also be added
to the final assessment package if required. They are not generated by this source guide.
