"""Summarize the separate Gemini pilot, including physical quota use."""

import argparse
from collections import Counter, deque
from pathlib import Path
from statistics import mean

from benchmark.runner.gemini_quota import Ledger, Limits, MODEL_KEY
from benchmark.runner.run import RESULTS, ROOT, atomic_json, load_json


def peak_window(rows, field=None):
    window = deque()
    total = peak = 0
    for row in sorted(rows, key=lambda item: item["at"]):
        while window and window[0]["at"] <= row["at"] - 60:
            removed = window.popleft()
            total -= removed[field] if field else 1
        window.append(row)
        total += row[field] if field else 1
        peak = max(total, peak)
    return peak


def summarize(output, ledger):
    rows = [load_json(path) for path in sorted((output / "runs").glob("*.json"))]
    graded = [row for row in rows if row["execution_status"] == "completed"
              and row["model_id"] == MODEL_KEY and row["context_wire_verified"]]
    cells = [(row["task_id"], row["context_id"]) for row in graded]
    if len(set(cells)) != len(cells):
        raise ValueError("Duplicate completed Gemini cells.")
    physical = [request for path in (output / "telemetry").glob("*") if path.is_dir()
                for request in ledger.attempts(path.name)]
    usage = [request for request in physical if request["kind"] == "generate" and request["total_tokens"] is not None]
    quota = ledger.snapshot()
    calls = [row["gemini_quota"]["api_requests"] for row in graded]
    usage_fields = ("input_tokens", "output_tokens", "total_tokens")
    return {
        "model": MODEL_KEY, "attempts_recorded": len(rows), "completed": len(graded),
        "verified_passes": sum(row["resolved"] is True for row in graded),
        "completed_cells": [{**{field: row[field] for field in (
            "task_id", "context_id", "context_hash", "resolved", "input_tokens", "output_tokens", "total_tokens",
            "latency_seconds", "wall_seconds", "tool_calls", "execution_id")},
            "provider_usage": row["gemini_quota"]} for row in sorted(graded, key=lambda item: (item["task_id"], item["context_id"]))],
        "local_guard_limits": vars(ledger.limits), "quota_snapshot": quota,
        "all_pilot_requests": len(physical), "all_pilot_request_types": dict(Counter(row["kind"] for row in physical)),
        "requests_without_final_http_status": sum(row["status"] is None for row in physical),
        "generation_requests_with_observed_usage": len(usage),
        "all_pilot_observed_usage": {key: sum(row[key] for row in usage) if usage and all(row[key] is not None for row in usage)
                                    else None for key in usage_fields},
        "provider_429_count": sum(row["status"] == 429 for row in physical),
        "peak_rolling_requests": peak_window(physical), "peak_rolling_reserved_input_tokens": peak_window(physical, "reserved"),
        "completed_mean_requests_per_cell": mean(calls) if calls else None,
        "completed_max_requests_per_cell": max(calls, default=None),
        "rough_additional_cells_at_pilot_max": quota["remaining_rpd"] // max(calls) if calls else None,
        "capacity_note": "Local snapshot. The provider project can also have usage from other clients. A pilot maximum is a planning estimate.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=RESULTS / "gemini-pilot")
    parser.add_argument("--gemini-rpm", type=int, default=10)
    parser.add_argument("--gemini-tpm", type=int, default=200_000)
    parser.add_argument("--gemini-rpd", type=int, default=400)
    args = parser.parse_args()
    ledger = Ledger(ROOT / ".contextroute" / "gemini-quota.sqlite3", Limits(args.gemini_rpm, args.gemini_tpm, args.gemini_rpd))
    result = summarize(args.output, ledger)
    atomic_json(args.output / "pilot-summary.json", result)
    print(f"{result['completed']} verified Gemini executions · {result['all_pilot_requests']} metered requests")


if __name__ == "__main__":
    main()

