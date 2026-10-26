"""The brain: one ledger of every post, metric, follower count, research item and piece of feedback across
Threads and X, plus labels, follower attribution and the Brain Score.

  python brain.py sync     [--db PATH] [--threads-db PATH] [--x-db PATH] [--no-collect] [--no-sheet]
  python brain.py score                 print the latest scores and summaries

sync: ingest the Threads and X databases, read Threads metrics when a post crosses a checkpoint (1h, 6h,
24h, 72h, 7d), record follower counts (Threads every run, X every 3 h), pull your unticks and notes,
label new posts with Claude, attribute follower gains to posts, score everything, refresh the brain_scores tab.

Brain Score (100 = a typical post on that platform at that age):
  points  = 25 x follows + 5 x reposts + 5 x quotes + 3 x replies + 3 x shares/bookmarks + 1 x likes   (brain.json)
  quality = points per 1,000 views (views smoothed so tiny posts cannot spike)
  reach   = views at that age against the typical post at the same age
  score   = 100 x (quality / typical quality)^0.6 x (reach)^0.4
Follows are attributed: each follower gain between two readings is split across the posts that were
collecting views in that window, in proportion to the views each was likely getting.
"""
import argparse
import json
import math
import re
import sqlite3
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
TH = HERE.parent / "threads_agent"
XA = HERE.parent / "x_agent"
sys.path.insert(0, str(TH))
sys.path.insert(0, str(XA))
from insights import SGT, UTC, clean, parse_ts, write_tab  # noqa: E402
from threads_common import SHEETS_BASE, load_env, sheets_session, threads_call  # noqa: E402

