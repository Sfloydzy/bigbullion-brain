# Social media brain

Automated X content system for Big Bullion (X only: modules.threads is false, so the X planner writes native posts). Runs on a Windows PC through a small task runner.

| Folder | What it does |
|---|---|
| `threads_agent/` | Threads queue publisher, insights pull, self-improving content engine (48 posts a day), replacements, daily brief, weekly look-back, ETF facts |
| `x_agent/` | X loop: research, plan 12 posts a day from yesterday's best Threads posts, publish, per-post review, learn, weekly look-back |
| `brain/` | One ledger across both platforms: metrics at fixed ages, follower attribution, Claude labels, Brain Score (`brain_scores` sheet tab) |
| `brand/` | `brand.json`: everything brand-specific (voice, rules, pillars, keywords, goal, timezone, modules) |
| `deploy/` | `DEPLOY.md` runbook for setting this up for a new brand, templates, Supabase schema, `verify.py` health check |
| `dashboard/` | Windows runner (`runner.py`, every minute via Task Scheduler), `tasks.json` schedule, local dashboard |

## Setup
For a new brand, follow `deploy/DEPLOY.md`. Check any install with `python deploy/verify.py`.

1. `pip install -r requirements.txt`
2. Create `threads_agent/.env` with: `THREADS_ACCESS_TOKEN`, `THREADS_USER_ID`, `THREADS_APP_ID`, `THREADS_APP_SECRET`, `GOOGLE_SHEET_ID`, `ANTHROPIC_API_KEY`, `POLYGON_API_KEY`, `X_CONSUMER_KEY`, `X_SECRET_KEY`, `X_BEARER_TOKEN`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`.
3. Put the Google service account key at `threads_agent/google_service_account.json` and share the sheet with it.
4. Run `dashboard/setup_windows.bat` to register the runner.

Secrets, databases (`*.db`) and run state are git-ignored. The databases hold the history the brain learns from, so back them up separately.

## Brain Score
points = 25 x follows + 5 x reposts + 5 x quotes + 3 x replies + 3 x shares/bookmarks + 1 x likes.
quality = points per 1,000 views. reach = views vs a typical post at the same age.
score = 100 x (quality / typical)^0.6 x reach^0.4, so 100 = a typical post. Weights live in `brain/brain.json`.
