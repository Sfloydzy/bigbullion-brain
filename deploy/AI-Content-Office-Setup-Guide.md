# Build Your Own AI Content Office

A step-by-step guide to setting up an automated content system from scratch. It researches what works in your niche, writes posts in your voice, publishes them to Threads and X, measures the results and learns from them. You stay in control: every post waits in a Google Sheet where you can approve or kill it from your phone.

It runs on a small rented server, so your own computer can be off.

**Who this is for:** anyone comfortable copying commands into a terminal and creating a few free accounts. You do not need to be a programmer.

**What it does not do:** it will not make you famous. It automates the work of researching, writing, posting and measuring. A clear point of view, real stories and showing up in replies are still yours to bring.

---

## Part 1. How it works

The system is an "office" with four rooms and one loop.

```
INTAKE (research + numbers)  ->  BRAIN (scores every post)
        ^                               |
        |                               v
OUTPUTS (Threads + X)    <-    WRITERS (draft the posts)
```

1. **Intake** gathers what is working in your niche and how your own posts performed.
2. **The Brain** files every post and gives it a score.
3. **Writers** draft new posts using what the scores say.
4. **Outputs** publish the approved posts on a schedule.
5. Results flow back to Intake and the loop repeats.

### Who works in each room

**Intake: research and numbers**

| Role | Job |
|---|---|
| Threads metrics desk | Reads views, likes, replies, reposts and shares for every Threads post, including ones you posted by hand. Runs every 3 hours. |
| X metrics desk | Does the same for X. |
| Follower counter | Records your follower count so the Brain can tell which posts earned follows. |
| X scout | Searches X for posts that are doing well in your topics and picks good accounts to reply to. Capped each day to keep costs down. |
| Notes mailbox | Reads the notes you write in the Sheet when you approve or kill a post, so the system learns your taste. |

**The Brain: scoring**

| Role | Job |
|---|---|
| Ledger clerk | Keeps one record of every post with its numbers at fixed ages (1 hour, 6 hours, 24 hours, 72 hours, 7 days), so posts compare fairly. |
| Labeller | Uses Claude to tag each post: topic, angle, and whether it contains proof such as a real number or a concrete example. |
| Credit clerk | Splits follower gains across the posts that most likely caused them. |
| Score analyst | Gives each post a Brain Score, where 100 is a typical post. Follows count most, then reposts, replies and saves, and likes count least. |

**Writers: drafting**

| Role | Job |
|---|---|
| Threads writer | Writes the Threads queue in your voice, learning from what scored well. Tops it up so there are no gaps. |
| Replacer | When you kill a post, writes a new one for the same time slot within about 10 minutes. |
| X planner | Each morning plans the day's X posts (6 by default), drawing on the best recent Threads posts and the research. Keeps a few reserves. |
| Reply helper | For the best reply targets, gives you a one-click link that opens the reply box. You send the reply yourself, because X does not allow automated replies to strangers. |

**Outputs: publishing**

| Role | Job |
|---|---|
| Threads publisher | Every 5 minutes, posts anything that is approved and due. |
| X publisher | Same for X, at the times you set. |

**Building staff (the server)**

| Role | Job |
|---|---|
| Clock | Wakes the whole office every minute so everything runs on time. |
| Inspector | Checks memory, disk space and that the clock is still ticking, every hour. |
| Firewall | Lets only you in, using your SSH key. |
| Update truck | Brings new code from GitHub to the server when you run the update command. |
| Dashboard | A status board you open on your computer through a private tunnel. |

---

## Part 2. What you need

### Accounts

| Account | Used for | Cost |
|---|---|---|
| Threads (Instagram) account | The profile that posts | Free |
| Meta developer account | Gives you access to the Threads API | Free |
| X account and X developer account | Posting and research on X | Pay as you go |
| Anthropic API account | The AI that writes the posts | Pay as you go |
| Google account | The Sheet that holds the queue | Free |
| GitHub account | Stores your copy of the code (keep it private) | Free |
| DigitalOcean account | The always-on server | About $12 a month for the 2 GB plan (check current pricing) |
| Supabase account (optional) | An off-site copy of the brain's data | Free tier |

### On your computer
- **Python 3.11 or newer.** Check with `python --version` (on Mac or Linux, `python3 --version`).
- **Git.** Check with `git --version`.
- **A terminal.** Windows: PowerShell. Mac: Terminal. Linux: any terminal.

### The code
You need the project folder, which contains these folders and files: `brain`, `threads_agent`, `x_agent`, `dashboard`, `brand`, `deploy` and `requirements.txt`. Create a new **private** repository on GitHub and push the project into it. Everything below assumes your repository is at `git@github.com:YOUR_USER/YOUR_REPO.git`. Replace `YOUR_USER` and `YOUR_REPO` with your own.