CFG = json.loads((HERE / "brain.json").read_text(encoding="utf-8"))
import brandkit  # noqa: E402
CFG["pillars"] = list(brandkit.PILLARS)
CFG["topic_groups"] = brandkit.B.get("topic_groups", CFG.get("topic_groups", []))
CHECKPOINTS = [("1h", 1), ("6h", 6), ("24h", 24), ("72h", 72), ("7d", 168)]
METRICS = ["views", "likes", "replies", "reposts", "quotes", "shares", "bookmarks"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS posts(platform TEXT, post_id TEXT, text TEXT, posted_at TEXT, source TEXT, origin_id TEXT,
  queue_id TEXT, permalink TEXT, PRIMARY KEY(platform, post_id));
CREATE TABLE IF NOT EXISTS obs(platform TEXT, post_id TEXT, at TEXT, age_h REAL, views INTEGER, likes INTEGER, replies INTEGER,
  reposts INTEGER, quotes INTEGER, shares INTEGER, bookmarks INTEGER, src TEXT, PRIMARY KEY(platform, post_id, at));
CREATE TABLE IF NOT EXISTS followers(platform TEXT, at TEXT, followers INTEGER, src TEXT, PRIMARY KEY(platform, at));
CREATE TABLE IF NOT EXISTS labels(platform TEXT, post_id TEXT, pillar TEXT, topic_group TEXT, topic TEXT, angle TEXT, proof TEXT,
  length TEXT, number_open TEXT, ends_question TEXT, list_format TEXT, first_person TEXT, labeled_at TEXT, PRIMARY KEY(platform, post_id));
CREATE TABLE IF NOT EXISTS attribution(platform TEXT, post_id TEXT, follows REAL, follows_24h REAL, PRIMARY KEY(platform, post_id));
CREATE TABLE IF NOT EXISTS scores(platform TEXT, post_id TEXT, checkpoint TEXT, estimated INTEGER, views REAL, likes REAL, replies REAL,
  reposts REAL, quotes REAL, shares REAL, bookmarks REAL, follows REAL, points REAL, quality REAL, reach REAL, score REAL,
  computed_at TEXT, PRIMARY KEY(platform, post_id, checkpoint));
CREATE TABLE IF NOT EXISTS research(platform TEXT, ext_id TEXT, author TEXT, author_followers INTEGER, text TEXT, created_at TEXT,
  views INTEGER, likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER, bookmarks INTEGER, query TEXT, collected_at TEXT,
  PRIMARY KEY(platform, ext_id));
CREATE TABLE IF NOT EXISTS research_themes(day TEXT, platform TEXT, text TEXT, PRIMARY KEY(day, platform, text));
CREATE TABLE IF NOT EXISTS feedback(platform TEXT, queue_id TEXT, kind TEXT, text TEXT, note TEXT, at TEXT, PRIMARY KEY(platform, queue_id, kind));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def open_db(path, schema=True):
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    if schema:
        db.executescript(SCHEMA)
    return db


def age_h(posted_at, at):
    return max((parse_ts(at) - parse_ts(posted_at)).total_seconds() / 3600, 0.01)


def curve(a):
    """Share of 7-day views a post has typically collected by age a (hours). Used only to estimate gaps."""
    return min(1.0, math.log1p(max(a, 0)) / math.log1p(168))


def table_exists(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


# ---------------------------------------------------------------- ingest: Threads
def ingest_threads(db, tdb):
    engine = {r["post_id"]: r["queue_id"] for r in tdb.execute("SELECT post_id, queue_id FROM experiments WHERE post_id IS NOT NULL")} \
        if table_exists(tdb, "experiments") else {}
    n = 0
    for p in tdb.execute("SELECT * FROM my_posts WHERE media_type!='REPOST_FACADE'"):
        db.execute("""INSERT INTO posts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(platform, post_id) DO UPDATE SET text=excluded.text,
                      source=excluded.source, queue_id=excluded.queue_id, permalink=excluded.permalink""",
                   ("threads", p["post_id"], p["text"] or "", p["posted_at"], "engine" if p["post_id"] in engine else "manual", None,
                    engine.get(p["post_id"]), p["permalink"]))
        n += 1
    posted = {r["post_id"]: r["posted_at"] for r in db.execute("SELECT post_id, posted_at FROM posts WHERE platform='threads'")}
    for s in tdb.execute("SELECT * FROM my_snapshots"):
        if s["post_id"] in posted:
            db.execute("INSERT OR IGNORE INTO obs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("threads", s["post_id"], s["fetched_at"], age_h(posted[s["post_id"]], s["fetched_at"]), s["views"], s["likes"],
                        s["replies"], s["reposts"], s["quotes"], s["shares"], None, "insights"))
    for f in tdb.execute("SELECT * FROM followers"):
        db.execute("INSERT OR IGNORE INTO followers VALUES(?,?,?,?)", ("threads", f["fetched_at"], f["followers"], "insights"))
    if table_exists(tdb, "rejections"):
        for r in tdb.execute("SELECT * FROM rejections"):
            db.execute("INSERT OR IGNORE INTO feedback VALUES(?,?,?,?,?,?)", ("threads", r["queue_id"], "unticked", r["text"], None, r["at"]))
    db.commit()
    return n


def collect_threads(db, token):
    """Read metrics for posts that crossed a checkpoint since their last reading, plus the follower count."""
    now = datetime.now(UTC)
    fc = threads_call("GET", "me/threads_insights", {"metric": "followers_count", "access_token": token})
    db.execute("INSERT OR IGNORE INTO followers VALUES(?,?,?,?)", ("threads", now.isoformat(), fc["data"][0]["total_value"]["value"], "brain"))
    n = 0
    for p in db.execute("SELECT post_id, posted_at FROM posts WHERE platform='threads' AND posted_at>=?",
                        ((now - timedelta(days=8)).isoformat(),)).fetchall():
        age = age_h(p["posted_at"], now.isoformat())
        last = db.execute("SELECT MAX(age_h) a FROM obs WHERE platform='threads' AND post_id=?", (p["post_id"],)).fetchone()["a"] or 0
        if not any(last < h <= age for _, h in CHECKPOINTS):
            continue
        try:
            d = threads_call("GET", f"{p['post_id']}/insights", {"metric": "views,likes,replies,reposts,quotes,shares", "access_token": token})
        except RuntimeError as e:
            print(f"Threads insights failed for {p['post_id']}: {str(e)[:120]}")
            continue
        v = {m["name"]: m["values"][0]["value"] for m in d.get("data", [])}
        db.execute("INSERT OR IGNORE INTO obs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("threads", p["post_id"], now.isoformat(), age, v.get("views"), v.get("likes"), v.get("replies"), v.get("reposts"),
                    v.get("quotes"), v.get("shares"), None, "brain"))
        n += 1
    db.commit()
    return n


