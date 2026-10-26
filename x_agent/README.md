# X agent

Loop: research -> plan -> post -> review -> learn -> repeat. Runs on the Windows runner (dashboard/tasks.json).

| When | Task | What it does |
|---|---|---|
| Daily 06:00 SGT | `x_engine.py cycle` | Research X (5 rotating keywords, 40 posts each), review, learn, then plan today's 12 posts |
| Every 5 min | `x_engine.py publish` | Posts due rows from `x_queue`, one per run. Unticked rows get a reserve swapped in |
| Every 3 h | `x_engine.py review` | Impressions and engagement at 1h, 6h, 24h, 72h, 7d. Verdict + one-line note per post at 24h |
| Monday 09:30 | `x_engine.py weekly` | Week look-back, test verdicts, whether Threads winners carry over to X |

## How posts are chosen
1. Every Threads post from the last 14 days gets an age-adjusted lift: its views against what a post that old normally has.
   Engagement rate adds a small bonus. Posts about the Threads platform or the automation are skipped.
2. Yesterday's posts (SGT) are ranked first. If there are not enough, the rest of the week fills in. Max 2 per topic tag.
3. Claude (Opus) rewrites the top 10 natively for X (275 chars max, no links or hashtags, no invented claims).
   It uses X learnings, Threads learnings, today's research themes and posts you have unticked.
4. The 6 best go to slots, with the strongest in the best slot. The other 4 become reserves.
5. 2 of the 6 carry one deliberate test from `test_menu` in `x.json` (least-tested first). The top 2 are never tested.

## Sheet tabs
- `x_queue`: the schedule. Untick `approved` before the slot and a reserve replaces it.
- `x_review`: one row per X post with verdict (WIN 1.5x typical, FLOP 0.67x or less), numbers, test and note.
- `x_research`: what is getting traction, angles for you, and reply targets with a drafted reply (send by hand).
- `x_weekly`: the Monday look-back.

## Keys (threads_agent/.env)
- Reads: `X_BEARER_TOKEN` (or the user keys).
- Posting: `X_CONSUMER_KEY`, `X_SECRET_KEY`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`.

## Spend controls (x.json)
- `max_reads_per_day` 400 caps search and metric reads. Usage is logged in the `x_usage` table and in the weekly report.
- 12 posts a day, one every 2 hours.
