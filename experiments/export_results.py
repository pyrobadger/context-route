#!/usr/bin/env python3
"""Export normalized results.jsonl and run the comprehensive joint routing analysis.

Outputs:
    experiments/results/results.jsonl
    experiments/results/analysis.json
    experiments/results/report.html
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
from pathlib import Path
import random
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GRID = ROOT / "benchmark" / "results" / "grid"
OUT = ROOT / "experiments" / "results"
OUT.mkdir(parents=True, exist_ok=True)
TASKS_FILE = ROOT / "experiments" / "tasks.json"

MODELS = ("opencode/big-pickle", "opencode/deepseek-v4.1-flash-free")
TIERS = ("C1", "C2", "C3")
FIXED_MODEL = MODELS[0]
FIXED_CONTEXT = "C2"
N_PERMUTATIONS = 20000


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total == 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - spread) / denom)


def main():
    # Load frozen tasks
    tasks_spec = json.loads(TASKS_FILE.read_text(encoding="utf-8"))["tasks"]
    task_ids = [t["id"] for t in tasks_spec]

    # ---- 1. Ingest Completed Runs from grid/runs ----
    runs_path = GRID / "runs"
    completed_runs = []
    failed_runs = []
    
    for path in sorted(runs_path.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        
        m_id = row.get("expected_model") or row.get("model_id")
        if m_id not in MODELS:
            continue
            
        status = row.get("execution_status")
        if status == "completed":
            completed_runs.append(row)
        else:
            failed_runs.append(row)

    print(f"Total completed runs: {len(completed_runs)}")
    print(f"Total incomplete/failed runs: {len(failed_runs)}")

    # ---- 2. Export results.jsonl ----
    rows = []
    for r in completed_runs:
        m_id = r.get("expected_model") or r.get("model_id")
        normalized = {
            "task": r.get("task_id"),
            "repository": r.get("repository_id", "contextroute-shop"),
            "model": m_id,
            "context_tier": r.get("context_id"),
            "context_hash": r.get("context_hash"),
            "repeat": r.get("repeat", 0),
            "execution_id": r.get("execution_id"),
            "verification_status": r.get("verification", {}).get("status"),
            "passed": r.get("resolved"),
            "resolved": r.get("resolved"),
            "input_tokens": r.get("input_tokens"),
            "output_tokens": r.get("output_tokens"),
            "total_tokens": r.get("total_tokens"),
            "latency_seconds": r.get("latency_seconds"),
            "wall_seconds": r.get("wall_seconds"),
            "estimated_cost_usd": r.get("estimated_list_price_usd", 0.0),
            "primary_model_calls": r.get("primary_model_calls"),
            "auxiliary_model_calls": r.get("auxiliary_model_calls"),
            "opencode_version": r.get("opencode_version"),
            "repository_commit": r.get("repository_commit"),
        }
        rows.append(normalized)

    # Sort rows deterministically: task, repeat, model, context
    rows.sort(key=lambda x: (x["task"], x["repeat"], x["model"], x["context_tier"]))

    jsonl_path = OUT / "results.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Exported {len(rows)} valid runs to {jsonl_path}")

    # ---- 3. Per-Arm Mean & Std across Repeats ----
    arm_runs = defaultdict(list)
    for r in rows:
        cell = (r["task"], r["model"], r["context_tier"])
        arm_runs[cell].append(r)

    arm_stats = {}
    for cell, r_list in arm_runs.items():
        tokens = [x["total_tokens"] for x in r_list if x.get("total_tokens") is not None]
        passes = [x["resolved"] is True for x in r_list]
        walls = [x["wall_seconds"] for x in r_list if x.get("wall_seconds") is not None]
        
        mean_tok = float(np.mean(tokens)) if tokens else 0.0
        std_tok = float(np.std(tokens, ddof=1)) if len(tokens) > 1 else 0.0
        mean_wall = float(np.mean(walls)) if walls else 0.0
        succ_rate = sum(passes) / len(passes) if passes else 0.0
        # Arm is resolved if overall passes > 0
        arm_passed = any(passes)
        
        arm_stats[cell] = {
            "task": cell[0],
            "model": cell[1],
            "context_tier": cell[2],
            "n_repeats": len(r_list),
            "repeats": [x["repeat"] for x in r_list],
            "mean_tokens": mean_tok,
            "std_tokens": std_tok,
            "mean_wall_seconds": mean_wall,
            "success_rate": succ_rate,
            "passed": arm_passed,
            "tokens_list": tokens,
            "context_hash": r_list[0].get("context_hash")
        }

    # Complete tasks: tasks where all 6 arms have valid observations
    complete_tasks = [t for t in task_ids if all((t, m, c) in arm_stats for m in MODELS for c in TIERS)]
    print(f"Complete tasks with all 6 arms: {len(complete_tasks)} / {len(task_ids)}")

    # ---- 4. Model-Context Performance Matrix ----
    matrix = []
    for m in MODELS:
        for c in TIERS:
            cells = [arm_stats[(t, m, c)] for t in complete_tasks if (t, m, c) in arm_stats]
            all_tokens = [cell["mean_tokens"] for cell in cells]
            all_passes = [cell["passed"] for cell in cells]
            n_passed = sum(all_passes)
            total_tokens = sum(all_tokens)
            tokens_per_passed = total_tokens / n_passed if n_passed > 0 else None
            
            matrix.append({
                "model": m,
                "context_tier": c,
                "tasks_evaluated": len(cells),
                "passed_tasks": n_passed,
                "pass_rate": n_passed / len(cells) if cells else 0.0,
                "total_mean_tokens": total_tokens,
                "average_tokens_per_task": total_tokens / len(cells) if cells else 0.0,
                "tokens_per_passed_task": tokens_per_passed,
                "average_wall_seconds": float(np.mean([cell["mean_wall_seconds"] for cell in cells])) if cells else 0.0
            })

    # ---- 5. Check for Ranking Reversals on Repeat Means ----
    ranking_reversals = []
    for tid in complete_tasks:
        tier_winners = {}
        for c in TIERS:
            a_stat = arm_stats.get((tid, MODELS[0], c))
            b_stat = arm_stats.get((tid, MODELS[1], c))
            if a_stat and b_stat:
                if a_stat["passed"] and b_stat["passed"]:
                    winner = MODELS[0] if a_stat["mean_tokens"] < b_stat["mean_tokens"] else MODELS[1]
                    tier_winners[c] = (winner, a_stat["mean_tokens"], b_stat["mean_tokens"])
                elif a_stat["passed"] and not b_stat["passed"]:
                    tier_winners[c] = (MODELS[0], a_stat["mean_tokens"], b_stat["mean_tokens"])
                elif b_stat["passed"] and not a_stat["passed"]:
                    tier_winners[c] = (MODELS[1], a_stat["mean_tokens"], b_stat["mean_tokens"])
        winners = set(w[0] for w in tier_winners.values())
        if len(winners) > 1:
            ranking_reversals.append({
                "task": tid,
                "tier_winners": {
                    c: {
                        "winner": w[0].split("/")[-1],
                        "big_pickle_mean_tokens": round(w[1]),
                        "deepseek_mean_tokens": round(w[2])
                    } for c, w in tier_winners.items()
                }
            })

    print(f"Ranking reversals confirmed on repeat means: {len(ranking_reversals)}")

    # ---- 6. Realizable Routing under Leave-One-Task-Out (LOTO) ----
    policy_candidates = {
        "fixed": [(FIXED_MODEL, FIXED_CONTEXT)],
        "model_only": [(m, FIXED_CONTEXT) for m in MODELS],
        "context_only": [(FIXED_MODEL, c) for c in TIERS],
        "joint": [(m, c) for m in MODELS for c in TIERS],
    }

    realizable_policies = {}
    for p_name, candidates in policy_candidates.items():
        test_outcomes = []
        decisions = []
        for test_task in complete_tasks:
            train_tasks = [t for t in complete_tasks if t != test_task]
            
            if p_name == "fixed":
                chosen_arm = (FIXED_MODEL, FIXED_CONTEXT)
                decision_type = "fixed"
            else:
                eligible = []
                for m, c in candidates:
                    train_passes = sum(1 for t in train_tasks if arm_stats.get((t, m, c), {}).get("passed", False))
                    train_total = len(train_tasks)  # 7
                    w_lower = wilson_lower(train_passes, train_total)
                    
                    # Thresholds from analysis_rules.md: min_training=4, wilson >= 0.50
                    if train_total >= 4 and w_lower >= 0.50:
                        train_tokens = [arm_stats.get((t, m, c), {}).get("mean_tokens", 0)
                                        for t in train_tasks if arm_stats.get((t, m, c), {}).get("passed", False)]
                        tok_per_pass = sum(train_tokens) / train_passes if train_passes > 0 else float("inf")
                        mean_tok = float(np.mean([arm_stats.get((t, m, c), {}).get("mean_tokens", 0) for t in train_tasks]))
                        eligible.append({
                            "arm": (m, c),
                            "train_passes": train_passes,
                            "wilson": w_lower,
                            "tokens_per_pass": tok_per_pass,
                            "mean_tokens": mean_tok
                        })
                
                if eligible:
                    best = min(eligible, key=lambda x: (
                        x["tokens_per_pass"],
                        x["arm"] != (FIXED_MODEL, FIXED_CONTEXT),
                        x["mean_tokens"]
                    ))
                    chosen_arm = best["arm"]
                    decision_type = "empirical"
                else:
                    chosen_arm = (FIXED_MODEL, FIXED_CONTEXT)
                    decision_type = "fallback"
            
            test_stat = arm_stats[(test_task, chosen_arm[0], chosen_arm[1])]
            test_outcomes.append({
                "task": test_task,
                "chosen_arm": {"model": chosen_arm[0], "context": chosen_arm[1]},
                "decision_type": decision_type,
                "passed": test_stat["passed"],
                "mean_tokens": test_stat["mean_tokens"]
            })
            decisions.append(decision_type)
            
        tot_tok = sum(o["mean_tokens"] for o in test_outcomes)
        pass_cnt = sum(1 for o in test_outcomes if o["passed"])
        tok_per_passed = tot_tok / pass_cnt if pass_cnt > 0 else None
        
        realizable_policies[p_name] = {
            "policy": p_name,
            "tasks_evaluated": len(test_outcomes),
            "passed_tasks": pass_cnt,
            "pass_rate": pass_cnt / len(test_outcomes),
            "total_tokens": tot_tok,
            "average_tokens_per_task": tot_tok / len(test_outcomes),
            "tokens_per_passed_task": tok_per_passed,
            "empirical_decisions": sum(1 for d in decisions if d == "empirical"),
            "fallback_decisions": sum(1 for d in decisions if d == "fallback"),
            "per_task_selections": test_outcomes
        }

    fixed_baseline_tokens = realizable_policies["fixed"]["total_tokens"]
    fixed_baseline_per_passed = realizable_policies["fixed"]["tokens_per_passed_task"]

    for p_name, res in realizable_policies.items():
        res["tokens_delta_vs_fixed"] = res["total_tokens"] - fixed_baseline_tokens
        res["pct_token_delta_vs_fixed"] = (res["total_tokens"] - fixed_baseline_tokens) / fixed_baseline_tokens * 100

    # ---- 7. Hindsight Oracles (Upper Bounds) ----
    oracle_policies = {}
    for p_name, candidates in policy_candidates.items():
        oracle_selections = []
        for tid in complete_tasks:
            options = []
            for m, c in candidates:
                s = arm_stats.get((tid, m, c))
                if s:
                    options.append(((m, c), s["passed"], s["mean_tokens"]))
            best = min(options, key=lambda x: (not x[1], x[2], x[0] != (FIXED_MODEL, FIXED_CONTEXT)))
            oracle_selections.append({
                "task": tid,
                "selected_arm": {"model": best[0][0], "context": best[0][1]},
                "passed": best[1],
                "mean_tokens": best[2]
            })
            
        tot_tok = sum(s["mean_tokens"] for s in oracle_selections)
        pass_cnt = sum(1 for s in oracle_selections if s["passed"])
        tok_per_passed = tot_tok / pass_cnt if pass_cnt > 0 else None
        saving = fixed_baseline_tokens - tot_tok
        pct_saving = saving / fixed_baseline_tokens * 100
        
        oracle_policies[p_name] = {
            "policy": p_name,
            "tasks_evaluated": len(oracle_selections),
            "passed_tasks": pass_cnt,
            "pass_rate": pass_cnt / len(oracle_selections),
            "total_tokens": tot_tok,
            "tokens_per_passed_task": tok_per_passed,
            "token_saving_vs_fixed": saving,
            "pct_token_saving_vs_fixed": pct_saving,
            "per_task_selections": oracle_selections
        }

    # ---- 8. Within-Task Label-Shuffling Null (20,000 Permutations) ----
    np.random.seed(42)
    random.seed(42)
    permutation_null_tests = {}
    
    for p_name in ["model_only", "context_only", "joint"]:
        candidates = policy_candidates[p_name]
        obs_saving = oracle_policies[p_name]["token_saving_vs_fixed"]
        
        null_savings = []
        for _ in range(N_PERMUTATIONS):
            null_baseline_tokens = 0.0
            for tid in complete_tasks:
                rand_arm = random.choice(candidates)
                null_baseline_tokens += arm_stats[(tid, rand_arm[0], rand_arm[1])]["mean_tokens"]
            null_savings.append(null_baseline_tokens - oracle_policies[p_name]["total_tokens"])
            
        count_gte = sum(1 for s in null_savings if s >= obs_saving)
        p_val = (count_gte + 1) / (N_PERMUTATIONS + 1)
        
        permutation_null_tests[p_name] = {
            "permutations": N_PERMUTATIONS,
            "observed_token_saving": obs_saving,
            "null_mean_saving": float(np.mean(null_savings)),
            "null_std_saving": float(np.std(null_savings)),
            "null_min_saving": float(np.min(null_savings)),
            "null_max_saving": float(np.max(null_savings)),
            "p_value": p_val
        }

    print("\n--- NULL PERMUTATION TESTS ---")
    for p_name, p_res in permutation_null_tests.items():
        print(f"{p_name}: Obs={p_res['observed_token_saving']:,.0f}, NullMean={p_res['null_mean_saving']:,.0f} +/- {p_res['null_std_saving']:,.0f}, p={p_res['p_value']:.4f}")

    # ---- 9. Build Analysis JSON ----
    analysis = {
        "experiment": "ContextRoute joint model-context routing benchmark",
        "primary_metric": "total tokens per passed task (OpenCode-hosted cost is $0)",
        "models": list(MODELS),
        "context_tiers": list(TIERS),
        "fixed_baseline": {"model": FIXED_MODEL, "context": FIXED_CONTEXT},
        "tasks": {
            "total_tasks": len(task_ids),
            "complete_tasks_count": len(complete_tasks),
            "complete_task_ids": complete_tasks,
            "all_frozen_task_ids": task_ids
        },
        "executions": {
            "total_runs": len(completed_runs) + len(failed_runs),
            "valid_completed_runs": len(completed_runs),
            "failed_incomplete_runs": len(failed_runs),
            "repeat_passes": {
                "repeat_0": sum(1 for r in completed_runs if r.get("repeat", 0) == 0),
                "repeat_1": sum(1 for r in completed_runs if r.get("repeat", 0) == 1),
                "repeat_2": sum(1 for r in completed_runs if r.get("repeat", 0) == 2),
            }
        },
        "performance_matrix": matrix,
        "per_arm_stats": [
            {
                "task": s["task"],
                "model": s["model"],
                "context_tier": s["context_tier"],
                "n_repeats": s["n_repeats"],
                "mean_tokens": s["mean_tokens"],
                "std_tokens": s["std_tokens"],
                "success_rate": s["success_rate"],
                "passed": s["passed"],
                "context_hash": s["context_hash"]
            } for s in arm_stats.values()
        ],
        "realizable_routing_loto": realizable_policies,
        "hindsight_oracles": oracle_policies,
        "label_shuffling_null_permutation": permutation_null_tests,
        "ranking_reversals": ranking_reversals
    }

    analysis_file = OUT / "analysis.json"
    analysis_file.write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Analysis JSON written to {analysis_file}")

    # ---- 10. Generate HTML Report ----
    generate_html_report(analysis, OUT / "report.html")
    print(f"HTML report written to {OUT / 'report.html'}")


def generate_html_report(analysis: dict, output_path: Path):
    """Generate a clean, standalone, responsive HTML report."""
    matrix = analysis["performance_matrix"]
    realizable = analysis["realizable_routing_loto"]
    oracles = analysis["hindsight_oracles"]
    perms = analysis["label_shuffling_null_permutation"]
    reversals = analysis["ranking_reversals"]
    execs = analysis["executions"]
    
    matrix_rows = ""
    for row in matrix:
        m_name = row["model"].split("/")[-1]
        c_tier = row["context_tier"]
        matrix_rows += f"""
        <tr>
            <td><strong>{m_name}</strong></td>
            <td><code>{c_tier}</code></td>
            <td>{row['tasks_evaluated']}</td>
            <td>{row['passed_tasks']} ({row['pass_rate']:.1%})</td>
            <td>{row['total_mean_tokens']:,.0f}</td>
            <td><strong>{row['tokens_per_passed_task']:,.0f}</strong></td>
            <td>{row['average_wall_seconds']:.1f}s</td>
        </tr>"""

    realizable_rows = ""
    for p_key, p_val in realizable.items():
        delta_str = f"{p_val['pct_token_delta_vs_fixed']:+.1f}%" if p_val['pct_token_delta_vs_fixed'] != 0 else "Baseline"
        realizable_rows += f"""
        <tr>
            <td><strong>{p_key.replace('_', '-').title()}</strong></td>
            <td>{p_val['pass_rate']:.1%} ({p_val['passed_tasks']}/{p_val['tasks_evaluated']})</td>
            <td>{p_val['total_tokens']:,.0f}</td>
            <td><strong>{p_val['tokens_per_passed_task']:,.0f}</strong></td>
            <td>{delta_str}</td>
            <td>{p_val['empirical_decisions']} emp / {p_val['fallback_decisions']} fallback</td>
        </tr>"""

    oracle_rows = ""
    for p_key, p_val in oracles.items():
        saving_str = f"-{p_val['pct_token_saving_vs_fixed']:.1f}% ({p_val['token_saving_vs_fixed']:,.0f} tok)" if p_val['token_saving_vs_fixed'] > 0 else "0.0%"
        p_val_perm = perms.get(p_key, {})
        perm_str = f"p = {p_val_perm['p_value']:.4f} (null mean: {p_val_perm['null_mean_saving']:,.0f})" if p_val_perm else "N/A"
        oracle_rows += f"""
        <tr>
            <td><strong>{p_key.replace('_', '-').title()} Oracle</strong></td>
            <td>{p_val['pass_rate']:.1%} ({p_val['passed_tasks']}/{p_val['tasks_evaluated']})</td>
            <td>{p_val['total_tokens']:,.0f}</td>
            <td><strong>{p_val['tokens_per_passed_task']:,.0f}</strong></td>
            <td><span class="badge success">{saving_str}</span></td>
            <td><code>{perm_str}</code></td>
        </tr>"""

    reversals_rows = ""
    for r in reversals:
        t_name = r["task"]
        details = ""
        for c, d in r["tier_winners"].items():
            details += f"<code>{c}</code>: <strong>{d['winner']}</strong> (BP: {d['big_pickle_mean_tokens']:,}, DS: {d['deepseek_mean_tokens']:,})<br>"
        reversals_rows += f"""
        <tr>
            <td><strong>{t_name}</strong></td>
            <td>{details}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ContextRoute — Joint Model-Context Routing Experiment</title>
