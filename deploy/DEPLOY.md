# Deploying the Social Media Brain for a new brand

This is the runbook Claude follows to set the whole system up for a new person or brand. It is also readable by a human doing it by hand. Work top to bottom. After each step, run `python deploy/verify.py` and do not move on while that step's checks are FAIL.

## What gets deployed

| Part | Folder | What it does |
|---|---|---|
| Threads engine | `threads_agent/` | Writes posts in the brand's voice to a Google Sheet queue, publishes them on schedule, replaces unticked posts, learns from results, daily brief and weekly look-back |
| X agent | `x_agent/` | Picks yesterday's best Threads posts, rewrites 6 a day for X, posts them, reviews each post, researches the niche, drafts replies |
| Brain | `brain/` | One ledger for both platforms: metrics at fixed ages, follower attribution, labels, Brain Score; mirrors to Supabase |
| Runner | `dashboard/` | Runs every task on schedule on a Windows PC (`tasks.json`) |
| Brand | `brand/brand.json` | The only brand-specific file. Voice, rules, pillars, keywords, goal, timezone, modules |

Everything runs on one always-on Windows PC. Data lives in local SQLite files, a Google Sheet (the control panel) and optionally Supabase.

## Rules for Claude while deploying
- Never ask the owner to paste a secret into chat. Every key goes straight into `threads_agent/.env` on their machine. Check presence with `verify.py`, never by printing values.
- Never invent anything about the owner in `brand.json`. The writer is told never to fabricate income, results or stories, and `author_story` is the only background it may use, so it must be true and approved by the owner.
- Use connectors when present: the Supabase connector to create the project and apply the schema, the device bridge to write files on the owner's PC.
- Ask before anything that costs money: X API credits, Supabase paid tiers, Claude API spend.

## Costs to tell the owner up front (check current pricing)
- Claude API: the main cost. Roughly 2 to 4 generation calls a day plus small labelling and report calls.
- X API: pay-per-use; each post and each read is billed. Reads are capped at 400 a day in `x_agent/x.json`.
- Google Sheets, Threads API, Polygon (free tier), Supabase (free tier): normally free at this volume.

---

## Step 1. Brand interview, then write `brand/brand.json`

Start from `deploy/brand.template.json`. Ask the owner these questions (one short round, then draft and confirm):

1. Name or brand name, and the Threads and X handles.
2. Timezone (city). Set `label`, `iana` and `utc_offset_hours` (fixed offset; no daylight saving support, so pick the offset they want year-round).
3. The 3 to 6 subjects they post about, and who the audience is.
4. Their true background in one or two sentences (becomes `author_story`). What may the writer say in first person?
5. Voice: three adjectives, sentence length, anything they hate (jargon, cliches, emojis).
6. Hard rules: regulated topics (finance, health, legal) and how to handle them; phrases or disclaimers they never want; claims to avoid.
7. Content pillars (3 to 6). For each, 1 to 2 Threads topic tags (short, existing Threads topics work best).
8. 15 to 30 research keywords, each tagged with a pillar, phrased the way people actually post.
9. Follower goal: start date, target, target date, milestones.
10. Modules: X on or off, Supabase on or off, ETF facts only for finance brands that want real fund returns (needs Polygon).

