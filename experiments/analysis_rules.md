# ContextRoute — Experiment Analysis Rules

**Frozen at:** 2026-11-22T11:35:00+05:30  
**Experiment:** Two-model (Big Pickle, DeepSeek V4.1 Flash Free) × three-context (C1/C2/C3) joint routing benchmark  
**Repository:** `contextroute-shop` @ commit `6a04966b04f2bee261f11fbb203cd2bef3d9769c`

---

## 1. Model Selection

**Selected models:**
- **Model A:** `opencode/big-pickle` — highest valid execution coverage (12/12 valid, 12/12 pass, 100% success rate across prior 4-task grid)
- **Model B:** `opencode/deepseek-v4.1-flash-free` — second highest coverage (11/12 valid, 9/11 pass, 82% task-level success)

**Excluded:**
- `google/gemini-3.5-flash-lite` — requires `GEMINI_API_KEY` which is not available in this environment. Cannot be run.

**Provider access verification:** Big Pickle and DeepSeek are OpenCode-hosted; no external API key required. Access verified by prior successful grid executions.

---

## 2. Task Freeze Rules

- **Eight tasks** are frozen from `benchmark/tasks/tasks.json` (first 8 entries, in order).
- Task definitions (ID, category, prompt, verifier name) are fixed at freeze time and **must not be altered**.
- Prompts must not be changed to produce favorable results.
- **Reference patches, gold labels, and hidden-test solutions must never appear in model prompts.**
- Normal repository source files (shop/*.py, tests/*.py) remain eligible for context retrieval.
- Repository commit is pinned: `6a04966b04f2bee261f11fbb203cd2bef3d9769c` (reproducible via `fixture_repository()`).
- Context tiers C1/C2/C3 are fixed by the existing context generator. Context hash must be verified per-run.

---

## 3. Execution Protocol

- Run each arm in an isolated, clean Git worktree (disposable clone via `checkout_fixture()`).
- Use OpenCode v2.2.7 in `--standalone --archived --auto` mode.
- Deny `shell`, `external_directory`, and `subagent` permissions.
- Record actual primary model and context hash in every run.
- **Retry transient infrastructure errors at most twice** with exponential backoff (10s, 20s).
- **Distinguish** infrastructure errors (timeout, HTTP 5xx/429, process crash), legitimate task failures (verification failed), and missing observations.
- Prefer completing all 6 arms for one task before beginning another.
- Preserve all existing valid observations; never overwrite a completed valid run.
- Resume incomplete grids; skip cells that already have a valid accepted observation.

---

## 4. Telemetry and Recording

- Record per-run: `execution_id`, `task_id`, `model_id`, `context_id`, `context_hash`, `repository_commit`, `input_tokens`, `output_tokens`, `total_tokens`, `latency_seconds`, `wall_seconds`, `verification` outcome, `resolved` (bool), `execution_status`, `provider_statuses`.
- Token counts are provider-reported cumulative session totals (input + output = total).
- Include all model calls; do not double-count cached or repeated telemetry.
- Simulated cost (list price) only when verified rates exist. OpenCode-hosted free SKUs record `$0`.
- Never expose API credentials in records or logs.

---

## 5. Validity Criteria

A run is **valid** if and only if:
1. `execution_status == "completed"` with no integration errors.
2. Fixture commit matches the pinned revision.
3. Run mode is `forced`, OpenCode version is `v2.2.7`.
4. Actual observed model matches expected model.
5. Context hash is consistent across all trace events.
6. Exactly 6 pytest acceptance tests ran and their pass/fail count matches `resolved`.
7. Session was not reused across attempts.
8. For Gemini (not applicable here): quota reconciliation passes.

---

## 6. Grid Completion Criteria

- **Complete task:** All 6 arms (2 models × 3 tiers) have exactly one valid observation each, with matching context hashes per tier across models.
- **Target:** 8 complete task grids (or as many as the 4-hour time budget allows).
- Existing 3 complete tasks (`inventory-adjustment`, `pricing-discount`, `refund-balance`) count toward the target without re-running.
- If fewer than 8 complete grids are achieved, report what was completed and explain gaps.

---

## 7. Routing Policy Definitions

All four policies use **leave-one-task-out cross-validation** on the complete-task grids only.

| Policy | Model selection | Context selection |
|---|---|---|
| **Fixed** | `opencode/big-pickle` (always) | C2 (always) |
| **Model-only** | Adaptive (evidence-driven) | C2 (fixed) |
| **Context-only** | `opencode/big-pickle` (fixed) | Adaptive (evidence-driven) |
| **Joint** | Adaptive | Adaptive |

**Empirical thresholds** (existing, unmodified):
- Minimum training tasks per arm: **4**
- Wilson lower success bound: **≥ 0.50**
- Requires known average list cost per arm

**Fallback behavior** (existing, unmodified): If no pair qualifies empirically, retain the original model at C2 (or its smallest compatible pack if C2 is unavailable).

**Oracle policies:** Use held-out actual outcomes to find the minimum-token passing arm for each task per policy. Oracle results are clearly separated from actual routing results.

---

## 8. Primary Outcome

primary metric = total tokens per passed task (OpenCode-hosted cost is $0)

**Total model tokens per successfully verified task** — lower is better, provided correctness (pass rate) remains comparable.

**Secondary outcomes:**
- Pass rate (successes / tasks evaluated)
- End-to-end latency (mean and median seconds)
- Simulated cost per success (where reliable)
- Tokens per task (mean and median)
- Successful resolutions per million tokens
- Model–context ranking reversals (documented by task and tier pair)
- Evidence-driven selection rate (empirical vs fallback decisions)

---

## 9. Analysis Rules

- **Descriptive only** when fewer than 12 complete tasks are available (expected in this run).
- Do **not** claim statistical significance from task count alone.
- Do **not** tune routing thresholds or selectively include/exclude observations after inspecting outcomes.
- Report actual joint-router performance separately from oracle performance.
- If all routing decisions fall back to the fixed baseline, explicitly state: "Empirical joint routing was not evaluated."
- Check context hashes to distinguish genuine context differences from run-to-run variation.
- Interaction effects: Note any task × context ranking reversals observed; do not interpret as causal without replication.

---

## 10. Report Requirements

The final HTML report at `experiments/results/report.html` must include:
1. Experiment coverage (complete/partial/missing task counts)
2. Two-model × three-context performance matrix
3. Per-task outcomes (pass/fail per arm)
4. Total tokens and pass rates by policy
5. Fixed vs model-only vs context-only vs joint comparisons
6. Empirical vs fallback decision counts (reported separately)
7. Oracle comparison (clearly separated from actual routing)
8. Observed interaction effects (or explicit note of their absence)
9. Missing observations and their causes
10. Infrastructure error breakdown by category

---

## 11. Limitations

- Single execution per arm — no replication. Reversals cannot be distinguished from run-to-run variation without replication.
- One repository (`contextroute-shop`), one task type (Python bug/feature/refactor). Results may not generalize.
- OpenCode-hosted model rates are recorded at $0; simulated cost cannot be compared to Gemini.
- Gemini 3.5 Flash Lite is excluded; two-model experiment only.
- With fewer than 12 complete tasks, empirical routing thresholds require ≥5 complete tasks (4 training + 1 held-out) before any empirical decision is possible.
