#!/usr/bin/env bash
# Start a set of configs: record the queue and run it in tmux session harness. The watchdog resumes from results/queue.args.
#   bash scripts/start.sh configs/B0-flash.json configs/B1-opus55.json
set -euo pipefail
cd ~/cost-aware-harness && mkdir -p results
if tmux has-session -t harness 2>/dev/null; then echo "tmux session harness is already running"; exit 1; fi
printf '%s\n' "$@" > results/queue.args
rm -f results/DONE
tmux new-session -d -s harness "bash scripts/run_queue.sh $* 2>&1 | tee -a results/queue.log"
echo "started: $*"
