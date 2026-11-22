"""Build a standalone HTML report from observed benchmark results."""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path

from benchmark.evaluator.charts import export_charts, heatmap, tokens_vs_success
from benchmark.evaluator.evaluate import POLICIES, matrix_summary, read_runs, task_key
from benchmark.runner.run import RESULTS, load_json

STYLE = """
:root{color-scheme:light;--paper:#f5f4ee;--ink:#18312a;--accent:#21654f;--rule:#cbd5ca;--muted:#4d6257}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 system-ui,sans-serif}
main{max-width:1140px;margin:auto;padding:42px 24px 75px}header{border-bottom:3px solid var(--accent);padding:0 0 24px}
h1,h2,h3{line-height:1.13;letter-spacing:-.03em}h1{font:clamp(2.6rem,8vw,5rem)/.95 Georgia,serif;margin:.18em 0}
h2{font-size:1.8rem;margin:0 0 .75em}h3{font-size:1.14rem;margin:.8em 0}p{margin:.5em 0}.sub,small{color:var(--muted)}
section{margin-top:38px}article{background:#fffef8;border:1px solid var(--rule);border-radius:7px;padding:20px;margin-top:15px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}caption{text-align:left;font-weight:600}
th,td{padding:9px 12px;border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}thead th{background:#e8ede4}
.table{overflow:auto}td.num{text-align:right;white-space:nowrap}.bad{color:#9b2929;font-weight:700}.good{color:#206249;font-weight:650}
details{margin-top:12px}summary{cursor:pointer;font-weight:650}code{overflow-wrap:anywhere}.plots{display:grid;grid-template-columns:1fr 1fr;gap:22px}
svg{width:100%;height:auto;display:block}.plots svg text{fill:var(--ink);font:12px system-ui}.axis{stroke:#708278;stroke-width:1}
.bar{display:grid;grid-template-columns:7rem 1fr 5rem;gap:9px;align-items:center;margin:12px 0}.track{height:12px;background:#e8ede4}
.fill{display:block;height:100%;background:var(--accent)}footer{margin-top:50px;border-top:1px solid var(--rule);padding-top:18px}
@media(max-width:740px){.plots{grid-template-columns:1fr}.bar{grid-template-columns:6rem 1fr 4rem}main{padding:28px 15px}}
"""


def html(value):
    return escape(str(value), quote=True)


def fmt(value, unit="", decimals=0):
    if value is None:
        return "Unavailable"
    if unit == "$":
        return f"${value:,.{decimals}f}"
    return f"{value:,.{decimals}f}{unit}"


def percentage_change(baseline, routed):
    if baseline is None or routed is None or baseline == 0:
        return None
    return 100 * (routed - baseline) / baseline


def success(row):
    if row.get("execution_status") != "completed":
        return html(row.get("execution_status", "unavailable").capitalize())
    return '<span class="good">Pass</span>' if row.get("resolved") is True else '<span class="bad">Fail</span>'


