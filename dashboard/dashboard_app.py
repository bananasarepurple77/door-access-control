#!/usr/bin/env python3
"""Read-only access-event dashboard for dash-rpi. Python 3.9+.

Does not import the door controller or MQTT listener, operate GPIO, publish
messages, serve photos, modify events.db, or read the MQTT .env file.

    python dashboard_app.py init
    python dashboard_app.py check
    python dashboard_app.py

The web server intentionally binds ONLY to 127.0.0.1:8080. View it through
an SSH tunnel; see README-dashboard.md. Credentials are stored in a separate
owner-readable .dashboard-settings.json, never in the source code.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import closing
import csv
from datetime import date, datetime, time as daytime, timedelta, timezone
import getpass
import hashlib
import hmac
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
SETTINGS_FILE = BASE / ".dashboard-settings.json"
REQUIRED_COLUMNS = {
    "id", "ts", "received_ts", "card_uid", "name", "decision", "reason",
    "similarity", "latency_s",
}
SELECT_COLUMNS = (
    "id, ts, received_ts, card_uid, name, decision, reason, similarity, latency_s"
)
PAGE_SIZE = 50
CSV_LIMIT = 10000
SESSION_SECONDS = 4 * 60 * 60
PBKDF2_ITERATIONS = 600000
log = logging.getLogger("access_dashboard")


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    ).hex()
    return "pbkdf2_sha256${}${}${}".format(PBKDF2_ITERATIONS, salt, digest)


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        count = int(iterations)
        if algorithm != "pbkdf2_sha256" or not 100000 <= count <= 2000000:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("ascii"), count
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


def load_settings(path: Path = SETTINGS_FILE) -> Dict[str, Any]:
    if not path.is_file():
        raise ValueError("No dashboard login configured. Run: python dashboard_app.py init")
    settings = json.loads(path.read_text(encoding="utf-8"))
    for key in ("username", "password_hash", "secret_key", "db_path", "timezone"):
        if not isinstance(settings.get(key), str) or not settings[key]:
            raise ValueError("Missing or invalid setting: " + key)
    if len(settings["secret_key"]) < 32:
        raise ValueError("The dashboard session secret is too short.")
    ZoneInfo(settings["timezone"])
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("Protect your settings first: chmod 600 .dashboard-settings.json")
    return settings


def open_database(path: Path) -> sqlite3.Connection:
    """Open an existing database read-only. Never create a substitute DB."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError("events.db not found at " + str(path))
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 3000")
    return connection


def check_database(path: Path) -> int:
    with closing(open_database(path)) as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError("events table is missing columns: " + ", ".join(sorted(missing)))
        return connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        # A timezone-less value is not silently treated as local or UTC.
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def finite_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def safe_reason(value: Any) -> str:
    """Do not display raw exception payloads/URLs from cloud-service failures."""
    text = str(value or "")
    lower = text.lower()
    if lower.startswith("snapshot failure:"):
        return "Camera / snapshot request failed; inspect the controller's private log."
    if lower.startswith("verification error:"):
        return "Face verification service error; inspect the controller's private log."
    text = re.sub(r"https?://\S+", "[URL omitted]", text, flags=re.I)
    text = re.sub(r"(?i)(token|password|api[_ -]?key|secret)\s*[:=]\s*\S+", r"\1=[redacted]", text)
    return text[:500] or "No reason recorded"


def display_row(row: sqlite3.Row, zone: ZoneInfo) -> Dict[str, Any]:
    event_time = parse_timestamp(row["ts"])
    received = parse_timestamp(row["received_ts"])
    effective = event_time or received
    similarity = finite_number(row["similarity"])
    duration = finite_number(row["latency_s"])
    if duration is not None and duration < 0:
        duration = None
    return {
        "id": row["id"],
        "ts": event_time.isoformat() if event_time else None,
        "received_ts": received.isoformat() if received else None,
        "time_local": effective.astimezone(zone).strftime("%d %b %Y, %H:%M:%S %Z") if effective else "Unknown time",
        "time_source": "event" if event_time else "receipt" if received else "unknown",
        "name": str(row["name"] or "")[:128],
        "card_uid": str(row["card_uid"] or "")[:128],
        "decision": str(row["decision"] or "UNKNOWN")[:32],
        "reason": safe_reason(row["reason"]),
        "similarity": similarity,
        "latency_s": duration,
    }


