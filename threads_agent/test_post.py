"""Threads API connection test.

1. Reads credentials from .env in the same folder.
2. Verifies the token by calling /me.
3. Asks for confirmation, then publishes one text post.

Run:  python test_post.py
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://graph.threads.net/v1.0"


def load_env(path):
    env = {}
    if not path.exists():
        sys.exit(f"Could not find {path}. Put test_post.py in the same folder as .env")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def call(method, url, params):
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        req = urllib.request.Request(f"{url}?{data.decode()}")
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        sys.exit(f"API error {e.code}: {body}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="Testing my Threads API connection.")
    ap.add_argument("--tag", default=None, help="Topic tag, e.g. AI")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = ap.parse_args()
    env = load_env(Path(__file__).parent / ".env")
    token = env.get("THREADS_ACCESS_TOKEN")
    user_id = env.get("THREADS_USER_ID")
    if not token or not user_id:
        sys.exit("THREADS_ACCESS_TOKEN or THREADS_USER_ID is missing from .env")

    me = call("GET", f"{BASE}/me", {"fields": "id,username", "access_token": token})
    print(f"Token OK. Logged in as @{me.get('username')} (id {me.get('id')})")
    if str(me.get("id")) != str(user_id):
        sys.exit("THREADS_USER_ID in .env does not match the id returned by the API. Fix it and rerun.")

    text = args.text
    answer = "yes" if args.yes else input(f'Publish this test post to @{me.get("username")}?\n"{text}"\nType yes to post: ')
    if answer.strip().lower() != "yes":
        print("Cancelled. Nothing was posted.")
        return

    params = {"media_type": "TEXT", "text": text, "access_token": token}
    if args.tag:
        params["topic_tag"] = args.tag
    container = call("POST", f"{BASE}/{user_id}/threads", params)
    creation_id = container["id"]
    print(f"Container created: {creation_id}. Waiting a few seconds...")
    time.sleep(5)

    published = call(
        "POST",
        f"{BASE}/{user_id}/threads_publish",
        {"creation_id": creation_id, "access_token": token},
    )
    print(f"Published. Post id: {published.get('id')}")
    print("Check your Threads profile, then delete the test post if you like.")


if __name__ == "__main__":
    main()
