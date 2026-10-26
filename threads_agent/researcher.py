"""Threads researcher, Stage 1: collect, store, rank.

  python researcher.py run  [--provider meta|mock] [--db PATH] [--max-queries N] [--recent] [--no-sheet] [--reprobe]
  python researcher.py top  [--db PATH] [--n 20]

Collects public posts for the keywords in keywords.json, stores every post and an
engagement snapshot in SQLite, ranks them, and writes the top posts to the
'research' tab of the Google Sheet. Standard library plus google-auth/requests.
"""
import argparse
import hashlib
import json
import re
import random
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

from threads_common import HERE, SHEETS_BASE, load_env, sheets_session, threads_call

UTC = timezone.utc
DB_DEFAULT = HERE / "research.db"
BASE_FIELDS = "id,text,timestamp,username,permalink,media_type,is_reply,is_quote_post,has_replies"
ENG_FIELDS = "like_count,reply_count,repost_count,quote_count,views"
TOP_N_SHEET = 25
# Starting weights, not confirmed Threads ranking signals. Tune with your own results.
WEIGHTS = {"rank": 0.25, "breadth": 0.15, "recency": 0.10,
           "velocity": 0.25, "outperform": 0.15, "conversation": 0.10}


class Fatal(Exception):
    pass


# ---------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(post_id TEXT PRIMARY KEY, username TEXT, text TEXT, permalink TEXT,
  posted_at TEXT, media_type TEXT, is_reply INTEGER, is_quote INTEGER, has_replies INTEGER,
  first_seen TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS hits(id INTEGER PRIMARY KEY, post_id TEXT, keyword TEXT, mode TEXT,
  search_type TEXT, rank INTEGER, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, post_id TEXT, fetched_at TEXT,
  likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER, views INTEGER);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started TEXT, finished TEXT, provider TEXT,
  queries INTEGER, posts_seen INTEGER, new_posts INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_hits_time ON hits(fetched_at);
