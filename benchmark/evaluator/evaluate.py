"""Replay ContextRoute policies on complete held-out task matrices."""

from __future__ import annotations

import argparse
from collections import defaultdict
from itertools import combinations
import json
from pathlib import Path
from statistics import mean, median

from benchmark.runner.run import ROOT, RESULTS, atomic_json, load_json

POLICIES = {"fixed": "Fixed", "model_only": "Model-only", "context_only": "Context-only", "joint": "Joint"}


def read_runs(directory: Path) -> list[dict]:
    return [load_json(path) for path in sorted(directory.glob("*.json"))]


def task_key(row):
    return row["repository_id"], row["repository_commit"], row["task_id"]


def eligible(row):
    return (row.get("mode") == "forced" and row.get("execution_status") == "completed"
            and row.get("verification_type") in {"task_tests", "issue_verified"} and isinstance(row.get("resolved"), bool)
            and row.get("context_wire_verified") is True and row.get("context_hash") and row.get("retrieval_version")
            and row.get("decision", {}).get("model_metadata"))


def strict_mean(values):
    return mean(values) if values and all(value is not None for value in values) else None


def strict_sum(values):
    return sum(values) if values and all(value is not None for value in values) else None


def metrics(rows):
    count = len(rows)
    successes = sum(row["resolved"] is True for row in rows)
    cost = strict_sum([row.get("estimated_list_price_usd") for row in rows])
    tokens = strict_sum([row.get("total_tokens") for row in rows])
    latencies = [row.get("latency_seconds") for row in rows]
    return {
        "tasks": count, "successes": successes, "success_rate": successes / count if count else None,
        "input_tokens": strict_sum([row.get("input_tokens") for row in rows]),
        "output_tokens": strict_sum([row.get("output_tokens") for row in rows]), "total_tokens": tokens,
        "total_cost_usd": cost, "mean_latency_seconds": strict_mean(latencies),
        "median_latency_seconds": median(latencies) if count and None not in latencies else None,
        "cost_per_success_usd": cost / successes if cost is not None and successes else None,
        "successes_per_million_tokens": successes * 1_000_000 / tokens if tokens else None,
        "initial_context_tokens": strict_sum([row.get("initial_context_tokens") for row in rows]),
    }


def matrix(rows):
    grids = defaultdict(dict)
    for row in sorted(rows, key=lambda value: value["execution_id"]):
        if eligible(row):
            design = (row.get("opencode_version"), row["retrieval_version"], *task_key(row))
            grids[design].setdefault((row["model_id"], row["context_id"]), row)
    return grids


def reverse_ranks(grids, pairs):
    changes = []
    for left, right in combinations(sorted({model for model, _ in pairs}), 2):
        for first, second in combinations(sorted({context for _, context in pairs}), 2):
            shared = [grid for grid in grids if all((model, context) in grid for model in (left, right) for context in (first, second))]
            if not shared:
                continue
            first_diff, second_diff = [
                sum(int(grid[left, context]["resolved"]) - int(grid[right, context]["resolved"]) for grid in shared)
                for context in (first, second)]
            if first_diff * second_diff < 0:
                changes.append({"models": [left, right], "contexts": [first, second], "common_tasks": len(shared),
                                "success_count_differences": [first_diff, second_diff]})
    return changes


def matrix_summary(rows):
    pairs = defaultdict(list)
    for grid in matrix(rows).values():
        for pair, row in grid.items():
            pairs[pair].append(row)
    return [{"model": pair[0], "context": pair[1], **metrics(pairs[pair])} for pair in sorted(pairs)]


def execute_replay(inputs):
    import subprocess
    result = subprocess.run(["node", str(ROOT / "dist" / "router" / "replay.js")], cwd=ROOT,
                            input=json.dumps(inputs), capture_output=True, text=True, encoding="utf-8", timeout=60)
    if result.returncode:
        raise RuntimeError("Build the TypeScript optimizer and check replay inputs. " + result.stderr.strip())
    return json.loads(result.stdout)


