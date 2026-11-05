# Appendix C – Installation and Reproduction

Repository: https://github.com/bananasarepurple77/door-access-control

Submitted version: **[insert the commit returned by `git rev-parse HEAD` after the final push]**.
At the source review the repository was private; teacher access must be granted and
confirmed separately. A source-code archive is also included in the final submission
**only after the author has actually created and attached it**.

The repository's `README.md` provides two reproduction paths. The computer-only
assessment path runs the actual monitoring dashboard against a separate synthetic
database. It supports login, event filtering, CSV export and automatic refresh without
requiring Raspberry Pis, an RFID reader, camera, AWS credentials or a webhook endpoint.
The demonstration is visibly labelled as synthetic and does not validate real facial
verification or physical locking.

On Windows, after cloning or extracting the source and opening PowerShell in its root:

```powershell
py -3 -m venv .assessor-venv
.\.assessor-venv\Scripts\python.exe -m pip install -r requirements-assessor.txt
.\.assessor-venv\Scripts\python.exe tools\assessor_demo.py create
.\.assessor-venv\Scripts\python.exe tools\assessor_demo.py serve
```

Open `http://127.0.0.1:8081` and log in as `assessor` with the locally chosen password.
In a second terminal, add a clearly labelled synthetic event:

```powershell
.\.assessor-venv\Scripts\python.exe tools\assessor_demo.py add unknown-card
```

The dashboard should display the new row on its next refresh. Test commands, expected
counts, Unix webhook-test instructions and real two-Pi deployment requirements are
provided in the README and `docs/installation-and-reproduction.md`. Real-system
configuration is created locally using the operator's own credentials; secrets and
personal-image databases are not required for the computer-only demonstration.

**Before pasting this appendix into the final report:** replace the commit placeholder,
confirm teacher access and archive inclusion, and attach your actual command output and
interpretation for any terminal screenshots. Do not describe expected output as an
observed result until you have executed it.
