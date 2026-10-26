"""Threads insights: pull your own post metrics and follower count, and review them weekly.

  python insights.py pull   [--db PATH] [--days 30] [--no-sheet]
  python insights.py review [--db PATH] [--days 7] [--ai] [--no-sheet]

pull:   snapshots views/likes/replies/reposts/quotes/shares for your recent posts and your
        follower count into SQLite, and refreshes the 'insights' and 'followers' sheet tabs.
review: builds the weekly review (followers vs goal path, what worked, what flopped, by tag,
        by length), saves it to reviews/ and the 'review' tab. --ai adds Claude's commentary.
"""
import argparse
import json
import re
import sqlite3
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from threads_common import HERE, SHEETS_BASE, load_env, sheets_session, threads_call
import brandkit

UTC = timezone.utc
SGT = brandkit.TZ  # brand timezone (name kept for compatibility)
DB_DEFAULT = HERE / "insights.db"
POST_FIELDS = "id,text,timestamp,permalink,media_type,topic_tag,is_quote_post"
SCHEMA = """
CREATE TABLE IF NOT EXISTS my_posts(post_id TEXT PRIMARY KEY, text TEXT, posted_at TEXT, permalink TEXT,
  media_type TEXT, topic_tag TEXT, is_quote INTEGER);
CREATE TABLE IF NOT EXISTS my_snapshots(id INTEGER PRIMARY KEY, post_id TEXT, fetched_at TEXT,
  views INTEGER, likes INTEGER, replies INTEGER, reposts INTEGER, quotes INTEGER, shares INTEGER);
CREATE TABLE IF NOT EXISTS followers(day TEXT PRIMARY KEY, followers INTEGER, fetched_at TEXT);
CREATE INDEX IF NOT EXISTS idx_my_snap ON my_snapshots(post_id, id);
"""


def open_db(path):
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def parse_ts(s):
    return datetime.fromisoformat(s.replace("+0000", "+00:00")).astimezone(UTC) if s else None


def clean(text):
    """House style: no long dashes."""
    return re.sub(r"\s*[—–]\s*", ", ", text)


# ---------------------------------------------------------------- goal
def load_goal():
    g = dict(brandkit.GOAL)
    g["start"] = datetime.strptime(g["start_date"], "%Y-%m-%d").date()
    return g


def baseline(db, goal):
    r = db.execute("SELECT followers FROM followers WHERE day>=? ORDER BY day LIMIT 1", (goal["start_date"],)).fetchone()
    r = r or db.execute("SELECT followers FROM followers ORDER BY day LIMIT 1").fetchone()
    return r["followers"] if r else None


def expected(goal, base, day):
    """Linear path from the baseline at the start date through the milestones (weeks)."""
    w = (day - goal["start"]).days / 7
    pts = [(0, base)] + [(m["week"], m["followers"]) for m in goal["milestones"]]
    if w <= 0:
        return base
    for (w0, f0), (w1, f1) in zip(pts, pts[1:]):
        if w <= w1:
            return f0 + (f1 - f0) * (w - w0) / (w1 - w0)
    return pts[-1][1]


