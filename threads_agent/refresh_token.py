"""Refreshes the long-lived Threads token (valid 60 days) and saves it to .env.
Tokens must be at least 24 hours old and not yet expired to refresh."""
import sys
from datetime import datetime, timedelta, timezone

from threads_common import HERE, load_env, threads_call

# refresh endpoint lives at graph.threads.net/refresh_access_token (no version prefix)
import json
import urllib.error
import urllib.parse
import urllib.request


def main():
    env = load_env()
    token = env.get("THREADS_ACCESS_TOKEN")
    if not token:
        sys.exit("THREADS_ACCESS_TOKEN missing from .env")
    qs = urllib.parse.urlencode({"grant_type": "th_refresh_token", "access_token": token})
    try:
        with urllib.request.urlopen(f"https://graph.threads.net/refresh_access_token?{qs}", timeout=30) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"Refresh failed {e.code}: {e.read().decode()}")
    new = data["access_token"]
    path = HERE / ".env"
    lines = path.read_text(encoding="utf-8").splitlines()
    out, done = [], False
    for line in lines:
        if line.strip().startswith("THREADS_ACCESS_TOKEN="):
            out.append(f"THREADS_ACCESS_TOKEN={new}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"THREADS_ACCESS_TOKEN={new}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    days = data.get("expires_in", 0) // 86400
    expiry = datetime.now(__import__("brandkit").TZ) + timedelta(seconds=data.get("expires_in", 0))
    print(f"Token refreshed. Valid for about {days} days (until {expiry:%Y-%m-%d}).")


if __name__ == "__main__":
    main()
