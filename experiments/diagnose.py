import json
from pathlib import Path

runs_dir = Path("benchmark/results/grid/runs")
runs = [json.loads(p.read_text(encoding="utf-8")) for p in runs_dir.glob("*.json")]

completed = [r for r in runs if r.get("execution_status") == "completed"]
incomplete = [r for r in runs if r.get("execution_status") != "completed"]

print(f"Total runs: {len(runs)}, Completed: {len(completed)}, Incomplete: {len(incomplete)}")

print("\n--- COMPLETED RUNS ---")
for r in sorted(completed, key=lambda x: (x.get("task_id", ""), x.get("model_id", ""), x.get("context_id", ""))):
    eid = r["execution_id"]
    t_file = Path("benchmark/results/grid/telemetry") / eid / "execution.jsonl"
    first_time = "no-telemetry"
    if t_file.exists():
        lines = t_file.read_text(encoding="utf-8").splitlines()
        if lines:
            try:
                first_time = json.loads(lines[0]).get("time")
            except Exception:
                first_time = "json-error"
    print(f"{r['task_id']} | {r['model_id']} | {r['context_id']} | wall: {r.get('wall_seconds')}s | {first_time}")

print("\n--- INCOMPLETE RUNS ---")
for r in sorted(incomplete, key=lambda x: (x.get("task_id", ""), x.get("expected_model", ""), x.get("context_id", ""))):
    eid = r["execution_id"]
    t_file = Path("benchmark/results/grid/telemetry") / eid / "execution.jsonl"
    first_time = "no-telemetry"
    if t_file.exists():
        lines = t_file.read_text(encoding="utf-8").splitlines()
        if lines:
            try:
                first_time = json.loads(lines[0]).get("time")
            except Exception:
                first_time = "json-error"
    print(f"{r.get('task_id')} | {r.get('expected_model')} | {r.get('context_id')} | status: {r.get('execution_status')} | wall: {r.get('wall_seconds')}s | {first_time}")
