# ContextRoute

ContextRoute is a native OpenCode plugin for deterministic model and repository-context routing. It compares compatible model–context pairs using verified task outcomes and measured whole-task cost. Without enough evidence, it keeps the current model and prefers C2 when compatible. The server plugin supports OpenCode CLI 2.2.7. The native panel was verified in OpenCode Desktop 2.3.2.

## Install on Windows

Use Node.js 24+, Git, and OpenCode 2.2.7. Python 3.13 and pytest are required for the benchmark and evaluation commands.

```powershell
Set-Location E:\Programing\context_route_new
opencode --version
opencode models
npm ci
npm run build
python -m pip install -r benchmark/requirements.txt
$pluginUri = ([Uri] (Resolve-Path .).Path).AbsoluteUri
$pluginUri
```

Add the plugin directory URI printed by PowerShell to the existing `plugins` array in the target project's `opencode.jsonc`. The target project uses its existing provider credentials and model selection. Restart its OpenCode server after adding the plugin.

```jsonc
{
  "plugins": [
    {
      "package": "file:///E:/Programing/context_route_new",
      "options": {
        "mode": "auto",
        "contexts": ["C1", "C2", "C3"],
        "constraints": { "min_samples": 4, "min_success": 0.50 }
      }
    }
  ]
}
```

Open a normal OpenCode session in the target checkout. For a standalone CLI demonstration, run these commands there and substitute an available model and task.

```powershell
opencode run --standalone --model 'opencode/big-pickle' 'Fix the stock availability calculation in shop/inventory.py.'
Get-Content .contextroute/execution.jsonl
```

`npm pack` builds a distribution with the server entry point and compiled modules. OpenCode loads the built local directory through its `./server` export. The compiled plugin has no npm runtime dependencies. The pinned SDK provides the development types.

## OpenCode Desktop installation

Build both entries with `npm ci` and `npm run build` from this repository. Keep the project-level `plugins` entry from the Windows installation above. Add the same package to the global OpenCode configuration at `$HOME/.config/opencode/opencode.jsonc`. For Desktop discovery only, set `desktop_only` in the global entry. In projects with the local entry, the local routing options take precedence. The global entry alone leaves routing disabled.

```jsonc
{
  "plugins": [
    {
      "package": "file:///E:/Programing/context_route_new",
      "options": { "desktop_only": true }
    }
  ]
}
```

Merge the entry with any existing `plugins` array. If the background server started before the GUI export was added, wait for active tasks to finish and restart the background service with the CLI that matches your Desktop version. On this Windows installation the command is:

```powershell
& "$env:APPDATA/ai.opencode.desktop/cli/2.3.2-x64/opencode-cli.exe" service restart
```

Open the project in Desktop, enter a session, and run **ContextRoute: Show Routing Decision** from the command palette. The default Windows shortcut is **Ctrl+K**. You can also open **Session Context** and select the **ContextRoute** tab in the side pane. A new draft gets a session panel after the first prompt creates a session.

