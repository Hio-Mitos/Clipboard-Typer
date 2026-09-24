"""
Clipboard Typer - telemetry ingest + employee dashboard (REFERENCE / STARTER)
===============================================================================
This is a self-contained starting point for receiving the crash/install-
failure reports that clipboard_typer.py's opt-in telemetry sends (see that
file's "Diagnostics/telemetry" section). It is NOT a drop-in final answer -
you know your own site's framework, auth system, and hosting, this file
doesn't. Treat it as: "here is exactly what the app sends, here is a working
ingest route and a working (if basic) employee-only dashboard, wire it into
your real site."

What this gives you:
  - POST /api/telemetry           - the endpoint the app POSTs reports to.
                                     Validates a write-only API key, stores
                                     the report, and returns 204. Never
                                     returns any existing data - it's a
                                     write-only door, which is what makes it
                                     safe for the API key to live inside a
                                     distributed EXE (see the app's own
                                     comment on TELEMETRY_API_KEY).
  - GET  /dashboard                - a plain HTML table of recent reports,
                                     gated by _require_employee() below.
                                     Replace that function's body with your
                                     site's real login/session check - as
                                     shipped it's a single shared password
                                     from an environment variable, which is
                                     fine to test with but not how you
                                     should gate real employee access.
  - SQLite storage (telemetry.db, created automatically) - swap for your
    site's real database by replacing the six functions in the "Storage"
    section below; nothing else needs to change.

Run standalone to try it out:
    pip install flask
    set TELEMETRY_INGEST_KEY=some-long-random-string     (Windows: use `set`, not `export`)
    set TELEMETRY_DASHBOARD_PASSWORD=some-other-string
    python telemetry_backend.py
    -> ingest:    http://127.0.0.1:5000/api/telemetry
    -> dashboard: http://127.0.0.1:5000/dashboard

To fold into an existing Flask app instead of running standalone, import
`telemetry_bp` (a Blueprint) and `app.register_blueprint(telemetry_bp)`,
and delete the `if __name__ == "__main__"` block at the bottom.
If your site isn't Flask, use this file as the spec for what to build:
the request/response shapes and the SQL schema are the parts that matter.
"""

import json
import os
import sqlite3
import time
from contextlib import closing
from functools import wraps

from flask import Blueprint, Flask, abort, g, jsonify, request, Response

# ---------------------------------------------------------------------------
# Configuration - read from environment variables so the real values never
# end up committed to source control.
# ---------------------------------------------------------------------------
INGEST_API_KEY = os.environ.get("TELEMETRY_INGEST_KEY", "")           # must match TELEMETRY_API_KEY in the app
DASHBOARD_PASSWORD = os.environ.get("TELEMETRY_DASHBOARD_PASSWORD", "")  # temporary - replace with real employee auth
DB_PATH = os.environ.get("TELEMETRY_DB_PATH", os.path.join(os.path.dirname(__file__), "telemetry.db"))
MAX_BODY_BYTES = 64 * 1024  # a crash report is small; reject anything abnormally large outright

ALLOWED_REPORT_TYPES = {"crash", "install_failure"}

telemetry_bp = Blueprint("telemetry", __name__)


# ---------------------------------------------------------------------------
# Storage - SQLite for a quick start. To use your real database instead,
# replace the bodies of _init_db / _insert_report / _fetch_reports and
# leave every route above untouched.
# ---------------------------------------------------------------------------
def _init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_reports (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at     TEXT NOT NULL,
                report_type     TEXT NOT NULL,
                app_version     TEXT,
                packaged        INTEGER,
                os_version      TEXT,
                device_id       TEXT,
                event_timestamp TEXT,
                context         TEXT,
                exception_type  TEXT,
                exception_message TEXT,
                traceback       TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_telemetry_received_at ON telemetry_reports(received_at)"
        )
        conn.commit()


