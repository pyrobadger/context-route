from hashlib import sha256

import pytest

from benchmark.evaluator.cross_model import (
    ARMS, FIXED_MODEL, MODELS, TIERS, VERSION, arm_matrix, audit_run, availability, empirical_analysis,
    interaction_analysis, match_tasks, observed_metrics, oracle_analysis,
)


def sample(task, model, tier, *, resolved=True, total=120, cost=0):
    packs = [
        {"context_id": context, "context_hash": sha256(f"{task}/{context}".encode()).hexdigest(),
         "actual_tokens": 40 + 20 * i, "retrieval_version": "matched-test"}
        for i, context in enumerate(TIERS)
    ]
    pack = next(p for p in packs if p["context_id"] == tier)
    return {
        "task_id": task, "task_category": "bug", "model_id": model, "expected_model": model,
        "execution_id": f"{task}/{model}/{tier}", "repository_id": "contextroute-shop",
        "repository_commit": "commit", "opencode_version": VERSION, "mode": "forced",
        "verification_type": "task_tests", "execution_status": "completed", "context_wire_verified": True,
        "resolved": resolved, "context_id": tier, "context_hash": pack["context_hash"],
        "retrieval_version": pack["retrieval_version"], "initial_context_tokens": pack["actual_tokens"],
        "input_tokens": total - 10, "output_tokens": 10, "total_tokens": total, "latency_seconds": 12,
        "estimated_list_price_usd": cost,
        "decision": {
            "task_features": {"category": "bug", "tokens": 10}, "packs": packs,
            "model_metadata": {"key": model, "enabled": True, "supports_text": True, "supports_tools": True,
                               "context_window": 40000, "pricing": []},
        },
    }


def matched(*tasks, best=None):
    return {task: {(model, tier): sample(task, model, tier, total=60 if (model, tier) == best else 120)
                   for model, tier in ARMS} for task in tasks}


def by_cell(grids):
    return {(task, *arm): row for task, grid in grids.items() for arm, row in grid.items()}


def test_complete_grid_matches_same_fixture_task_and_tier():
    grids = matched("full", "wrong-commit")
    grids["wrong-commit"][MODELS[2], "C3"]["repository_commit"] = "wrong"
    all_rows = by_cell(grids)
    all_rows["partial", FIXED_MODEL, "C2"] = sample("partial", FIXED_MODEL, "C2")
    complete, issues = match_tasks(all_rows, ["full", "wrong-commit", "partial"])
    assert list(complete) == ["full"]
    assert len(complete["full"]) == 9
    assert [(issue["task"], issue["kind"]) for issue in issues] == [("wrong-commit", "execution design")]
    assert {row["tasks"] for row in arm_matrix(complete)} == {1}


def test_missing_cells_distinguish_attempts_from_unrun_and_preserve_completed_failures():
    rows = {("task", FIXED_MODEL, "C2"): sample("task", FIXED_MODEL, "C2", resolved=False)}
    observed = rows["task", FIXED_MODEL, "C2"]
    attempts = {
        ("task", FIXED_MODEL, "C2"): [{"execution_id": observed["execution_id"]}],
        ("task", MODELS[1], "C3"): [{"execution_id": "429", "execution_validity": "infrastructure"}],
    }
    cells = {(r["model"], r["tier"]): r for r in availability(["task"], rows, attempts)}
    assert len(cells) == 9
    assert cells[FIXED_MODEL, "C2"]["status"] == "verified failure"
    assert cells[FIXED_MODEL, "C2"]["observation"]["resolved"] is False
    assert cells[MODELS[1], "C3"]["status"] == "missing (attempted)"
    assert cells[MODELS[1], "C3"]["observation"] is None
    assert cells[MODELS[2], "C1"]["status"] == "missing (unattempted)"
    assert match_tasks(rows, ["task"])[0] == {}


def test_context_hash_conflict_excludes_even_a_nine_arm_task():
    grids = matched("conflict", "full")
    grids["conflict"][MODELS[2], "C3"]["context_hash"] = "different-pack"
    complete, issues = match_tasks(by_cell(grids), grids)
    assert list(complete) == ["full"]
    assert len(issues) == 1
    assert issues[0]["tier"] == "C3"
    assert issues[0]["model_hashes"][MODELS[2]] == "different-pack"


def test_model_alias_and_infrastructure_result_are_rejected(tmp_path):
    row = sample("task", MODELS[2], "C1")
    row["model_id"] = "google/gemini-flash-lite-latest"
    assert audit_run(row, tmp_path, "commit", {})[:2] == ("rejected", "Wrong observed model")
    row["execution_status"] = "failed"
    assert audit_run(row, tmp_path, "commit", {})[0] == "infrastructure"