### Costs and approvals to expect
- The server is the fixed cost. The Anthropic and X APIs are pay as you go, so spend depends on how much you post. Check each dashboard after the first few days.
- Account approvals are the slow part. Start the Meta and X developer applications first.
- Threads keyword search needs Meta's app review. Until it is approved, research and reply targets on Threads stay off. Everything else works.

### Safety rules
- Never paste API keys, tokens or passwords into a chat, a document or GitHub.
- Keys live in one file on your computer and one file on the server. The `.gitignore` already stops them from being committed.
- Never share your SSH **private** key (the file with no `.pub` ending).

---

## Part 3. Set up on your computer

Run these in your terminal. Replace the repository address with yours.

```
git clone git@github.com:YOUR_USER/YOUR_REPO.git ai-content-office
cd ai-content-office
pip install -r requirements.txt
```

Create your keys file from the template.

Windows (PowerShell):
```
copy deploy\env.template threads_agent\.env
```
Mac or Linux:
```
cp deploy/env.template threads_agent/.env
```

Open `threads_agent/.env` in any text editor. You will fill in the empty values over the next steps.

### Step 1. Describe your brand

Everything specific to you lives in one file: `brand/brand.json`. Copy `deploy/brand.template.json` over it and fill in:

| Field | What to write |
|---|---|
| `name` | Your name or brand name |
| `handles` | Your Threads and X usernames |
| `timezone` | Your city, its IANA name (for example `America/New_York`) and its UTC offset in hours. It is a fixed offset, so change it by hand if daylight saving applies to you. |
| `topics` | The 3 to 6 subjects you post about |
| `author_story` | Who you are in one or two **true** sentences. The writer may only use this as your background and will not invent more. |
| `voice` | How you sound, for example short sentences, plain language, no hype |
| `post_rules` | Hard rules: claims never to make, phrases to avoid, how to handle regulated topics such as money, health or legal. Keep anything sensitive as education, not advice. |
| `pillars` | 3 to 6 content themes, each with 1 or 2 Threads topic tags |
| `research_keywords` | 15 to 30 phrases people actually use when posting about your topics, each tagged with a pillar |
| `goal` | Your follower target and dates |
| `modules` | Switch on only what you use: `x`, `brain`, `supabase` |

Tip: you can ask Claude to interview you and fill this file in. Read the rules it writes back to you before you accept them.

### Step 2. Threads access (required)

1. Go to developers.facebook.com and create an app with the **Threads API** use case.
2. Add these permissions: `threads_basic`, `threads_content_publish`, `threads_manage_insights`, `threads_manage_replies`, `threads_read_replies`. Also request `threads_keyword_search`, which needs Meta's app review before it works.
3. Add your Threads account as a Threads tester, then accept the invite in the Threads app (Settings, Account, Website permissions, Invites).
4. Generate a user token, exchange it for a long-lived token, and put it in `THREADS_ACCESS_TOKEN`. Put your app ID and secret in `THREADS_APP_ID` and `THREADS_APP_SECRET`.
5. For `THREADS_USER_ID`, leave it for now. The test in Step 6 prints the right value if it is wrong.

The system refreshes the token every week by itself.

### Step 3. Google Sheet (required)

1. In the Google Cloud console, create a project and enable the **Google Sheets API**.
2. Create a **service account** and download its JSON key.
3. Save that file as `threads_agent/google_service_account.json`.
4. Create a new Google Sheet. Share it with the service account's email address as **Editor**.
5. Copy the long ID from the Sheet's web address and put it in `GOOGLE_SHEET_ID`.

### Step 4. Claude API (required)

Go to console.anthropic.com, create an API key and put it in `ANTHROPIC_API_KEY`. Add a small amount of credit and set a monthly spending limit.

### Step 5. X (if you switched the `x` module on)

1. Go to developer.x.com and create an app.
2. Under user authentication, set permissions to **Read and write** and the type to **Web App, Automated App or Bot**. Any https address works for the callback and website fields.
3. Save. **Then** go to Keys and tokens and generate the OAuth 1.0 **Access Token and Secret**. If you generate them before saving Read and write, posting fails with a 403 error.
4. Fill in `X_CONSUMER_KEY`, `X_SECRET_KEY`, `X_BEARER_TOKEN`, `X_ACCESS_TOKEN` and `X_ACCESS_SECRET`.
5. Add credit to your X developer account.

### Step 6. Test everything

```
python deploy/verify.py --fix
```

