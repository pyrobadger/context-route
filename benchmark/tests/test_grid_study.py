from xml.etree import ElementTree

import pytest

from benchmark.evaluator.charts import heatmap, tokens_vs_success
from benchmark.evaluator.study import MODELS, TIERS, grid_matrix, interactions, matched_efficiency, paired_difference
from benchmark.runner.run import Configuration, atomic_json, execution_plan, parse_args, protocol_key


def row(model, tier, tokens, *, pack=None, passed=True):
    return {"model_id": model, "context_id": tier, "resolved": passed, "total_tokens": tokens,
            "initial_context_tokens": 100, "context_hash": pack or tier, "latency_seconds": 1,
            "estimated_list_price_usd": 0}


def test_grid_keeps_all_expected_cells_and_does_not_show_missing_as_zero():
    valid = {("task", MODELS[0], "C1"): row(MODELS[0], "C1", 100)}
    matrix = grid_matrix(valid)
    assert len(matrix) == 6
    assert matrix[0]["average_tokens"] == 100
    assert matrix[0]["success_rate"] == 1
    assert matrix[1]["average_tokens"] is None
    assert matrix[1]["success_rate"] is None
    assert "Missing" in heatmap(matrix)
    ElementTree.fromstring(heatmap(matrix))
    ElementTree.fromstring(tokens_vs_success({}))


def test_interaction_distinguishes_reversals_with_different_and_identical_context():
    token_counts = [(100, 130), (140, 110), (100, 150)]
    valid = {(task, model, tier): row(model, tier, token_counts[index][side], pack=("reused" if task == "identical" else tier))
             for task in ("distinct", "identical") for side, model in enumerate(MODELS) for index, tier in enumerate(TIERS)}
    records = interactions(valid, ["distinct", "identical"])
    assert records["distinct"]["token_reversals_distinct_packs"] == [["C1", "C2"], ["C2", "C3"]]
    assert records["identical"]["token_reversals_distinct_packs"] == []
    assert records["identical"]["token_reversals_identical_packs"] == [["C1", "C2"], ["C2", "C3"]]
    assert not any(records["distinct"]["cost_winner"].values())
    assert matched_efficiency(valid, ["distinct", "identical"])["tiers"][1]["winner"] == MODELS[1]


def test_token_savings_matches_coverage_and_reports_regressions():
    selections = {
        "paired": {
            "fixed": {"execution_id": "fixed", "total_tokens": 300, "resolved": True},
            "joint": {"execution_id": "joint", "total_tokens": 150, "resolved": False},
        },
        "abstained": {
            "fixed": {"execution_id": "fixed2", "total_tokens": 600, "resolved": True},
            "joint": {"execution_id": None, "total_tokens": None, "resolved": None},
        },
    }
    effect = paired_difference(selections, "joint", "fixed")
    assert effect["paired_tasks"] == 1
    assert effect["token_savings_pct"] == 50
    assert effect["verified_success_delta"] == -1
    assert effect["regressions"] == ["paired"]


def test_pinned_grid_resume_skips_finished_cells_and_detects_a_changed_runtime(tmp_path):
    runtime = tmp_path / "benchmark-runtime"
    (runtime / "dist" / "plugin").mkdir(parents=True)
    (runtime / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    plugin = runtime / "dist" / "plugin" / "index.js"
    plugin.write_text("export default 1;", encoding="utf-8")
    args = parse_args(["--models", *MODELS, "--contexts", *TIERS, "--output", str(tmp_path), "--resume"])
    task = {"id": "fixture"}
    version = "opencode v2.2.7"
    c1 = Configuration("forced", MODELS[0], MODELS[0], "C1")
    c2 = Configuration("forced", MODELS[0], MODELS[0], "C2")
    for config, status in ((c1, "completed"), (c2, "failed")):
        atomic_json(tmp_path / "runs" / f"{config.context}.json", {
            "protocol_key": protocol_key("fixture-commit", task, config, args, version), "mode": "forced",
            "expected_model": MODELS[0], "context_id": config.context, "execution_status": status,
            "task_id": task["id"], "resolved": False if status == "completed" else None,
        })
    plan = execution_plan([task], [c1, c2], "fixture-commit", args, version)
    assert [skip for _, _, _, skip in plan] == [True, False]

    plugin.write_text("export default 2;", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Pinned grid protocol mismatch"):
        execution_plan([task], [c1, c2], "fixture-commit", args, version)