The native tab and command use OpenCode's experimental `@opencode/plugin/gui` SDK. The package exports `./gui-experimental` as a bundled Solid view. The backend uses OpenCode's authenticated RPC client to serve a read-only snapshot and signal changes for the active session. It replays saved telemetry for that session when opened after a restart. The panel never reads provider credentials, raw prompts, or source files. See the [OpenCode GUI plugin reference](https://opencode.ai/docs/build/plugins/gui/) and [plugin RPC reference](https://opencode.ai/docs/build/plugins/rpc/).

### What the panel shows

- **Empirical joint routing** means that the selected pair passed the configured evidence requirements. Expand **Why this configuration?** for the recorded reasons, historical measures, and qualifying alternatives when available.
- **Cold-start fallback** means no pair met those requirements. The router prefers a compatible C2 pack while retaining the original model. **Forced experimental configuration** identifies an arm explicitly selected for measurement.
- **Model verified** means the first primary request used the applied model, including its exact variant. **Context delivered — verified** means the recorded pack hashes matched at the request hook. The panel distinguishes that hook check from an HTTP-body check when the latter was recorded.
- Expand **Files and lines sent** under **Initial repository context** to see each verified excerpt's path and line range. The panel shows the selected ranges before verification. It displays repository metadata and keeps source text out of the panel and default telemetry.
- **Execution measurements** summarize the completed primary steps when telemetry includes them. The panel shows the recorded list-price estimate and the execution duration, without inferring savings against an unmeasured baseline.

### Desktop troubleshooting

| Symptom | Check |
|---|---|
| The plugin or command is missing | Build `dist/gui-experimental.cjs`, verify the same local package URI is in project and global `opencode.json(c)`, then check **Settings → Extensions → Plugins**. Restart the matching background server if it was already running when the export was added. |
| The panel is waiting for a task | Submit a prompt in that session. In the project log, `plugin_loaded` confirms startup and `route` confirms a routing decision. |
| The panel stops updating | Check that Desktop is connected to the session's server. Use **Retry** if the panel reports that status is unavailable. A remote session needs ContextRoute configured on its own server. |
| The wrong repository or session seems to be showing | Check the repository name in the panel header and the current session tab. Open the intended session in Desktop and check its own `.contextroute/execution.jsonl`. |
| A friendly model name is missing | The panel displays OpenCode's recorded model ID and variant when metadata is unavailable. |
| Context hash or model mismatch | Compare the `route` and `first_primary_request` records for the same task and session, then check any associated `integration_error`. A mismatch stays unverified in the panel. |

## Presenting the Desktop demo

From the ContextRoute checkout, start the local demo and keep its terminal open.

```powershell
npm run build
npm run demo
```

The terminal prints two temporary project folders and one prompt. Open each folder in OpenCode Desktop, start a fresh session on **Demo Default**, and submit that prompt. Press **Ctrl+K** and select **ContextRoute: Show Routing Decision** to open the panel.

- **1-retain-model** shows **Model retained**. The router uses C2 and OpenCode sends the primary request to Demo Default.
- **2-switch-model** shows **Model switch verified**. The same optimizer uses sample evidence to select Demo Routed and C1. OpenCode records the switch in the conversation, and the loopback provider records the model and context hash it received.

Both workspaces use local simulated providers. The panel identifies the simulated responses, token counts, and routing evidence. The demo shows model switching and context delivery. The benchmark reports separately measure routing performance.

## What is applied

| Integration step | OpenCode 2.2.7 interface |
|---|---|
| Capture a submitted prompt | `session.hook("prompt")`, at top-level prompt admission |
| Identify checkout and original model | `session.get`, including the session location and model, with agent and catalog defaults as fallbacks |
| Discover models | `model.list` for the active location. Preserve exact provider, model, and non-default variant IDs |
| Switch the model | `session.switchModel` inside admission, then read the selected session model back |
| Attach repository context | `session.hook("context")` attaches the exact selected pack as an outgoing context block before the first primary request |
| Verify dispatch | `model.request` records primary model identity. Benchmarks additionally verify exact pack text or baseline absence in the native HTTP body |
| Observe usage and completion | Public step, tool, execution, and cumulative usage events. Step IDs deduplicate primary usage. Auxiliary request kinds are recorded separately |

The project uses the v2 `@opencode/plugin` SDK. The workstation had no v1 `@opencode-ai/plugin` installation. Phase 0 verified the session switch against a local HTTP provider before implementation. The automated integration test uses two loopback providers and verifies the model ID and exact pack at the receiving endpoint during a normal OpenCode task. It uses temporary evidence as a test input. The provider smoke runs use a real configured provider and task-specific tests. See the [OpenCode server plugin reference](https://opencode.ai/docs/build/plugins/server/) and [plugin installation reference](https://opencode.ai/docs/plugins/).

ContextRoute evaluates each submitted text task once. It observes the actual selected model on primary provider requests and retains the session model if the optimizer selects it. Context injection applies to the first primary step, including its retries. Later tool calls and model requests use OpenCode's usual agent loop and may consume additional tokens. Session switches persist in the OpenCode session. The confirmation mode keeps the current model and logs a suggested model and tier. Select that model in OpenCode and resubmit to confirm. Concurrent prompt admission and incompatible or failed switches are logged. Ordinary sessions continue through recoverable routing errors.

## Retrieval and evidence

- C1 uses ranked BM25 excerpts with a 1,200-token ceiling. C2 uses up to 3,200 tokens and adds first-hop imports, callers, and nearby tests. C3 adds second-hop relationships at a 6,400-token ceiling. L1–L3 use lexical retrieval at the same respective ceilings for controlled comparisons.
- The scanner reads the checkout locally. Git-tracked files are preferred. An empty Git index on a new branch uses non-ignored candidates. Outside Git, source files are discovered by walking the directory. Size, type, symlink, environment-file, and common credential filters run before indexing. Secret filtering is heuristic. A repository can contain sensitive material under arbitrary names.
- Each pack records its exact text, strategy, retrieval version, SHA-256 digest, and relevant line ranges. The tokenizer estimates consumption using the greater of UTF-8 bytes divided by four and the number of word or punctuation pieces. The budget covers the rendered context block. Provider tokenizers may differ.
- Evidence is read from a JSON file, JSONL file, or a directory of atomically written JSON records. A usable record has verified task outcomes, a completed execution, exact model and tier IDs, and the matching retrieval version. Observations are deduplicated by execution ID and then by repository and task within a pair. At most one execution contributes to each task's Wilson estimate.
- For a category with enough observations, the router uses category-specific evidence. Otherwise it pools evidence for the exact pair. The default requirement is four distinct tasks and a 95% Wilson lower confidence bound of at least 0.50. Among qualifying pairs, the router minimizes mean simulated list price across complete tasks, then measured tokens, then measured latency. A separate estimate divides mean cost by the success lower bound where available.
- Whole-task cost estimates use OpenCode session usage and catalog prices, including cache-aware pricing where OpenCode reports it. When prices or measurements are missing, the router preserves the unknown values. Optional resource constraints require measured support. Reported provider billing is unavailable.

| Plugin option | Behavior |
|---|---|
| `mode` | `auto` applies a supported recommendation. `confirm` asks for a manual model selection if a switch would be required. `context-only` keeps the selected model and uses compact C0 context. The runner supplies strict `baseline`, `forced`, and `joint` modes |
| `models`, `contexts` | Optional exact model allowlist and context tier list |
| `fallback_context` | Preferred context tier for provisional decisions. Defaults to C2 |
| `evidence` | Defaults to `.contextroute/evidence` in the target checkout |
| `telemetry_path`, `telemetry` | Defaults to `.contextroute/execution.jsonl`. Set telemetry to `false` to disable ordinary-session logging |
| `pack_path` | Optional path to save the latest exact pack as JSON. Benchmark traces save a pack per routed execution |
| `constraints` | Supports `min_samples`, `min_success`, `max_context_tokens`, `max_total_tokens`, `max_cost_usd`, `max_latency_seconds`, `reserved_input_tokens`, and `reserved_output_tokens` |

### Fallback and replay interpretation

The current policy is `default-tier-fallback-v2`. A `cold_start_fallback` records `no_usable_evidence` when compatible candidates lack observations and `insufficient_evidence` when observations exist but no eligible pair clears the evidence gates. The fallback keeps the original model and prefers the configured `fallback_context` tier, which defaults to C2. If that tier is unavailable, empty, or exceeds a limit, it uses the smallest compatible nonempty pack for the original model. Ties use known initial-pack price and deterministic tier order. With no eligible original-model pack, the router abstains and OpenCode retains its original session configuration.

Initial context size covers only the pack injected into the first primary request. Even identical packs can produce different whole-session token counts. Fallback ranking does not use historical whole-task cost, token count, or latency to order candidates. Configured resource caps still apply. Empirical ranking uses measured whole-task metrics after the evidence gates pass.

The fixed grid reference is Big Pickle at C2. With the revised fallback, all three adaptive policies prefer Big Pickle C2 whenever it satisfies their candidate restrictions. The pinned grid runtime preserves the original forced executions. Replay uses the current policy and labels the original minimum-pack fallback separately. The revised fallback was chosen after inspecting the original pilot. A replay of the same observations is a prospective policy check; independent validation requires new held-out tasks.

Default JSONL records include metadata and counters. Source excerpts and raw prompts are kept out of that log. The optional pack artifact contains source text.

## Public benchmark routing

Ordinary OpenCode sessions first use the existing local empirical joint optimizer. If it has no eligible pair, ContextRoute can use a cached public benchmark comparison to choose a model and select a context pack from the current repository. It records `public_benchmark_informed` for that decision. If comparison evidence is absent or inconclusive, `default-tier-fallback-v2` retains the original model and prefers C2. Forced benchmark runs and historical empirical replays keep their existing protocols.

Run the update from the repository whose OpenCode model catalog you want to check. With the npm package installed, use `contextroute benchmarks update` and `contextroute benchmarks status`. With a locally built checkout, use the equivalent commands below.

```powershell
node E:/Programing/context_route_new/dist/cli.js benchmarks update
node E:/Programing/context_route_new/dist/cli.js benchmarks status --json
```

The updater uses `opencode models` and writes to `~/.contextroute/benchmarks/`. It downloads only reviewed submissions for exact IDs present in that catalog and reuses immutable records it already verified. `status` works offline and reports the catalog from the latest update. Run `update` again after changing configured models. `CONTEXTROUTE_OPENCODE` can select a specific native executable, and `CONTEXTROUTE_BENCHMARK_CACHE` can isolate the cache for a test or a separate environment.

The first adapter reads metadata and per-task verdicts from [SWE-bench Experiments](https://github.com/swe-bench/experiments) at a pinned revision. It checks the leaderboard verification flag, reviewed model identity, one-attempt mini-SWE-agent protocol, complete 500-task verdicts, and agreement with the published score. The initial reviewed mappings are in `src/router/public-cache.ts`. Provider ID, model version, and reasoning variant must match exactly. The cache enforces reviewed provider redirection and retirement dates offline. Files from an unreviewed source, rolling aliases, and submissions with missing or inconsistent verdicts carry no routing authority.

For each prompt, ContextRoute matches the current OpenCode catalog to cached public records by exact model ID. With a score for the current model, an alternative must show a clear advantage on the same tasks under the same evaluation conditions. Without a score for the current model, ContextRoute can compare two or more available alternatives with known list rates no higher than the current model's. A switch requires a clear paired advantage over every eligible scored alternative. Both comparisons apply the existing significance threshold with correction for the comparisons made. The panel identifies which model was compared and shows when the current model has no matching score. With insufficient or inconclusive public evidence, the normal fallback applies.

ContextRoute indexes eligible files locally. It sends a bounded pack of excerpts using paths named in the prompt, lexical matches, dependencies, and nearby tests. The model's limits and the configured token budgets apply. Public scores describe the model's results under the recorded harness and provide no measurement of the repository pack or its effect on a specific task.

The Desktop panel shows the routing source and cache coverage at decision time. For public decisions it also shows the exact benchmark model version, numerator and denominator, evaluation date, paired results, limitations, and a link to the pinned submission. **Auto** can apply a qualified public decision. **Confirm** retains its existing manual model-switch behavior.

## Compact context pilot

Set `"mode": "context-only"` in the project's ContextRoute plugin options to try compact context with the current model. When the prompt names one or two identifiable source files, C0 sends at most one ranked excerpt per named file within a 500 estimated-token budget. For prompts without identifiable files, or when the compact pack would omit a named file, OpenCode proceeds with its usual context and tools.

The Desktop panel compares the estimated C0 and C2 pack sizes for the same prompt and repository snapshot. ContextRoute builds the C2 reference locally and sends the C0 pack alone. The break-even figure is the estimated size of the added C0 pack. Compared with plain OpenCode, later calls would have to save more than that amount for whole-task usage to decrease. The panel shows these estimates separately from execution measurements.

The 500-token cap applies to the injected repository excerpts. Measure whole-task input and output as well because OpenCode can make several model calls and read additional files. For the pilot, compare the same model, prompt, repository revision, and verifier with ContextRoute injection disabled and with C0 forced. Count a quality-preserving saving only when both arms pass the same verifier. Save the pilot separately from the existing grids.

```powershell
python -m benchmark.runner.run --task inventory-adjustment --task pricing-discount --task refund-balance --original-model opencode/beam-free --contexts C0 --baseline --output benchmark/results/compact-context-pilot --timeout 100 --interval 12 --retries 0 --resume
python -m benchmark.evaluator.report --runs benchmark/results/compact-context-pilot/runs --output benchmark/results/compact-context-pilot/report.html
```

## Run the smoke test and report

One task, one original configuration, and one forced routed configuration test both the provider path and correctness. Exact model availability and pricing come from the system's OpenCode configuration.

```powershell
python -m benchmark.runner.run --task inventory-adjustment --original-model opencode/big-pickle --models opencode/deepseek-v4.1-flash-free --contexts C2 --baseline --output benchmark/results/final-smoke --timeout 75 --retries 0 --dry-run
python -m benchmark.runner.run --task inventory-adjustment --original-model opencode/big-pickle --models opencode/deepseek-v4.1-flash-free --contexts C2 --baseline --output benchmark/results/final-smoke --timeout 75 --retries 0 --resume
python -m benchmark.evaluator.evaluate --runs benchmark/results/final-smoke/runs --output benchmark/results/final-smoke/evaluation.json
python -m benchmark.evaluator.report --runs benchmark/results/final-smoke/runs --evaluation benchmark/results/final-smoke/evaluation.json --output benchmark/results/final-smoke/report.html
Invoke-Item benchmark/results/final-smoke/report.html
```

The runner creates a reproducible Git base commit and restores it in a disposable clone for every attempt. Hidden checks and reference fixes stay outside the execution checkout. Verification is run separately, using pristine storefront tests and a task-specific check. A completed execution with failed tests is an observed failure. A timeout, unavailable model, incomplete telemetry, or unenforced configuration is retained as a diagnostic run and excluded from routing evidence. The runner is serial and saves every completed attempt atomically. `--resume` skips complete records with a matching protocol. Server failures and timeouts permit bounded clean retries. An unresolved HTTP 429 pauses the grid until provider access is available again. `--interval` spaces task starts, and `--retries` controls additional attempts. One OpenCode task can make many provider requests. Use model credentials and a run interval appropriate for the account's documented limits. If OpenCode is installed outside PATH, set `$env:CONTEXTROUTE_OPENCODE` to its native executable path.

## Collect evidence and evaluate policies

Run these separately after reviewing the smoke report. Keep the primary study grid defined before collecting data.

```powershell
$models = @('opencode/big-pickle', 'opencode/deepseek-v4.1-flash-free')
python -m benchmark.runner.run --models $models --contexts C1 C2 C3 --limit 8 --output benchmark/results/grid --interval 20 --resume --dry-run
python -m benchmark.runner.run --models $models --contexts C1 C2 C3 --limit 8 --output benchmark/results/grid --interval 20 --resume
python -m benchmark.evaluator.study --output benchmark/results/grid --limit 8
python -m benchmark.evaluator.report --runs benchmark/results/grid/runs --evaluation benchmark/results/grid/evaluation.json --output benchmark/results/grid/report.html
python -m benchmark.runner.run --task orders-export --original-model $models[0] --models $models --contexts C1 C2 C3 --baseline --joint --no-grid --evidence benchmark/results/grid/runs --output benchmark/results/held-out-demo --interval 20 --resume
```

Offline replay runs fixed, model-only, context-only, and joint policies through the same TypeScript optimizer. It trains on other repositories where available, or other tasks for a single-repository pilot. It scores the decision using only matching outcomes for held-out tasks. Incomplete matrices are excluded from policy metrics. Rank reversal analysis compares success counts on matched tasks. The report also shows measured default-versus-routed runs, task verification, and absolute resource metrics. Changes are displayed for paired validated runs with meaningful baselines. The smoke comparison is forced and provides no evidence of the optimizer improving task outcomes or generalizing across repositories.

## Audit the eight-task grid

Run the grid audit after the provider runs. It independently checks the disposable checkout location, revision, exact model and context on the wire, usage, and verification against each raw trace. It excludes duplicate or conflicting cells from held-out replay.

```powershell
python -m benchmark.evaluator.study --output benchmark/results/grid --limit 8
python -m benchmark.evaluator.report --runs benchmark/results/grid/runs --evaluation benchmark/results/grid/evaluation.json --output benchmark/results/grid/report.html
```

The single-repository grid uses leave-one-task-out folds. Eight complete grids give each fold seven distinct training tasks. At least five complete grids are needed for any such fold to reach four training tasks. Count eligibility alone is insufficient. Each empirical selection also requires the unchanged Wilson lower bound of 0.50, qualifying cost evidence, and the configured resource constraints. A candidate with four successes in four training tasks clears the default Wilson bound. Held-out outcomes are used only after selection.

The full replay includes fallbacks. The empirical section pairs empirical joint decisions with the fixed reference on the same tasks and compares the adaptive policies on the common folds where all made empirical decisions. It reports decision coverage, empty cohorts, and each fold's training count. These conditional comparisons complement the full replay.

The grid directory contains `grid-attempts.jsonl`, `coverage.json`, `evaluation.json`, the HTML report, and standalone SVG charts. The historical report and evaluation are under `history/initial-pack-fallback-v1`. The existing forced grid uses its pinned `benchmark-runtime` so future application builds cannot change resume keys. Before contacting a provider, a pinned resume checks its matching protocol keys and skips completed cells, including verified task failures. It takes no inference requests to calculate the resume plan. An unresolved HTTP 429 pauses the grid without changing previously saved attempts.

## Gemini 3.5 Flash-Lite pilot

The Gemini pilot uses `google/gemini-3.5-flash-lite` through the native OpenCode Google adapter and the directly connected Google AI Studio project. Connect Google in OpenCode or set `GEMINI_API_KEY` in the PowerShell session. The runner makes that environment key available under OpenCode's native name, `GOOGLE_GENERATIVE_AI_API_KEY`. An existing OpenCode Google connection can supply the key. The [official model metadata](https://aistudio.google.com/docs/models/gemini-3.5-flash-lite) lists function calling and standard inference is [available on the free tier](https://aistudio.google.com/docs/pricing#gemini-3.5-flash-lite). Confirm that the connected credential uses the project with the stated free limits before running. The OpenCode catalog's simulated list price is not actual billing.

```powershell
python -m pip install -r benchmark/requirements.txt
npm run build
python -m benchmark.runner.run --models google/gemini-3.5-flash-lite --contexts C1 C2 C3 --limit 1 --gemini-quota --output benchmark/results/gemini-pilot --timeout 420 --interval 20 --resume --dry-run
python -m benchmark.runner.run --models google/gemini-3.5-flash-lite --contexts C1 C2 C3 --limit 1 --gemini-quota --output benchmark/results/gemini-pilot --timeout 420 --interval 20 --resume
python -m benchmark.evaluator.gemini_pilot --output benchmark/results/gemini-pilot
```

`--gemini-rpm`, `--gemini-tpm`, and `--gemini-rpd` can lower the 10 RPM, 200,000 input TPM, and 400 RPD guard limits. These defaults are below the supplied project ceilings of 15 RPM, 250,000 TPM, and 500 RPD. The native Google transport is confined to a per-attempt authenticated loopback gate, which forwards only the exact model's standard Gemini inference to Google's API. The gate counts the original structured payload using Google's `countTokens` before generation. It budgets that estimate plus headroom, then records actual input and output from usage metadata when present. All physical Google calls through the gate, including token-count calls and auxiliary generations, count toward the local request budgets. Reservations are atomic and shared across invocations in `.contextroute/gemini-quota.sqlite3`. Neither credentials nor request bodies are saved by the gate.

Google applies [input TPM and per-project quotas](https://aistudio.google.com/docs/rate-limits). The guard uses rolling 60-second windows and resets the daily counter at midnight Pacific. Reservations survive interruption, with actual usage increasing a reservation if needed. Before starting another task, the runner checks that the remaining daily budget can cover the largest recorded completed run in that Gemini experiment, with an initial minimum of two calls. The local ledger sees only guarded traffic. Google's project-wide remaining quota can be lower if other clients share the project. An upstream HTTP 429 stops the benchmark and saves the receipt. The guard honors Retry-After and Google's retry delay. A daily limit or a 429 without reset information pauses guarded traffic until the next Pacific reset. Credential and model checks precede routing; ContextRoute excludes this Gemini model from automatic candidate selection when a quota gate is absent.

OpenCode resolves session titles independently from coding requests. The Gemini runner pins the hidden `title` agent to `google/gemini-3.5-flash-lite#minimal` and keeps the primary coding model at `google/gemini-3.5-flash-lite`. Both use the same exact-model quota gate. The runner reconciles the title and primary request counts, token-count calls, provider model versions, and usage counters before accepting a cell. A resume can reuse an older completed cell only when its trace and counters verify the same title model and accounting.

Review the three pilot cells and the quota summary before expanding. Once the pilot is validated and the project has capacity, use the same command with `--limit 8` to resume in the same Gemini output directory. The original Big Pickle and DeepSeek grid uses its separate pinned benchmark runtime and data.

## Three-model matched study

Regenerate the analysis from the separate saved grids with the existing compiled optimizer. Both commands run offline.

```powershell
python -m benchmark.evaluator.cross_model
python -m benchmark.evaluator.cross_report
```

Open `benchmark/results/cross-model-study/report.html`. The directory also contains the evaluation JSON with source fingerprints, the held-out decision trace, the separate oracle results, and standalone SVG charts. The available-data view marks every missing or failed cell. The matched view includes only tasks with nine valid arms and identical context hashes across the three models at each tier.

The fixed baseline is `opencode/big-pickle` at C2. The leave-one-task-out replay uses `default-tier-fallback-v2` and the optimizer's existing evidence requirements. The oracle selects by observed verification and then token count using each held-out task's recorded outcomes. Provider token counts and OpenCode catalog cost estimates are reported separately from acceptance, and the Gemini gateway can add latency.

## Test

```powershell
npm test
npm run test:integration
python -m pytest -q benchmark/tests
python -m benchmark.runner.run --check-fixtures
npm pack --dry-run
```

The strict benchmark wire check currently requires a readable native JSON HTTP request. Other transports can still use the ordinary routing hooks. Runtime dispatch evidence identifies the selected model, while provider-specific token reporting may omit failed physical attempts or background work. The grid runner denies shell commands, external-directory access, and subagents in model sessions. Verification runs separately against a copy of each disposable checkout. Corpus and verifier code are controlled fixtures. The evaluation is descriptive until enough complete held-out tasks have been collected.

