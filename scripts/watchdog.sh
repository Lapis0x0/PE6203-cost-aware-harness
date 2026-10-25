#!/usr/bin/env bash
# Watchdog: called by cron every 10 minutes (and at boot).
# - Queue unfinished but tmux session harness is gone (process crash, VPS reboot) -> clean leftover containers and resume
# - Progress log not updated for STALL_MIN minutes (per-task wall-clock limit is 3 hours; this leaves margin) -> treat as hung and restart
# - At most 3 automatic restarts per hour; beyond that, stop and wait for a human
# All actions are logged in results/watchdog.log.
set -uo pipefail
cd ~/cost-aware-harness || exit 0
LOG=results/watchdog.log
STALL_MIN=200
now() { date '+%F %T'; }

[ -f results/queue.args ] || exit 0          # queue never started
[ -f results/DONE ] && exit 0                 # already finished

# --delete per task removes only the image, so the build cache keeps growing (about 1.5 GB/task). When free space drops below 12 GB, prune the cache to 20 GB, keeping the most recently used
free_gb=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
if [ "$free_gb" -lt 12 ]; then
  docker builder prune -f --max-used-space 20GB >/dev/null 2>&1
  echo "$(now) disk free ${free_gb}G, free after pruning build cache: $(df -BG --output=avail / | tail -1 | tr -dc 0-9)G" >> "$LOG"
fi

latest=$(ls -t jobs/*/progress.log 2>/dev/null | head -1)
reason=""
if ! tmux has-session -t harness 2>/dev/null; then
  reason="tmux session is gone"
elif [ -n "$latest" ] && [ $(( ($(date +%s) - $(stat -c %Y "$latest")) / 60 )) -ge $STALL_MIN ]; then
  reason="$latest not updated for ${STALL_MIN} minutes"
fi
[ -z "$reason" ] && exit 0

recent=$(awk -v cutoff="$(date -d '-1 hour' '+%F %T')" '/auto-restart/ && ($1" "$2) >= cutoff' "$LOG" 2>/dev/null | wc -l)
if [ "$recent" -ge 3 ]; then
  grep -q "needs manual handling" <(tail -1 "$LOG" 2>/dev/null) || echo "$(now) auto-restarted ${recent} times within one hour, no more restarts, needs manual handling (reason: $reason)" >> "$LOG"
  exit 0
fi

tmux kill-session -t harness 2>/dev/null
pkill -f "scripts/run_config.py" 2>/dev/null
pkill -f "venv-harbor/bin/harbor run" 2>/dev/null
sleep 5
# Remove only task containers started by harbor (compose project name contains "__"); leave other containers such as fast-note-sync alone
ids=$(docker ps -a --filter "label=com.docker.compose.project" --format '{{.ID}} {{.Label "com.docker.compose.project"}}' | awk '$2 ~ /__/ {print $1}')
[ -n "$ids" ] && docker rm -f $ids >/dev/null 2>&1
args=$(tr '\n' ' ' < results/queue.args)
tmux new-session -d -s harness "bash scripts/run_queue.sh $args 2>&1 | tee -a results/queue.log"
echo "$(now) auto-restart (reason: $reason)" >> "$LOG"
