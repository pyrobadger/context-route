"""Render the cross-model benchmark as standalone SVGs and a local HTML report."""

from __future__ import annotations

import argparse
from html import escape
from math import ceil
from pathlib import Path

from benchmark.evaluator.charts import shade, svg, text, tokens_vs_success
from benchmark.evaluator.cross_model import MODELS, TIERS
from benchmark.evaluator.evaluate import POLICIES
from benchmark.runner.run import RESULTS, atomic_json, load_json

NAMES = dict(zip(MODELS, ("Big Pickle", "DeepSeek V4.1 Flash Free", "Gemini 3.5 Flash Lite"), strict=True))
COLORS = dict(zip(MODELS, ("#185467", "#a34c36", "#61549c"), strict=True))
TASK_NAMES = {"sku-lookup": "SKU Lookup"}
MISSING = "—"
WIDTH = 864


def task_name(task):
    return TASK_NAMES.get(task, task.replace("-", " ").title())


def fmt(value, spec=",.0f"):
    return MISSING if value is None else format(value, spec)


def dollars(value):
    return MISSING if value is None else "$" + fmt(value, ",.5f")


def table(headers, rows, *, wide=False):
    headings = "".join(f"<th scope='col'>{escape(str(value))}</th>" for value in headers)
    body = []
    for row in rows:
        first, *rest = row
        fields = "".join(f"<td>{escape(str(value))}</td>" for value in rest)
        body.append(f"<tr><th scope='row'>{escape(str(first))}</th>{fields}</tr>")
    tabindex = ' tabindex="0"' if wide else ""
    return f'<div class="scroll"{tabindex}><table><thead><tr>{headings}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def heatmap(matrix):
    cells = {(r["model"], r["context"]): r for r in matrix}
    max_tokens = max((r["average_tokens"] or 0 for r in matrix), default=0) or 1
    elements = []
    for panel, title in enumerate(("Verified pass rate", "Mean total tokens")):
        origin = 14 + panel * 218
        elements.append(text(23, origin + 25, title, size=21, weight="700"))
        for tier_index, tier in enumerate(TIERS):
            elements.append(text(378 + 181 * tier_index, origin + 57, tier, anchor="middle", weight="650"))
        for model_index, model in enumerate(MODELS):
            y = origin + 70 + model_index * 43
            elements.append(text(23, y + 24, NAMES[model], weight="600"))
            for tier_index, tier in enumerate(TIERS):
                r = cells[model, tier]
                rate = r["success_rate"]
                ratio, label = (rate, f'{rate:.0%} ({r["successes"]}/{r["tasks"]})') if panel == 0 and rate is not None \
                    else ((r["average_tokens"] or 0) / max_tokens, fmt(r["average_tokens"]))
                colors = ((230, 244, 237), (23, 100, 77)) if panel == 0 else ((232, 242, 247), (29, 76, 111))
                fill = shade(ratio, *colors) if r["tasks"] else "#ebeee9"
                ink = "white" if ratio >= .63 else "#18312a"
                x = 296 + 181 * tier_index
                elements.append(f'<rect x="{x}" y="{y}" width="164" height="37" fill="{fill}" rx="5"/>')
                elements.append(text(x + 82, y + 24, label if r["tasks"] else "Missing", anchor="middle", weight="650", fill=ink))
    return svg("Matched task pass rates and mean whole-session tokens in nine model and context arms",
               WIDTH, 439, "".join(elements))


