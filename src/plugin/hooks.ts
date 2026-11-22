import { writeFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import type { Plugin } from "@opencode/plugin";
import { buildContexts, type ContextId } from "../context/context-builder.js";
import { scanRepository } from "../context/repository-scanner.js";
import { policyVersion, rankPairs } from "../router/joint-optimizer.js";
import { benchmarkCoverage, readPublicCache } from "../router/public-cache.js";
import { refineWithPublicBenchmark } from "../router/public-routing.js";
import type { RoutingInput } from "../router/types.js";
import { loadEvidence } from "../router/evidence-store.js";
import { discoverModels, modelKey } from "../router/model-registry.js";
import { createLogger } from "../telemetry/execution-logger.js";
import { TaskMetrics, usageMetrics } from "../telemetry/metrics.js";
import { readConfig } from "./config.js";
import { applyModel } from "./model-switcher.js";
import { routingRpc } from "./desktop-rpc.js";
import { desktopState, sameDirectory } from "./desktop-state.js";
import { SessionManager, type RoutedTask } from "./session-manager.js";

const guardedGemini = "google/gemini-3.5-flash-lite";

export async function registerHooks(ctx: Plugin.Context) {
  const config = readConfig(ctx.options, ctx.location.directory);
  const writeLog = createLogger(config.telemetry, config.telemetryEnabled, config.strict);
  const desktop = desktopState(config.telemetry, ctx.location.directory);
  const rpc = await ctx.rpc.register(routingRpc, {
    snapshot: async input => {
      const { sessionID } = input as { sessionID: string };
      const session = await ctx.session.get({ sessionID });
      if (!sameDirectory(session.location.directory, ctx.location.directory)) return { state: null };
      return { state: await desktop.snapshot(sessionID) };
    },
  }).catch(() => {
    console.warn("[ContextRoute] Desktop telemetry could not register.");
    return null;
  });
  const log = (event: Record<string, unknown>) => {
    writeLog(event);
    const sessionID = desktop.record({ ...event, time: new Date().toISOString() });
    if (sessionID && rpc) void rpc.events.emit("updated", { sessionID }).catch(() => {});
  };
  const sessions = new SessionManager();
  const controller = new AbortController();
  const errorRecord = (stage: string, error: unknown, sessionID?: string) => {
    log({ type: "integration_error", stage, session_id: sessionID, error_type: error instanceof Error ? error.name : "UnknownError" });
    if (config.strict) throw error;
    console.warn(`[ContextRoute] ${stage} failed. See the configured telemetry log.`);
  };

  const activate = (sessionID: string, messageID: string) => {
    const previous = sessions.get(sessionID);
    const next = sessions.deliver(sessionID, messageID);
    if (previous && next && !previous.finished) {
      previous.finished = true;
      log({ type: "execution", task_id: previous.taskId, session_id: sessionID, execution_status: "interrupted",
        latency_seconds: (Date.now() - previous.startedAt) / 1000, ...previous.metrics.summary() });
    }
    return next;
  };

  const consume = async () => {
    for await (const event of ctx.event.subscribe({
      signal: controller.signal,
      types: ["session.inbox.delivered", "session.inbox.cancelled", "session.deleted", "session.step.ended",
        "session.tool.called", "session.execution.succeeded", "session.execution.failed",
        "session.execution.interrupted", "session.usage.updated"],
    })) {
      const { sessionID } = event.data;
      if (event.type === "session.inbox.delivered") {
        activate(sessionID, event.data.inboxID);
        continue;
      }
      if (event.type === "session.inbox.cancelled") {
        sessions.cancel(event.data.inboxID);
        continue;
      }
      if (event.type === "session.deleted") {
        sessions.remove(sessionID);
        continue;
      }
      const task = sessions.get(sessionID);
      if (!task) continue;
      const identity = { task_id: task.taskId, session_id: sessionID };
      switch (event.type) {
        case "session.step.ended":
          if (!task.finished) task.metrics.step(event.data.assistantMessageID, event.data.tokens, event.data.finish,
            task.pricedModels.has(task.expected) ? event.data.cost : null);
          break;
        case "session.tool.called":
          if (!task.finished) task.metrics.tool(event.id);
          break;
        case "session.usage.updated":
          log({ type: "usage", ...identity, ...usageMetrics(event.data.tokens),
            opencode_cost_usd: event.data.cost, token_scope: "OpenCode cumulative session usage" });
          break;
        case "session.execution.succeeded":
        case "session.execution.failed":
        case "session.execution.interrupted": {
          if (task.finished) break;
          task.finished = true;
          const status = event.type === "session.execution.succeeded" ? "completed"
            : event.type === "session.execution.failed" ? "failed" : "interrupted";
          log({ type: "execution", ...identity, execution_status: status, actual_model: task.firstModel ?? null,
            context_hash: task.contextPrepared ? task.pack?.context_hash ?? null : null,
            latency_seconds: (Date.now() - task.startedAt) / 1000, ...task.metrics.summary(),
            actual_billed_cost_usd: null, verification_type: "execution_only",
            error_type: event.type === "session.execution.failed" ? event.data.error.type : null });
          break;
        }
      }
    }
  };
  void consume().catch(error => {
    if (controller.signal.aborted) return;
    log({ type: "integration_error", stage: "event-stream", error_type: error instanceof Error ? error.name : "UnknownError" });
    console.error("[ContextRoute] Event stream failed.");
  });

  await ctx.session.hook("prompt", async event => {
    if (!event.prompt.text?.trim()) return;
    const { sessionID, messageID } = event;
    if (sessions.admissions.has(sessionID)) {
      errorRecord("concurrent-prompt", new Error("Another prompt is being admitted for this session."), sessionID);
      return;
    }
    sessions.admissions.add(sessionID);
    const startedAt = Date.now();
    let originalRef: Awaited<ReturnType<typeof ctx.session.get>>["model"];
    let switched = false;
    try {
      const session = await ctx.session.get({ sessionID });
      if (session.parentID) return;
      const location = { directory: session.location.directory };
      const availability = await ctx.model.list({ location });
      const agent = !session.model && session.agent ? await ctx.agent.get({ agentID: session.agent }) : undefined;
      originalRef = session.model ?? agent?.data?.model;
      if (!originalRef) {
        const fallback = (await ctx.model.default({ location })).data;
        if (fallback) originalRef = { providerID: fallback.providerID, id: fallback.id };
      }
      if (!originalRef) throw new Error("OpenCode did not report an original model.");
      const original = modelKey(originalRef);
      const models = discoverModels(availability.data, originalRef);
      const task: RoutedTask = { taskId: config.taskId ?? messageID, messageId: messageID, sessionId: sessionID,
        startedAt, original, expected: original, contextPrepared: false, finished: false,
        pricedModels: new Set(models.filter(model => model.pricing_source).map(model => model.key)), metrics: new TaskMetrics() };
      const identity = { task_id: task.taskId, session_id: sessionID, original_model: original };
      const modelInfo = models.find(model => model.key === original);
      if (config.mode === "baseline") {
        log({ type: "route", ...identity, selected_model: original, decision_source: "baseline", policy_version: policyVersion, context_id: "default",
          initial_context_tokens: 0, pricing_source: modelInfo?.pricing_source ?? null, router_overhead_ms: 0 });
        sessions.add(task);
        return;
      }
      if (!config.strict) log({ type: "routing_stage", ...identity, stage: "analyzing" });
      const compact = config.mode === "context-only";
      const desired: (ContextId | undefined)[] = compact ? ["C0"] : config.mode === "forced" ? [config.context] : config.contexts;
      const repository = await scanRepository(location.directory);
      const analysis = buildContexts(repository, event.prompt.text, desired);
      const evidence = compact ? [] : await loadEvidence(config.evidence);
      if (!config.strict) log({ type: "routing_stage", ...identity, stage: "routing" });
      const candidateModels = models.filter(model => (compact ? model.key === original : !config.models || config.models.includes(model.key))
        && (config.geminiGate || model.key.split("#")[0] !== guardedGemini));
      const input: RoutingInput = { models: candidateModels, packs: analysis.packs.filter(pack => desired.includes(pack.context_id)),
        evidence, task: analysis.task, repository: analysis.repository, original, fallbackContext: compact ? "C0" : config.fallbackContext,
        constraints: config.constraints };
      const local = rankPairs(input);
      const cache = config.strict || compact ? null : await readPublicCache();
      const decision = cache ? refineWithPublicBenchmark(input, local, cache) : local;
      const selected = config.mode === "forced"
        ? decision.alternatives.find(candidate => candidate.model === config.model && candidate.context === config.context && candidate.constraints_met)
        : decision.selected;
      if (config.strict && (!selected || !analysis.packs.find(pack => pack.context_id === selected.context)?.actual_tokens)) {
        throw new Error("Benchmark configuration is unavailable or its repository pack is empty.");
      }
      const context = analysis.packs.find(pack => pack.context_id === selected?.context);
      const blocked = selected && selected.model !== original && (config.mode === "confirm" || sessions.busy(sessionID));
      const provisional = selected && selected.model !== original && config.mode === "auto"
        && decision.decision_type !== "empirical" && decision.decision_type !== "public_benchmark_informed";
      if (config.strict && blocked) throw new Error("A running task prevented the requested model switch.");
      if (selected && !blocked && !provisional) {
        if (selected.model !== original) {
          switched = true;
          await applyModel(ctx, sessionID, selected.model, availability.data);
        }
        task.expected = selected.model;
        task.pack = context;
        if (context && config.packPath) {
          await mkdir(dirname(config.packPath), { recursive: true });
          await writeFile(config.packPath, JSON.stringify(context, null, 2) + "\n", { encoding: "utf8", mode: 0o600 });
        }
      }
      const { text: _packText, files, ...packMeta } = task.pack ?? {} as Partial<NonNullable<RoutedTask["pack"]>>;
      const appliedModelInfo = models.find(model => model.key === task.expected);
      const coverage = cache && benchmarkCoverage(models.filter(model => model.enabled && !model.variant).map(model => model.key), cache);
      log({
        type: "route", ...identity, selected_model: selected?.model ?? null, applied_model: task.expected,
        ...(config.demo ? { demo: "loopback_fixture" } : {}),
        decision_source: compact ? "compact_context" : config.mode === "forced" ? "forced" : decision.decision_type,
        policy_version: compact ? "compact-context-v1" : decision.policy_version,
        fallback_reason: config.mode === "forced" || compact ? undefined : decision.fallback_reason,
        context_id: task.pack?.context_id ?? "default", context_hash: task.pack?.context_hash ?? null,
        initial_context_tokens: task.pack?.actual_tokens ?? 0,
        ...(task.pack?.context_id === "C0" && analysis.c2ReferenceTokens !== null
          ? { context_c2_reference_tokens: analysis.c2ReferenceTokens } : {}),
        context: task.pack ? { ...packMeta, files: files?.map(({ text: _text, ...metadata }) => metadata) } : null,
        evidence_count: selected?.evidence_count ?? 0, estimated_cost_usd: selected?.average_cost_usd ?? null,
        reasons: compact ? [context ? "Included compact excerpts from files named in the prompt."
          : "No compatible compact pack covered the files named in the prompt."]
          : config.mode === "forced" ? ["Benchmark supplied this exact model and context tier."] : decision.reasons,
        alternatives: decision.alternatives, task_features: { ...analysis.task, explicitPaths: undefined },
        repository: analysis.repository, repository_commit: repository.commit,
        pricing_source: appliedModelInfo?.pricing_source ?? null,
        model_metadata: appliedModelInfo,
        ...(!config.strict ? {
          original_model_metadata: modelInfo ? { key: modelInfo.key, display_name: modelInfo.display_name } : null,
          routing_evidence_limits: { min_samples: config.constraints.min_samples ?? 4, min_success: config.constraints.min_success ?? 0.50 },
        } : {}),
        ...(coverage ? { public_benchmark_coverage: coverage } : {}),
        ...(decision.decision_type === "public_benchmark_informed" ? { public_benchmark: decision.publicBenchmark } : {}),
        packs: analysis.packs.map(pack => ({ context_id: pack.context_id, actual_tokens: pack.actual_tokens,
          budget_tokens: pack.budget_tokens, context_hash: pack.context_hash, retrieval_version: pack.retrieval_version, strategy: pack.strategy })),
        confirmation_required: Boolean(blocked || provisional),
        router_overhead_ms: Date.now() - startedAt,
      });
      if (blocked || provisional) {
        console.warn(`[ContextRoute] Suggested ${selected?.model} + ${selected?.context}. Select that model in OpenCode and resubmit to confirm.`);
      }
      sessions.add(task);
    } catch (error) {
      if (switched && originalRef) {
        try { await ctx.session.switchModel({ sessionID, model: originalRef }); }
        catch (restoreError) { errorRecord("restore-model", restoreError, sessionID); }
      }
      errorRecord("route-prompt", error, sessionID);
    } finally {
      sessions.admissions.delete(sessionID);
    }
  });

  await ctx.session.hook("context", event => {
    // The request already contains the delivered user message even if the async inbox event is still queued.
    const messageID = event.messages.findLast(message => message.role === "user")?.id;
    const task = sessions.bindPrimary(event.sessionID, messageID);
    if (!task || task.finished || !task.pack || task.metrics.hasStep) return;
    if (messageID && task.messageId !== messageID) return;
    if (modelKey(event.model) !== task.expected) {
      errorRecord("context-model-mismatch", new Error("OpenCode resolved an unexpected model before context injection."), event.sessionID);
      return;
    }
    event.system.push({ type: "text", text: task.pack.text });
    task.contextPrepared = true;
  });

  await ctx.session.hook("model.request", event => {
    const task = event.kind === "primary" ? sessions.primaryRequest(event.sessionID) : sessions.latest(event.sessionID);
    if (!task) return;
    if (event.kind !== "primary") {
      task.metrics.auxiliaryRequests++;
      log({ type: "auxiliary_request", task_id: task.taskId, session_id: event.sessionID, request_kind: event.kind,
        actual_model: modelKey(event.model), pricing_known: task.pricedModels.has(modelKey(event.model)) });
      return;
    }
    if (task.finished) return;
    task.metrics.primaryRequests++;
    const actual = modelKey(event.model);
    if (!task.firstModel) {
      log({ type: "first_primary_request", task_id: task.taskId, session_id: event.sessionID, actual_model: actual,
        expected_model: task.expected, context_hash: task.contextPrepared ? task.pack?.context_hash ?? null : null });
      task.firstModel = actual;
    }
    if (actual !== task.expected || (task.pack && !task.contextPrepared)) {
      errorRecord("primary-request-mismatch", new Error("The requested model or initial pack did not match routing."), event.sessionID);
    }
  });

  await ctx.session.hook("retry", event => {
    const task = sessions.latest(event.sessionID);
    if (!task) return;
    const previous = event.decision;
    if (config.geminiGate) event.decision = { retry: false };
    else if (config.strict) {
      const status = "status" in event.error ? event.error.status : undefined;
      if (event.attempt >= 2) event.decision = { retry: false };
      else if (status === 429 && previous.retry) event.decision = { retry: true, delay: Math.max(previous.delay, 5000) };
    }
    log({ type: "retry", task_id: task.taskId, session_id: event.sessionID, provider_error: event.error.type,
      http_status: "status" in event.error ? event.error.status : undefined, attempt: event.attempt, decision: event.decision });
  });

  if (config.strict) {
    await ctx.session.hook("http.request", async event => {
      if (config.geminiGate) {
        const target = new URL(event.request.url);
        const model = event.model.providerID + "/" + event.model.id;
        if (target.origin !== config.geminiGate.origin || model !== guardedGemini) {
          errorRecord("gemini-gate-bypass", new Error("Gemini request did not target the configured quota gate."), event.sessionID);
        }
      }
      if (event.kind !== "primary") return;
      const task = sessions.primaryRequest(event.sessionID);
      if (!task || task.finished || task.wireChecked) return;
      task.wireChecked = true;
      let contextVerified: boolean | null = null;
      try {
        const body: unknown = await event.request.clone().json();
        contextVerified = task.pack ? containsText(body, task.pack.text) : !containsText(body, "<contextroute_repository>");
      } catch {
        // Compressed, streaming, or non-JSON bodies cannot be attested by this hook.
      }
      log({ type: "primary_http_request", task_id: task.taskId, session_id: event.sessionID,
        actual_model: modelKey(event.model), context_verified: contextVerified,
        context_hash: contextVerified && task.pack ? task.pack.context_hash : null });
    });
    await ctx.session.hook("http.response", event => {
      const task = sessions.latest(event.sessionID);
      if (task && !event.response.ok) log({ type: "provider_http_error", task_id: task.taskId, session_id: event.sessionID,
        request_kind: event.kind, http_status: event.response.status });
    });
  }

  log({ type: "plugin_loaded", directory: ctx.location.directory, mode: config.mode });
  return async () => {
    controller.abort();
    if (rpc) await rpc.dispose();
  };
}

function containsText(value: unknown, text: string): boolean {
  if (typeof value === "string") return value.includes(text);
  if (!value || typeof value !== "object") return false;
  return Object.values(value).some(child => containsText(child, text));
}

