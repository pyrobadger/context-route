"""Run controlled OpenCode tasks in disposable clones and write one JSON record per attempt."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
from hashlib import sha256
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

from .gemini_gateway import GeminiGateway
from .gemini_quota import MODEL as GEMINI_MODEL, MODEL_KEY as GEMINI_MODEL_KEY, Ledger, Limits, QuotaBlocked

ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = ROOT / "benchmark"
FIXTURE = BENCHMARK / "fixtures" / "shop"
TASKS = BENCHMARK / "tasks"
RESULTS = BENCHMARK / "results"
CONTEXTS = {"C0", "C1", "C2", "C3", "L1", "L2", "L3"}
BENCHMARK_PROTOCOL = 2
GEMINI_TITLE_MODEL = GEMINI_MODEL_KEY + "#minimal"


@dataclass(frozen=True)
class Configuration:
    mode: str
    original_model: str
    forced_model: str | None = None
    context: str | None = None


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_process(command, cwd: Path, *, timeout: float = 60, env=None, stdin=subprocess.DEVNULL):
    return subprocess.run(command, cwd=cwd, env=env, timeout=timeout, capture_output=True,
                          stdin=stdin, text=True, encoding="utf-8", errors="replace",
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def git(cwd: Path, *arguments: str, env=None) -> str:
    result = run_process(["git", *arguments], cwd, env=env)
    if result.returncode:
        raise RuntimeError(f"Git {arguments[0]} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def opencode_executable() -> str:
    override = os.environ.get("CONTEXTROUTE_OPENCODE")
    if override:
        path = Path(override).resolve()
        if not path.is_file() or path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise RuntimeError("CONTEXTROUTE_OPENCODE must point to the native OpenCode executable.")
        return str(path)
    found = shutil.which("opencode.exe" if os.name == "nt" else "opencode")
    if found:
        return found
    if os.name == "nt":
        for directory in os.get_exec_path():
            candidate = Path(directory) / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
            if candidate.is_file():
                return str(candidate.resolve())
    raise RuntimeError("OpenCode executable not found. Set CONTEXTROUTE_OPENCODE to its absolute path.")


def fixture_repository(target: Path) -> str:
    shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git"))
    env = dict(os.environ, GIT_AUTHOR_NAME="ContextRoute Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="ContextRoute Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid",
               GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
    (target / ".gitattributes").write_text("* text eol=lf\n", encoding="utf-8")
    git(target, "init", "-q", "-b", "main", "--object-format=sha1")
    git(target, "-c", "core.autocrlf=false", "add", "--all")
    git(target, "-c", "commit.gpgSign=false", "-c", "core.hooksPath=.git/no-hooks", "commit", "-qm", "Controlled shop fixture", env=env)
    return git(target, "rev-parse", "HEAD")


def checkout_fixture(base: Path, commit: str, workspace: Path) -> None:
    git(base, "-c", "core.autocrlf=false", "clone", "-q", "--no-hardlinks", str(base), str(workspace))
    git(workspace, "checkout", "-q", "--detach", commit)
    if git(workspace, "rev-parse", "HEAD") != commit or git(workspace, "status", "--porcelain"):
        raise RuntimeError("Fixture checkout did not start clean at the recorded revision.")


def verifier(workspace: Path, target: Path, task: dict) -> dict:
    target.mkdir()
    shutil.copytree(workspace / "shop", target / "shop", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(FIXTURE / "tests", target / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    addition = workspace / "tests" / "test_quantity_regression.py"
    if task["category"] == "test" and addition.is_file() and addition.resolve().is_relative_to(workspace.resolve()):
        shutil.copyfile(addition, target / "tests" / addition.name)
    shutil.copyfile(TASKS / "verification.py", target / "contextroute_acceptance.py")
    try:
        result = run_process([sys.executable, "-B", "-m", "pytest", "-q", "tests",
                              f"contextroute_acceptance.py::{task['verify']}"], target, timeout=45,
                             env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=""))
    except (subprocess.TimeoutExpired, OSError):
        return {"status": "verifier_error", "passed": None, "summary": "Verification did not complete."}
    counts = re.findall(r"\b\d+ (?:passed|failed|errors?|skipped)\b", result.stdout)
    status = "passed" if result.returncode == 0 else "failed" if result.returncode == 1 else "verifier_error"
    return {"status": status, "passed": result.returncode == 0 if status != "verifier_error" else None,
            "summary": ", ".join(counts) or f"pytest exit {result.returncode}"}


def check_fixtures(tasks: list[dict]) -> None:
    reference = load_json(TASKS / "reference_edits.json")
    with tempfile.TemporaryDirectory(prefix="contextroute-fixtures-") as directory:
        root = Path(directory)
        for task in tasks:
            task_id = task["id"]
            work = root / task_id
            shutil.copytree(FIXTURE, work)
            faulty = verifier(work, root / (task_id + "-faulty"), task)
            if faulty["status"] != "failed":
                raise RuntimeError(f"{task_id} did not fail its acceptance check: {faulty}")
            for filename, before, after in reference[task_id]:
                path = work / filename
                contents = path.read_text(encoding="utf-8") if path.exists() else ""
                if before is not None and contents.count(before) != 1:
                    raise RuntimeError(f"Invalid reference edit in {filename}")
                path.write_text(contents + after if before is None else contents.replace(before, after, 1), encoding="utf-8")
            fixed = verifier(work, root / (task_id + "-fixed"), task)
            if fixed["status"] != "passed":
                raise RuntimeError(f"{task_id} failed after the reference fix: {fixed}")
            print(f"{task_id}: base {faulty['summary']}; corrected {fixed['summary']}", flush=True)


def events_from(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        except json.JSONDecodeError:
            events.append({"type": "integration_error", "stage": "malformed-telemetry"})
    return events


def last_event(events: list[dict], kind: str) -> dict:
    return next((event for event in reversed(events) if event["type"] == kind), {})


def benchmark_runtime(output: Path, mode: str) -> Path:
    # A forced grid can pin its original runtime independently of newer replay policies.
    pinned = output.resolve() / "benchmark-runtime"
    root = pinned if mode == "forced" and pinned.exists() else ROOT
    if not (root / "package.json").is_file() or not (root / "dist" / "plugin" / "index.js").is_file():
        raise RuntimeError(f"Missing built OpenCode plugin in {root}.")
    return root


def protocol_key(commit: str, task: dict, config: Configuration, args, version: str, *, legacy_gemini=False, repeat: int = 0) -> str:
    runtime = benchmark_runtime(args.output, config.mode)
    inputs = [BENCHMARK_PROTOCOL, commit, task, vars(config), version, sha256((TASKS / "verification.py").read_bytes()).hexdigest(),
              args.models, args.contexts, args.constraints, sorted((path.relative_to(runtime).as_posix(), sha256(path.read_bytes()).hexdigest())
                                for path in (runtime / "dist").rglob("*.js"))]
    if repeat > 0:
        inputs.append({"repeat": repeat})
    if args.gemini_quota:
        inputs.append({"gemini_gateway_protocol": 3, "limits": vars(args.gemini_limits)})
        if not legacy_gemini:
            inputs.append({"gemini_title_model": GEMINI_TITLE_MODEL})
    if config.mode == "joint":
        evidence_path = Path(args.evidence)
        candidates = sorted(evidence_path.glob("*.json")) if evidence_path.is_dir() else [evidence_path] if evidence_path.is_file() else []
        inputs.append([sha256(path.read_bytes()).hexdigest() for path in candidates])
    return sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def gemini_project_settings(settings):
    return {
        "model": GEMINI_MODEL_KEY,
        "agents": {"title": {"model": GEMINI_TITLE_MODEL}},
        "providers": {"google": {
            "settings": {"baseURL": settings["url"] + "/v1beta", "timeout": 180_000},
            "headers": {"x-contextroute-gate": settings["token"]},
        }},
    }


def gemini_request_accounting(events, quota, primary_calls, tokens):
    """Reconcile OpenCode request kinds and the exact-model gate's physical calls."""
    auxiliary = [(event.get("request_kind"), event.get("actual_model"))
                 for event in events if event["type"] == "auxiliary_request"]
    errors = []
    if auxiliary != [("title", GEMINI_TITLE_MODEL)]:
        errors.append("Expected exactly one title request on the pinned Gemini variant.")
    generated = quota.get("generation_requests")
    counted = quota.get("token_count_requests")
    physical = quota.get("api_requests")
    if (type(primary_calls) is not int or primary_calls < 1 or generated != primary_calls + len(auxiliary)
        or counted != generated or physical != generated + counted
        or quota.get("generation_requests_with_usage") != generated):
        errors.append("OpenCode requests and metered generation/token-count calls did not reconcile.")
    if quota.get("response_model_versions") != [GEMINI_MODEL] or quota.get("upstream_statuses") != [200]:
        errors.append("Expected successful provider responses on the exact Gemini model.")
    if any(type(tokens.get(name)) is not int or tokens[name] != quota.get("observed_" + name)
           for name in ("input_tokens", "output_tokens", "total_tokens")):
        errors.append("OpenCode and Google token counters disagreed or were missing.")
    return {
        "primary_model": GEMINI_MODEL_KEY,
        "primary_requests": primary_calls,
        "auxiliary": [{"kind": kind, "model": model} for kind, model in auxiliary],
        "metered_generation_requests": generated,
        "metered_token_count_requests": counted,
        "verified": not errors,
        "errors": errors,
    }


