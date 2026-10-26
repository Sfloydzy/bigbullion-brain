"""Mirror brain.db to Supabase (Postgres) after each brain sync.

Needs in threads_agent/.env: SUPABASE_URL and SUPABASE_SECRET_KEY (a sb_secret_... key, or the legacy service_role key).
Append-only tables (obs, followers, research) are sent incrementally by SQLite rowid; the small tables that
change in place (posts, labels, attribution, scores, feedback, research_themes) are upserted in full.
The local SQLite file stays the working copy, so posting never depends on Supabase being reachable.

  python supa_sync.py            push changes now
  python supa_sync.py --full     resend everything
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

TABLES = {  # table: (conflict columns, incremental?)
    "posts": ("platform,post_id", False),
    "labels": ("platform,post_id", False),
    "attribution": ("platform,post_id", False),
    "scores": ("platform,post_id,checkpoint", False),
    "feedback": ("platform,queue_id,kind", False),
    "research_themes": ("day,platform,text", False),
    "obs": ("platform,post_id,at", True),
    "followers": ("platform,at", True),
    "research": ("platform,ext_id", True),
}
BATCH = 500


def headers(key):
    h = {"apikey": key, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}
    if not key.startswith("sb_"):  # legacy JWT keys also go in Authorization
        h["Authorization"] = f"Bearer {key}"
    return h


def post(url, key, table, conflict, rows):
    req = urllib.request.Request(f"{url}/rest/v1/{table}?on_conflict={conflict}", data=json.dumps(rows).encode(),
                                 method="POST", headers=headers(key))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase {table} {e.code}: {e.read().decode()[:300]}") from None


def clean_row(table, r):
    d = dict(r)
    d.pop("_rowid", None)
    if table == "scores" and d.get("estimated") is not None:
        d["estimated"] = bool(d["estimated"])
    for k, v in list(d.items()):
        if v == "":
            d[k] = None if k not in ("text", "note") else v
    return d


def push(db, env, full=False):
    url = (env.get("SUPABASE_URL") or "").rstrip("/")
    key = env.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_KEY") or env.get("SUPABASE_SERVICE_KEY") or env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return "Supabase: skipped (SUPABASE_URL or SUPABASE_SECRET_KEY missing in .env)."
    db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    sent, detail = 0, {}
    for table, (conflict, incremental) in TABLES.items():
        mk = f"supa_rowid_{table}"
        last = 0
        if incremental and not full:
            r = db.execute("SELECT v FROM meta WHERE k=?", (mk,)).fetchone()
            last = int(r[0]) if r else 0
        rows = db.execute(f"SELECT rowid AS _rowid, * FROM {table} WHERE rowid>? ORDER BY rowid", (last,)).fetchall()
        if not rows:
            continue
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            post(url, key, table, conflict, [clean_row(table, r) for r in chunk])
            if incremental:
                db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (mk, str(chunk[-1]["_rowid"])))
                db.commit()
        sent += len(rows)
        detail[table] = len(rows)
    try:
        post(url, key, "sync_runs", "id", [{"rows_sent": sent, "detail": detail}])
    except RuntimeError:
        pass
    return f"Supabase: {sent} rows sent {detail}" if sent else "Supabase: up to date."


if __name__ == "__main__":
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "threads_agent"))
    from threads_common import load_env
    import sqlite3
    db = sqlite3.connect(str(Path(__file__).parent / "brain.db"), timeout=30)
    db.row_factory = sqlite3.Row
    print(push(db, load_env(), "--full" in sys.argv))