def build_filter(arguments: Mapping[str, str], zone: ZoneInfo) -> Tuple[str, list]:
    clauses, parameters = [], []
    decision = str(arguments.get("decision", "")).strip().upper()
    if decision and decision not in ("GRANTED", "DENIED", "OTHER"):
        raise ValueError("Choose All, Granted, Denied, or Other.")
    if decision == "OTHER":
        clauses.append("COALESCE(decision, '') NOT IN ('GRANTED', 'DENIED')")
    elif decision:
        clauses.append("decision = ?")
        parameters.append(decision)
    query = str(arguments.get("q", "")).strip()
    if len(query) > 128:
        raise ValueError("Search is limited to 128 characters.")
    if query:
        clauses.append("(instr(lower(COALESCE(name,'')), lower(?)) > 0 OR instr(COALESCE(card_uid,''), ?) > 0)")
        parameters.extend((query, query))
    start, end = None, None
    for key in ("from", "to"):
        value = str(arguments.get(key, "")).strip()
        if not value:
            continue
        try:
            parsed = date.fromisoformat(value)
            if parsed.year < 2000 or parsed.year > 2100:
                raise ValueError
        except ValueError:
            raise ValueError("Dates must be between 2000 and 2100, in YYYY-MM-DD format.")
        if key == "from":
            start = parsed
        else:
            end = parsed
    if start and end and start > end:
        raise ValueError("The start date must not be later than the end date.")
    # julianday handles UTC offsets in the listener's ISO 8601 timestamps.
    timestamp_sql = "COALESCE(julianday(ts), julianday(received_ts))"
    if start:
        instant = datetime.combine(start, daytime.min, zone).astimezone(timezone.utc)
        clauses.append(timestamp_sql + " >= julianday(?)")
        parameters.append(instant.isoformat())
    if end:
        instant = datetime.combine(end + timedelta(days=1), daytime.min, zone).astimezone(timezone.utc)
        clauses.append(timestamp_sql + " < julianday(?)")
        parameters.append(instant.isoformat())
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters


