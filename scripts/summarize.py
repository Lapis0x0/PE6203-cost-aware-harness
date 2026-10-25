#!/usr/bin/env python3
"""Summarize results of one or more configs: success rate, tokens, cost from the price table, per task family.

Cost formula: uncached input x input price + cache reads x cache_read price + output x output price.
harbor's n_input_tokens already includes the cached part, so the uncached part = n_input_tokens - n_cache_tokens.
Tasks with no final result (environment failures after retries are exhausted, or not yet run) count as 0 and are listed separately.

Usage: python3 scripts/summarize.py B0-flash B1-opus [--csv results/summary.csv]
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def last_trial(task_dir: Path) -> dict | None:
    trials = []
    for r in task_dir.glob("*/*/result.json"):
        try:
            trials.append(json.loads(r.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    trials.sort(key=lambda t: t.get("started_at") or "")
    return trials[-1] if trials else None


def summarize(name: str, subset: list[str], families: dict, prices: dict) -> tuple[dict, list[dict]]:
    cfg = json.loads((ROOT / "jobs" / name / "config.json").read_text())
    p = prices["models"][cfg["price_id"]]
    rows = []
    for task in subset:
        t = last_trial(ROOT / "jobs" / name / task)
        a = (t or {}).get("agent_result") or {}
        n_in, n_cache, n_out = a.get("n_input_tokens") or 0, a.get("n_cache_tokens") or 0, a.get("n_output_tokens") or 0
        reward = (((t or {}).get("verifier_result") or {}).get("rewards") or {}).get("reward")
        exc = ((t or {}).get("exception_info") or {}).get("exception_type")
        cost = (n_in - n_cache) * p["input"] + n_cache * p["cache_read"] + n_out * p["output"]
        rows.append({
            "config": name, "task": task, "family": families.get(task, ["?"])[0],
            "reward": reward if reward is not None else 0.0, "graded": reward is not None,
            "exception": exc or "", "input_tokens": n_in, "cache_tokens": n_cache,
            "output_tokens": n_out, "cost_usd": round(cost, 5), "ran": t is not None,
        })
    n = len(rows)
    agg = {
        "config": name,
        "tasks": n,
        "ran": sum(r["ran"] for r in rows),
        "success_rate": sum(r["reward"] for r in rows) / n if n else 0,
        "cost_usd": sum(r["cost_usd"] for r in rows),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "cache_tokens": sum(r["cache_tokens"] for r in rows),
        "output_tokens": sum(r["output_tokens"] for r in rows),
        "ungraded": [r["task"] for r in rows if r["ran"] and not r["graded"]],
    }
    fam = defaultdict(list)
    for r in rows:
        fam[r["family"]].append(r["reward"])
    agg["by_family"] = {f: (sum(v) / len(v), len(v)) for f, v in sorted(fam.items())}
    return agg, rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("configs", nargs="+")
    ap.add_argument("--subset", default=str(ROOT / "configs" / "subset-40.json"))
    ap.add_argument("--csv")
    args = ap.parse_args()

    subset = json.loads(Path(args.subset).read_text())["tasks"]
    families = json.loads((ROOT / "configs" / "task_families.json").read_text())
    prices = json.loads((ROOT / "pricing" / "prices.json").read_text())

    all_rows = []
    print(f"| Config | Run | Success rate | Cost (USD) | Input / of which cached / output tokens |")
    print("|---|---|---|---|---|")
    aggs = []
    for name in args.configs:
        agg, rows = summarize(name, subset, families, prices)
        aggs.append(agg)
        all_rows += rows
        print(f"| {name} | {agg['ran']}/{agg['tasks']} | {agg['success_rate']:.1%} | {agg['cost_usd']:.2f} | "
              f"{agg['input_tokens']:,} / {agg['cache_tokens']:,} / {agg['output_tokens']:,} |")
    for agg in aggs:
        print(f"\n{agg['config']} by task family: " + "; ".join(f"{f} {s:.0%} ({n} tasks)" for f, (s, n) in agg["by_family"].items()))
        if agg["ungraded"]:
            print(f"{agg['config']} tasks with no score, counted as 0: {', '.join(agg['ungraded'])}")

    if args.csv:
        Path(args.csv).parent.mkdir(parents=True, exist_ok=True)
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)


if __name__ == "__main__":
    main()
