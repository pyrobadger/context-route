import { isAbsolute, join, resolve } from "node:path";
import { contextIds } from "../context/context-builder.js";
const modes = ["auto", "confirm", "baseline", "forced", "joint"];
export function readConfig(raw, directory) {
    const mode = modes.find(value => value === raw.mode) ?? "auto";
    const strict = mode === "baseline" || mode === "forced" || mode === "joint";
    const data = join(directory, ".contextroute");
    const file = (value, fallback) => typeof value === "string" && value
        ? isAbsolute(value) ? value : resolve(directory, value)
        : join(data, fallback);
    const constraints = {};
    const requested = raw.constraints;
    if (requested && typeof requested === "object") {
        const keys = ["min_success", "min_samples", "max_context_tokens", "max_total_tokens", "max_cost_usd",
            "max_latency_seconds", "reserved_input_tokens", "reserved_output_tokens"];
        for (const key of keys) {
            const value = requested[key];
            if (typeof value !== "number")
                continue;
            if (!Number.isFinite(value) || value < 0 || (key === "min_success" && value > 1)
                || (key === "min_samples" && (!Number.isInteger(value) || value < 1))) {
                throw new RangeError("Invalid ContextRoute constraint " + key);
            }
            constraints[key] = value;
        }
    }
    const context = contextIds.find(value => raw.context === value);
    const model = typeof raw.model === "string" ? raw.model : undefined;
    if (mode === "forced" && (!model || !context))
        throw new Error("Forced mode requires a model and a valid context tier.");
    return {
        mode, strict, model, context,
        constraints,
        evidence: file(raw.evidence, "evidence"),
        telemetry: file(raw.telemetry_path, "execution.jsonl"),
        telemetryEnabled: strict || raw.telemetry !== false,
        packPath: typeof raw.pack_path === "string" ? file(raw.pack_path, "context.json") : undefined,
        taskId: typeof raw.task_id === "string" ? raw.task_id : undefined,
        models: Array.isArray(raw.models) ? raw.models.filter((id) => typeof id === "string") : undefined,
        contexts: Array.isArray(raw.contexts) ? raw.contexts.filter((id) => contextIds.some(context => context === id)) : ["C1", "C2", "C3"],
    };
}
