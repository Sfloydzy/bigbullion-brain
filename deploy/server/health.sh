#!/usr/bin/env bash
# Logs memory, swap, disk and each brand's runner heartbeat to the journal.
# Exits non-zero if something is wrong, so `systemctl status brand-health` turns red.
problems=0
mem_avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
swap_used=$(awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {print int((t-f)/1024)}' /proc/meminfo)
disk_pct=$(df --output=pcent / | tail -1 | tr -dc 0-9)
echo "mem_available=${mem_avail}MB swap_used=${swap_used}MB disk_used=${disk_pct}%"
[ "$mem_avail" -lt 150 ] && { echo "WARN low memory"; problems=1; }
[ "$disk_pct" -gt 85 ] && { echo "WARN disk above 85%"; problems=1; }
for d in /srv/brands/*/; do
  [ -d "$d" ] || continue
  n=$(basename "$d")
  hb="$d/dashboard/heartbeat.json"
  if ! systemctl is-enabled --quiet "brand-runner@$n.timer" 2>/dev/null; then echo "$n: timer not enabled"; continue; fi
  age=$(python3 - "$hb" <<'PY'
import json, sys, datetime
try:
    t = datetime.datetime.fromisoformat(json.load(open(sys.argv[1]))["last_tick"])
    print(int((datetime.datetime.now(t.tzinfo) - t).total_seconds()))
except Exception:
    print(-1)
PY
)
  if [ "$age" -lt 0 ] || [ "$age" -gt 300 ]; then echo "WARN $n: runner heartbeat stale (${age}s)"; problems=1; else echo "$n: ok (heartbeat ${age}s ago)"; fi
done
exit $problems
