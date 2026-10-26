"""Real trailing returns for a named ETF universe, from Polygon daily bars.

Feeds the engine's ETF list experiment so every number in those posts is real.
Price return only (split-adjusted closes, dividends not included).
The free Polygon plan covers about 2 years, so the windows are 1 year and 2 years.

Run:  python etf_facts.py          (refreshes if older than 2 days)
      python etf_facts.py --force
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from threads_common import load_env

HERE = Path(__file__).parent
OUT = HERE / "etf_facts.json"
UNIVERSE = [("QQQ", "Nasdaq 100"), ("VGT", "US tech sector"), ("SMH", "semiconductors"), ("SCHD", "US dividend growth"),
            ("VYM", "US high dividend"), ("XLV", "US healthcare"), ("XLE", "US energy"), ("VTI", "total US market")]
BENCH = ("SPY", "S&P 500")
PACE = 12.5  # free plan allows 5 calls a minute
WINDOWS = {"1y": 365, "2y": 722}


def fetch(ticker, key):
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=735)
    url = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
           f"?adjusted=true&sort=asc&limit=50000&apiKey={key}")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read().decode())
            bars = [(datetime.fromtimestamp(b["t"] / 1000, timezone.utc).date(), b["c"]) for b in d.get("results", [])]
            if len(bars) < 300:
                raise RuntimeError(f"{ticker}: only {len(bars)} bars returned")
            return bars
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(25)
                continue
            raise RuntimeError(f"{ticker}: Polygon HTTP {e.code} {e.read().decode()[:120]}")
    raise RuntimeError(f"{ticker}: rate limited")


def close_on_or_before(bars, target):
    best = None
    for d, c in bars:
        if d <= target:
            best = (d, c)
    if not best or (target - best[0]).days > 5:
        return None
    return best


def window_return(bars, days):
    end_d, end_c = bars[-1]
    s = close_on_or_before(bars, end_d - timedelta(days=days))
    if not s:
        return None
    return {"from": s[0].isoformat(), "to": end_d.isoformat(), "ret": end_c / s[1] - 1, "growth": 1000 * end_c / s[1]}


def pct(x):
    return f"{x * 100:+.1f}%"


def build(key):
    rows, bench = [], None
    for i, (t, name) in enumerate([BENCH] + UNIVERSE):
        if i:
            time.sleep(PACE)
        print(f"fetching {t} ({i + 1}/{len(UNIVERSE) + 1})", flush=True)
        bars = fetch(t, key)
        w = {k: window_return(bars, d) for k, d in WINDOWS.items()}
        rec = {"ticker": t, "name": name, "windows": w}
        if i == 0:
            bench = rec
        else:
            rows.append(rec)
    lines, nums = [], set()
    for k, label in (("1y", "over the past 12 months"), ("2y", "over the past 24 months")):
        b = bench["windows"][k]
        if not b:
            continue
        for r in rows:
            w = r["windows"][k]
            if not w:
                continue
            r.setdefault("beat", {})[k] = w["ret"] > b["ret"]
            lines.append(f"{r['ticker']} ({r['name']}): {pct(w['ret'])} {label}, $1,000 became ${w['growth']:,.0f}. "
                         f"S&P 500 (SPY): {pct(b['ret'])}, $1,000 became ${b['growth']:,.0f}. "
                         f"{'Beat' if w['ret'] > b['ret'] else 'Lagged'} the S&P 500.")
            for v in (pct(w["ret"]), pct(b["ret"]), f"{w['growth']:,.0f}", f"{b['growth']:,.0f}"):
                nums.add(v)
    first = bench["windows"]["1y"]
    out = {"as_of": first["to"], "fetched_at": datetime.now(timezone.utc).isoformat(),
           "source": "Polygon daily closes. Split-adjusted price return, dividends not included.",
           "benchmark": bench, "etfs": rows, "lines": lines, "numbers": sorted(nums)}
    OUT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def ensure_fresh(max_age_days=2, force=False):
    if OUT.exists() and not force:
        d = json.loads(OUT.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(d["fetched_at"])).days
        if age < max_age_days:
            return d
    key = load_env().get("POLYGON_API_KEY")
    if not key:
        raise RuntimeError("POLYGON_API_KEY missing from .env")
    return build(key)


def norm(tok):
    return tok.replace(",", "").replace("%", "").lstrip("+-").rstrip(".")


TICKER_RE = r"\b(QQQ|VGT|SMH|SCHD|VYM|XLV|XLE|VTI|VOO|IVV|SPY|XLK|ARKK|VXUS)\b"
SAFE = {"1", "2", "3", "4", "5", "6", "12", "24", "500", "100", "1000"}


def facts_ok(text, facts):
    """Every number in an ETF post must come from the fact sheet. Tickers must be in the universe."""
    allowed = {norm(n) for n in facts["numbers"]} | SAFE | {str(y) for y in range(2023, 2028)}
    body = text.replace(facts["as_of"], "")
    months = {"Oct 2025", "Oct 2024"}
    for tok in re.findall(r"\d[\d,]*\.?\d*", body):
        if norm(tok) not in allowed:
            return False, f"number {tok} not in fact sheet"
    known = {r["ticker"] for r in facts["etfs"]} | {"SPY"}
    for t in re.findall(r"\b[A-Z]{3,4}\b", text):
        if t in {"ETF", "ETFS", "SPY", "USD", "CPI", "GDP", "AI"} or t in known:
            continue
        if t in {"THE", "AND", "FOR", "BUT", "NOT", "ALL", "TOP", "NEW", "WHY", "HOW", "YOU", "ONE"}:
            continue
        return False, f"unknown ticker {t}"
    return True, ""


if __name__ == "__main__":
    d = ensure_fresh(force="--force" in sys.argv)
    print(f"\nas of {d['as_of']}, {len(d['lines'])} fact lines. Benchmark 1y {pct(d['benchmark']['windows']['1y']['ret'])}")
    for l in d["lines"]:
        print(" ", l)
