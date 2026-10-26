"""Rewrite posts that are already queued (Threads queue tab and X x_queue tab) into the current voice in brand/brand.json.

  python threads_agent/rewrite_queue.py                  dry run: show OLD and NEW, change nothing
  python threads_agent/rewrite_queue.py --apply          write the new text to the sheet and the databases
  options: --only threads|x   --lead 15 (skip posts due within this many minutes)

Only posts the engine wrote are touched (Threads rows the engine created, every X row). Posts you wrote yourself,
posts already posting or posted, and posts due soon are left alone. Topic, figures and facts are kept; a rewrite that
adds a number which was not in the original (or in the author facts), or breaks a rule, is rejected and the old text stays.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "x_agent"))

import engine as th  # noqa: E402
from insights import DB_DEFAULT, SGT, UTC, clean, open_db  # noqa: E402
from threads_common import load_env, read_rows, sheets_session, update_cells  # noqa: E402

CHUNK = 12
RULES = ("Rewrite each queued draft into the voice and rules above. Keep its topic and core idea. Copy every number, ticker, "
         "date and fund return exactly; never add a number that is not already in the draft. Cut it down: most posts under 150 "
         "characters, one idea, then stop. ETF or fund lists may stay as lists, but end the data with one short clause such as "
         "'Past returns, not a forecast.' and drop any longer caveat. Turn any 'Picture...' or 'Imagine...' scenario, any parable "
         "about two people and any 'the belief / the other side' structure into one direct, sharp statement. Remove any moral, "
         "lesson or question tacked onto the end, unless the whole post is a question. Never add personal claims beyond the "
         "AUTHOR FACTS. Reply with only a JSON list [{\"id\": ..., \"text\": ...}] with one item per draft.")


def nums_ok(old, new):
    return th._nums(new) <= (th._nums(old) | th.FACT_NUMS)


def ask(system, drafts, call):
    out = {}
    for i in range(0, len(drafts), CHUNK):
        part = drafts[i:i + CHUNK]
        try:
            res = th.parse_json_list(call(system, json.dumps([{"id": d["id"], "text": d["text"]} for d in part])))
        except Exception as e:
            print(f"Claude call failed for {len(part)} drafts: {e}")
            continue
        out.update({str(r.get("id")): clean(str(r.get("text", "")).strip()) for r in res})
    return out


def threads(sess, sheet, key, apply, lead):
    db = open_db(DB_DEFAULT)
    exp = {r["queue_id"] for r in db.execute("SELECT queue_id FROM experiments")}
    cutoff = datetime.now(SGT) + timedelta(minutes=lead)
    rows = read_rows(sess, sheet)
    todo = []
    for n, r in rows:
        t = th.pub_time(r["publish_at"]) if r["publish_at"].strip() else None
        if r["status"].strip().lower() == "scheduled" and r["text"].strip() and t and t > cutoff and r["id"].strip() in exp:
            todo.append({"row": n, "id": r["id"].strip(), "text": r["text"]})
    print(f"\nTHREADS: {len(todo)} queued engine posts to rewrite.")
    if not todo:
        return
    system = th.GEN_SYSTEM.replace("Reply with only a JSON list.", "") + " " + RULES
    new = ask(system, todo, lambda s, u: th.claude(s, u, key, 12000))
    others = [d["text"] for d in todo]
    done = 0
    for d in todo:
        nt = new.get(d["id"])
        ok = bool(nt) and th.valid(nt, [o for o in others if o != d["text"]]) and nums_ok(d["text"], nt)
        print(f"\n[{d['id']}] OLD: {d['text']}\n      NEW: {nt if ok else '(rejected, keeping the old text)'}")
        if apply and ok:
            update_cells(sess, sheet, d["row"], {"text": nt})
            db.execute("UPDATE experiments SET text=? WHERE queue_id=?", (nt, d["id"]))
            db.execute("INSERT INTO revisions(queue_id,old_text,new_text,at) VALUES(?,?,?,?)", (d["id"], d["text"], nt, datetime.now(UTC).isoformat()))
            done += 1
    db.commit()
    print(f"\nThreads: {done} rewritten." if apply else "\nThreads: dry run, nothing written.")


def x_queue(sess, sheet, key, apply, lead):
    import x_engine as xe
    db = xe.open_db()
    cutoff = datetime.now(SGT) + timedelta(minutes=lead)
    todo = []
    for n, r in xe.read_tab(sess, sheet):
        if r.get("status", "").strip().lower() != "scheduled" or not r.get("text", "").strip():
            continue
        try:
            due = xe.slot_dt(r["slot_sgt"].strip())
        except (ValueError, KeyError):
            continue
        if due > cutoff:
            todo.append({"row": n, "id": r["id"].strip(), "text": r["text"].strip()})
    print(f"\nX: {len(todo)} queued posts to rewrite.")
    if not todo:
        return
    system = xe.X_SYSTEM.format(max=xe.CFG["max_chars"]).replace("Reply with only a JSON list.", "") + " " + RULES
    new = ask(system, todo, lambda s, u: xe.claude(s, u, key, xe.CFG["writer_model"], 8000))
    others = [d["text"] for d in todo]
    done = 0
    for d in todo:
        nt = new.get(d["id"])
        ok = bool(nt) and xe.valid_x(nt, [o for o in others if o != d["text"]]) and nums_ok(d["text"], nt)
        print(f"\n[{d['id']}] OLD: {d['text']}\n      NEW: {nt if ok else '(rejected, keeping the old text)'}")
        if apply and ok:
            xe.update_row(sess, sheet, d["row"], {"text": nt, "chars": len(nt)})
            db.execute("UPDATE x_posts SET text=? WHERE qid=?", (nt, d["id"]))
            done += 1
    db.commit()
    print(f"\nX: {done} rewritten." if apply else "\nX: dry run, nothing written.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", choices=["threads", "x"])
    ap.add_argument("--lead", type=int, default=15)
    a = ap.parse_args()
    env = load_env()
    key, sheet = env["ANTHROPIC_API_KEY"], env["GOOGLE_SHEET_ID"]
    sess = sheets_session()
    if a.only in (None, "threads"):
        threads(sess, sheet, key, a.apply, a.lead)
    if a.only in (None, "x"):
        x_queue(sess, sheet, key, a.apply, a.lead)


if __name__ == "__main__":
    main()
