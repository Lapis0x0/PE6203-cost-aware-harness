#!/usr/bin/env bash
# Unattended pipeline for phase three, run in the tmux session pipeline; does not depend on the local machine being online:
#   1. Wait for the current queue (results/DONE) to finish
#   2. Gate: M3 baseline success rate must be below 55%
#   3. Smoke: run only 4 tasks of H-m3; the results count directly toward H-m3
#   4. Gate: no harness-side exceptions in the smoke run
#   5. Full experiments: H-m3 (resume the remaining 36 tasks), A1 to A5, H-flash, H-opus55, handed to start.sh; the watchdog resumes as usual
# Any failing step writes results/HALT and stops. To restart: fix the problem, delete results/HALT, then rerun this script.
#   tmux new-session -d -s pipeline "bash scripts/pipeline.sh 2>&1 | tee -a results/pipeline.log"
set -uo pipefail
cd ~/cost-aware-harness
LOG=results/pipeline.log
say() { echo "$(date '+%F %T') $*"; }
SMOKE_TASKS="018-calculate-total-earnings 111-set-video-wallpaper 059-make-summary-slide 045-united-bag-fee-page"
FULL="configs/H-m3.json configs/A1-m3.json configs/A2-m3.json configs/A3-m3.json configs/A4-m3.json configs/A5-m3.json configs/H-flash.json configs/H-opus55.json"

[ -f results/HALT ] && { say "results/HALT exists, handle it first: $(cat results/HALT)"; exit 1; }

wait_done() {
  while [ ! -f results/DONE ] || tmux has-session -t harness 2>/dev/null; do sleep 300; done
}

say "waiting for the current queue to finish"
wait_done
say "current queue finished"

if [ ! -f results/.gate_weak_ok ]; then
  python3 scripts/gate.py weak B0-minimax-m3 0.55 || exit 1
  touch results/.gate_weak_ok
fi

if [ ! -f results/.gate_smoke_ok ]; then
  say "smoke: running H-m3 on $SMOKE_TASKS"
  only=""; for t in $SMOKE_TASKS; do only="$only --only $t"; done
  python3 scripts/run_config.py configs/H-m3.json $only
  python3 scripts/gate.py smoke H-m3 $SMOKE_TASKS || exit 1
  touch results/.gate_smoke_ok
fi

say "starting full experiments: $FULL"
bash scripts/start.sh $FULL || exit 1
sleep 60
wait_done
say "all full experiments finished, summary in results/summary.md"