# ---------------------------------------------------------------- ingest: X
def ingest_x(db, xdb):
    n = 0
    for p in xdb.execute("SELECT * FROM x_posts WHERE status='posted' AND x_id IS NOT NULL"):
        db.execute("""INSERT INTO posts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(platform, post_id) DO UPDATE SET text=excluded.text""",
                   ("x", p["x_id"], p["text"], p["posted_at"], "threads_adapt", p["source_id"], p["qid"], f"https://x.com/i/web/status/{p['x_id']}"))
        n += 1
    for s in xdb.execute("SELECT * FROM x_snapshots"):
        db.execute("INSERT OR IGNORE INTO obs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("x", s["x_id"], s["at"], s["age_h"], s["impressions"], s["likes"], s["replies"], s["reposts"], s["quotes"], None,
                    s["bookmarks"], "x_review"))
    for f in xdb.execute("SELECT * FROM x_followers"):
        db.execute("INSERT OR IGNORE INTO followers VALUES(?,?,?,?)", ("x", f["at"], f["followers"], "x_review"))
    for r in xdb.execute("SELECT * FROM x_research"):
        db.execute("INSERT OR REPLACE INTO research VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("x", r["tweet_id"], r["author"], r["author_followers"], r["text"], r["created_at"], r["impressions"], r["likes"],
                    r["replies"], r["reposts"], r["quotes"], r["bookmarks"], r["query"], r["collected_at"]))
    th = xdb.execute("SELECT v FROM x_meta WHERE k='themes'").fetchone()
    for t in json.loads(th["v"]) if th else []:
        db.execute("INSERT OR IGNORE INTO research_themes VALUES(?,?,?)", (datetime.now(SGT).date().isoformat(), "x", t))
    for r in xdb.execute("SELECT * FROM x_rejections"):
        db.execute("INSERT OR IGNORE INTO feedback VALUES(?,?,?,?,?,?)", ("x", r["qid"], "unticked", r["text"], None, r["at"]))
    db.commit()
    return n


def collect_x_followers(db, env):
    last = db.execute("SELECT MAX(at) a FROM followers WHERE platform='x'").fetchone()["a"]
    if last and (datetime.now(UTC) - parse_ts(last)).total_seconds() < CFG["x_follower_every_hours"] * 3600:
        return
    import xclient
    x = xclient.X(env)
    if not x.can_post:
        return
    try:
        me = x.me()
    except xclient.XError as e:
        print("X follower read failed:", e)
        return
    db.execute("INSERT OR IGNORE INTO followers VALUES(?,?,?,?)", ("x", datetime.now(UTC).isoformat(), me["public_metrics"]["followers_count"], "brain"))
    db.commit()


# ---------------------------------------------------------------- feedback from the sheet (note column)
def ingest_notes(db, sess, sheet):
    for tab, plat, idcol in (("queue", "threads", "id"), ("x_queue", "x", "id")):
        r = sess.get(f"{SHEETS_BASE}/{sheet}/values/{tab}!A1:Z3000")
        if r.status_code != 200:
            continue
        vals = r.json().get("values", [])
        if not vals:
            continue
        head = [h.strip().lower() for h in vals[0]]
        if "note" not in head:
            continue
        for row in vals[1:]:
            d = dict(zip(head, row + [""] * (len(head) - len(row))))
            if d.get("note", "").strip() and d.get(idcol, "").strip():
                db.execute("""INSERT INTO feedback VALUES(?,?,?,?,?,?) ON CONFLICT(platform, queue_id, kind) DO UPDATE SET note=excluded.note, text=excluded.text""",
                           (plat, d[idcol].strip(), "note", d.get("text", ""), d["note"].strip(), datetime.now(UTC).isoformat()))
    db.commit()