Then:
- Write `brand/brand.json`. Fill `topic_groups` with 10 to 15 specific sub-topics plus `personal story` and `other`.
- Read back the rendered rules to the owner: "The writer will be told: ..." and get a yes.
- Set the posting times in `x_agent/x.json` (`slots_sgt` and `prime_slot_order` are in the brand's local time despite the name) and the Threads slot spacing in `threads_agent/engine.json` (`slot_minutes`, `batch_size`).

## Step 2. The machine

1. A Windows PC that stays on. Set sleep to Never on power (Settings, System, Power).
2. Install Python 3.11+ (tick "Add to PATH") and Git.
3. Get the code into a folder such as `C:\agents`:
   `git clone <repo url> C:\agents` (the repo is private; the owner needs access, or copy the folder).
4. `cd C:\agents` then `pip install -r requirements.txt`.
5. Copy `deploy/env.template` to `threads_agent/.env`. Empty values are filled in step 3.
6. Make sure the old brand's data is not carried over: no `*.db` files, no `etf_facts.json` from another brand, an empty `reviews/` folder.

## Step 3. Accounts and keys

### 3a. Threads API (required)
1. developers.facebook.com, create an app with the Threads API use case.
2. Permissions: `threads_basic`, `threads_content_publish`, `threads_manage_insights`, `threads_manage_replies`, `threads_read_replies`. Add `threads_keyword_search` too; it needs Meta App Review before it works (research and reply targets on Threads stay off until approved, everything else works).
3. Add the brand's Threads account as a Threads tester, then accept the invite in the Threads app (Settings, Account, Website permissions, Invites).
4. Generate a user token, exchange it for a long-lived token, put it in `THREADS_ACCESS_TOKEN`. Put the app ID and secret in `THREADS_APP_ID` and `THREADS_APP_SECRET` (the weekly `token_refresh` task keeps the token alive).
5. `THREADS_USER_ID`: run `verify.py`; it prints the right id if it differs.

### 3b. Google Sheet (required)
1. Google Cloud console: new project, enable the Google Sheets API, create a service account, download a JSON key.
2. Save the key as `threads_agent/google_service_account.json`.
3. Create a Google Sheet (any name). Share it with the service account's email as Editor.
4. Put the sheet ID (the long part of its URL) in `GOOGLE_SHEET_ID`.
5. `python deploy/verify.py --fix` creates the `queue` tab and its header row. Other tabs are created by the tasks on first run.

### 3c. Claude API (required)
console.anthropic.com, create an API key, put it in `ANTHROPIC_API_KEY`. Models are set in `threads_agent/engine.json`, `x_agent/x.json` and `brain/brain.json`.

### 3d. X (if `modules.x`)
1. developer.x.com, create an app. User authentication settings: permissions **Read and write**, type **Web App, Automated App or Bot**, any https callback and website URL.
2. Save, then in Keys and tokens generate (or regenerate) the **OAuth 1.0 Access Token and Secret**. They must be generated after Read and write is saved, or posting fails with 403.
3. Fill `X_CONSUMER_KEY`, `X_SECRET_KEY`, `X_BEARER_TOKEN`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`. OAuth 2.0 tokens are not used.
4. Add API credits to the X account. Note: since Feb 2026 the X API cannot reply to posts from accounts that have not tagged you, so reply targets come as one-click links the owner sends by hand.

### 3e. Polygon (if `modules.etf_facts`)
polygon.io free key into `POLYGON_API_KEY`. Edit the fund list in `threads_agent/etf_facts.py` (`UNIVERSE`) if needed.

### 3f. Supabase (if `modules.supabase`)
1. Create a project in the region nearest the owner (Claude: Supabase connector `create_project`; ask which organization).
2. Apply `deploy/supabase_schema.sql` (connector `apply_migration`, or the SQL editor).
3. Put the project URL in `SUPABASE_URL`. The owner copies a secret key (`sb_secret_...`, Project Settings, API Keys) into `SUPABASE_SECRET_KEY` themselves; connectors cannot read secret keys.

Run `python deploy/verify.py` now. Everything should be PASS except the runner heartbeat.

## Step 4. First data and dry runs (no posting yet)

Run from `C:\agents`:
1. `python threads_agent/insights.py pull --days 30` to load the account's existing posts and followers.
2. `python brain/brain.py sync` to build the ledger, labels and first scores.
3. `python threads_agent/engine.py learn` to see what the data says (new accounts will say "not enough data", which is fine).
4. `python threads_agent/engine.py run --dry-run` and read every draft with the owner. Fix voice or rules in `brand.json` and repeat until the owner would post them.
5. If X is on: `python x_agent/x_engine.py research` then `python x_agent/x_engine.py plan --dry-run`.

## Step 5. Go live

1. Decide approval mode in `threads_agent/engine.json`: `auto_approve: true` (posts go out unless unticked) or `false` with `buffer_hours` (owner ticks each post). Start new brands on `false` for the first week if they are nervous.
2. `python threads_agent/engine.py run` writes the first real batch into the queue.
3. In `dashboard/tasks.json`, disable tasks for modules that are off (`x_publish`, `x_cycle`, `x_review`, `x_weekly` if no X). `threads_research` and `research` stay disabled until keyword search is approved.
4. Run `dashboard/setup_windows.bat` to register the runner (Task Scheduler, every minute). Check `dashboard/heartbeat.json` updates and `python deploy/verify.py` is fully green.
5. Watch the first scheduled post go out in the `queue` tab (`status` becomes `posted` with a `post_id`).

## Step 6. Hand over to the owner

What they do:
- Daily: skim the `queue` and `x_queue` tabs; untick anything off-brand and write a few words in `note` (a replacement is written for the same slot).
- Daily, 20 minutes: send replies from `x_research` with the one-click links.
- Read: `daily_brief` (each new batch), `x_review` (per X post), `brain_scores` (what is working), `weekly` and `x_weekly` (Mondays).
- Write some posts themselves. Manual posts are tracked and often score highest.

Schedule (brand's local time): Threads publisher every 5 min; replacements every 10 min; brain sync every 30 min; insights and top-up every 3 h; X plan 06:00; X posts at the `x.json` slots; weekly look-backs Monday 09:00 and 09:30.

## Deploy to a VPS (always on, several brands)

Use this instead of Windows Task Scheduler when the PC sleeps or you run more than one brand. One small Ubuntu 24.04 server (2 GB RAM, 1 vCPU) runs 3 to 4 brands.

Layout on the server: `/srv/brands/<brand>/` is one full copy of the repo per brand, each with its own `.venv`, `.env`, service account file and databases. A systemd timer (`brand-runner@<brand>.timer`) runs `dashboard/runner.py` every minute for that brand, exactly like the Windows runner. Each brand's schedule uses its own timezone from `brand/brand.json`.

### One-time server setup
1. Create the droplet (Ubuntu 24.04, SSH key login, public IPv4, monitoring on).
2. SSH in as root, get the code onto the server once (e.g. paste `deploy/server/setup_server.sh` into a file, or clone temporarily), then run `bash setup_server.sh`. It updates the OS, adds 2 GB swap, a firewall (SSH only), a non-root `brand` user, a GitHub key, and an hourly health check.
3. Add `/home/brand/.ssh/id_github.pub` as a read-only Deploy key on the brand's GitHub repo.

### Add a brand
1. As root: `bash /srv/brands/<first-brand>/deploy/server/new_brand.sh <name> git@github.com:<owner>/<repo>.git [branch]`
2. Copy secrets and data from the old machine (from your PC, never through chat):
   `scp threads_agent\.env threads_agent\google_service_account.json brand@SERVER:/srv/brands/<name>/threads_agent/`
   plus the databases if you are migrating a running brand: `threads_agent/threads.db`, `x_agent/x.db`, `brain/brain.db`.
3. Check: `sudo -u brand /srv/brands/<name>/.venv/bin/python /srv/brands/<name>/deploy/verify.py`
4. Start it: `systemctl enable --now brand-runner@<name>.timer`
5. Watch it: `journalctl -u brand-runner@<name>.service -n 30` and `/srv/brands/<name>/dashboard/state.json`.

### Migrating a brand from the PC
Never run the same brand on two machines at once (double posts). Order: stop the PC tasks, copy the databases, start the server timer, confirm the next scheduled post goes out, keep the PC copy untouched for a week as a fallback.

### Day to day
- Update code: `bash deploy/server/update_brand.sh <name>` (pulls GitHub, refreshes packages).
- Health: `systemctl status brand-health` (memory, swap, disk, runner heartbeat per brand). `journalctl -u brand-health -n 20` for history.
- Dashboard: runs all the time as `brand-dashboard@<name>` on localhost only (first brand 8765, next 8766, and so on). From your PC: `ssh -N -L 8765:127.0.0.1:8765 brand@SERVER`, then open http://localhost:8765 (use the brand's own port). Pause works from the dashboard; change schedules in `dashboard/tasks.json` in the repo, not in the dashboard.
- Daylight saving: schedules use a fixed UTC offset from `brand.json`. Brands in DST zones need `utc_offset_hours` changed twice a year.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Threads keyword search 403 code 10 | Needs Meta App Review for `threads_keyword_search`. Everything else still works |
| Threads token expired | `python threads_agent/refresh_token.py` (runs weekly by itself); if fully expired, generate a new long-lived token |
| X 403 on posting | Access token was generated before Read and write was saved. Regenerate the OAuth 1.0 token |
| X reply refused | Expected: API replies only work when the other account tagged you. Use the reply links |
| Gaps in posting | PC asleep or runner stopped. Check `dashboard/heartbeat.json` and the sleep setting |
| "database is locked" or disk I/O errors | Keep `C:\agents` on a local drive, not a network or synced folder |
| Engine output cut off | Lower `batch_size` in `engine.json`; generation already runs in chunks of 12 |
| Supabase mirror failed | Logged and retried every sync; check `SUPABASE_SECRET_KEY` and that the schema was applied |

## Files a new brand changes

- `brand/brand.json` (always)
- `threads_agent/.env` and `threads_agent/google_service_account.json` (always, never committed)
- `x_agent/x.json` posting slots, `threads_agent/engine.json` cadence and approval mode (usually)
- `dashboard/tasks.json` enabled flags (if a module is off)
- `threads_agent/etf_facts.py` fund list (finance brands only)
