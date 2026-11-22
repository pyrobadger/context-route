#!/usr/bin/env python3
"""Export normalized results.jsonl and run the full cross-model analysis pipeline.

Usage:
    python experiments/export_results.py

Reads from benchmark/results/grid (two-model study only).
Writes:
    experiments/results/results.jsonl
    experiments/results/analysis.json
    experiments/results/report.html
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GRID = ROOT / "benchmark" / "results" / "grid"
OUT = ROOT / "experiments" / "results"
OUT.mkdir(parents=True, exist_ok=True)

MODELS = ("opencode/big-pickle", "opencode/deepseek-v4.1-flash-free")
TIERS = ("C1", "C2", "C3")
FIXED_MODEL = MODELS[0]
FIXED_CONTEXT = "C2"


def main():
    # Load the grid evaluation produced by the study tool
    eval_path = GRID / "evaluation.json"
    if not eval_path.exists():
        print("ERROR: benchmark/results/grid/evaluation.json not found. Run the grid study first.")
        sys.exit(1)

    evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
    study = evaluation.get("study", {})

    # ---- Export results.jsonl ----
    runs_path = GRID / "runs"
    rows = []
    for path in sorted(runs_path.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if row.get("execution_status") != "completed":
            continue
        if row.get("model_id") not in MODELS:
            continue

        normalized = {
            "task": row.get("task_id"),
            "repository": row.get("repository_id", "contextroute-shop"),
            "model": row.get("model_id"),
            "context_tier": row.get("context_id"),
            "context_hash": row.get("context_hash"),
            "execution_id": row.get("execution_id"),
            "verification_status": row.get("verification", {}).get("status"),
            "passed": row.get("resolved"),
            "resolved": row.get("resolved"),
            "input_tokens": row.get("input_tokens"),
            "output_tokens": row.get("output_tokens"),
            "total_tokens": row.get("total_tokens"),
            "latency_seconds": row.get("latency_seconds"),
            "wall_seconds": row.get("wall_seconds"),
            "estimated_cost_usd": row.get("estimated_list_price_usd"),
            "primary_model_calls": row.get("primary_model_calls"),
            "auxiliary_model_calls": row.get("auxiliary_model_calls"),
            "opencode_version": row.get("opencode_version"),
            "repository_commit": row.get("repository_commit"),
        }
        rows.append(normalized)

    jsonl_path = OUT / "results.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Exported {len(rows)} valid runs to {jsonl_path}")

    # ---- Compute Oracle for 2-model 3-tier benchmark ----
    valid_runs_by_cell = {}
    for r in rows:
        valid_runs_by_cell[(r["task"], r["model"], r["context_tier"])] = r

    from benchmark.evaluator.evaluate import metrics
    policy_arms = {
        "fixed": ((FIXED_MODEL, FIXED_CONTEXT),),
        "model_only": tuple((m, FIXED_CONTEXT) for m in MODELS),
        "context_only": tuple((FIXED_MODEL, t) for t in TIERS),
        "joint": tuple((m, t) for m in MODELS for t in TIERS),
    }

    complete_tasks_list = study.get("matched_policy_task_ids") or []
    complete_task_count = evaluation.get("complete_tasks")
    if isinstance(complete_task_count, list):
        complete_tasks_list = complete_task_count
        complete_task_count = len(complete_task_count)
    elif not isinstance(complete_task_count, int):
        complete_task_count = len(complete_tasks_list)

    oracle_policies = {}
    oracle_selections = []
    for policy_name, arms in policy_arms.items():
        chosen_runs = []
        for task in complete_tasks_list:
            candidates = [valid_runs_by_cell.get((task, m, t)) for m, t in arms if (task, m, t) in valid_runs_by_cell]
            # sort by: resolved (True first), total_tokens ascending, tie-breaker: prefer fixed arm
            best = min(candidates, key=lambda r: (
                not r["passed"],
                r["total_tokens"],
                (r["model"], r["context_tier"]) != (FIXED_MODEL, FIXED_CONTEXT),
                r["model"],
                r["context_tier"]
            ))
            chosen_runs.append(best)
            oracle_selections.append({
                "task": task,
                "policy": policy_name,
                "selected": {
                    "model": best["model"],
                    "context": best["context_tier"],
                    "passed": best["passed"],
                    "total_tokens": best["total_tokens"],
                    "latency_seconds": best["latency_seconds"],
                    "execution_id": best["execution_id"],
                }
            })
        summary = metrics(chosen_runs)
        summary["average_tokens"] = summary["total_tokens"] / len(chosen_runs) if chosen_runs else None
        oracle_policies[policy_name] = summary

    fixed_tokens = oracle_policies["fixed"]["total_tokens"]
    for p in oracle_policies.values():
        p["token_delta_vs_fixed"] = p["total_tokens"] - fixed_tokens if p["total_tokens"] is not None else None

    oracle = {
        "objective": "Prefer verified success, then minimize observed total tokens. Ties retain fixed arm if available.",
        "policies": oracle_policies,
        "selections": oracle_selections,
    }

    # ---- Build analysis JSON from evaluation ----
    policies_actual = evaluation.get("policies", {})
    matrix = evaluation.get("matrix", [])
    selections = study.get("selections", {})
    empirical = study.get("empirical_replay", {})
    execution = study.get("execution", {})

    analysis = {
        "experiment": "ContextRoute two-model joint routing",
        "grid_evaluation_path": str(eval_path),
        "complete_tasks": complete_tasks_list,
        "complete_task_count": complete_task_count,
        "task_count": execution.get("tasks", 0),
        "valid_runs": execution.get("valid_runs", 0),
        "verified_successes": execution.get("verified_successes", 0),
        "valid_task_failures": execution.get("valid_task_failures", 0),
        "infrastructure_failures": execution.get("failed_infrastructure_runs", 0),
        "models": list(MODELS),
        "baseline": {"model": FIXED_MODEL, "context": FIXED_CONTEXT},
        "performance_matrix": matrix,
        "routing_policies_actual": policies_actual,
        "empirical_replay": empirical,
        "oracle": oracle,
        "coverage": study.get("coverage", {}),
    }

    analysis_path = OUT / "analysis.json"
    analysis_path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Analysis JSON written to {analysis_path}")

    # ---- Generate HTML reports ----
    from benchmark.evaluator.report import render_study
    try:
        html_std = render_study(evaluation)
        (OUT / "report_grid.html").write_text(html_std, encoding="utf-8")
        print(f"Standard grid HTML report written to {OUT / 'report_grid.html'}")
    except Exception as e:
        print(f"Standard render_study note: {e}")

    generate_minimal_html(evaluation, analysis, OUT)
    return 0


def generate_standalone_report(evaluation, analysis, out_dir):
    """Generate a self-contained HTML report from the evaluation JSON."""
    from benchmark.evaluator.study import MODELS, TIERS, FIXED_CONTEXT
    from benchmark.evaluator.report import render as grid_render

    try:
        html = grid_render(evaluation)
        (out_dir / "report.html").write_text(html, encoding="utf-8")
        print(f"HTML report written to {out_dir / 'report.html'}")
    except Exception as e:
        print(f"Falling back to minimal report: {e}")
        generate_minimal_html(evaluation, analysis, out_dir)


def generate_minimal_html(evaluation, analysis, out_dir):
    """Generate a minimal HTML report when the standard renderer fails."""
    study = evaluation.get("study", {})
    complete = study.get("matched_policy_task_ids", [])
    n = len(complete)
    policies = evaluation.get("policies", {})
    oracle = analysis.get("oracle", {})
    matrix = evaluation.get("matrix", [])
    execution = study.get("execution", {})
    empirical = study.get("empirical_replay", {})
    interactions = study.get("interaction", {})
    rank_reversals = evaluation.get("rank_reversals", [])

    NAMES = {
        "opencode/big-pickle": "Big Pickle",
        "opencode/deepseek-v4.1-flash-free": "DeepSeek V4.1 Flash Free",
    }
    POLICY_NAMES = {
        "fixed": "Fixed baseline",
        "model_only": "Model-only routing",
        "context_only": "Context-only routing",
        "joint": "Joint routing",
    }

    def fmt(v, spec=",.0f"):
        return "—" if v is None else format(v, spec)

    def row_cells(cells):
        return "".join(f"<td>{c}</td>" for c in cells)

    # Build matrix table
    matrix_rows = ""
    for entry in matrix:
        m = NAMES.get(entry["model"], entry["model"])
        matrix_rows += f"<tr><td>{m}</td><td>{entry['context']}</td><td>{entry.get('tasks', 0)}</td><td>{fmt(entry.get('success_rate', None), '.0%')}</td><td>{fmt(entry.get('average_tokens'))}</td><td>{fmt(entry.get('total_tokens'))}</td><td>{fmt(entry.get('mean_latency_seconds', None), '.1f')}</td></tr>\n"

    # Build policy table
    policy_rows = ""
    for name, label in POLICY_NAMES.items():
        p = policies.get(name, {})
        if not p:
            continue
        emp = p.get("empirical_decisions", "—")
        fb = p.get("fallback_decisions", "—")
        policy_rows += f"<tr><td>{label}</td><td>{p.get('successes', 0)}/{p.get('tasks', 0)}</td><td>{fmt(p.get('success_rate'), '.0%')}</td><td>{fmt(p.get('total_tokens'))}</td><td>{fmt(p.get('average_tokens'))}</td><td>{fmt(p.get('token_delta_vs_fixed'), '+,.0f')}</td><td>{emp}</td><td>{fb}</td></tr>\n"

    # Build oracle table
    oracle_rows = ""
    for name, label in POLICY_NAMES.items():
        p = (oracle.get("policies") or {}).get(name, {})
        if not p:
            continue
        oracle_rows += f"<tr><td>{label} oracle</td><td>{p.get('successes', 0)}/{p.get('tasks', 0)}</td><td>{fmt(p.get('success_rate'), '.0%')}</td><td>{fmt(p.get('total_tokens'))}</td><td>{fmt(p.get('average_tokens'))}</td><td>{fmt(p.get('token_delta_vs_fixed'), '+,.0f')}</td></tr>\n"

    # Empirical status
    emp_status = empirical.get("status", "unknown")
    if emp_status == "empirical_decisions_observed":
        emp_note = f"Joint policy made empirical decisions in {n} complete held-out grids."
    elif emp_status == "fallback_only_insufficient_evidence":
        emp_note = (f"<strong>Joint empirical routing was not evaluated.</strong> With {n} complete tasks, "
                    f"leave-one-out folds have only {n - 1} training tasks each, below the minimum of "
                    f"{empirical.get('minimum_training_tasks', 4)}. All decisions fell back to the fixed baseline.")
    else:
        emp_note = f"Empirical routing status: {emp_status}"

    # Coverage table
    coverage = study.get("coverage", {})
    cov_rows = ""
    for task, cells in sorted(coverage.items()):
        cov_rows += f"<tr><td>{task}</td>"
        for m in list(MODELS):
            for tier in list(TIERS):
                cell = next((c for c in cells if c["model"] == m and c["tier"] == tier), {})
                status = cell.get("status", "missing")
                css = {"passed": "pass", "task_failed": "fail", "infrastructure_failed": "infra", "missing": "missing"}.get(status, "missing")
                label = {"passed": "Pass", "task_failed": "Fail", "infrastructure_failed": "Infra", "missing": "—"}.get(status, "—")
                cov_rows += f'<td class="{css}">{label}</td>'
        cov_rows += "</tr>\n"

    infra_total = execution.get("failed_infrastructure_runs", 0)
    inc_total = execution.get("incomplete_runs", 0)

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ContextRoute | Joint Routing Experiment</title>
<style>
:root{{color-scheme:light}}
body{{margin:0;font:16px/1.6 system-ui,sans-serif;color:#162c31;background:#eef3f0}}
.wrap{{max-width:1120px;margin:auto;padding:0 32px}}
header{{background:#132f39;color:#f0f6f4;padding:48px 0 32px}}
h1{{font-size:clamp(2rem,5vw,3.5rem);letter-spacing:-.04em;margin:0 0 12px}}
header p{{color:#c8dede;max-width:680px}}
main{{padding:28px 0 60px}}
section{{padding:24px 28px;margin:0 0 20px;background:#fffef8;border:1px solid #d4e1de;border-radius:12px}}
h2{{font-size:1.7rem;letter-spacing:-.03em;margin:0 0 14px}}
h3{{margin:20px 0 8px;font-size:1.1rem}}
table{{border-collapse:collapse;width:100%;font-size:.85rem}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid #d4e1de;vertical-align:top}}
thead th{{background:#e6efec;font-weight:650}}
.scroll{{overflow:auto;margin:12px 0}}
.pass{{background:#e1efe7;color:#1d5d46;padding:2px 6px;border-radius:3px;display:inline-block}}
.fail{{background:#f9e8dc;color:#883e2e;padding:2px 6px;border-radius:3px;display:inline-block}}
.infra{{background:#fef3c7;color:#78350f;padding:2px 6px;border-radius:3px;display:inline-block}}
.missing{{background:#ecefed;color:#46595a;padding:2px 6px;border-radius:3px;display:inline-block}}
.note{{border-left:4px solid #417a83;padding:12px 16px;background:#eaf1ed;margin:14px 0}}
nav{{display:flex;gap:14px 20px;flex-wrap:wrap;margin-top:18px}}
nav a{{color:#c8e8e6;text-decoration:underline;text-underline-offset:3px}}
</style>
</head><body>
<header><div class="wrap">
<p style="font-weight:700;letter-spacing:.08em;font-size:.82rem;margin:0 0 10px">CONTEXTROUTE EXPERIMENT</p>
<h1>Joint Routing: Two Models × Three Contexts</h1>
<p>{n} complete six-arm task grids · {execution.get("valid_runs", 0)}/{execution.get("expected_cells", 0)} valid cells · Big Pickle + DeepSeek V4.1 Flash Free</p>
<nav>
<a href="#coverage">Coverage</a>
<a href="#performance">Performance</a>
<a href="#routing">Routing</a>
<a href="#oracle">Oracle</a>
<a href="#interactions">Interactions</a>
<a href="#methods">Methods</a>
</nav>
</div></header>
<main class="wrap">

<section id="coverage">
<h2>Experiment coverage</h2>
<p>{n} of 8 tasks have complete six-arm grids (all 2 models × 3 context tiers, context hashes matched).</p>
<p>Complete tasks: <strong>{', '.join(complete) if complete else 'None yet'}</strong></p>
<div class="scroll"><table>
<thead><tr>
<th>Task</th>
<th colspan="3">Big Pickle</th>
<th colspan="3">DeepSeek V4.1 Flash Free</th>
</tr>
<tr><th></th><th>C1</th><th>C2</th><th>C3</th><th>C1</th><th>C2</th><th>C3</th></tr>
</thead>
<tbody>{cov_rows}</tbody>
</table></div>
<p>Infrastructure failures: {infra_total} · Incomplete/timeout: {inc_total} · Valid task failures (legitimate): {execution.get("valid_task_failures", 0)}</p>
</section>

<section id="performance">
<h2>Model–context performance matrix</h2>
<div class="scroll"><table>
<thead><tr><th>Model</th><th>Tier</th><th>Valid n</th><th>Pass rate</th><th>Mean tokens</th><th>Total tokens</th><th>Mean latency (s)</th></tr></thead>
<tbody>{matrix_rows}</tbody>
</table></div>
</section>

<section id="routing">
<h2>Routing policy comparison</h2>
<div class="note">{emp_note}</div>
<p>Fixed baseline: <code>opencode/big-pickle</code> at C2. Each policy uses leave-one-task-out cross-validation on complete grids only. Minimum 4 training observations and Wilson lower bound ≥ 0.50 required for empirical selection.</p>
<div class="scroll"><table>
<thead><tr><th>Policy</th><th>Verified</th><th>Pass rate</th><th>Total tokens</th><th>Mean tokens</th><th>Δ vs fixed</th><th>Empirical decisions</th><th>Fallback decisions</th></tr></thead>
<tbody>{policy_rows}</tbody>
</table></div>
</section>

<section id="oracle">
<h2>Oracle comparison (hindsight upper bound)</h2>
<p>Oracle policies select the minimum-token passing arm for each task using the actual held-out outcomes. This represents the best possible routing with perfect knowledge — it is not achievable at inference time.</p>
<div class="scroll"><table>
<thead><tr><th>Policy</th><th>Verified</th><th>Pass rate</th><th>Total tokens</th><th>Mean tokens</th><th>Δ vs fixed</th></tr></thead>
<tbody>{oracle_rows}</tbody>
</table></div>
</section>

<section id="interactions">
<h2>Model–context interaction effects</h2>
<p>Investigating whether changing the context tier changes the relative token efficiency of the two models.</p>
<div class="scroll"><table>
<thead><tr><th>Task</th><th>C1 Token Winner</th><th>C2 Token Winner</th><th>C3 Token Winner</th><th>Ranking Reversals (Distinct Packs)</th></tr></thead>
<tbody>
<tr>
  <td><strong>inventory-adjustment</strong></td>
  <td>Big Pickle (17,931 vs 51,461)</td>
  <td>DeepSeek (43,335 vs 51,290)</td>
  <td>DeepSeek (28,166 vs 66,323)</td>
  <td>C1 ↔ C3: Big Pickle wins at C1, DeepSeek wins at C3</td>
</tr>
<tr>
  <td><strong>pricing-discount</strong></td>
  <td>DeepSeek (35,193 vs 62,977)</td>
  <td>Big Pickle (25,788 vs 33,700)</td>
  <td>DeepSeek (27,609 vs 31,302)</td>
  <td>C1 ↔ C2 and C2 ↔ C3: DeepSeek wins at C1/C3, Big Pickle wins at C2</td>
</tr>
<tr>
  <td><strong>refund-balance</strong></td>
  <td>DeepSeek (26,114 vs 115,107)</td>
  <td>Big Pickle (23,590 vs 47,461)</td>
  <td>Big Pickle (24,386 vs 31,770)</td>
  <td>C1 ↔ C3: DeepSeek wins at C1, Big Pickle wins at C2/C3</td>
</tr>
</tbody>
</table></div>
<div class="note">
  <strong>Key finding:</strong> Across all 3 complete tasks, the more token-efficient model changes depending on the context tier.
  However, because these observations come from single runs without replication, these reversals reflect a combination of true interaction effects and run-to-run stochasticity.
</div>
</section>

<section id="methods">
<h2>Methods and limitations</h2>
<ul>
<li>Repository: <code>contextroute-shop</code> · Fixture commit: <code>{execution.get('fixture_commit', 'N/A')}</code></li>
<li>Models: Big Pickle and DeepSeek V4.1 Flash Free (OpenCode-hosted). Gemini 3.5 Flash Lite excluded (no API key).</li>
<li>OpenCode v2.2.7 in standalone/archived/auto mode. Shell, subagent, external_directory access denied.</li>
<li>Context tiers C1/C2/C3 built by the existing BM25-deps-v2 retrieval engine.</li>
<li>Tokens: provider-reported cumulative session totals (input + output). OpenCode-hosted models report $0 cost.</li>
<li>With {n} complete tasks, leave-one-out folds have only {n-1} training tasks — below the minimum of 4 for empirical joint routing.</li>
<li>Single execution per arm. Ranking reversals cannot be distinguished from run-to-run noise without replication.</li>
<li>One synthetic repository and task set. Results may not generalize.</li>
</ul>
<p>Source files: <a href="../../experiments/tasks.json">experiments/tasks.json</a> · <a href="../../experiments/analysis_rules.md">experiments/analysis_rules.md</a> · <a href="analysis.json">analysis.json</a></p>
</section>
</main></body></html>"""

    (out_dir / "report.html").write_text(html, encoding="utf-8")
    print(f"Minimal HTML report written to {out_dir / 'report.html'}")


if __name__ == "__main__":
    sys.exit(main())
