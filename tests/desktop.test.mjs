import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { formatContextReduction, formatContextTokens, modelDisposition, routingSourceLabels } from "../dist/plugin/desktop-format.js";
import { desktopState, parseEvent, readSessionState, reduceRouting, waitingState } from "../dist/plugin/desktop-state.js";

const checkout = join(tmpdir(), "sample-project");
const original = "opencode/deepseek-v4.1-flash-free#high";
const alternative = "mock/candidate";
const digest = "a".repeat(64);
const session = "ses_sample";
const task = "msg_sample";

function route(fields = {}) {
  return {
    type: "route", session_id: session, task_id: task, original_model: original,
    selected_model: original, applied_model: original, context_id: "C2", initial_context_tokens: 2059,
    decision_source: "cold_start_fallback", fallback_reason: "insufficient_evidence", context_hash: digest,
    original_model_metadata: { key: original, display_name: "DeepSeek V4.1 Flash Free" },
    policy_version: "default-tier-fallback-v2",
    context: { budget_tokens: 3200, strategy: "dependencies",
      files: [{ path: "sample.ts" }, { path: "sample.ts" }, { path: "another.ts" }] },
    reasons: ["Retained the original model and used the provisional default tier C2."],
    ...fields,
  };
}

function step(state, event) {
  return reduceRouting(state, { session_id: session, task_id: task, ...event }, session, checkout);
}

test("parsed cold-start decisions retain original identifiers and report real context usage", () => {
  const state = step(waitingState(), parseEvent(JSON.stringify(route())));
  assert.equal(state.stage, "routing");
  assert.equal(state.route.source, "cold_start_fallback");
  assert.equal(routingSourceLabels[state.route.source], "Cold-start fallback");
  assert.equal(state.route.original.id, original);
  assert.equal(state.route.original.name, "DeepSeek V4.1 Flash Free");
  assert.equal(state.route.applied.id, original);
  assert.deepEqual([state.route.initialTokens, state.route.budget, state.route.files], [2059, 3200, 2]);
  assert.equal(state.contextCheck, "pending");
  assert.equal(state.modelCheck, "pending");
});

test("empirical decisions show supporting measurements and separate the original and selected models", () => {
  const chosen = { model: alternative, context: "C2", evidence_count: 5, observed_success: 1,
    success_lower_bound: 0.56, average_tokens: 915, average_cost_usd: 0.02, constraints_met: true };
  const state = step(waitingState(), route({ decision_source: "empirical", selected_model: alternative,
    applied_model: alternative, routing_evidence_limits: { min_samples: 4, min_success: 0.50 },
    alternatives: [chosen, { ...chosen, model: original }, { ...chosen, constraints_met: false },
      { ...chosen, context: "C1", evidence_count: 2 }] }));
  assert.equal(state.route.original.id, original);
  assert.equal(state.route.recommended.id, alternative);
  assert.equal(routingSourceLabels[state.route.source], "Empirical joint routing");
  assert.equal(state.route.applied.id, alternative);
  assert.equal(state.route.evidence.evidence, 5);
  assert.equal(state.route.evidence.wilson, 0.56);
  assert.equal(state.route.evidence.cost, 0.02);
  assert.equal(state.route.alternatives.length, 1);
  assert.equal(state.route.evidenceQualifiedAlternatives, true);
});

test("primary request verification uses exact reasoning variants and matching hashes", () => {
  const state = step(waitingState(), route());
  const good = step(state, { type: "first_primary_request", actual_model: original, context_hash: digest });
  assert.equal(good.modelCheck, "verified");
  assert.equal(good.contextCheck, "verified");
  assert.equal(good.contextScope, "primary");
  const bad = step(state, { type: "first_primary_request", actual_model: original.replace("#high", "#low"),
    context_hash: "b".repeat(64) });
  assert.equal(bad.modelCheck, "mismatch");
  assert.equal(bad.contextCheck, "mismatch");
  const wire = step(good, { type: "primary_http_request", context_verified: false, actual_model: original });
  assert.equal(wire.contextCheck, "mismatch");
  assert.equal(wire.contextScope, "http");
});

test("the initial context manifest exposes validated file ranges and excludes source text", () => {
  const state = step(waitingState(), route({ context_id: "C0", decision_source: "compact_context", context: {
    strategy: "lexical", budget_tokens: 500, files: [
      { path: "src/inventory.ts", start_line: 19, end_line: 34, text: "hidden source excerpt" },
      { path: "../outside.ts", start_line: 1, end_line: 4 },
      { path: "src/invalid.ts", start_line: 9, end_line: 1 },
    ],
  } }));
  assert.equal(state.route.tier, "C0");
  assert.deepEqual(state.route.excerpts, [{ path: "src/inventory.ts", start: 19, end: 34 }]);
  assert.ok(!JSON.stringify(state).includes("hidden source excerpt"));
});

test("compact context estimates use a local C2 reference and keep measured usage separate", () => {
  const row = { context_id: "C0", initial_context_tokens: 127, context_c2_reference_tokens: 814 };
  const state = step(waitingState(), route(row));
  assert.deepEqual(state.route.contextEstimate, { compactTokens: 127, c2Tokens: 814 });
  assert.equal(state.metrics, null);
  assert.deepEqual(step(waitingState(), route({ ...row, context_c2_reference_tokens: undefined })).route.contextEstimate,
    { compactTokens: 127, c2Tokens: null });
  assert.equal(step(waitingState(), route({ ...row, context_id: "C2" })).route.contextEstimate, null);
  assert.equal(formatContextReduction(127, 814, "en-US"), "687 fewer (84.4%)");
  assert.equal(formatContextReduction(120, 100, "en-US"), "20 more (20%)");
  assert.equal(formatContextReduction(120, 120, "en-US"), "Same estimate");
});