def efficiency(matrix, complete_tasks):
    elements = [text(23, 35, "Mean whole-session tokens by model and tier", size=21, weight="700")]
    elements.append(text(23, 59, f"{complete_tasks} common tasks per arm", size=13))
    if not complete_tasks:
        elements.append(text(23, 97, "No complete nine-arm task grids are available."))
        return svg("Mean tokens on common tasks", WIDTH, 125, "".join(elements))
    lookup = {(r["model"], r["context"]): r["average_tokens"] for r in matrix}
    x0, y0, width, height = 78, 280, 743, 182
    scale = max(10000, ceil(max(lookup.values()) / 10000) * 10000)
    for fraction in range(5):
        y = y0 - height * fraction / 4
        elements.append(f'<path d="M{x0} {y}H{x0 + width}" stroke="#dbe4df"/>')
        elements.append(text(x0 - 10, y + 4, f"{scale * fraction / 4 / 1000:g}k", anchor="end", size=12))
    for tier_index, tier in enumerate(TIERS):
        middle = 192 + tier_index * 252
        elements.append(text(middle, y0 + 25, tier, anchor="middle", weight="650"))
        for model_index, model in enumerate(MODELS):
            bar = height * lookup[model, tier] / scale
            x = middle - 75 + model_index * 52
            y = y0 - bar
            title = f"{NAMES[model]} {tier}, {fmt(lookup[model, tier])} tokens"
            elements.append(f'<rect x="{x}" y="{y:.2f}" width="46" height="{bar:.2f}" rx="3" '
                            f'fill="{COLORS[model]}"><title>{escape(title)}</title></rect>')
    for index, model in enumerate(MODELS):
        x = 79 + 252 * index
        elements.append(f'<rect x="{x}" y="329" width="10" height="10" rx="2" fill="{COLORS[model]}"/>')
        elements.append(text(x + 16, 338, NAMES[model], size=13))
    return svg("Grouped bars of mean tokens by model and context tier on complete grids", WIDTH, 363, "".join(elements))


def decisions_chart(policies):
    elements = [text(22, 33, "Held-out policy decisions", size=21, weight="700")]
    if not policies:
        elements.append(text(22, 73, "No held-out replay was available."))
        return svg("Held-out policy decisions", WIDTH, 112, "".join(elements))
    category = (("fixed_decisions", "Fixed assignment", "#526961"), ("empirical_decisions", "Empirical", "#185467"),
                ("fallback_decisions", "Fallback", "#9d4b2e"), ("abstentions", "Abstention", "#655499"))
    for index, (key, name) in enumerate(POLICIES.items()):
        summary = policies[key]
        y = 58 + 48 * index
        count_text = f'{summary["successes"]}/{summary["held_out_tasks"]} verified · {fmt(summary["total_tokens"])} tokens'
        elements.extend([text(23, y + 15, name, weight="700"), text(23, y + 34, count_text, size=12)])
        offset = 300
        for field, label, color in category:
            count = summary[field]
            if count:
                width = 533 * count / summary["held_out_tasks"]
                elements.append(f'<rect x="{offset}" y="{y}" width="{width}" height="31" rx="4" fill="{color}"/>')
                elements.append(text(offset + width / 2, y + 21, f"{count} {label.lower()}", anchor="middle", size=13,
                                     weight="650", fill="white"))
                offset += width
    return svg("Counts of fixed assignments, empirical choices, fallbacks, and abstentions by policy",
               WIDTH, 252, "".join(elements))


def interaction_chart(complete_tasks, reversals):
    elements = [text(22, 34, "Pairwise token-ranking reversals by task", size=21, weight="700")]
    if not complete_tasks:
        elements.append(text(22, 72, "No complete tasks to compare."))
        return svg("Pairwise token-ranking reversals", WIDTH, 100, "".join(elements))
    elements.extend([text(313, 63, "Identical pack", size=13), text(492, 63, "Different pack", size=13)])
    maximum = max((sum(r["task"] == task and r["metric"] == "total_tokens" for r in reversals)
                   for task in complete_tasks), default=0) or 1
    for index, task in enumerate(complete_tasks):
        y = 82 + index * 39
        elements.append(text(23, y + 19, task_name(task), weight="650"))
        x = 312
        for identical, color in ((True, "#78847b"), (False, "#185467")):
            count = sum(r["task"] == task and r["metric"] == "total_tokens" and r["identical_packs"] is identical for r in reversals)
            elements.append(text(x, y + 19, str(count), weight="700"))
            if count:
                elements.append(f'<rect x="{x + 23}" y="{y + 5}" width="{140 * count / maximum}" height="17" rx="3" fill="{color}"/>')
            x += 179
    return svg("Token ranking reversals distinguished by whether the context packs changed",
               WIDTH, 95 + 39 * len(complete_tasks), "".join(elements))