# ---------------------------------------------------------------- labels
LABEL_SYSTEM = (f"Label social posts for a personal brand about {brandkit.TOPICS}. ".replace("{", "{{").replace("}", "}}") +
                "For each post return pillar (one of: {pillars}), topic_group (one of: {groups}), topic (2 to 4 words), angle (one of: "
                "{angles}) and proof (one of: {proofs}; personal_story = the author's own experience, real_numbers = specific figures, "
                + brandkit.B.get("proof_note", "named_assets = named products or assets").replace("{", "{{").replace("}", "}}") + "). Reply with only a JSON list "
                "[{{\"id\": \"...\", \"pillar\": \"...\", \"topic_group\": \"...\", \"topic\": \"...\", \"angle\": \"...\", \"proof\": \"...\"}}].")


def claude(system, user, key, max_tokens=8000):
    body = json.dumps({"model": CFG["label_model"], "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API HTTP {e.code}: {e.read().decode()[:200]}") from None
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def rule_labels(t):
    n = len(t)
    return {"length": "short" if n < 140 else "medium" if n <= 300 else "long",
            "number_open": "yes" if re.search(r"\d", t[:80]) else "no",
            "ends_question": "yes" if t.strip().endswith("?") else "no",
            "list_format": "yes" if re.search(r"(^|\n)\s*(\d+[\.\)]|[-•])\s", t) else "no",
            "first_person": "yes" if re.search(r"\b(I|I'm|I've|my|me)\b", t) else "no"}


def label(db, key):
    todo = db.execute("""SELECT platform, post_id, text FROM posts p WHERE NOT EXISTS
        (SELECT 1 FROM labels l WHERE l.platform=p.platform AND l.post_id=p.post_id) AND text!='' ORDER BY posted_at DESC LIMIT 120""").fetchall()
    if not todo or not key:
        return 0
    sysmsg = LABEL_SYSTEM.format(pillars=", ".join(CFG["pillars"]), groups=", ".join(CFG["topic_groups"]),
                                 angles=", ".join(CFG["angles"]), proofs=", ".join(CFG["proofs"]))
    n = 0
    for i in range(0, len(todo), 30):
        chunk = todo[i:i + 30]
        ids = {f"{r['platform']}:{r['post_id']}": r for r in chunk}
        try:
            text = claude(sysmsg, json.dumps([{"id": k, "text": r["text"][:500]} for k, r in ids.items()]), key)
            out = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
        except Exception as e:
            print("Labelling failed:", e)
            continue
        for o in out:
            r = ids.get(o.get("id"))
            if not r:
                continue
            pick = lambda v, allowed: v if v in allowed else allowed[-1]
            rl = rule_labels(r["text"])
            db.execute("INSERT OR REPLACE INTO labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (r["platform"], r["post_id"], pick(o.get("pillar"), CFG["pillars"]), pick(o.get("topic_group"), CFG["topic_groups"]),
                        (o.get("topic") or "")[:40], pick(o.get("angle"), CFG["angles"]), pick(o.get("proof"), CFG["proofs"]),
                        rl["length"], rl["number_open"], rl["ends_question"], rl["list_format"], rl["first_person"], datetime.now(UTC).isoformat()))
            n += 1
        db.commit()
    return n


# ---------------------------------------------------------------- checkpoint values
def obs_for(db, plat, pid):
    return [dict(r) for r in db.execute("SELECT * FROM obs WHERE platform=? AND post_id=? AND views IS NOT NULL ORDER BY age_h", (plat, pid))]


def value_at(obs, a, metric):
    """Value of a metric at age a hours, interpolated in log time. Returns (value, estimated) or (None, None)."""
    pts = [(o["age_h"], o[metric]) for o in obs if o[metric] is not None]
    if not pts:
        return None, None
    for x, v in pts:
        if abs(x - a) <= max(0.5, a * 0.1):
            return float(v), False
    lo = [(x, v) for x, v in pts if x < a]
    hi = [(x, v) for x, v in pts if x > a]
    if lo and hi:
        (x0, v0), (x1, v1) = lo[-1], hi[0]
        f = (math.log1p(a) - math.log1p(x0)) / (math.log1p(x1) - math.log1p(x0))
        return v0 + f * (v1 - v0), (x1 - x0) > max(3, a * 0.5)
    if hi:  # first reading came after this age: scale back along the typical curve
        x1, v1 = hi[0]
        return v1 * curve(a) / curve(x1), True
    return None, None  # post not old enough, or no reading past this age


