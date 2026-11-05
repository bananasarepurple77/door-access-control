# Cabinet access dashboard — Pi 2

Version: 2026-10-30. Prepared for the existing `dash-rpi` event listener.

## What this adds

An administrator login, read-only SQLite audit log, automatic refresh every three
seconds, summary counts, name/card search, decision/date filters, Sydney local
times, pagination and filtered CSV export (up to 10,000 rows).

It does NOT import or change `controller.py`, `listener.py`, `mqtt_pub.py`,
`events.db`, the MQTT `.env`, system services or GPIO. It does not serve face
photos, raw MQTT messages or unlock controls. There is no webhook delivery in
this package: that is the next component, not a feature silently claimed here.

The application opens the database using SQLite `mode=ro` and `query_only`.
All schema checks and queries are read-only. It never creates an empty events.db.
The first-time `init` command writes only `.dashboard-settings.json`, which is
owner-readable (0600) and contains a salted password hash and session secret.
Do not commit that file or disclose it.

## Architecture

Controller Pi -> MQTT -> existing listener.py -> events.db
                                                  |
                                           SELECT queries only
                                                  |
                               dashboard_app.py (Flask + Waitress)
                                                  |
                                    loopback 127.0.0.1:8080
                                                  |
                                      SSH tunnel to your laptop
                                                  |
                                         laptop web browser

Keep the existing listener in `/home/cqlm2/venv`; create a separate environment
for the dashboard. No GPIO or Raspberry Pi packages are required in this one.

## Installation

1. Download `MFA-Dashboard.zip` to your computer. On the COMPUTER, copy it to Pi 2:

```sh
scp "$HOME/Downloads/MFA-Dashboard.zip" cqlm2@dash-rpi.local:/home/cqlm2/
```

The same quoted Downloads path normally works in macOS Terminal and Windows
PowerShell. Use the actual downloaded path if different. If `.local` does not
resolve, use Pi 2's IP, as used for SSH. This is NOT `os-rpi`.

2. On DASH-RPI, as `cqlm2`:

```sh
cd /home/cqlm2/dashboard
python3 -m zipfile -e /home/cqlm2/MFA-Dashboard.zip /home/cqlm2/dashboard
python3 -m venv dashboard-venv
./dashboard-venv/bin/python -m pip install -r requirements-dashboard.txt
```

The ZIP contains only the new dashboard source, requirements, documentation and
tests. It contains no listener, controller, database, MQTT config or secret file.
A repeat extraction replaces the supplied dashboard source files, so preserve
local changes before re-extracting.

If venv creation reports that ensurepip is unavailable, install the OS package
`python3-venv`, then repeat the venv command. Do not change system Python symlinks
or reinstall the working listener environment.

3. Run the packaged tests (temporary sample database only):

```sh
./dashboard-venv/bin/python -m unittest -v test_dashboard.py test_dashboard_web.py
```

4. Create your dashboard account on the Pi, interactively:

```sh
./dashboard-venv/bin/python dashboard_app.py init
```

Press Enter to use username `admin`. Choose a NEW password of at least 12
characters and type it twice. Password input is intentionally invisible.
The password is not your SSH password. There is no built-in default password.
This step checks the existing database but does not change it.

5. Check and start the dashboard in the foreground:

```sh
./dashboard-venv/bin/python dashboard_app.py check
./dashboard-venv/bin/python dashboard_app.py
```

Keep this terminal open. Ctrl+C stops only this dashboard, not the existing
listener or controller. The initial count will reflect the live database and
may exceed 25 as new events arrive.