This checks every piece and prints PASS, WARN or FAIL. The `--fix` flag creates the missing Sheet tabs. Fix every FAIL before going on. A WARN about the runner not being started is normal at this point.

### Step 7. A first dry run (nothing gets posted)

```
python threads_agent/insights.py pull --days 30
python brain/brain.py sync
python threads_agent/engine.py run --dry-run
```

The last command prints draft posts without publishing. Read every draft. If it does not sound like you, edit `voice` and `post_rules` in `brand/brand.json` and run it again until you would post them yourself. If you use X, also run:

```
python x_agent/x_engine.py research
python x_agent/x_engine.py plan --dry-run
```

New accounts have little data, so messages like "not enough data" are normal.

### Step 8. Choose approval mode and tidy the task list

- In `threads_agent/engine.json`, `auto_approve: true` posts everything unless you untick it. `auto_approve: false` waits for you to tick each post. Start with `false` for the first week if you want full control.
- In `dashboard/tasks.json`, set `"enabled": false` for any task belonging to a module you turned off (for X: `x_publish`, `x_cycle`, `x_review`, `x_weekly`). Leave `threads_research` and `research` disabled until Meta approves keyword search.
- Set your posting times in `x_agent/x.json` (`slots_sgt`, in your own local time despite the name) and the Threads spacing in `threads_agent/engine.json` (`slot_minutes`, `batch_size`).

Commit and push your changes (this does not include your keys):

```
git add .
git commit -m "My brand settings"
git push
```

---

## Part 4. Put it on a server

### Step 9. Create an SSH key (on your computer)

```
ssh-keygen -t ed25519 -C "my-server"
```

Press Enter to accept the default file location. A passphrase is optional. Then print the **public** key:

Windows (PowerShell):
```
type $env:USERPROFILE\.ssh\id_ed25519.pub
```
Mac or Linux:
```
cat ~/.ssh/id_ed25519.pub
```

Copy the whole line that starts with `ssh-ed25519`.

### Step 10. Create the server

On DigitalOcean, create a Droplet:

- **Image:** Ubuntu 24.04 (LTS)
- **Plan:** Basic, Regular (SSD), 1 vCPU, 2 GB RAM
- **Region:** the one closest to you
- **Authentication:** SSH Key. Click Add SSH Key, paste your public key and give it a name.
- **Networking:** public IPv4 on, IPv6 off
- **Monitoring:** Improved metrics on
- **Backups:** off to start (you can turn them on later for about 20% extra)
- **Hostname:** `brand-engine`

Click Create Droplet and copy its **public IPv4 address**. In the commands below it is written `YOUR_SERVER_IP`.

### Step 11. Log in

```
ssh root@YOUR_SERVER_IP
```

Type `yes` if it asks about the fingerprint. You should now see a prompt like `root@brand-engine:~#`. All the commands in steps 12 to 14 run here, as root.

### Step 12. Prepare the server and create a GitHub key for it

Paste this whole block:

```
apt-get update && apt-get install -y git
id brand >/dev/null 2>&1 || adduser --disabled-password --gecos "" brand
mkdir -p /home/brand/.ssh && cp /root/.ssh/authorized_keys /home/brand/.ssh/
chown -R brand:brand /home/brand/.ssh && chmod 700 /home/brand/.ssh && chmod 600 /home/brand/.ssh/authorized_keys
mkdir -p /srv/brands && chown brand:brand /srv/brands
[ -f /home/brand/.ssh/id_github ] || sudo -u brand ssh-keygen -t ed25519 -N "" -f /home/brand/.ssh/id_github -C brand-engine-deploy
cat /home/brand/.ssh/id_github.pub
```

It prints a public key. Copy it, then in your GitHub repository go to **Settings, Deploy keys, Add deploy key**. Paste it, name it `brand-engine`, and leave **Allow write access unticked**. This lets the server read your code but not change it.

### Step 13. Download the code and finish setup

Replace `mybrand` with a short lowercase name for your brand (letters, numbers and dashes only), and use your own repository address:

```
sudo -u brand env GIT_SSH_COMMAND="ssh -i /home/brand/.ssh/id_github -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" git clone git@github.com:YOUR_USER/YOUR_REPO.git /srv/brands/mybrand
bash /srv/brands/mybrand/deploy/server/setup_server.sh
bash /srv/brands/mybrand/deploy/server/new_brand.sh mybrand git@github.com:YOUR_USER/YOUR_REPO.git
```

What these do:
- The first downloads your code into `/srv/brands/mybrand`.
- The second updates the server, adds 2 GB of swap memory, turns on the firewall (SSH only), and installs the hourly health check.
- The third builds your brand's Python environment, registers its one-minute clock, and starts its dashboard. The clock is not started yet.