def execute(base: Path, commit: str, taskspec: dict, configuration: Configuration, args, executable: str,
            key: str, attempt: int, version: str, gemini_ledger=None, repeat: int = 0) -> tuple[dict, bool]:
    execution_id = uuid4().hex
    results = args.output.resolve()
    trace = results / "telemetry" / execution_id
    trace.mkdir(parents=True)
    log = trace / "execution.jsonl"
    pack = trace / "context.json"
    with tempfile.TemporaryDirectory(prefix="contextroute-run-") as directory:
        temporary = Path(directory).resolve()
        workspace = temporary / "checkout"
        checkout_fixture(base, commit, workspace)
        options = {
            "mode": configuration.mode, "telemetry_path": str(log), "pack_path": str(pack),
            "evidence": str(Path(args.evidence).resolve()), "task_id": taskspec["id"],
            "constraints": args.constraints,
        }
        if configuration.mode == "forced":
            options.update(model=configuration.forced_model, context=configuration.context)
        if configuration.mode == "joint":
            options.update(contexts=args.contexts, models=args.models or [configuration.original_model])
        runtime = benchmark_runtime(results, configuration.mode)
        project = {"plugins": [{"package": runtime.as_uri(), "options": options}], "permissions": [
            {"action": action, "resource": "*", "effect": "deny"} for action in ("external_directory", "subagent", "shell")
        ]}
        gateway = GeminiGateway(gemini_ledger, execution_id) if gemini_ledger else None
        command = [executable, "run", "--standalone", "--archived", "--auto", "--format", "json",
                   "--model", configuration.original_model, taskspec["prompt"]]
        child_env = dict(os.environ,
                         XDG_CONFIG_HOME=str(temporary / "config"),
                         XDG_DATA_HOME=str(temporary / "data"),
                         XDG_CACHE_HOME=str(temporary / "cache"),
                         XDG_STATE_HOME=str(temporary / "state"))
        with gateway if gateway else nullcontext():
            if gateway:
                settings = gateway.settings
                options["gemini_gate_url"] = settings["url"]
                project.update(gemini_project_settings(settings))
                if child_env.get("GEMINI_API_KEY"):
                    child_env["GOOGLE_GENERATIVE_AI_API_KEY"] = child_env["GEMINI_API_KEY"]
            atomic_json(workspace / "opencode.json", project)
            timed_out = False
            launched = time.monotonic()
            try:
                process = run_process(command, workspace, timeout=args.timeout, env=child_env)
            except subprocess.TimeoutExpired as error:
                timed_out = True
                process = error
            wall_seconds = time.monotonic() - launched

        gateway_errors = gateway.errors if gateway else []
        quota_metrics = gemini_ledger.metrics(execution_id) if gemini_ledger else None
        if gemini_ledger:
            atomic_json(trace / "gemini-requests.json", gemini_ledger.attempts(execution_id))


        events = events_from(log)
        routing = last_event(events, "route")
        first = last_event(events, "first_primary_request")
        wire = last_event(events, "primary_http_request")
        ending = last_event(events, "execution")
        counters = last_event(events, "usage")
        failure = last_event(events, "integration_error")
        rates = [event.get("http_status") for event in events if event["type"] in {"retry", "provider_http_error"}
                 and event.get("request_kind", "primary") == "primary"]
        if quota_metrics:
            rates.extend(status for status in quota_metrics["upstream_statuses"] if status >= 400)
        flaky = timed_out or any(status == 429 or isinstance(status, int) and status >= 500 for status in rates)
        expected_model = configuration.forced_model or routing.get("selected_model")
        expected_context = configuration.context or routing.get("context_id")
        context_valid = wire.get("context_verified") is True and (
                         not routing.get("context_hash") and not first.get("context_hash") if configuration.mode == "baseline"
                         else routing.get("context_hash") == first.get("context_hash")
                         == wire.get("context_hash") == ending.get("context_hash") and pack.is_file())
        if pack.is_file() and configuration.mode != "baseline":
            packed = load_json(pack)
            context_valid = context_valid and packed["context_id"] == expected_context and (
                sha256(packed["text"].encode()).hexdigest() == wire.get("context_hash") == packed["context_hash"])
        valid_run = (
            not timed_out and process.returncode == 0 and not failure and bool(last_event(events, "plugin_loaded"))
            and routing.get("original_model") == configuration.original_model
            and expected_model == first.get("actual_model") == wire.get("actual_model") == ending.get("actual_model")
            and routing.get("context_id") == expected_context and context_valid
            and ending.get("execution_status") == "completed" and ending.get("final_model_finish") == "stop"
            and isinstance(counters.get("total_tokens"), int) and counters["total_tokens"] > 0
            and counters["total_tokens"] >= (ending.get("total_tokens") or 0)
        )
        if quota_metrics is not None:
            accounting = gemini_request_accounting(events, quota_metrics, ending.get("primary_model_calls"), counters)
            valid_run = valid_run and not gateway_errors and accounting["verified"]
        verification = verifier(workspace, temporary / "verification", taskspec)
        status = ("timeout" if timed_out else "failed" if getattr(process, "returncode", None) not in (None, 0)
                  or ending.get("execution_status") == "failed" or rates else "invalid")
        if valid_run and verification["passed"] is not None:
            status = "completed"

        auxiliary = [event for event in events if event["type"] == "auxiliary_request"]
        pricing_known = routing.get("pricing_source") and all(event.get("pricing_known") is True for event in auxiliary)
        row = {
            "execution_id": execution_id, "protocol_key": key, "attempt": attempt, "repeat": repeat,
            "task_id": taskspec["id"], "task_category": taskspec["category"], "repository_id": "contextroute-shop",
            "repository_commit": commit, "opencode_version": version, "mode": configuration.mode,
            "original_model": configuration.original_model, "expected_model": expected_model,
            "model_id": first.get("actual_model"), "context_id": expected_context,
            "context_hash": first.get("context_hash"), "retrieval_version": (routing.get("context") or {}).get("retrieval_version"),
            "initial_context_tokens": routing.get("initial_context_tokens"), "resolved": verification["passed"] if status == "completed" else None,
            "verification_type": "task_tests", "verification": verification, "context_wire_verified": context_valid,
            "execution_status": status, "transient_error": flaky, "provider_statuses": sorted(set(status for status in rates if status is not None)),
            "integration_error": failure.get("stage"), "process_exit": getattr(process, "returncode", None),
            "session_id": first.get("session_id"), "latency_seconds": ending.get("latency_seconds"),
            "wall_seconds": round(wall_seconds, 3), "router_overhead_ms": routing.get("router_overhead_ms"),
            "estimated_list_price_usd": counters.get("opencode_cost_usd") if pricing_known else None,
            "actual_billed_cost_usd": None,
            "token_scope": counters.get("token_scope"), "input_tokens": counters.get("input_tokens"),
            "output_tokens": counters.get("output_tokens"), "total_tokens": counters.get("total_tokens"),
            "cache_read_tokens": counters.get("cache_read_tokens"), "cache_write_tokens": counters.get("cache_write_tokens"),
            "primary_model_calls": ending.get("primary_model_calls"),
            "auxiliary_model_calls": len(auxiliary),
            "tool_calls": ending.get("tool_calls"), "decision": routing,
        }
        if quota_metrics is not None:
            row["gemini_quota"] = quota_metrics
            row["quota_errors"] = gateway_errors
            row["gemini_request_accounting"] = accounting
        if status != "completed":
            lines = (getattr(process, "stdout", "") or "")
            if isinstance(lines, bytes):
                lines = lines.decode(errors="replace")
            error_types = []
            for line in lines.splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("type") == "error":
                    error_types.append((item.get("error") or {}).get("name", "OpenCodeError"))
            row["opencode_error_types"] = error_types
        atomic_json(results / "runs" / f"{execution_id}.json", row)
        return row, flaky


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", action="append", help="Task ID. Repeat to select several.")
    parser.add_argument("--limit", type=int, help="Maximum number of tasks.")
    parser.add_argument("--models", nargs="+", default=[], help="Exact provider/model IDs in the experimental matrix.")
    parser.add_argument("--contexts", nargs="+", choices=sorted(CONTEXTS), default=["C1", "C2", "C3"])
    parser.add_argument("--original-model", help="Initial OpenCode model. Defaults to the first --models entry.")
    parser.add_argument("--baseline", action="store_true", help="Also run the original model without ContextRoute injection.")
    parser.add_argument("--joint", action="store_true", help="Also run the actual optimizer using --evidence.")
    parser.add_argument("--no-grid", action="store_true", help="Run only baseline and/or joint.")
    parser.add_argument("--resume", action="store_true", help="Skip complete results under the identical protocol.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned tasks and configurations without execution.")
    parser.add_argument("--check-fixtures", action="store_true", help="Check that base and reference-fixed revisions fail and pass respectively.")
    parser.add_argument("--output", type=Path, default=RESULTS)
    parser.add_argument("--evidence", type=Path, default=RESULTS / "training")
    parser.add_argument("--constraints", type=json.loads, default={}, help="JSON optimizer constraints.")
    parser.add_argument("--gemini-quota", action="store_true", help="Guard a standalone, Gemini-only experiment.")
    parser.add_argument("--gemini-rpm", type=int, default=10)
    parser.add_argument("--gemini-tpm", type=int, default=200_000)
    parser.add_argument("--gemini-rpd", type=int, default=400)
    parser.add_argument("--timeout", type=float, default=150)
    parser.add_argument("--retries", type=int, default=1, help="Additional attempts for transient failures.")
    parser.add_argument("--interval", type=float, default=5, help="Minimum seconds between executions.")
    args = parser.parse_args(argv)
    if args.retries < 0 or args.interval < 0 or args.timeout <= 0 or (args.limit is not None and args.limit < 1):
        parser.error("Timeout and limit must be positive; retries and interval must be nonnegative.")
    if not isinstance(args.constraints, dict):
        parser.error("--constraints must be a JSON object.")
    try:
        args.gemini_limits = Limits(args.gemini_rpm, args.gemini_tpm, args.gemini_rpd)
    except ValueError as error:
        parser.error(str(error))
    return args


