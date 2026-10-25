#!/usr/bin/env python3
"""Gate checks in the pipeline: on failure, write results/HALT and exit non-zero so the pipeline stops and waits for a human.

  python3 scripts/gate.py weak B0-minimax-m3 0.55
      The weak-model baseline success rate must be below the threshold; otherwise it is not weak enough and ablations on top of it are meaningless.
  python3 scripts/gate.py smoke H-m3 018-calculate-total-earnings 111-set-video-wallpaper ...
      Every smoke task must have a final result with no harness-side exception (a wrong answer from the model does not count), and the agent must take at least 2 steps.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from run_config import FINAL_EXCEPTIONS  # noqa: E402


def last_trial(task_dir: Path):
    trials = []
    for r in task_dir.glob("*/*/result.json"):
        try:
            trials.append(json.loads(r.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    trials.sort(key=lambda t: t.get("started_at") or "")
    return trials[-1] if trials else None


def halt(reason: str) -> None:
    line = f"{datetime.now():%F %T} {reason}"
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "HALT").write_text(line + "\n")
    print("HALT:", line)
    sys.exit(1)


def weak(name: str, threshold: float) -> None:
    subset = json.loads((ROOT / "configs" / "subset-40.json").read_text())["tasks"]
    rewards = []
    for task in subset:
        t = last_trial(ROOT / "jobs" / name / task)
        r = (((t or {}).get("verifier_result") or {}).get("rewards") or {}).get("reward")
        rewards.append(r or 0.0)
    rate = sum(rewards) / len(subset)
    print(f"{name}: success rate {rate:.1%} over {len(subset)} tasks (threshold {threshold:.0%})")
    if rate >= threshold:
        halt(f"{name} success rate {rate:.1%} is not below {threshold:.0%}, so it is not weak enough; skip the M3 ablations for now and consider switching to MiniMax M2.5")


def smoke(name: str, tasks: list[str]) -> None:
    problems = []
    for task in tasks:
        t = last_trial(ROOT / "jobs" / name / task)
        if t is None:
            problems.append(f"{task}: no result")
            continue
        exc = (t.get("exception_info") or {}).get("exception_type")
        rewards = (t.get("verifier_result") or {}).get("rewards")
        steps = (t.get("agent_result") or {}).get("metadata", {}) or {}
        n_episodes = steps.get("n_episodes")
        if exc and exc not in FINAL_EXCEPTIONS:
            msg = ((t.get("exception_info") or {}).get("exception_message") or "")[:200]
            problems.append(f"{task}: harness or environment exception {exc}: {msg}")
        elif not rewards and exc not in FINAL_EXCEPTIONS:
            problems.append(f"{task}: no reward obtained")
        if n_episodes is not None and n_episodes < 2:
            problems.append(f"{task}: agent took only {n_episodes} steps")
        print(f"{task}: reward={rewards} exc={exc} episodes={n_episodes}")
    if problems:
        halt(f"{name} smoke test failed: " + "; ".join(problems))
    print(f"{name} smoke test passed")


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "weak":
        weak(sys.argv[2], float(sys.argv[3]))
    elif mode == "smoke":
        smoke(sys.argv[2], sys.argv[3:])
    else:
        sys.exit(f"unknown mode {mode}")
