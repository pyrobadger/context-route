import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { analyzeTask } from "../dist/context/task-analysis.js";
import { rankPairs, policyVersion } from "../dist/router/joint-optimizer.js";
import { benchmarkCoverage, readPublicCache, reviewedRuns } from "../dist/router/public-cache.js";
import { importRun, importStatus, updateBenchmarks } from "../dist/router/public-import.js";
import { pairedComparison, refineWithPublicBenchmark } from "../dist/router/public-routing.js";
import { reduceRouting, waitingState } from "../dist/plugin/desktop-state.js";
import { fixtureDate, fixtureRecord, submission } from "./fixtures/public-benchmark.mjs";

const [original, challenger, retired] = reviewedRuns;
const originalScore = fixtureRecord(original, 220);
const challengerScore = fixtureRecord(challenger, 300);
const cache = { records: [originalScore, challengerScore], update: null };
const mkModel = key => ({
  key, provider_id: key.split("/")[0], enabled: true,
  supports_tools: true, supports_text: true, context_window: 28000, pricing: [],
});
const file = (path, reason) => ({ path, reason, retrieval_score: 2 });
const pack = (context_id, actual_tokens, files) => ({
  context_id, actual_tokens, files, retrieval_version: "bm25-deps-v2",
});
function inputs(overrides = {}) {
  return {
    models: [mkModel(original.key), mkModel(challenger.key)],
    packs: [pack("C1", 700, [file("inventory.ts", "lexical")]),
      pack("C2", 1100, [file("inventory.ts", "lexical"), file("stock.ts", "dependency")]),
      pack("C3", 2200, [file("inventory.ts", "lexical"), file("stock.ts", "dependency"), file("unrelated.ts", "dependency")])],
    evidence: [], task: analyzeTask("Fix inventory.ts and stock.ts across modules", 2, false), original: original.key,
    ...overrides,
  };
}
const decide = (input, state = cache, now = fixtureDate) => refineWithPublicBenchmark(input, rankPairs(input), state, now);

test("official record parsing requires a checked, exact, complete submission", () => {
  const { metadata, results } = submission(original, 268);
  assert.equal(importRun(original, metadata, results, fixtureDate).resolved, 268);
  assert.throws(() => importRun(original, metadata.replace("checked: true", "checked: null"), results), /verification/);
  assert.throws(() => importRun(original, metadata.replace(original.label, "gemini-2.5-flash"), results), /identity/);
  assert.throws(() => importRun(original, metadata.replace("resolved: 53.6", "resolved: 29"), results), /do not agree/);
  assert.throws(() => importRun(original, metadata, "{}"), /500 exact task outcomes/);
  assert.throws(() => importRun(original, metadata.replace("attempts: 1", "attempts: 2"), results), /harness/);
});

