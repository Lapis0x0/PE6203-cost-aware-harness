#!/usr/bin/env python3
"""Compute all metrics for charts, slides and the report from analysis/all_runs.json and write analysis/metrics.json.

Run locally: python3 scripts/compute_metrics.py
"""
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
d = json.loads((ROOT / "analysis" / "all_runs.json").read_text())
prices = d["prices"]
PRICE_ID = {"m3": "minimaxai/minimax-m3", "flash": "deepseek-ai/deepseek-v4.1-flash", "opus": "anthropic/claude-opus-5.5"}
META = {
    "B0-minimax-m3": ("m3", "default", "MiniMax M3 · default"),
    "H-m3": ("m3", "ours", "MiniMax M3 · ours"),
    "A1-m3": ("m3", "C1", "M3 + C1 only"),
    "A2-m3": ("m3", "C2", "M3 + C2 only"),
    "A3-m3": ("m3", "C3", "M3 + C3 only"),
    "A4-m3": ("m3", "C4", "M3 + C4 only"),
    "A5-m3": ("m3", "C5", "M3 + C5 only"),
    "B0-flash": ("flash", "default", "DeepSeek V4.1 Flash · default"),
    "H-flash": ("flash", "ours", "DeepSeek V4.1 Flash · ours"),
    "B1-opus55": ("opus", "default", "Claude Opus 5.5 · default"),
    "H-opus55": ("opus", "ours", "Claude Opus 5.5 · ours"),
}

rows_by_cfg = defaultdict(list)
for r in d["rows"]:
    rows_by_cfg[r["config"]].append(r)

configs = {}
for cfg, rows in rows_by_cfg.items():
    model, variant, label = META[cfg]
    p = prices[PRICE_ID[model]]
    n = len(rows)
    inp = sum(r["input_tokens"] for r in rows)
    cache = sum(r["cache_tokens"] for r in rows)
    out = sum(r["output_tokens"] for r in rows)
    usd_uncached = (inp - cache) * p["input"]
    usd_cache = cache * p["cache_read"]
    usd_out = out * p["output"]
    fam = defaultdict(list)
    for r in rows:
        fam[r["family"]].append(r["reward"])
    configs[cfg] = {
        "model": model, "variant": variant, "label": label, "n": n,
        "success": round(100 * sum(r["reward"] for r in rows) / n, 1),
        "full_solves": sum(1 for r in rows if r["reward"] >= 0.999),
        "cost": round(usd_uncached + usd_cache + usd_out, 2),
        "cost_split": {"uncached_input": round(usd_uncached, 2), "cache_read": round(usd_cache, 2), "output": round(usd_out, 2)},
        "tokens": {"input": inp, "cache": cache, "output": out},
        "steps": sum(r["steps"] for r in rows),
        "agent_minutes": round(sum(r["agent_sec"] or 0 for r in rows) / 60),
        "harness_overhead_tokens": sum(r["harness_overhead_tokens"] for r in rows),
        "harness_overhead_usd": round(sum(r["harness_overhead_usd"] for r in rows), 3),
        "harness_overhead_share_of_input": round(100 * sum(r["harness_overhead_tokens"] for r in rows) / inp, 2) if inp else 0,
        "by_family": {f: {"n": len(v), "success": round(100 * sum(v) / len(v), 1)} for f, v in sorted(fam.items())},
        "tasks_with_event": {
            k: sum(1 for r in rows if r["events"].get(k, 0) > 0)
            for k in ["c2_gate", "c2_report", "c3_time", "c3_repeat", "c3_stuck", "c3_respawn", "c5_noise", "c5_repeat"]
        },
    }

# Pareto frontier: cost ascending, success rate strictly increasing
pts = sorted(((c["cost"], c["success"], k) for k, c in configs.items()))
frontier, best = [], -1
for cost, sr, k in pts:
    if sr > best:
        frontier.append(k)
        best = sr

# Per-task flips (relative to each model's default)
R = {(r["config"], r["task"]): r["reward"] for r in d["rows"]}
tasks = [r["task"] for r in rows_by_cfg["H-m3"]]
base_of = {"m3": "B0-minimax-m3", "flash": "B0-flash", "opus": "B1-opus55"}
flips = {}
for cfg, c in configs.items():
    if c["variant"] == "default":
        continue
    b = base_of[c["model"]]
    flips[cfg] = {
        "gained": [t for t in tasks if R[(cfg, t)] - R[(b, t)] > 0.3],
        "lost": [t for t in tasks if R[(b, t)] - R[(cfg, t)] > 0.3],
    }

# For each task the stock M3 failed, how many single-component configs rescue it
rescued_by = {t: [c for c in ["A1-m3", "A2-m3", "A3-m3", "A4-m3", "A5-m3"] if t in flips[c]["gained"]] for t in tasks}

per_task = {t: {cfg: R[(cfg, t)] for cfg in configs} for t in tasks}
per_task_tokens = {
    t: {cfg: next(r["input_tokens"] for r in rows_by_cfg[cfg] if r["task"] == t) for cfg in ["B1-opus55", "H-opus55"]}
    for t in tasks
}

out = {
    "subset_size": len(tasks),
    "configs": configs,
    "pareto_frontier": frontier,
    "flips": flips,
    "rescued_by_single_component": {t: v for t, v in rescued_by.items() if v},
    "per_task": per_task,
    "opus_input_tokens_per_task": per_task_tokens,
    "prices_per_mtok": {m: {k: round(v * 1e6, 3) for k, v in prices[pid].items()} for m, pid in PRICE_ID.items()},
}
(ROOT / "analysis" / "metrics.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
for k, c in configs.items():
    print(f'{k:14} {c["success"]:5.1f}%  ${c["cost"]:6.2f}  split={c["cost_split"]}  steps={c["steps"]}  oh=${c["harness_overhead_usd"]} ({c["harness_overhead_share_of_input"]}%)  fam={ {f: v["success"] for f, v in c["by_family"].items()} }')
print("frontier", frontier)
print("prices", out["prices_per_mtok"])
