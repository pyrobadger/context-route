from copy import deepcopy
from hashlib import sha256

import pytest

from benchmark.evaluator.evaluate import matrix, metrics, replay
from benchmark.evaluator.report import fmt, percentage_change, render
from benchmark.evaluator.study import empirical_replay, fallback_audit, selected_rows
from benchmark.runner.run import FIXTURE, TASKS, load_json, verifier

VERSION = "bm25-deps-v2"


def sample(task, model, context, resolved, cost):
    digest = sha256(f"{task}:{context}".encode()).hexdigest()
    packs = [{"context_id": tier, "actual_tokens": 100 if tier == "C1" else 200,
              "context_hash": sha256(f"{task}:{tier}".encode()).hexdigest(), "retrieval_version": VERSION,
              "budget_tokens": 1200 if tier == "C1" else 3200, "strategy": "lexical"} for tier in ("C1", "C2")]
    return {
        "execution_id": f"{task}-{model}-{context}", "task_id": str(task), "task_category": "bug",
        "repository_id": "test-input", "repository_commit": "test-only", "mode": "forced", "model_id": model,
        "context_id": context, "context_hash": digest, "context_wire_verified": True, "retrieval_version": VERSION,
        "opencode_version": "test", "execution_status": "completed", "verification_type": "task_tests",
        "original_model": "sample/a", "resolved": resolved, "estimated_list_price_usd": cost,
        "latency_seconds": 10, "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        "initial_context_tokens": 100 if context == "C1" else 200, "router_overhead_ms": 1,
        "decision": {"model_metadata": {"key": model, "provider_id": "sample", "model_id": model.split("/")[1],
                     "enabled": True, "supports_tools": True, "supports_text": True, "context_window": 64000,
                     "pricing": [], "pricing_source": None, "pricing_read_at": None, "display_name": model},
                     "task_features": {"category": "bug", "tokens": 15}, "packs": packs},
        "verification": {"summary": "test input"},
    }


def test_comparison_only_reports_deltas_with_observed_nonzero_baseline():
    assert percentage_change(100, 75) == -25
    assert percentage_change(100, 125) == 25
    assert percentage_change(0, 0) is None
    assert percentage_change(50, None) is None
    assert fmt(0, "$", 6) == "$0.000000"


def test_metrics_preserve_unknown_costs_and_report_cost_per_success():
    rows = [sample(1, "sample/a", "C1", True, 0.05), sample(2, "sample/a", "C1", False, 0.03)]
    assert metrics(rows)["cost_per_success_usd"] == 0.08
    assert metrics(rows)["success_rate"] == 0.5
    assert metrics(rows)["mean_latency_seconds"] == 10
    rows[1]["estimated_list_price_usd"] = None
    assert metrics(rows)["total_cost_usd"] is None


def test_replay_uses_complete_grids_and_the_same_optimizer_without_held_out_outcomes():
    rows = [sample(task, model, context, resolved, cost) for task in range(6)
            for model, context, resolved, cost in (
                ("sample/a", "C1", True, 0.05), ("sample/a", "C2", False, 0.005),
                ("sample/b", "C1", False, 0.005), ("sample/b", "C2", True, 0.02))]
    rows.append(sample(7, "sample/a", "C1", True, 0.01))
    result = replay(rows, "sample/a", "C1")
    assert result["replay_status"] == "ok"
    assert result["complete_tasks"] == 6
    assert result["incomplete_tasks"] == 1
    assert result["policies"]["fixed"]["success_rate"] == 1
    assert result["policies"]["joint"]["cost_per_success_usd"] == 0.02
    assert result["policies"]["model_only"]["cost_per_success_usd"] == pytest.approx(0.05)
    assert result["policies"]["context_only"]["cost_per_success_usd"] == pytest.approx(0.05)
    decisions = result["policies"]["joint"]["decisions"]
    assert len(decisions) == 6
    assert all(row["decision"]["selected"]["evidence_count"] == 5 for row in decisions)
    assert result["rank_reversals"][0]["success_count_differences"] == [6, -6]