def replay(rows, fixed_model, fixed_context="C2", models=None, contexts=None, constraints=None, fallback_context="C2"):
    grids = matrix(rows)
    pairs = {(model, context) for grid in grids.values() for model, context in grid}
    models = sorted(set(models or [pair[0] for pair in pairs]))
    contexts = sorted(set(contexts or [pair[1] for pair in pairs]))
    required = {(model, context) for model in models for context in contexts}
    full = {task: grid for task, grid in grids.items() if required and required <= grid.keys()}
    usable_protocols = {(task[0], task[1]) for task in full}
    repositories = {task[2] for task in full}
    split = "leave-one-repository-out" if len(repositories) > 1 else "leave-one-task-out synthetic pilot"
    result = {"split": split, "complete_tasks": len(full), "incomplete_tasks": len(grids) - len(full),
              "attempted_runs": len(rows), "ineligible_runs": sum(not eligible(row) for row in rows),
              "matrix": matrix_summary(rows), "rank_reversals": reverse_ranks(list(full.values()), required),
              "policies": {}, "replay_status": "insufficient_matrix"}
    if len(usable_protocols) > 1:
        result["replay_status"] = "mixed_opencode_or_retrieval_versions"
        return result
    if len(full) < 2 or (fixed_model, fixed_context) not in required:
        return result

    inputs, queries = [], []
    for task, grid in sorted(full.items()):
        prototype = grid[fixed_model, fixed_context]["decision"]
        model_info = {model: grid[model, fixed_context]["decision"]["model_metadata"] for model in models}
        pack_info = {context: {"files": [], "text": "", **pack} for pack in prototype["packs"]
                     if (context := pack["context_id"]) in contexts}
        examples = [row for other, other_grid in full.items() if
                    (other[2] != task[2] if len(repositories) > 1 else (other[2], other[4]) != (task[2], task[4]))
                    for pair, row in other_grid.items() if pair in required]
        training_tasks = len({(row["repository_id"], row["task_id"]) for row in examples})
        for policy in POLICIES:
            candidate_models = models if policy in {"model_only", "joint"} else [fixed_model]
            candidate_contexts = contexts if policy in {"context_only", "joint"} else [fixed_context]
            inputs.append({
                "models": [model_info[model] for model in candidate_models],
                "packs": [pack_info[context] for context in candidate_contexts],
                "evidence": examples, "task": prototype["task_features"], "repository": prototype.get("repository"),
                "original": fixed_model, "constraints": constraints or {}, "fallbackContext": fallback_context,
            })
            queries.append((policy, task, grid, training_tasks))
    decisions = execute_replay(inputs)
    result["policy_version"] = decisions[0]["policy_version"]
    result["fallback_context"] = fallback_context
    selections = defaultdict(list)
    all_decisions = defaultdict(list)
    for (policy, task, grid, training_tasks), decision in zip(queries, decisions, strict=True):
        selected = decision["selected"]
        if selected:
            selections[policy].append(grid[selected["model"], selected["context"]])
        all_decisions[policy].append({"task": task[4], "repository": task[2], "training_tasks": training_tasks,
                                      "decision": decision})
    result["replay_status"] = "ok"
    for policy in POLICIES:
        observed = selections[policy]
        policy_decisions = all_decisions[policy]
        result["policies"][policy] = {**metrics(observed), "tasks_requested": len(full),
            "abstentions": len(full) - len(observed),
            "cold_start_fallbacks": sum(row["decision"].get("fallback_reason") == "no_usable_evidence" for row in policy_decisions),
            "insufficient_evidence_fallbacks": sum(row["decision"].get("fallback_reason") == "insufficient_evidence" for row in policy_decisions),
            "mean_recorded_router_overhead_ms": strict_mean([row.get("router_overhead_ms") for row in observed]),
            "decisions": policy_decisions}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, default=RESULTS / "runs")
    parser.add_argument("--fixed-model")
    parser.add_argument("--fixed-context", default="C2")
    parser.add_argument("--fallback-context", choices=("C1", "C2", "C3", "L1", "L2", "L3"), default="C2")
    parser.add_argument("--models", nargs="+")
    parser.add_argument("--contexts", nargs="+")
    parser.add_argument("--constraints", type=json.loads, default={})
    parser.add_argument("--output", type=Path, default=RESULTS / "evaluation.json")
    args = parser.parse_args(argv)
    rows = read_runs(args.runs)
    fixed = args.fixed_model or next((row["original_model"] for row in rows if row.get("original_model")), None)
    result = replay(rows, fixed, args.fixed_context, args.models, args.contexts, args.constraints, args.fallback_context)
    atomic_json(args.output, result)
    print(f"{result['replay_status']}: {result['complete_tasks']} complete task matrices, {result['split']}")


if __name__ == "__main__":
    main()

