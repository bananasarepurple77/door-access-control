# Source review and validation record

Reviewed 5 November 2026. Input: `dashboard-source.zip`, 13 files.
Archive SHA-256: `cfa837088931043d4529203ceeb5bb52832f594ef8b4ec6fa9eb4c28014a65a0`.

## Scope and limits

Reviewed the exported dashboard, listener, webhook worker, tests, documentation,
requirements and service examples. Also read the repository's controller, enrolment,
MQTT publisher, root requirements and MQTT setup notes through the GitHub connector.
GitHub reported repository visibility **private**. No repository writes, invitations,
visibility changes, SSH access to either Pi or updates to the user's live code were made.

The source ZIP contains no `.env`, private runtime settings, live database, photo or
log file. A review and selected-pattern scan found no obvious live API key, private key
or real webhook token in these files. This is **not** a full secret/history audit or
independent security certification. Test strings such as `SECRET`, example.invalid URLs
and fixed test-only passwords are intentional fixtures, not live credentials.

A test fixture contained the actual card UID previously used in the project. The update
replaces that UID in its two test-only occurrences with synthetic value `900000000001`.
This changes only `dashboard/test_webhook_worker.py`, not the worker or controller.

## Important reproduction findings

1. The original controller is hardware/cloud dependent and contains a fixed deployment
   base path. Its stub flag does not make it a credential-free computer demo.
2. The root requirements file omits paho-mqtt and hardware libraries used by the code.
   The new guide identifies these missing dependencies but does not pretend to recreate
   the deployed Pi's complete package freeze.
3. The webhook worker imports Unix `fcntl` and uses Unix file permissions. It is not
   natively portable to Windows. Dashboard/demo and Unix webhook test paths are separated.
4. A new, additive demo runs the existing dashboard with a private synthetic database,
   loopback binding and a synthetic-data banner. It never imports the controller,
   listener or webhook worker, never uses live settings, and sends no external alerts.
5. Existing `test_verify.py` is a live-service helper, so blanket root discovery is not
   the recommended offline-test command.
6. MQTT local logging is not an automatic durable replay pipeline; the current publisher
   does not replay missed records out of door.db. Email/SMS remain unimplemented.

## Executed validation

Environment: Linux, CPython 3.13.5. This is not the deployed Pi's Python
3.9.2 environment and is not a Windows machine.

| Check | Observed result |
|---|---|
| Original dashboard and webhook suites | 79 discovered; 67 passed; 12 web tests skipped |
| Same suites after synthetic UID fixture change | 79 discovered; 67 passed; 12 web tests skipped |
| New assessor-demo helper tests | 9/9 passed |
| Python 3.9 syntax parsing of reviewed/added Python files | Passed |
| Comparison of live dashboard_app.py/listener.py/webhook_worker.py | Byte-for-byte unchanged |
| Fresh dependency installation / real HTTP dashboard run | Not completed: package download access unavailable |
| Real AWS/Meraki/GPIO/MQTT/Discord operation | Not tested here |

Flask/Waitress were unavailable in this execution environment. Package installation
could not be completed, so the 12 Flask-route tests were explicitly skipped. Do not
report them as passing. In total **76 tests passed and 12 were skipped** across the two
suites including the new helper. With dependencies installed on Unix, the intended local
checks cover 88 tests; the user must run them and retain actual output before claiming a
fully passing reproduction check. Webhook network responses are mocked in these tests.

## Apply contents

This update is an overlay for the already consolidated repository, **not a complete
standalone copy of the project**. It replaces README.md and one test fixture; other
files are additive. It does not replace .gitignore: append docs/reproduction-ignore.txt
to the existing exclusions. It contains no controller.py, dashboard_app.py, listener.py
or webhook_worker.py replacement. Keep the original 13 exported source files in the
repository as previously imported.

A source ZIP created from Git HEAD is a different artefact from the full assessment ZIP.
Include report, evidence, final diagrams and video separately as required. Review source
and history for secrets, verify teacher access and record the submitted commit.

## Validation on os-rpi — 5 November 2026

The project author ran the assessor demonstration helper, dashboard,
dashboard web-route and webhook-worker test suites in the separate
.assessor-venv environment. All 88 automated tests passed.

This result covers these software test suites. It does not establish
biometric accuracy, physical lock safety or Windows compatibility.
It supplements the earlier validation results recorded above.

## Windows validation after SQLite test-cleanup correction — 5 November 2026

The project author reran the tests on Windows after applying explicit
SQLite connection cleanup in the two affected test files.

All 9 demonstration-helper tests and all 37 dashboard/web tests passed,
with no skipped tests. The synthetic demonstration was created with four
records, passed its configuration check and reached server startup.

This log does not yet document manual browser login, live refresh or CSV
export. The Unix-only webhook-worker suite was not run on native Windows.
