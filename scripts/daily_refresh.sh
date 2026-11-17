#!/usr/bin/env bash
# Daily urai-router maintenance: re-probe keys, refresh model catalogues and
# snapshot paid balances. Keeps free/rotating credentials usable without
# manual intervention. Does not spend tokens (probes use authenticated /models).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOG="${XDG_STATE_HOME:-$HOME/.local/state}/urai-router/daily-refresh.log"
mkdir -p "$(dirname "$LOG")"

{
  echo "=== $(date -Is) daily refresh ==="
  echo "-- key probes --"
  python3 -m urai_router keys check 2>&1 | tail -12
  echo "-- catalogue sync --"
  python3 -m urai_router sync 2>&1 | tail -8
  echo "-- paid balances --"
  python3 -m urai_router keys balance --provider bai 2>&1 | tail -5
} >> "$LOG" 2>&1

tail -n 250 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
echo "daily refresh complete: $LOG"
