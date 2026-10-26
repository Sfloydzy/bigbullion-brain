"""Local dashboard for agent scheduled tasks.
Run:  python dashboard.py   then open http://127.0.0.1:8765
Only reachable from this computer."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dashlib import (BEAT_FILE, HERE, STATE_FILE, describe, is_running, load_json,
                     load_tasks, next_run, now, parse, run_task, save_json, save_tasks)

PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))  # one port per brand on a shared server
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def status():
    at = now()
    state = load_json(STATE_FILE, {})
    beat = parse(load_json(BEAT_FILE, {}).get("last_tick"))
    paused = bool(state.get("_paused"))
    tasks = []
    for t in load_tasks():
        st = state.get(t["id"], {})
        built = bool(t.get("script"))
        live = t.get("enabled") and built and not paused
        nxt = next_run(t, st, at) if live else None
        tasks.append({
            "id": t["id"], "name": t["name"], "description": t.get("description", ""),
            "schedule": describe(t["schedule"]), "enabled": bool(t.get("enabled")),
            "built": built, "running": is_running(st, at),
            "last_run": st.get("last_run"), "last_ok": st.get("last_ok"),
            "last_output": st.get("last_output", ""), "last_seconds": st.get("last_seconds"),
            "last_trigger": st.get("last_trigger"),
            "next_run": nxt.isoformat() if nxt else None,
            "history": st.get("history", [])[-10:],
        })
    return {"now": at.isoformat(), "paused": paused,
            "runner_age": (at - beat).total_seconds() if beat else None, "tasks": tasks}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "bad host"})
        if self.path == "/api/status":
            return self.send(200, status())
        if self.path in ("/", "/index.html"):
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-Dash") != "1":
            return self.send(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self.send(400, {"error": "bad json"})
        if self.path == "/api/pause":
            state = load_json(STATE_FILE, {})
            state["_paused"] = bool(body.get("paused"))
            save_json(STATE_FILE, state)
            return self.send(200, {"ok": True})
        tasks = load_tasks()
        task = next((t for t in tasks if t["id"] == body.get("id")), None)
        if not task:
            return self.send(404, {"error": "unknown task"})
        if self.path == "/api/toggle":
            if not task.get("script"):
                return self.send(400, {"error": "task not built yet"})
            task["enabled"] = not task.get("enabled")
            save_tasks(tasks)
            state = load_json(STATE_FILE, {})
            st = state.setdefault(task["id"], {})
            if task["enabled"]:
                st["enabled_at"] = now().isoformat()
            save_json(STATE_FILE, state)
            return self.send(200, {"ok": True, "enabled": task["enabled"]})
        if self.path == "/api/run":
            if not task.get("script"):
                return self.send(400, {"error": "task not built yet"})
            if is_running(load_json(STATE_FILE, {}).get(task["id"], {}), now()):
                return self.send(409, {"error": "already running"})
            threading.Thread(target=run_task, args=(task, "manual"), daemon=True).start()
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"Dashboard running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