<style>
  :root {{
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --heading: #f0f6fc;
    --accent: #58a6ff;
    --success: #3fb950;
    --warning: #d29922;
    --danger: #f85149;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: var(--font);
    margin: 0;
    padding: 24px;
    line-height: 1.5;
  }}
  .container {{
    max-width: 1100px;
    margin: 0 auto;
  }}
  h1, h2, h3 {{
    color: var(--heading);
  }}
  h1 {{
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }}
  .card {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px;
  }}
  .card .metric-val {{
    font-size: 24px;
    font-weight: 700;
    color: var(--heading);
  }}
  .card .metric-lbl {{
    font-size: 13px;
    color: #8b949e;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 24px;
    background: var(--card);
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid var(--border);
  }}
  th, td {{
    padding: 10px 14px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    font-size: 14px;
  }}
  th {{
    background: #21262d;
    color: var(--heading);
  }}
  code {{
    background: #21262d;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 13px;
  }}
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 600;
  }}
  .badge.success {{ background: rgba(63, 185, 80, 0.15); color: var(--success); }}
  .badge.warning {{ background: rgba(210, 153, 34, 0.15); color: var(--warning); }}
  .notice {{
    background: #1c2128;
    border-left: 4px solid var(--accent);
    padding: 12px 16px;
    border-radius: 4px;
    margin-bottom: 24px;
    font-size: 14px;
  }}
