"""Minimal X API v2 client using only the standard library.

Posting needs OAuth 1.0a user keys: X_CONSUMER_KEY, X_SECRET_KEY, X_ACCESS_TOKEN, X_ACCESS_SECRET.
Reading (search, metrics) uses the same user keys when present, else X_BEARER_TOKEN.
Every read is counted so the engine can cap spend under pay-per-use pricing.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.x.com/2"


def enc(s):
    return urllib.parse.quote(str(s), safe="~-._")


class XError(RuntimeError):
    def __init__(self, code, body):
        super().__init__(f"X API {code}: {body[:300]}")
        self.code = code
        self.body = body


class X:
    def __init__(self, env):
        up = {k.upper(): v for k, v in env.items() if v}
        g = lambda *names: next((up[n] for n in names if up.get(n)), None)
        self.ck = g("X_CONSUMER_KEY", "X_API_KEY")
        self.cs = g("X_SECRET_KEY", "X_API_SECRET", "X_CONSUMER_SECRET")
        self.at = g("X_ACCESS_TOKEN")
        self.ats = g("X_ACCESS_SECRET", "X_ACCESS_TOKEN_SECRET")
        self.bearer = g("X_BEARER_TOKEN")
        self.reads = 0

    @property
    def can_post(self):
        return all([self.ck, self.cs, self.at, self.ats])

    @property
    def can_read(self):
        return self.can_post or bool(self.bearer)

    def _oauth_header(self, method, url, query):
        oauth = {"oauth_consumer_key": self.ck, "oauth_nonce": secrets.token_hex(16), "oauth_signature_method": "HMAC-SHA1",
                 "oauth_timestamp": str(int(time.time())), "oauth_token": self.at, "oauth_version": "1.0"}
        pairs = sorted((enc(k), enc(v)) for k, v in {**query, **oauth}.items())
        base = "&".join([method.upper(), enc(url), enc("&".join(f"{k}={v}" for k, v in pairs))])
        key = f"{enc(self.cs)}&{enc(self.ats)}".encode()
        oauth["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
        return "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(oauth.items()))

    def request(self, method, path, query=None, body=None, user=None):
        query = {k: str(v) for k, v in (query or {}).items()}
        url = API + path
        full = url + ("?" + "&".join(f"{enc(k)}={enc(v)}" for k, v in query.items()) if query else "")
        user = self.can_post if user is None else user
        if user:
            if not self.can_post:
                raise XError("auth", "X_ACCESS_TOKEN and X_ACCESS_SECRET are missing from .env")
            auth = self._oauth_header(method, url, query)
        else:
            if not self.bearer:
                raise XError("auth", "X_BEARER_TOKEN is missing from .env")
            auth = "Bearer " + self.bearer
        headers = {"Authorization": auth, "User-Agent": "scott-x-agent/1.0"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(full, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise XError(e.code, e.read().decode(errors="replace")) from None

    # ------------------------------------------------------------ endpoints
    def me(self):
        return self.request("GET", "/users/me", {"user.fields": "public_metrics,username"}, user=True)["data"]

    def post(self, text):
        return self.request("POST", "/tweets", body={"text": text}, user=True)["data"]

    def tweets(self, ids):
        out = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            r = self.request("GET", "/tweets", {"ids": ",".join(chunk), "tweet.fields": "public_metrics,created_at"})
            out += r.get("data", [])
            self.reads += len(r.get("data", []))
        return out

    def search(self, query, n=20):
        r = self.request("GET", "/tweets/search/recent", {
            "query": query, "max_results": max(10, min(n, 100)), "sort_order": "relevancy",
            "tweet.fields": "public_metrics,created_at,author_id,lang", "expansions": "author_id",
            "user.fields": "username,public_metrics"}, user=False if self.bearer else None)
        users = {u["id"]: u for u in r.get("includes", {}).get("users", [])}
        data = r.get("data", [])
        self.reads += len(data)
        for t in data:
            t["author"] = users.get(t.get("author_id"), {})
        return data

    def users_by(self, usernames):
        out = []
        for i in range(0, len(usernames), 100):
            r = self.request("GET", "/users/by", {"usernames": ",".join(usernames[i:i + 100]), "user.fields": "public_metrics"}, user=False if self.bearer else None)
            out += r.get("data", [])
            self.reads += len(r.get("data", []))
        return out

    def user_tweets(self, user_id, n=5):
        r = self.request("GET", f"/users/{user_id}/tweets", {"max_results": max(5, min(n, 100)), "exclude": "retweets,replies",
                                                             "tweet.fields": "public_metrics,created_at"}, user=False if self.bearer else None)
        data = r.get("data", [])
        self.reads += len(data)
        return data

