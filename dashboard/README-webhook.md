# MFA cabinet denial webhooks

This add-on runs on **dash-rpi**, beside the working dashboard. It reads the
existing `events.db` in SQLite read-only mode. It does not change the controller,
MQTT listener, MQTT `.env`, dashboard application, or their environments.
Only newly recorded **DENIED** rows are sent. GRANTED and unrecognised decisions
are ignored. All denial causes are included, including camera/service errors.

## Files

- `webhook_worker.py`: Python 3.9+ standard-library worker; no pip installation.
- `test_webhook_worker.py`: 42 offline unit tests using temporary databases and
  mocked HTTP requests, not your event database or external services.
- `denial-webhook.service.example`: systemd service for user `cqlm2`.
- `.gitignore-webhook.example`: private-file exclusions to append to Git rules.

Private generated files:

- `.webhook-settings.json`: webhook URL, optional bearer token, pseudonym key,
  source ID and source DB path; mode 0600. Never share or commit this file.
- `.webhook-state.db`: scan cursor, pending alerts, delivery results and attempt
  history; separate from `events.db`; mode 0600.
- `.webhook-worker.lock`: prevents two copies of this worker processing the same
  state simultaneously.

The ZIP contains no settings, URLs, credentials or databases.

## 1. Obtain a receiving URL

Use a receiver you control and permit to receive these event records.

### Generic HTTPS receiver / Webhook.site

Open https://webhook.site in your browser and copy **Your unique URL**, not the
browser's `#!/view/...` address. Choose `generic` at setup. Webhook.site displays
the incoming POST body for test evidence; it is not automatically an email/SMS or
phone notification service. Treat the full URL as secret. Free endpoints expire
and have request limits, so use this for demonstration/test capture, not ongoing
production monitoring. Use only test-card events on a public test service.

A receiver you have built can use the same JSON schema, optionally protected by
a bearer token entered during setup. It must accept HTTPS POST and return a 2xx
status. No HMAC request signing is implemented; use TLS and bearer authentication
where needed. HTTPS certificate verification cannot be disabled by this worker.

### Discord incoming webhook

In a server you manage: Server Settings > Integrations > Create Webhook. Select
a private **text channel**, name the webhook, then copy its webhook URL.
Choose `discord` at setup; do not append `/github` or `/slack` to the URL.
The worker adds `wait=true` and disables mentions. Actual phone notifications
also depend on your Discord/channel/phone notification settings.

### Slack incoming webhook

Create/choose a Slack app, enable Incoming Webhooks, choose Add New Webhook to
Workspace, authorise an appropriate private channel, and copy the webhook URL.
Choose `slack` at setup. No separate bearer token is needed for Slack/Discord.
Other chat platforms may need different JSON; generic format is not guaranteed
compatible with Teams or every automation provider.

## 2. Install on dash-rpi

From your COMPUTER, assuming the ZIP was downloaded to Downloads:

```sh
scp "$HOME/Downloads/MFA-Webhook.zip" cqlm2@dash-rpi.local:/home/cqlm2/
```

Use Pi 2's IP address instead of `dash-rpi.local` if needed. On Pi 2:

```sh
cd /home/cqlm2/dashboard
python3 -m zipfile -e /home/cqlm2/MFA-Webhook.zip /home/cqlm2/dashboard
python3 -m unittest -v test_webhook_worker.py
python3 webhook_worker.py init
```

Setup prompts for a format, an HTTPS receiving URL, and (generic only) an optional
bearer token. URL/token input is hidden; paste, then press Enter. The last existing
row ID is saved at initialisation, so old historical denials are not replayed.
Denials created **after** init, including while the worker is stopped, are eligible.
Init does not send any request. It refuses to overwrite existing configuration.

Keep the listener and dashboard running; do not use sudo for these Python commands.

## 3. Test the endpoint before live alerts

```sh
python3 webhook_worker.py test
```

This sends ONE explicitly labelled synthetic test notification. It does not read
real identities, insert source events, or add a delivery to the live ledger. Check
that it appears at the receiver. Success looks like `Test result: http_200` or
another 2xx response such as 204. Non-2xx is not acceptance.

```sh
python3 webhook_worker.py run
```

Leave this terminal running. Trigger an **unknown card** once through the existing
cabinet controller. The listener should record a new DENIED row, the dashboard
should display it, and the worker should log something like:

```text
INFO Queued 1 new denial(s)
INFO row=26 attempt=1 status=accepted result=http_200
```

