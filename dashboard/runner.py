"""One-minute tick. Windows Task Scheduler runs this every minute.
Runs any enabled task that is due, unless the dashboard has paused everything."""
from dashlib import (BEAT_FILE, STATE_FILE, is_due, load_json, load_tasks,
                     now, run_task, save_json)


def main():
    at = now()
    state = load_json(STATE_FILE, {})
    save_json(BEAT_FILE, {"last_tick": at.isoformat()})
    if state.get("_paused"):
        return
    changed = False
    for task in load_tasks():
        st = state.setdefault(task["id"], {})
        if task.get("enabled") and not st.get("enabled_at") and not st.get("last_run"):
            st["enabled_at"] = at.isoformat()
            changed = True
    if changed:
        save_json(STATE_FILE, state)
    for task in load_tasks():
        st = load_json(STATE_FILE, {}).get(task["id"], {})
        if is_due(task, st, now()):
            run_task(task, "schedule")


if __name__ == "__main__":
    main()