# ---------------------------------------------------------------- follower attribution
def attribute(db):
    now = datetime.now(UTC)
    for plat in ("threads", "x"):
        fol = [dict(r) for r in db.execute("SELECT at, followers FROM followers WHERE platform=? ORDER BY at", (plat,))]
        posts = []
        for p in db.execute("SELECT post_id, posted_at FROM posts WHERE platform=?", (plat,)):
            obs = obs_for(db, plat, p["post_id"])
            if not obs:
                continue
            last = obs[-1]
            total7 = (last["views"] or 0) / max(curve(last["age_h"]), 0.05)  # estimated 7-day views
            posts.append({"id": p["post_id"], "t0": parse_ts(p["posted_at"]), "v7": total7})
        got = {p["id"]: 0.0 for p in posts}
        got24 = {p["id"]: 0.0 for p in posts}
        for f0, f1 in zip(fol, fol[1:]):
            gain = f1["followers"] - f0["followers"]
            if gain == 0:
                continue
            t0, t1 = parse_ts(f0["at"]), parse_ts(f1["at"])
            shares = {}
            for p in posts:
                a0 = (t0 - p["t0"]).total_seconds() / 3600
                a1 = (t1 - p["t0"]).total_seconds() / 3600
                if a1 <= 0:
                    continue
                v = p["v7"] * (curve(a1) - curve(max(a0, 0)))
                if v > 0:
                    shares[p["id"]] = (v, a0, a1)
            tot = sum(v for v, _, _ in shares.values())
            if tot <= 0:
                continue
            for pid, (v, a0, a1) in shares.items():
                g = gain * v / tot
                got[pid] += g
                if a0 < 24:  # part of this window fell inside the post's first 24 hours
                    inside = (curve(min(a1, 24)) - curve(max(a0, 0))) / max(curve(a1) - curve(max(a0, 0)), 1e-9)
                    got24[pid] += g * inside
        for pid in got:
            db.execute("INSERT OR REPLACE INTO attribution VALUES(?,?,?,?)", (plat, pid, round(got[pid], 3), round(got24[pid], 3)))
    db.commit()


# ---------------------------------------------------------------- Brain Score
def points(m, follows):
    w = CFG["weights"]
    return (w["follow"] * max(follows, 0) + w["repost"] * (m["reposts"] or 0) + w["quote"] * (m["quotes"] or 0) + w["reply"] * (m["replies"] or 0)
            + w["share"] * (m["shares"] or 0) + w["bookmark"] * (m["bookmarks"] or 0) + w["like"] * (m["likes"] or 0))


def score_all(db):
    now = datetime.now(UTC)
    att = {(r["platform"], r["post_id"]): r for r in db.execute("SELECT * FROM attribution")}
    rows = {}
    for p in db.execute("SELECT platform, post_id, posted_at FROM posts"):
        obs = obs_for(db, p["platform"], p["post_id"])
        if not obs:
            continue
        age = age_h(p["posted_at"], now.isoformat())
        a = att.get((p["platform"], p["post_id"]))
        for cp, h in CHECKPOINTS:
            if age < h:
                break
            m, est = {}, False
            for k in METRICS:
                v, e = value_at(obs, h, k)
                m[k] = v
                est = est or bool(e)
            if m["views"] is None:
                continue
            fol = 0.0
            if a:
                fol = a["follows_24h"] if h <= 24 else a["follows"]
                if h < 24 and a["follows_24h"]:
                    fol = a["follows_24h"] * curve(h) / curve(24)
            pts = points(m, fol)
            rows[(p["platform"], p["post_id"], cp)] = {"m": m, "est": est, "fol": fol, "pts": pts,
                                                       "q": 1000 * pts / (m["views"] + CFG["view_smoothing"]), "posted": p["posted_at"]}
    since = (now - timedelta(days=CFG["baseline_days"])).isoformat()
    base = {}
    for (plat, pid, cp), r in rows.items():
        if r["posted"] >= since:
            base.setdefault((plat, cp), []).append(r)
    stamp = now.isoformat()
    db.execute("DELETE FROM scores")
    for (plat, pid, cp), r in rows.items():
        b = base.get((plat, cp), [])
        if len(b) < CFG["min_baseline_posts"]:
            reach = quality_rel = None
        else:
            gv = math.exp(statistics.mean(math.log(max(x["m"]["views"], 1)) for x in b))
            gq = math.exp(statistics.mean(math.log(x["q"] + 0.5) for x in b))
            reach = max(r["m"]["views"], 1) / gv
            quality_rel = (r["q"] + 0.5) / gq
        score = round(100 * quality_rel ** CFG["quality_mix"] * reach ** CFG["reach_mix"]) if reach else None
        m = r["m"]
        db.execute("INSERT INTO scores VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (plat, pid, cp, int(r["est"]), m["views"], m["likes"], m["replies"], m["reposts"], m["quotes"], m["shares"], m["bookmarks"],
                    round(r["fol"], 2), round(r["pts"], 1), round(r["q"], 1), round(reach, 2) if reach else None, score, stamp))
    db.commit()