When it finishes it prints the next steps. Leave this window open.

### Step 14. Send your keys to the server

Open a **second** terminal on your computer (do not use the server window), go to your project folder, and run:

```
scp threads_agent/.env threads_agent/google_service_account.json brand@YOUR_SERVER_IP:/srv/brands/mybrand/threads_agent/
```

Back in the **server** window, test the setup:

```
sudo -u brand /srv/brands/mybrand/.venv/bin/python /srv/brands/mybrand/deploy/verify.py
```

You want zero FAIL lines. Then start your brand:

```
systemctl enable --now brand-runner@mybrand.timer
sleep 75
cat /srv/brands/mybrand/dashboard/heartbeat.json
journalctl -u brand-runner@mybrand.service -n 15 --no-pager
```

If `last_tick` in the heartbeat shows a recent time, the office is running. Finally, create a second login that is not root:

```
exit
ssh brand@YOUR_SERVER_IP
```

If that works, use the `brand` login from now on.

### Step 15. See the dashboard

On your computer:

```
ssh -N -L 8765:127.0.0.1:8765 brand@YOUR_SERVER_IP
```

Leave that window open and visit http://localhost:8765 in your browser. Press Ctrl+C in the window to disconnect. The dashboard on the server keeps running either way. The dashboard is only reachable through this tunnel, so nothing is exposed to the internet.

---

## Part 5. Running it

### Your daily routine (10 to 20 minutes)
- Open the `queue` tab. Untick anything off-brand and write a few words in the `note` column about why. A replacement is written for that slot.
- Send 3 to 5 replies from the `x_research` tab using the one-click links.
- Read the `daily_brief` and `brain_scores` tabs to see what is working.
- Write a few posts yourself each week. Posts you publish by hand are tracked too, and personal stories are the strongest signal of who you are.

### Useful commands

On the server:

| Task | Command |
|---|---|
| Update the code after you push changes | `bash /srv/brands/mybrand/deploy/server/update_brand.sh mybrand` (run as root) |
| Check server health | `systemctl status brand-health` |
| See recent activity | `journalctl -u brand-runner@mybrand.service -n 30 --no-pager` |
| Stop the office | `systemctl disable --now brand-runner@mybrand.timer` |
| Start it again | `systemctl enable --now brand-runner@mybrand.timer` |

**Remember:** the live code runs on the server. If you change a prompt, a rule or a setting on your computer, it does nothing until you `git push` and then run the update command on the server.

### Adding a second brand
Repeat Parts 3 and 4's brand and key steps for the new brand (its own repository or branch, its own keys and Sheet), then on the server run:

```
bash /srv/brands/mybrand/deploy/server/new_brand.sh secondbrand git@github.com:YOUR_USER/YOUR_OTHER_REPO.git
```

Each brand gets its own folder, clock (staggered by 15 seconds) and dashboard (the next ports are 8766, 8767 and so on). A 2 GB server comfortably runs 3 to 4 brands. Note that GitHub allows a deploy key on only one repository, so a brand in a separate repository needs its own server key.

---

## Part 6. Honest expectations

- The office is very good at producing views. It is much weaker at turning views into followers. Followers come from trust, a clear point of view, your own stories and replies you write yourself.
- Early results are noisy. Treat what the Brain Score says as a lead to test, not a rule, until you have a few weeks of data.
- Keep reviewing. The system saves hours of work, but its voice is only as good as your brand file and the feedback you give it in the notes.
- If you post about money, health or law, keep it educational. The `post_rules` field is where you stop the writer from making claims you cannot support.
- Do not fake results, income or testimonials in your content. The writer is told not to, and your credibility is the part that lasts.

---

## Part 7. When something goes wrong

| What you see | What to do |
|---|---|
| Nothing is posting | In the Sheet, is the post ticked as approved and due? Then check the `last_tick` in the heartbeat file and the last lines of the journal command above. |
| `verify.py` shows FAIL | Read the line. It names the missing key or setting. Fix it and run it again. |
| X posting gives a 403 error | The access token was created before Read and write was saved. Generate a new one and update `X_ACCESS_TOKEN` and `X_ACCESS_SECRET`. |
| Threads token expired | It refreshes itself weekly. If it fully expired, generate a new long-lived token and replace it in `.env` on the server. |
| Changed a setting and nothing happened | Push to GitHub, then run the update command on the server. |
| "database is locked" or disk errors | Keep the project on a normal local drive, not a synced or network folder. |
| Server feels slow or out of memory | Run `systemctl status brand-health`. If memory is low, run fewer brands or move to a larger plan. |
