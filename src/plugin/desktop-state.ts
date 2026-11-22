import { createReadStream } from "node:fs";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { publicLimitations, type PublicBenchmarkEvidence } from "../router/public-routing.js";

type Row = Record<string, unknown>;
type Check = "pending" | "verified" | "mismatch" | "unavailable" | "not_applied";
type Stage = "waiting" | "analyzing" | "routing" | "executing" | "completed" | "interrupted" | "error";
type Model = { id: string; name: string | null };
type ContextExcerpt = { path: string; start: number; end: number };
type Candidate = {
  model: string; context: string; evidence: number | null; success: number | null; wilson: number | null;
  tokens: number | null; cost: number | null; seconds: number | null;
};
type Route = {
  demo: boolean;
  source: string; fallbackReason: string | null; policy: string | null; confirmation: boolean;
  original: Model | null; recommended: Model | null; applied: Model | null;
  tier: string | null; strategy: string | null; initialTokens: number | null; budget: number | null;
  contextEstimate: { compactTokens: number; c2Tokens: number | null } | null;
  files: number | null; excerpts: ContextExcerpt[]; hash: string | null; reasons: string[]; evidence: Candidate | null; alternatives: Candidate[];
  evidenceQualifiedAlternatives: boolean;
  publicBenchmark: PublicBenchmarkEvidence | null;
  publicCoverage: { available: number; matched: number; lastUpdate: string | null } | null;
};
export type RoutingState = {
  task: string | null; stage: Stage; time: string | null; route: Route | null;
  actual: string | null; contextCheck: Check; modelCheck: Check; contextScope: "primary" | "http" | null;
  error: string | null;
  metrics: { input: number | null; output: number | null; total: number | null; seconds: number | null; cost: number | null } | null;
};

const transitions = new Set(["routing_stage", "route", "first_primary_request", "primary_http_request", "execution", "integration_error"]);
const modelID = /^[\w.+:@%-]+\/[\w./+:%@-]+(?:#[\w.+:@%-]+)?$/;
const secret = /AIza[\w-]{20,}|(?:sk|xai|gh[pousr]|glpat|xox[baprs])[-_][\w-]{12,}|AKIA[A-Z0-9]{16}|ya29\.[\w-]{12,}|(?:key|token|password|secret)\s*[:=]\s*\S+|bearer\s+\S+|eyJ[\w-]{12,}\.[\w-]{12,}/i;
const reasons = [
  /^Wilson lower bound \d+\.\d+ meets \d+\.\d+ from \d+ verified tasks \((?:category|pooled) evidence\)\.$/,
  /^Lowest mean whole-task list price among qualifying pairs: \$\d+\.\d+\.$/,
  /^The selected repository pack uses approximately \d+ tokens\.$/,
  /^The selected model won a comparable, paired SWE-bench Verified evaluation\.$/,
  /^The selected model led the eligible alternatives on paired SWE-bench Verified results\. The current model has no matching score\.$/,
  /^Public benchmark check found no score for the current model and (?:[01] of the two comparable, price-eligible alternatives needed|no clear winner among the eligible alternatives)\.$/,
  /^Public benchmark check found no comparable alternative with a clear advantage over the current model\.$/,
  /^(?:Included compact excerpts from files named in the prompt|No compatible compact pack covered the files named in the prompt)\.$/,
  /^[C][123] covers \d+ named paths, \d+ lexical matches and \d+ dependency, caller or test files within its context budget\.$/,
  /^Retained the original model and used the provisional default tier (?:C0|[CL][123])\.$/,
  /^Tier (?:C0|[CL][123]) was unavailable or incompatible for the original model\. Used its smallest compatible nonempty pack\.$/,
  /^(?:No allowed pair met the minimum sample, success, and whole-task cost criteria|No usable verified observations exist for these exact pairs|No empirical pair qualified and the original model has no compatible nonempty pack\. Kept the original session configuration|Repository indexing reached its resource cap|Benchmark supplied this exact model and context tier)\.$/,
];

const object = (value: unknown): Row => value && typeof value === "object" && !Array.isArray(value) ? value as Row : {};
const text = (value: unknown): string | null =>
  typeof value === "string" && value.length <= 160 && !secret.test(value) && !/[\x00-\x1f\x7f]/.test(value) ? value : null;
const identifier = (value: unknown) => typeof value === "string" && /^[\w-]{1,160}$/.test(value) ? text(value) : null;
const model = (value: unknown) => typeof value === "string" && modelID.test(value) ? text(value) : null;
const number = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const hash = (value: unknown) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value) ? value : null;
const tier = (value: unknown) => typeof value === "string" && /^(?:C0|[CL][123]|default)$/.test(value) ? value : null;
const time = (value: unknown) => typeof value === "string" && /^\d{4}-\d{2}-\d{2}T[\d:.]+Z$/.test(value) ? value : null;
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];

