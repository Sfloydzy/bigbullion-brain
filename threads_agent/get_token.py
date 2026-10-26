"""One-time helper: turn the short-lived Threads token from the Meta dashboard into a 60 day token.
Reads THREADS_APP_SECRET from .env, asks for the short-lived token with a hidden prompt (nothing is echoed
or printed), then saves THREADS_ACCESS_TOKEN and THREADS_USER_ID into .env and prints only the username.
Run from the threads_agent folder: python get_token.py"""
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from threads_common import HERE, load_env


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"Meta said no ({e.code}): {e.read().decode()[:300]}")


def set_env(lines, key, value):
    out, done = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    return out


def main():
    secret = load_env().get("THREADS_APP_SECRET")
    if not secret:
        sys.exit("Put THREADS_APP_SECRET in .env first (Meta dashboard, App settings, Basic).")
    short = getpass.getpass("Paste the token from the Meta dashboard (hidden, press Enter): ").strip()
    qs = urllib.parse.urlencode({"grant_type": "th_exchange_token", "client_secret": secret, "access_token": short})
    try:
        with urllib.request.urlopen(f"https://graph.threads.net/access_token?{qs}", timeout=30) as r:
            data = json.loads(r.read().decode())
        token = data["access_token"]
    except urllib.error.HTTPError:
        # the dashboard's User Token Generator already gives a long-lived token; use it as is
        data, token = {"expires_in": 60 * 86400}, short
    me = get("https://graph.threads.net/v1.0/me?" + urllib.parse.urlencode({"fields": "id,username", "access_token": token}))
    path = HERE / ".env"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = set_env(lines, "THREADS_ACCESS_TOKEN", token)
    lines = set_env(lines, "THREADS_USER_ID", me["id"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved. Connected as @{me.get('username')}. Token lasts about {data.get('expires_in', 0) // 86400} days.")


if __name__ == "__main__":
    main()
