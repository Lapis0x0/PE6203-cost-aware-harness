#!/usr/bin/env bash
# Run several configs in order and summarize when done. Usually not called directly; start.sh / watchdog.sh run it inside the tmux session harness.
# After an interruption, just rerun it to resume (finished tasks are skipped).
set -uo pipefail
cd ~/cost-aware-harness && mkdir -p results
rm -f results/DONE
for cfg in "$@"; do
  python3 scripts/run_config.py "$cfg"
done
python3 scripts/summarize.py $(for c in "$@"; do python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['name'])" "$c"; done) \
  --csv results/summary.csv | tee results/summary.md
date '+%F %T' > results/DONE
