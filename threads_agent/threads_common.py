"""Shared helpers: .env loading, Threads API calls, Google Sheets access."""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

HERE = Path(__file__).parent
THREADS_BASE = "https://graph.threads.net/v1.0"
SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
TAB = "queue"
HEADERS = ["id", "text", "topic_tag", "publish_at", "status", "approved",
           "post_id", "posted_at", "error"]


def load_env():
    env = {}
    path = HERE / ".env"
    if not path.exists():
        sys.exit(f"Missing {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def threads_call(method, path, params):
    url = f"{THREADS_BASE}/{path}"
    data = urllib.parse.urlencode(params)
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data}")
    else:
        req = urllib.request.Request(url, data=data.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Threads API {e.code}: {e.read().decode()}") from None


def sheets_session():
    creds = service_account.Credentials.from_service_account_file(
        str(HERE / "google_service_account.json"),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return AuthorizedSession(creds)


def read_rows(sess, sheet_id):
    r = sess.get(f"{SHEETS_BASE}/{sheet_id}/values/{TAB}!A1:I2000")
    r.raise_for_status()
    values = r.json().get("values", [])
    if not values:
        raise RuntimeError("Sheet is empty")
    header = [h.strip().lower() for h in values[0]]
    if header[: len(HEADERS)] != HEADERS:
        raise RuntimeError(f"Headers do not match. Found: {header}")
    rows = []
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (len(HEADERS) - len(row))
        rows.append((i, dict(zip(HEADERS, row))))
    return rows


def update_cells(sess, sheet_id, row_num, changes):
    """changes: {column_name: value}. Writes each cell in one batch call."""
    data = []
    for col, val in changes.items():
        letter = "ABCDEFGHI"[HEADERS.index(col)]
        data.append({"range": f"{TAB}!{letter}{row_num}", "values": [[val]]})
    r = sess.post(
        f"{SHEETS_BASE}/{sheet_id}/values:batchUpdate",
        json={"valueInputOption": "RAW", "data": data},
    )
    r.raise_for_status()
