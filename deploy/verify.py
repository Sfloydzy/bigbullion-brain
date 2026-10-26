"""Checks a deployment end to end and prints PASS / WARN / FAIL for every piece.

  python deploy/verify.py          check everything (read-only, plus one tiny Claude call)
  python deploy/verify.py --fix    also create missing sheet tabs and header rows

Run it after every setup step until nothing is FAIL. Exit code 1 if anything failed.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "threads_agent"))
sys.path.insert(0, str(ROOT / "x_agent"))
FIX = "--fix" in sys.argv
results = []


def report(status, area, msg):
    results.append(status)
    print(f"  [{status}] {area:<14} {msg}")


def section(name):
    print(f"\n{name}")


def http_json(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


# ---------------------------------------------------------------- brand
section("1. Brand file (brand/brand.json)")
try:
    import brandkit
    B = brandkit.B
    need = ["name", "handles", "timezone", "topics", "author_story", "voice", "first_person_rule", "post_rules", "pillars", "goal"]
    missing = [k for k in need if not B.get(k)]
    placeholders = [k for k in need if isinstance(B.get(k), str) and ("e.g." in B[k] or B[k].startswith(("First name", "the 3 to 6", "One or two", "Tone in", "what the writer", "Topic-specific")))]
    if missing:
        report("FAIL", "fields", "missing: " + ", ".join(missing))
    elif placeholders:
        report("FAIL", "fields", "still has template text: " + ", ".join(placeholders))
    else:
        report("PASS", "fields", f"{B['name']} · @{brandkit.HANDLE_THREADS} · {len(brandkit.PILLARS)} pillars · tz {brandkit.TZ_LABEL} (UTC{brandkit.B['timezone']['utc_offset_hours']:+})")
    report("PASS" if len(brandkit.KEYWORDS) >= 10 else "WARN", "keywords", f"{len(brandkit.KEYWORDS)} research keywords (aim for 15 to 30)")
    bad = [k["pillar"] for k in brandkit.KEYWORDS if k.get("pillar") not in brandkit.PILLARS]
    if bad:
        report("FAIL", "keywords", "keyword pillars not in pillars: " + ", ".join(sorted(set(bad))))
    mods = B.get("modules", {})
    report("PASS", "modules", ", ".join(f"{k}={'on' if v else 'off'}" for k, v in mods.items()))
except Exception as e:
    report("FAIL", "brand", f"cannot load brand/brand.json: {e}")
    print("\nFix the brand file first.")
    sys.exit(1)

# ---------------------------------------------------------------- env
section("2. Keys (threads_agent/.env)")
from threads_common import HEADERS, SHEETS_BASE, TAB, load_env, sheets_session  # noqa: E402

env = load_env()
up = {k.upper(): v for k, v in env.items() if v}
THREADS_ON = brandkit.module("threads")
req = (["THREADS_ACCESS_TOKEN", "THREADS_USER_ID"] if THREADS_ON else []) + ["GOOGLE_SHEET_ID", "ANTHROPIC_API_KEY"]
opt = {"THREADS_APP_ID": "token refresh", "THREADS_APP_SECRET": "token refresh"} if THREADS_ON else {}
if brandkit.module("x"):
    req += ["X_CONSUMER_KEY", "X_SECRET_KEY", "X_BEARER_TOKEN", "X_ACCESS_TOKEN"]
    if not (up.get("X_ACCESS_SECRET") or up.get("X_ACCESS_TOKEN_SECRET")):
        report("FAIL", ".env", "X_ACCESS_SECRET missing (needed to post on X)")
if brandkit.module("etf_facts"):
    req.append("POLYGON_API_KEY")
if brandkit.module("supabase"):
    req.append("SUPABASE_URL")
    if not (up.get("SUPABASE_SECRET_KEY") or up.get("SUPABASE_KEY")):
        report("FAIL", ".env", "SUPABASE_SECRET_KEY missing")
miss = [k for k in req if not up.get(k)]
report("FAIL" if miss else "PASS", ".env", ("missing: " + ", ".join(miss)) if miss else f"{len(req)} required keys present")
for k, why in opt.items():
    if not up.get(k):
        report("WARN", ".env", f"{k} missing ({why})")

# ---------------------------------------------------------------- google
section("3. Google Sheet")
sess = None
if not (ROOT / "threads_agent" / "google_service_account.json").exists():
    report("FAIL", "service acct", "threads_agent/google_service_account.json not found")
else:
    try:
        sess = sheets_session()
        meta = sess.get(f"{SHEETS_BASE}/{env['GOOGLE_SHEET_ID']}?fields=properties.title,sheets.properties.title")
        if meta.status_code != 200:
            raise RuntimeError(f"HTTP {meta.status_code}: share the sheet with the service account email as Editor")
        m = meta.json()
        tabs = [s["properties"]["title"] for s in m.get("sheets", [])]
        report("PASS", "sheet", f"'{m['properties']['title']}' reachable, {len(tabs)} tabs")
        if not THREADS_ON:
            report("PASS", "queue tab", "Threads is off; the X tabs are created on first run")
        elif TAB not in tabs:
            if FIX:
                sess.post(f"{SHEETS_BASE}/{env['GOOGLE_SHEET_ID']}:batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": TAB}}}]}).raise_for_status()
                tabs.append(TAB)
                report("PASS", "queue tab", "created")
            else:
                report("FAIL", "queue tab", f"no '{TAB}' tab (run with --fix)")
        if THREADS_ON and TAB in tabs:
            head = sess.get(f"{SHEETS_BASE}/{env['GOOGLE_SHEET_ID']}/values/{TAB}!A1:J1").json().get("values", [[]])
            head = [h.strip().lower() for h in (head[0] if head else [])]
            if head[:len(HEADERS)] == HEADERS:
                report("PASS", "queue tab", "headers ok")
            elif not head and FIX:
                sess.put(f"{SHEETS_BASE}/{env['GOOGLE_SHEET_ID']}/values/{TAB}!A1?valueInputOption=RAW", json={"values": [HEADERS + ["note"]]}).raise_for_status()
                report("PASS", "queue tab", "header row written")
            else:
                report("FAIL", "queue tab", f"row 1 must be: {', '.join(HEADERS)}, note" + ("" if head else " (run with --fix)"))
    except Exception as e:
        report("FAIL", "sheet", str(e)[:200])

# ---------------------------------------------------------------- threads
section("4. Threads")
try:
    if not THREADS_ON:
        raise StopIteration
    from threads_common import threads_call
    me = threads_call("GET", "me", {"fields": "id,username", "access_token": env["THREADS_ACCESS_TOKEN"]})
    ok = me.get("username", "").lower() == brandkit.HANDLE_THREADS.lower()
    report("PASS" if ok else "WARN", "account", f"token is for @{me.get('username')}" + ("" if ok else f" but brand.json says @{brandkit.HANDLE_THREADS}"))
    if str(me.get("id")) != str(env.get("THREADS_USER_ID", "")):
        report("WARN", "user id", f"THREADS_USER_ID should be {me.get('id')}")
    fc = threads_call("GET", "me/threads_insights", {"metric": "followers_count", "access_token": env["THREADS_ACCESS_TOKEN"]})
    report("PASS", "insights", f"{fc['data'][0]['total_value']['value']} followers readable")
except StopIteration:
    report("PASS", "threads", "module off (X only brand)")
except Exception as e:
    report("FAIL", "threads", str(e)[:200])

# ---------------------------------------------------------------- claude
section("5. Claude API")
try:
    model = json.loads((ROOT / "threads_agent" / "engine.json").read_text(encoding="utf-8"))["model"]
    r = http_json("https://api.anthropic.com/v1/messages", {"x-api-key": env["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                  json.dumps({"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "Reply OK"}]}).encode(), "POST")
    report("PASS", "anthropic", f"{model} answered")
except urllib.error.HTTPError as e:
    report("FAIL", "anthropic", f"HTTP {e.code}: {e.read().decode()[:150]}")
except Exception as e:
    report("FAIL", "anthropic", str(e)[:200])

# ---------------------------------------------------------------- x
section("6. X")
if not brandkit.module("x"):
    report("PASS", "x", "module off (disable the x_* tasks in dashboard/tasks.json)")
else:
    try:
        import xclient
        x = xclient.X(env)
        if not x.can_post:
            report("FAIL", "x keys", "posting keys incomplete")
        else:
            me = x.me()
            ok = me["username"].lower() == brandkit.HANDLE_X.lower()
            report("PASS" if ok else "WARN", "x account", f"@{me['username']}, {me['public_metrics']['followers_count']} followers" + ("" if ok else f" (brand.json says @{brandkit.HANDLE_X})"))
    except Exception as e:
        report("FAIL", "x", str(e)[:200] + "  (regenerate the access token AFTER setting Read and write)")

# ---------------------------------------------------------------- polygon
section("7. ETF facts (Polygon)")
if not brandkit.module("etf_facts"):
    report("PASS", "etf", "module off")
else:
    try:
        r = http_json(f"https://api.polygon.io/v2/aggs/ticker/SPY/prev?apiKey={env['POLYGON_API_KEY']}")
        report("PASS" if r.get("results") else "FAIL", "polygon", "SPY price readable" if r.get("results") else str(r)[:150])
    except Exception as e:
        report("FAIL", "polygon", str(e)[:200])

# ---------------------------------------------------------------- supabase
section("8. Supabase mirror")
if not brandkit.module("supabase"):
    report("PASS", "supabase", "module off")
else:
    try:
        key = up.get("SUPABASE_SECRET_KEY") or up.get("SUPABASE_KEY")
        h = {"apikey": key}
        if not key.startswith("sb_"):
            h["Authorization"] = f"Bearer {key}"
        http_json(f"{env['SUPABASE_URL'].rstrip('/')}/rest/v1/posts?select=post_id&limit=1", h)
        report("PASS", "supabase", "schema present and key works")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:150]
        report("FAIL", "supabase", f"HTTP {e.code}: {body}" + ("  (apply deploy/supabase_schema.sql)" if "posts" in body else ""))
    except Exception as e:
        report("FAIL", "supabase", str(e)[:200])

# ---------------------------------------------------------------- runner
section("9. Runner (dashboard/tasks.json)")
try:
    tasks = json.loads((ROOT / "dashboard" / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    bad = [t["id"] for t in tasks if t.get("enabled") and t.get("script") and not (ROOT / t["script"]).exists()]
    xon = [t["id"] for t in tasks if t.get("enabled") and str(t.get("script", "")).startswith("x_agent") and not brandkit.module("x")]
    report("FAIL" if bad else "PASS", "scripts", ("missing: " + ", ".join(bad)) if bad else f"{sum(1 for t in tasks if t.get('enabled'))} enabled tasks, all scripts exist")
    ton = [t["id"] for t in tasks if t.get("enabled") and str(t.get("script", "")).startswith("threads_agent") and not THREADS_ON]
    if ton:
        report("FAIL", "threads tasks", "Threads module is off but these tasks are enabled: " + ", ".join(ton))
    if xon:
        report("WARN", "x tasks", "X module is off but these tasks are enabled: " + ", ".join(xon))
    hb = ROOT / "dashboard" / "heartbeat.json"
    report("PASS" if hb.exists() else "WARN", "heartbeat", "runner has run on this machine" if hb.exists() else "runner not started yet (Windows: dashboard/setup_windows.bat, VPS: systemctl enable --now brand-runner@<name>.timer)")
except Exception as e:
    report("FAIL", "tasks", str(e)[:200])

fails = results.count("FAIL")
print(f"\n{'READY' if not fails else 'NOT READY'}: {results.count('PASS')} pass, {results.count('WARN')} warn, {fails} fail")
sys.exit(1 if fails else 0)
