"""X loop: research -> plan -> post -> review -> learn -> repeat.

  python x_engine.py cycle              daily 06:00: research, review, learn, plan today's posts
  python x_engine.py publish            every 5 min: post due rows; swap a reserve into any unticked row
  python x_engine.py review             every 3 h: metrics at 1h/6h/24h/72h/7d, per-post verdicts, x_review tab
  python x_engine.py research           what is working on X in the niche + reply targets with drafts
  python x_engine.py plan [--dry-run] [--force]   pick yesterday's best Threads posts, rewrite for X, schedule
  python x_engine.py learn              print what the X data says so far
  python x_engine.py weekly             Monday look-back for X
  python x_engine.py check              verify keys and print the account

Source material is your own Threads posts only. Research reads other people's posts for themes and
reply targets, never to copy wording.
"""
import argparse
import html
import json
import math
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

X_HERE = Path(__file__).parent
TH = X_HERE.parent / "threads_agent"
sys.path.insert(0, str(TH))
from insights import DB_DEFAULT, SGT, UTC, clean, latest_posts, open_db as open_threads_db, parse_ts, write_tab, ensure_tab  # noqa: E402
from threads_common import SHEETS_BASE, load_env, sheets_session  # noqa: E402
import engine as th  # noqa: E402  (Threads engine: learnings, banned patterns, hook labels)
import xclient  # noqa: E402
import brandkit  # noqa: E402

CFG = json.loads((X_HERE / "x.json").read_text(encoding="utf-8"))
DB_PATH = X_HERE / "x.db"
TAB = "x_queue"
HEAD = ["id", "slot_sgt", "text", "chars", "source", "source_lift", "test", "why", "status", "approved", "x_post_id", "posted_at", "url", "error", "note"]
CHECKPOINTS = [("1h", 1), ("6h", 6), ("24h", 24), ("72h", 72), ("7d", 168)]
K = 4
SCHEMA = """
CREATE TABLE IF NOT EXISTS x_posts(qid TEXT PRIMARY KEY, day TEXT, slot TEXT, text TEXT, source_id TEXT, source_lift REAL,
  source_views INTEGER, tag TEXT, hook TEXT, test TEXT, why TEXT, status TEXT, x_id TEXT, posted_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS x_reserves(id INTEGER PRIMARY KEY, day TEXT, text TEXT, source_id TEXT, source_lift REAL,
  source_views INTEGER, tag TEXT, hook TEXT, why TEXT, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS x_rejections(qid TEXT, text TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS x_snapshots(x_id TEXT, checkpoint TEXT, at TEXT, age_h REAL, impressions INTEGER, likes INTEGER,
  replies INTEGER, reposts INTEGER, quotes INTEGER, bookmarks INTEGER, PRIMARY KEY(x_id, checkpoint));
CREATE TABLE IF NOT EXISTS x_notes(x_id TEXT PRIMARY KEY, lift REAL, verdict TEXT, note TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS x_followers(day TEXT PRIMARY KEY, followers INTEGER, at TEXT);
CREATE TABLE IF NOT EXISTS x_research(tweet_id TEXT PRIMARY KEY, query TEXT, pillar TEXT, author TEXT, author_followers INTEGER,
  text TEXT, created_at TEXT, likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER, bookmarks INTEGER,
  impressions INTEGER, collected_at TEXT);
CREATE TABLE IF NOT EXISTS x_meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS x_usage(day TEXT, kind TEXT, n INTEGER, PRIMARY KEY(day, kind));
"""


# ---------------------------------------------------------------- basics
def now_sgt():
    return datetime.now(SGT)