test("updates fetch only relevant metadata and verdicts, reuse records, and exclude expired IDs", async () => {
  const directory = await mkdtemp(join(tmpdir(), "contextroute-public-"));
  const urls = [];
  const sources = async url => {
    urls.push(url);
    const run = url.includes(original.id) ? original : challenger;
    const files = submission(run, run === original ? 220 : 300);
    return url.endsWith(".yaml") ? files.metadata : files.results;
  };
  try {
    assert.deepEqual(await readPublicCache(directory), { records: [], update: null });
    const first = await updateBenchmarks([original.key, original.key + "#high", retired.key], directory, sources, fixtureDate);
    assert.equal(first.imported, 1);
    assert.equal(urls.length, 2);
    assert.ok(urls.every(url => url.includes(original.id) && !/logs|trajs|all_preds/.test(url)));
    assert.equal(importStatus(first.cache, fixtureDate).unmatched.length, 2);
    const second = await updateBenchmarks([original.key, challenger.key], directory, sources, fixtureDate);
    assert.equal(second.imported, 1);
    assert.equal(second.reused, 1);
    assert.equal(urls.length, 4);
    const status = importStatus(second.cache, fixtureDate);
    assert.equal(status.available, 2);
    assert.equal(status.cachedRecords, 2);
    assert.equal(status.matched.length, 2);
    assert.equal(benchmarkCoverage([original.key, original.key + "#high"], second.cache, fixtureDate).matched, 1);
    await writeFile(join(directory, original.id + ".json"), "{damaged", "utf8");
    assert.equal((await readPublicCache(directory)).records.length, 1);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("failed source imports preserve previously verified records", async () => {
  const directory = await mkdtemp(join(tmpdir(), "contextroute-public-error-"));
  try {
    const selected = submission(original, 268);
    await updateBenchmarks([original.key], directory, async url => url.endsWith(".yaml") ? selected.metadata : selected.results, fixtureDate);
    const attempted = await updateBenchmarks([original.key, challenger.key], directory, async () => { throw new Error("HTTP 503"); }, fixtureDate);
    assert.equal(attempted.cache.records.length, 1);
    assert.deepEqual(attempted.cache.update.errors, [{ key: challenger.key, reason: "HTTP 503" }]);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("matched public verdicts select a model and the smallest adequate repository pack", () => {
  const decision = decide(inputs());
  assert.equal(decision.decision_type, "public_benchmark_informed");
  assert.equal(decision.selected.model, challenger.key);
  assert.equal(decision.selected.context, "C2");
  assert.equal(decision.publicBenchmark.improvements, 80);
  assert.equal(decision.publicBenchmark.selected.resolved, 300);
  assert.equal(decision.publicBenchmark.selected.tasks, 500);
  assert.equal(decision.selected.observed_success, null);
  const scoped = decide(inputs({ task: analyzeTask("Fix inventory.ts", 1, false) }));
  assert.equal(scoped.selected.context, "C1");
});

test("paired comparison refuses different task cohorts and inconclusive differences", () => {
  assert.equal(pairedComparison(originalScore, fixtureRecord(challenger, 300, { shift: 1 })), null);
  assert.equal(pairedComparison(originalScore, { ...challengerScore, harnessVersion: "2.0.0" }), null);
  assert.equal(pairedComparison(originalScore, { ...challengerScore, reasoning: "high" }), null);
  assert.equal(pairedComparison(originalScore, { ...challengerScore, evaluationDate: "2025-08-01" }), null);
  assert.equal(pairedComparison(originalScore, fixtureRecord(challenger, 221)).p, 1);
  const close = { ...cache, records: [originalScore, fixtureRecord(challenger, 221)] };
  assert.equal(decide(inputs(), close).decision_type, "cold_start_fallback");
});

test("unknown models, suffixes, expired identities and unsatisfied cost constraints keep original C2", () => {
  for (const key of ["opencode/big-pickle", original.key + "#high", "google/gemini-pro-latest"]) {
    const result = decide(inputs({ original: key, models: [mkModel(key), mkModel(challenger.key)] }));
    assert.equal(result.decision_type, "cold_start_fallback");
    assert.equal(result.selected.model, key);
    assert.equal(result.selected.context, "C2");
    assert.equal(result.policy_version, policyVersion);
  }
  assert.equal(decide(inputs(), cache, new Date("2026-12-02T00:00:00Z")).decision_type, "cold_start_fallback");
  assert.equal(decide(inputs({ constraints: { max_cost_usd: 0 } })).decision_type, "no_feasible_pair");
});

test("an unscored session can switch to a clear winner among equally priced available models", () => {
  const unscored = "opencode/beam-free";
  const zero = { input: 0, output: 0, cache_read: 0, cache_write: 0 };
  const models = [unscored, original.key, challenger.key].map(key => ({ ...mkModel(key), pricing: [zero] }));
  const input = inputs({ original: unscored, models, task: analyzeTask("Fix inventory.ts", 1, false) });
  const decision = decide(input);
  assert.equal(decision.policy_version, "public-benchmark-pool-v1");
  assert.equal(decision.selected.model, challenger.key);
  assert.equal(decision.selected.context, "C1");
  assert.equal(decision.publicBenchmark.original, null);
  assert.equal(decision.publicBenchmark.reference.key, original.key);
  const state = reduceRouting(waitingState(), {
    type: "route", session_id: "sess-fixture", original_model: unscored, selected_model: challenger.key,
    decision_source: decision.decision_type, policy_version: decision.policy_version, public_benchmark: decision.publicBenchmark,
  }, "sess-fixture", ".");
  assert.equal(state.route.publicBenchmark.reference.key, original.key);

  const expensive = [...models.slice(0, 2), { ...models[2], pricing: [{ ...zero, output: 1 }] }];
  const disabled = [...models.slice(0, 2), { ...models[2], enabled: false }];
  const close = { ...cache, records: [originalScore, fixtureRecord(challenger, 221)] };
  const different = { ...cache, records: [originalScore, fixtureRecord(challenger, 300, { shift: 1 })] };
  for (const result of [decide({ ...input, models: expensive }), decide({ ...input, models: disabled }),
    decide(input, { ...cache, records: [challengerScore] }), decide(input, close), decide(input, different)]) {
    assert.equal(result.decision_type, "cold_start_fallback");
    assert.equal(result.selected.model, unscored);
    assert.equal(result.selected.context, "C2");
    assert.match(result.reasons.at(-1), /^Public benchmark check/);
  }
});

test("the unchanged local empirical optimizer takes priority", () => {
  const evidence = Array.from({ length: 4 }, (_, i) => ({
    execution_id: "exec-" + i, task_id: "task-" + i, task_category: "bug", repository_id: "sample",
    repository_commit: "fixture", model_id: original.key, context_id: "C2", context_hash: "a".repeat(64),
    retrieval_version: "bm25-deps-v2", resolved: true, verification_type: "task_tests", input_tokens: 100,
    output_tokens: 20, total_tokens: 120, latency_seconds: 3, estimated_list_price_usd: 0.001, execution_status: "completed",
  }));
  const input = inputs({ evidence });
  const local = rankPairs(input);
  assert.equal(local.decision_type, "empirical");
  assert.strictEqual(refineWithPublicBenchmark(input, local, cache, fixtureDate), local);
});

test("Desktop displays validated public provenance and drops unsafe source URLs", () => {
  const decision = decide(inputs());
  const row = {
    type: "route", session_id: "sess-fixture", original_model: original.key, selected_model: challenger.key,
    decision_source: decision.decision_type, public_benchmark: decision.publicBenchmark,
    public_benchmark_coverage: { available: 3, matched: 2, lastUpdate: fixtureDate.toISOString() },
    reasons: decision.reasons,
  };
  const state = reduceRouting(waitingState(), row, "sess-fixture", ".");
  assert.equal(state.route.publicBenchmark.selected.resolved, 300);
  assert.equal(state.route.publicCoverage.matched, 2);
  assert.equal(state.route.reasons.length, 2);
  row.public_benchmark.selected.sourceUrl = "https://example.org/redirect";
  assert.equal(reduceRouting(waitingState(), row, "sess-fixture", ".").route.publicBenchmark, null);
});