def matrix_table(matrix):
    headers = ["Model", "Tier", "Valid n", "Pass rate", "Mean tokens", "Median tokens", "Mean latency", "Total tokens",
               "Successes / 1M tokens", "Est. total cost"]
    rows = [[NAMES[r["model"]], r["context"], r["tasks"], fmt(r["success_rate"], ".0%"),
             fmt(r["average_tokens"]), fmt(r["median_tokens"]), fmt(r["mean_latency_seconds"], ".2f"),
             fmt(r["total_tokens"]), fmt(r["successes_per_million_tokens"], ".2f"), dollars(r["total_cost_usd"])]
            for r in matrix]
    return table(headers, rows, wide=True)


def policy_table(policies, *, oracle=False):
    if not policies:
        return "<p>No held-out replay is available.</p>"
    headers = ["Policy", "Verified", "Pass rate", "Total tokens", "Mean tokens", "Δ tokens vs fixed", "Mean latency",
               "Est. total cost", "Est. cost / success", "Successes / 1M tokens"]
    if not oracle:
        headers += ["Empirical", "Fallback", "Abstained"]
    rows = []
    for name, r in policies.items():
        label = POLICIES[name] + (" oracle" if oracle and name != "fixed" else "")
        evaluated = r.get("held_out_tasks", r["tasks"])
        row = [label, f'{r["successes"]}/{evaluated}', fmt(r["success_rate"], ".0%"), fmt(r["total_tokens"]),
               fmt(r["average_tokens"]), fmt(r["token_delta_vs_fixed"], "+,.0f"), fmt(r["mean_latency_seconds"], ".2f"),
               dollars(r["total_cost_usd"]), dollars(r["cost_per_success_usd"]), fmt(r["successes_per_million_tokens"], ".2f")]
        if not oracle:
            row += [r["empirical_decisions"], r["fallback_decisions"], r["abstentions"]]
        rows.append(row)
    return table(headers, rows, wide=True)


