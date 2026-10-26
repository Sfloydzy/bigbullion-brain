"""Shared logic for the agent task dashboard and runner (standard library only)."""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent  # C:\agents
TASKS_FILE = HERE / "tasks.json"
STATE_FILE = HERE / "state.json"
BEAT_FILE = HERE / "heartbeat.json"


def _brand_tz():
    """Brand's local time from brand/brand.json (fixed UTC offset). Falls back to Singapore."""
    try:
        b = json.loads((ROOT / "brand" / "brand.json").read_text(encoding="utf-8"))
        return timezone(timedelta(hours=float(b["timezone"]["utc_offset_hours"])))
    except Exception:
        return timezone(timedelta(hours=8))


SGT = _brand_tz()  # name kept for compatibility; fixed offset, so brands in DST zones need a manual change twice a year
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def now():
    return datetime.now(SGT)


def parse(ts):
    return datetime.fromisoformat(ts) if ts else None


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_tasks():
    return load_json(TASKS_FILE, {"tasks": []})["tasks"]


def save_tasks(tasks):
    save_json(TASKS_FILE, {"tasks": tasks})


def describe(s):
    if s["type"] == "interval":
        m = s["minutes"]
        return f"Every {m} min" if m < 120 else f"Every {m // 60} h"
    if s["type"] == "daily":
        return f"Daily at {s['time']}"
    return f"Weekly, {s['day'].capitalize()} at {s['time']}"


def next_run(task, st, at):
    s = task["schedule"]
    if s["type"] == "interval":
        last = parse(st.get("last_run"))
        return last + timedelta(minutes=s["minutes"]) if last else at
    hh, mm = map(int, s["time"].split(":"))
    cand = at.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if s["type"] == "daily":
        return cand if cand > at else cand + timedelta(days=1)
    cand += timedelta(days=(DAYS.index(s["day"]) - cand.weekday()) % 7)
    return cand if cand > at else cand + timedelta(days=7)


def is_running(st, at):
    since = parse(st.get("running_since"))
    return bool(since) and (at - since).total_seconds() < 600


def is_due(task, st, at):
    if not task.get("enabled") or not task.get("script") or is_running(st, at):
        return False
    s = task["schedule"]
    nxt = next_run(task, st, at)
    if s["type"] == "interval":
        return at >= nxt
    prev = nxt - timedelta(days=1 if s["type"] == "daily" else 7)
    marks = [m for m in (parse(st.get("last_run")), parse(st.get("enabled_at"))) if m]
    return prev <= at and (not marks or prev > max(marks))


def _secrets():
    vals = []
    env = ROOT / "threads_agent" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if v and any(w in k.upper() for w in ("TOKEN", "SECRET", "KEY")):
                    vals.append(v)
    return vals


def run_task(task, trigger):
    """Run one task's script, record the result in state.json."""
    tid = task["id"]
    state = load_json(STATE_FILE, {})
    state.setdefault(tid, {})["running_since"] = now().isoformat()
    save_json(STATE_FILE, state)

    script = ROOT / task["script"]
    exe = sys.executable.replace("pythonw.exe", "python.exe")
    started = time.time()
    try:
        p = subprocess.run(
            [exe, str(script), *task.get("args", [])],
            cwd=str(script.parent), capture_output=True, text=True,
            timeout=task.get("timeout", 120),
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        ok, out = p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        ok, out = False, "Timed out"
    except Exception as e:  # script missing, python missing, etc.
        ok, out = False, f"{type(e).__name__}: {e}"
    for secret in _secrets():
        out = out.replace(secret, "***")

    state = load_json(STATE_FILE, {})
    t = state.setdefault(tid, {})
    t.pop("running_since", None)
    stamp = now().isoformat()
    t.update(last_run=stamp, last_ok=ok, last_output=out.strip()[-1500:],
             last_seconds=round(time.time() - started, 1), last_trigger=trigger)
    hist = t.setdefault("history", [])
    hist.append({"at": stamp, "ok": ok, "trigger": trigger})
    t["history"] = hist[-20:]
    save_json(STATE_FILE, state)
    return ok
