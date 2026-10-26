"""Self-improving content engine (Stage 3).

  python engine.py run    [--dry-run]            link results, label hooks, learn, write the next batch
  python engine.py learn                         show what the data says so far
  python engine.py revise [--apply] [--max N]    rewrite still-queued posts that match losing patterns

Daily loop: link queued posts to their live post ids, label hook types with Claude, measure each
pattern's lift at 24h+ against your own median (shrunk toward 1 for small samples), then write the
next batch: mostly proven patterns plus deliberate one-variable experiments. New posts go to the
queue with approved = engine.json auto_approve (default false). Inputs are only your own posts
and numbers, never other people's text.
"""
import argparse
import json
import math
import re
import statistics
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta

from insights import (DB_DEFAULT, HERE, SGT, UTC, build_review, clean, latest_posts, length_bucket, open_db, parse_ts, write_tab)
import etf_facts
import brandkit
from threads_common import SHEETS_BASE, TAB, load_env, read_rows, sheets_session, update_cells

CFG = json.loads((HERE / "engine.json").read_text(encoding="utf-8"))
CFG["pillars"] = brandkit.PILLARS  # pillars and their topic tags come from brand/brand.json
if not brandkit.module("etf_facts"):
    CFG["etf_posts_per_batch"] = 0
HOOKS = ["contrarian", "question", "list", "story", "stat_hook", "how_to", "confession", "observation", "other"]
K = 4  # shrinkage strength: acts like K extra average posts
BANNED = [r"not (financial |investment )?advice", r"education,? not advice", r"for educational purposes", r"guarantee", r"get rich", r"risk.free", r"#\w", r"^\s*(picture|imagine)\b", r"\bthe other side:", r"^\s*the belief:", r"here'?s the thing",
          r"\$\s?\d[\d,]*\s*(/|per|a)\s*(day|week|month|hour)"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS post_features(post_id TEXT PRIMARY KEY, hook TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS experiments(queue_id TEXT PRIMARY KEY, batch_id TEXT, pillar TEXT, tag TEXT, hook TEXT,
  hypothesis TEXT, is_explore INTEGER, text TEXT, created_at TEXT, post_id TEXT);
CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, queue_id TEXT, old_text TEXT, new_text TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS rejections(queue_id TEXT PRIMARY KEY, text TEXT, hook TEXT, tag TEXT, pillar TEXT, at TEXT);
"""


FACTS = brandkit.B.get("author_facts", [])


def _nums(text):
    """Numbers in text, normalised: 500k, 500,000 and $500,000 all become 500000."""
    out = set()
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(k|m|million|thousand)?\b", text, re.I):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get((m.group(2) or "").lower(), 1)
        out.add(round(v * mult, 2))
    return out


FACT_NUMS = _nums(" ".join(FACTS))
FIRST_PERSON = re.compile(r"\b(I|I'm|I've|I'd|my|me)\b")


def facts_only(text):
    """A first-person sentence may only use numbers that appear in the author's facts (no invented income or results)."""
    for sent in re.split(r"(?<=[.!?\n])\s+", text):
        if FIRST_PERSON.search(sent) and not _nums(sent) <= FACT_NUMS:
            return False
    return True


# ---------------------------------------------------------------- Claude
def claude(system, user, key, max_tokens=12000):
    body = json.dumps({"model": CFG["model"], "max_tokens": max_tokens, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API HTTP {e.code}: {e.read().decode()[:200]}") from None
    if data.get("stop_reason") == "max_tokens":
        raise RuntimeError("Claude hit the token limit before finishing; lower batch_size or retry.")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def parse_json_list(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise RuntimeError("Claude did not return a JSON list.")
    return json.loads(m.group(0))


# ---------------------------------------------------------------- link and label
def link_results(db, sess, sheet):
    rows = {r["id"]: r for _, r in read_rows(sess, sheet)}
    n = 0
    for e in db.execute("SELECT queue_id FROM experiments WHERE post_id IS NULL").fetchall():
        r = rows.get(e["queue_id"])
        if r and r["status"].strip().lower() == "posted" and r["post_id"].strip():
            db.execute("UPDATE experiments SET post_id=? WHERE queue_id=?", (r["post_id"].strip(), e["queue_id"]))
            db.execute("INSERT OR REPLACE INTO post_features(post_id,hook,source) SELECT post_id, hook, 'engine' FROM experiments WHERE queue_id=?",
                       (e["queue_id"],))
            n += 1
    db.commit()
    return n


def classify(db, key):
    todo = db.execute("""SELECT post_id, text FROM my_posts WHERE post_id NOT IN (SELECT post_id FROM post_features)
                         AND media_type!='REPOST_FACADE' ORDER BY posted_at DESC LIMIT 40""").fetchall()
    if not todo:
        return 0
    sysmsg = ("Label the opening hook style of each Threads post. Allowed labels: " + ", ".join(HOOKS) +
              ". Reply with only a JSON list like [{\"id\": \"...\", \"hook\": \"...\"}].")
    out = parse_json_list(claude(sysmsg, json.dumps([{"id": t["post_id"], "text": t["text"][:400]} for t in todo]), key, 6000))
    n = 0
    for o in out:
        if o.get("hook") in HOOKS and any(o.get("id") == t["post_id"] for t in todo):
            db.execute("INSERT OR REPLACE INTO post_features(post_id,hook,source) VALUES(?,?,'claude')", (o["id"], o["hook"]))
            n += 1
    db.commit()
    return n


# ---------------------------------------------------------------- learn
def block(h):
    return "night (0 to 6)" if h < 6 else "morning (6 to 12)" if h < 12 else "afternoon (12 to 18)" if h < 18 else "evening (18 to 24)"


def feats(p, hook):
    t = p["text"]
    return {"tag": p["topic_tag"] or "(none)", "length": length_bucket(p),
            "format": "question" if t.strip().endswith("?") else "statement", "hook": hook or "unlabelled",
            "number_in_opening": "yes" if re.search(r"\d", t[:90]) else "no",
            "named_etfs": "yes" if re.search(r"\b(QQQ|VGT|SMH|SCHD|VYM|XLV|XLE|VTI|VOO|IVV|SPY|XLK|ARKK|VXUS)\b", t) else "no",
            "list_format": "yes" if re.search(r"(^|\n)\s*(\d+[\.\)]|[-•])", t) else "no",
            "time_block": block(p["sgt"].hour)}


def learn(db):
    now = datetime.now(UTC)
    posts = [p for p in latest_posts(db, 60)
             if (now - parse_ts(p["posted_at"])).total_seconds() >= CFG["min_age_hours"] * 3600 and p["media_type"] != "REPOST_FACADE"]
    info = {"mature": len(posts), "baseline": None, "features": [], "confidence": "low"}
    if len(posts) < CFG["min_mature_posts_for_learning"]:
        return info
    base = max(math.exp(statistics.mean(math.log(max(p["views"] or 0, 1)) for p in posts)), 1)  # geometric mean: shared features score exactly 1.0
    info["baseline"] = round(base)
    hooks = {r["post_id"]: r["hook"] for r in db.execute("SELECT post_id, hook FROM post_features")}
    groups = {}
    for p in posts:
        lift = max(p["views"] or 0, 1) / base
        for f, v in feats(p, hooks.get(p["post_id"])).items():
            groups.setdefault((f, v), []).append(math.log(lift))
    for (f, v), logs in sorted(groups.items()):
        n = len(logs)
        shrunk = math.exp(sum(logs) / (n + K))
        verdict = "insufficient data" if n < 3 else "WINNER" if shrunk >= 1.25 else "LOSER" if shrunk <= 0.8 else "neutral"
        info["features"].append({"feature": f, "value": v, "n": n, "lift": round(shrunk, 2), "verdict": verdict})
    info["confidence"] = "low" if len(posts) < 20 else "medium" if len(posts) < 60 else "higher"
    return info


def learn_text(info):
    if not info["features"]:
        return f"Only {info['mature']} posts are 24h+ old; need {CFG['min_mature_posts_for_learning']} before learning anything."
    lines = [f"{info['mature']} posts at 24h+, typical {info['baseline']:,} views (geometric average), confidence {info['confidence']}. Lift 1.0 = a typical post."]
    for f in sorted(info["features"], key=lambda x: (x["feature"], -x["lift"])):
        lines.append(f"  {f['feature']:<18} {f['value']:<22} n={f['n']:<3} lift {f['lift']:<5} {f['verdict']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- generate
GEN_SYSTEM = (
    f"You write Threads posts for a personal brand about {brandkit.TOPICS}. {brandkit.B['author_story']} Voice: {brandkit.B['voice']} Hard rules: "
    f"never invent personal stories, income, results, screenshots or numbers about the author; {brandkit.B['first_person_rule']}. "
    f"{brandkit.B['post_rules']} "
    + ("AUTHOR FACTS (true, the only personal material you may use; use at most one or two per post, in his own plain voice): "
       + " ".join(f"({i + 1}) {f}" for i, f in enumerate(FACTS)) + " " if FACTS else "")
    + f"Under 500 characters each. No hashtags. No em dashes or "
    "long dashes. Write original posts; do not imitate or reuse anyone else's wording. Reply with only a JSON list.")


def next_slots(sess, sheet, n):
    rows = read_rows(sess, sheet)
    mins = CFG["slot_minutes"]
    start = datetime.now(SGT).replace(second=0, microsecond=0) + timedelta(minutes=20)
    start += timedelta(minutes=(mins - start.minute % mins) % mins)
    last = None
    for _, r in rows:
        if r["status"].strip().lower() in ("scheduled", "posting") and r["publish_at"].strip():
            for f in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
                try:
                    t = datetime.strptime(r["publish_at"].strip(), f).replace(tzinfo=SGT)
                    last = t if not last or t > last else last
                    break
                except ValueError:
                    pass
    first = max(start, last + timedelta(minutes=mins)) if last else start
    return [first + timedelta(minutes=mins * i) for i in range(n)], rows


def pub_time(s):
    for f in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), f).replace(tzinfo=SGT)
        except ValueError:
            pass
    return None


def review_backlog(sess, sheet, write):
    """Expire proposals nobody approved before their slot; count the ones still waiting."""
    now, expired, pending = datetime.now(SGT), 0, 0
    for n, r in read_rows(sess, sheet):
        if r["status"].strip().lower() != "scheduled" or r["approved"].strip().upper() == "TRUE":
            continue
        t = pub_time(r["publish_at"])
        if t and t < now:
            expired += 1
            if write:
                update_cells(sess, sheet, n, {"status": "expired", "error": "Not approved before its slot"})
        elif t:
            pending += 1
    return expired, pending


def write_digest(sess, sheet, posts, slots, info, batch):
    head = ["scheduled_sgt", "status", "pillar", "tag", "hook", "type", "hypothesis", "text"]
    rows = [head] + [[slots[i].strftime("%Y-%m-%d %H:%M"), "buffer, auto-approved" if p.get("auto_approved") else "waiting for you", p["pillar"], p["tag"], p["hook"],
                      "TEST" if p["is_explore"] else "proven", p["hypothesis"], p["text"]] for i, p in enumerate(posts)]
    rows.insert(0, [f"Batch {batch}. Data: {info['mature']} posts at 24h+, confidence {info['confidence']}. "
                   + ("All posts are auto-approved. Untick approved on any post and a replacement is written for the same slot." if CFG["auto_approve"] else f"The first {CFG.get('buffer_hours', 0)} hours are pre-approved so posting never stops. Tick approved on the rest before their slot, or they expire.")])
    write_tab(sess, sheet, "proposals", rows)


def valid(text, seen):
    if not text or len(text) > 500 or any(re.search(b, text, re.I | re.M) for b in BANNED) or not facts_only(text):
        return False
    toks = set(re.findall(r"\w+", text.lower()))
    for s in seen:
        o = set(re.findall(r"\w+", s.lower()))
        if o and len(toks & o) / len(toks | o) >= 0.7:
            return False
    return True


CHUNK = 12  # posts per Claude call: one big call ran out of output tokens


def etf_prompt(facts, k):
    return (f"\nETF LIST EXPERIMENT: {k} of the money-pillar posts must be named ETF list posts. Each has tag Investing, "
            f"hook list, \"etf_list\": true, \"is_explore\": true. Format: a short opening line that states a concrete comparison "
            f"(for example how a group of ETFs did against the S&P 500 over a stated period), then 3 to 5 numbered ETFs with ticker, "
            f"what it holds in a few words, and its return. Use ONLY these facts, copy numbers exactly, state the period "
            f"(12 months or 24 months, as of {facts['as_of']}), and say they are price returns. Include at least one ETF that "
            f"lagged the S&P 500 so the list is balanced. Frame as what happened, never as a recommendation or a prediction. "
            f"Close the data with one short clause such as 'Past returns, not a forecast.' and nothing longer. "
            f"TEST: alternate two versions. Half end right after that clause and set \"hypothesis\" to 'ETF plain'. The other half "
            f"add one last plain takeaway line that is general, never advice to buy anything (for example that the boring "
            f"option often holds up), and set \"hypothesis\" to 'ETF takeaway'. Do not use any other figure. Vary which ETFs "
            f"and which period each post uses.\n"
            f"FACTS ({facts['source']}):\n" + "\n".join("- " + l for l in facts["lines"]) + "\n")


def generate(db, info, key, sess, sheet, n):
    pillars = list(CFG["pillars"])
    w = CFG.get("pillar_weights") or {}
    w = {p: float(w.get(p, 0)) for p in pillars} if w and sum(w.values()) > 0 else {p: 1.0 for p in pillars}
    raw = {p: n * w[p] / sum(w.values()) for p in pillars}
    left = {p: int(raw[p]) for p in pillars}
    for p in sorted(pillars, key=lambda p: -(raw[p] - int(raw[p])))[:n - sum(left.values())]:
        left[p] += 1
    explore = CFG["low_data_explore_share"] if info["confidence"] == "low" else CFG["explore_share"]
    recent = [r["text"] for r in db.execute("SELECT text FROM my_posts ORDER BY posted_at DESC LIMIT 40")]
    slots, rows = next_slots(sess, sheet, n)
    queued = [r["text"] for _, r in rows if r["text"].strip()]
    facts, remaining_etf = None, min(int(CFG.get("etf_posts_per_batch", 0)), left.get("money", 0))
    if remaining_etf:
        try:
            facts = etf_facts.ensure_fresh()
        except Exception as e:  # never block the whole batch on market data
            print(f"ETF facts unavailable ({e}). Skipping the ETF list experiment this batch.")
            remaining_etf = 0
    seen, good, dropped = list(recent + queued), [], 0
    part = 0
    while sum(left.values()) > 0:
        part += 1
        nc = min(CHUNK, sum(left.values()))
        qc = {p: 0 for p in pillars}
        while sum(qc.values()) < nc:
            for p in pillars:
                if left[p] - qc[p] > 0 and sum(qc.values()) < nc:
                    qc[p] += 1
        for p in pillars:
            left[p] -= qc[p]
        k_etf = min(remaining_etf, qc.get("money", 0)) if facts else 0
        block = etf_prompt(facts, k_etf) if k_etf else ""
        avoid = [g["text"] for g in good] + recent + queued
        user = (f"WHAT THE DATA SAYS (lift 1.0 = a typical post at 24h+):\n{learn_text(info)}\n\n"
                f"Write {nc} new posts. Split by pillar exactly: {json.dumps(qc)}.\n"
                f"About {round(explore * 100)}% must be EXPERIMENTS: each tests exactly one variable (for example length, hook style, "
                f"a number in the opening, list format, question vs statement) against what is already working, and states the "
                f"hypothesis. The rest EXPLOIT patterns marked WINNER, and avoid patterns marked LOSER. If data is thin, vary "
                f"length and hook style deliberately so results are comparable.\n"
                f"{block}"
                + (f"What each pillar should cover: {json.dumps(CFG['pillar_notes'])}\n" if CFG.get("pillar_notes") else "") +
                f"Allowed tags per pillar: {json.dumps(CFG['pillars'])}. Allowed hook labels: {', '.join(HOOKS)}.\n"
                f"Do not repeat or closely rephrase these existing posts:\n" + "\n".join("- " + t[:110] for t in avoid[:60]) +
                "\n\nReturn a JSON list. Each item: {\"pillar\": ..., \"tag\": ..., \"hook\": ..., \"text\": ..., "
                "\"is_explore\": true/false, \"etf_list\": true/false, \"hypothesis\": \"one short sentence\"}.")
        rej = [r["text"] for r in db.execute("SELECT text FROM rejections ORDER BY at DESC LIMIT 15")]
        if rej:
            user += "\n\nThe author UNAPPROVED these earlier posts, so avoid their angle and style:\n" + "\n".join("- " + t[:110] for t in rej)
        cap_list = max(1, math.ceil(float(CFG.get("max_list_share", 0.25)) * nc))
        cap_num = max(1, math.ceil(float(CFG.get("max_number_opener_share", 0.3)) * nc))
        mn = lambda share: max(1, round(nc * share))
        user += (f"\n\nFORMAT MIX, hard rules for these {nc} posts, in addition to everything above: at most {cap_list} may be numbered "
                 f"lists (lines starting '1.', '2.'), and ETF list posts count toward that; at most {cap_num} may open with a digit. "
                 f"At least {mn(.4)} must be very short statements under 120 characters (one or two lines, no list). Exactly one may be a "
                 f"standalone question post that invites replies, about AI or money; no other post may end with a question. "
                 f"{'At least one must use one of the AUTHOR FACTS in first person. ' if FACTS else ''}No scenario posts about "
                 f"hypothetical people, no 'common belief vs the other side' posts, and no moral or lesson tacked on at the end. "
                 f"Fill the rest with observations, stat hooks, plain how-to paragraphs and confessions built only from AUTHOR FACTS. "
                 f"Most posts under 150 characters; only ETF lists and posts built on AUTHOR FACTS may run to 400. "
                 f"Do not put similar formats in neighbouring posts. Set the hook "
                 f"label to the real format (question, contrarian, list, story, stat_hook, how_to, confession, observation). "
                 f"Still return only the JSON list.")
        items = None
        for attempt in (1, 2):
            try:
                items = parse_json_list(claude(GEN_SYSTEM, user, key, 12000))
                break
            except Exception as e:
                print(f"Part {part} failed (attempt {attempt}): {e}")
        if items is None:
            continue
        got_etf, n_list, n_num = 0, 0, 0
        items.sort(key=lambda it: not it.get("etf_list"))
        for it in items:
            text = clean(str(it.get("text", "")).strip())
            pillar = it.get("pillar")
            tag = it.get("tag")
            if pillar not in CFG["pillars"] or tag not in CFG["pillars"][pillar] or it.get("hook") not in HOOKS or not valid(text, seen):
                dropped += 1
                continue
            hyp = clean(str(it.get("hypothesis", "")))[:200]
            is_etf = bool(it.get("etf_list")) and facts is not None
            if is_etf:
                ok, why = etf_facts.facts_ok(text, facts)
                if not ok:
                    print(f"Dropped an ETF post: {why}.\n    {text[:400]!r}")
                    dropped += 1
                    continue
                hyp = "ETF takeaway" if "takeaway" in hyp.lower() else "ETF plain"
                got_etf += 1
            elif facts is not None and re.search(r"\d+(\.\d+)?\s?%", text) and re.search(etf_facts.TICKER_RE, text):
                print("Dropped a non-ETF post that quotes a fund return without a fact sheet.")
                dropped += 1
                continue
            is_list = bool(re.search(r"(^|\n)\s*\d+[\.\)]\s", text))
            is_num = text[:1].isdigit()
            if (is_list and n_list >= cap_list) or (is_num and n_num >= cap_num):
                print("Dropped a post over the list or number-opener cap.")
                dropped += 1
                continue
            n_list += is_list
            n_num += is_num
            seen.append(text)
            good.append({"pillar": pillar, "tag": tag, "hook": it["hook"], "text": text, "is_explore": bool(it.get("is_explore")) or is_etf,
                         "hypothesis": hyp})
        remaining_etf -= got_etf
        print(f"Part {part}: {len(items)} drafted, {len(good)} valid so far.", flush=True)
    if not good:
        raise RuntimeError("No valid posts were generated.")
    g_list = sum(bool(re.search(r"(^|\n)\s*\d+[\.\)]\s", g["text"])) for g in good)
    print(f"Format mix: {len(good)} posts, {g_list} numbered lists, {sum(g['text'][:1].isdigit() for g in good)} open with a digit, "
          f"{sum(g['text'].strip().endswith('?') or '?' in g['text'][:140] for g in good)} questions, "
          f"{sum(len(g['text']) < 150 for g in good)} under 150 characters. Hooks: {dict(Counter(g['hook'] for g in good))}")
    by = {p: [g for g in good if g["pillar"] == p] for p in pillars}
    ordered = []
    while any(by.values()):
        for p in pillars:
            if by[p]:
                ordered.append(by[p].pop(0))
    return ordered, dropped, slots, rows


def write_batch(db, sess, sheet, posts, slots, rows, force_approve=False):
    start = next(i for i, r in rows if not r["text"].strip() and not r["status"].strip())
    nid = max([int(r["id"]) for _, r in rows if r["id"].strip().isdigit()] + [0]) + 1
    batch = datetime.now(SGT).strftime("b%Y%m%d%H%M")
    values = []
    for i, p in enumerate(posts):
        auto = force_approve or bool(CFG["auto_approve"]) or (slots[i] - slots[0]) < timedelta(hours=float(CFG.get("buffer_hours", 0)))
        p["auto_approved"] = auto
        values.append([str(nid + i), p["text"], p["tag"], slots[i].strftime("%Y-%m-%d %H:%M"), "scheduled", auto])
        db.execute("INSERT OR REPLACE INTO experiments(queue_id,batch_id,pillar,tag,hook,hypothesis,is_explore,text,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (str(nid + i), batch, p["pillar"], p["tag"], p["hook"], p["hypothesis"], int(p["is_explore"]), p["text"], datetime.now(UTC).isoformat()))
    r = sess.put(f"{SHEETS_BASE}/{sheet}/values/{TAB}!A{start}:F{start + len(values) - 1}?valueInputOption=RAW", json={"values": values})
    r.raise_for_status()
    db.commit()
    return start, start + len(values) - 1, batch


# ---------------------------------------------------------------- revise
def revise(db, info, key, sess, sheet, apply, max_n):
    losers = {(f["feature"], f["value"]) for f in info["features"] if f["verdict"] == "LOSER"}
    FORM = ("length", "format", "number_in_opening", "list_format", "hook")  # form only; never topic, tag or time
    winners = [f"{f['feature']}={f['value']}" for f in info["features"] if f["verdict"] == "WINNER" and f["feature"] in FORM]
    if not losers:
        print("No losing patterns with enough data yet, so nothing to revise.")
        return
    cutoff = datetime.now(SGT) + timedelta(hours=1)
    cands = []
    for n, r in read_rows(sess, sheet):
        if r["status"].strip().lower() != "scheduled" or not r["text"].strip():
            continue
        try:
            when = datetime.strptime(r["publish_at"].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=SGT)
        except ValueError:
            continue
        if when < cutoff:
            continue
        fake = {"text": r["text"], "topic_tag": r["topic_tag"], "sgt": when}
        hit = [f"{k}={v}" for k, v in feats(fake, None).items() if (k, v) in losers and k in ("length", "format", "number_in_opening", "list_format")]
        if hit:
            cands.append((n, r, hit))
    cands = cands[:max_n]
    if not cands:
        print("No queued posts match a losing pattern.")
        return
    sysmsg = (GEN_SYSTEM.replace("Reply with only a JSON list.", "") +
              " Rewrite each given draft: keep its pillar, core idea and honesty, but change the form to use winning patterns "
              f"({', '.join(winners) or 'none established yet'}) and avoid the losing ones listed for that draft. "
              "Change form only: length, structure, opening. Keep the exact same topic. Never add new topics, analogies, examples from "
              "other pillars, or personal claims. Any number in the opening must read naturally, never forced or gimmicky. "
              "Reply with only a JSON list [{\"id\": ..., \"text\": ...}].")
    out = parse_json_list(claude(sysmsg, json.dumps([{"id": r["id"], "text": r["text"], "avoid": hit} for _, r, hit in cands]), key, 8000))
    by_id = {o.get("id"): clean(str(o.get("text", "")).strip()) for o in out}
    for n, r, hit in cands:
        new = by_id.get(r["id"])
        ok = new and valid(new, [])
        print(f"\nRow {n} (id {r['id']}, avoids {', '.join(hit)})\n  OLD: {r['text']}\n  NEW: {new if ok else '(rejected by safety checks)'}")
        if apply and ok:
            update_cells(sess, sheet, n, {"text": new})
            db.execute("INSERT INTO revisions(queue_id,old_text,new_text,at) VALUES(?,?,?,?)", (r["id"], r["text"], new, datetime.now(UTC).isoformat()))
    db.commit()
    print("\nApplied." if apply else "\nDry run only. Re-run with --apply to write these to the sheet.")


# ---------------------------------------------------------------- main
# ---------------------------------------------------------------- replace unapproved posts
def replace_rejected(db, key, sess, sheet, dry=False):
    """A queued engine post that you untick gets a fresh replacement in the same slot, auto-approved."""
    now = datetime.now(SGT)
    rows = read_rows(sess, sheet)
    exp = {r["queue_id"]: r for r in db.execute("SELECT * FROM experiments")}
    todo = []
    for n, r in rows:
        if r["status"].strip().lower() != "scheduled" or str(r["approved"]).strip().upper() == "TRUE":
            continue
        e, t = exp.get(r["id"].strip()), pub_time(r["publish_at"])
        if e and t and t > now + timedelta(minutes=2):
            todo.append((n, r, e, t))
    todo = todo[:12]
    if not todo:
        return 0
    info = learn(db)
    recent = [r["text"] for r in db.execute("SELECT text FROM my_posts ORDER BY posted_at DESC LIMIT 40")]
    seen = recent + [r["text"] for _, r in rows if r["text"].strip()]
    got = {}
    for attempt in (1, 2):
        pend = [i for i in range(len(todo)) if i not in got]
        if not pend:
            break
        items_in = [{"i": i, "pillar": todo[i][2]["pillar"], "tag": todo[i][2]["tag"], "rejected_hook": todo[i][2]["hook"],
                     "rejected_text": todo[i][1]["text"][:300]} for i in pend]
        user = (f"WHAT THE DATA SAYS (lift 1.0 = a typical post at 24h+):\n{learn_text(info)}\n\n"
                f"The author unapproved these {len(pend)} queued posts. Write exactly one replacement for each: same pillar and same tag, "
                f"a clearly different angle and, where it fits, a different format (question, short statement, contrarian take, "
                f"scenario, how-to prose, list). Do not mention the rejection. No fund or stock performance figures or ticker returns. "
                f"Allowed hook labels: {', '.join(HOOKS)}.\n" + json.dumps(items_in) +
                "\n\nDo not repeat these:\n" + "\n".join("- " + t[:110] for t in seen[:50]) +
                "\n\nReturn a JSON list, one item per input: {\"i\": ..., \"hook\": ..., \"text\": ..., \"hypothesis\": \"one short sentence\"}.")
        try:
            out = parse_json_list(claude(GEN_SYSTEM, user, key, 8000))
        except Exception as e:
            print(f"Replacement attempt {attempt} failed: {e}")
            continue
        for it in out:
            i = it.get("i")
            text = clean(str(it.get("text", "")).strip())
            if i in pend and i not in got and it.get("hook") in HOOKS and valid(text, seen) \
                    and not (re.search(r"\d+(\.\d+)?\s?%", text) and re.search(etf_facts.TICKER_RE, text)):
                seen.append(text)
                got[i] = {"pillar": todo[i][2]["pillar"], "tag": todo[i][2]["tag"], "hook": it["hook"], "text": text, "is_explore": False,
                          "hypothesis": "Replacement for an unapproved post"}
    print(f"{len(todo)} unapproved posts found, {len(got)} replacements ready.")
    if dry:
        for i, p in got.items():
            print(f"\n[{todo[i][3]:%d %b %H:%M}] {p['pillar']} | {p['tag']} | {p['hook']}\n  was: {todo[i][1]['text'][:100]!r}\n  now: {p['text']}")
        return len(got)
    for n, r, e, t in todo:
        db.execute("INSERT OR IGNORE INTO rejections(queue_id,text,hook,tag,pillar,at) VALUES(?,?,?,?,?,?)",
                   (r["id"], r["text"], e["hook"], e["tag"], e["pillar"], datetime.now(UTC).isoformat()))
    db.commit()
    if not got:
        return 0
    order = sorted(got)
    write_batch(db, sess, sheet, [got[i] for i in order], [todo[i][3] for i in order], rows, force_approve=True)
    for i in order:
        update_cells(sess, sheet, todo[i][0], {"status": "rejected"})
    return len(got)


# ---------------------------------------------------------------- daily brief and weekly review
BRIEF_SYSTEM = (
    f"You write a short daily brief for the owner of a Threads personal brand about {brandkit.TOPICS}. Plain, direct, specific. Do not flatter. Use only the numbers provided and never invent data. When a "
    "pattern rests on few posts, say how few. No em dashes or long dashes. Reply in plain text under 200 words with exactly these "
    "two headings on their own lines: FROM THE LAST BATCH, then WHAT WE ARE TRYING NEXT. Under the first, 3 to 4 short lines that say "
    f"what worked, what did not, and one thing to do about it, citing views and sample size. The data is every post published yesterday (the previous calendar day, {brandkit.TZ_LABEL} time); "
    "name that day in the first line and note that late-evening posts have had fewer hours to collect views. If there are no posts from yesterday, say so in "
    "one line. Under the second, 3 to 5 short lines, one per experiment, each saying what is being tested and what result would count "
    "as a win. End with one line: Untick approved on any post you dislike and a replacement will be written for the same slot.")

WEEKLY_SYSTEM = (
    f"You are the content strategist and growth analyst for a personal brand on Threads ({brandkit.TOPICS_SHORT}). Direct, specific, plain-spoken. Do not flatter. Use only the numbers provided and never invent data. If the "
    "samples are small, say so and keep conclusions modest. No em dashes or long dashes. Reply in plain text under 380 words with "
    "these headings on their own lines: THE WEEK IN NUMBERS (3 lines), WHAT WORKED, WHAT DID NOT, EXPERIMENT VERDICTS (each test we "
    "ran: keep, drop or retest, with its sample size), NEXT WEEK (3 specific tests), GOAL CHECK (path status and the most likely "
    "cause and one fix if behind).")


def save_report(sess, sheet, tab, name, header, text):
    body = (header + "\n\n" + text).strip()
    (HERE / "reviews").mkdir(exist_ok=True)
    (HERE / "reviews" / f"{name}_{datetime.now(SGT):%Y-%m-%d}.md").write_text(body + "\n", encoding="utf-8")
    write_tab(sess, sheet, tab, [[line] for line in body.splitlines()])
    return body


def daily_brief(db, info, key, sess, sheet):
    now = datetime.now(UTC)
    base = info.get("baseline")
    hooks = {r["post_id"]: r["hook"] for r in db.execute("SELECT post_id, hook FROM post_features")}
    ex = {r["post_id"]: r for r in db.execute("SELECT * FROM experiments WHERE post_id IS NOT NULL")}
    last = []
    yday = (datetime.now(SGT) - timedelta(days=1)).date()
    for p in latest_posts(db, 6):
        age = (now - parse_ts(p["posted_at"])).total_seconds() / 3600
        if parse_ts(p["posted_at"]).astimezone(SGT).date() == yday and p["media_type"] != "REPOST_FACADE":
            f = feats(p, hooks.get(p["post_id"]))
            e = ex.get(p["post_id"])
            last.append({"views": p["views"] or 0, "lift": round(max(p["views"] or 1, 1) / base, 2) if base else None,
                         "age_hours": round(age), "likes": p["likes"], "replies": p["replies"], "hook": f["hook"], "length": f["length"],
                         "numbered_list": f["list_format"], "number_in_opening": f["number_in_opening"], "names_etfs": f["named_etfs"],
                         "test": e["hypothesis"] if e and e["is_explore"] else None, "text": p["text"][:90]})
    last.sort(key=lambda x: -x["views"])
    latest = db.execute("SELECT MAX(batch_id) b FROM experiments WHERE batch_id!='replace'").fetchone()["b"]
    new = [dict(r) for r in db.execute("SELECT hook, tag, hypothesis, is_explore, text FROM experiments WHERE batch_id=?", (latest,))]
    tests = {}
    for r in new:
        if r["is_explore"]:
            tests.setdefault(r["hypothesis"], []).append(r["text"][:80])
    since = (now - timedelta(hours=24)).isoformat()
    ctx = {"date": f"{datetime.now(SGT):%A %d %B %Y}", "typical_views": base, "confidence": info["confidence"],
           "posts_from_yesterday": len(last), "yesterday": f"{yday:%A %d %B}", "best_of_them": last[:4], "weakest_of_them": last[-3:] if len(last) > 4 else [],
           "format_verdicts_so_far": [f for f in info["features"] if f["verdict"] in ("WINNER", "LOSER")],
           "new_batch": {"posts": len(new), "hooks": dict(Counter(r["hook"] for r in new)), "tests": [
               {"hypothesis": h, "posts": len(v), "example": v[0]} for h, v in sorted(tests.items(), key=lambda x: -len(x[1]))[:8]]},
           "posts_you_unapproved_last_24h": db.execute("SELECT COUNT(*) c FROM rejections WHERE at>=?", (since,)).fetchone()["c"]}
    text = claude(BRIEF_SYSTEM, "Data as JSON:\n" + json.dumps(ctx), key, 6000)
    body = save_report(sess, sheet, "daily_brief", "daily", f"DAILY BRIEF  {ctx['date']}", text)
    print("\n" + body)
    return body


def weekly(db, info, key, sess, sheet):
    text, stats = build_review(db, 7)
    base, now = info.get("baseline"), datetime.now(UTC)
    mature = {p["post_id"]: p for p in latest_posts(db, 8) if (now - parse_ts(p["posted_at"])).total_seconds() >= CFG["min_age_hours"] * 3600}
    ran = []
    for e in db.execute("SELECT * FROM experiments WHERE post_id IS NOT NULL AND is_explore=1"):
        p = mature.get(e["post_id"])
        if p:
            ran.append({"test": e["hypothesis"], "hook": e["hook"], "views": p["views"] or 0, "lift": round(max(p["views"] or 1, 1) / base, 2) if base else None})
    ran.sort(key=lambda x: -x["views"])
    stats["format_learnings"] = [f for f in info["features"] if f["verdict"] != "insufficient data"]
    stats["experiments_ran"] = len(ran)
    stats["experiments_best"], stats["experiments_worst"] = ran[:8], ran[-6:] if len(ran) > 8 else []
    stats["posts_you_unapproved"] = db.execute("SELECT COUNT(*) c FROM rejections WHERE at>=?", ((now - timedelta(days=7)).isoformat(),)).fetchone()["c"]
    commentary = claude(WEEKLY_SYSTEM, "Weekly data as JSON:\n" + json.dumps(stats), key, 8000)
    body = save_report(sess, sheet, "weekly", "weekly", f"WEEKLY LOOK-BACK  {datetime.now(SGT):%Y-%m-%d}", commentary + "\n\n---- THE NUMBERS ----\n" + text)
    print("\n" + body)
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "learn", "revise", "replace", "brief", "weekly"])
    ap.add_argument("--db", default=str(DB_DEFAULT))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max", type=int, default=12)
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--ignore-pending", action="store_true", help="generate even if many proposals are still waiting")
    ap.add_argument("--force", action="store_true", help="allow --apply while data confidence is low")
    a = ap.parse_args()
    env = load_env()
    key, sheet = env.get("ANTHROPIC_API_KEY"), env["GOOGLE_SHEET_ID"]
    db = open_db(a.db)
    db.executescript(SCHEMA)
    sess = sheets_session()
    if a.cmd != "learn" and not key:
        raise SystemExit("ANTHROPIC_API_KEY missing from .env")
    if a.cmd == "replace":
        print(f"Replaced {replace_rejected(db, key, sess, sheet, dry=a.dry_run)} unapproved posts.")
        return
    linked = link_results(db, sess, sheet)
    labelled = classify(db, key) if key else 0
    info = learn(db)
    print(f"Linked {linked} engine posts to live post ids. Labelled {labelled} posts.\n")
    print(learn_text(info))
    if a.cmd == "learn":
        return
    if a.cmd == "brief":
        daily_brief(db, info, key, sess, sheet)
        return
    if a.cmd == "weekly":
        weekly(db, info, key, sess, sheet)
        return
    if a.cmd == "revise":
        if a.apply and info["confidence"] == "low" and not a.force:
            raise SystemExit("Data confidence is low (fewer than 20 posts at 24h+). Review the dry run, then add --force if you still want to apply.")
        return revise(db, info, key, sess, sheet, a.apply, a.max)
    replaced = replace_rejected(db, key, sess, sheet, dry=a.dry_run)
    if replaced:
        print(f"Replaced {replaced} posts you unapproved.")
    expired, pending = review_backlog(sess, sheet, write=not a.dry_run)
    print(f"\nExpired {expired} unapproved proposals whose slot passed. {pending} proposals are still waiting for approval.")
    first_slot = next_slots(sess, sheet, 1)[0][0]
    runway = (first_slot - datetime.now(SGT)).total_seconds() / 3600
    need = float(CFG.get("min_runway_hours", 20))
    if runway > need and not a.ignore_pending:
        print(f"Skipping generation: the queue already runs {runway:.0f} hours ahead (tops up below {need:.0f}).")
        return
    print(f"Queue runs {max(runway, 0):.1f} hours ahead. Topping up.")
    n = a.n or CFG["batch_size"]
    posts, dropped, slots, rows = generate(db, info, key, sess, sheet, n)
    c = Counter(p["pillar"] for p in posts)
    print(f"\nGenerated {len(posts)} valid posts ({dropped} dropped by safety or duplicate checks). By pillar: {dict(c)}. "
          f"Experiments: {sum(p['is_explore'] for p in posts)}.")
    if a.dry_run:
        for i, p in enumerate(posts[:8]):
            print(f"\n[{slots[i]:%d %b %H:%M}] {p['pillar']} | {p['tag']} | {p['hook']} | {'TEST' if p['is_explore'] else 'proven'}\n  {p['text']}\n  hypothesis: {p['hypothesis']}")
        print("\nDry run: nothing written.")
        return
    if posts:
        s, e, batch = write_batch(db, sess, sheet, posts, slots, rows)
        write_digest(sess, sheet, posts, slots, info, batch)
        try:
            daily_brief(db, info, key, sess, sheet)
        except Exception as e:
            print(f"Daily brief failed: {e}")
        write_tab(sess, sheet, "learnings", [["feature", "value", "n", "lift", "verdict"]] +
                  [[f["feature"], f["value"], f["n"], f["lift"], f["verdict"]] for f in info["features"]])
        print(f"Wrote rows {s} to {e} of the queue (batch {batch}), approved = {bool(CFG['auto_approve'])}.")


if __name__ == "__main__":
    main()