A valid card with a failed face check should also create an alert. A GRANTED event
should not. A synthetic test does not validate this full path; do all three checks.

Use a second Pi SSH terminal for:

```sh
cd /home/cqlm2/dashboard
python3 webhook_worker.py status
```

This prints row IDs, states, attempts, HTTP codes and timing measurements, without
exposing the receiver URL, token or event identity. The existing dashboard's UI
is not changed to show delivery statuses; inspect them using this command.

## 4. Run automatically after manual validation

Stop the foreground worker with Ctrl+C. This does not stop the controller,
listener or dashboard. Then, on Pi 2:

```sh
cd /home/cqlm2/dashboard
sudo cp denial-webhook.service.example /etc/systemd/system/denial-webhook.service
sudo systemctl daemon-reload
sudo systemctl enable --now denial-webhook.service
systemctl status denial-webhook.service --no-pager
journalctl -u denial-webhook.service -n 30 --no-pager
```

Look for `active (running)`. The service runs as cqlm2 using `/usr/bin/python3`;
it does not use or modify either existing virtual environment. No extra network
port, router forwarding or browser/SSH tunnel is needed for outbound alerts.
The service makes settings and source events.db read-only to its own process and
allows private delivery-state writes in the dashboard folder. This does not change
filesystem access for the separate listener service.

Useful operations:

```sh
sudo systemctl stop denial-webhook.service
sudo systemctl restart denial-webhook.service
sudo systemctl disable --now denial-webhook.service
journalctl -u denial-webhook.service -f
```

Do not run a foreground `run` at the same time as the service. The process lock
rejects the second worker. `status` and `export` can run alongside the service.

## 5. Payload and privacy

Generic example (illustrative values, not a real event):

```json
{
  "type": "access.denied",
  "test": false,
  "event_id": "a-32-character-source-id:26",
  "event_row_id": 26,
  "source": "cabinet-01",
  "event_time_utc": "2026-10-30T01:00:00.000+00:00",
  "received_time_utc": "2026-10-30T01:00:01.000+00:00",
  "decision": "DENIED",
  "reason": "unknown_card",
  "card_ref": "a1b2c3d4e5f67890",
  "similarity": null,
  "decision_time_s": 0.0
}
```

No name, full RFID UID, image, image URL, image filename, raw event JSON or raw
exception string leaves the Pi through this worker. Reasons are converted to a
small allow-list. Card references are keyed HMAC pseudonyms, not plain hashes of
guessable UIDs. They are still linkable pseudonymous data, not anonymous data.
Only UTC timestamps and necessary event information are sent. The source number
in `event_row_id` lets the administrator find the full local dashboard record.

Do not include real participant data in screenshots of third-party receivers;
use your own test cards, and confirm applicable consent/retention arrangements.
Rotate/recreate a leaked webhook URL through its provider.

## 6. Reliability, boundaries and timing

- Polls SQLite every second; normally first send is on the next poll. This is NOT
  a guarantee of delivery within one second or ten seconds: queue backlog, DNS,
  network, receiver processing and retries can extend it.
- Does not hold a transaction on `events.db` while making network requests.
- Durable outbox records alerts before sending and keeps accepted/failed history.
- Counts one row once locally. Does not automatically resend accepted rows after
  normal restart. Preserves stable `event_id` and `Idempotency-Key` on retries.
- Five total attempts for timeouts/network failures, HTTP 408, 429 and 5xx.
  Backoffs are 2, 5, 15, 30 seconds, increased for `Retry-After`. HTTP 429 pauses
  the destination for at least its requested wait (or 30 seconds if unspecified).
- Other 4xx and all redirects fail without automatic retries. Fix configuration
  first, then explicitly requeue failed entries.
- A 5-second socket-operation timeout is used, not a hard overall request/DNS
  deadline. A stalled webhook can delay other alerts but not lock authentication.
- A 2xx response is labelled **accepted**, not proof a person saw the alert.
  Discord uses `wait=true`; other receivers must implement their own processing
  semantics. No browser/mobile/email read receipts are provided.
- HTTP cannot provide exactly-once delivery alone. If the receiver accepted a POST
  but its response was lost, a retry can duplicate the message. A custom receiver
  should deduplicate by event_id/Idempotency-Key; Slack/Discord need not do so.
