"""Loads brand/brand.json: the one place for everything brand-specific (who, voice, rules, pillars,
keywords, goal, timezone, modules). Every module imports from here so a new brand is one file change."""
import json
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATH = ROOT / "brand" / "brand.json"
B = json.loads(PATH.read_text(encoding="utf-8"))

TZ = timezone(timedelta(hours=float(B["timezone"]["utc_offset_hours"])), B["timezone"].get("label"))
TZ_LABEL = B["timezone"].get("label", "local")
HANDLE_THREADS = B.get("handles", {}).get("threads", "")
HANDLE_X = B.get("handles", {}).get("x", "")
TOPICS = B["topics"]
TOPICS_SHORT = B.get("topics_short", B["topics"])
PILLARS = B["pillars"]                      # {pillar: [threads topic tags]}
KEYWORDS = B.get("research_keywords", [])   # [{"q": ..., "pillar": ...}]
GOAL = B["goal"]


def module(name):
    return bool(B.get("modules", {}).get(name, name == "threads"))  # Threads is on unless a brand turns it off