def execution_plan(tasks, configurations, commit, args, version):
    output = args.output.resolve()
    pinned = (output / "benchmark-runtime").exists() or args.gemini_quota
    completed = set()
    forced_keys = {}
    for path in (output / "runs").glob("*.json") if args.resume or pinned else ():
        row = load_json(path)
        if row.get("execution_status") == "completed":
            completed.add(row["protocol_key"])
            if row["mode"] == "forced":
                cell = (row["task_id"], row["expected_model"], row["context_id"])
                forced_keys.setdefault(cell, {})[row["protocol_key"]] = row
    if pinned and forced_keys and not args.resume:
        raise RuntimeError("Use --resume to extend a pinned grid with existing forced executions.")

    plan = []
    for task in tasks:
        for configuration in configurations:
            key = protocol_key(commit, task, configuration, args, version)
            cell = (task["id"], configuration.forced_model, configuration.context)
            skip = key in completed
            if pinned and configuration.mode == "forced" and cell in forced_keys and not skip:
                # v3 let OpenCode pick the title model. Reuse it only if the trace proves
                # it picked the exact same model/variant and all requests were metered.
                if args.gemini_quota:
                    earlier_key = protocol_key(commit, task, configuration, args, version, legacy_gemini=True)
                    earlier = forced_keys[cell].get(earlier_key)
                    if earlier and earlier.get("context_wire_verified") and earlier.get("model_id") == GEMINI_MODEL_KEY:
                        trace = output / "telemetry" / earlier["execution_id"] / "execution.jsonl"
                        accounting = gemini_request_accounting(
                            events_from(trace), earlier.get("gemini_quota") or {}, earlier.get("primary_model_calls"), earlier,
                        )
                        skip = accounting["verified"]
                if not skip:
                    raise RuntimeError(f"Pinned grid protocol mismatch for {cell}. No requests were launched.")
            plan.append((task, configuration, key, skip))
    return plan