export function sameDirectory(a: string, b: string) {
  const path = (value: string) => process.platform === "win32" ? resolve(value).toLowerCase() : resolve(value);
  return path(a) === path(b);
}

export function waitingState(task: string | null = null): RoutingState {
  return { task, stage: "waiting", time: null, route: null, actual: null, contextCheck: "pending", modelCheck: "pending",
    contextScope: null, error: null, metrics: null };
}

function candidate(value: unknown): Candidate | null {
  const row = object(value);
  const id = model(row.model);
  const context = tier(row.context);
  return id && context ? {
    model: id, context, evidence: number(row.evidence_count), success: number(row.observed_success),
    wilson: number(row.success_lower_bound), tokens: number(row.average_tokens),
    cost: number(row.average_cost_usd), seconds: number(row.average_latency_seconds),
  } : null;
}

function publicScore(value: unknown) {
  const row = object(value);
  const key = model(row.key);
  const modelVersion = text(row.modelVersion);
  const resolved = number(row.resolved);
  const tasks = number(row.tasks);
  const sourceUrl = typeof row.sourceUrl === "string" && /^https:\/\/github\.com\/swe-bench\/experiments\/tree\/[a-f0-9]{40}\/evaluation\/verified\/[\w.-]+$/.test(row.sourceUrl)
    ? row.sourceUrl : null;
  return key && modelVersion && sourceUrl && tasks && resolved !== null && resolved <= tasks
    ? { key, modelVersion, resolved, tasks, sourceUrl } : null;
}

function benchmarkEvent(row: Row, expectedOriginal: string | null, expectedSelected: string | null): PublicBenchmarkEvidence | null {
  if (row.decision_source !== "public_benchmark_informed") return null;
  const evidence = object(row.public_benchmark);
  const original = publicScore(evidence.original);
  const reference = publicScore(evidence.reference);
  const selected = publicScore(evidence.selected);
  const improvements = number(evidence.improvements);
  const regressions = number(evidence.regressions);
  const harness = text(evidence.harness);
  const date = text(evidence.evaluationDate);
  const contextReason = text(evidence.contextReason);
  if (evidence.benchmark !== "SWE-bench Verified" || !selected || selected.key !== expectedSelected
    || !expectedOriginal || !harness || !date || !/^\d{4}-\d{2}-\d{2}$/.test(date)
    || !contextReason || !reasons.some(pattern => pattern.test(contextReason)) || improvements === null || regressions === null) return null;
  if (original ? original.key !== expectedOriginal
    : evidence.original !== null || row.policy_version !== "public-benchmark-pool-v1" || !reference
      || reference.key === expectedOriginal || reference.key === expectedSelected) return null;
  return { benchmark: evidence.benchmark, harness, evaluationDate: date, original,
    ...(!original && reference ? { reference } : {}), selected, improvements, regressions,
    contextReason, limitations: publicLimitations };
}

function benchmarkCoverage(value: unknown) {
  const row = object(value);
  const available = number(row.available);
  const matched = number(row.matched);
  return available !== null && matched !== null && matched <= available ? { available, matched, lastUpdate: time(row.lastUpdate) } : null;
}