- The MQTT listener has no global event UUID/deduplication. If it stores a duplicate
  MQTT delivery under a new row ID, this worker treats it as another event. Do not
  claim end-to-end exactly-once handling.
- On restart, interrupted sends are recorded as outcome unknown and retried within
  the attempt cap. Failed alerts remain visible; they are not silently discarded.
- events.db must retain monotonically increasing IDs. A detected ID rollback stops
  the worker. A replacement database that already has higher IDs cannot always be
  detected. Preserve both databases and settings when backing up/restoring.
- If an event never reaches Pi 2/events.db, this worker cannot alert on it. Monitoring
  controller/MQTT outages would require a separate heartbeat feature.
- No face liveness, physical door/lock sensor feedback, email or SMS functionality
  is added by this package.

`event_to_ack_s` measures from the controller's stored event timestamp to the
receiver's HTTP acknowledgement. `received_to_ack_s` measures from the listener's
stored receipt timestamp. The source timestamps currently have one-second
resolution and depend on clock synchronisation. Negative clock differences are
reported as missing rather than silently changed to zero. These are useful coarse
checks, not precise timing instrumentation or phone-notification latency.

`decision_time_s` is your existing recorded duration, not physical unlock time.

For five denial trials, check arrival at the actual destination and export:

```sh
umask 077
python3 webhook_worker.py export > webhook-deliveries.csv
```

The export excludes private receiver settings and identity information, but still
contains access timestamps; keep it private and review before using in a report.

## 7. Changes and troubleshooting

To change URL or format after testing:

```sh
sudo systemctl stop denial-webhook.service
python3 webhook_worker.py configure
python3 webhook_worker.py test
sudo systemctl start denial-webhook.service
```

`configure` preserves the cursor/source ID and rejects changes while pending or
sending deliveries exist, to prevent sending queued data to a different receiver
unintentionally. Failed/accepted records remain in the local ledger.

After correcting a failed endpoint, stop the worker then explicitly retry:

```sh
python3 webhook_worker.py retry-failed
```

This resets the attempt budget for failed records, retaining attempt history.
Start the worker again. Duplicate delivery is possible for unknown-outcome retries.
Do not delete state to fix an endpoint: that loses the delivery record/cursor.

Common cases:

- 400: wrong provider format or receiver rejects this JSON schema.
- 401/403: credentials/permissions wrong; 404: deleted/expired endpoint.
- 405: likely copied a receiver's browser/view URL, not its receiving endpoint.
- 429: receiver quota/rate limit; worker respects its wait and records failures.
- `tls_error`: check Pi date, CA certificates and server certificate; never disable
  certificate checking as a workaround.
- `network_error`: connectivity/DNS/timeout; no raw URL is printed in the error.
- `pending` without sending: run/start the worker and check its logs; a stopped
  worker cannot deliver alerts. Alerts created after init are caught up on restart.
- No denial records: check the listener and events.db first; ensure the denial is
  newer than the row cursor captured at init.
- `sending` after a forced stop: restart the worker to recover it.
- GPIO/relay problems: unrelated; this worker never accesses hardware.

## 8. Version control

This folder may not be a Git repository on Pi 2. The canonical project repository
is on os-rpi. Copy only these source/documentation/service files back into its
`dashboard/` folder when ready. Append `.gitignore-webhook.example` entries to the
repository's existing `.gitignore`. Do NOT copy settings, database files, logs or
exports to GitHub, and do not use `git add .` without review.

## Validation and sources

The included 42 tests exercise filtering, first-start/history behaviour, restart
recovery, retries/rate limits, local duplicate prevention, payload privacy, TLS
settings, redirect refusal, credential permissions and read-only source access.
Local tests are not confirmation of deployment on the Pi or the real endpoint.

Python 3.9 compatibility is intentional for the existing prototype. It does not
make Python 3.9 a currently supported production runtime; upgrade the OS/runtime
before longer-term deployment.

Official references checked for this implementation:
- https://docs.python.org/3.9/library/urllib.request.html
- https://docs.python.org/3.9/library/sqlite3.html
- https://docs.webhook.site/
- https://docs.discord.com/developers/resources/webhook
- https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks
- https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/

Packaging validation: 42 offline tests passed in the build environment (Python 3.13). Python 3.9 grammar parsing also passed. A separate loopback-only HTTPS smoke test verified certificate checking, JSON POST/auth headers, redirect refusal and rate-limit handling. No real webhook endpoint, Pi deployment or live remote receipt was tested here.