def main(argv=None) -> int:
    args = parse_args(argv)
    tasks = load_json(TASKS / "tasks.json")
    if args.task:
        missing = set(args.task) - {task["id"] for task in tasks}
        if missing:
            raise SystemExit("Unknown tasks: " + ", ".join(sorted(missing)))
        tasks = [task for task in tasks if task["id"] in args.task]
    tasks = tasks[:args.limit]
    if args.check_fixtures:
        check_fixtures(tasks)
        return 0
    original = args.original_model or next(iter(args.models), None)
    if not original:
        raise SystemExit("Specify --original-model or a list of --models.")
    grid = [] if args.no_grid else [Configuration("forced", original, model, context)
                                   for model in dict.fromkeys(args.models or [original]) for context in dict.fromkeys(args.contexts)]
    if args.baseline:
        grid.insert(0, Configuration("baseline", original))
    if args.joint:
        grid.append(Configuration("joint", original))
    if not grid:
        raise SystemExit("Select a grid, --baseline, or --joint.")
    includes_gemini = original.split("#")[0] == GEMINI_MODEL_KEY or any(
        (model.split("#")[0] == GEMINI_MODEL_KEY) for model in args.models)
    if includes_gemini and not args.gemini_quota:
        raise SystemExit("Gemini 3.5 Flash-Lite requires --gemini-quota.")
    if args.gemini_quota and (original != GEMINI_MODEL_KEY or any(cfg.mode != "forced" or cfg.forced_model != GEMINI_MODEL_KEY for cfg in grid)
                              or args.output.resolve().is_relative_to(RESULTS / "grid")):
        raise SystemExit("Gemini guard requires a separate output directory and a forced grid using only the exact Gemini model.")
    if args.dry_run:
        for task in tasks:
            for configuration in grid:
                print(json.dumps({"task": task["id"], **vars(configuration)}))
        return 0

    executable = opencode_executable()
    version_result = run_process([executable, "--version"], ROOT)
    version = version_result.stdout.strip()
    if version_result.returncode or not re.search(r"\bv?2\.2\.7\b", version):
        raise SystemExit(f"Benchmark is pinned to OpenCode 2.2.7. Detected {version!r}.")
    output = args.output.resolve()
    if args.evidence.resolve() == output / "runs" and args.joint:
        raise SystemExit("Use a frozen evidence file or directory outside the current run records.")
    errors = 0
    earliest = 0.0
    gemini_ledger = Ledger(ROOT / ".contextroute" / "gemini-quota.sqlite3", args.gemini_limits) if args.gemini_quota else None
    gemini_run_budget = 2
    if gemini_ledger:
        for path in (output / "runs").glob("*.json"):
            existing = load_json(path)
            if existing["execution_status"] == "completed" and existing.get("gemini_quota"):
                gemini_run_budget = max(gemini_run_budget, existing["gemini_quota"]["api_requests"])
    with tempfile.TemporaryDirectory(prefix="contextroute-base-") as directory:
        base = Path(directory) / "fixture"
        commit = fixture_repository(base)
        for task, configuration, key, skip in execution_plan(tasks, grid, commit, args, version):
            if skip:
                print(f"{task['id']} {configuration.mode} {configuration.forced_model or original} {configuration.context or ''}: resumed", flush=True)
                continue
            for attempt in range(1, args.retries + 2):
                time.sleep(max(0, earliest - time.monotonic()))
                if gemini_ledger:
                    try:
                        gemini_ledger.ready(gemini_run_budget)
                    except QuotaBlocked as error:
                        atomic_json(output / "gemini-quota-block.json", {"reason": error.reason,
                                    "quota": gemini_ledger.snapshot(), "retry_at_utc_epoch": error.retry_at})
                        print(f"Gemini quota guard stopped before task launch: {error.reason}", file=sys.stderr)
                        return 1
                row, transient = execute(base, commit, task, configuration, args, executable, key, attempt, version, gemini_ledger)
                earliest = time.monotonic() + args.interval
                print(json.dumps({field: row[field] for field in ("task_id", "mode", "original_model", "model_id", "context_id",
                      "context_wire_verified", "execution_status", "verification", "resolved", "total_tokens",
                      "estimated_list_price_usd", "latency_seconds", "provider_statuses", "integration_error", "execution_id")}), flush=True)

                # Retry a clean cell once for an upstream server error or timeout. Any 429 stops.
                gemini_retryable = gemini_ledger and transient and 429 not in row["provider_statuses"] and (
                    row["execution_status"] == "timeout" or any(
                        status >= 500 for status in row["gemini_quota"]["upstream_statuses"]))
                if (gemini_ledger and row["execution_status"] != "completed"
                    and not (gemini_retryable and attempt <= args.retries)):
                    atomic_json(output / "gemini-quota-block.json", {"reason": row["quota_errors"],
                                "integration_error": row["integration_error"], "execution_id": row["execution_id"],
                                "quota": gemini_ledger.snapshot()})
                    print("Gemini pilot paused after a failed or unverified execution. Completed observations are saved.", file=sys.stderr)
                    return 1
                if row["execution_status"] != "completed" and 429 in row["provider_statuses"]:
                    print("Provider throttled this configuration. Completed results are saved. Resume after the limit resets.", file=sys.stderr)
                    return 1
                if gemini_ledger and row["execution_status"] == "completed":
                    gemini_run_budget = max(gemini_run_budget, row["gemini_quota"]["api_requests"])
                if row["execution_status"] == "completed" or not transient or attempt > args.retries:
                    break
                earliest = max(earliest, time.monotonic() + min(60, 10 * 2 ** (attempt - 1)))
            if row["execution_status"] != "completed":
                errors += 1
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted. Completed attempts are saved. Use --resume to continue.", file=sys.stderr)
        raise SystemExit(130)