# ---------------------------------------------------------------- pull
def pull(db, token, days):
    now = datetime.now(UTC)
    fc = threads_call("GET", "me/threads_insights", {"metric": "followers_count", "access_token": token})
    followers = fc["data"][0]["total_value"]["value"]
    day = now.astimezone(SGT).strftime("%Y-%m-%d")
    db.execute("INSERT INTO followers(day,followers,fetched_at) VALUES(?,?,?) "
               "ON CONFLICT(day) DO UPDATE SET followers=excluded.followers, fetched_at=excluded.fetched_at",
               (day, followers, now.isoformat()))
    params = {"fields": POST_FIELDS, "limit": 50, "since": int((now - timedelta(days=days)).timestamp()), "access_token": token}
    posts, after = [], None
    while len(posts) < 300:
        if after:
            params["after"] = after
        page = threads_call("GET", "me/threads", params)
        posts += page.get("data", [])
        after = page.get("paging", {}).get("cursors", {}).get("after")
        if not page.get("paging", {}).get("next"):
            break
    ok = errors = 0
    metrics = "views,likes,replies,reposts,quotes,shares"
    for p in posts:
        if p.get("media_type") == "REPOST_FACADE":
            continue
        db.execute("INSERT INTO my_posts(post_id,text,posted_at,permalink,media_type,topic_tag,is_quote) VALUES(?,?,?,?,?,?,?) "
                   "ON CONFLICT(post_id) DO UPDATE SET text=excluded.text, topic_tag=excluded.topic_tag, permalink=excluded.permalink",
                   (p["id"], p.get("text") or "", parse_ts(p.get("timestamp")).isoformat(), p.get("permalink"),
                    p.get("media_type"), p.get("topic_tag"), int(bool(p.get("is_quote_post")))))
        try:
            try:
                d = threads_call("GET", f"{p['id']}/insights", {"metric": metrics, "access_token": token})
            except RuntimeError as e:
                if "shares" not in str(e):
                    raise
                metrics = "views,likes,replies,reposts,quotes"
                d = threads_call("GET", f"{p['id']}/insights", {"metric": metrics, "access_token": token})
            v = {m["name"]: m["values"][0]["value"] for m in d.get("data", [])}
            last = db.execute("SELECT fetched_at FROM my_snapshots WHERE post_id=? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
            if not last or last["fetched_at"][:13] != now.isoformat()[:13]:
                db.execute("INSERT INTO my_snapshots(post_id,fetched_at,views,likes,replies,reposts,quotes,shares) VALUES(?,?,?,?,?,?,?,?)",
                           (p["id"], now.isoformat(), v.get("views"), v.get("likes"), v.get("replies"),
                            v.get("reposts"), v.get("quotes"), v.get("shares")))
            ok += 1
        except RuntimeError:
            errors += 1
    db.commit()
    if posts and ok == 0:
        raise RuntimeError("Could not read insights for any post. Check the token has threads_manage_insights.")
    return followers, len(posts), ok, errors


# ---------------------------------------------------------------- data views
def latest_posts(db, days):
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = db.execute("""SELECT p.*, s.views, s.likes, s.replies, s.reposts, s.quotes, s.shares
        FROM my_posts p JOIN my_snapshots s ON s.post_id=p.post_id AND s.id=(SELECT MAX(id) FROM my_snapshots WHERE post_id=p.post_id)
        WHERE p.posted_at>=? ORDER BY p.posted_at DESC""", (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        eng = sum(d[k] or 0 for k in ("likes", "replies", "reposts", "quotes"))
        d["eng"] = eng
        d["rate"] = eng / d["views"] if d["views"] else 0.0
        d["sgt"] = parse_ts(d["posted_at"]).astimezone(SGT)
        out.append(d)
    return out


def ensure_tab(sess, sheet_id, title):
    meta = sess.get(f"{SHEETS_BASE}/{sheet_id}?fields=sheets.properties.title")
    meta.raise_for_status()
    if title not in [s["properties"]["title"] for s in meta.json().get("sheets", [])]:
        sess.post(f"{SHEETS_BASE}/{sheet_id}:batchUpdate",
                  json={"requests": [{"addSheet": {"properties": {"title": title}}}]}).raise_for_status()


def write_tab(sess, sheet_id, title, values):
    ensure_tab(sess, sheet_id, title)
    sess.post(f"{SHEETS_BASE}/{sheet_id}/values/{title}!A:Z:clear").raise_for_status()
    sess.put(f"{SHEETS_BASE}/{sheet_id}/values/{title}!A1?valueInputOption=RAW", json={"values": values}).raise_for_status()


def write_sheets(db, sheet_id, days=30):
    sess = sheets_session()
    head = ["posted_sgt", "tag", "views", "likes", "replies", "reposts", "quotes", "shares", "engagement_rate", "text", "link", "post_id"]
    rows = [head] + [[p["sgt"].strftime("%Y-%m-%d %H:%M"), p["topic_tag"] or "", p["views"] or 0, p["likes"] or 0, p["replies"] or 0,
                      p["reposts"] or 0, p["quotes"] or 0, p["shares"] or 0, f"{p['rate']:.1%}", p["text"][:120],
                      p["permalink"] or "", p["post_id"]] for p in latest_posts(db, days)]
    write_tab(sess, sheet_id, "insights", rows)
    goal = load_goal()
    base = baseline(db, goal)
    frows = [["day", "followers", "change", "goal_path", "vs_path"]]
    prev = None
    for r in db.execute("SELECT day, followers FROM followers ORDER BY day"):
        d = datetime.strptime(r["day"], "%Y-%m-%d").date()
        exp = expected(goal, base, d) if base is not None else None
        frows.append([r["day"], r["followers"], "" if prev is None else r["followers"] - prev,
                      "" if exp is None else round(exp), "" if exp is None else round(r["followers"] - exp)])
        prev = r["followers"]
    write_tab(sess, sheet_id, "followers", frows)


# ---------------------------------------------------------------- review
def group_stats(posts, keyfn):
    groups = {}
    for p in posts:
        groups.setdefault(keyfn(p), []).append(p)
    out = []
    for k, ps in groups.items():
        out.append({"key": k, "n": len(ps), "avg_views": round(statistics.mean(x["views"] or 0 for x in ps)),
                    "avg_eng": round(statistics.mean(x["eng"] for x in ps), 1),
                    "avg_rate": statistics.mean(x["rate"] for x in ps)})
    return sorted(out, key=lambda g: -g["avg_views"])


def length_bucket(p):
    n = len(p["text"])
    return "short (under 140)" if n < 140 else "medium (140 to 300)" if n <= 300 else "long (over 300)"


def build_review(db, days):
    today = datetime.now(SGT).date()
    goal = load_goal()
    base = baseline(db, goal)
    lines = [f"WEEKLY REVIEW  ({today}, last {days} days)", ""]
    f_now = db.execute("SELECT day, followers FROM followers ORDER BY day DESC LIMIT 1").fetchone()
    stats = {"window_days": days}
    if f_now:
        cutoff = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        f_then = db.execute("SELECT day, followers FROM followers WHERE day<=? ORDER BY day DESC LIMIT 1", (cutoff,)).fetchone() \
            or db.execute("SELECT day, followers FROM followers ORDER BY day LIMIT 1").fetchone()
        exp = expected(goal, base, today) if base is not None else None
        gained = f_now["followers"] - f_then["followers"]
        lines.append(f"FOLLOWERS: {f_now['followers']:,} now. Change since {f_then['day']}: {gained:+,}.")
        stats.update(followers=f_now["followers"], follower_change=gained, since=f_then["day"])
        if exp is not None:
            gap = f_now["followers"] - exp
            weeks_left = max((goal["start"] + timedelta(days=91) - today).days / 7, 0.1)
            nxt = next((m for m in goal["milestones"] if (goal["start"] + timedelta(weeks=m["week"])) >= today), goal["milestones"][-1])
            need = (nxt["followers"] - f_now["followers"]) / max((goal["start"] + timedelta(weeks=nxt["week"]) - today).days / 7, 0.1)
            status = "on or ahead of path" if gap >= -0.05 * max(exp - base, 1) else "behind path"
            lines.append(f"GOAL PATH: expected about {round(exp):,} today ({round(gap):+,} vs path, {status}). "
                         f"To reach {nxt['followers']:,} by week {nxt['week']} you need about {max(need, 0):.0f} per week.")
            stats.update(path_expected=round(exp), vs_path=round(gap), status=status, needed_per_week=round(max(need, 0)),
                         next_milestone=nxt)
        lines.append("")
    posts = latest_posts(db, days)
    mature = [p for p in posts if (datetime.now(UTC) - parse_ts(p["posted_at"])).total_seconds() > 3 * 3600]
    stats["posts_in_window"] = len(posts)
    stats["posts_with_3h_data"] = len(mature)
    if not mature:
        lines.append("No posts old enough (3+ hours) in this window yet.")
        return "\n".join(lines), stats
    tv = sum(p["views"] or 0 for p in mature)
    te = sum(p["eng"] for p in mature)
    lines.append(f"POSTS: {len(mature)} analysed. Views {tv:,}, interactions {te:,} (likes, replies, reposts, quotes). "
                 f"Average {tv // len(mature):,} views per post.")
    stats.update(total_views=tv, total_interactions=te, avg_views=tv // len(mature))
    if len(mature) < 20:
        lines.append(f"Low confidence: only {len(mature)} posts, so treat patterns below as hints, not conclusions.")
    top = sorted(mature, key=lambda p: -(p["views"] or 0))[:5]
    flop = sorted(mature, key=lambda p: (p["views"] or 0))[:5]
    best_rate = sorted([p for p in mature if (p["views"] or 0) >= 50], key=lambda p: -p["rate"])[:3]
    fmt = lambda p: f"  {p['views'] or 0:>6,} views, {p['eng']} interactions, {p['rate']:.1%}  [{p['topic_tag'] or '-'}] {p['text'][:80]!r}"
    lines += ["", "TOP BY VIEWS:"] + [fmt(p) for p in top] + ["", "LOWEST BY VIEWS:"] + [fmt(p) for p in flop]
    if best_rate:
        lines += ["", "BEST ENGAGEMENT RATE (50+ views):"] + [fmt(p) for p in best_rate]
    for title, fn in (("BY TAG", lambda p: p["topic_tag"] or "(none)"), ("BY LENGTH", length_bucket),
                      ("BY FORMAT", lambda p: "question" if p["text"].strip().endswith("?") else "statement")):
        gs = group_stats(mature, fn)
        lines += ["", title + ":"] + [f"  {g['key']:<22} {g['n']:>3} posts  avg {g['avg_views']:,} views  avg {g['avg_eng']} interactions  rate {g['avg_rate']:.1%}" for g in gs]
        stats[title.lower().replace(" ", "_")] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in g.items()} for g in gs]
    stats["top_posts"] = [{"text": p["text"][:140], "tag": p["topic_tag"], "views": p["views"], "interactions": p["eng"]} for p in top]
    stats["lowest_posts"] = [{"text": p["text"][:140], "tag": p["topic_tag"], "views": p["views"], "interactions": p["eng"]} for p in flop]
    return "\n".join(lines), stats


AI_SYSTEM = (f"You are the content strategist and growth analyst for a personal brand on Threads about {brandkit.TOPICS}. "
             "Voice: direct, specific, plain-spoken. Do not flatter. "
             "Use only the numbers provided. Never invent data. If samples are small, say so and keep conclusions modest. "
             "Do not use em dashes or long dashes. Reply in under 250 words with these headings: What worked, What flopped, "
             "Make more of next week, 3 tests to run, and if the follower count is behind the goal path, the most likely cause "
             "(hooks, posting volume, consistency, niche clarity) with one fix.")


def ai_commentary(stats, key):
    body = json.dumps({"model": "claude-sonnet-5-5", "max_tokens": 4000, "system": AI_SYSTEM,
                       "messages": [{"role": "user", "content": "Weekly stats as JSON:\n" + json.dumps(stats)}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} {e.read().decode()[:200]}"
    except Exception as e:
        return None, str(e)
    text = clean("".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"))
    if data.get("stop_reason") == "max_tokens":
        text += "\n(Commentary was cut off at the length limit.)"
    return text, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pull", "review"])
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--no-sheet", action="store_true")
    a = ap.parse_args()
    env = load_env()
    db = open_db(a.db)
    sid = env.get("GOOGLE_SHEET_ID")
    if a.cmd == "pull":
        followers, n, ok, err = pull(db, env["THREADS_ACCESS_TOKEN"], a.days or 30)
        print(f"Followers: {followers:,}. Posts found: {n}. Insights read: {ok}. Errors: {err}.")
        if sid and not a.no_sheet:
            write_sheets(db, sid)
            print("Updated the 'insights' and 'followers' tabs.")
        return
    text, stats = build_review(db, a.days or 7)
    if a.ai:
        key = env.get("ANTHROPIC_API_KEY")
        if not key:
            text += "\n\n(AI commentary skipped: no ANTHROPIC_API_KEY in .env)"
        else:
            msg, err = ai_commentary(stats, key)
            text += "\n\nCLAUDE'S READ:\n" + msg if msg else f"\n\n(AI commentary skipped: {err})"
    text = clean(text)
    (HERE / "reviews").mkdir(exist_ok=True)
    out = HERE / "reviews" / f"review_{datetime.now(SGT):%Y-%m-%d}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    if sid and not a.no_sheet:
        write_tab(sheets_session(), sid, "review", [[line] for line in text.split("\n")])
        print("\nSaved to the 'review' tab and", out.name)


if __name__ == "__main__":
    main()
