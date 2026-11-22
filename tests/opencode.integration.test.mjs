import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import test from "node:test";
import { retrievalVersion } from "../dist/context/context-builder.js";

import { policyVersion } from "../dist/router/joint-optimizer.js";
import { activeRecord, atomicCacheWrite, reviewedRuns } from "../dist/router/public-cache.js";
import { reduceRouting, waitingState } from "../dist/plugin/desktop-state.js";
import { fixtureRecord } from "./fixtures/public-benchmark.mjs";
import { demoOriginal, demoPrompt, demoSelected, startDesktopDemo } from "../examples/desktop-demo.mjs";

const root = new URL("../", import.meta.url).href;
const zero = { input: 0, output: 0, cache: { read: 0, write: 0 } };

function executable() {
  if (process.env.CONTEXTROUTE_OPENCODE) return process.env.CONTEXTROUTE_OPENCODE;
  if (process.platform !== "win32") return "opencode";
  for (const path of (process.env.PATH ?? "").split(delimiter)) {
    for (const file of ["opencode.exe", "node_modules/@opencode/cli/bin/opencode.exe"]) {
      const candidate = join(path, file);
      if (existsSync(candidate)) return candidate;
    }
  }
  throw new Error("Set CONTEXTROUTE_OPENCODE to the native OpenCode executable.");
}

function start(command, args, cwd, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", chunk => { stdout += chunk; });
    child.stderr.on("data", chunk => { stderr += chunk; });
    const timer = setTimeout(() => child.kill(), 45_000);
    child.on("error", error => { clearTimeout(timer); reject(error); });
    child.on("close", code => { clearTimeout(timer); resolve({ code, stdout, stderr }); });
  });
}

function hasText(value, exact) {
  if (typeof value === "string") return value.includes(exact);
  return value && typeof value === "object" && Object.values(value).some(child => hasText(child, exact));
}