6. On your COMPUTER, open a second terminal and run:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8080:127.0.0.1:8080 cqlm2@dash-rpi.local
```

Use Pi 2's IP if needed. Enter the Pi SSH password if asked. A connected tunnel
usually shows no normal shell prompt or output; leave it open.

7. Open this address on your COMPUTER's browser:

```
http://127.0.0.1:8080
```

Sign in with the dashboard account from step 4. The HTTP connection is local at
each end; the traffic between your computer and Pi travels through SSH. This is
not a publicly accessible HTTPS site. No router port forwarding is needed.
Do not change the server to 0.0.0.0 or expose it to the Internet for this demo.

## First acceptance check

Keep the existing controller and listener running. Trigger an unknown-card
attempt and confirm that a new DENIED row appears on the next dashboard refresh.
Then trigger a legitimate successful attempt and confirm a GRANTED row. Check
that search, decision filters, Today and CSV export work. Default scope is all
stored events. Filter dates are Sydney calendar dates, not UTC dates.

The page should display the supplied example time
`2026-10-29T21:59:18+00:00` as `30 Oct 2026, 08:59:18 AEDT`.

## Interpret the display correctly

- `latency_s` is the existing controller's recorded decision duration. It does
  not include subsequent logging, GPIO command completion, actual lock release,
  door movement or the five-second unlock window. Do not relabel it as complete
  RFID-to-door timing.
- The mean includes all numeric, nonnegative durations in the filter, including
  0-second unknown-card denials. Use the decision/name/date filters and exports
  to examine conditions separately; this is not a FAR/FRR study.
- Similarity is a matching score, not a probability of correct identification.
- Counts are database rows. The current listener does not deduplicate MQTT
  redeliveries using a controller-generated event ID. Counts therefore are not
  guaranteed unique physical attempts.
- Missing similarity stays missing (`—`); it is not replaced by zero.
- Missing event time falls back to receipt time and is labelled accordingly.
- The last stored decision and receipt time do not prove the controller is
  currently online. There is no heartbeat/door-position/lock-engagement sensor.
- "Database read successful" means this dashboard could read events.db, not
  that Mosquitto, the listener, AWS, the camera or the lock are currently healthy.
- Cloud-error strings are shortened/redacted; consult private controller logs
  for full troubleshooting. Raw payloads and snapshot references are excluded.

## Privacy / operating limits

The web app has an administrator login, salted PBKDF2 password hashing,
CSRF-protected forms, signed HttpOnly/SameSite cookies, a four-hour session
limit, a modest failed-login rate limit and restrictive browser headers.
These are implementation controls, not an independent security certification.
It is intended for a private, supervised prototype using SSH forwarding.

Python 3.9 is end-of-life. The requirements use Python-3.9-compatible package
branches so the working build can be tested without rebuilding either Pi today.
Keep it off the public Internet. Plan an OS/Python upgrade and security review
before continuing real deployment. The module does not solve the previously
unverified relay electrical interface or other hardware limitations.

The source does not require an API key. Keep reference images and credentials
out of the repository. CSV files contain access-event personal information;
store them privately, use test-participant consent and do not publish them.

## Optional: start the dashboard automatically

Do this only AFTER the foreground test passes. Stop the foreground dashboard
with Ctrl+C first, or the service will find port 8080 already in use.

```sh
cd /home/cqlm2/dashboard
sudo install -m 644 dashboard-web.service.example /etc/systemd/system/door-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now door-dashboard.service
sudo systemctl status door-dashboard.service --no-pager
```

The example is specific to `cqlm2` and `/home/cqlm2/dashboard` and does not
alter the listener or broker. The computer still needs its SSH tunnel.

Inspect only this web service:

```sh
journalctl -u door-dashboard.service -n 30 --no-pager
```

Stop/disable it without touching the listener:

```sh
sudo systemctl disable --now door-dashboard.service
```

## Password reset

Stop the dashboard (or its web service), then run:

```sh
./dashboard-venv/bin/python dashboard_app.py init --reset
```

Restart it afterwards. This replaces only the dashboard account/session secret
and invalidates prior sessions. It does not affect SSH, MQTT or enrolled users.

## Troubleshooting

- Browser cannot connect: ensure the dashboard process and SSH tunnel are both
  running; the browser address is localhost on your computer, not the Pi IP.
- Local 8080 already occupied: use `-L 127.0.0.1:8081:127.0.0.1:8080` for the
  tunnel and browse `http://127.0.0.1:8081` instead. The Pi server stays on 8080.
- Permission warning: `chmod 600 .dashboard-settings.json`.
- Too many login failures: wait 60 seconds and try the correct dashboard login.
- Database error: `./dashboard-venv/bin/python dashboard_app.py check`.
- Missing timezone: install the OS `tzdata` package or install `tzdata` in this
  new environment. Do not edit timestamps in events.db.
- No new records: inspect the existing listener log; do not start a second copy
  of listener.py just to troubleshoot the dashboard.
- Stop on installation/test errors and preserve the exact output; do not paste
  `.env`, `.dashboard-settings.json` or actual passwords into chat.

## Git

Commit source, requirements, tests and this README. Merge the rules from
`.gitignore-dashboard.example` into your repository's existing .gitignore.
NEVER stage `.dashboard-settings.json`, `dashboard-venv/`, `.env`, events.db,
logs, exported personal records or photos. The working copy here is on Pi 2;
copy its source back into the repository's dashboard/ folder on Pi 1 before
committing, or use a properly configured repository checkout on Pi 2.

## Verification performed during preparation

- Python 3.9 grammar compatibility check and Python compile check passed.
- 25 standard-library unit tests passed against a temporary database matching
  the listener's schema, covering read-only access, SQL parameterisation,
  filters, pagination, timezone conversion, DST boundaries, NULL/zero values,
  credentials, redaction and CSV formula neutralisation.
- Browser rendering and JavaScript were checked in Chromium using a mocked
  fetch function backed by the real query code. Table rendering, filters,
  automatic updates, XSS text escaping and mobile layout passed.
- This preparation environment could not install Flask/Waitress from the
  network, so the real Flask authentication routes and Waitress server were
  not executed here. The 12 included web-route tests run after installing the
  requirements on the Pi. The package has not been deployed to your Pis.
- Browser-test fixtures used demo identities; no database or credentials are
  included in the package. Do not present those fixtures as project results.

## References

Existing schema: bananasarepurple77/door-access-control, dashboard/listener.py,
retrieved 2026-10-30, blob 72c8cdfc3744fbb75fc4e2b2f036f75f01124910.

- Flask installation: https://flask.palletsprojects.com/en/stable/installation/
- Flask security: https://flask.palletsprojects.com/en/stable/web-security/
- Waitress deployment: https://flask.palletsprojects.com/en/stable/deploying/waitress/
- Flask 3.1.3 metadata: https://pypi.org/project/Flask/3.1.3/
- Waitress 3.0.2 metadata: https://pypi.org/project/waitress/3.0.2/
- Python 3.9 EOL: https://peps.python.org/pep-0596/
- SSH forwarding: https://man.openbsd.org/ssh.1