def coverage_table(study):
    cells = {(r["task"], r["model"], r["tier"]): r for r in study["availability"]}
    columns = [(model, tier) for model in MODELS for tier in TIERS]
    heads = "".join(f'<th scope="colgroup" colspan="3">{escape(NAMES[model])}</th>' for model in MODELS)
    tiers = "".join(f'<th scope="col">{tier}</th>' for _, tier in columns)
    classes = {"verified pass": ("pass", "Pass"), "verified failure": ("fail", "Test failure"),
               "missing (attempted)": ("tried", "Missing"), "missing (unattempted)": ("missing", "Unrun")}
    body = []
    for task in study["task_ids"]:
        fields = []
        for model, tier in columns:
            r = cells[task, model, tier]
            cls, label = classes[r["status"]]
            description = f'{r["status"]}, {r["attempts"]} recorded attempts'
            fields.append(f'<td><span class="pill {cls}" title="{description}">{label}</span></td>')
        body.append(f'<tr><th scope="row">{escape(task_name(task))}</th>{"".join(fields)}</tr>')
    return (f'<div class="scroll" tabindex="0"><table class="coverage"><colgroup span="1"></colgroup>'
            '<colgroup span="3"></colgroup><colgroup span="3"></colgroup><colgroup span="3"></colgroup>'
            f'<thead><tr><th rowspan="2" scope="col">Task</th>{heads}</tr><tr>{tiers}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def trace_table(decisions):
    rows = []
    for r in decisions:
        outcome = r["selected_outcome"] or {}
        outcome_label = "Passed" if outcome.get("resolved") is True else "Failed" if outcome else MISSING
        rows.append([task_name(r["task"]), POLICIES[r["policy"]], NAMES[r["original_model"]],
                     NAMES.get(outcome.get("model_id"), MISSING), outcome.get("context_id", MISSING),
                     r["decision_source"].replace("_", " "), r["training_tasks"],
                     fmt(r["evidence_count"]), fmt(r["wilson_lower_bound"], ".3f"), outcome_label,
                     fmt(outcome.get("total_tokens")), fmt(outcome.get("latency_seconds"), ".2f")])
    return table(["Held-out task", "Policy", "Original model", "Selected model", "Tier", "Decision source", "Training tasks",
                  "Evidence n", "Wilson lower bound", "Verification", "Tokens", "Latency (s)"], rows, wide=True)


def reversal_table(reversals):
    if not reversals:
        return "<p>No strict pairwise ranking reversals were observed in complete grids.</p>"
    rows = []
    for r in reversals:
        winners = [NAMES[m] for m in r["winners"]]
        values = ["/".join(fmt(v) if type(v) is int else ("pass" if v else "fail") for v in pair) for pair in r["values"]]
        rows.append([task_name(r["task"]), " / ".join(NAMES[m] for m in r["models"]), " → ".join(r["tiers"]),
                     "Tokens" if r["metric"] == "total_tokens" else "Verification", " → ".join(values),
                     " → ".join(winners), "Identical" if r["identical_packs"] else "Different"])
    return table(["Task", "Model pair", "Tier change", "Metric", "Measured values in model order", "Winner by tier", "Packs"],
                 rows, wide=True)


def paired_examples(pairs):
    found = [r for r in pairs if r["larger_pack_used_fewer_tokens"] or r["outcome_changed"]]
    if not found:
        return "<p>No qualifying within-model changes were observed.</p>"
    return table(["Task", "Model", "Tier change", "Initial Δ", "Whole-session Δ", "Verification before → after", "Cohort"],
                 [[task_name(r["task"]), NAMES[r["model"]], " → ".join(r["tiers"]), fmt(r["initial_token_delta"], "+,.0f"),
                   fmt(r["total_token_delta"], "+,.0f"), " → ".join("Pass" if result else "Fail" for result in r["resolved"]),
                   "Complete grid" if r["strictly_matched_task"] else "Available only"] for r in found], wide=True)


def report(study, charts):
    complete = study["complete_tasks"]
    n = len(complete)
    interaction = study["interaction"]
    reversals = interaction["ranking_reversals"]
    tokens_r = [r for r in reversals if r["metric"] == "total_tokens"]
    same_count = sum(r["identical_packs"] for r in tokens_r)
    adaptive = study["empirical_replay"]
    oracle = study["oracle"]
    matched = study["matched_matrix"]
    cov = study["coverage"]
    audit = study["protocol_audit"]
    cohort = ", ".join(task_name(task) for task in complete) or "None"
    c3 = next(r for r in study["availability"] if (r["task"], r["model"], r["tier"]) == ("daily-report", MODELS[-1], "C3"))
    daily = c3["observation"]
    if daily:
        daily_summary = ("Gemini Daily Report C3 completed with " + escape(daily["verification"]["summary"])
                         + f' and {fmt(daily["total_tokens"])} measured tokens.')
    else:
        daily_summary = ("Gemini Daily Report C3 is missing. Prior attempts are retained in the attempt audit. "
                         "Any partial verification results from those runs were excluded.")
    valid_failures = sum(cell["status"] == "verified failure" for cell in study["availability"])
    counts = " · ".join(f'{NAMES[model]} {cov[model]["valid"]}/{cov[model]["planned"]}' for model in MODELS)
    matching = ("No same-task, same-tier pack conflicts or execution-design conflicts were found among the audited cells."
                if not study["comparability_issues"] else
                f'{len(study["comparability_issues"])} execution design or pack conflicts excluded tasks from the matched cohort.')
    min_arms = []
    if n:
        for model in MODELS:
            best = min((r for r in matched if r["model"] == model), key=lambda r: r["average_tokens"])
            min_arms.append(f'{NAMES[model]} {best["context"]} at {fmt(best["average_tokens"])}')
    best_note = ("Lowest mean tokens within each model on the matched tasks were " + " · ".join(min_arms) + "."
                 if min_arms else "No matched per-model efficiency comparison was available.")
    rankings = [[r["tier"], " → ".join(NAMES[m] for m in sorted(MODELS, key=r["mean_tokens"].get)),
                 " / ".join(f'{NAMES[m]} {r["verified_successes"][m]}/{n}' for m in MODELS)]
                for r in interaction["tier_rankings"]]
    paired = interaction["paired_tier_observations"]
    improvements = sum(r["strictly_matched_task"] and r["larger_pack_used_fewer_tokens"] for r in paired)
    correctness = sum(r["strictly_matched_task"] and r["outcome_changed"] for r in paired)
    distinct_increases = sum(r["strictly_matched_task"] and not r["identical_packs"] and r["initial_token_delta"] > 0 for r in paired)
    if adaptive["status"] == "empirical_decisions_observed":
        empirical_note = (f'The joint policy made {adaptive["policies"]["joint"]["empirical_decisions"]} empirical '
                          "decisions in complete held-out grids.")
    elif adaptive["policies"]:
        empirical_note = (f'Joint empirical performance remains unevaluated. The replay used {n - 1} training tasks per fold. '
                          f'The joint policy selected {adaptive["policies"]["joint"]["fallback_decisions"]} fallbacks and made '
                          "zero empirical choices.")
    else:
        empirical_note = "Joint empirical performance remains unevaluated. The saved grid has too few complete tasks for replay."
    oracle_note = "No oracle comparison is available." if not n else (
        f'Across {n} complete tasks the joint oracle used {fmt(oracle["policies"]["joint"]["total_tokens"])} tokens. '
        f'The model-only and context-only oracles used {fmt(oracle["policies"]["model_only"]["total_tokens"])} '
        f'and {fmt(oracle["policies"]["context_only"]["total_tokens"])} tokens respectively.')
    diffs = "".join(f"<li>{escape(note)}</li>" for note in audit["provider_differences"])
    trace = trace_table(adaptive["decisions"]) if adaptive["decisions"] else "<p>No held-out decisions are available.</p>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ContextRoute | Three-model benchmark</title><style>
:root{{color-scheme:light;--ink:#162c31;--muted:#4b6267;--line:#d4e1de;--paper:#fffef8;--accent:#185467}}
*{{box-sizing:border-box}}body{{margin:0;font:16px/1.55 system-ui,sans-serif;color:var(--ink);background:#eef3f0}}
.wrap{{max-width:1120px;margin:auto;padding:0 clamp(16px,4vw,60px)}}header{{background:#132f39;color:#f0f6f4;padding:64px 0 40px}}
header h1{{font-size:clamp(2.4rem,6vw,4.55rem);letter-spacing:-.055em;line-height:1.01;max-width:12em;margin:0 0 18px}}
header p{{color:#d0e4e3;max-width:700px}}.eyebrow{{font-weight:650;letter-spacing:.08em;font-size:.82rem}}
nav{{display:flex;flex-wrap:wrap;gap:10px 20px;margin-top:25px}}nav a{{color:#e1f2f0;text-underline-offset:4px}}
main{{padding-top:28px!important;padding-bottom:60px!important}}section{{padding:clamp(16px,3vw,32px);margin:0 0 20px;background:var(--paper);border:1px solid var(--line);border-radius:15px}}
h2{{font-size:clamp(1.45rem,3.1vw,2.15rem);line-height:1.15;letter-spacing:-.035em;margin:0 0 15px}}h3{{margin:24px 0 9px}}
p{{margin:0 0 15px}}figure{{margin:14px 0}}svg{{display:block;width:100%;height:auto}}figcaption,.muted{{color:var(--muted);font-size:.92rem}}
.scroll{{overflow:auto;margin:12px 0 20px}}table{{border-collapse:collapse;width:100%;font-size:.85rem;font-variant-numeric:tabular-nums}}
th,td{{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}thead th{{background:#e6efec}}
tbody th{{font-weight:650}}.coverage thead th{{text-align:center}}.coverage th:first-child{{text-align:left}}td{{min-width:53px}}
.pill{{display:inline-block;padding:2px 7px;border-radius:4px;white-space:nowrap;font-weight:650}}.pass{{background:#e1efe7;color:#1d5d46}}
.fail{{background:#f9e8dc;color:#883e2e}}.tried{{background:#f0e6dc;color:#584035}}.missing{{background:#ecefed;color:#46595a}}
.note{{border-left:4px solid #417a83;padding:12px 14px;background:#eaf1ed}}details>summary{{cursor:pointer;font-weight:650;margin:12px 0}}
code{{overflow-wrap:anywhere}}a{{color:var(--accent)}}@media print{{body,section{{background:white}}header{{padding:20px 0;color:#162c31;background:white}}header p,nav a{{color:#162c31}}figure{{break-inside:avoid}}}}
</style></head><body><header><div class="wrap">
<p class="eyebrow">CONTEXTROUTE BENCHMARK</p><h1>Three models across three context tiers</h1>
<p>{n} complete nine-arm task grids. {escape(counts)} valid cells across the eight-task design.</p>
<nav aria-label="Report sections"><a href="#coverage">Coverage</a><a href="#performance">Performance</a>
<a href="#routing">Routing</a><a href="#interactions">Interactions</a><a href="#oracle">Oracle</a><a href="#methods">Methods</a></nav>
</div></header><main class="wrap">
<section id="coverage"><h2>Experiment coverage</h2><p>{daily_summary}</p>{coverage_table(study)}
<p class="muted">The availability summary includes {valid_failures} verified task failures. Missing marks attempted cells without an accepted observation. Unrun marks unscheduled or unattempted cells.</p>
<p><strong>Strictly matched tasks</strong> {escape(cohort)}. {escape(matching)}</p></section>
<section id="performance"><h2>Matched model–context performance</h2>
<figure>{charts["model-context-heatmap.svg"]}<figcaption>Every arm uses the same complete-task cohort.</figcaption></figure>
{matrix_table(matched)}
<figure>{charts["token-efficiency.svg"]}<figcaption>{escape(best_note)} C1–C3 describe the initial pack. Totals are whole-session tokens.</figcaption></figure>
<details><summary>Available observations by arm</summary><p>The task sets in this table can differ by arm. The coverage grid lists each arm’s contributing tasks.</p>{matrix_table(study["available_matrix"])}</details></section>
<section id="routing"><h2>Held-out routing replay</h2><p class="note">{escape(empirical_note)}</p>
<p>Fixed baseline <code>{escape(study["baseline"]["model"])}</code> at C2. Model-only can change the model at C2. Context-only can change Big Pickle’s context. Joint can choose any of the nine arms.</p>
<p>Each fold excludes the entire held-out task from optimizer evidence. The unmodified optimizer requires at least {adaptive["training_minimum"]} prior tasks per arm, a Wilson lower success bound of {adaptive["wilson_minimum"]:.2f} or higher, and usable cost evidence. This split needs at least {adaptive["training_minimum"] + 1} complete tasks before a fold can qualify. Its default fallback retains the original model at a compatible C2.</p>
<figure>{charts["routing-policy-comparison.svg"]}</figure>{policy_table(adaptive["policies"])}
<figure>{charts["tokens-vs-success.svg"]}<figcaption>The axes use measured tokens and test verification from the chosen arms. Dollar estimates appear in the tables.</figcaption></figure>
<details open><summary>Per-task decision trace</summary>{trace}</details></section>
<section id="interactions"><h2>Model–context interactions</h2>
<p class="note">The measured ordering changes with tier. Evidence for a systematic interaction is inconclusive with a single execution per arm.</p>
{table(["Tier", "Mean token ordering, lowest first", "Verified successes"], rankings) if rankings else ""}
<p>There are {len(tokens_r)} pairwise token-ranking reversals across complete tasks, including {same_count} with identical context text. A reversal with the same pack shows run-to-run variation under the same injected context. Reversals with changed packs also need replication to separate a context effect from run-to-run variation.</p>
<figure>{charts["interaction-reversals.svg"]}</figure>{reversal_table(reversals)}
<p>Among {distinct_increases} matched within-model comparisons with a larger injected pack, {improvements} used fewer whole-session tokens. There were {correctness} within-model verification changes across matched tier pairs.</p>
<details><summary>Within-model changes in verification or tokens</summary>{paired_examples(paired)}</details></section>
<section id="oracle"><h2>Oracle comparison</h2><p>{escape(oracle["objective"])} {escape(oracle_note)}</p>
<p>The oracle uses held-out outcomes after the fact to estimate headroom in this particular grid. Its scores describe selection with perfect knowledge of these executions.</p>
{policy_table(oracle["policies"], oracle=True)}</section>
<section id="methods"><h2>Methods and limitations</h2>
<p>Fixture commit <code>{escape(audit["repository_commit"])}</code>. Completed records were checked against task prompts, the verifier fingerprint, pinned runtime protocol, exact primary model, title trace, injected pack text, and six real acceptance checks. Reference edits calibrate the verifier outside the agent checkout.</p>
<p>All attempts use OpenCode’s standalone benchmark mode. The runner denies shell, subagent, and outside-directory access. Acceptance tests use a disposable verification copy with pristine fixtures. The audited context-generation modules are byte-identical between the two pinned runtimes.</p>
<ul>{diffs}<li>Tokens are provider-reported whole-session counts including titles. Tokenizers can differ across models.</li>
<li>Cost uses saved OpenCode catalog estimates. The hosted free SKUs were recorded at $0. Gemini reports estimated list-price usage. Actual billing is unavailable.</li>
<li>Replicate each affected model–tier pair on the same immutable fixture, including the identical-pack controls, before interpreting reversals as a context effect.</li>
<li>The complete cases come from one synthetic repository and all arms passed on these tasks. Daily Report includes valid failures outside that cohort.</li></ul>
<p><a href="evaluation.json">Evaluation and provenance JSON</a> · <a href="decision-trace.json">Decision trace JSON</a> · <a href="oracle.json">Oracle JSON</a></p>
</section></main></body></html>
"""


def export(study, directory):
    interaction = study["interaction"]
    charts = {
        "model-context-heatmap.svg": heatmap(study["matched_matrix"]),
        "token-efficiency.svg": efficiency(study["matched_matrix"], len(study["complete_tasks"])),
        "routing-policy-comparison.svg": decisions_chart(study["empirical_replay"]["policies"]),
        "tokens-vs-success.svg": tokens_vs_success(study["empirical_replay"]["policies"]),
        "interaction-reversals.svg": interaction_chart(study["complete_tasks"], interaction["ranking_reversals"]),
    }
    for filename, chart in charts.items():
        (directory / filename).write_text(chart + "\n", encoding="utf-8")
    (directory / "report.html").write_text(report(study, charts), encoding="utf-8")
    atomic_json(directory / "decision-trace.json", study["empirical_replay"])
    atomic_json(directory / "oracle.json", study["oracle"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=RESULTS / "cross-model-study")
    args = parser.parse_args(argv)
    export(load_json(args.output / "evaluation.json"), args.output)
    print(args.output / "report.html")


if __name__ == "__main__":
    main()