def table(headers, rows):
    head = "".join(f'<th scope="col">{html(title)}</th>' for title in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="table"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def route_details(row):
    route = row.get("decision") or {}
    reasons = "".join(f"<li>{html(reason)}</li>" for reason in route.get("reasons", []))
    files = (route.get("context") or {}).get("files", [])
    file_rows = [[f"<code>{html(file['path'])}</code>", f"{file['start_line']}–{file['end_line']}", html(file["reason"])] for file in files]
    alternatives = [item for item in route.get("alternatives", []) if item["constraints_met"]]
    choice_rows = [[html(item["model"]), html(item["context"]), str(item["evidence_count"]),
                    fmt(item["success_lower_bound"] * 100 if item["success_lower_bound"] is not None else None, "%", 1),
                    fmt(item["average_cost_usd"], "$", 6)] for item in alternatives]
    return (f'<details><summary>Routing decision</summary><p>{html(route.get("decision_source", row.get("mode")))} '
            f'· {route.get("evidence_count", 0)} supporting observations '
            f'· estimated whole-task cost {fmt(route.get("estimated_cost_usd"), "$", 6)}</p>'
            + (f"<ul>{reasons}</ul>" if reasons else "")
            + (table(["Context file", "Lines", "Retrieval"], file_rows) if file_rows else "")
            + (f"<h3>Eligible configurations</h3>{table(['Model', 'Context', 'Observations', 'Success lower bound', 'Mean cost'], choice_rows)}" if choice_rows else "")
            + "</details>")


def observed_comparisons(rows):
    by_task = {}
    for row in rows:
        key = (*task_key(row), row.get("opencode_version"), row.get("original_model"))
        by_task.setdefault(key, []).append(row)
    blocks = []
    for key, group in sorted(by_task.items()):
        baseline = next((row for row in group if row.get("mode") == "baseline" and row.get("execution_status") == "completed"),
                        next((row for row in group if row.get("mode") == "baseline"), None))
        routed_group = [row for row in group if row.get("mode") in {"joint", "forced"}]
        if not baseline:
            continue
        for routed in routed_group:
            complete = baseline["execution_status"] == routed["execution_status"] == "completed"
            worse = baseline.get("resolved") is True and routed.get("resolved") is False
            banner = '<p class="bad">Regression: OpenCode passed and ContextRoute failed verification.</p>' if worse else ""
            if not complete:
                banner += '<p class="sub">A validated pair is required to calculate differences.</p>'
            label = "Joint router" if routed["mode"] == "joint" else (
                "Compact context" if routed.get("context_id") == "C0" else "Forced configuration")
            title = f"{key[2]} · {label}"
            measures = [
                ["Model", html(baseline.get("model_id") or "Unavailable"), html(routed.get("model_id") or "Unavailable"), ""],
                ["Context", "OpenCode default", html(routed.get("context_id") or "Unavailable"), ""],
                ["Task verification", success(baseline), success(routed), ""],
            ]
            for name, field, unit, decimals in [
                ("Injected context (estimated tokens)", "initial_context_tokens", "", 0),
                ("Input tokens", "input_tokens", "", 0), ("Output tokens", "output_tokens", "", 0),
                ("Total tokens", "total_tokens", "", 0), ("Tool calls", "tool_calls", "", 0),
                ("List-price cost", "estimated_list_price_usd", "$", 6),
                ("Execution time", "latency_seconds", " s", 2),
            ]:
                before, after = baseline.get(field), routed.get(field)
                delta = percentage_change(before, after) if complete else None
                measures.append([name, fmt(before, unit, decimals), fmt(after, unit, decimals), f"{delta:+.1f}%" if delta is not None else "—"])
            blocks.append(f"<article><h3>{html(title)}</h3>{banner}"
                          + table(["Measure", "OpenCode default", "ContextRoute", "Change"], measures) + route_details(routed) + "</article>")
    return "".join(blocks) or '<p class="sub">No paired baseline and routed runs are available.</p>'


def plots(policies):
    if not policies:
        return ""
    values = [(POLICIES[key], policy) for key, policy in policies.items()]
    max_tokens = max((policy["total_tokens"] or 0 for _, policy in values), default=1) or 1
    bars = "".join(f'<div class="bar"><span>{html(label)}</span><span class="track"><span class="fill" '
                   f'style="width:{100 * (policy["total_tokens"] or 0) / max_tokens:.2f}%"></span></span>'
                   f'<span>{fmt(policy["total_tokens"])}</span></div>' for label, policy in values)
    points = [(label, p["total_cost_usd"], p["success_rate"]) for label, p in values
              if p["total_cost_usd"] is not None and p["success_rate"] is not None]
    scale = max((cost for _, cost, _ in points), default=0) or 1
    markers = ""
    colors = ["#21654f", "#a45b25", "#5b4e86", "#1a6291"]
    for i, (label, cost, rate) in enumerate(points):
        x, y = 58 + 310 * cost / scale, 159 - 138 * rate
        markers += f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{9-i}" fill="none" stroke="{colors[i]}" stroke-width="2"><title>{html(label)} · {fmt(cost, "$", 6)} · {rate:.1%}</title></circle>'
    chart = (f'<svg viewBox="0 0 410 188" role="img" aria-label="Task success against simulated list-price cost">'
             '<path class="axis" d="M57 19V160H374" fill="none"/><text x="8" y="25">100%</text><text x="25" y="160">0%</text>'
             f'<text x="55" y="181">$0</text><text x="310" y="181">{fmt(max(point[1] for point in points), "$", 4)}</text>{markers}</svg>'
             if points else '<p class="sub">No policy has both cost and success measurements.</p>')
    return ('<div class="plots"><article><h3>Cost versus success</h3>' + chart
            + '</article><article><h3>Total tokens by policy</h3>' + bars + '</article></div>')


def render(rows, evaluation):
    cells = evaluation.get("matrix") or matrix_summary(rows)
    matrix_rows = [[html(row["model"]), html(row["context"]), str(row["tasks"]),
                    fmt(row["success_rate"] * 100 if row["success_rate"] is not None else None, "%", 1),
                    fmt(row["total_tokens"]), fmt(row["total_cost_usd"], "$", 6), fmt(row["mean_latency_seconds"], " s", 2)] for row in cells]
    policies = evaluation.get("policies", {})
    policy_rows = [[html(POLICIES[name]), str(policy["tasks"]), str(policy["abstentions"]),
                    fmt(policy["success_rate"] * 100 if policy["success_rate"] is not None else None, "%", 1),
                    fmt(policy["total_tokens"]), fmt(policy["cost_per_success_usd"], "$", 6),
                    fmt(policy["mean_latency_seconds"], " s", 2), str(policy["cold_start_fallbacks"])] for name, policy in policies.items()]
    expansion = [[html(POLICIES[name]), fmt(policy["input_tokens"]), fmt(policy["output_tokens"]),
                  fmt(policy["initial_context_tokens"]), fmt(policy["median_latency_seconds"], " s", 2),
                  fmt(policy["successes_per_million_tokens"], "", 1), fmt(policy["mean_recorded_router_overhead_ms"], " ms", 1)]
                 for name, policy in policies.items()]
    reversals = "".join(f"<li>{html(' vs '.join(item['models']))}, {html(' vs '.join(item['contexts']))}: "
                        f"success differences {html(item['success_count_differences'])} on {item['common_tasks']} matched tasks.</li>"
                        for item in evaluation.get("rank_reversals", []))
    invalid = [[html(row["task_id"]), html(row["mode"]), html(row.get("context_id")),
                html(row.get("expected_model")), html(row["execution_status"]),
                html(row.get("integration_error") or row.get("provider_statuses") or row["verification"]["summary"])]
               for row in rows if row["execution_status"] != "completed"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ContextRoute benchmark report</title><style>{STYLE}</style></head>
<body><main><header><h1>ContextRoute</h1><p>OpenCode benchmark results</p>
<p class="sub">{len(rows)} recorded attempts · {sum(row["execution_status"] == "completed" for row in rows)} validated executions</p></header>
<section aria-label="Measured comparisons"><h2>OpenCode default versus ContextRoute</h2>{observed_comparisons(rows)}</section>
<section><h2>Model–context matrix</h2>
{table(["Model", "Tier", "Tasks", "Success", "Total tokens", "List-price cost", "Mean time"], matrix_rows) if matrix_rows else '<p class="sub">No validated forced runs are available.</p>'}</section>
<section><h2>Held-out policy replay</h2>
{f'<p class="sub">{html(evaluation.get("split", ""))} · {evaluation.get("complete_tasks", 0)} complete task matrices. Success rates describe selected tasks.</p>' if evaluation else ''}
{table(["Policy", "Selected", "Abstained", "Success", "Tokens", "Cost per success", "Mean time", "Cold starts"], policy_rows) if policies else '<p class="sub">Replay requires at least two tasks with complete matrices and a fixed configuration present in the grid.</p>'}
{plots(policies)}
{table(["Policy", "Input tokens", "Output tokens", "Initial context", "Median time", "Resolutions per 1M tokens", "Recorded routing overhead"], expansion) if expansion else ''}
<h3>Model rank reversals</h3>{f'<ul>{reversals}</ul>' if reversals else '<p class="sub">No rank reversal was observed in eligible matched comparisons.</p>'}</section>
{f'<section><h2>Incomplete runs</h2>{table(["Task", "Mode", "Tier", "Expected model", "Status", "Observed detail"], invalid)}</section>' if invalid else ''}
<footer class="sub">Controlled synthetic-shop pilot. Token totals use OpenCode session telemetry. Costs are simulated list prices from the catalog where available. Percentage changes describe the individual matched runs.</footer>
</main></body></html>"""



def empirical_coverage(empirical):
    if not empirical:
        return ""
    coverage = [[POLICIES[name], str(counts.get("empirical", 0)),
                 str(sum(counts.get(kind, 0) for kind in ("cold_start_fallback", "cold_start", "insufficient_evidence"))),
                 str(counts.get("no_feasible_pair", 0))]
                for name, counts in empirical["decision_counts"].items()]
    fold_rows = [[html(task), str(count)] for task, count in empirical["training_tasks_by_fold"].items()]
    cohort_rows = [[html(label), POLICIES[name], f'{row["successes"]}/{row["tasks"]}',
                    fmt(row["total_tokens"]), fmt(row["mean_latency_seconds"], " s", 2)]
                   for label, cohort in (
                       ("Empirical joint vs fixed", empirical["joint_empirical_vs_fixed"]),
                       ("All three adaptive policies empirical", empirical["all_adaptive_empirical"]),
                   ) for name, row in cohort["measurements"].items()]
    return f"""<section><h2>Empirical decisions and fallback coverage</h2>
<p>{empirical["minimum_count_folds"]} of {len(fold_rows)} held-out folds reached the minimum of
{empirical["minimum_training_tasks"]} distinct training tasks. Each empirical choice also needed a Wilson
lower bound of {empirical["minimum_success_lower_bound"]:.2f} and known whole-task cost. Selections are replayed against recorded executions of the corresponding held-out configurations.</p>
{table(["Adaptive policy", "Empirical", "Fallback", "Abstained"], coverage)}
{table(["Held-out task", "Distinct training tasks"], fold_rows) if fold_rows else ""}
{table(["Matched cohort", "Policy", "Passed", "Total tokens", "Mean latency"], cohort_rows) if cohort_rows
 else "<p>No matched empirical cohort is available. The full replay above includes fallback outcomes.</p>"}
</section>
"""



def decision_traces(selections):
    traces = []
    for task, choices in selections.items():
        for name in POLICIES:
            row = choices.get(name)
            if not row:
                continue
            reason = " ".join(row.get("reasons", []))
            alternatives = [
                [html(item["model"]), html(item["context"]), str(item["evidence_count"]),
                 fmt(item["wilson_lower_bound"], "", 3),
                 html("; ".join(item["empirical_exclusions"]) or "Empirically eligible")]
                for item in row.get("eligible_configurations", [])
            ]
            details = (f'<details><summary>{len(alternatives)} technically eligible</summary>'
                       + table(["Model", "Tier", "Prior tasks", "Wilson bound", "Evidence gates"], alternatives)
                       + "</details>" if alternatives else "None")
            outcome = "Passed" if row["resolved"] is True else "Failed" if row["resolved"] is False else "Unavailable"
            source = f'<code>{html(row["decision_type"])}</code>'
            if row.get("fallback_reason"):
                source += f'<br>{html(row["fallback_reason"].replace("_", " "))}'
            traces.append([
                html(task), POLICIES[name], html(row.get("original_model") or "—"), html(row["model"] or "—"),
                html(row["context"] or "—"), source, str(row["evidence_count"]),
                fmt(row["success_lower_bound"], "", 3), fmt(row.get("initial_context_tokens")),
                fmt(row.get("estimated_total_tokens")), fmt(row["total_tokens"]), outcome, html(reason), details,
            ])
    return traces


def experiment_coverage(coverage, model_names, tiers):
    if not coverage:
        return ""
    labels = {"passed": "Passed", "task_failed": "Task failed", "infrastructure_failed": "Infrastructure failed",
              "incomplete": "Incomplete", "missing": "Missing"}
    rows = []
    for task, cells in coverage.items():
        items = []
        for cell in cells:
            status = cell["status"]
            label = html(labels[status])
            if cell["provider_statuses"]:
                label += f'<br><small>HTTP {html(", ".join(map(str, cell["provider_statuses"])))}</small>'
            color = "good" if status == "passed" else "bad" if status in ("task_failed", "infrastructure_failed") else "sub"
            items.append(f'<span class="{color}">{label}</span>')
        rows.append([html(task), *items])
    headers = ["Task", *[f"{model.removeprefix('opencode/')} {tier}" for model in model_names for tier in tiers]]
    return table(headers, rows)


def render_study(evaluation):
    study = evaluation["study"]
    execution = study["execution"]
    policies = evaluation["policies"]
    cases = study["selections"]
    rows = evaluation["matrix"]
    model_names = list(dict.fromkeys(cell["model"] for cell in rows))
    tiers = list(dict.fromkeys(cell["context"] for cell in rows))
    fixed_model, fixed_tier = study["fixed_configuration"]
    matrix_rows = [[html(cell["model"]), cell["context"], str(cell["tasks"]), str(cell["successes"]),
                    fmt(cell["success_rate"] * 100 if cell["success_rate"] is not None else None, "%", 1),
                    fmt(cell["average_tokens"]), fmt(cell["mean_latency_seconds"], " s", 2), fmt(cell["average_cost_usd"], "$", 6)]
                   for cell in rows]
    policy_rows = [[POLICIES[name], str(row["tasks"]), f'{row["successes"]}/{row["tasks"]}',
                    fmt(row["success_rate"] * 100 if row["success_rate"] is not None else None, "%", 1),
                    fmt(row["average_tokens"]), fmt(row["total_tokens"]), fmt(row["mean_latency_seconds"], " s", 2),
                    fmt(row["total_cost_usd"], "$", 6), fmt(row["cost_per_success_usd"], "$", 6),
                    fmt(row["successes_per_million_tokens"], "", 2), fmt(row["token_savings_pct"], "%", 1),
                    "—" if name == "fixed" else str(row["empirical_decisions"]),
                    "—" if name == "fixed" else str(row.get("fallback_decisions", row["cold_start_fallbacks"] + row["insufficient_evidence_fallbacks"]))]
                   for name, row in policies.items()]

    def selected_cell(row):
        if not row["execution_id"]:
            return "Abstained"
        verdict = '<span class="good">Pass</span>' if row["resolved"] else '<span class="bad">Fail</span>'
        return (f'<code>{html(row["model"].removeprefix("opencode/"))} · {html(row["context"])}</code>'
                f'<br>{verdict} · {fmt(row["total_tokens"])} tokens · {fmt(row["latency_seconds"], " s", 2)}')

    fallback_rows = [[html(item["task"]), item["fixed_context"], item["joint_context"],
                      fmt(item["fixed_initial_tokens"]), fmt(item["joint_initial_tokens"]),
                      "Yes" if item["identical_packs"] else "No", fmt(item["fixed_total_tokens"]),
                      fmt(item["joint_total_tokens"]), str(item["evidence_count"]),
                      fmt(item["success_lower_bound"], "", 3)] for item in study.get("fallback_audit", [])]

    trace_rows = decision_traces(cases)
    paired_outcomes = [
        [POLICIES[name], source.title(), str(row["tasks"]),
         fmt(100 * row["success_rate"] if row["success_rate"] is not None else None, "%", 1),
         fmt(row["total_tokens"]), fmt(row["mean_latency_seconds"], " s", 2),
         fmt(row["successes_per_million_tokens"], "", 2)]
        for name, policy in policies.items() if name != "fixed"
        for source, row in policy.get("outcomes_by_source", {}).items()
    ]
    receipts = study.get("provider_availability", [])
    throttled = next((entry for entry in reversed(receipts) if entry.get("upstream_status") == 429), None)
    policy_version = evaluation.get("policy_version", "initial-pack-fallback-v1")
    history = ('<p><a href="history/initial-pack-fallback-v1/report.html">Original three-task evaluation</a>. '
               'This replay applies a prospective fallback change to the same recorded executions.</p>'
               if policy_version == "default-tier-fallback-v2" else "")

    per_task = []
    for task, options in cases.items():
        base, model_only, joint = (options[name] for name in ("fixed", "model_only", "joint"))
        note = html(joint["decision_type"].replace("_", " "))
        if base["resolved"] is True and joint["resolved"] is False:
            note += '<br><span class="bad">Regression versus fixed</span>'
        if model_only["resolved"] is False and joint["resolved"] is True:
            note += '<br><span class="good">Improved versus model-only</span>'
        delta = percentage_change(base["total_tokens"], joint["total_tokens"])
        per_task.append([html(task), selected_cell(base), selected_cell(model_only), selected_cell(joint),
                         f"{delta:+.1f}%" if delta is not None else "—", note])

    interaction_rows = []
    all_cells = []
    for task, record in study["interaction"].items():
        min_tiers = record["smallest_passing_tier"]
        winner = record["token_winner"]
        reversals = record["token_reversals_distinct_packs"]
        identical = record["token_reversals_identical_packs"]
        changes = [f'{item["model"].removeprefix("opencode/")} {item["from"]}→{item["to"]} '
                   f'{("pass" if item["gained"] else "fail")}'
                   f'{(" (identical pack)" if not item["distinct_pack"] else "")}' for item in record["outcome_changes"]]
        extra = [f'{item["model"].removeprefix("opencode/")} {item["from"]}→{item["to"]} (+{item["extra_tokens"]:,})'
                 for item in record["extra_tokens_without_better_correctness"]]
        interaction_rows.append([html(task), "<br>".join(html(f'{model.removeprefix("opencode/")}: {min_tiers[model] or "no pass"}')
                                                          for model in model_names if model in min_tiers) or "Incomplete",
            "<br>".join(html(item) for item in changes) or "—", "<br>".join(html(item) for item in extra) or "—",
            "<br>".join(html(f'{tier}: {winner[tier].removeprefix("opencode/")}') for tier in tiers if winner[tier]) or "—",
            html(", ".join("→".join(pair) for pair in reversals)) or "—",
            html(", ".join("→".join(pair) for pair in identical)) or "—"])
        all_cells.append([html(task), *[
            ("Missing" if cell is None else (
             ('<span class="good">Pass</span>' if cell["passed"] else '<span class="bad">Fail</span>')
             + f'<br>{cell["total_tokens"]:,} tokens<br><small>Initial: {cell["initial_context_tokens"]:,}</small>'))
            for model in model_names for cell in record["outcomes"][model]
        ]])

    issues = [[html(task), html(model), html(tier)] for task, model, tier in execution["missing_cells"]]
    attempts = [[html(task), html(model), html(tier), html(issue["reason"]), html(issue["execution_id"] or "—")]
                for issue in execution["issues"] for task, model, tier in [issue["cell"]]]
    efficiency = study["matched_efficiency"]
    matched = [[row["tier"], *[fmt(row["mean_tokens"][model]) for model in model_names],
                html(row["winner"].removeprefix("opencode/") if row["winner"] else "—")] for row in efficiency["tiers"]]
    joint_cmp = study["joint_vs_model_only"]
    savings = joint_cmp["token_savings_pct"]
    direction = "fewer" if savings is not None and savings >= 0 else "more"
    token_comparison = f'{fmt(abs(savings), "%", 1)} {direction} tokens' if savings is not None else "token difference unavailable"
    comparison_note = (f'<p>{joint_cmp["paired_tasks"]} tasks paired with model-only · '
                       f'{joint_cmp["verified_success_delta"]:+d} verified successes · '
                       f'{token_comparison}.</p>'
                       if joint_cmp["paired_tasks"] else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ContextRoute routing evaluation</title><style>{STYLE}</style></head>
<body><main><header><h1>ContextRoute</h1><p>Joint model and context evaluation</p>
<p class="sub">{execution["valid_runs"]} of {execution["expected_cells"]} grid cells verified ·
{execution["attempts"]} provider attempts · {execution["all_recorded_tokens"]:,} recorded tokens</p>
<p class="sub">Policy version <code>{html(policy_version)}</code></p>{history}</header>
<section><h2>Benchmark execution</h2>
<p>{execution["verified_successes"]} verified successes, {execution["valid_task_failures"]} task failures,
{execution["failed_infrastructure_runs"]} infrastructure failures, {execution["incomplete_runs"]} incomplete attempts,
and {execution["other_rejected_attempts"]} other rejected attempts.</p>
<p>{len(execution["missing_cells"])} cells lack a valid result, including {len(execution["unattempted_cells"])} that were never attempted.
Recorded HTTP 429 attempts: {execution["provider_rate_limited_attempts"]}.</p>
<p class="sub">All cells use the same synthetic-shop revision. Token totals include recorded session usage and can omit usage from interrupted attempts.
Model runs used disposable Git clones with shell access denied. Evaluation used independent pytest verification.</p>
{f'<p class="bad">A stateless availability check for {html(throttled["model"])} returned HTTP 429. The benchmark remains paused.</p>' if throttled else ""}
{experiment_coverage(study.get("coverage"), model_names, tiers)}</section>
<section><h2>Model–context matrix</h2>{heatmap(rows)}
{table(["Model", "Tier", "Evaluated", "Passed", "Pass rate", "Mean tokens", "Mean latency", "Mean list price"], matrix_rows)}</section>
<section><h2>Held-out routing policies</h2>
<p class="sub">{html(evaluation["split"])} · {evaluation["complete_tasks"]} complete task grids ·
{html(evaluation["replay_status"].replace("_", " "))}. Fixed configuration is {html(fixed_model)} with {html(fixed_tier)}.
The policy comparison uses {len(study.get("matched_policy_task_ids", cases))} tasks shared by all policies.
The default requirement is four observations and a Wilson lower bound of 0.50.</p>
{table(["Policy", "Tasks", "Passed", "Pass rate", "Mean tokens", "Total tokens", "Mean latency", "Total list price",
        "List price per pass", "Passes per 1M tokens", "Token savings vs fixed", "Empirical", "Fallbacks"], policy_rows)
 if policy_rows else '<p>At least two complete task grids are required for held-out replay.</p>'}
{comparison_note}
{f'<p class="sub">All {policies["joint"]["tasks"]} joint decisions used fallback because the training evidence fell below the configured requirements.</p>' if policies and policies["joint"]["tasks"] > 0 and policies["joint"]["empirical_decisions"] == 0 and policies["joint"]["abstentions"] == 0 else ''}
{tokens_vs_success(policies)}
<p class="sub">At zero catalog prices, token consumption and verified results distinguish the policies. Dollar savings are undefined.</p></section>
{empirical_coverage(study.get("empirical_replay"))}
{f'<section><h2>Outcomes by decision source</h2>{table(["Policy", "Source", "Tasks", "Pass rate", "Total tokens", "Mean latency", "Passes per 1M tokens"], paired_outcomes)}</section>' if paired_outcomes else ""}
{f'<section><h2>Held-out decision traces</h2><p class="sub">Initial tokens measure the injected pack. Prior mean total tokens describe the training observations. The measured total comes from the held-out execution.</p>{table(["Task", "Policy", "Original model", "Selected model", "Tier", "Decision source", "Prior tasks", "Wilson bound", "Initial tokens", "Prior mean total tokens", "Measured total tokens", "Verification", "Reason", "Eligible configurations"], trace_rows)}</section>' if trace_rows else ""}
{f'<section><h2>Fallback versus the fixed reference</h2><p>{("Fallback preserves the original model and prefers the configured default tier when compatible." if policy_version == "default-tier-fallback-v2" else "Fallback ranks initial context after preserving the original model.")} The fixed reference uses C2. Whole-session usage is measured after execution.</p>{table(["Task", "Fixed tier", "Fallback tier", "Fixed initial tokens", "Fallback initial tokens", "Identical pack", "Fixed total tokens", "Fallback total tokens", "Prior tasks", "Wilson lower bound"], fallback_rows)}</section>' if fallback_rows else ""}
<section><h2>Per-task fixed versus routed results</h2>
{table(["Task", "Original model and fixed tier", "Model-only", "ContextRoute joint", "Token change vs fixed", "Joint decision"], per_task)
 if per_task else '<p>No held-out policy selections are available.</p>'}</section>
<section><h2>Model–context interactions</h2>
<p>Winner means fewer total tokens among two verified successes. A reversal with identical context text reflects variation between executions.
Different packs still give only descriptive comparisons because each cell has one execution.</p>
<p class="sub">Matched aggregate token use on {len(efficiency["all_six_pass_tasks"])} tasks that passed all six configurations.</p>
{table(["Tier", *[model.removeprefix("opencode/") for model in model_names], "Lower mean tokens"], matched)}
{table(["Task", "First passing tier", "Correctness changes", "Higher tier used extra tokens with no correctness gain",
        "Token winner by tier", "Reversals across distinct packs", "Reversals across identical packs"], interaction_rows)}
<details><summary>All task and configuration measurements</summary>{table(
 ["Task", *[f"{model.removeprefix('opencode/')} {tier}" for model in model_names for tier in tiers]], all_cells)}</details></section>
{f'<section><h2>Missing grid cells</h2>{table(["Task", "Model", "Tier"], issues)}</section>' if issues else ''}
{f'<section><h2>Invalid or failed attempts</h2>{table(["Task", "Model", "Tier", "Reason", "Execution ID"], attempts)}</section>' if attempts else ''}
<footer class="sub">This small, single-repository study uses synthetic shop tasks. Incomplete cells reduce the matched set.
Free-tier limits, model ordering, provider load, and run-to-run variability can affect coverage, tokens, and latency.
Generalization requires repeated trials and additional repositories.</footer>
</main></body></html>"""



def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=RESULTS / "runs")
    parser.add_argument("--evaluation", type=Path, default=RESULTS / "evaluation.json")
    parser.add_argument("--output", type=Path, default=RESULTS / "report.html")
    args = parser.parse_args(argv)
    evaluation = load_json(args.evaluation) if args.evaluation.is_file() else {}
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if "study" in evaluation:
        document = render_study(evaluation)
        export_charts(evaluation, output.parent)
    else:
        document = render(read_runs(args.runs), evaluation)
    output.write_text(document, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()