function routeEvent(row: Row): Route {
  const context = object(row.context);
  const meta = object(row.model_metadata);
  const original = object(row.original_model_metadata);
  const toModel = (value: unknown) => {
    const id = model(value);
    return id ? { id, name: id === original.key ? text(original.display_name) : id === meta.key ? text(meta.display_name) : null } : null;
  };
  const options = list(row.alternatives).map(object).filter(value => value.constraints_met === true)
    .map(candidate).filter(value => value !== null);
  const recommended = toModel(row.selected_model);
  const applied = toModel(row.applied_model ?? row.selected_model);
  const selected = options.find(option => option.model === recommended?.id && option.context === row.context_id);
  const limits = object(row.routing_evidence_limits);
  const minimum = number(limits.min_samples);
  const threshold = number(limits.min_success);
  const eligible = minimum === null || threshold === null ? options : options.filter(option =>
    option.evidence !== null && option.evidence >= minimum && option.wilson !== null && option.wilson >= threshold && option.cost !== null);
  const initialTokens = number(row.initial_context_tokens);
  const c2Tokens = number(row.context_c2_reference_tokens);
  const excerpts = list(context.files).flatMap(value => {
    const excerpt = object(value);
    const path = text(excerpt.path);
    const start = number(excerpt.start_line);
    const end = number(excerpt.end_line);
    if (!path || /^(?:\/|[a-z]:)|\\/i.test(path) || path.split("/").some(part => part === ".." || !part)
      || start === null || end === null || !Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 1 || end < start) return [];
    return [{ path, start, end }];
  });
  return {
    demo: row.demo === "loopback_fixture",
    source: identifier(row.decision_source) ?? "unknown", fallbackReason: identifier(row.fallback_reason),
    policy: identifier(row.policy_version), confirmation: row.confirmation_required === true,
    original: toModel(row.original_model), recommended, applied,
    tier: tier(row.context_id), strategy: context.strategy === "lexical" || context.strategy === "dependencies" ? context.strategy : null,
    initialTokens, budget: number(context.budget_tokens),
    contextEstimate: row.context_id === "C0" && initialTokens && Number.isSafeInteger(initialTokens) ? {
      compactTokens: initialTokens, c2Tokens: Number.isSafeInteger(c2Tokens) ? c2Tokens : null,
    } : null,
    files: Array.isArray(context.files) ? new Set(context.files.map(file => object(file).path).filter(path => typeof path === "string")).size : null,
    excerpts,
    hash: hash(row.context_hash), reasons: list(row.reasons).filter((value): value is string =>
      typeof value === "string" && reasons.some(pattern => pattern.test(value))),
    evidence: selected ?? (recommended && tier(row.context_id) ? {
      model: recommended.id, context: tier(row.context_id)!, evidence: number(row.evidence_count),
      success: null, wilson: null, tokens: null, cost: null, seconds: null,
    } : null),
    alternatives: eligible.filter(option => option !== selected).slice(0, 16),
    evidenceQualifiedAlternatives: minimum !== null && threshold !== null,
    publicBenchmark: benchmarkEvent(row, model(row.original_model), recommended?.id ?? null),
    publicCoverage: benchmarkCoverage(row.public_benchmark_coverage),
  };
}

function request(state: RoutingState, row: Row, scope: "primary" | "http"): RoutingState {
  const actual = model(row.actual_model) ?? state.actual;
  const expected = state.route?.applied?.id;
  const modelCheck = actual && expected ? actual === expected ? "verified" : "mismatch" : state.modelCheck;
  const expectedHash = state.route?.hash;
  const observed = hash(row.context_hash);
  let contextCheck = state.contextCheck;
  let contextScope = state.contextScope;
  if (state.route && !expectedHash && contextCheck === "pending") contextCheck = "unavailable";
  if (expectedHash && (scope === "http" || state.contextScope !== "http")) {
    if (scope === "http" && row.context_verified === null) {
      if (contextCheck === "pending") contextCheck = "unavailable";
    } else {
      contextCheck = observed === expectedHash && (scope === "primary" || row.context_verified === true) ? "verified" : "mismatch";
      contextScope = scope;
    }
  }
  return { ...state, actual, modelCheck, contextCheck, contextScope,
    stage: state.stage === "error" || state.stage === "completed" || state.stage === "interrupted" ? state.stage : "executing" };
}

