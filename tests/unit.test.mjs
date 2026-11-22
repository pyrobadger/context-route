import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { buildContexts, hashContext, retrievalVersion } from "../dist/context/context-builder.js";
import { scanRepository } from "../dist/context/repository-scanner.js";
import { analyzeDependencies } from "../dist/context/dependency-analyzer.js";
import { analyzeTask } from "../dist/context/task-analysis.js";
import { estimateTokens } from "../dist/context/tokenizer.js";
import { estimateCallCost } from "../dist/router/cost-estimator.js";
import { isObservation, loadEvidence, pairEvidence, uniqueEvidence } from "../dist/router/evidence-store.js";
import { policyVersion, rankPairs, wilsonLowerBound } from "../dist/router/joint-optimizer.js";
import { capabilityExclusions, modelKey } from "../dist/router/model-registry.js";
import { serializeEvent } from "../dist/telemetry/execution-logger.js";
import { readConfig } from "../dist/plugin/config.js";
import { registerHooks } from "../dist/plugin/hooks.js";
import { TaskMetrics } from "../dist/telemetry/metrics.js";

const usage = { input: 100, output: 20, reasoning: 10, cache: { read: 200, write: 30 } };
const price = { input: 1, output: 2, cache_read: 0.5, cache_write: 1.5 };
const model = (key, overrides = {}) => ({
  key, provider_id: "test", model_id: key.split("/").at(-1), display_name: key,
  enabled: true, supports_tools: true, supports_text: true, context_window: 50000, pricing: [price],
  pricing_source: "test", pricing_read_at: null, ...overrides,
});
const pack = (context_id, actual_tokens) => ({
  context_id, actual_tokens, retrieval_version: retrievalVersion, budget_tokens: 6400,
  context_hash: hashContext(""), files: [], text: "", strategy: "lexical",
});
const row = (index, model_id = "test/a", context_id = "C1", overrides = {}) => ({
  execution_id: `${model_id}/${context_id}/${index}`,
  task_id: "task-" + index, repository_id: "fixture", repository_commit: "abcd",
  task_category: "bug", model_id, context_id, retrieval_version: retrievalVersion, context_hash: hashContext(index.toString()),
  resolved: true, verification_type: "task_tests", estimated_list_price_usd: 0.01,
  total_tokens: 1000, input_tokens: 700, output_tokens: 300, latency_seconds: 5, execution_status: "completed",
  ...overrides,
});
const batch = (model, context, overrides) => Array.from({ length: 6 }, (_, index) => row(index, model, context, overrides));
const optimize = (evidence, constraints = {}, overrides = {}) => rankPairs({
  models: [model("test/a"), model("test/b"), model("test/incompatible", { supports_tools: false })],
  packs: [pack("C1", 1200), pack("C2", 2800), pack("C3", 3900)],
  evidence, task: analyzeTask("Fix the allocation bug", 1, true), original: "test/a", constraints, ...overrides,
});

test("catalog constraints exclude unsupported or undersized models and preserve exact variant identity", () => {
  assert.equal(modelKey({ providerID: "p", id: "org/id", variant: "high" }), "p/org/id#high");
  assert.equal(modelKey({ providerID: "p", id: "id", variant: "default" }), "p/id");
  assert.equal(capabilityExclusions(model("test/a"), 1000, 2048).length, 0);
  assert.equal(capabilityExclusions(model("test/a", { enabled: false, supports_tools: false, input_limit: 900 }), 1000, 2048).length, 3);
  assert.equal(capabilityExclusions(model("test/a", { context_window: 0, output_limit: 100 }), 1000, 2048).length, 3);
});

test("cost includes reasoning and cache rates, chooses context brackets, and preserves unknown pricing", () => {
  assert.equal(estimateCallCost([price], usage), 305 / 1e6);
  assert.equal(estimateCallCost([price, { ...price, context_over: 300, input: 3 }], usage), 505 / 1e6);
  assert.equal(estimateCallCost([], usage), null);
});

test("evidence validates rows, deduplicates executions and tasks, and excludes invalid verification", () => {
  const observation = row(1);
  assert.ok(isObservation(observation));
  assert.equal(isObservation({ ...observation, total_tokens: Infinity }), false);
  const duplicateTask = { ...observation, execution_id: "zz", resolved: false };
  const executionOnly = row(2, "test/a", "C1", { verification_type: "execution_only" });
  const invalid = row(3, "test/a", "C1", { execution_status: "timeout" });
  const prepared = uniqueEvidence([{}, observation, observation, duplicateTask, executionOnly, invalid]);
  assert.equal(prepared.length, 4);
  assert.deepEqual(pairEvidence(prepared, "test/a", "C1", retrievalVersion), [observation]);
  assert.deepEqual(pairEvidence(prepared, "test/a#alternate", "C1", retrievalVersion), []);
});