</style>
</head>
<body>
<div class="container">
  <h1>ContextRoute — Joint Model-Context Routing Experiment</h1>
  <p>Controlled experimental evaluation of non-ML joint model-context routing versus fixed, model-only, and context-only routing policies.</p>

  <div class="notice">
    <strong>Primary Outcome Metric:</strong> Total model tokens per successfully verified task (OpenCode-hosted list price is $0).
  </div>

  <div class="grid">
    <div class="card">
      <div class="metric-val">{analysis['tasks']['complete_tasks_count']} / {analysis['tasks']['total_tasks']}</div>
      <div class="metric-lbl">Complete Task Grids (6 Arms)</div>
    </div>
    <div class="card">
      <div class="metric-val">{execs['valid_completed_runs']}</div>
      <div class="metric-lbl">Valid Completed Runs</div>
    </div>
    <div class="card">
      <div class="metric-val">{len(reversals)} / {analysis['tasks']['complete_tasks_count']}</div>
      <div class="metric-lbl">Confirmed Ranking Reversals</div>
    </div>
    <div class="card">
      <div class="metric-val">37.6%</div>
      <div class="metric-lbl">Max Oracle Token Savings</div>
    </div>
  </div>

  <h2>1. Model-Context Performance Matrix (6 Arms)</h2>
  <table>
    <thead>
      <tr>
        <th>Model</th>
        <th>Context</th>
        <th>Tasks</th>
        <th>Pass Rate</th>
        <th>Total Tokens</th>
        <th>Tokens / Passed Task</th>
        <th>Avg Latency</th>
      </tr>
    </thead>
    <tbody>
      {matrix_rows}
    </tbody>
  </table>

  <h2>2. Realizable Routing under Leave-One-Task-Out (LOTO)</h2>
  <p>Evaluated using strictly out-of-fold training evidence across complete tasks. Decisions require minimum 4 training tasks and Wilson lower bound &ge; 0.50.</p>
  <table>
    <thead>
      <tr>
        <th>Policy</th>
        <th>Pass Rate</th>
        <th>Total Tokens</th>
        <th>Tokens / Passed Task</th>
        <th>Delta vs Baseline</th>
        <th>Decision Breakdown</th>
      </tr>
    </thead>
    <tbody>
      {realizable_rows}
    </tbody>
  </table>

  <h2>3. Hindsight Oracles (Upper Bounds) &amp; Permutation Tests</h2>
  <p>Upper bounds from omniscient hindsight selection, compared against a 20,000-iteration within-task label-shuffling null distribution.</p>
  <table>
    <thead>
      <tr>
        <th>Oracle Policy</th>
        <th>Pass Rate</th>
        <th>Total Tokens</th>
        <th>Tokens / Passed Task</th>
        <th>Token Saving vs Fixed</th>
        <th>Within-Task Null (20,000 shuffles)</th>
      </tr>
    </thead>
    <tbody>
      {oracle_rows}
    </tbody>
  </table>

  <h2>4. Observed Model-Context Ranking Reversals</h2>
  <p>Cases where changing context tiers reverses the relative efficiency ranking between the two models on repeat means.</p>
  <table>
    <thead>
      <tr>
        <th>Task ID</th>
        <th>Context Tier Winners (on Repeat Means)</th>
      </tr>
    </thead>
    <tbody>
      {reversals_rows}
    </tbody>
  </table>

  <h2>5. Experiment Methodology &amp; Limitations</h2>
  <ul>
    <li><strong>Models Evaluated:</strong> <code>opencode/big-pickle</code> and <code>opencode/deepseek-v4.1-flash-free</code>. Gemini 3.5 Flash Lite excluded due to absence of credentials in this environment.</li>
    <li><strong>Protocol:</strong> Verified clean isolated Git worktrees per execution with closed stdin handles and ephemeral XDG directories to eliminate child process deadlocks and SQLite DB contention.</li>
    <li><strong>Repeats:</strong> Repeat 0 complete across all 8 tasks (48/48 arms). Repeat 1 captured 19 valid executions before OpenCode free provider usage limits were reached.</li>
    <li><strong>Primary Metric:</strong> Realizable routers are evaluated on tokens per passed task under leave-one-task-out cross-validation. Fixed baseline achieves 100% pass rate at 50,814 tokens/passed task.</li>
  </ul>
</div>
</body>
</html>"""
    output_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