export function reduceRouting(previous: RoutingState, value: unknown, sessionID: string, directory: string): RoutingState {
  const row = object(value);
  if (row.session_id !== sessionID || typeof row.type !== "string" || !transitions.has(row.type)
    || (typeof row.directory === "string" && !sameDirectory(row.directory, directory))) return previous;
  const task = identifier(row.task_id);
  const stamp = time(row.time);
  if (row.type === "routing_stage") {
    if (!task || (row.stage !== "analyzing" && row.stage !== "routing")) return previous;
    return { ...(task === previous.task ? previous : waitingState(task)), stage: row.stage, time: stamp };
  }
  if (row.type === "route") {
    const route = routeEvent(row);
    return { ...waitingState(task), stage: "routing", route, time: stamp,
      contextCheck: route.tier === "default" || route.initialTokens === 0 ? "not_applied" : "pending" };
  }
  if (task && previous.task !== task) return previous;
  if (row.type === "integration_error") {
    return { ...previous, stage: "error", error: identifier(row.stage) ?? "integration-error", time: stamp ?? previous.time };
  }
  if (!previous.route) return previous;
  if (row.type === "first_primary_request") return request(previous, row, "primary");
  if (row.type === "primary_http_request") return request(previous, row, "http");
  if (row.type !== "execution") return previous;
  const stage = row.execution_status === "completed" ? "completed" : row.execution_status === "interrupted" ? "interrupted" : "error";
  return {
    ...previous, stage: previous.stage === "error" ? "error" : stage,
    modelCheck: previous.modelCheck === "pending" ? "unavailable" : previous.modelCheck,
    contextCheck: previous.contextCheck === "pending" ? "unavailable" : previous.contextCheck,
    metrics: { input: number(row.input_tokens), output: number(row.output_tokens), total: number(row.total_tokens),
      seconds: number(row.latency_seconds), cost: number(row.estimated_list_price_usd) },
  };
}

export function parseEvent(line: string): Row | null {
  try {
    const row = object(JSON.parse(line));
    return typeof row.type === "string" ? row : null;
  } catch {
    return null;
  }
}

export async function readSessionState(path: string, sessionID: string, directory: string) {
  let current = waitingState();
  const stream = createReadStream(path, { encoding: "utf8" });
  const lines = createInterface({ input: stream, crlfDelay: Infinity });
  try {
    for await (const line of lines) current = reduceRouting(current, parseEvent(line), sessionID, directory);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  } finally {
    lines.close();
    stream.destroy();
  }
  return current;
}

export function desktopState(path: string, directory: string) {
  const sessions = new Map<string, RoutingState>();
  const pending = new Map<string, Promise<RoutingState>>();
  const save = (id: string, state: RoutingState) => {
    sessions.delete(id);
    sessions.set(id, state);
    if (sessions.size > 128) sessions.delete(sessions.keys().next().value!);
    return state;
  };
  return {
    record(event: Row) {
      const id = identifier(event.session_id);
      if (!id || typeof event.type !== "string" || !transitions.has(event.type)) return null;
      const before = sessions.get(id) ?? waitingState();
      const state = reduceRouting(before, event, id, directory);
      if (state === before) return null;
      save(id, state);
      return id;
    },
    async snapshot(id: string) {
      const current = sessions.get(id);
      if (current) return save(id, current);
      const existing = pending.get(id);
      if (existing) return existing;
      const read = readSessionState(path, id, directory)
        .then(state => sessions.get(id) ?? save(id, state))
        .finally(() => pending.delete(id));
      pending.set(id, read);
      return read;
    },
  };
}

