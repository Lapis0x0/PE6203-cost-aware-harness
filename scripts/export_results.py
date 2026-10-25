#!/usr/bin/env python3
"""Export per-task results of all official configs into one JSON, the single data source for charts, tables and the report.

Run on the VPS under ~/cost-aware-harness: python3 scripts/export_results.py > results/all_runs.json

Take the last trial per task (consistent with summarize.py), plus these statistics:
- step count (agent turns in the trajectory) and agent time
- how often each component fired in the trajectory (identified by the fixed prefix of harness-injected text)
- harness overhead estimate: the harness makes no extra model calls; all overhead is text injected into the conversation.
  Injected text is resent as context in every later call; estimated at 4 characters ≈ 1 token,
  billed at the input price the first time and at the cache-read price afterwards.
"""
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

# Load notes.py directly by file, bypassing cah_agent/__init__ (which imports litellm)
_spec = importlib.util.spec_from_file_location(
    "cah_notes", Path(__file__).resolve().parent.parent / "harness" / "cah_agent" / "notes.py"
)
_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_notes)
DOMAIN_NOTES, VERIFY_CHECKLIST = _notes.DOMAIN_NOTES, _notes.VERIFY_CHECKLIST

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = [
    "B0-minimax-m3", "H-m3", "A1-m3", "A2-m3", "A3-m3", "A4-m3", "A5-m3",
    "B0-flash", "H-flash", "B1-opus55", "H-opus55",
]
MARKERS = {
    "c2_gate": "Before your solution is graded, verify it independently.",
    "c2_report": "[harness] Automatic check of office files",
    "c2_refuse": "[harness] You confirmed completion, but the automatic file check",
    "c3_time": "[harness] Time check:",
    "c3_repeat": "[harness] You have tried essentially the same thing",
    "c3_stuck": "[harness] The terminal has shown no change",
    "c3_respawn": "[harness] The terminal was still unresponsive",
    "c5_noise": "system noise lines (browser, dbus, font, audio warnings) removed]",
    "c5_repeat": "[previous line repeated",
}


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def last_trial(task_dir: Path):
    trials = []
    for r in task_dir.glob("*/*/result.json"):
        try:
            trials.append((json.loads(r.read_text()), r.parent))
        except (OSError, json.JSONDecodeError):
            pass
    trials.sort(key=lambda t: t[0].get("started_at") or "")
    return trials[-1] if trials else (None, None)


def harness_text_chunks(text: str) -> list[str]:
    """Cut out the harness-injected part of an observation ([harness] lines and the verification checklist)."""
    out = []
    if VERIFY_CHECKLIST[:60] in text:
        out.append(VERIFY_CHECKLIST)
    for line in text.split("\n"):
        if "[harness" in line:
            out.append(line)
    return out


def main():
    subset = json.loads((ROOT / "configs" / "subset-40.json").read_text())["tasks"]
    families = json.loads((ROOT / "configs" / "task_families.json").read_text())
    prices = json.loads((ROOT / "pricing" / "prices.json").read_text())["models"]
    rows = []
    for cfg_name in CONFIGS:
        cfg = json.loads((ROOT / "jobs" / cfg_name / "config.json").read_text())
        p = prices[cfg["price_id"]]
        kw = cfg.get("agent_kwargs", {})
        c4 = bool(kw.get("c4_domain_notes"))
        for task in subset:
            t, tdir = last_trial(ROOT / "jobs" / cfg_name / task)
            a = (t or {}).get("agent_result") or {}
            n_in, n_cache, n_out = a.get("n_input_tokens") or 0, a.get("n_cache_tokens") or 0, a.get("n_output_tokens") or 0
            reward = (((t or {}).get("verifier_result") or {}).get("rewards") or {}).get("reward")
            ae = (t or {}).get("agent_execution") or {}
            agent_sec = (ts(ae.get("finished_at")) - ts(ae.get("started_at"))).total_seconds() if ae.get("finished_at") else None
            cost = (n_in - n_cache) * p["input"] + n_cache * p["cache_read"] + n_out * p["output"]

            steps = 0
            events = {k: 0 for k in MARKERS}
            overhead_tok = 0.0
            overhead_usd = 0.0
            traj = tdir / "agent" / "trajectory.json" if tdir else None
            if traj and traj.exists():
                tj = json.loads(traj.read_text())
                agent_steps = [s for s in tj["steps"] if s.get("source") == "agent"]
                steps = len(agent_steps)
                # The observation of agent turn i appears in every call after call i+1
                injected = []  # (call index of first appearance, token count)
                if c4:
                    injected.append((0, len(DOMAIN_NOTES) / 4))
                for i, s in enumerate(agent_steps):
                    # The observation in the trajectory is stored as a repr string, so newlines are a literal \n
                    obs = str(s.get("observation") or "").replace("\\n", "\n")
                    for k, m in MARKERS.items():
                        events[k] += obs.count(m)
                    for chunk in harness_text_chunks(obs):
                        injected.append((i + 1, len(chunk) / 4))
                for first, tok in injected:
                    resends = max(0, steps - first)
                    if resends == 0:
                        continue
                    overhead_tok += tok * resends
                    overhead_usd += tok * p["input"] + tok * (resends - 1) * p["cache_read"]
            rows.append({
                "config": cfg_name, "task": task, "family": families.get(task, ["?"])[0],
                "reward": reward if reward is not None else 0.0, "graded": reward is not None,
                "exception": ((t or {}).get("exception_info") or {}).get("exception_type") or "",
                "input_tokens": n_in, "cache_tokens": n_cache, "output_tokens": n_out,
                "cost_usd": round(cost, 6), "steps": steps,
                "agent_sec": round(agent_sec, 1) if agent_sec is not None else None,
                "events": events,
                "harness_overhead_tokens": round(overhead_tok),
                "harness_overhead_usd": round(overhead_usd, 6),
            })
    json.dump({"prices": prices, "configs": CONFIGS, "rows": rows}, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