# ---------------------------------------------------------------- outputs
def best_checkpoint(db, plat, pid):
    r = db.execute("""SELECT * FROM scores WHERE platform=? AND post_id=? AND checkpoint IN ('24h','6h')
        ORDER BY CASE checkpoint WHEN '24h' THEN 0 ELSE 1 END LIMIT 1""", (plat, pid)).fetchone()
    return dict(r) if r else None


def summary(db, days=7):
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    out = {}
    for plat in ("threads", "x"):
        posts = [dict(p) for p in db.execute("SELECT * FROM posts WHERE platform=? AND posted_at>=?", (plat, since))]
        sc = [s for s in (best_checkpoint(db, plat, p["post_id"]) for p in posts) if s]
        fol = [dict(r) for r in db.execute("SELECT at, followers FROM followers WHERE platform=? ORDER BY at", (plat,))]
        recent = [f for f in fol if f["at"] >= since]
        start = recent[0]["followers"] if recent else None
        end = fol[-1]["followers"] if fol else None
        views = sum(s["views"] or 0 for s in sc)
        net = (end - start) if start is not None and end is not None else None
        out[plat] = {"posts": len(posts), "scored": len(sc), "views_at_24h": round(views), "followers_now": end, "net_followers": net,
                     "follows_per_1k_views": round(1000 * net / views, 2) if net is not None and views else None,
                     "avg_score": round(statistics.median(s["score"] for s in sc if s["score"] is not None)) if any(s["score"] for s in sc) else None}
    return out


def group_scores(db, field, plat="threads", days=30):
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    g = {}
    for r in db.execute(f"""SELECT l.{field} k, s.score FROM labels l JOIN posts p ON p.platform=l.platform AND p.post_id=l.post_id
        JOIN scores s ON s.platform=l.platform AND s.post_id=l.post_id AND s.checkpoint='24h'
        WHERE l.platform=? AND p.posted_at>=? AND s.score IS NOT NULL""", (plat, since)):
        g.setdefault(r["k"], []).append(r["score"])
    return sorted(([k, len(v), round(math.exp(statistics.mean(math.log(max(x, 1)) for x in v)))] for k, v in g.items()), key=lambda x: -x[2])