def test_held_out_outcomes_cannot_enter_empirical_selection():
    best = MODELS[1], "C3"
    grids = matched("held", "a", "b", "c", "d", best=best)
    before = empirical_analysis(grids)
    row = grids["held"][best]
    row.update(resolved=False, total_tokens=1000, input_tokens=990, estimated_list_price_usd=0.25)
    after = empirical_analysis(grids)
    case = next(r for r in before["decisions"] if (r["task"], r["policy"]) == ("held", "joint"))
    changed = next(r for r in after["decisions"] if (r["task"], r["policy"]) == ("held", "joint"))
    assert case["decision_source"] == changed["decision_source"] == "empirical"
    assert changed["evidence_count"] == 4
    assert changed["wilson_lower_bound"] == pytest.approx(0.5101, abs=0.0002)
    assert case["alternatives"] == changed["alternatives"]
    assert changed["selected_outcome"]["execution_id"] == row["execution_id"]
    assert changed["selected_outcome"]["resolved"] is False
    assert after["folds"][0] == {"held_out": "held", "training_tasks": ["a", "b", "c", "d"]}


def test_routes_count_fallback_and_fixed_decisions_separately():
    grids = matched("first", "second", "third")
    result = empirical_analysis(grids)
    for name in ("model_only", "context_only", "joint"):
        counts = result["policies"][name]
        assert counts["empirical_decisions"] == 0
        assert counts["fallback_decisions"] == 3
        assert counts["token_delta_vs_fixed"] == 0
    assert result["policies"]["fixed"]["fixed_decisions"] == 3
    assert result["policies"]["fixed"]["fallback_decisions"] == 0
    assert {r["selected_outcome"]["model_id"] for r in result["decisions"]} == {FIXED_MODEL}
    assert {r["selected_outcome"]["context_id"] for r in result["decisions"]} == {"C2"}


def test_oracle_uses_outcomes_and_does_not_change_fallback_replay():
    grids = matched("first", "second", best=(MODELS[2], "C3"))
    picked = (FIXED_MODEL, "C2")
    grids["first"][picked]["resolved"] = False
    empirical = empirical_analysis(grids)
    oracle = oracle_analysis(grids)
    assert oracle["policies"]["joint"]["successes"] == 2
    assert oracle["policies"]["joint"]["total_tokens"] == 120
    assert oracle["policies"]["context_only"]["total_tokens"] == 240
    assert all(s["selected_outcome"]["model_id"] == MODELS[2] for s in oracle["selections"] if s["policy"] == "joint")
    assert empirical["policies"]["joint"]["successes"] == 1
    assert empirical["policies"]["joint"]["fallback_decisions"] == 2


def test_metrics_compute_matched_usage_and_preserve_missing_prices():
    passed = sample("good", FIXED_MODEL, "C1", total=100, cost=0.01)
    failed = sample("bad", FIXED_MODEL, "C1", total=300, cost=0.03, resolved=False)
    failed["latency_seconds"] = 4
    result = observed_metrics([passed, failed])
    assert result["total_tokens"] == 400
    assert result["average_tokens"] == result["median_tokens"] == 200
    assert result["success_rate"] == 0.5
    assert result["mean_latency_seconds"] == 8
    assert result["successes_per_million_tokens"] == 2500
    assert result["cost_per_success_usd"] == pytest.approx(0.04)
    failed["estimated_list_price_usd"] = None
    assert observed_metrics([passed, failed])["total_cost_usd"] is None
    assert observed_metrics([])["average_tokens"] is None


def test_reversal_labels_identical_packs_without_inventing_context_effect():
    grids = matched("task")
    left, right = MODELS[:2]
    grid = grids["task"]
    for model in MODELS:
        grid[model, "C2"]["context_hash"] = grid[model, "C1"]["context_hash"]
        grid[model, "C2"]["initial_context_tokens"] = grid[model, "C1"]["initial_context_tokens"]
    grid[left, "C1"]["total_tokens"] = grid[right, "C2"]["total_tokens"] = 60
    result = interaction_analysis(grids, by_cell(grids))
    assert result["interaction_evidence"] == "inconclusive"
    pair = next(row for row in result["ranking_reversals"] if row["tiers"] == ["C1", "C2"])
    assert pair["identical_packs"] is True
    assert pair["winners"] == [left, right]