def _insert_report(report: dict):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            """
            INSERT INTO telemetry_reports
                (received_at, report_type, app_version, packaged, os_version, device_id,
                 event_timestamp, context, exception_type, exception_message, traceback)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                report.get("report_type"),
                report.get("app_version"),
                1 if report.get("packaged") else 0,
                report.get("os_version"),
                report.get("device_id"),
                report.get("timestamp_utc"),
                report.get("context"),
                report.get("exception_type"),
                report.get("exception_message"),
                report.get("traceback"),
            ),
        )
        conn.commit()


def _fetch_reports(limit=200):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM telemetry_reports ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Ingest endpoint - what clipboard_typer.py's _send_telemetry_report() POSTs to.
# ---------------------------------------------------------------------------
def _valid_api_key(req) -> bool:
    if not INGEST_API_KEY:
        return False  # refuse everything until you've actually set a key - fail closed, not open
    return req.headers.get("X-API-Key", "") == INGEST_API_KEY


@telemetry_bp.route("/api/telemetry", methods=["POST"])
def ingest_telemetry():
    if not _valid_api_key(request):
        abort(401)

    if request.content_length and request.content_length > MAX_BODY_BYTES:
        abort(413)

    try:
        report = request.get_json(force=True, silent=False)
    except Exception:
        abort(400)

    if not isinstance(report, dict) or report.get("report_type") not in ALLOWED_REPORT_TYPES:
        abort(400)

    # Defense in depth: truncate any oversized text field server-side too,
    # in case a future app version's redaction has a bug - the dashboard
    # doesn't need (and shouldn't store) unbounded text.
    for field in ("exception_message", "traceback", "context", "os_version", "app_version", "device_id"):
        value = report.get(field)
        if isinstance(value, str) and len(value) > 4000:
            report[field] = value[:4000]

    _insert_report(report)
    return Response(status=204)  # write-only - deliberately no response body, nothing to read back


# ---------------------------------------------------------------------------
# Employee dashboard - REPLACE _require_employee with your site's real
# login/session check before this goes anywhere near production. As shipped
# it's a single shared password via HTTP Basic Auth from an env var, which
# is only meant to get you unblocked for local testing.
# ---------------------------------------------------------------------------
def _require_employee(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not DASHBOARD_PASSWORD or not auth or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "Employee login required.", 401,
                {"WWW-Authenticate": 'Basic realm="Clipboard Typer telemetry"'},
            )
        return view(*args, **kwargs)
    return wrapped


_DASHBOARD_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Clipboard Typer - Telemetry</title>
  <style>
    body { font-family: Segoe UI, Arial, sans-serif; margin: 24px; color: #1f2430; }
    table { border-collapse: collapse; width: 100%; font-size: 13px; }
    th, td { border: 1px solid #e3e6ec; padding: 6px 8px; text-align: left; vertical-align: top; }
    th { background: #f7f9fc; }
    tr.crash { background: #fff5f5; }
    tr.install_failure { background: #fffaf0; }
    pre { white-space: pre-wrap; max-width: 480px; margin: 0; font-size: 11px; }
    .count { color: #8890a0; margin-bottom: 12px; }
  </style>
</head>
<body>
  <h2>Clipboard Typer - recent telemetry</h2>
  <p class="count">{{ n }} most recent reports</p>
  <table>
    <tr>
      <th>Received</th><th>Type</th><th>Version</th><th>Packaged</th><th>OS</th>
      <th>Device</th><th>Context</th><th>Exception</th><th>Traceback</th>
    </tr>
    {% for r in reports %}
    <tr class="{{ r.report_type }}">
      <td>{{ r.received_at }}</td>
      <td>{{ r.report_type }}</td>
      <td>{{ r.app_version }}</td>
      <td>{{ 'yes' if r.packaged else 'no' }}</td>
      <td>{{ r.os_version }}</td>
      <td>{{ (r.device_id or '')[:8] }}</td>
      <td>{{ r.context }}</td>
      <td>{{ r.exception_type }}: {{ r.exception_message }}</td>
      <td><pre>{{ r.traceback }}</pre></td>
    </tr>
    {% endfor %}
  </table>
</body>
</html>
"""


@telemetry_bp.route("/dashboard", methods=["GET"])
@_require_employee
def dashboard():
    from flask import render_template_string

    reports = _fetch_reports()
    return render_template_string(_DASHBOARD_TEMPLATE, reports=reports, n=len(reports))


# ---------------------------------------------------------------------------
# Standalone runner (for local testing only - delete this block once you've
# registered telemetry_bp on your real site).
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _init_db()
    app = Flask(__name__)
    app.register_blueprint(telemetry_bp)
    if not INGEST_API_KEY:
        print("WARNING: TELEMETRY_INGEST_KEY is not set - /api/telemetry will reject everything.")
    if not DASHBOARD_PASSWORD:
        print("WARNING: TELEMETRY_DASHBOARD_PASSWORD is not set - /dashboard will reject everything.")
    app.run(debug=True)
else:
    _init_db()
