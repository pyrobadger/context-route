"""Audit and replay the three-model ContextRoute benchmark from saved observations."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
from itertools import combinations, product
from pathlib import Path
import re
from statistics import median
from tempfile import TemporaryDirectory

from benchmark.evaluator.evaluate import POLICIES, metrics, read_runs, replay
from benchmark.evaluator.study import (
    FALLBACK_TYPES, FIXED_CONTEXT, MIN_SUCCESS_LOWER_BOUND, MIN_TRAINING_TASKS, TIERS, audit_finished,
)
from benchmark.runner.run import (
    GEMINI_MODEL_KEY, GEMINI_TITLE_MODEL, RESULTS, TASKS, Configuration, atomic_json, events_from,
    fixture_repository, gemini_request_accounting, load_json, parse_args, protocol_key,
)

FIXED_MODEL = "opencode/big-pickle"
DEEPSEEK = "opencode/deepseek-v4.1-flash-free"
MODELS = (FIXED_MODEL, DEEPSEEK, GEMINI_MODEL_KEY)
ARMS = tuple(product(MODELS, TIERS))
CHECK_COUNT = 6
VERSION = "opencode v2.2.7"
SOURCES = {"grid": (FIXED_MODEL, DEEPSEEK), "gemini-pilot": (GEMINI_MODEL_KEY,)}
POLICY_ARMS = {
    "fixed": ((FIXED_MODEL, FIXED_CONTEXT),),
    "model_only": tuple((model, FIXED_CONTEXT) for model in MODELS),
    "context_only": tuple((FIXED_MODEL, tier) for tier in TIERS),
    "joint": ARMS,
}


def observed_metrics(rows):
    summary = metrics(rows)
    totals = [row["total_tokens"] for row in rows]
    return {
        **summary,
        "average_tokens": summary["total_tokens"] / len(rows) if rows else None,
        "median_tokens": median(totals) if rows else None,
        "average_cost_usd": summary["total_cost_usd"] / len(rows) if summary["total_cost_usd"] is not None else None,
    }


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def execution_protocol(source, models, task_specs, commit):
    args = parse_args(["--models", *models, "--contexts", *TIERS, "--output", str(source)]
                      + (["--gemini-quota"] if GEMINI_MODEL_KEY in models else []))
    keys = {}
    for task, model, tier in product(task_specs, models, TIERS):
        cfg = Configuration("forced", models[0], model, tier)
        protocol = protocol_key(commit, task_specs[task], cfg, args, VERSION)
        protocols = {protocol: "current"}
        if args.gemini_quota:
            legacy = protocol_key(commit, task_specs[task], cfg, args, VERSION, legacy_gemini=True)
            protocols[legacy] = "legacy title selection, exact model verified in trace"
        keys[task, model, tier] = protocols
    return keys


def gemini_saved_calls(path, row):
    try:
        calls = load_json(path)
    except (OSError, ValueError):
        return False
    generated = [call for call in calls if call["kind"] == "generate"]
    quota = row["gemini_quota"]
    return (len(calls) == len({call["id"] for call in calls}) == quota["api_requests"]
            and Counter(call["kind"] for call in calls) == {
                "generate": quota["generation_requests"], "count_tokens": quota["token_count_requests"],
            }
            and all(call["attempt"] == row["execution_id"] and call["status"] == 200 for call in calls)
            and {call["model_version"] for call in generated} == set(quota["response_model_versions"])
            and all(all(type(call[field]) is int for call in generated)
                    and sum(call[field] for call in generated) == row[field]
                    for field in ("input_tokens", "output_tokens", "total_tokens")))


def audit_run(row, source, commit, protocols):
    if row["execution_status"] != "completed":
        return "infrastructure", "Execution status " + row["execution_status"], {}
    common_issue = audit_finished(row, source, commit)
    if common_issue:
        return "rejected", common_issue, {}
    protocol = protocols.get(row.get("protocol_key"))
    if protocol is None:
        return "rejected", "Task, fixture, verifier, or pinned runtime protocol could not be reproduced", {}

    verification = row["verification"]["summary"]
    counts = {label: int(count) for count, label in re.findall(r"(\d+) (passed|failed|errors?|skipped)", verification)}
    if (set(counts) - {"passed", "failed"} or sum(counts.values()) != CHECK_COUNT
            or (counts.get("failed", 0) == 0) != row["resolved"]):
        return "rejected", "Six acceptance results could not be verified", {}

    trace_path = source / "telemetry" / row["execution_id"] / "execution.jsonl"
    trace = events_from(trace_path)
    aux = [(e.get("request_kind"), e.get("actual_model")) for e in trace if e["type"] == "auxiliary_request"]
    model = row["model_id"]
    primary = row["primary_model_calls"]
    if (row["decision"].get("model_metadata", {}).get("key") != model
            or row["decision"].get("decision_source") != "forced"
            or row["decision"].get("original_model") != row.get("original_model")
            or row["decision"].get("selected_model") != model
            or row["decision"].get("applied_model") != model
            or row.get("token_scope") != "OpenCode cumulative session usage"
            or type(primary) is not int or primary < 1
            or row["auxiliary_model_calls"] != len(aux)):
        return "rejected", "Model, routing, usage, or call counts disagree with telemetry", {}
    if aux != [("title", GEMINI_TITLE_MODEL if model == GEMINI_MODEL_KEY else model)]:
        return "rejected", "Unexpected auxiliary title model", {}

    accounting = {
        "protocol_match": protocol, "title_model": aux[0][1], "primary_requests": primary,
        "auxiliary_requests": len(aux), "provider_request_count": None, "request_count_scope": "OpenCode logical calls",
        "provider_model_versions": None,
    }
    if model == GEMINI_MODEL_KEY:
        quota = row.get("gemini_quota") or {}
        if (row.get("quota_errors") or not gemini_request_accounting(trace, quota, primary, row)["verified"]
                or not gemini_saved_calls(trace_path.with_name("gemini-requests.json"), row)):
            return "rejected", "Google quota, model, or usage reconciliation failed", {}
        accounting.update(provider_request_count=quota["api_requests"], request_count_scope="Google physical calls",
                          provider_model_versions=quota["response_model_versions"])
    return "valid", None, accounting


def audit_sources(sources, task_specs, commit):
    attempted = defaultdict(list)
    candidates = defaultdict(list)
    attempts = []
    for label, source in sources.items():
        protocols = execution_protocol(source, SOURCES[label], task_specs, commit)
        rows = read_runs(source / "runs")
        sessions = Counter(row["session_id"] for row in rows if row.get("session_id"))
        for row in rows:
            cell = row["task_id"], row["expected_model"], row["context_id"]
            if cell not in protocols:
                continue
            state, reason, accounting = audit_run(row, source, commit, protocols[cell])
            if state == "valid" and sessions[row["session_id"]] != 1:
                state, reason = "rejected", "Session was reused across execution attempts"
            record = {
                "task": cell[0], "expected_model": cell[1], "model": row["model_id"], "tier": cell[2],
                "repository_commit": row["repository_commit"], "execution_id": row["execution_id"],
                "context_hash": row["context_hash"], "verification": row["verification"],
                "execution_status": row["execution_status"], "execution_validity": state, "reason": reason,
                "provider_statuses": row.get("provider_statuses", []), "source": label,
                "record": f"runs/{row['execution_id']}.json",
                "record_sha256": digest(source / "runs" / f"{row['execution_id']}.json"),
            }
            if model_accounting := row.get("gemini_request_accounting"):
                record["saved_gemini_request_accounting"] = model_accounting
            if quota := row.get("gemini_quota"):
                record["saved_gemini_usage"] = {field: quota[field] for field in (
                    "api_requests", "generation_requests", "token_count_requests", "generation_requests_with_usage",
                    "observed_input_tokens", "observed_output_tokens", "observed_total_tokens", "response_model_versions",
                )}
            attempts.append(record)
            attempted[cell].append(record)
            if state == "valid":
                candidates[cell].append((row, record, accounting))

    available = {}
    for cell, verified in sorted(candidates.items()):
        if len(verified) == 1:
            row, record, accounting = verified[0]
            record.update(accounting)
            available[cell] = row
        else:
            for _, record, _ in verified:
                record.update(execution_validity="ambiguous", reason="Multiple valid records for the same task and arm")
    return available, attempted, attempts


def match_tasks(available, tasks):
    complete = {}
    mismatches = []
    for task in tasks:
        cells = {arm: available[task, *arm] for arm in ARMS if (task, *arm) in available}
        design = {(row["repository_id"], row["repository_commit"], row["opencode_version"], row["retrieval_version"])
                  for row in cells.values()}
        conflict = len(design) > 1
        if conflict:
            mismatches.append({"task": task, "kind": "execution design", "designs": sorted(design)})
        for tier in TIERS:
            hashes = {model: cells[model, tier]["context_hash"] for model in MODELS if (model, tier) in cells}
            if len(set(hashes.values())) > 1:
                mismatches.append({"task": task, "tier": tier, "kind": "context hash", "model_hashes": hashes})
                conflict = True
        if len(cells) == len(ARMS) and not conflict:
            complete[task] = cells
    return complete, mismatches


def availability(tasks, available, attempted):
    result = []
    for task, (model, tier) in product(tasks, ARMS):
        cell = task, model, tier
        row = available.get(cell)
        records = attempted.get(cell, [])
        record = next((r for r in records if row and r["execution_id"] == row["execution_id"]), None)
        result.append({
            "task": task, "model": model, "tier": tier,
            "status": "verified pass" if row and row["resolved"] else "verified failure" if row
                      else "missing (attempted)" if records else "missing (unattempted)",
            "attempts": len(records), "observation": None if row is None else {
                **record, **{field: row.get(field) for field in (
                    "opencode_version", "retrieval_version", "resolved", "initial_context_tokens", "input_tokens",
                    "output_tokens", "total_tokens", "latency_seconds", "wall_seconds", "estimated_list_price_usd",
                )},
            },
        })
    return result


def arm_matrix(grids):
    return [{"model": model, "context": tier,
             **observed_metrics([grid[model, tier] for grid in grids.values() if (model, tier) in grid])}
            for model, tier in ARMS]


def strict_winner(a, b, left, right, *, higher=False):
    if a == b:
        return None
    return left if (a > b if higher else a < b) else right


def interaction_analysis(complete, available):
    rankings = []
    for tier in TIERS:
        if complete:
            model_metrics = {model: observed_metrics([grid[model, tier] for grid in complete.values()]) for model in MODELS}
            rankings.append({"tier": tier, "mean_tokens": {m: r["average_tokens"] for m, r in model_metrics.items()},
                             "verified_successes": {m: r["successes"] for m, r in model_metrics.items()}})
    reversals = []
    for task, grid in complete.items():
        for (left, right), (low, high) in product(combinations(MODELS, 2), combinations(TIERS, 2)):
            for field in ("total_tokens", "resolved"):
                values = [[grid[model, tier][field] for model in (left, right)] for tier in (low, high)]
                winners = [strict_winner(*pair, left, right, higher=field == "resolved") for pair in values]
                if all(winners) and winners[0] != winners[1]:
                    reversals.append({
                        "task": task, "models": [left, right], "tiers": [low, high], "metric": field,
                        "values": values, "winners": winners, "identical_packs":
                        grid[left, low]["context_hash"] == grid[left, high]["context_hash"],
                    })

    paired_tiers = []
    for task, model in sorted({(task, model) for task, model, _ in available}):
        for low, high in combinations(TIERS, 2):
            before, after = available.get((task, model, low)), available.get((task, model, high))
            if before is None or after is None:
                continue
            initial_delta = after["initial_context_tokens"] - before["initial_context_tokens"]
            total_delta = after["total_tokens"] - before["total_tokens"]
            same_pack = before["context_hash"] == after["context_hash"]
            paired_tiers.append({
                "task": task, "model": model, "tiers": [low, high], "strictly_matched_task": task in complete,
                "identical_packs": same_pack, "initial_token_delta": initial_delta, "total_token_delta": total_delta,
                "resolved": [before["resolved"], after["resolved"]],
                "larger_pack_used_fewer_tokens": not same_pack and initial_delta > 0 and total_delta < 0,
                "outcome_changed": before["resolved"] != after["resolved"],
            })
    return {"tier_rankings": rankings, "ranking_reversals": reversals, "paired_tier_observations": paired_tiers,
            "interaction_evidence": "inconclusive"}


def picked_outcome(row):
    return {field: row[field] for field in ("execution_id", "model_id", "context_id", "resolved", "total_tokens",
                                           "latency_seconds", "estimated_list_price_usd")}


def compare_to_fixed(policies):
    fixed = policies["fixed"]["total_tokens"]
    for summary in policies.values():
        spent = summary["total_tokens"]
        summary["token_delta_vs_fixed"] = spent - fixed if fixed is not None and spent is not None else None


def empirical_analysis(complete):
    folds = [{"held_out": task, "training_tasks": [other for other in complete if other != task]} for task in complete]
    verdict = {
        "split": "leave-one-task-out", "folds": folds, "training_minimum": MIN_TRAINING_TASKS,
        "wilson_minimum": MIN_SUCCESS_LOWER_BOUND, "policy_version": "default-tier-fallback-v2",
        "status": "insufficient_complete_grids", "policies": {}, "decisions": [],
    }
    if len(complete) < 2:
        return verdict
    result = replay([row for grid in complete.values() for row in grid.values()], FIXED_MODEL, FIXED_CONTEXT,
                    MODELS, TIERS, fallback_context=FIXED_CONTEXT)
    if not result["policies"]:
        verdict["status"] = result["replay_status"]
        return verdict
    if result["policy_version"] != verdict["policy_version"]:
        raise RuntimeError("Built optimizer does not implement the required fallback policy")
    for policy in POLICIES:
        outcomes, sources = [], Counter()
        for selected in result["policies"][policy]["decisions"]:
            task = selected["task"]
            decision = selected["decision"]
            candidate = decision["selected"]
            row = complete[task][candidate["model"], candidate["context"]] if candidate else None
            source = ("fixed_baseline" if policy == "fixed" and row else
                      "fallback" if decision["decision_type"] in FALLBACK_TYPES else decision["decision_type"])
            sources[source] += 1
            if row:
                outcomes.append(row)
            verdict["decisions"].append({
                "task": task, "policy": policy, "original_model": FIXED_MODEL, "decision_source": source,
                "optimizer_decision": decision["decision_type"] if policy != "fixed" else None,
                "fallback_reason": decision.get("fallback_reason") if source == "fallback" else None,
                "training_tasks": selected["training_tasks"], "evidence_count": candidate["evidence_count"] if candidate else None,
                "wilson_lower_bound": candidate["success_lower_bound"] if candidate else None,
                "selected_outcome": picked_outcome(row) if row else None,
                "alternatives": [entry for entry in decision["alternatives"] if entry["constraints_met"]],
            })
        verdict["policies"][policy] = {
            **observed_metrics(outcomes), "held_out_tasks": len(complete), "abstentions": len(complete) - len(outcomes),
            "fixed_decisions": sources["fixed_baseline"], "empirical_decisions": sources["empirical"],
            "fallback_decisions": sources["fallback"],
        }
    compare_to_fixed(verdict["policies"])
    verdict["status"] = ("empirical_decisions_observed" if verdict["policies"]["joint"]["empirical_decisions"] else
                         "fallback_only_insufficient_evidence")
    return verdict


def oracle_analysis(complete):
    traces = []
    summaries = {}
    for policy, arms in POLICY_ARMS.items():
        selected = []
        for task, grid in complete.items():
            arm = min(arms, key=lambda arm: (not grid[arm]["resolved"], grid[arm]["total_tokens"],
                                            arm != (FIXED_MODEL, FIXED_CONTEXT), arm))
            row = grid[arm]
            selected.append(row)
            traces.append({"task": task, "policy": policy,
                           "selection_basis": "fixed" if policy == "fixed" else "observed outcome and token usage",
                           "selected_outcome": picked_outcome(row)})
        summaries[policy] = observed_metrics(selected)
    compare_to_fixed(summaries)
    return {"objective": "Prefer verified success, then minimize observed total tokens. Ties retain the fixed arm if available.",
            "policies": summaries, "selections": traces}


def protocol_audit(sources, task_specs, commit):
    runtimes = {
        label: {p.relative_to(root / "benchmark-runtime").as_posix(): digest(p)
                for p in sorted((root / "benchmark-runtime" / "dist").rglob("*.js"))}
        for label, root in sources.items()
    }
    common = set.intersection(*(set(files) for files in runtimes.values()))
    identical = sorted(path for path in common if len({files[path] for files in runtimes.values()}) == 1)
    different = sorted(set.union(*(set(files) for files in runtimes.values())) - set(identical))
    if not common or any("/context/" in path or "/task-analysis/" in path for path in different):
        raise RuntimeError("Pinned context-generation implementations could not be matched")
    return {
        "repository_commit": commit, "opencode_version": VERSION, "verifier_sha256": digest(TASKS / "verification.py"),
        "reference_edits_sha256": digest(TASKS / "reference_edits.json"), "task_specs_sha256": digest(TASKS / "tasks.json"),
        "tasks": {task: {"prompt_sha256": sha256(spec["prompt"].encode()).hexdigest(), "verifier": spec["verify"]}
                  for task, spec in task_specs.items()},
        "source_directories": {label: root.resolve().as_posix() for label, root in sources.items()},
        "runtime_sha256_by_source": runtimes, "identical_runtime_files": identical, "differing_runtime_files": different,
        "verification_command": "python -B -m pytest -q tests contextroute_acceptance.py::<task verifier>",
        "shared_settings": ["OpenCode 2.2.7", "clean disposable fixture checkout", "forced candidate arms",
                            "standalone archived auto mode", "shell, external_directory, and subagent denied by runner"],
        "provider_differences": [
            "Gemini adds a quota gateway, metered countTokens calls, and a same-model #minimal title variant.",
            "The OpenCode-hosted models use native transport and same-model titles; physical request counts are unavailable.",
            "Pinned plugin files differ in configuration, forced-run hooks and guards, and the historical fallback/replay code. "
            "Forced mode takes the named arm directly. Context compiler/retrieval bytes and applied pack hashes are audited.",
            "Effective user-level permissions, provider serving revisions for hosted models, and historical timing controls "
            "cannot be independently reconstructed from saved traces. Gemini gateway waits can affect latency.",
        ],
    }


def analyze(sources, task_specs, commit):
    available, attempted, attempts = audit_sources(sources, task_specs, commit)
    complete, mismatches = match_tasks(available, task_specs)
    available_grids = defaultdict(dict)
    for (task, model, tier), row in available.items():
        available_grids[task][model, tier] = row
    return {
        "study": "ContextRoute three-model comparison", "baseline": {"model": FIXED_MODEL, "tier": FIXED_CONTEXT},
        "models": list(MODELS), "tiers": list(TIERS), "task_ids": list(task_specs), "complete_tasks": list(complete),
        "protocol_audit": protocol_audit(sources, task_specs, commit), "comparability_issues": mismatches,
        "availability": availability(task_specs, available, attempted), "attempts": attempts,
        "coverage": {model: {"valid": sum(cell[1] == model for cell in available),
                             "successes": sum(row["resolved"] for cell, row in available.items() if cell[1] == model),
                             "planned": len(task_specs) * len(TIERS)} for model in MODELS},
        "available_matrix": arm_matrix(available_grids), "matched_matrix": arm_matrix(complete),
        "interaction": interaction_analysis(complete, available),
        "empirical_replay": empirical_analysis(complete), "oracle": oracle_analysis(complete),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid", type=Path, default=RESULTS / "grid")
    parser.add_argument("--gemini", type=Path, default=RESULTS / "gemini-pilot")
    parser.add_argument("--output", type=Path, default=RESULTS / "cross-model-study")
    args = parser.parse_args(argv)
    if any(args.output.resolve().is_relative_to(source.resolve()) for source in (args.grid, args.gemini)):
        parser.error("The analysis must be stored outside both source directories.")
    task_specs = {task["id"]: task for task in load_json(TASKS / "tasks.json")[:8]}
    with TemporaryDirectory(prefix="contextroute-cross-study-") as directory:
        commit = fixture_repository(Path(directory) / "fixture")
    study = analyze({"grid": args.grid, "gemini-pilot": args.gemini}, task_specs, commit)
    atomic_json(args.output / "evaluation.json", study)
    print(f"{len(study['complete_tasks'])} complete tasks; {study['empirical_replay']['status']}")


if __name__ == "__main__":
    main()