test("isolates interleaved sessions, tasks, and repositories", () => {
  const state = step(waitingState(), route());
  for (const event of [
    { type: "first_primary_request", session_id: "another", context_hash: digest },
    { type: "first_primary_request", task_id: "older", context_hash: digest },
    route({ directory: join(checkout, "another") }),
  ]) assert.equal(step(state, event), state);
  const next = step(state, { type: "routing_stage", stage: "analyzing", task_id: "another" });
  assert.equal(next.route, null);
  assert.equal(next.stage, "analyzing");
});

test("missing files, malformed records, and partial writes preserve the last valid state", async () => {
  const temp = await mkdtemp(join(tmpdir(), "contextroute-desktop-test-"));
  try {
    const path = join(temp, "execution.jsonl");
    assert.equal((await readSessionState(path, session, checkout)).stage, "waiting");
    await writeFile(path, JSON.stringify(route()) + "\n{invalid}\n" +
      JSON.stringify({ type: "route", session_id: "another" }) + '\n{"type":"first_');
    assert.equal((await readSessionState(path, session, checkout)).route.tier, "C2");
    assert.equal(parseEvent("{"), null);
    assert.equal(parseEvent("123"), null);
  } finally {
    await rm(temp, { recursive: true, force: true });
  }
});

test("live snapshots update from routing through completion without redisplaying an older task", async () => {
  const store = desktopState(join(checkout, ".contextroute", "execution.jsonl"), checkout);
  assert.equal(store.record(route()), session);
  assert.equal((await store.snapshot(session)).contextCheck, "pending");
  store.record({ type: "first_primary_request", session_id: session, task_id: task, actual_model: original, context_hash: digest });
  assert.equal((await store.snapshot(session)).stage, "executing");
  store.record({ type: "execution", session_id: session, task_id: task, execution_status: "completed",
    input_tokens: 2100, output_tokens: 30, total_tokens: 2130, latency_seconds: 2, estimated_list_price_usd: 0 });
  const completed = await store.snapshot(session);
  assert.equal(completed.stage, "completed");
  assert.equal(completed.metrics.total, 2130);
  assert.equal(completed.metrics.cost, 0);
});

test("context token formatting preserves exact counts and missing telemetry", () => {
  assert.equal(formatContextTokens(2059, 3200, "en-US"), "2,059 / 3,200");
  assert.equal(formatContextTokens(null, 3200, "en-US"), "— / 3,200");
  assert.equal(formatContextTokens(0, null, "en-US"), "0");
  assert.equal(formatContextTokens(null, null, "en-US"), "—");
});

test("the panel calls a model switch verified only after an exact primary request match", () => {
  const originalState = step(waitingState(), route());
  assert.equal(modelDisposition(originalState), "retained");
  const pending = step(waitingState(), route({ selected_model: alternative, applied_model: alternative, demo: "loopback_fixture" }));
  assert.equal(modelDisposition(pending), "unverified");
  assert.equal(pending.route.demo, true);
  const verified = step(pending, { type: "first_primary_request", actual_model: alternative, context_hash: digest });
  assert.equal(modelDisposition(verified), "switched");
  const wrong = step(pending, { type: "first_primary_request", actual_model: original, context_hash: digest });
  assert.equal(modelDisposition(wrong), "mismatch");
  const confirmation = step(waitingState(), route({ selected_model: alternative, confirmation_required: true }));
  assert.equal(modelDisposition(confirmation), "confirmation");
});

test("completed executions with no primary-request observations report unavailable verification", () => {
  const state = step(waitingState(), route());
  const execution = step(state, { type: "execution", execution_status: "completed", actual_model: null,
    context_hash: null, primary_model_calls: 0, total_tokens: 11099 });
  assert.equal(execution.stage, "completed");
  assert.equal(execution.modelCheck, "unavailable");
  assert.equal(execution.contextCheck, "unavailable");
  assert.equal(execution.metrics.total, 11099);
  const late = step(execution, { type: "first_primary_request", actual_model: original, context_hash: digest });
  assert.equal(late.stage, "completed");
  assert.equal(late.modelCheck, "verified");
  assert.equal(late.contextCheck, "verified");
});

test("projection drops prompts, source excerpts, provider settings, and credential-shaped values", () => {
  const credential = "AIza" + "x".repeat(35);
  const state = step(waitingState(), route({
    prompt: "private original prompt", context: { text: "private source", files: [{ path: credential }] },
    model_metadata: { key: original, display_name: "token=" + credential, apiKey: credential },
    original_model_metadata: null, reasons: ["Injected reason " + credential], headers: { authorization: credential },
  }));
  const serialized = JSON.stringify(state);
  for (const privateValue of [credential, "private original prompt", "private source", "Injected reason", "authorization"]) {
    assert.ok(!serialized.includes(privateValue));
  }
  assert.equal(state.route.applied.name, null);
});