def test_fallback_and_empirical_cohorts_keep_whole_task_estimates_separate():
    rows = []
    for task in range(5):
        for model, context, total in (
            ("sample/a", "C1", 300), ("sample/a", "C2", 150),
            ("sample/b", "C1", 250), ("sample/b", "C2", 120),
        ):
            rows.append({**sample(task, model, context, True, 0), "total_tokens": total})

    sparse = [row for row in rows if int(row["task_id"]) < 3]
    sparse_grid = {(row["task_id"], row["model_id"], row["context_id"]): row for row in sparse}
    sparse_result = replay(sparse, "sample/a", "C2")
    sparse_selected = selected_rows(sparse_grid, sparse_result)
    provisional = empirical_replay(sparse_selected, sparse_grid)
    assert provisional["status"] == "insufficient_training_tasks"
    assert provisional["minimum_count_folds"] == 0
    assert provisional["decision_counts"]["joint"] == {"cold_start_fallback": 3}
    assert provisional["joint_empirical_vs_fixed"]["measurements"] == {}
    assert all(case["fixed"]["decision_type"] == "fixed_baseline" for case in sparse_selected.values())
    audit = fallback_audit(sparse_selected)
    assert len(audit) == 3
    assert all(case["joint_context"] == case["context_only_context"] == "C2" for case in audit)
    assert all(case["identical_packs"] and case["token_delta"] == 0 for case in audit)
    assert all(case["joint"]["fallback_reason"] == "insufficient_evidence" for case in sparse_selected.values())

    full_grid = {(row["task_id"], row["model_id"], row["context_id"]): row for row in rows}
    full_result = replay(rows, "sample/a", "C2")
    full_selected = selected_rows(full_grid, full_result)
    empirical = empirical_replay(full_selected, full_grid)
    assert empirical["status"] == "evaluated"
    assert set(empirical["training_tasks_by_fold"].values()) == {4}
    assert empirical["decision_counts"]["joint"] == {"empirical": 5}
    assert all(case["joint"]["model"] == "sample/b" and case["joint"]["context"] == "C2" for case in full_selected.values())
    assert empirical["joint_empirical_vs_fixed"]["difference"]["token_savings_pct"] == 20
    assert empirical["all_adaptive_empirical"]["joint_vs_model_only"]["token_savings_pct"] == 0
    assert fallback_audit(full_selected) == []


def test_duplicate_and_invalid_runner_outcomes_do_not_inflate_the_grid():
    record = sample(1, "sample/a", "C1", True, 0.01)
    duplicate = {**record, "execution_id": "z-repeat"}
    invalid = {**record, "context_id": "C2", "execution_status": "timeout"}
    missing = {**record, "context_id": "C2", "context_wire_verified": False}
    assert len(next(iter(matrix([record, duplicate, invalid, missing]).values()))) == 1


def test_html_reports_regression_escapes_content_and_suppresses_invalid_deltas():
    original = sample(1, "sample/a", "C1", True, 0.05)
    original["mode"] = "baseline"
    routed = sample(1, "sample/b", "C2", False, 0.02)
    routed["decision"]["reasons"] = ["<script>alert(1)</script>"]
    routed["decision"]["decision_source"] = "forced"
    routed["total_tokens"] = 100
    output = render([original, routed], {})
    assert "Regression: OpenCode passed" in output
    assert "&lt;script&gt;" in output
    assert "<script>" not in output
    assert "-16.7%" in output
    failed = deepcopy(routed)
    failed["execution_status"] = "invalid"
    assert "-16.7%" not in render([original, failed], {})


def test_runner_verifier_catches_faulty_revision_and_accepts_fixed_copy(tmp_path):
    import shutil

    task = load_json(TASKS / "tasks.json")[0]
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURE, workspace)
    faulty = verifier(workspace, tmp_path / "verify-base", task)
    assert faulty == {"passed": False, "status": "failed", "summary": "1 failed, 5 passed"}
    path = workspace / "shop" / "inventory.py"
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("return item.on_hand", "return max(0, item.on_hand - item.reserved)"), encoding="utf-8")
    fixed = verifier(workspace, tmp_path / "verify-fixed", task)
    assert fixed == {"passed": True, "status": "passed", "summary": "6 passed"}

