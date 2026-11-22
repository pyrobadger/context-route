import { writeFile, mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import { buildContexts } from "../context/context-builder.js";
import { scanRepository } from "../context/repository-scanner.js";
import { rankPairs } from "../router/joint-optimizer.js";
import { loadEvidence } from "../router/evidence-store.js";
import { discoverModels, modelKey } from "../router/model-registry.js";
import { createLogger } from "../telemetry/execution-logger.js";
import { TaskMetrics, usageMetrics } from "../telemetry/metrics.js";
import { readConfig } from "./config.js";
import { applyModel } from "./model-switcher.js";
import { SessionManager } from "./session-manager.js";
export async function registerHooks(ctx) {
    const config = readConfig(ctx.options, ctx.location.directory);
    const log = createLogger(config.telemetry, config.telemetryEnabled, config.strict);
    const sessions = new SessionManager();
    const controller = new AbortController();
    const errorRecord = (stage, error, sessionID) => {
        log({ type: "integration_error", stage, session_id: sessionID, error_type: error instanceof Error ? error.name : "UnknownError" });
        if (config.strict)
            throw error;
        console.warn(`[ContextRoute] ${stage} failed. See the configured telemetry log.`);
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
                const previous = sessions.get(sessionID);
                const next = sessions.deliver(sessionID, event.data.inboxID);
                if (previous && next && previous !== next && !previous.finished) {
                    previous.finished = true;
                    log({ type: "execution", task_id: previous.taskId, session_id: sessionID, execution_status: "interrupted",
                        latency_seconds: (Date.now() - previous.startedAt) / 1000, ...previous.metrics.summary() });
                }
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
            if (!task)
                continue;
            const identity = { task_id: task.taskId, session_id: sessionID };
            switch (event.type) {
                case "session.step.ended":
                    if (!task.finished)
                        task.metrics.step(event.data.assistantMessageID, event.data.tokens, event.data.finish, task.pricedModels.has(task.expected) ? event.data.cost : null);
                    break;
                case "session.tool.called":
                    if (!task.finished)
                        task.metrics.tool(event.id);
                    break;
                case "session.usage.updated":
                    log({ type: "usage", ...identity, ...usageMetrics(event.data.tokens),
                        opencode_cost_usd: event.data.cost, token_scope: "OpenCode cumulative session usage" });
                    break;
                case "session.execution.succeeded":
                case "session.execution.failed":
                case "session.execution.interrupted": {
                    if (task.finished)
                        break;
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
        if (controller.signal.aborted)
            return;
        log({ type: "integration_error", stage: "event-stream", error_type: error instanceof Error ? error.name : "UnknownError" });
        console.error("[ContextRoute] Event stream failed.");
    });
    await ctx.session.hook("prompt", async (event) => {
        if (!event.prompt.text?.trim())
            return;
        const { sessionID, messageID } = event;
        if (sessions.admissions.has(sessionID)) {
            errorRecord("concurrent-prompt", new Error("Another prompt is being admitted for this session."), sessionID);
            return;
        }
        sessions.admissions.add(sessionID);
        const startedAt = Date.now();
        let originalRef;
        let switched = false;
        try {
            const session = await ctx.session.get({ sessionID });
            if (session.parentID)
                return;
            const location = { directory: session.location.directory };
            const availability = await ctx.model.list({ location });
            const agent = !session.model && session.agent ? await ctx.agent.get({ agentID: session.agent }) : undefined;
            originalRef = session.model ?? agent?.data?.model;
            if (!originalRef) {
                const fallback = (await ctx.model.default({ location })).data;
                if (fallback)
                    originalRef = { providerID: fallback.providerID, id: fallback.id };
            }
            if (!originalRef)
                throw new Error("OpenCode did not report an original model.");
            const original = modelKey(originalRef);
            const models = discoverModels(availability.data, originalRef);
            const task = { taskId: config.taskId ?? messageID, messageId: messageID, sessionId: sessionID,
                startedAt, original, expected: original, delivered: false, contextPrepared: false, finished: false,
                pricedModels: new Set(models.filter(model => model.pricing_source).map(model => model.key)), metrics: new TaskMetrics() };
            const identity = { task_id: task.taskId, session_id: sessionID, original_model: original };
            const modelInfo = models.find(model => model.key === original);
            if (config.mode === "baseline") {
                log({ type: "route", ...identity, selected_model: original, decision_source: "baseline", context_id: "default",
                    initial_context_tokens: 0, pricing_source: modelInfo?.pricing_source ?? null, router_overhead_ms: 0 });
                sessions.add(task);
                return;
            }
            const repository = await scanRepository(location.directory);
            const analysis = buildContexts(repository, event.prompt.text);
            const evidence = await loadEvidence(config.evidence);
            const desired = config.mode === "forced" ? [config.context] : config.contexts;
            const candidateModels = config.models ? models.filter(model => config.models.includes(model.key)) : models;
            const decision = rankPairs({ models: candidateModels, packs: analysis.packs.filter(pack => desired.includes(pack.context_id)),
                evidence, task: analysis.task, repository: analysis.repository, original, constraints: config.constraints });
            const selected = config.mode === "forced"
                ? decision.alternatives.find(candidate => candidate.model === config.model && candidate.context === config.context && candidate.constraints_met)
                : decision.selected;
            if (config.strict && (!selected || !analysis.packs.find(pack => pack.context_id === selected.context)?.actual_tokens)) {
                throw new Error("Benchmark configuration is unavailable or its repository pack is empty.");
            }
            const context = analysis.packs.find(pack => pack.context_id === selected?.context);
            const blocked = selected && selected.model !== original && (config.mode === "confirm" || sessions.busy(sessionID));
            const provisional = selected && selected.model !== original && config.mode === "auto" && decision.decision_type !== "empirical";
            if (config.strict && blocked)
                throw new Error("A running task prevented the requested model switch.");
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
            const { text: _packText, files, ...packMeta } = task.pack ?? {};
            const appliedModelInfo = models.find(model => model.key === task.expected);
            log({
                type: "route", ...identity, selected_model: selected?.model ?? null, applied_model: task.expected,
                decision_source: config.mode === "forced" ? "forced" : decision.decision_type,
                context_id: task.pack?.context_id ?? "default", context_hash: task.pack?.context_hash ?? null,
                initial_context_tokens: task.pack?.actual_tokens ?? 0,
                context: task.pack ? { ...packMeta, files: files?.map(({ text: _text, ...metadata }) => metadata) } : null,
                evidence_count: selected?.evidence_count ?? 0, estimated_cost_usd: selected?.average_cost_usd ?? null,
                reasons: config.mode === "forced" ? ["Benchmark supplied this exact model and context tier."] : decision.reasons,
                alternatives: decision.alternatives, task_features: { ...analysis.task, explicitPaths: undefined },
                repository: analysis.repository, repository_commit: repository.commit,
                pricing_source: appliedModelInfo?.pricing_source ?? null,
                model_metadata: appliedModelInfo,
                packs: analysis.packs.map(pack => ({ context_id: pack.context_id, actual_tokens: pack.actual_tokens,
                    budget_tokens: pack.budget_tokens, context_hash: pack.context_hash, retrieval_version: pack.retrieval_version, strategy: pack.strategy })),
                confirmation_required: Boolean(blocked || provisional),
                router_overhead_ms: Date.now() - startedAt,
            });
            if (blocked || provisional) {
                console.warn(`[ContextRoute] Suggested ${selected?.model} + ${selected?.context}. Select that model in OpenCode and resubmit to confirm.`);
            }
            sessions.add(task);
        }
        catch (error) {
            if (switched && originalRef) {
                try {
                    await ctx.session.switchModel({ sessionID, model: originalRef });
                }
                catch (restoreError) {
                    errorRecord("restore-model", restoreError, sessionID);
                }
            }
            errorRecord("route-prompt", error, sessionID);
        }
        finally {
            sessions.admissions.delete(sessionID);
        }
    });
    await ctx.session.hook("context", event => {
        const task = sessions.get(event.sessionID);
        if (!task || task.finished || !task.pack || task.metrics.hasStep)
            return;
        if (modelKey(event.model) !== task.expected) {
            errorRecord("context-model-mismatch", new Error("OpenCode resolved an unexpected model before context injection."), event.sessionID);
            return;
        }
        event.system.push({ type: "text", text: task.pack.text });
        task.contextPrepared = true;
    });
    await ctx.session.hook("model.request", event => {
        const task = sessions.latest(event.sessionID);
        if (!task)
            return;
        if (event.kind !== "primary") {
            task.metrics.auxiliaryRequests++;
            log({ type: "auxiliary_request", task_id: task.taskId, session_id: event.sessionID, request_kind: event.kind,
                actual_model: modelKey(event.model), pricing_known: task.pricedModels.has(modelKey(event.model)) });
            return;
        }
        if (!task.delivered || task.finished)
            return;
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
        if (!task)
            return;
        const previous = event.decision;
        if (config.strict) {
            const status = "status" in event.error ? event.error.status : undefined;
            if (event.attempt >= 2)
                event.decision = { retry: false };
            else if (status === 429 && previous.retry)
                event.decision = { retry: true, delay: Math.max(previous.delay, 5000) };
        }
        log({ type: "retry", task_id: task.taskId, session_id: event.sessionID, provider_error: event.error.type,
            http_status: "status" in event.error ? event.error.status : undefined, attempt: event.attempt, decision: event.decision });
    });
    if (config.strict) {
        await ctx.session.hook("http.request", async (event) => {
            if (event.kind !== "primary")
                return;
            const task = sessions.get(event.sessionID);
            if (!task || task.wireChecked)
                return;
            task.wireChecked = true;
            let contextVerified = null;
            try {
                const body = await event.request.clone().json();
                contextVerified = task.pack ? containsText(body, task.pack.text) : !containsText(body, "<contextroute_repository>");
            }
            catch {
                // Compressed, streaming, or non-JSON bodies cannot be attested by this hook.
            }
            log({ type: "primary_http_request", task_id: task.taskId, session_id: event.sessionID,
                actual_model: modelKey(event.model), context_verified: contextVerified,
                context_hash: contextVerified && task.pack ? task.pack.context_hash : null });
        });
        await ctx.session.hook("http.response", event => {
            const task = sessions.latest(event.sessionID);
            if (task && !event.response.ok)
                log({ type: "provider_http_error", task_id: task.taskId, session_id: event.sessionID,
                    request_kind: event.kind, http_status: event.response.status });
        });
    }
    log({ type: "plugin_loaded", directory: ctx.location.directory, mode: config.mode });
    return () => controller.abort();
}
function containsText(value, text) {
    if (typeof value === "string")
        return value.includes(text);
    if (!value || typeof value !== "object")
        return false;
    return Object.values(value).some(child => containsText(child, text));
}
