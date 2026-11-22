"""Run the full 8-task x 6-arm x 3-repeat experiment with model-parallel execution."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark.runner.run import (
    FIXTURE, TASKS, RESULTS, Configuration, atomic_json, load_json,
    opencode_executable, fixture_repository, checkout_fixture, verifier,
    execute, protocol_key, benchmark_runtime
)

GRID_OUTPUT = ROOT / "benchmark" / "results" / "grid"
TRAINING_EVIDENCE = ROOT / "benchmark" / "results" / "grid" / "training"

MODELS = [
    "opencode/big-pickle",
    "opencode/deepseek-v4.1-flash-free",
]

CONTEXTS = ["C1", "C2", "C3"]

class ArgsShim:
    def __init__(self, output: Path):
        self.output = output
        self.evidence = TRAINING_EVIDENCE
        self.constraints = {}
        self.timeout = 150.0
        self.contexts = CONTEXTS
        self.models = MODELS
        self.gemini_quota = False


def get_existing_completed(output_dir: Path) -> dict[tuple[str, str, str, int], dict]:
    """Scan existing runs and return a map of (task_id, model, context, repeat) -> row for completed valid runs."""
    completed = {}
    runs_dir = output_dir / "runs"
    if not runs_dir.exists():
        return completed
    for path in runs_dir.glob("*.json"):
        try:
            row = load_json(path)
            if row.get("execution_status") == "completed":
                task_id = row.get("task_id")
                model_id = row.get("expected_model") or row.get("model_id")
                context_id = row.get("context_id")
                repeat = row.get("repeat", 0)
                if task_id and model_id and context_id is not None:
                    completed[(task_id, model_id, context_id, repeat)] = row
        except Exception:
            continue
    return completed


def run_single_arm(base: Path, commit: str, task: dict, model: str, context: str,
                   repeat: int, args_shim: ArgsShim, executable: str, version: str,
                   max_retries: int = 2) -> dict:
    """Execute a single arm with up to max_retries attempts on transient failure."""
    config = Configuration("forced", model, model, context)
    key = protocol_key(commit, task, config, args_shim, version, repeat=repeat)
    
    last_row = None
    for attempt in range(1, max_retries + 2):
        row, transient = execute(base, commit, task, config, args_shim, executable,
                                 key, attempt, version, gemini_ledger=None, repeat=repeat)
        last_row = row
        status = row.get("execution_status")
        resolved = row.get("resolved")
        tokens = row.get("total_tokens")
        latency = row.get("wall_seconds")
        print(f"[{task['id']}] {model.split('/')[-1]} {context} (rep {repeat}, att {attempt}): {status} (resolved={resolved}, tokens={tokens}, wall={latency}s)", flush=True)
        
        if status == "completed":
            return row
        
        if not transient or attempt > max_retries:
            break
            
        backoff = min(30, 10 * (2 ** (attempt - 1)))
        print(f"  Transient failure on {model} {context}, backing off for {backoff}s...", flush=True)
        time.sleep(backoff)
        
    return last_row


def run_model_slice(base: Path, commit: str, task: dict, model: str,
                    contexts: list[str], repeat: int, args_shim: ArgsShim,
                    executable: str, version: str, interval: float = 4.0) -> list[dict]:
    """Execute all contexts for one model on a task sequentially, pacing requests."""
    results = []
    existing = get_existing_completed(args_shim.output)
    
    for context in contexts:
        cell = (task["id"], model, context, repeat)
        if cell in existing:
            prev = existing[cell]
            print(f"[{task['id']}] {model.split('/')[-1]} {context} (rep {repeat}): RESUMED (resolved={prev.get('resolved')}, tokens={prev.get('total_tokens')})", flush=True)
            results.append(prev)
            continue
            
        row = run_single_arm(base, commit, task, model, context, repeat, args_shim, executable, version)
        results.append(row)
        time.sleep(interval)
        
    return results


def run_task_parallel(base: Path, commit: str, task: dict, repeat: int,
                      args_shim: ArgsShim, executable: str, version: str) -> list[dict]:
    """Execute all 6 arms for one task running the 2 models in parallel."""
    print(f"\n==================================================", flush=True)
    print(f"TASK: {task['id']} | REPEAT: {repeat}", flush=True)
    print(f"==================================================", flush=True)
    
    results = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(run_model_slice, base, commit, task, model, CONTEXTS, repeat, args_shim, executable, version): model
            for model in MODELS
        }
        for future in as_completed(futures):
            model = futures[future]
            try:
                model_results = future.result()
                results.extend(model_results)
            except Exception as e:
                print(f"Error running model {model} on task {task['id']}: {e}", file=sys.stderr, flush=True)
                
    return results


def main():
    parser = argparse.ArgumentParser(description="Run full ContextRoute experiment")
    parser.add_argument("--repeats", type=int, default=3, help="Total repeats per arm (default: 3)")
    parser.add_argument("--task", action="append", help="Specific task ID(s) to run")
    parser.add_argument("--repeat-only", type=int, help="Run only this repeat index (0, 1, or 2)")
    args = parser.parse_args()

    # Load frozen tasks
    tasks_file = ROOT / "experiments" / "tasks.json"
    frozen_spec = load_json(tasks_file)
    tasks = frozen_spec["tasks"]
    if args.task:
        tasks = [t for t in tasks if t["id"] in args.task]
        
    executable = opencode_executable()
    version_res = subprocess.run([executable, "--version"], capture_output=True, text=True, check=True)
    version = version_res.stdout.strip()
    print(f"Using OpenCode: {executable} ({version})")
    
    args_shim = ArgsShim(GRID_OUTPUT)
    
    # Initialize base fixture repository
    with tempfile.TemporaryDirectory(prefix="contextroute-exp-base-") as base_dir:
        base = Path(base_dir) / "fixture"
        commit = fixture_repository(base)
        print(f"Initialized fixture commit: {commit}")
        
        target_repeats = [args.repeat_only] if args.repeat_only is not None else list(range(args.repeats))
        
        for repeat in target_repeats:
            print(f"\n##################################################", flush=True)
            print(f"STARTING REPEAT PASS {repeat} (of {args.repeats})", flush=True)
            print(f"##################################################", flush=True)
            
            for task in tasks:
                run_task_parallel(base, commit, task, repeat, args_shim, executable, version)
                
    print("\nExperiment execution pass completed!", flush=True)

if __name__ == "__main__":
    main()
