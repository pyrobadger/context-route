"""Audit the provider grid and add paired analyses to the existing held-out replay."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
from itertools import combinations
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark.evaluator.evaluate import POLICIES, metrics, read_runs, replay, strict_mean
from benchmark.runner.run import (
    ROOT, RESULTS, TASKS, atomic_json, events_from, fixture_repository, last_event, load_json, run_process,
)

MODELS = ("opencode/big-pickle", "opencode/deepseek-v4.1-flash-free")
TIERS = ("C1", "C2", "C3")
FIXED_CONTEXT = "C2"
# The grid study uses rankPairs' default constraints without overrides.
MIN_TRAINING_TASKS = 4
MIN_SUCCESS_LOWER_BOUND = 0.50
FALLBACK_TYPES = {"cold_start", "insufficient_evidence", "cold_start_fallback"}


def key(row):
    return row["task_id"], row["expected_model"], row["context_id"]


def audit_finished(row, source, commit):
    if row.get("repository_id") != "contextroute-shop" or row["repository_commit"] != commit:
        return "Wrong fixture revision"
    if row.get("mode") != "forced" or row.get("opencode_version") != "opencode v2.2.7":
        return "Wrong run mode or OpenCode version"
    if row.get("model_id") != row["expected_model"]:
        return "Wrong observed model"
    if type(row.get("resolved")) is not bool:
        return "Missing verification outcome"
    verification = row.get("verification") or {}
    if (row.get("verification_type") != "task_tests" or verification.get("passed") is not row["resolved"]
        or verification.get("status") != ("passed" if row["resolved"] else "failed")):
        return "Verification inconsistent with the record"
    if (not isinstance(row.get("total_tokens"), int) or row["total_tokens"] <= 0
        or not isinstance(row.get("latency_seconds"), (int, float)) or row["latency_seconds"] <= 0
        or row["total_tokens"] != (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0)):
        return "Missing or inconsistent usage or latency"

    trace = source / "telemetry" / row["execution_id"]
    try:
        pack = load_json(trace / "context.json")
        events = events_from(trace / "execution.jsonl")
        loaded = last_event(events, "plugin_loaded")
        route = last_event(events, "route")
        primary = last_event(events, "first_primary_request")
        wire = last_event(events, "primary_http_request")
        usage = last_event(events, "usage")
        end = last_event(events, "execution")
        fingerprint = sha256(pack["text"].encode()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        return "Missing or damaged raw trace"
    checkout = Path(loaded.get("directory", ""))
    if checkout.name != "checkout" or not checkout.parent.name.startswith("contextroute-run-"):
        return "Plugin location is outside the disposable checkout"
    if route.get("repository_commit") != commit or route.get("context_id") != row["context_id"]:
        return "Route revision or tier disagrees with the expected cell"
    if (row.get("context_wire_verified") is not True or wire.get("context_verified") is not True
        or pack.get("context_id") != row["context_id"] or pack.get("actual_tokens") != row["initial_context_tokens"]
        or pack.get("retrieval_version") != row.get("retrieval_version")
        or not fingerprint == row.get("context_hash") == pack.get("context_hash") == primary.get("context_hash")
        == wire.get("context_hash") == route.get("context_hash") == end.get("context_hash")):
        return "Exact context cannot be verified"
    if (not row["model_id"] == primary.get("actual_model") == wire.get("actual_model") == end.get("actual_model")
        or primary.get("session_id") != row["session_id"] or usage.get("session_id") != row["session_id"]
        or usage.get("total_tokens") != row["total_tokens"] or end.get("execution_status") != "completed"
        or end.get("final_model_finish") != "stop" or last_event(events, "integration_error")):
        return "Provider or session trace disagrees with the record"
    return None


def audit(rows, source, tasks, commit):
    expected = {(task, model, tier) for task in tasks for model in MODELS for tier in TIERS}
    attempts = defaultdict(list)
    issues = []
    for row in rows:
        cell = key(row)
        if cell not in expected:
            issues.append({"cell": cell, "execution_id": row["execution_id"], "reason": "Outside the planned grid"})
            continue
        attempts[cell].append(row)

    valid = {}
    sessions = Counter(row.get("session_id") for row in rows if row.get("session_id"))
    for cell, cell_rows in sorted(attempts.items()):
        verified = []
        for row in cell_rows:
            if row["execution_status"] != "completed":
                issues.append({"cell": cell, "execution_id": row["execution_id"],
                    "reason": row.get("integration_error") or row.get("provider_statuses") or row["execution_status"]})
                continue
            reason = audit_finished(row, source, commit)
            if not reason and sessions[row["session_id"]] != 1:
                reason = "OpenCode session reused across attempts"
            if reason:
                issues.append({"cell": cell, "execution_id": row["execution_id"], "reason": reason})
            else:
                verified.append(row)
        if len(verified) == 1:
            valid[cell] = verified[0]
        elif verified:
            issues.append({"cell": cell, "execution_id": None, "reason": f"{len(verified)} valid records for one planned cell"})
    for task in tasks:
        for tier in TIERS:
            available = [valid[(task, model, tier)] for model in MODELS if (task, model, tier) in valid]
            if len({row["context_hash"] for row in available}) > 1:
                for row in available:
                    issues.append({"cell": key(row), "execution_id": row["execution_id"], "reason": "Models received different packs for this task and tier"})
                    valid.pop(key(row))
    verified_rows = list(valid.values())
    failures = Counter(row["execution_status"] for row in rows if row["execution_status"] != "completed")
    summary = {
        "tasks": len(tasks), "configurations_per_task": len(MODELS) * len(TIERS),
        "expected_cells": len(expected), "valid_runs": len(valid),
        "verified_successes": sum(row["resolved"] for row in verified_rows),
        "valid_task_failures": sum(not row["resolved"] for row in verified_rows),
        "failed_infrastructure_runs": failures["failed"],
        "incomplete_runs": sum(failures.values()) - failures["failed"],
        "other_rejected_attempts": len(rows) - len(verified_rows) - sum(failures.values()),
        "missing_cells": sorted(expected - valid.keys()), "unattempted_cells": sorted(expected - attempts.keys()),
        "attempts": len(rows), "attempt_statuses": dict(Counter(row["execution_status"] for row in rows)),
        "valid_tokens": sum(row["total_tokens"] for row in verified_rows),
        "all_recorded_tokens": sum(row.get("total_tokens") or 0 for row in rows),
        "attempts_without_usage": sum(row.get("total_tokens") is None for row in rows),
        "provider_rate_limited_attempts": sum(429 in row.get("provider_statuses", []) for row in rows),
        "fixture_commit": commit, "issues": issues,
    }
    return valid, summary


def grid_matrix(valid):
    cells = []
    for model in MODELS:
        for tier in TIERS:
            rows = [row for (_, chosen, context), row in valid.items() if (chosen, context) == (model, tier)]
            cells.append({
                "model": model, "context": tier, **metrics(rows),
                "average_tokens": strict_mean([row["total_tokens"] for row in rows]),
                "average_context_tokens": strict_mean([row["initial_context_tokens"] for row in rows]),
                "average_cost_usd": strict_mean([row["estimated_list_price_usd"] for row in rows]),
            })
    return cells


def efficiency_winner(left, right, measure):
    if not left or not right or not (left["resolved"] and right["resolved"]):
        return None
    a, b = left.get(measure), right.get(measure)
    if a is None or b is None or a == b:
        return None
    return left["model_id"] if a < b else right["model_id"]


def interactions(valid, tasks):
    results = {}
    for task in tasks:
        smallest = {}
        changes, extra, equivalent = [], [], []
        outcomes = {}
        for model in MODELS:
            variants = [valid.get((task, model, tier)) for tier in TIERS]
            outcomes[model] = [None if row is None else {
                "tier": row["context_id"], "passed": row["resolved"], "total_tokens": row["total_tokens"],
                "initial_context_tokens": row["initial_context_tokens"], "context_hash": row["context_hash"],
                "latency_seconds": row["latency_seconds"],
            } for row in variants]
            if all(variants):
                smallest[model] = next((row["context_id"] for row in variants if row["resolved"]), None)
            for low, high in combinations([row for row in variants if row], 2):
                pair = {"model": model, "from": low["context_id"], "to": high["context_id"]}
                distinct = low["context_hash"] != high["context_hash"]
                if low["resolved"] != high["resolved"]:
                    changes.append(dict(pair, gained=high["resolved"], distinct_pack=distinct))
                if distinct and high["total_tokens"] > low["total_tokens"] and not (high["resolved"] and not low["resolved"]):
                    extra.append(dict(pair, extra_tokens=high["total_tokens"] - low["total_tokens"]))
                if not distinct:
                    equivalent.append(dict(pair, token_delta=high["total_tokens"] - low["total_tokens"]))
        winners = {measure: {tier: efficiency_winner(*(valid.get((task, model, tier)) for model in MODELS), measure)
                             for tier in TIERS} for measure in ("total_tokens", "estimated_list_price_usd")}
        distinct_reversals = []
        identical_reversals = []
        for left, right in combinations(TIERS, 2):
            a, b = winners["total_tokens"][left], winners["total_tokens"][right]
            if a and b and a != b:
                reversed_tiers = [left, right]
                if valid[task, a, left]["context_hash"] != valid[task, a, right]["context_hash"]:
                    distinct_reversals.append(reversed_tiers)
                else:
                    identical_reversals.append(reversed_tiers)
        results[task] = {
            "outcomes": outcomes, "smallest_passing_tier": smallest,
            "different_smallest_passing_tiers": len(smallest) == len(MODELS) and len(set(smallest.values())) > 1,
            "outcome_changes": changes, "extra_tokens_without_better_correctness": extra,
            "identical_pack_pairs": equivalent, "token_winner": winners["total_tokens"], "cost_winner": winners["estimated_list_price_usd"],
            "token_reversals_distinct_packs": distinct_reversals, "token_reversals_identical_packs": identical_reversals,
        }
    return results



def matched_efficiency(valid, tasks):
    common = [
        task for task in tasks if all(
            valid.get((task, model, tier), {}).get("resolved") is True for model in MODELS for tier in TIERS
        )
    ]
    by_tier = []
    for tier in TIERS:
        averages = {model: strict_mean([valid[task, model, tier]["total_tokens"] for task in common]) for model in MODELS}
        winner = min(MODELS, key=averages.get) if common and len(set(averages.values())) > 1 else None
        by_tier.append({"tier": tier, "mean_tokens": averages, "winner": winner})
    return {"all_six_pass_tasks": common, "tiers": by_tier}


def eligibility(candidate):
    blocked = list(candidate["exclusions"])
    if candidate["evidence_count"] < MIN_TRAINING_TASKS:
        blocked.append("Too few verified training tasks")
    bound = candidate["success_lower_bound"]
    if bound is None or bound < MIN_SUCCESS_LOWER_BOUND:
        blocked.append("Wilson lower bound below the minimum or unavailable")
    if candidate["average_cost_usd"] is None:
        blocked.append("Unknown mean whole-task list price")
    return blocked


def selected_rows(valid, result):
    rows = defaultdict(dict)
    original = {entry["task"]: entry["decision"]["alternatives"][0]["model"]
                for entry in result["policies"].get("fixed", {}).get("decisions", [])
                if entry["decision"]["alternatives"]}
    for policy, summary in result["policies"].items():
        for entry in summary["decisions"]:
            task = entry["task"]
            decision = entry["decision"]
            candidate = decision["selected"]
            row = valid[(task, candidate["model"], candidate["context"])] if candidate else None
            rows[task][policy] = {
                "model": candidate["model"] if candidate else None, "context": candidate["context"] if candidate else None,
                "resolved": row["resolved"] if row else None, "total_tokens": row["total_tokens"] if row else None,
                "latency_seconds": row["latency_seconds"] if row else None,
                "decision_type": "fixed_baseline" if policy == "fixed" and row else decision["decision_type"],
                "policy_version": decision["policy_version"], "original_model": original.get(task),
                "fallback_reason": decision.get("fallback_reason") if policy != "fixed" else None,
                "reasons": decision["reasons"] if policy != "fixed" else ["Fixed benchmark reference."],
                "estimated_total_tokens": candidate["average_tokens"] if candidate else None,
                "eligible_configurations": [{"model": item["model"], "context": item["context"],
                     "evidence_count": item["evidence_count"], "wilson_lower_bound": item["success_lower_bound"],
                     "empirical_exclusions": eligibility(item)}
                    for item in decision["alternatives"] if item["constraints_met"]],
                "training_tasks": entry["training_tasks"],
                "evidence_count": candidate["evidence_count"] if candidate else 0,
                "success_lower_bound": candidate["success_lower_bound"] if candidate else None,
                "initial_context_tokens": row["initial_context_tokens"] if row else None,
                "context_hash": row["context_hash"] if row else None,
                "execution_id": row["execution_id"] if row else None,
            }
    return dict(sorted(rows.items()))


def paired_difference(selections, candidate, reference):
    pairs = [(entry[candidate], entry[reference]) for entry in selections.values()
             if entry[candidate]["execution_id"] and entry[reference]["execution_id"]]
    base = sum(row["total_tokens"] for _, row in pairs)
    actual = sum(row["total_tokens"] for row, _ in pairs)
    return {
        "paired_tasks": len(pairs), "token_savings_pct": 100 * (base - actual) / base if base else None,
        "verified_success_delta": sum(int(row["resolved"]) - int(before["resolved"]) for row, before in pairs),
        "improved_tasks": [task for task, entry in selections.items() if entry[reference]["resolved"] is False and entry[candidate]["resolved"] is True],
        "regressions": [task for task, entry in selections.items() if entry[reference]["resolved"] is True and entry[candidate]["resolved"] is False],
        "both_pass_fewer_tokens": [task for task, entry in selections.items()
                                  if entry[reference]["resolved"] is entry[candidate]["resolved"] is True
                                  and entry[candidate]["total_tokens"] < entry[reference]["total_tokens"]],
    }



def empirical_replay(selections, valid):
    """Keep performance conditional on an empirical decision separate from the full replay."""
    by_id = {row["execution_id"]: row for row in valid.values()}
    folds = {task: options["joint"]["training_tasks"] for task, options in selections.items()}
    route_policies = [policy for policy in POLICIES if policy != "fixed"]
    counts = {
        policy: dict(Counter(options[policy]["decision_type"] for options in selections.values()))
        for policy in route_policies
    }
    joint_tasks = {
        task: options for task, options in selections.items()
        if options["joint"]["decision_type"] == "empirical" and options["fixed"]["execution_id"]
    }
    shared_tasks = {
        task: options for task, options in joint_tasks.items()
        if all(options[policy]["decision_type"] == "empirical" for policy in route_policies)
    }

    def cohort(tasks, policies):
        return {
            "tasks": sorted(tasks),
            "measurements": {
                policy: metrics([by_id[options[policy]["execution_id"]] for options in tasks.values()])
                for policy in policies
            } if tasks else {},
        }

    ready = sum(count >= MIN_TRAINING_TASKS for count in folds.values())
    joint_fixed = cohort(joint_tasks, ("fixed", "joint"))
    joint_fixed["difference"] = paired_difference(joint_tasks, "joint", "fixed")
    shared = cohort(shared_tasks, POLICIES)
    shared["joint_vs_model_only"] = paired_difference(shared_tasks, "joint", "model_only")
    status = ("insufficient_training_tasks" if not ready else "no_empirical_joint_selection"
              if not counts["joint"].get("empirical") else "evaluated")
    return {
        "status": status, "minimum_training_tasks": MIN_TRAINING_TASKS,
        "minimum_success_lower_bound": MIN_SUCCESS_LOWER_BOUND,
        "training_tasks_by_fold": folds, "minimum_count_folds": ready,
        "decision_counts": counts, "joint_empirical_vs_fixed": joint_fixed, "all_adaptive_empirical": shared,
    }


def fallback_audit(selections):
    audit = []
    for task, options in selections.items():
        fixed, joint, context = (options[name] for name in ("fixed", "joint", "context_only"))
        if not fixed["execution_id"] or joint["decision_type"] not in FALLBACK_TYPES:
            continue
        audit.append({
            "task": task, "original_model": fixed["model"], "fixed_context": fixed["context"],
            "joint_context": joint["context"], "context_only_context": context["context"],
            "identical_packs": fixed["context_hash"] == joint["context_hash"],
            "fixed_initial_tokens": fixed["initial_context_tokens"], "joint_initial_tokens": joint["initial_context_tokens"],
            "fixed_total_tokens": fixed["total_tokens"], "joint_total_tokens": joint["total_tokens"],
            "token_delta": joint["total_tokens"] - fixed["total_tokens"],
            "evidence_count": joint["evidence_count"], "success_lower_bound": joint["success_lower_bound"],
        })
    return audit


def coverage_table(rows, valid, tasks):
    attempts = defaultdict(list)
    for row in rows:
        attempts[key(row)].append(row)
    table = {}
    for task in tasks:
        cells = []
        for model in MODELS:
            for tier in TIERS:
                cell = (task, model, tier)
                accepted = valid.get(cell)
                seen = attempts[cell]
                if accepted:
                    status = "passed" if accepted["resolved"] else "task_failed"
                elif any(row["execution_status"] == "failed" for row in seen):
                    status = "infrastructure_failed"
                else:
                    status = "incomplete" if seen else "missing"
                cells.append({"model": model, "tier": tier, "status": status,
                              "attempt_ids": [row["execution_id"] for row in seen],
                              "provider_statuses": sorted({status for row in seen for status in row.get("provider_statuses", [])})})
        table[task] = cells
    return table


def analyze(rows, source, tasks, commit):
    valid, summary = audit(rows, source, tasks, commit)
    result = replay(list(valid.values()), MODELS[0], FIXED_CONTEXT, MODELS, TIERS)
    result["matrix"] = grid_matrix(valid)
    selections = selected_rows(valid, result)
    matched = {task: options for task, options in selections.items()
               if all(options[name]["execution_id"] for name in POLICIES)}
    by_id = {row["execution_id"]: row for row in valid.values()}
    result["study"] = {
        "execution": summary, "fixed_configuration": [MODELS[0], FIXED_CONTEXT],
        "coverage": coverage_table(rows, valid, tasks), "matched_policy_task_ids": sorted(matched),
        "interaction": interactions(valid, tasks), "selections": selections,
        "matched_efficiency": matched_efficiency(valid, tasks),
        "empirical_replay": empirical_replay(selections, valid), "fallback_audit": fallback_audit(selections),
        "comparisons_with_fixed": {policy: paired_difference(matched, policy, "fixed") for policy in POLICIES},
        "joint_vs_model_only": paired_difference(matched, "joint", "model_only"),
    }
    for name, policy in result["policies"].items():
        matched_choices = [options[name] for options in matched.values()]
        chosen = [by_id[option["execution_id"]] for option in matched_choices]
        policy.update(metrics(chosen))
        policy["empirical_decisions"] = sum(option["decision_type"] == "empirical" for option in matched_choices)
        policy["fallback_decisions"] = sum(option["decision_type"] in FALLBACK_TYPES for option in matched_choices)
        policy["outcomes_by_source"] = {
            source: metrics([by_id[option["execution_id"]] for option in matched_choices
                             if option["decision_type"] in sources])
            for source, sources in (("empirical", {"empirical"}), ("fallback", FALLBACK_TYPES))
        }
        policy["average_tokens"] = policy["total_tokens"] / policy["tasks"] if policy["tasks"] and policy["total_tokens"] is not None else None
        policy["token_savings_pct"] = result["study"]["comparisons_with_fixed"][name]["token_savings_pct"]
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=RESULTS / "grid")
    parser.add_argument("--limit", type=int, choices=range(1, 13), default=8)
    args = parser.parse_args(argv)
    tasks = [row["id"] for row in load_json(TASKS / "tasks.json")[:args.limit]]
    rows = read_runs(args.output / "runs")
    with TemporaryDirectory(prefix="contextroute-study-") as directory:
        commit = fixture_repository(Path(directory) / "fixture")
    result = analyze(rows, args.output, tasks, commit)
    if "policy_version" not in result:
        version = run_process(["node", str(ROOT / "dist" / "router" / "replay.js"), "--policy-version"], ROOT)
        if version.returncode:
            raise RuntimeError("Build the router before running the grid study.")
        result["policy_version"] = version.stdout.strip()
    receipt = args.output / "provider-availability.json"
    if receipt.is_file():
        result["study"]["provider_availability"] = load_json(receipt)
    atomic_json(args.output / "coverage.json", result["study"]["coverage"])
    atomic_json(args.output / "evaluation.json", result)
    serialized = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    temporary = args.output / "grid-attempts.jsonl.tmp"
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(args.output / "grid-attempts.jsonl")
    summary = result["study"]["execution"]
    print(f"{summary['valid_runs']}/{summary['expected_cells']} valid cells · {summary['attempts']} attempts · "
          f"{result['complete_tasks']} complete tasks · {result['replay_status']}")


if __name__ == "__main__":
    main()