def read_page(db_path: Path, zone: ZoneInfo, arguments: Mapping[str, str]) -> Dict[str, Any]:
    where, parameters = build_filter(arguments, zone)
    try:
        page = int(arguments.get("page", "1"))
    except (TypeError, ValueError):
        raise ValueError("Invalid page number.")
    if not 1 <= page <= 1000000:
        raise ValueError("Invalid page number.")
    with closing(open_database(db_path)) as connection:
        # A short read transaction keeps the count and rows consistent.
        connection.execute("BEGIN")
        stats = dict(connection.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(decision='GRANTED'),0) AS granted, "
            "COALESCE(SUM(decision='DENIED'),0) AS denied, "
            "AVG(CASE WHEN typeof(latency_s) IN ('real','integer') AND latency_s >= 0 "
            "THEN latency_s END) AS mean_duration FROM events" + where, parameters
        ).fetchone())
        pages = max(1, math.ceil(stats["total"] / PAGE_SIZE))
        page = min(page, pages)
        rows = connection.execute(
            "SELECT " + SELECT_COLUMNS + " FROM events" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            parameters + [PAGE_SIZE, (page - 1) * PAGE_SIZE]
        ).fetchall()
        last = connection.execute(
            "SELECT " + SELECT_COLUMNS + " FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        connection.commit()
    stats["other"] = stats["total"] - stats["granted"] - stats["denied"]
    stats["mean_duration"] = finite_number(stats["mean_duration"])
    now = datetime.now(timezone.utc)
    return {
        "stats": stats, "events": [display_row(row, zone) for row in rows],
        "last_record": display_row(last, zone) if last else None,
        "page": page, "pages": pages, "page_size": PAGE_SIZE,
        "generated_at": now.isoformat(), "timezone": str(zone),
        "today": now.astimezone(zone).date().isoformat(),
    }


def csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # Neutralise spreadsheet formula prefixes in user-controlled text.
    if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(("=", "+", "-", "@")):
        text = "'" + text
    return text


def read_csv(db_path: Path, zone: ZoneInfo, arguments: Mapping[str, str]) -> str:
    where, parameters = build_filter(arguments, zone)
    with closing(open_database(db_path)) as connection:
        rows = connection.execute(
            "SELECT " + SELECT_COLUMNS + " FROM events" + where + " ORDER BY id DESC LIMIT ?",
            parameters + [CSV_LIMIT + 1]
        ).fetchall()
    if len(rows) > CSV_LIMIT:
        raise ValueError("More than 10,000 rows match. Narrow the date range before exporting.")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    columns = ["id", "ts", "received_ts", "time_local", "time_source", "name", "card_uid",
               "decision", "reason", "similarity", "latency_s"]
    writer.writerow(columns)
    for row in rows:
        values = display_row(row, zone)
        writer.writerow([csv_cell(values[column]) for column in columns])
    return "\ufeff" + stream.getvalue()


CSS = r"""
:root{--ink:#132b38;--muted:#607481;--line:#dde6e9;--paper:#f3f7f8;--accent:#086a68;--grant:#087857;--deny:#b12d3c;--radius:16px}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}a{color:var(--accent)}
.topbar{background:#102f3b;color:white;padding:18px 32px;display:flex;gap:24px;align-items:center;justify-content:space-between}.brand{font-weight:750;letter-spacing:.02em}.smallcaps{font-size:11px;letter-spacing:.12em;text-transform:uppercase;font-weight:700}.topbar .smallcaps{color:#b8d4dd}.top-actions{display:flex;align-items:center;gap:18px}.top-actions form{margin:0}
main{max-width:1400px;margin:auto;padding:32px}.hero{display:flex;align-items:start;justify-content:space-between;gap:24px;margin-bottom:22px}h1{font-size:34px;letter-spacing:-.04em;line-height:1.15;margin:7px 0 12px}h2{font-size:19px;margin:0}.muted,small{color:var(--muted)}p{margin:8px 0}.tag{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:6px 12px;font-size:12px;background:white;white-space:nowrap}.tag.ok{color:var(--grant);background:#e8f5ef;border-color:#b5ddcf}.tag.bad{color:var(--deny);background:#fff0f0;border-color:#eec4c8}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:24px 0}.card{background:white;border:1px solid var(--line);border-radius:var(--radius);padding:22px;box-shadow:0 3px 12px #16344505}.metric{display:block;font-size:36px;font-weight:740;letter-spacing:-.04em;margin:4px 0}.grant{color:var(--grant)}.deny{color:var(--deny)}
.statusstrip{display:flex;gap:26px;flex-wrap:wrap;align-items:center;background:#e9f0f3;border:1px solid var(--line);padding:13px 18px;border-radius:12px;margin-bottom:22px;font-size:13px}.statusstrip strong{font-weight:650}.panel{background:white;border:1px solid var(--line);border-radius:var(--radius);overflow:hidden}.panelhead{padding:22px;display:flex;gap:20px;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line)}.controls{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
button,.button{border:1px solid #c9d6db;border-radius:8px;padding:9px 13px;background:white;color:var(--ink);font:inherit;font-size:13px;cursor:pointer;text-decoration:none;display:inline-block}button:hover,.button:hover{background:#edf4f5}button:disabled{opacity:.4;cursor:default}button.primary{background:var(--accent);color:white;border-color:var(--accent)}button.primary:hover{background:#075956}.topbar button{color:white;border-color:#56707a;background:transparent}
.filters{display:grid;grid-template-columns:minmax(180px,2fr) 1fr 1fr 1fr auto;gap:12px;padding:18px 22px;align-items:end;border-bottom:1px solid var(--line)}label{font-size:12px;font-weight:650;display:block}input,select{font:inherit;color:var(--ink);border:1px solid #c9d6db;border-radius:8px;padding:9px 10px;background:white;width:100%;min-width:0;margin-top:5px}input:focus,select:focus,button:focus-visible,a:focus-visible{outline:3px solid #88b8c9;outline-offset:2px}.filterbuttons{display:flex;gap:7px}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;text-align:left;font-size:13px;min-width:920px}th{color:#59717f;font-size:11px;text-transform:uppercase;letter-spacing:.06em;background:#f8fafb;padding:12px 18px;border-bottom:1px solid var(--line);white-space:nowrap}td{padding:15px 18px;border-bottom:1px solid #edf1f3;vertical-align:top}tbody tr:hover{background:#f9fbfc}td.reason{min-width:200px;max-width:360px;overflow-wrap:anywhere}td.identity{min-width:160px}td.time{white-space:nowrap}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}.badge{display:inline-block;border-radius:6px;padding:4px 8px;font-size:11px;font-weight:750;letter-spacing:.035em;background:#edf1f4}.badge.granted{color:var(--grant);background:#e8f5ef}.badge.denied{color:var(--deny);background:#fff0f0}.num{text-align:right;font-variant-numeric:tabular-nums}.subline{font-size:11px;color:var(--muted);display:block;margin-top:3px}.empty{padding:44px;text-align:center;color:var(--muted)}.pager{padding:16px 22px;display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px}.footnote{font-size:12px;color:var(--muted);margin-top:15px;max-width:1000px}.notice{padding:12px 16px;border-radius:10px;background:#fff1e3;color:#784415;margin:0 0 18px;border:1px solid #ecd4b9}.error{background:#fff0f0;color:#9d2335;border-color:#e9b7c0}.hidden{display:none!important}.login{max-width:470px;margin:64px auto}.login .card{padding:32px}.login h1{font-size:29px}.login label{margin-top:18px}.login button{margin-top:24px;width:100%}.login .footnote{margin-top:20px}.nowrap{white-space:nowrap}
@media(max-width:960px){.stats{grid-template-columns:repeat(2,1fr)}.filters{grid-template-columns:1fr 1fr}.filterbuttons{grid-column:1/-1}.hero{display:block}.hero .tag{margin-top:10px}main{padding:22px}.panelhead{align-items:flex-start;flex-direction:column}}
@media(max-width:560px){.topbar{padding:16px}.topbar .userlabel{display:none}main{padding:18px 12px}.stats{gap:10px}.card{padding:16px}.metric{font-size:30px}h1{font-size:29px}.filters{padding:14px}.panelhead{padding:16px}.statusstrip{gap:10px}.login{margin:30px 12px}.pager{align-items:start;flex-direction:column}}
"""

LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in | Cabinet access</title><link rel="stylesheet" href="/assets/app.css"></head><body>
<header class="topbar"><div><div class="brand">CABINET / ACCESS</div><div class="smallcaps">RFID + face verification</div></div><span class="smallcaps">Administrator view</span></header>
<main class="login"><section class="card"><div class="smallcaps muted">Monitoring dashboard</div><h1>Sign in to the audit log</h1><p class="muted">A read-only view of recorded access decisions.</p>
{% if error %}<p class="notice error" role="alert">{{ error }}</p>{% endif %}
<form method="post" action="/login"><input type="hidden" name="csrf" value="{{ csrf }}"><label>Username<input name="username" autocomplete="username" maxlength="64" required autofocus></label><label>Password<input type="password" name="password" autocomplete="current-password" maxlength="1024" required></label><button class="primary" type="submit">Sign in</button></form>
<p class="footnote">Use the dashboard account you created on dash-rpi. This is not your Raspberry Pi SSH password.</p></section><p class="footnote">Private prototype. Access through the SSH tunnel. No remote-unlock control or facial images are served.</p></main></body></html>"""

DASHBOARD_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Cabinet access | Audit dashboard</title><link rel="stylesheet" href="/assets/app.css"><script src="/assets/app.js" defer></script></head><body>
<header class="topbar"><div><div class="brand">CABINET / ACCESS</div><div class="smallcaps">RFID + face verification</div></div><div class="top-actions"><span class="userlabel">{{ username }}</span><form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf }}"><button type="submit">Sign out</button></form></div></header>
<main><section class="hero"><div><div class="smallcaps muted">Controller → MQTT → event store</div><h1>Access monitoring</h1><p class="muted">Recorded decisions from your cabinet. Times shown in Australia/Sydney.</p></div><span class="tag">Read-only · No remote unlock</span></section>
<div id="error" class="notice error hidden" role="alert"></div>
<section class="stats" aria-label="Filtered summary"><div class="card"><span class="smallcaps muted">Recorded attempts</span><strong class="metric" id="total">—</strong><small>Rows matching the filters</small></div><div class="card"><span class="smallcaps muted">Granted</span><strong class="metric grant" id="granted">—</strong><small>Decision = GRANTED</small></div><div class="card"><span class="smallcaps muted">Denied</span><strong class="metric deny" id="denied">—</strong><small>Decision = DENIED</small></div><div class="card"><span class="smallcaps muted">Mean recorded duration</span><strong class="metric" id="mean">—</strong><small>All filtered rows with a duration</small></div></section>
<section class="statusstrip" aria-label="Data freshness"><span id="db-state" class="tag">Loading database…</span><span><strong>Last stored decision:</strong> <span id="last-decision">—</span></span><span><strong>Last event received:</strong> <span id="last-received">—</span></span></section>
<section class="panel"><div class="panelhead"><div><h2>Event audit log</h2><p class="muted">Newest recorded events first</p></div><div class="controls"><span id="refresh-info" class="muted">Refreshes every 3 seconds</span><button id="pause" type="button">Pause refresh</button><a class="button" id="export" href="/export.csv">Export filtered CSV</a></div></div>
<form id="filters" class="filters"><label>Search name or card UID<input type="search" id="q" maxlength="128" placeholder="Name or card UID"></label><label>Decision<select id="decision"><option value="">All decisions</option value="GRANTED">Granted</option><option value="DENIED">Denied</option><option value="OTHER">Other / malformed</option></select></label><label>From date<input type="date" id="from"></label><label>To date<input type="date" id="to"></label><div class="filterbuttons"><button class="primary" type="submit">Apply</button><button type="button" id="today">Today</button><button type="button" id="clear">Clear</button></div></form>
<div class="tablewrap"><table><thead><tr><th>ID</th><th>Event time</th><th>User / card</th><th>Decision</th><th>Reason</th><th class="num">Similarity</th><th class="num">Decision time</th></tr></thead><tbody id="rows"><tr><td colspan="7" class="empty">Loading events…</td></tr></tbody></table></div>
<div class="pager"><span id="page-info">—</span><div class="controls"><button type="button" id="prev" disabled>Previous</button><button type="button" id="next" disabled>Next</button></div></div></section>
<p class="footnote"><strong>Measurement note:</strong> Decision time is the existing controller's latency_s value, recorded before subsequent logging and lock actuation. It is not a measured door-opening or end-to-end lock-response time. Similarity is a comparison score, not the probability that an identity is correct.</p>
<p class="footnote">Counts represent stored rows, not deduplicated physical attempts. A recent event is not a controller heartbeat, and a GRANTED decision is not sensor confirmation of the door or lock state. Raw messages and facial images are not displayed. CSV export is limited to 10,000 filtered rows.</p>
<p class="footnote" id="updated">Awaiting first refresh.</p></main></body></html>"""

JS = r"""
'use strict';
const $ = id => document.getElementById(id);
let page = 1, pages = 1, paused = false, busy = false, today = '';
let active = {q:'', decision:'', from:'', to:''};
function parameters(withPage=true){const p=new URLSearchParams(); for(const [k,v] of Object.entries(active)) if(v) p.set(k,v); if(withPage) p.set('page',page); return p;}
function cell(tr,text,cls=''){const td=document.createElement('td');td.textContent=text;td.className=cls;tr.appendChild(td);return td;}
function number(value,digits=2){return typeof value==='number' && Number.isFinite(value) ? value.toFixed(digits) : '—';}
function localtime(value){if(!value)return 'No events'; const d=new Date(value);return Number.isNaN(d.getTime())?'Unknown time':new Intl.DateTimeFormat('en-AU',{timeZone:'Australia/Sydney',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZoneName:'short'}).format(d);}
function showError(message){$('error').textContent=message;$('error').classList.remove('hidden');$('db-state').textContent='Refresh failed — data may be stale';$('db-state').className='tag bad';}
async function refresh(){
 if(busy)return;busy=true;
 try{
  const response=await fetch('/api/events?'+parameters(),{cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(10000)});
  if(response.status===401){window.location.replace('/login');return;}
  const data=await response.json();if(!response.ok)throw new Error(data.error||'Database query failed.');
  $('error').classList.add('hidden');$('db-state').textContent='Database read successful';$('db-state').className='tag ok';
  today=data.today;page=data.page;pages=data.pages;
  $('total').textContent=data.stats.total.toLocaleString();$('granted').textContent=data.stats.granted.toLocaleString();$('denied').textContent=data.stats.denied.toLocaleString();$('mean').textContent=data.stats.mean_duration===null?'—':number(data.stats.mean_duration)+' s';
  $('last-decision').textContent=data.last_record?data.last_record.decision:'No events';$('last-received').textContent=localtime(data.last_record?.received_ts);
  $('rows').replaceChildren();
  if(!data.events.length){const tr=document.createElement('tr');const td=cell(tr,'No recorded events match these filters.','empty');td.colSpan=7;$('rows').appendChild(tr);}
  for(const event of data.events){
   const tr=document.createElement('tr');cell(tr,String(event.id),'muted');const dt=cell(tr,event.time_local,'time');if(event.time_source!=='event'){const small=document.createElement('span');small.className='subline';small.textContent='Receipt time; event timestamp unavailable';dt.appendChild(small);}
   const ident=cell(tr,event.name||'Unidentified card','identity');const uid=document.createElement('code');uid.className='subline';uid.textContent=event.card_uid||'No card UID';ident.appendChild(uid);
   const status=cell(tr,'');const badge=document.createElement('span');badge.className='badge '+(event.decision==='GRANTED'?'granted':event.decision==='DENIED'?'denied':'');badge.textContent=event.decision;status.appendChild(badge);
   cell(tr,event.reason,'reason');cell(tr,event.similarity===null?'—':number(event.similarity)+'%','num');cell(tr,event.latency_s===null?'—':number(event.latency_s)+' s','num');$('rows').appendChild(tr);
  }
  const start=data.stats.total?((page-1)*data.page_size+1):0;const end=Math.min(page*data.page_size,data.stats.total);
  $('page-info').textContent=`Rows ${start}–${end} of ${data.stats.total} · Page ${page} of ${pages}`;
  $('prev').disabled=page<=1;$('next').disabled=page>=pages;$('export').href='/export.csv?'+parameters(false);
  $('updated').textContent='Dashboard last read: '+localtime(data.generated_at)+'. This is not an MQTT connection-status check.';
 }catch(error){showError(error.message||'Cannot reach dashboard. Check the server and SSH tunnel.');}finally{busy=false;}
}
function apply(){active={q:$('q').value.trim(),decision:$('decision').value,from:$('from').value,to:$('to').value};page=1;refresh();}
$('filters').addEventListener('submit',event=>{event.preventDefault();apply();});
$('clear').addEventListener('click',()=>{for(const id of ['q','decision','from','to'])$(id).value='';apply();});
$('today').addEventListener('click',()=>{if(today){$('from').value=today;$('to').value=today;apply();}});
$('pause').addEventListener('click',()=>{paused=!paused;$('pause').textContent=paused?'Resume refresh':'Pause refresh';$('refresh-info').textContent=paused?'Auto-refresh paused':'Refreshes every 3 seconds';if(!paused)refresh();});
$('prev').addEventListener('click',()=>{if(page>1){page--;refresh();}});$('next').addEventListener('click',()=>{if(page<pages){page++;refresh();}});
setInterval(()=>{if(!paused&&!document.hidden)refresh();},3000);refresh();
"""


def create_app(settings: Dict[str, Any]):
    # Import here so the database checker and unit tests need only stdlib.
    from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request, session, url_for

    app = Flask(__name__, static_folder=None)
    app.config.update(
        SECRET_KEY=settings["secret_key"], SESSION_COOKIE_NAME="cabinet_dashboard_session",
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        # Browser-to-loopback is HTTP; traffic between laptop and Pi goes through SSH.
        SESSION_COOKIE_SECURE=False, SESSION_REFRESH_EACH_REQUEST=False,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_SECONDS),
        MAX_CONTENT_LENGTH=8192, TRUSTED_HOSTS=["127.0.0.1", "localhost", "[::1]"],
    )
    db_path, zone = Path(settings["db_path"]), ZoneInfo(settings["timezone"])
    failures = deque()
    failure_lock = threading.Lock()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def logged_in():
        return (session.get("user") == settings["username"]
                and isinstance(session.get("login_at"), (int, float))
                and 0 <= time.time() - session["login_at"] < SESSION_SECONDS)

    @app.before_request
    def protect_routes():
        if request.method == "POST":
            submitted, expected = request.form.get("csrf", ""), session.get("csrf", "")
            if not expected or not hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8")):
                abort(400, "Form expired. Reload the page and try again.")
        if request.endpoint not in ("login", "css", "javascript") and not logged_in():
            if request.path.startswith("/api/"):
                return jsonify(error="Sign in again."), 401
            return redirect(url_for("login"))

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET" and logged_in():
            return redirect(url_for("dashboard"))
        error, status = None, 200
        if request.method == "POST":
            now = time.monotonic()
            with failure_lock:
                while failures and failures[0] < now - 60:
                    failures.popleft()
                blocked = len(failures) >= 5
            if blocked:
                error, status = "Too many failed sign-ins. Wait 60 seconds and try again.", 429
            else:
                username, password = request.form.get("username", ""), request.form.get("password", "")
                valid_password = password_matches(password[:1024], settings["password_hash"])
                if hmac.compare_digest(username.encode(), settings["username"].encode()) and valid_password:
                    session.clear()
                    session.permanent = True
                    session["user"] = settings["username"]
                    session["login_at"] = time.time()
                    session["csrf"] = secrets.token_urlsafe(32)
                    with failure_lock:
                        failures.clear()
                    return redirect(url_for("dashboard"))
                with failure_lock:
                    failures.append(now)
                error, status = "Incorrect username or password.", 401
        return render_template_string(LOGIN_HTML, error=error, csrf=csrf_token()), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        return render_template_string(DASHBOARD_HTML, username=settings["username"], csrf=csrf_token())

    @app.get("/assets/app.css")
    def css():
        return Response(CSS, mimetype="text/css")

    @app.get("/assets/app.js")
    def javascript():
        return Response(JS, mimetype="application/javascript")

    @app.get("/api/events")
    def events():
        try:
            return jsonify(read_page(db_path, zone, request.args))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        except sqlite3.Error:
            log.exception("Read-only events query failed")
            return jsonify(error="Cannot read the event database. Check the dashboard terminal; the listener is not changed."), 503

    @app.get("/export.csv")
    def export():
        try:
            data = read_csv(db_path, zone, request.args)
        except ValueError as error:
            return Response(str(error), status=400, mimetype="text/plain")
        except sqlite3.Error:
            log.exception("CSV query failed")
            return Response("Database unavailable. Try again later.", status=503, mimetype="text/plain")
        response = Response(data, mimetype="text/csv")
        response.headers["Content-Disposition"] = 'attachment; filename="cabinet-events.csv"'
        return response

    return app