test("persistent evidence reads JSON, JSONL, and result records and supports a missing store", async () => {
  const root = await mkdtemp(join(tmpdir(), "contextroute-evidence-"));
  try {
    assert.deepEqual(await loadEvidence(join(root, "absent")), []);
    await writeFile(join(root, "a.json"), JSON.stringify([row(1)]));
    await writeFile(join(root, "b.json"), JSON.stringify(row(1)));
    await writeFile(join(root, "observations.jsonl"), [row(1), row(2)].map(value => JSON.stringify(value)).join("\n"));
    assert.equal((await loadEvidence(root)).length, 1);
    assert.equal((await loadEvidence(join(root, "observations.jsonl"))).length, 2);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Wilson lower bound distinguishes small samples and missing evidence", () => {
  assert.equal(wilsonLowerBound(0, 0), null);
  assert.equal(wilsonLowerBound(0, 10), 0);
  assert.ok(Math.abs(wilsonLowerBound(4, 4) - 0.5101) < 0.0002);
  assert.ok(wilsonLowerBound(60, 60) > wilsonLowerBound(6, 6));
});

test("joint routing prefers lowest whole-task cost after constraints and sample support", () => {
  const evidence = [
    ...batch("test/a", "C1", { estimated_list_price_usd: 0.03 }),
    ...batch("test/a", "C2", { resolved: false, estimated_list_price_usd: 0.005 }),
    ...batch("test/b", "C1", { resolved: false, estimated_list_price_usd: 0.005 }),
    ...batch("test/b", "C2", { estimated_list_price_usd: 0.02 }),
    ...batch("test/b", "C3", { estimated_list_price_usd: 0.01, latency_seconds: 21 }),
    ...batch("test/incompatible", "C2", { estimated_list_price_usd: 0.001 }),
  ];
  const result = optimize(evidence, { max_latency_seconds: 10 });
  assert.equal(result.decision_type, "empirical");
  assert.equal(result.selected.model, "test/b");
  assert.equal(result.selected.context, "C2");
  assert.equal(result.selected.evidence_scope, "category");
  assert.equal(optimize([...evidence].reverse(), { max_latency_seconds: 10 }, { fallbackContext: "C3" }).selected.context, "C2");
  assert.ok(result.alternatives.some(candidate => candidate.context === "C3" && candidate.model === "test/b" && !candidate.constraints_met));
});

test("cold-start fallback retains the original model and uses C2 by default", () => {
  const result = optimize([]);
  assert.equal(result.policy_version, policyVersion);
  assert.equal(result.decision_type, "cold_start_fallback");
  assert.equal(result.fallback_reason, "no_usable_evidence");
  assert.equal(result.selected.model, "test/a");
  assert.equal(result.selected.context, "C2");
  assert.equal(result.selected.success_lower_bound, null);
  const sparse = optimize(batch("test/b", "C2", { estimated_list_price_usd: null }));
  assert.equal(sparse.decision_type, "cold_start_fallback");
  assert.equal(sparse.fallback_reason, "insufficient_evidence");
  assert.equal(sparse.selected.model, "test/a");
});

test("fallback tier is configurable and falls back to the smallest compatible nonempty pack", () => {
  const { fallbackContext } = readConfig({ fallback_context: "C3" }, tmpdir());
  assert.equal(optimize([], {}, { fallbackContext }).selected.context, "C3");
  assert.throws(() => readConfig({ fallback_context: "typo" }, tmpdir()), /fallback_context/);
  assert.equal(optimize([], { max_context_tokens: 1500 }).selected.context, "C1");
  const missing = optimize([], {}, { packs: [pack("C2", 0), pack("C3", 3800)] });
  assert.equal(missing.selected.context, "C3");
  assert.match(missing.reasons[1], /C2 was unavailable/);
});

test("cold start abstains when preserving the original model would violate limits", () => {
  const unavailable = optimize([], {}, { models: [model("test/a", { supports_tools: false }), model("test/b")] });
  assert.equal(unavailable.decision_type, "no_feasible_pair");
  assert.equal(unavailable.selected, null);
  assert.equal(optimize([], { max_cost_usd: 0.25 }).decision_type, "no_feasible_pair");
});

test("Git scanner respects tracking, path boundaries, and credential exclusions", async () => {
  const root = await mkdtemp(join(tmpdir(), "contextroute-scan-"));
  try {
    const git = (...args) => execFileSync("git", args, { cwd: root, stdio: "pipe" });
    git("init", "-q");
    await mkdir(join(root, "src"));
    await writeFile(join(root, ".gitignore"), "generated/\nignored.py\n");
    await writeFile(join(root, "ignored.py"), "ignored content");
    assert.equal((await scanRepository(root)).files.length, 0);
    await writeFile(join(root, ".env"), "example=private");
    await writeFile(join(root, "private-key.py"), "export key");
    await writeFile(join(root, "src", "settings.py"), 'api_key = "example-private-value"\n');
    await writeFile(join(root, "src", "stock.py"), "class Stock:\n    pass\n");
    await writeFile(join(root, "src", "wrong.py"), Buffer.from([0, 1, 2]));
    try { await symlink(join(root, "src", "stock.py"), join(root, "link.py"), "file"); } catch {}
    git("add", "--all");
    await writeFile(join(root, "src", "untracked.py"), "not in the index");
    const scanned = await scanRepository(join(root, "src"));
    assert.deepEqual(scanned.files.map(file => file.path), ["src/stock.py"]);
    assert.deepEqual(scanned.languages, ["Python"]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("Python and TypeScript imports connect implementation, callers, and tests", () => {
  const files = [
    { path: "app/main.py", language: "Python", lines: ["from .stock import lookup"], isTest: false },
    { path: "app/stock.py", language: "Python", lines: ["def lookup(): pass"], isTest: false },
    { path: "tests/test_stock.py", language: "Python", lines: ["from app.stock import lookup"], isTest: true },
    { path: "src/agent.ts", language: "TypeScript", lines: ["import { calculate } from './price.js'"], isTest: false },
    { path: "src/price.ts", language: "TypeScript", lines: ["export const calculate = () => 1;"], isTest: false },
  ];
  const graph = analyzeDependencies(files);
  assert.deepEqual(graph.imports.get("app/main.py"), ["app/stock.py"]);
  assert.deepEqual(graph.nearbyTests.get("app/stock.py"), ["tests/test_stock.py"]);
  assert.deepEqual(graph.callers.get("src/price.ts"), ["src/agent.ts"]);
});

test("context tiers are reproducible, content-addressed, budgeted, and dependency-aware", () => {
  const source = [
    { path: "shop/stock.py", language: "Python", lines: ["from .capacity import cap", ...Array(90).fill("available_stock = 1")], isTest: false },
    { path: "shop/capacity.py", language: "Python", lines: ["def cap(): return 25"], isTest: false },
    { path: "shop/orders.py", language: "Python", lines: ["from .stock import available_stock", "def checkout(): pass"], isTest: false },
    { path: "tests/test_stock.py", language: "Python", lines: ["from shop.stock import available_stock", "def test_available(): pass"], isTest: true },
    { path: "shop/unrelated.py", language: "Python", lines: ["def lullaby(): pass"], isTest: false },
    { path: "shop/wide.py", language: "Python", lines: ["available_stock = " + "`!".repeat(12_000)], isTest: false },
  ];
  const repository = { root: "/repo", commit: null, files: source, languages: ["Python"], truncated: false };
  const first = buildContexts(repository, "Fix available_stock in shop/stock.py");
  assert.deepEqual(buildContexts(repository, "Fix available_stock in shop/stock.py"), first);
  assert.equal(first.task.nearbyTests, true);
  assert.equal(first.packs[0].context_hash, first.packs.find(item => item.context_id === "L1").context_hash);
  for (const pack of first.packs) {
    assert.equal(pack.actual_tokens, estimateTokens(pack.text));
    assert.ok(pack.actual_tokens <= pack.budget_tokens);
    assert.equal(hashContext(pack.text), pack.context_hash);
    assert.ok(!pack.files.some(file => file.path === "shop/unrelated.py"));
    for (const file of pack.files) assert.equal(
      file.text, source.find(source => source.path === file.path).lines.slice(file.start_line - 1, file.end_line).join("\n"));
  }
  const c2 = first.packs.find(item => item.context_id === "C2");
  assert.ok(c2.files.some(file => file.path === "shop/capacity.py" && file.reason === "dependency"));
  assert.ok(estimateTokens("你好🙂") > 0);

  const compactAnalysis = buildContexts(repository, "Fix available_stock in shop/stock.py", ["C0"]);
  const [compact] = compactAnalysis.packs;
  assert.equal(compact.context_id, "C0");
  assert.ok(compact.actual_tokens > 0 && compact.actual_tokens <= 500);
  assert.deepEqual(compact.files.map(file => file.path), ["shop/stock.py"]);
  assert.equal(compactAnalysis.packs.length, 1);
  assert.equal(compactAnalysis.c2ReferenceTokens, c2.actual_tokens);
  const ambiguous = { ...repository, files: [...source, { ...source[0], path: "other/stock.py" }] };
  assert.equal(buildContexts(ambiguous, "Fix stock.py", ["C0"]).packs[0].text, "");
  assert.equal(buildContexts(repository, "Fix inventory availability", ["C0"]).packs[0].text, "");
  assert.equal(buildContexts(repository, "Fix shop/stock.py and missing.py", ["C0"]).packs[0].text, "");
});

test("telemetry serializes null measurements and deduplicates completed step and tool events", () => {
  const telemetry = new TaskMetrics();
  const empty = JSON.parse(serializeEvent({ type: "complete", ...telemetry.summary(), billed: null }));
  assert.equal(empty.total_tokens, null);
  assert.equal(empty.billed, null);
  telemetry.step("id-1", usage, "stop");
  telemetry.step("id-1", usage, "stop");
  telemetry.tool("tool-1");
  telemetry.tool("tool-1");
  const totals = telemetry.summary();
  assert.equal(totals.total_tokens, 360);
  assert.equal(totals.input_tokens, 330);
  assert.equal(totals.output_tokens, 30);
  assert.equal(totals.tool_calls, 1);
});


test("a primary context request can identify the exact prompt before the inbox event arrives", async () => {
  const directory = await mkdtemp(join(tmpdir(), "contextroute-request-order-"));
  const events = join(directory, "execution.jsonl");
  const sessionID = "sess-fixture";
  const messageID = "msg-current";
  const previousID = "msg-previous";
  const model = {
    providerID: "fixture", id: "coding", name: "Fixture model", enabled: true,
    capabilities: { tools: true, input: ["text"], output: ["text"] },
    limit: { context: 32000, output: 4096 }, cost: [], variants: [],
  };
  const hook = new Map();
  const previousDelivery = Promise.withResolvers();
  const previousActive = Promise.withResolvers();
  const previousComplete = Promise.withResolvers();
  const delivery = Promise.withResolvers();
  const complete = Promise.withResolvers();
  const drained = Promise.withResolvers();
  const stop = await registerHooks({
    options: { mode: "auto", telemetry_path: events },
    location: { directory },
    rpc: { register: async () => ({ events: { emit: async () => {} }, dispose: async () => {} }) },
    event: {
      async *subscribe() {
        yield await previousDelivery.promise;
        previousActive.resolve();
        yield await previousComplete.promise;
        yield await delivery.promise;
        yield await complete.promise;
        drained.resolve();
      },
    },
    session: {
      hook: async (name, fn) => { hook.set(name, fn); },
      get: async () => ({ location: { directory }, model }),
    },
    model: { list: async () => ({ data: [model] }) },
  });
  try {
    await writeFile(join(directory, "inventory.py"), "def inventory():\n    return 1\n");
    await hook.get("prompt")({ sessionID, messageID: previousID, prompt: { text: "Explain inventory.py" } });
    previousDelivery.resolve({ type: "session.inbox.delivered", data: { sessionID, inboxID: previousID } });
    await previousActive.promise;
    await hook.get("prompt")({ sessionID, messageID, prompt: { text: "Fix inventory.py" } });
    const unrelated = { sessionID, model, messages: [{ role: "user", id: "msg-unrelated" }], system: [] };
    await hook.get("context")(unrelated);
    assert.deepEqual(unrelated.system, []);

    const context = { sessionID, model, messages: [{ role: "user", id: messageID }], system: [] };
    await hook.get("context")(context);
    assert.equal(context.system.length, 1);
    assert.match(context.system[0].text, /inventory\.py/);
    await hook.get("model.request")({ sessionID, model, kind: "primary" });

    previousComplete.resolve({ type: "session.execution.succeeded", data: { sessionID } });
    delivery.resolve({ type: "session.inbox.delivered", data: { sessionID, inboxID: messageID } });
    complete.resolve({ type: "session.execution.succeeded", data: { sessionID } });
    await drained.promise;

    const trace = (await readFile(events, "utf8")).trim().split("\n").map(JSON.parse);
    const observed = trace.filter(event => event.type === "first_primary_request");
    assert.equal(observed.length, 1);
    assert.equal(observed[0].task_id, messageID);
    assert.equal(observed[0].actual_model, "fixture/coding");
    assert.equal(observed[0].context_hash, hashContext(context.system[0].text));
    const executions = trace.filter(event => event.type === "execution");
    assert.deepEqual(executions.map(event => [event.task_id, event.execution_status, event.primary_model_calls]), [
      [previousID, "completed", 0], [messageID, "completed", 1],
    ]);
  } finally {
    await stop();
    await rm(directory, { recursive: true, force: true });
  }
});