def open_db(path=DB_PATH):
    import sqlite3
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def meta(db, k, v=None):
    if v is None:
        r = db.execute("SELECT v FROM x_meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None
    db.execute("INSERT OR REPLACE INTO x_meta VALUES(?,?)", (k, v))
    db.commit()


def usage(db, kind, n=0):
    day = now_sgt().date().isoformat()
    if n:
        db.execute("INSERT INTO x_usage VALUES(?,?,?) ON CONFLICT(day,kind) DO UPDATE SET n=n+?", (day, kind, n, n))
        db.commit()
    r = db.execute("SELECT n FROM x_usage WHERE day=? AND kind=?", (day, kind)).fetchone()
    return r["n"] if r else 0


def claude(system, user, key, model, max_tokens=8000):
    body = json.dumps({"model": model, "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API HTTP {e.code}: {e.read().decode()[:200]}") from None
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def parse_json(text, kind="["):
    m = re.search(r"\[.*\]" if kind == "[" else r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Claude did not return JSON.")
    return json.loads(m.group(0))


def truthy(v):
    return str(v).strip().upper() in ("TRUE", "YES", "Y", "1")


def slot_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SGT)


# ---------------------------------------------------------------- sheet
def read_tab(sess, sheet, tab=TAB):
    ensure_tab(sess, sheet, tab)
    r = sess.get(f"{SHEETS_BASE}/{sheet}/values/{tab}!A1:Z3000")
    r.raise_for_status()
    vals = r.json().get("values", [])
    if not vals:
        sess.put(f"{SHEETS_BASE}/{sheet}/values/{tab}!A1?valueInputOption=RAW", json={"values": [HEAD]}).raise_for_status()
        checkbox_column(sess, sheet, tab)
        return []
    head = [h.strip().lower() for h in vals[0]]
    return [(i, dict(zip(head, row + [""] * (len(head) - len(row))))) for i, row in enumerate(vals[1:], start=2)]


def checkbox_column(sess, sheet, tab):
    meta_ = sess.get(f"{SHEETS_BASE}/{sheet}?fields=sheets.properties").json()
    sid = next(s["properties"]["sheetId"] for s in meta_["sheets"] if s["properties"]["title"] == tab)
    col = HEAD.index("approved")
    sess.post(f"{SHEETS_BASE}/{sheet}:batchUpdate", json={"requests": [
        {"setDataValidation": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 3000, "startColumnIndex": col, "endColumnIndex": col + 1},
                               "rule": {"condition": {"type": "BOOLEAN"}}}},
        {"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}}]}).raise_for_status()


def col_letter(i):
    return chr(ord("A") + i)


def update_row(sess, sheet, row_num, changes, tab=TAB):
    data = [{"range": f"{tab}!{col_letter(HEAD.index(c))}{row_num}", "values": [[v]]} for c, v in changes.items()]
    sess.post(f"{SHEETS_BASE}/{sheet}/values:batchUpdate", json={"valueInputOption": "USER_ENTERED", "data": data}).raise_for_status()


def append_rows(sess, sheet, rows, tab=TAB):
    existing = [n for n, r in read_tab(sess, sheet, tab) if r.get("id", "").strip()]
    start = (existing[-1] + 1) if existing else 2
    sess.put(f"{SHEETS_BASE}/{sheet}/values/{tab}!A{start}?valueInputOption=USER_ENTERED", json={"values": rows}).raise_for_status()


# ---------------------------------------------------------------- learn (X)
def length_bucket(t):
    n = len(t)
    return "short (under 120)" if n < 120 else "medium (120 to 200)" if n <= 200 else "long (over 200)"


def xfeats(p):
    t = p["text"]
    return {"hook": p["hook"] or "unlabelled", "length": length_bucket(t), "ends_with_question": "yes" if t.strip().endswith("?") else "no",
            "number_in_opening": "yes" if re.search(r"\d", t[:60]) else "no", "tag": p["tag"] or "(none)",
            "slot": p["slot"][-5:], "test": p["test"] or "(none)",
            "threads_source": "strong (lift 1.5+)" if (p["source_lift"] or 0) >= 1.5 else "normal"}


def matured(db):
    return [dict(r) for r in db.execute("""SELECT p.*, s.impressions, s.likes, s.replies, s.reposts, s.quotes, s.bookmarks
        FROM x_posts p JOIN x_snapshots s ON s.x_id=p.x_id AND s.checkpoint='24h' WHERE p.status='posted' ORDER BY p.slot""")]


def x_learn(db):
    posts = matured(db)
    info = {"mature": len(posts), "baseline": None, "features": [], "confidence": "low"}
    if len(posts) < 5:
        return info
    base = max(math.exp(statistics.mean(math.log(max(p["impressions"] or 0, 1)) for p in posts)), 1)
    info["baseline"] = round(base)
    groups = {}
    for p in posts:
        for f, v in xfeats(p).items():
            groups.setdefault((f, v), []).append(math.log(max(p["impressions"] or 0, 1) / base))
    for (f, v), logs in sorted(groups.items()):
        n = len(logs)
        lift = math.exp(sum(logs) / (n + K))
        verdict = "insufficient data" if n < 3 else "WINNER" if lift >= 1.25 else "LOSER" if lift <= 0.8 else "neutral"
        info["features"].append({"feature": f, "value": v, "n": n, "lift": round(lift, 2), "verdict": verdict})
    info["confidence"] = "low" if len(posts) < 20 else "medium" if len(posts) < 60 else "higher"
    return info


def learn_text(info):
    if not info["features"]:
        return f"X: {info['mature']} posts have 24h data; need 5 before learning anything."
    lines = [f"X: {info['mature']} posts at 24h, typical {info['baseline']:,} impressions, confidence {info['confidence']}."]
    for f in sorted(info["features"], key=lambda x: (x["feature"], -x["lift"])):
        lines.append(f"  {f['feature']:<18} {f['value']:<26} n={f['n']:<3} lift {f['lift']:<5} {f['verdict']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- pick sources from Threads
def score_threads(tdb, days=14):
    now = datetime.now(UTC)
    posts = [p for p in latest_posts(tdb, days) if p["media_type"] != "REPOST_FACADE" and (p["text"] or "").strip()]
    for p in posts:
        p["age_h"] = max((now - parse_ts(p["posted_at"])).total_seconds() / 3600, 1)
    fit = [p for p in posts if p["age_h"] >= 2]
    xs = [math.log(p["age_h"]) for p in fit]
    ys = [math.log((p["views"] or 0) + 1) for p in fit]
    a, b = (statistics.mean(ys) if ys else 0), 0.0
    if len(fit) >= 8 and statistics.pvariance(xs) > 0:
        mx, my = statistics.mean(xs), statistics.mean(ys)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        b = min(max(b, 0.0), 1.5)  # views only grow with age
        a = my - b * mx
    rates = [p["rate"] for p in fit if p["views"]]
    mr = statistics.median(rates) if rates else 0
    for p in posts:
        p["lift"] = math.exp(math.log((p["views"] or 0) + 1) - (a + b * math.log(p["age_h"])))  # views vs what a post this old usually has
        p["score"] = p["lift"] * (((p["rate"] or 0) + 0.002) / (mr + 0.002)) ** 0.3
    return posts


SKIP_SOURCE = re.compile(r"\bthreads\b|\bapi\b|testing my|\bthis app\b", re.I)  # posts about the Threads platform or the automation itself


def pick_sources(tdb, db, n_main, n_total):
    used = {r["source_id"] for r in db.execute(
        "SELECT source_id FROM x_posts WHERE created_at>=? UNION SELECT source_id FROM x_reserves WHERE day>=?",
        ((datetime.now(UTC) - timedelta(days=CFG["source_reuse_days"])).isoformat(),
         (now_sgt().date() - timedelta(days=CFG["source_reuse_days"])).isoformat()))}
    posts = [p for p in score_threads(tdb) if p["post_id"] not in used and not SKIP_SOURCE.search(p["text"])]
    yday = now_sgt().date() - timedelta(days=1)
    ranked = sorted([p for p in posts if p["sgt"].date() == yday], key=lambda p: -p["score"])
    ranked += sorted([p for p in posts if p["sgt"].date() < yday and p["age_h"] <= 24 * 7], key=lambda p: -p["score"])  # fallback: rest of the week
    main, reserve, per_tag = [], [], Counter()
    for p in ranked:
        tag = p["topic_tag"] or "(none)"
        if len(main) < n_main and per_tag[tag] < CFG["max_per_tag"]:
            main.append(p)
            per_tag[tag] += 1
        elif len(main) + len(reserve) < n_total:
            reserve.append(p)
        if len(main) + len(reserve) >= n_total and len(main) >= n_main:
            break
    return main, reserve


# ---------------------------------------------------------------- plan
X_SYSTEM = (
    f"You adapt Threads posts into posts for X (Twitter) for a personal brand about {brandkit.TOPICS}. {brandkit.B['author_story']} "
    f"Voice: {brandkit.B['voice']} "
    "X style: one idea, the sharpest line first, no warm-up, every word earns its place, line breaks allowed. Keep the core "
    "idea of the source, but write it natively for X rather than trimming it. Hard rules: at most {max} characters; no "
    "hashtags; no links; at most one emoji and usually none; no em dashes or long dashes; never invent personal stories, "
    "income, results or numbers about the author (a first-person claim may be kept only if it is in the source, never "
    f"enlarged); {brandkit.B['x_rules']}. Reply with only a JSON list.")


def pick_tests(db, n):
    counts = Counter(r["test"] for r in db.execute("SELECT test FROM x_posts WHERE test IS NOT NULL AND test!=''"))
    return sorted(CFG["test_menu"], key=lambda t: (counts[t], CFG["test_menu"].index(t)))[:n]


def valid_x(text, seen):
    if not text or len(text) > CFG["max_chars"] or re.search(r"https?://|www\.|#\w", text):
        return False
    if any(re.search(b, text, re.I) for b in th.BANNED if b != r"#\w"):
        return False
    toks = set(re.findall(r"\w+", text.lower()))
    for s in seen:
        o = set(re.findall(r"\w+", s.lower()))
        if o and len(toks & o) / len(toks | o) >= 0.6:
            return False
    return True


def rewrite(items, ctx, key, seen):
    sysmsg = X_SYSTEM.format(max=CFG["max_chars"])
    ask = {"task": "Rewrite each source into one X post. Where 'test' is set, apply that one change and keep everything else in the "
                   "usual style. Label each result's hook style with one of: " + ", ".join(th.HOOKS) + ".",
           "output": "[{\"id\": \"...\", \"text\": \"...\", \"hook\": \"...\"}]", **ctx,
           "sources": [{"id": i["id"], "text": i["text"], "threads_views": i["views"], "lift_vs_typical": round(i["lift"], 2),
                        "tag": i["tag"], "test": i.get("test")} for i in items]}
    out, todo = {}, items
    for attempt in range(2):
        res = parse_json(claude(sysmsg, json.dumps(ask), key, CFG["writer_model"], 8000))
        for r in res:
            t = clean((r.get("text") or "").strip())
            if r.get("id") in {i["id"] for i in todo} and valid_x(t, seen + [o["text"] for o in out.values()]):
                out[r["id"]] = {"text": t, "hook": r.get("hook") if r.get("hook") in th.HOOKS else "other"}
        todo = [i for i in items if i["id"] not in out]
        if not todo:
            break
        ask["sources"] = [s for s in ask["sources"] if s["id"] in {i["id"] for i in todo}]
        ask["retry_note"] = f"Some drafts broke a rule (over {CFG['max_chars']} characters, a link or hashtag, a banned phrase, or too close to a recent post). Rewrite only these."
    return out


def plan(db, tdb, key, sess, sheet, dry=False, force=False, top_up=False):
    today = now_sgt().date()
    if not (force or top_up) and db.execute("SELECT COUNT(*) c FROM x_posts WHERE day=?", (today.isoformat(),)).fetchone()["c"]:
        print("Today is already planned. Use --force to plan again.")
        return
    cutoff = now_sgt() + timedelta(minutes=10)
    xinfo = x_learn(db)
    order = list(CFG["prime_slot_order"])
    slot_lift = {f["value"]: f["lift"] for f in xinfo["features"] if f["feature"] == "slot" and f["n"] >= 4}
    if slot_lift:
        order.sort(key=lambda s: -slot_lift.get(s, 1.0))
    slots = [s for s in order if datetime.combine(today, datetime.strptime(s, "%H:%M").time(), SGT) > cutoff][:CFG["posts_per_day"]]
    taken, base = set(), 0
    if top_up:  # add posts for free slots only; never touch rows already planned or posted today
        rows_today = db.execute("SELECT slot FROM x_posts WHERE day=?", (today.isoformat(),)).fetchall()
        taken, base = {r["slot"] for r in rows_today}, len(rows_today)
        slots = [s for s in slots if f"{today} {s}" not in taken]
    if not slots:
        print("No slots left today.")
        return
    if not brandkit.module("threads"):
        return plan_native(db, key, sess, sheet, today, slots, base, xinfo, dry)
    main, reserve = pick_sources(tdb, db, len(slots), CFG["candidates_per_day"])
    if not main:
        print("No usable Threads posts from yesterday or this week.")
        return
    tests = pick_tests(db, CFG["tests_per_day"])
    items = []
    for i, p in enumerate(main + reserve):
        items.append({"id": f"s{i}", "src": p, "text": p["text"], "views": p["views"] or 0, "lift": p["lift"], "tag": p["topic_tag"] or "",
                      "test": tests.pop(0) if (i < len(main) and i >= 2 and tests) else None})  # keep the top 2 untested
    tinfo = th.learn(tdb)
    ctx = {"what_works_on_x": [f"{f['feature']}={f['value']} lift {f['lift']} (n={f['n']})" for f in xinfo["features"] if f["verdict"] in ("WINNER", "LOSER")],
           "what_works_on_threads": [f"{f['feature']}={f['value']} lift {f['lift']} (n={f['n']})" for f in tinfo["features"] if f["verdict"] in ("WINNER", "LOSER")],
           "x_themes_today": json.loads(meta(db, "themes") or "[]"),
           "avoid_like_these_rejected": [r["text"] for r in db.execute("SELECT text FROM x_rejections ORDER BY at DESC LIMIT 8")]}
    seen = [r["text"] for r in db.execute("SELECT text FROM x_posts WHERE created_at>=?", ((datetime.now(UTC) - timedelta(days=21)).isoformat(),))]
    out = rewrite(items, ctx, key, seen)
    usable = [i for i in items if i["id"] in out]
    chosen = [i for i in usable if i["src"] in main]
    spare = [i for i in usable if i["src"] not in main]
    while len(chosen) < len(slots) and spare:
        chosen.append(spare.pop(0))
    chosen = chosen[:len(slots)]
    spare += [i for i in usable if i not in chosen and i not in spare]
    created = datetime.now(UTC).isoformat()
    rows, report = [], []
    for n, (i, s) in enumerate(zip(chosen, slots)):
        p, o = i["src"], out[i["id"]]
        slot = f"{today} {s}"
        qid = f"x{today:%m%d}-{base + n + 1}"
        why = f"Threads {p['views']} views, {p['lift']:.1f}x a typical post that age" + (f"; test: {i['test']}" if i["test"] else "")
        report.append((slot, o["text"], why))
        if dry:
            continue
        db.execute("INSERT OR REPLACE INTO x_posts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (qid, today.isoformat(), slot, o["text"], p["post_id"], round(p["lift"], 2), p["views"] or 0, p["topic_tag"] or "",
                    o["hook"], i["test"] or "", why, "scheduled", None, None, created))
        rows.append([qid, slot, o["text"], len(o["text"]), (p["permalink"] or p["post_id"]), round(p["lift"], 2), i["test"] or "", why,
                     "scheduled", "TRUE" if CFG["auto_approve"] else "FALSE", "", "", "", "", ""])
    if not dry:
        for i in spare:
            p, o = i["src"], out[i["id"]]
            db.execute("INSERT INTO x_reserves(day,text,source_id,source_lift,source_views,tag,hook,why) VALUES(?,?,?,?,?,?,?,?)",
                       (today.isoformat(), o["text"], p["post_id"], round(p["lift"], 2), p["views"] or 0, p["topic_tag"] or "", o["hook"],
                        f"Reserve. Threads {p['views']} views, {p['lift']:.1f}x typical"))
        db.commit()
        rows.sort(key=lambda r: r[1])
        append_rows(sess, sheet, rows)
    print(f"Planned {len(report)} X posts for {today} ({len(spare)} reserves){' [dry run]' if dry else ''}:")
    for slot, text, why in sorted(report):
        print(f"\n{slot}  ({len(text)} chars)  {why}\n{text}")


# ---------------------------------------------------------------- plan, X only (brand has no Threads)
MARKET_NOTES = brandkit.B.get("market_notes", [])
MONEY_RE = re.compile(r"(?:S\$|US\$|\$|SGD|USD)\s?\d[\d,]*(?:\.\d+)?\s*(?:k|m|million|billion)?|\d[\d,]*(?:\.\d+)?\s?%", re.I)
MARKET_NUMS = th._nums(" ".join(f"{m.get('note', '')}" for m in MARKET_NOTES))
MARKET_NUMS |= th._nums(" ".join(brandkit.B.get("allowed_figures", [])))  # e.g. purity figures that are general knowledge


def money_ok(text):
    """Prices, amounts and percentages may only come from MARKET NOTES or the facts, never from the model's memory."""
    found = set()
    for m in MONEY_RE.finditer(text):
        found |= th._nums(m.group(0))
    return found <= (MARKET_NUMS | th.FACT_NUMS)


def native_system():
    facts = brandkit.B.get("author_facts", [])
    notes = "; ".join(f"{m.get('source')} ({m.get('date')}): {m.get('note')}" for m in MARKET_NOTES)
    return (f"You write original posts for X (Twitter) for {brandkit.B['name']}. {brandkit.B['author_story']} Topics: {brandkit.TOPICS}. "
            f"Voice: {brandkit.B['voice']} X style: one idea, the sharpest line first, no warm-up, line breaks allowed. "
            "Hard rules: at most {max} characters; no hashtags; no links; at most one emoji and usually none; no em dashes or long "
            f"dashes; {brandkit.B['first_person_rule']}; {brandkit.B['post_rules']} "
            + (f"FACTS you may use (true): {' '.join(facts)} " if facts else "")
            + (f"MARKET NOTES (the only analyst views and figures you may use; always name the source and date, and present it as "
               f"their view, never ours): {notes} " if notes else "There are no market notes right now, so make no price forecasts "
               "and quote no prices, returns or percentages. ")
            + "Reply with only a JSON list.")


def plan_native(db, key, sess, sheet, today, slots, base, xinfo, dry):
    w = CFG.get("pillar_weights") or {}
    pillars = list(brandkit.PILLARS)
    w = {p: float(w.get(p, 0)) for p in pillars} if w and sum(w.values()) > 0 else {p: 1.0 for p in pillars}
    n_total = max(len(slots), CFG["candidates_per_day"])
    raw = {p: n_total * w[p] / sum(w.values()) for p in pillars}
    split = {p: int(raw[p]) for p in pillars}
    for p in sorted(pillars, key=lambda p: -(raw[p] - int(raw[p])))[:n_total - sum(split.values())]:
        split[p] += 1
    tests = pick_tests(db, CFG["tests_per_day"])
    seen = [r["text"] for r in db.execute("SELECT text FROM x_posts WHERE created_at>=?", ((datetime.now(UTC) - timedelta(days=21)).isoformat(),))]
    user = {"task": f"Write {n_total} new X posts. Split by pillar exactly: {json.dumps(split)}. Give the first {len(tests)} posts one test each "
                    f"from this list, in order, and keep everything else in the usual style: {json.dumps(tests)}. Label each hook with one of: "
                    + ", ".join(th.HOOKS) + ". Vary hooks and length; most posts under 200 characters.",
            "pillar_notes": CFG.get("pillar_notes", {}),
            "what_works_on_x": learn_text(xinfo),
            "x_themes_today": json.loads(meta(db, "themes") or "[]"),
            "do_not_repeat": [t[:110] for t in seen[:60]],
            "avoid_like_these_rejected": [r["text"] for r in db.execute("SELECT text FROM x_rejections ORDER BY at DESC LIMIT 8")],
            "output": "[{\"pillar\": \"...\", \"hook\": \"...\", \"test\": \"... or empty\", \"text\": \"...\"}]"}
    sysmsg = native_system().replace("{max}", str(CFG["max_chars"]))
    good = []
    for attempt in range(2):
        try:
            res = parse_json(claude(sysmsg, json.dumps(user), key, CFG["writer_model"], 12000))
        except Exception as e:
            print(f"Native plan attempt {attempt + 1} failed: {e}")
            continue
        for r in res:
            t = clean((r.get("text") or "").strip())
            if r.get("pillar") in pillars and valid_x(t, seen + [g["text"] for g in good]) and th.facts_only(t) and money_ok(t):
                good.append({"text": t, "pillar": r["pillar"], "hook": r.get("hook") if r.get("hook") in th.HOOKS else "other",
                             "test": r.get("test") if r.get("test") in tests else ""})
            else:
                print(f"Dropped: {t[:90]!r}")
        if len(good) >= len(slots):
            break
        user["task"] += f" Some drafts broke a rule (length, banned phrase, a number not in the notes, or too close to another post). Write {len(slots) - len(good)} more."
    if not good:
        print("No valid native posts.")
        return
    chosen, spare = good[:len(slots)], good[len(slots):]
    created = datetime.now(UTC).isoformat()
    rows = []
    for n, (g, s_) in enumerate(zip(chosen, slots)):
        slot, qid = f"{today} {s_}", f"x{today:%m%d}-{base + n + 1}"
        why = f"Native post, pillar {g['pillar']}" + (f"; test: {g['test']}" if g["test"] else "")
        if not dry:
            db.execute("INSERT OR REPLACE INTO x_posts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (qid, today.isoformat(), slot, g["text"], "native", 0, 0, g["pillar"], g["hook"], g["test"], why, "scheduled", None, None, created))
        rows.append([qid, slot, g["text"], len(g["text"]), "native", "", g["test"], why, "scheduled", "TRUE" if CFG["auto_approve"] else "FALSE", "", "", "", "", ""])
    if not dry:
        for g in spare:
            db.execute("INSERT INTO x_reserves(day,text,source_id,source_lift,source_views,tag,hook,why) VALUES(?,?,?,?,?,?,?,?)",
                       (today.isoformat(), g["text"], "native", 0, 0, g["pillar"], g["hook"], f"Reserve. Native post, pillar {g['pillar']}"))
        db.commit()
        rows.sort(key=lambda r: r[1])
        append_rows(sess, sheet, rows)
    print(f"Planned {len(rows)} native X posts for {today} ({len(spare)} reserves){' [dry run]' if dry else ''}:")
    for r in sorted(rows, key=lambda r: r[1]):
        print(f"\n{r[1]}  ({r[3]} chars)  {r[7]}\n{r[2]}")


# ---------------------------------------------------------------- publish
def publish(db, x, sess, sheet, dry=False):
    now = now_sgt()
    posted = 0
    for row_num, r in read_tab(sess, sheet):
        if r.get("status", "").strip().lower() != "scheduled" or not r.get("slot_sgt"):
            continue
        try:
            due = slot_dt(r["slot_sgt"].strip())
        except ValueError:
            continue
        qid = r["id"].strip()
        if not truthy(r.get("approved")):
            if (due - now).total_seconds() > 120:
                swap_in_reserve(db, sess, sheet, row_num, r, dry)
            elif due <= now:
                if not dry:
                    update_row(sess, sheet, row_num, {"status": "skipped", "error": "not approved at post time"})
                    db.execute("UPDATE x_posts SET status='skipped' WHERE qid=?", (qid,))
            continue
        if due > now:
            continue
        if (now - due).total_seconds() > CFG["max_late_minutes"] * 60:
            if not dry:
                update_row(sess, sheet, row_num, {"status": "expired", "error": f"more than {CFG['max_late_minutes']} minutes late"})
                db.execute("UPDATE x_posts SET status='expired' WHERE qid=?", (qid,))
            continue
        if posted >= 1:  # one post per run, so a backlog never fires in a burst
            break
        text = r["text"].strip()
        if not x.can_post:
            if r.get("error") != "waiting for X_ACCESS_TOKEN and X_ACCESS_SECRET in .env" and not dry:
                update_row(sess, sheet, row_num, {"error": "waiting for X_ACCESS_TOKEN and X_ACCESS_SECRET in .env"})
            print(f"{qid}: due, but posting keys are missing.")
            continue
        if dry:
            print(f"[dry run] would post {qid}: {text[:80]}")
            posted += 1
            continue
        try:
            d = x.post(text)
            stamp = now.strftime("%Y-%m-%d %H:%M")
            update_row(sess, sheet, row_num, {"status": "posted", "x_post_id": d["id"], "posted_at": stamp,
                                              "url": f"https://x.com/i/web/status/{d['id']}", "error": ""})
            db.execute("UPDATE x_posts SET status='posted', x_id=?, posted_at=?, text=? WHERE qid=?", (d["id"], datetime.now(UTC).isoformat(), text, qid))
            usage(db, "posts", 1)
            print(f"Posted {qid} -> {d['id']}")
            posted += 1
        except xclient.XError as e:
            update_row(sess, sheet, row_num, {"status": "failed", "error": str(e)[:300]})
            db.execute("UPDATE x_posts SET status='failed' WHERE qid=?", (qid,))
            print(f"{qid} failed: {e}")
    db.commit()


def swap_in_reserve(db, sess, sheet, row_num, r, dry):
    qid = r["id"].strip()
    res = db.execute("SELECT * FROM x_reserves WHERE used=0 AND day>=? ORDER BY day DESC, source_lift DESC LIMIT 1",
                     ((now_sgt().date() - timedelta(days=3)).isoformat(),)).fetchone()
    if not res:
        if r.get("error") != "unticked, no reserve left: it will be skipped" and not dry:
            update_row(sess, sheet, row_num, {"error": "unticked, no reserve left: it will be skipped"})
        return
    print(f"{qid}: unticked, swapping in reserve {res['id']}")
    if dry:
        return
    db.execute("INSERT INTO x_rejections VALUES(?,?,?)", (qid, r["text"], datetime.now(UTC).isoformat()))
    db.execute("UPDATE x_reserves SET used=1 WHERE id=?", (res["id"],))
    db.execute("UPDATE x_posts SET text=?, source_id=?, source_lift=?, source_views=?, tag=?, hook=?, test='', why=? WHERE qid=?",
               (res["text"], res["source_id"], res["source_lift"], res["source_views"], res["tag"], res["hook"], res["why"], qid))
    db.commit()
    update_row(sess, sheet, row_num, {"text": res["text"], "chars": len(res["text"]), "source": res["source_id"], "source_lift": res["source_lift"],
                                      "test": "", "why": res["why"] + " (replaced an unticked post)", "approved": "TRUE", "error": ""})


# ---------------------------------------------------------------- review
NOTE_SYSTEM = (f"You review single X posts for a personal brand ({brandkit.TOPICS_SHORT}). For each post, "
               "write one short line: why it likely did well or badly and one concrete change for next time. Use only the numbers given. "
               "Be direct, no flattery, no em dashes. Reply with only a JSON list [{\"id\": \"...\", \"note\": \"...\"}].")


def verdict(lift):
    return "EARLY" if lift is None else "WIN" if lift >= 1.5 else "FLOP" if lift <= 0.67 else "OK"


def review(db, x, key, sess, sheet):
    if not x.can_read:
        print("No X read keys yet.")
        return
    now = datetime.now(UTC)
    due = {}
    for p in db.execute("SELECT qid, x_id, posted_at FROM x_posts WHERE status='posted' AND x_id IS NOT NULL AND posted_at>=?",
                        ((now - timedelta(days=8)).isoformat(),)):
        age = (now - parse_ts(p["posted_at"])).total_seconds() / 3600
        have = {r["checkpoint"] for r in db.execute("SELECT checkpoint FROM x_snapshots WHERE x_id=?", (p["x_id"],))}
        cps = [c for c, h in CHECKPOINTS if age >= h and c not in have]
        if cps:
            due[p["x_id"]] = (age, cps)
    if due and usage(db, "reads") + len(due) <= CFG["max_reads_per_day"]:
        for t in x.tweets(list(due)):
            m, (age, cps) = t["public_metrics"], due[t["id"]]
            for c in cps:
                db.execute("INSERT OR REPLACE INTO x_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (t["id"], c, now.isoformat(), round(age, 1), m.get("impression_count"), m.get("like_count"), m.get("reply_count"),
                            m.get("retweet_count"), m.get("quote_count"), m.get("bookmark_count")))
        usage(db, "reads", len(due))
        db.commit()
    last = db.execute("SELECT MAX(at) a FROM x_followers").fetchone()["a"]
    if x.can_post and (not last or (now - parse_ts(last)).total_seconds() >= 3 * 3600):
        try:
            me = x.me()
            db.execute("INSERT OR REPLACE INTO x_followers VALUES(?,?,?)", (now_sgt().date().isoformat(), me["public_metrics"]["followers_count"], now.isoformat()))
            meta(db, "username", me.get("username", ""))
            db.commit()
        except xclient.XError as e:
            print("Follower read failed:", e)
    info = x_learn(db)
    base = info["baseline"]
    todo = [p for p in matured(db) if not db.execute("SELECT 1 FROM x_notes WHERE x_id=?", (p["x_id"],)).fetchone()]
    if todo and key:
        payload = [{"id": p["x_id"], "text": p["text"], "impressions_24h": p["impressions"], "likes": p["likes"], "replies": p["replies"],
                    "reposts": p["reposts"], "bookmarks": p["bookmarks"], "typical_impressions": base, "slot_sgt": p["slot"],
                    "test": p["test"] or None, "threads_lift_of_source": p["source_lift"]} for p in todo]
        try:
            notes = {n["id"]: n["note"] for n in parse_json(claude(NOTE_SYSTEM, json.dumps(payload), key, CFG["analyst_model"], 4000))}
        except Exception as e:
            print("Notes failed:", e)
            notes = {}
        for p in todo:
            lift = round(max(p["impressions"] or 0, 1) / base, 2) if base else None
            db.execute("INSERT OR REPLACE INTO x_notes VALUES(?,?,?,?,?)", (p["x_id"], lift, verdict(lift), clean(notes.get(p["x_id"], "")), now.isoformat()))
        db.commit()
    write_review_tab(db, sess, sheet, info)
    print(f"Review: {len(due)} posts measured, {len(todo)} new verdicts. {learn_text(info).splitlines()[0]}")


def write_review_tab(db, sess, sheet, info):
    snaps = {}
    for s in db.execute("SELECT * FROM x_snapshots"):
        snaps.setdefault(s["x_id"], {})[s["checkpoint"]] = s
    notes = {n["x_id"]: n for n in db.execute("SELECT * FROM x_notes")}
    fol = db.execute("SELECT day, followers FROM x_followers ORDER BY day DESC LIMIT 1").fetchone()
    head = ["slot_sgt", "verdict", "lift", "impr_1h", "impr_24h", "impr_72h", "impr_7d", "likes", "replies", "reposts", "bookmarks",
            "eng_rate", "test", "threads_lift", "note", "text", "url"]
    rows = [[f"X review. Typical post: {info['baseline'] or 'not enough data yet'} impressions at 24h. Followers: {fol['followers'] if fol else 'n/a'}. "
             f"Verdicts: WIN 1.5x typical or more, FLOP 0.67x or less."], head]
    for p in db.execute("SELECT * FROM x_posts WHERE status='posted' ORDER BY slot DESC LIMIT 200"):
        s = snaps.get(p["x_id"], {})
        last = s.get("7d") or s.get("72h") or s.get("24h") or s.get("6h") or s.get("1h")
        n = notes.get(p["x_id"])
        imp = lambda c: s[c]["impressions"] if c in s else ""
        eng = (sum((last[k] or 0) for k in ("likes", "replies", "reposts", "quotes", "bookmarks")) / last["impressions"]) if last and last["impressions"] else None
        rows.append([p["slot"], n["verdict"] if n else ("EARLY" if s else "waiting"), n["lift"] if n and n["lift"] else "",
                     imp("1h"), imp("24h"), imp("72h"), imp("7d"), last["likes"] if last else "", last["replies"] if last else "",
                     last["reposts"] if last else "", last["bookmarks"] if last else "", f"{eng:.1%}" if eng is not None else "",
                     p["test"], p["source_lift"], n["note"] if n else "", p["text"], f"https://x.com/i/web/status/{p['x_id']}"])
    write_tab(sess, sheet, "x_review", rows)


# ---------------------------------------------------------------- research
RESEARCH_SYSTEM = (
    f"You analyse what is getting traction on X in {brandkit.B.get('research_topics', brandkit.TOPICS)}, for a personal brand "
    f"{brandkit.B['author_short']}. Use only the posts given. Never copy their wording. No em dashes. "
    "Reply with only JSON: {\"themes\": [5 short lines on topics, angles and formats getting traction], \"angles_for_us\": [5 "
    "short post angles the author could write in his own voice from his own experience], \"replies\": [{\"id\": \"...\", \"reply\": "
    "\"...\"}]}. Each reply drafts a response to that post: under 200 characters, adds a specific point, example or sharp "
    "question, no self-promotion, no links, no flattery, never claims results or experiences the author has not stated. Leave out (no reply entry) any post that is political, "
    "partisan, insulting, a crypto or stock pump, or an ad.")


def research(db, x, key, sess, sheet, no_search=False):
    if not x.can_read:
        print("No X read keys yet.")
        return
    kw = brandkit.KEYWORDS
    n = 0 if no_search else CFG["research_queries_per_day"]
    start = (now_sgt().date().toordinal() * n) % len(kw)
    picks = [kw[(start + i) % len(kw)] for i in range(n)]
    stamp = datetime.now(UTC).isoformat()
    got = 0
    for k in picks:
        if usage(db, "reads") + CFG["research_results_per_query"] > CFG["max_reads_per_day"]:
            print("Daily read cap reached; stopping research early.")
            break
        q = f"\"{k['q']}\" lang:en -is:retweet -is:reply"
        try:
            res = x.search(q, CFG["research_results_per_query"])
        except xclient.XError as e:
            print(f"Search failed for {k['q']}: {e}")
            continue
        usage(db, "reads", len(res))
        for t in res:
            m, a = t["public_metrics"], t["author"]
            db.execute("INSERT OR REPLACE INTO x_research VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (t["id"], k["q"], k.get("pillar", ""), a.get("username", ""), a.get("public_metrics", {}).get("followers_count", 0),
                        html.unescape(t["text"]), t["created_at"], m.get("like_count"), m.get("reply_count"), m.get("retweet_count"), m.get("quote_count"),
                        m.get("bookmark_count"), m.get("impression_count"), stamp))
        got += len(res)
    db.commit()
    if not no_search:
        got += watchlist(db, x, stamp)
    since = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    rows = [dict(r) for r in db.execute("SELECT * FROM x_research WHERE collected_at>=? AND created_at>=?", (since, since))]
    me = (meta(db, "username") or "").lower()
    for r in rows:
        age = max((datetime.now(UTC) - parse_ts(r["created_at"].replace("Z", "+00:00"))).total_seconds() / 3600, 1)
        r["eng"] = (r["likes"] or 0) + 2 * (r["replies"] or 0) + 3 * (r["reposts"] or 0) + 3 * (r["quotes"] or 0) + 2 * (r["bookmarks"] or 0)
        r["velocity"] = r["eng"] / age
        r["age_h"] = age
    top = sorted([r for r in rows if r["author"].lower() != me], key=lambda r: -r["eng"])[:25]
    targets = sorted([r for r in rows if r["age_h"] <= 36 and 3000 <= (r["author_followers"] or 0) <= 2_000_000 and r["author"].lower() != me
                      and ((r["impressions"] or 0) >= 1000 or r["eng"] >= 25) and "t.co/" not in r["text"]],
                     key=lambda r: -r["velocity"])[:CFG["reply_targets"]]
    out = {"themes": [], "angles_for_us": [], "replies": []}
    if key and (top or targets):
        payload = {"top_posts": [{"id": r["tweet_id"], "text": r["text"][:400], "likes": r["likes"], "replies": r["replies"], "reposts": r["reposts"],
                                  "impressions": r["impressions"], "topic": r["query"]} for r in top],
                   "reply_to_these_ids": [r["tweet_id"] for r in targets],
                   "reply_target_texts": {r["tweet_id"]: r["text"][:400] for r in targets}}
        try:
            out = parse_json(claude(RESEARCH_SYSTEM, json.dumps(payload), key, CFG["analyst_model"], 6000), "{")
        except Exception as e:
            print("Research summary failed:", e)
    meta(db, "themes", json.dumps(out.get("themes", []) + out.get("angles_for_us", [])))
    drafts = {d.get("id"): clean(d.get("reply", "")) for d in out.get("replies", [])}
    sheet_rows = [[f"X research {now_sgt():%Y-%m-%d %H:%M}. {got} posts read today. Reply targets: reply by hand, it is the fastest way to grow a small account."],
                  ["WHAT IS GETTING TRACTION"]] + [[clean(t)] for t in out.get("themes", [])] + [["ANGLES FOR YOU"]] + \
                 [[clean(t)] for t in out.get("angles_for_us", [])] + [[""], ["REPLY TARGETS (last 24h, sorted by engagement per hour)"],
                 ["reply (opens X with the draft filled in)", "draft_reply", "author", "followers", "likes", "replies", "impressions", "posted_sgt", "text", "link"]]
    for r in [t for t in targets if drafts.get(t["tweet_id"])]:
        d = drafts[r["tweet_id"]]
        link = "https://x.com/intent/tweet?" + urllib.parse.urlencode({"in_reply_to": r["tweet_id"], "text": d})
        sheet_rows.append([link, d, "@" + r["author"], r["author_followers"], r["likes"], r["replies"], r["impressions"],
                           parse_ts(r["created_at"].replace("Z", "+00:00")).astimezone(SGT).strftime("%Y-%m-%d %H:%M"), r["text"][:280],
                           f"https://x.com/{r['author']}/status/{r['tweet_id']}"])
    write_tab(sess, sheet, "x_research", sheet_rows)
    print(f"Research: {got} posts read, {len(targets)} reply targets, {len(out.get('themes', []))} themes.")


def watchlist(db, x, stamp):
    """Read the latest posts of the biggest-reach niche accounts research has found: reliable, fresh reply targets."""
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    rows = db.execute("""SELECT author, MAX(author_followers) f, MAX(impressions) imp FROM x_research
        WHERE collected_at>=? AND author_followers BETWEEN 10000 AND 2000000 AND query!='watchlist' GROUP BY author
        ORDER BY imp DESC LIMIT ?""", (since, CFG["watchlist_size"])).fetchall()
    names = [r["author"] for r in rows if r["author"]]
    if not names or usage(db, "reads") + len(names) * 6 > CFG["max_reads_per_day"]:
        return 0
    ids = json.loads(meta(db, "watch_ids") or "{}")
    missing = [n for n in names if n not in ids]
    if missing:
        users = x.users_by(missing)
        usage(db, "reads", len(users))
        ids.update({u["username"]: u["id"] for u in users})
        meta(db, "watch_ids", json.dumps(ids))
    got = 0
    for n in names:
        if n not in ids:
            continue
        try:
            res = x.user_tweets(ids[n], 5)
        except xclient.XError as e:
            print(f"Watchlist read failed for @{n}: {e}")
            continue
        usage(db, "reads", len(res))
        f = next(r["f"] for r in rows if r["author"] == n)
        for t in res:
            m = t["public_metrics"]
            db.execute("INSERT OR REPLACE INTO x_research VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (t["id"], "watchlist", "", n, f, html.unescape(t["text"]), t["created_at"], m.get("like_count"), m.get("reply_count"),
                        m.get("retweet_count"), m.get("quote_count"), m.get("bookmark_count"), m.get("impression_count"), stamp))
        got += len(res)
    db.commit()
    print(f"Watchlist: {len(names)} accounts, {got} posts.")
    return got


# ---------------------------------------------------------------- weekly
WEEKLY_SYSTEM = (
    f"You are the growth analyst for a personal brand on X ({brandkit.TOPICS_SHORT}). Direct, "
    "specific, no flattery, only the numbers given, say when samples are small. No em dashes. Plain text under 350 words with "
    "these headings on their own lines: THE WEEK IN NUMBERS, WHAT WORKED, WHAT DID NOT, TEST VERDICTS (keep, drop or retest, "
    "with sample size), " + ("DOES THREADS SUCCESS CARRY OVER (compare Threads lift of the source with X lift), " if brandkit.module("threads") else "") + "NEXT WEEK (3 changes).")


def weekly(db, key, sess, sheet):
    since = (now_sgt() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    posts = [p for p in matured(db) if p["slot"] >= since]
    info = x_learn(db)
    notes = {n["x_id"]: n for n in db.execute("SELECT * FROM x_notes")}
    fol = [dict(r) for r in db.execute("SELECT day, followers FROM x_followers ORDER BY day")]
    tests = {}
    for p in posts:
        if p["test"] and notes.get(p["x_id"]) and notes[p["x_id"]]["lift"]:
            tests.setdefault(p["test"], []).append(notes[p["x_id"]]["lift"])
    stats = {"posts_with_24h_data": len(posts), "typical_impressions": info["baseline"],
             "followers_start": next((f["followers"] for f in fol if f["day"] >= since[:10]), None), "followers_now": fol[-1]["followers"] if fol else None,
             "posts": [{"text": p["text"][:120], "impressions_24h": p["impressions"], "likes": p["likes"], "replies": p["replies"],
                        "bookmarks": p["bookmarks"], "x_lift": notes.get(p["x_id"], {})["lift"] if notes.get(p["x_id"]) else None,
                        "threads_lift": p["source_lift"], "test": p["test"] or None, "slot": p["slot"][-5:]} for p in posts],
             "tests": {t: {"n": len(v), "avg_lift": round(statistics.mean(v), 2)} for t, v in tests.items()},
             "feature_learnings": [f for f in info["features"] if f["verdict"] != "insufficient data"],
             "reads_used_7d": sum(r["n"] for r in db.execute("SELECT n FROM x_usage WHERE kind='reads' AND day>=?", (since[:10],))),
             "posts_used_7d": sum(r["n"] for r in db.execute("SELECT n FROM x_usage WHERE kind='posts' AND day>=?", (since[:10],)))}
    text = claude(WEEKLY_SYSTEM, json.dumps(stats), key, CFG["analyst_model"], 6000) if posts else "No X posts with 24h data this week yet."
    body = f"X WEEKLY LOOK-BACK  {now_sgt():%Y-%m-%d}\n\n{clean(text)}\n\nAPI use this week: {stats['posts_used_7d']} posts, {stats['reads_used_7d']} reads."
    (X_HERE / "reviews" / f"x_weekly_{now_sgt():%Y-%m-%d}.md").write_text(body + "\n", encoding="utf-8")
    write_tab(sess, sheet, "x_weekly", [[line] for line in body.splitlines()])
    print(body)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["cycle", "publish", "review", "research", "plan", "learn", "weekly", "check"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--top-up", action="store_true", help="plan: add posts only for free slots today, keep everything already planned")
    ap.add_argument("--no-search", action="store_true", help="research: rebuild the tab from stored posts, no new reads")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--threads-db", default=str(DB_DEFAULT))
    a = ap.parse_args()
    env = load_env()
    key, sheet = env.get("ANTHROPIC_API_KEY"), env["GOOGLE_SHEET_ID"]
    x = xclient.X(env)
    db = open_db(a.db)
    if a.cmd == "check":
        print(f"Posting keys: {'ok' if x.can_post else 'missing X_ACCESS_TOKEN / X_ACCESS_SECRET'}. Read keys: {'ok' if x.can_read else 'missing'}.")
        if x.can_post:
            me = x.me()
            print(f"Account @{me['username']}, {me['public_metrics']['followers_count']} followers.")
        return
    if a.cmd == "learn":
        print(learn_text(x_learn(db)))
        return
    sess = sheets_session()
    if a.cmd == "publish":
        publish(db, x, sess, sheet, a.dry_run)
    elif a.cmd == "review":
        review(db, x, key, sess, sheet)
    elif a.cmd == "research":
        research(db, x, key, sess, sheet, a.no_search)
    elif a.cmd == "weekly":
        weekly(db, key, sess, sheet)
    else:
        tdb = open_threads_db(a.threads_db)
        if a.cmd == "cycle":
            for name, fn in (("research", lambda: research(db, x, key, sess, sheet)), ("review", lambda: review(db, x, key, sess, sheet))):
                try:
                    fn()
                except Exception as e:
                    print(f"{name} failed, carrying on: {e}")
        plan(db, tdb, key, sess, sheet, a.dry_run, a.force, a.top_up)
        print(f"Reads used today: {usage(db, 'reads')} of {CFG['max_reads_per_day']}.")


if __name__ == "__main__":
    main()