def write_sheet(db, sess, sheet):
    s = summary(db)
    w = CFG["weights"]
    rows = [[f"BRAIN SCORES  updated {datetime.now(SGT):%Y-%m-%d %H:%M} SGT.  100 = a typical post at the same age.  Points: follow {w['follow']}, "
             f"repost/quote {w['repost']}, reply {w['reply']}, share/bookmark {w['share']}, like {w['like']}."],
            ["LAST 7 DAYS", "posts", "scored", "views (at 24h)", "followers now", "net followers", "follows per 1k views", "median score"]]
    for plat, v in s.items():
        rows.append([plat, v["posts"], v["scored"], v["views_at_24h"], v["followers_now"], v["net_followers"], v["follows_per_1k_views"], v["avg_score"]])
    rows.append([""])
    rows.append(["THREADS, LAST 30 DAYS: typical score by label (n = posts)"])
    blocks = [(f, group_scores(db, f)) for f in ("proof", "angle", "topic_group", "pillar", "length", "number_open", "ends_question")]
    for f, gs in blocks:
        rows.append([f] + [f"{k}: {sc} (n={n})" for k, n, sc in gs])
    rows.append([""])
    rows.append(["platform", "posted_sgt", "source", "score", "checkpoint", "views", "follows (est.)", "points", "quality /1k", "reach",
                 "likes", "replies", "reposts+quotes", "shares/bookmarks", "pillar", "topic", "angle", "proof", "text", "link"])
    since = (datetime.now(UTC) - timedelta(days=14)).isoformat()
    lines = []
    for p in db.execute("""SELECT p.*, l.pillar, l.topic, l.angle, l.proof FROM posts p LEFT JOIN labels l ON l.platform=p.platform AND l.post_id=p.post_id
                           WHERE p.posted_at>=?""", (since,)):
        sc = best_checkpoint(db, p["platform"], p["post_id"])
        if not sc:
            continue
        lines.append([p["platform"], parse_ts(p["posted_at"]).astimezone(SGT).strftime("%Y-%m-%d %H:%M"), p["source"],
                      sc["score"] if sc["score"] is not None else "", sc["checkpoint"] + (" est." if sc["estimated"] else ""), round(sc["views"]),
                      sc["follows"], sc["points"], sc["quality"], sc["reach"] or "", round(sc["likes"] or 0), round(sc["replies"] or 0),
                      round((sc["reposts"] or 0) + (sc["quotes"] or 0)), round((sc["shares"] or 0) + (sc["bookmarks"] or 0)),
                      p["pillar"] or "", p["topic"] or "", p["angle"] or "", p["proof"] or "", (p["text"] or "")[:140], p["permalink"] or ""])
    lines.sort(key=lambda r: -(r[3] if isinstance(r[3], (int, float)) else -1))
    write_tab(sess, sheet, "brain_scores", rows + lines)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sync", "score"])
    ap.add_argument("--db", default=str(HERE / "brain.db"))
    ap.add_argument("--threads-db", default=str(TH / "insights.db"))
    ap.add_argument("--x-db", default=str(XA / "x.db"))
    ap.add_argument("--no-collect", action="store_true", help="ingest only, no API reads")
    ap.add_argument("--no-sheet", action="store_true")
    a = ap.parse_args()
    env = load_env()
    db = open_db(a.db)
    if a.cmd == "sync":
        tdb = open_db(a.threads_db, schema=False)
        threads_on = __import__("brandkit").module("threads")
        nt = ingest_threads(db, tdb) if threads_on else 0
        nx = ingest_x(db, open_db(a.x_db, schema=False)) if Path(a.x_db).exists() else 0
        nc = 0
        if not a.no_collect:
            nc = collect_threads(db, env["THREADS_ACCESS_TOKEN"]) if threads_on else 0
            collect_x_followers(db, env)
        sess = None if a.no_sheet else sheets_session()
        if sess:
            ingest_notes(db, sess, env["GOOGLE_SHEET_ID"])
        nl = label(db, env.get("ANTHROPIC_API_KEY"))
        attribute(db)
        score_all(db)
        if sess:
            write_sheet(db, sess, env["GOOGLE_SHEET_ID"])
        print(f"Brain sync: {nt} Threads posts, {nx} X posts, {nc} checkpoint reads, {nl} newly labelled.")
        try:
            import supa_sync
            print(supa_sync.push(db, env))
        except Exception as e:  # the mirror must never break the local brain
            print(f"Supabase mirror failed, will retry next sync: {e}")
    s = summary(db)
    for plat, v in s.items():
        print(f"  {plat}: {v}")
    for f in ("proof", "angle", "topic_group"):
        print(f"  {f}: " + ", ".join(f"{k} {sc} (n={n})" for k, n, sc in group_scores(db, f)))


if __name__ == "__main__":
    main()