CREATE INDEX IF NOT EXISTS idx_snap_post ON snapshots(post_id, fetched_at);
"""


def open_db(path):
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def meta_get(db, k):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def meta_set(db, k, v):
    db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# ---------------------------------------------------------------- providers
def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("+0000", "+00:00").replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def norm(x):
    ts = parse_ts(x.get("timestamp"))
    return {
        "post_id": str(x.get("id")), "username": x.get("username") or "", "text": x.get("text") or "",
        "permalink": x.get("permalink") or "", "posted_at": ts.isoformat() if ts else None,
        "media_type": x.get("media_type") or "", "is_reply": int(bool(x.get("is_reply"))),
        "is_quote": int(bool(x.get("is_quote_post"))), "has_replies": int(bool(x.get("has_replies"))),
        "likes": x.get("like_count"), "replies": x.get("reply_count"),
        "reposts": x.get("repost_count"), "quotes": x.get("quote_count"), "views": x.get("views"),
    }


class MetaProvider:
    name = "meta"

    def __init__(self, token, db, reprobe=False):
        self.token, self.db = token, db
        if reprobe:
            meta_set(db, "engagement_fields", "unknown")
        self.fields = BASE_FIELDS if meta_get(db, "engagement_fields") == "no" else BASE_FIELDS + "," + ENG_FIELDS

    def search(self, q, mode, search_type, limit=50):
        params = {"q": q, "search_type": search_type, "search_mode": mode, "fields": self.fields,
                  "limit": limit, "access_token": self.token}
        try:
            data = threads_call("GET", "keyword_search", params)
        except RuntimeError as e:
            s = str(e)
            if self.fields != BASE_FIELDS and ("nonexisting field" in s or "(#100)" in s or '"code":100' in s):
                self.fields = BASE_FIELDS
                meta_set(self.db, "engagement_fields", "no")
                self.db.commit()
                return self.search(q, mode, search_type, limit)
            if "does not have permission" in s or "permission" in s.lower() and "403" in s:
                raise Fatal("Meta refused the search: this token or app lacks threads_keyword_search. "
                            "Regenerate the token in the User Token Generator after the permission is "
                            "added, update THREADS_ACCESS_TOKEN in .env, and run again.")
            if "rate limit" in s.lower() or '"code":4,' in s or '"code":613' in s:
                raise Fatal("Meta rate limit reached. Stopping this run; it will resume next time.")
            raise
        if self.fields == BASE_FIELDS + "," + ENG_FIELDS and meta_get(self.db, "engagement_fields") != "yes":
            meta_set(self.db, "engagement_fields", "yes")
        return [norm(x) for x in data.get("data", [])]


class MockProvider:
    """Synthetic data for testing the pipeline. Never use for real research."""
    name = "mock"
    USERS = ["maya_builds", "dev_dan", "sales.sam", "frugal.fiona", "ceo_in_training", "quietcompounder", "nina.invests", "hustle_hq"]

    def __init__(self):
        self.pool = []
        rnd = random.Random(7)
        now = datetime.now(UTC)
        for i in range(120):
            age = rnd.uniform(0.5, 60)
            likes = int(rnd.paretovariate(1.2) * 20)
            self.pool.append({
                "id": f"mock{i}", "text": f"Synthetic post {i} about money and business.",
                "timestamp": (now - timedelta(hours=age)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                "username": self.USERS[i % len(self.USERS)], "permalink": f"https://example.invalid/{i}",
                "media_type": "TEXT_POST", "is_reply": False, "is_quote_post": False, "has_replies": likes > 40,
                "like_count": likes, "reply_count": likes // 8, "repost_count": likes // 12,
                "quote_count": likes // 30, "views": likes * 25})

    def search(self, q, mode, search_type, limit=50):
        seed = int(hashlib.md5(f"{q}{mode}{search_type}".encode()).hexdigest(), 16)
        rnd = random.Random(seed)
        picks = rnd.sample(self.pool, 20)
        if search_type == "TOP":
            picks.sort(key=lambda p: -p["like_count"])
        # engagement drifts a little each call so snapshots differ
        out = []
        for p in picks:
            p = dict(p)
            bump = random.random() * 0.05
            p["like_count"] = int(p["like_count"] * (1 + bump))
            out.append(norm(p))
        return out


# ---------------------------------------------------------------- collect
def own_username(provider, token):
    if provider.name == "mock":
        return __import__("brandkit").HANDLE_THREADS
    return threads_call("GET", "me", {"fields": "username", "access_token": token}).get("username", "")


def collect(db, provider, own, max_queries, recent):
    kw = {"keywords": __import__("brandkit").KEYWORDS}
    jobs = [(k["q"], "KEYWORD") for k in kw["keywords"]] + [(t, "TAG") for t in kw["tags"]]
    types = ["TOP"] + (["RECENT"] if recent else [])
    started = datetime.now(UTC).isoformat()
    queries = seen = new = 0
    note = ""
    try:
        for st in types:
            for q, mode in jobs:
                if queries >= max_queries:
                    note = f"stopped at max-queries ({max_queries})"
                    raise StopIteration
                results = provider.search(q, mode, st)
                queries += 1
                now = datetime.now(UTC).isoformat()
                for rank, p in enumerate(results, start=1):
                    if p["username"] == own:
                        continue
                    seen += 1
                    cur = db.execute("INSERT OR IGNORE INTO posts(post_id,username,text,permalink,posted_at,media_type,"
                                     "is_reply,is_quote,has_replies,first_seen,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                     (p["post_id"], p["username"], p["text"], p["permalink"], p["posted_at"],
                                      p["media_type"], p["is_reply"], p["is_quote"], p["has_replies"], now, provider.name))
                    new += cur.rowcount
                    db.execute("INSERT INTO hits(post_id,keyword,mode,search_type,rank,fetched_at) VALUES(?,?,?,?,?,?)",
                               (p["post_id"], q, mode, st, rank, now))
                    last = db.execute("SELECT fetched_at FROM snapshots WHERE post_id=? ORDER BY id DESC LIMIT 1",
                                      (p["post_id"],)).fetchone()
                    if not last or last["fetched_at"][:13] != now[:13]:  # at most one snapshot per hour
                        db.execute("INSERT INTO snapshots(post_id,fetched_at,likes,replies,reposts,quotes,views) VALUES(?,?,?,?,?,?,?)",
                                   (p["post_id"], now, p["likes"], p["replies"], p["reposts"], p["quotes"], p["views"]))
                db.commit()
                time.sleep(0.3)
    except StopIteration:
        pass
    except Fatal as e:
        note = str(e)
        db.execute("INSERT INTO runs(started,finished,provider,queries,posts_seen,new_posts,note) VALUES(?,?,?,?,?,?,?)",
                   (started, datetime.now(UTC).isoformat(), provider.name, queries, seen, new, note))
        db.commit()
        raise
    db.execute("INSERT INTO runs(started,finished,provider,queries,posts_seen,new_posts,note) VALUES(?,?,?,?,?,?,?)",
               (started, datetime.now(UTC).isoformat(), provider.name, queries, seen, new, note))
    db.commit()
    return queries, seen, new, note


# ---------------------------------------------------------------- rank
def pct(values, higher_better=True):
    """Percentile (0..1) of each value within the list; ties share the average."""
    n = len(values)
    out = []
    for v in values:
        less = sum(1 for x in values if x < v)
        eq = sum(1 for x in values if x == v)
        p = (less + 0.5 * eq) / n
        out.append(p if higher_better else 1 - p)
    return out


def engagement(row):
    if row is None or row["likes"] is None:
        return None
    return (row["likes"] or 0) + 3 * (row["replies"] or 0) + 4 * (row["reposts"] or 0) + 4 * (row["quotes"] or 0)


def rank_posts(db, hours=24):
    now = datetime.now(UTC)
    since = (now - timedelta(hours=hours)).isoformat()
    cands = db.execute("""SELECT p.*, MIN(h.rank) AS best_rank, COUNT(DISTINCT h.keyword) AS breadth,
        GROUP_CONCAT(DISTINCT h.keyword) AS keywords FROM hits h JOIN posts p ON p.post_id=h.post_id
        WHERE h.fetched_at>=? GROUP BY p.post_id""", (since,)).fetchall()
    if not cands:
        return []
    # author medians from all stored posts with engagement
    author_eng = {}
    for r in db.execute("""SELECT p.username, s.likes, s.replies, s.reposts, s.quotes FROM posts p JOIN snapshots s
        ON s.post_id=p.post_id AND s.id=(SELECT MAX(id) FROM snapshots WHERE post_id=p.post_id)"""):
        e = engagement(r)
        if e is not None:
            author_eng.setdefault(r["username"], []).append(e)
    items = []
    for c in cands:
        snaps = db.execute("SELECT * FROM snapshots WHERE post_id=? ORDER BY id DESC LIMIT 2", (c["post_id"],)).fetchall()
        latest = snaps[0] if snaps else None
        e = engagement(latest)
        posted = parse_ts(c["posted_at"])
        age_h = max((now - posted).total_seconds() / 3600, 0.5) if posted else None
        vel = None
        if e is not None:
            if len(snaps) == 2 and engagement(snaps[1]) is not None:
                dt = (parse_ts(snaps[0]["fetched_at"]) - parse_ts(snaps[1]["fetched_at"])).total_seconds() / 3600
                vel = (e - engagement(snaps[1])) / dt if dt >= 0.25 else None
            if vel is None and age_h:
                vel = e / age_h
        outp = None
        vals = author_eng.get(c["username"], [])
        if e is not None and len(vals) >= 3:
            outp = e / max(statistics.median(vals), 1)
        conv = None
        if e is not None and latest["likes"] is not None:
            conv = ((latest["replies"] or 0) + (latest["reposts"] or 0) + (latest["quotes"] or 0)) / ((latest["likes"] or 0) + 1)
        items.append({"c": c, "latest": latest, "e": e, "age_h": age_h, "vel": vel, "outp": outp, "conv": conv})

    comps = {"rank": pct([-i["c"]["best_rank"] for i in items]),
             "breadth": pct([i["c"]["breadth"] for i in items]),
             "recency": pct([-(i["age_h"] if i["age_h"] is not None else 9999) for i in items])}
    for key, attr in (("velocity", "vel"), ("outperform", "outp"), ("conversation", "conv")):
        have = [(idx, i[attr]) for idx, i in enumerate(items) if i[attr] is not None]
        if have:
            ps = pct([v for _, v in have])
            col = [None] * len(items)
            for (idx, _), p in zip(have, ps):
                col[idx] = p
            comps[key] = col
    results = []
    for idx, i in enumerate(items):
        num = den = 0.0
        for key, col in comps.items():
            v = col[idx]
            if v is not None:
                num += WEIGHTS[key] * v
                den += WEIGHTS[key]
        l = i["latest"]
        results.append({
            "score": round(100 * num / den, 1) if den else 0,
            "basis": "engagement" if i["e"] is not None else "rank-only",
            "username": i["c"]["username"], "posted_at": i["c"]["posted_at"], "text": i["c"]["text"],
            "permalink": i["c"]["permalink"], "keywords": i["c"]["keywords"], "best_rank": i["c"]["best_rank"],
            "likes": l["likes"] if l else None, "replies": l["replies"] if l else None,
            "reposts": l["reposts"] if l else None, "views": l["views"] if l else None})
    results.sort(key=lambda r: -r["score"])
    return results


# ---------------------------------------------------------------- output
def reply_link(permalink):
    """Threads web intent: opens the composer in reply mode under that post."""
    m = re.search(r"/post/([A-Za-z0-9_-]+)", permalink or "")
    return f"https://www.threads.com/intent/post?reply_post_shortcode={m.group(1)}" if m else ""


def write_sheet(rows, sheet_id):
    sess = sheets_session()
    meta = sess.get(f"{SHEETS_BASE}/{sheet_id}?fields=sheets.properties.title")
    meta.raise_for_status()
    if "research" not in [s["properties"]["title"] for s in meta.json().get("sheets", [])]:
        r = sess.post(f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
                      json={"requests": [{"addSheet": {"properties": {"title": "research"}}}]})
        r.raise_for_status()
    head = ["score", "basis", "username", "posted_at_utc", "text", "link", "keywords", "best_rank",
            "likes", "replies", "reposts", "views", "updated", "reply (opens Threads composer)"]
    stamp = datetime.now(__import__("brandkit").TZ).strftime("%Y-%m-%d %H:%M")
    body = [head] + [[r["score"], r["basis"], r["username"], (r["posted_at"] or "")[:16], r["text"][:300],
                      r["permalink"], r["keywords"], r["best_rank"],
                      "" if r["likes"] is None else r["likes"], "" if r["replies"] is None else r["replies"],
                      "" if r["reposts"] is None else r["reposts"], "" if r["views"] is None else r["views"], stamp,
                      reply_link(r["permalink"])]
                     for r in rows[:TOP_N_SHEET]]
    sess.post(f"{SHEETS_BASE}/{sheet_id}/values/research!A:Z:clear").raise_for_status()
    sess.put(f"{SHEETS_BASE}/{sheet_id}/values/research!A1?valueInputOption=RAW", json={"values": body}).raise_for_status()


def print_top(rows, n):
    for r in rows[:n]:
        print(f"{r['score']:5.1f} [{r['basis']}] @{r['username']} rank#{r['best_rank']} "
              f"L{r['likes']} R{r['replies']} P{r['reposts']} | {r['text'][:70]!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "top"])
    ap.add_argument("--provider", default="meta", choices=["meta", "mock"])
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--max-queries", type=int, default=45)
    ap.add_argument("--recent", action="store_true", help="also run RECENT searches (doubles queries)")
    ap.add_argument("--no-sheet", action="store_true")
    ap.add_argument("--reprobe", action="store_true", help="re-test whether the API returns engagement fields")
    ap.add_argument("--n", type=int, default=20)
    a = ap.parse_args()
    db = open_db(a.db)
    if a.cmd == "top":
        print_top(rank_posts(db), a.n)
        return
    env = load_env()
    token = env.get("THREADS_ACCESS_TOKEN", "")
    provider = MockProvider() if a.provider == "mock" else MetaProvider(token, db, a.reprobe)
    try:
        own = own_username(provider, token)
        q, seen, new, note = collect(db, provider, own, a.max_queries, a.recent)
    except Fatal as e:
        sys.exit(f"STOPPED: {e}")
    rows = rank_posts(db)
    basis = "engagement numbers" if any(r["basis"] == "engagement" for r in rows) else "search rank only (no engagement fields returned)"
    print(f"{provider.name}: {q} queries, {seen} posts seen, {new} new. Ranking basis: {basis}. {note}".strip())
    print_top(rows, 5)
    sid = env.get("GOOGLE_SHEET_ID")
    if sid and not a.no_sheet and rows:
        write_sheet(rows, sid)
        print(f"Wrote top {min(len(rows), TOP_N_SHEET)} to the 'research' tab.")


if __name__ == "__main__":
    main()