async function runOpenCodeRoutingScenario(source, unscoredOriginal = false) {
  const empirical = source === "empirical";
  const publicTest = source === "public_benchmark_informed";
  const compact = source === "compact_context";
  const [originRun, candidateRun] = reviewedRuns;
  const original = publicTest && !unscoredOriginal ? originRun.key : "route-original/alpha";
  const candidate = publicTest ? candidateRun.key : "route-candidate/beta";
  const [originProvider, originID] = original.split("/");
  const [candidateProvider, candidateID] = candidate.split("/");
  const [referenceProvider, referenceID] = originRun.key.split("/");
  const directory = await mkdtemp(join(tmpdir(), "contextroute-integration-"));
  const workspace = join(directory, "checkout");
  const requests = [];
  const server = createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw || "{}");
    requests.push({ url: req.url, body });
    const frame = (choices, usage) => JSON.stringify({ id: "test-fixture", object: "chat.completion.chunk", created: 1, model: body.model, choices, usage });
    res.writeHead(200, { "Content-Type": "text/event-stream" });
    res.write("data: " + frame([{ index: 0, delta: { role: "assistant", content: "Local transport regression test." }, finish_reason: null }]) + "\n\n");
    res.write("data: " + frame([{ index: 0, delta: {}, finish_reason: "stop" }]) + "\n\n");
    res.end("data: " + frame([], { prompt_tokens: 119, completion_tokens: 4, total_tokens: 123 }) + "\n\ndata: [DONE]\n\n");
  });
  try {
    assert.match(execFileSync(executable(), ["--version"], { encoding: "utf8" }), /\bv?2\.(?:2\.7|3\.2)\b/);
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    await mkdir(workspace);
    execFileSync("git", ["init", "-q"], { cwd: workspace });
    await writeFile(join(workspace, "inventory.py"), "from stock import available\n\ndef inventory():\n    return available()\n");
    await writeFile(join(workspace, "stock.py"), "def available():\n    return 'transport-only-fixture'\n");
    if (unscoredOriginal || compact) await writeFile(join(workspace, "unrelated.py"), "COLOR = 'violet-sentinel'\n");
    execFileSync("git", ["add", "--all"], { cwd: workspace });
    const evidence = join(directory, "controlled-test-input.json");
    const records = [];
    for (let i = 0; i < 6; i++) {
      records.push({
        execution_id: "test-input-" + i, task_id: "sample-" + i, task_category: "bug", repository_id: "unit-input",
        repository_commit: "test-fixture", model_id: "route-candidate/beta", context_id: "C2",
        context_hash: createHash("sha256").update(String(i)).digest("hex"), retrieval_version: retrievalVersion,
        resolved: true, verification_type: "task_tests", input_tokens: 100, output_tokens: 20,
        total_tokens: 120, latency_seconds: 1, estimated_list_price_usd: 0, execution_status: "completed",
      });
    }
    await writeFile(evidence, JSON.stringify(empirical ? records : []));
    const cache = join(directory, "benchmark-cache");
    const publicRecords = publicTest ? [fixtureRecord(originRun, 180), fixtureRecord(candidateRun, 315)] : [];
    for (const record of publicRecords) await atomicCacheWrite(cache, record.run + ".json", record);
    const publicSwitch = publicTest && publicRecords.every(record => activeRecord(record));
    const switched = empirical || publicSwitch;
    const logFile = join(directory, "events.jsonl");
    const packFile = join(directory, "selected-pack.json");
    const provider = (name, id, endpoint) => ({
      name, env: ["CONTEXTROUTE_LOOPBACK_KEY"], package: "@opencode/ai/providers/openai-compatible",
      settings: { baseURL: `http://127.0.0.1:${server.address().port}/${endpoint}/v1` },
      models: { [id]: { cost: [zero], limit: { context: 25000, output: 4096 } } },
    });
    await writeFile(join(workspace, "opencode.json"), JSON.stringify({
      plugins: [{ package: root, options: { mode: compact ? "context-only" : publicTest ? "auto" : "joint", evidence, telemetry_path: logFile, pack_path: packFile,
        models: unscoredOriginal ? [original, candidate, originRun.key] : [original, candidate], contexts: ["C1", "C2", "C3"] } }],
      providers: { [originProvider]: provider("Original", originID, "origin"), [candidateProvider]: provider("Candidate", candidateID, "candidate"),
        ...(unscoredOriginal ? { [referenceProvider]: provider("Reference", referenceID, "reference") } : {}) },
    }));
    const result = await start(executable(), ["run", "--standalone", "--print-logs", "--log-level", "warn", "--archived", "--format", "json", "--model",
      original, "Fix the inventory bug in inventory.py"], workspace,
    { ...process.env, XDG_CONFIG_HOME: join(directory, "config"), XDG_DATA_HOME: join(directory, "data"),
      XDG_CACHE_HOME: join(directory, "cache"), XDG_STATE_HOME: join(directory, "state"),
      CONTEXTROUTE_LOOPBACK_KEY: "non-secret-loopback-test", CONTEXTROUTE_BENCHMARK_CACHE: cache });
    const contents = await readFile(logFile, "utf8").catch(() => "");
    assert.equal(result.code, 0, result.stderr + result.stdout + contents + JSON.stringify(requests.map(request => request.url)));
    assert.ok(contents, "The isolated OpenCode session did not write ContextRoute telemetry.");
    const events = contents.trim().split("\n").map(line => JSON.parse(line));
    assert.ok(events.some(event => event.type === "plugin_loaded"));
    assert.ok(!events.some(event => event.type === "integration_error"), JSON.stringify(events.filter(event => event.type === "integration_error")));
    const route = events.find(event => event.type === "route");
    const first = events.find(event => event.type === "first_primary_request");
    const wire = events.find(event => event.type === "primary_http_request");
    assert.equal(route.original_model, original);
    assert.equal(route.policy_version, publicSwitch
      ? unscoredOriginal ? "public-benchmark-pool-v1" : "public-benchmark-routing-v1" : compact ? "compact-context-v1" : policyVersion);
    assert.equal(route.decision_source, switched || compact ? source : "cold_start_fallback");
    assert.equal(route.fallback_reason, switched || compact ? undefined : "no_usable_evidence");
    assert.equal(route.evidence_count, empirical ? 6 : 0);
    assert.equal(route.repository.indexed_files, unscoredOriginal || compact ? 3 : 2);
    assert.equal(route.context_id, compact ? "C0" : publicSwitch ? "C1" : "C2");
    assert.equal(first.actual_model, switched ? candidate : original);
    if (!publicTest && !compact) assert.equal(wire.context_verified, true);
    const context = JSON.parse(await readFile(packFile, "utf8"));
    assert.equal(first.context_hash, createHash("sha256").update(context.text).digest("hex"));
    assert.ok(context.text.includes("inventory.py"));
    const primary = requests.filter(request => hasText(request.body, context.text));
    assert.equal(primary.length, 1);
    assert.match(primary[0].url, switched ? /^\/candidate\// : /^\/origin\//);
    assert.equal(primary[0].body.model, switched ? candidateID : originID);
    if (unscoredOriginal || compact) assert.ok(!hasText(primary[0].body, "violet-sentinel"));
    if (compact) {
      assert.ok(context.actual_tokens <= 500);
      assert.deepEqual(context.files.map(file => file.path), ["inventory.py"]);
      assert.ok(!hasText(primary[0].body, "transport-only-fixture"));
      assert.ok(route.context_c2_reference_tokens > context.actual_tokens);
      const panel = events.reduce((state, event) => reduceRouting(state, event, route.session_id, workspace), waitingState());
      assert.deepEqual(panel.route.contextEstimate, { compactTokens: context.actual_tokens, c2Tokens: route.context_c2_reference_tokens });
      assert.equal(panel.metrics.total, 123);
    }
    const completion = events.find(event => event.type === "execution");
    assert.equal(completion.execution_status, "completed");
    assert.equal(completion.total_tokens, 123);
    assert.equal(completion.primary_model_calls, 1);
    assert.ok(events.some(event => event.type === "usage" && event.total_tokens >= 123));
    if (publicSwitch) {
      const panel = events.reduce((state, event) => reduceRouting(state, event, route.session_id, workspace), waitingState());
      assert.equal(panel.route.publicBenchmark.selected.resolved, 315);
      if (unscoredOriginal) {
        assert.equal(panel.route.publicBenchmark.original, null);
        assert.equal(panel.route.publicBenchmark.reference.key, originRun.key);
      }
      assert.equal(panel.route.publicCoverage.matched, 2);
      assert.equal(panel.modelCheck, "verified");
      assert.equal(panel.contextCheck, "verified");
    }
  } finally {
    server.closeAllConnections();
    server.close();
    await rm(directory, { recursive: true, force: true });
  }
}

