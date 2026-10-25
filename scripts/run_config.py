#!/usr/bin/env python3
"""Run one config serially: hand tasks in the subset to harbor one by one, one container at a time.

- Each task is its own harbor job, with results in jobs/<config name>/<task name>/<timestamp>/.
- Tasks that already have a final result are skipped, so rerunning the same command after an interruption resumes the run.
- Environment failures (container did not start, no reward, API connection errors, etc.) are retried automatically;
  timeouts and context overflow are the model's own outcome, are not retried, and count as 0.

Usage: python3 scripts/run_config.py configs/B0-flash.json [--subset configs/subset-40.json] [--only 010-pivot-product-revenue]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARBOR = Path.home() / "cost-aware-harness" / ".venv-harbor" / "bin" / "harbor"
TASKS_DIR = Path.home() / "cost-aware-harness" / "TUA-Bench" / "tasks"
ENV_FILE = Path.home() / "cost-aware-harness" / ".env"

# These exceptions mean the model itself did not finish; they are valid experiment results and are not retried
FINAL_EXCEPTIONS = {
    "AgentTimeoutError",
    "ContextLengthExceededError",
    "OutputLengthExceededError",
    "NonZeroAgentExitCodeError",
    "VerifierTimeoutError",
    "MemoryLimitExceededError",
}
MAX_ATTEMPTS = 3
TASK_WALL_TIMEOUT = 3 * 3600  # Per-task wall-clock limit, to keep harbor from hanging


def load_env_file(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def trial_results(task_dir: Path) -> list[dict]:
    """Return the trial results of all attempts at this task, sorted by time."""
    out = []
    for job in sorted(task_dir.glob("*/")):
        for r in job.glob("*/result.json"):
            try:
                out.append(json.loads(r.read_text()))
            except (OSError, json.JSONDecodeError):
                pass
    return out


def is_final(trial: dict) -> bool:
    if not trial.get("finished_at"):
        return False
    rewards = (trial.get("verifier_result") or {}).get("rewards")
    if rewards:
        return True
    exc = (trial.get("exception_info") or {}).get("exception_type")
    return exc in FINAL_EXCEPTIONS


def log(progress: Path, msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with progress.open("a") as f:
        f.write(line + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--subset", default=str(ROOT / "configs" / "subset-40.json"))
    ap.add_argument("--only", action="append", help="run only the given task (repeatable)")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    tasks = args.only or json.loads(Path(args.subset).read_text())["tasks"]
    out_root = ROOT / "jobs" / cfg["name"]
    out_root.mkdir(parents=True, exist_ok=True)
    progress = out_root / "progress.log"
    (out_root / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1))

    secrets = load_env_file(ENV_FILE)
    api_base = secrets[cfg["api_base_env"]]
    if cfg.get("api_base_strip_v1"):
        api_base = api_base.removesuffix("/v1")
    env = os.environ.copy()
    env[cfg["key_env_name"]] = secrets["ATLASCLOUD_API_KEY"]
    env["PYTHONPATH"] = str(ROOT / "harness") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    log(progress, f"start {cfg['name']}, {len(tasks)} tasks")
    for i, task in enumerate(tasks, 1):
        task_dir = out_root / task
        trials = trial_results(task_dir)
        if trials and is_final(trials[-1]):
            log(progress, f"[{i}/{len(tasks)}] {task} already has a result, skipping")
            continue
        if len(trials) >= MAX_ATTEMPTS:
            log(progress, f"[{i}/{len(tasks)}] {task} tried {len(trials)} times and still an environment failure, giving up (counts as 0)")
            continue

        # Our own harness (harness/cah_agent) is loaded with --agent-import-path; the baseline still uses -a terminus-2
        agent_args = (["--agent-import-path", cfg["agent_import_path"]] if cfg.get("agent_import_path")
                      else ["-a", cfg["agent"]])
        cmd = [
            str(HARBOR), "run", "-p", str(TASKS_DIR), "-i", f"*{task}",
            *agent_args, "-m", cfg["model"], "--ak", f"api_base={api_base}",
            "-e", "docker", "-n", "1", "--delete", "-o", str(task_dir), "--yes",
        ]
        for k, v in cfg.get("agent_kwargs", {}).items():
            cmd += ["--ak", f"{k}={json.dumps(v) if not isinstance(v, str) else v}"]

        t0 = time.time()
        log(progress, f"[{i}/{len(tasks)}] {task} starting (attempt {len(trials) + 1})")
        try:
            subprocess.run(cmd, env=env, cwd=TASKS_DIR.parent, timeout=TASK_WALL_TIMEOUT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            log(progress, f"[{i}/{len(tasks)}] {task} exceeded the wall-clock limit, killing")
            # harbor does not clean up its containers after being killed; remove leftovers by task name
            ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={task.lower()}"],
                                 capture_output=True, text=True).stdout.split()
            if ids:
                subprocess.run(["docker", "rm", "-f", *ids], check=False, capture_output=True)

        trials = trial_results(task_dir)
        last = trials[-1] if trials else {}
        reward = ((last.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        exc = (last.get("exception_info") or {}).get("exception_type")
        status = f"reward={reward}" if reward is not None else f"exception={exc}"
        log(progress, f"[{i}/{len(tasks)}] {task} finished, {status}, took {time.time() - t0:.0f}s")

        # Environment failure: leave it for the next round (just rerun this script); retry once immediately here
        if last and not is_final(last) and len(trials) < MAX_ATTEMPTS:
            log(progress, f"[{i}/{len(tasks)}] {task} environment failure, will retry later")
            time.sleep(30)
            tasks.append(task)

    log(progress, f"{cfg['name']} all done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
