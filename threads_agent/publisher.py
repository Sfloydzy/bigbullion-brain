"""Publishes due, approved rows from the Google Sheet queue to Threads.

A row posts only when ALL are true:
  approved is TRUE, status is 'scheduled', publish_at (Singapore time) has passed.

Usage:
  python publisher.py --dry-run   (show what would post, change nothing)
  python publisher.py             (post due rows)
Run every 5 minutes with Windows Task Scheduler.
"""
import argparse
import sys
import time
from datetime import datetime, timedelta, timezone

from threads_common import (load_env, threads_call, sheets_session,
                            read_rows, update_cells)

from brandkit import TZ as SGT  # brand timezone from brand/brand.json (fixed offset, no tzdata needed)
FORMATS = ["%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
           "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]
MAX_LEN = 500
MAX_LATE_HOURS = 6  # approved but overdue by more than this: expire instead of posting a stale slot


def late_hours(now, when):
    return (now - when).total_seconds() / 3600


def parse_time(s):
    s = s.strip()
    for f in FORMATS:
        try:
            return datetime.strptime(s, f).replace(tzinfo=SGT)
        except ValueError:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = load_env()
    token, user_id, sheet_id = (env.get("THREADS_ACCESS_TOKEN"),
                                env.get("THREADS_USER_ID"),
                                env.get("GOOGLE_SHEET_ID"))
    if not (token and user_id and sheet_id):
        sys.exit("Missing THREADS_ACCESS_TOKEN, THREADS_USER_ID or GOOGLE_SHEET_ID in .env")

    sess = sheets_session()
    rows = read_rows(sess, sheet_id)
    now = datetime.now(SGT)
    due = 0

    for row_num, r in rows:
        if r["status"].strip().lower() != "scheduled":
            continue
        if r["approved"].strip().upper() != "TRUE":
            continue
        when = parse_time(r["publish_at"])
        if when is None:
            msg = f"Could not read publish_at '{r['publish_at']}'. Use DD/MM/YYYY HH:MM"
            print(f"Row {row_num}: {msg}")
            if not args.dry_run:
                update_cells(sess, sheet_id, row_num, {"status": "failed", "error": msg})
            continue
        if when > now:
            continue
        if late_hours(now, when) > MAX_LATE_HOURS:
            msg = f"Missed its slot by {int(late_hours(now, when))}h so it was not posted. Set a new publish_at and status to scheduled to retry."
            print(f"Row {row_num}: {msg}")
            if not args.dry_run:
                update_cells(sess, sheet_id, row_num, {"status": "expired", "error": msg})
            continue
        text = r["text"].strip()
        if not text or len(text) > MAX_LEN:
            msg = f"Text empty or over {MAX_LEN} chars ({len(text)})"
            print(f"Row {row_num}: {msg}")
            if not args.dry_run:
                update_cells(sess, sheet_id, row_num, {"status": "failed", "error": msg})
            continue

        due += 1
        print(f"Row {row_num}: due. {text[:60]!r} tag={r['topic_tag'] or '-'}")
        if args.dry_run:
            continue

        # Mark first so an overlapping run can never double post.
        update_cells(sess, sheet_id, row_num, {"status": "posting", "error": ""})
        try:
            params = {"media_type": "TEXT", "text": text, "access_token": token}
            if r["topic_tag"].strip():
                params["topic_tag"] = r["topic_tag"].strip()
            c = threads_call("POST", f"{user_id}/threads", params)
            time.sleep(5)
            p = threads_call("POST", f"{user_id}/threads_publish",
                             {"creation_id": c["id"], "access_token": token})
            update_cells(sess, sheet_id, row_num, {
                "status": "posted", "post_id": p.get("id", ""),
                "posted_at": datetime.now(SGT).strftime("%Y-%m-%d %H:%M"),
                "error": ""})
            print(f"Row {row_num}: posted {p.get('id')}")
        except Exception as e:
            update_cells(sess, sheet_id, row_num, {"status": "failed", "error": str(e)[:300]})
            print(f"Row {row_num}: FAILED {e}")

    print(f"Done. {due} due row(s)." + (" (dry run, nothing changed)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