def initialise(args):
    if SETTINGS_FILE.exists() and not args.reset:
        raise ValueError("Settings already exist. Leave them in place, or run init --reset to replace the login.")
    db_path = Path(args.db).expanduser().resolve()
    count = check_database(db_path)
    print("Existing database found: {} ({} rows). No database changes will be made.".format(db_path, count))
    username = (input("Dashboard username [admin]: ").strip() or "admin")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username):
        raise ValueError("Use 1–64 letters, numbers, dots, underscores or hyphens for the username.")
    password = getpass.getpass("New dashboard password (12+ characters): ")
    confirm = getpass.getpass("Repeat password: ")
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Choose a password between 12 and 1024 characters.")
    if password != confirm:
        raise ValueError("Passwords did not match. Nothing was saved.")
    settings = {"username": username, "password_hash": password_hash(password),
                "secret_key": secrets.token_hex(32), "db_path": str(db_path),
                "timezone": "Australia/Sydney"}
    ZoneInfo(settings["timezone"])
    # Atomic owner-only replacement. Never read or write the listener's .env.
    temporary = SETTINGS_FILE.with_name(".dashboard-settings-" + secrets.token_hex(8) + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=2)
            stream.write("\n")
        os.replace(str(temporary), str(SETTINGS_FILE))
    finally:
        if temporary.exists():
            temporary.unlink()
    print("Dashboard login created. Keep .dashboard-settings.json private and out of Git.")
    print("Your MQTT .env, listener.py and events.db were not changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    init = sub.add_parser("init", help="Create a private dashboard login; does not modify events.db")
    init.add_argument("--db", default=str(BASE / "events.db"))
    init.add_argument("--reset", action="store_true", help="Replace dashboard credentials and invalidate existing sessions")
    sub.add_parser("check", help="Check saved settings and existing DB schema without starting the web server")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "init":
            initialise(args)
            return
        settings = load_settings()
        count = check_database(Path(settings["db_path"]))
        if args.command == "check":
            print("Dashboard configuration OK; read-only database access OK; stored rows: {}".format(count))
            return
        from waitress import serve
        app = create_app(settings)
        print("\nDashboard listening on http://127.0.0.1:8080 (Pi loopback only).", flush=True)
        print("On your computer: ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 cqlm2@dash-rpi.local", flush=True)
        print("Then open http://127.0.0.1:8080 on that computer. Ctrl+C stops only the dashboard.\n", flush=True)
        serve(app, host="127.0.0.1", port=8080, threads=4,
              max_request_body_size=8192, channel_timeout=30, clear_untrusted_proxy_headers=True)
    except KeyboardInterrupt:
        print("\nDashboard stopped. The MQTT listener was not stopped.")
    except (ValueError, OSError, sqlite3.Error, ImportError, json.JSONDecodeError) as error:
        raise SystemExit("Dashboard setup error: " + str(error))


if __name__ == "__main__":
    main()