test("OpenCode switches across providers and sends the empirical context verbatim", () => runOpenCodeRoutingScenario("empirical"));
test("OpenCode retains the original provider and sends C2 with a versioned fallback trace", () => runOpenCodeRoutingScenario("cold_start_fallback"));
test("OpenCode auto routing acts on a controlled public cache while both fixture model IDs are eligible", () =>
  runOpenCodeRoutingScenario("public_benchmark_informed"));

test("OpenCode can route a focused request from an unscored default to a public benchmark winner", () =>
  runOpenCodeRoutingScenario("public_benchmark_informed", true));

test("OpenCode compact mode keeps the model and sends one excerpt from the named file", () =>
  runOpenCodeRoutingScenario("compact_context"));

test("the presentation demo keeps and switches models in separate local workspaces", async () => {
  const receipts = [];
  const demo = await startDesktopDemo(receipt => receipts.push(receipt));
  const env = { ...process.env, XDG_CONFIG_HOME: join(demo.directory, "config"), XDG_DATA_HOME: join(demo.directory, "data"),
    XDG_CACHE_HOME: join(demo.directory, "cache"), XDG_STATE_HOME: join(demo.directory, "state"),
    CONTEXTROUTE_BENCHMARK_CACHE: join(demo.directory, "empty-benchmark-cache") };
  try {
    const catalog = await start(executable(), ["models", "--standalone"], demo.fallback, env);
    assert.equal(catalog.code, 0);
    assert.deepEqual(catalog.stdout.trim().split(/\r?\n/).sort(), [demoOriginal, demoSelected].sort());
    const observed = [];
    for (const [workspace, model, tier, source] of [
      [demo.fallback, demoOriginal, "C2", "cold_start_fallback"],
      [demo.switched, demoSelected, "C1", "empirical"],
    ]) {
      const run = await start(executable(), ["run", "--standalone", "--archived", "--format", "json", "--model", demoOriginal, demoPrompt], workspace, env);
      assert.equal(run.code, 0, run.stdout + run.stderr);
      const records = (await readFile(join(workspace, ".contextroute", "execution.jsonl"), "utf8")).trim().split("\n").map(JSON.parse);
      const route = records.find(event => event.type === "route");
      const first = records.find(event => event.type === "first_primary_request");
      assert.equal(route.demo, "loopback_fixture");
      assert.equal(route.original_model, demoOriginal);
      assert.equal(route.context_id, tier);
      assert.equal(route.decision_source, source);
      assert.equal(first.actual_model, model);
      assert.equal(first.context_hash, route.context_hash);
      assert.match(first.context_hash, /^[a-f0-9]{64}$/);
      assert.equal(records.find(event => event.type === "execution")?.execution_status, "completed");
      observed.push({ model, contextHash: first.context_hash });
    }
    assert.deepEqual(receipts.filter(receipt => receipt.contextHash), observed);
    assert.notEqual(observed[0].contextHash, observed[1].contextHash);
  } finally {
    await demo.stop();
    await rm(demo.directory, { recursive: true, force: true });
  }
});
