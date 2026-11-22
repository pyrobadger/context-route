import { estimateCallCost, mean } from "./cost-estimator.js";
import { pairEvidence, uniqueEvidence } from "./evidence-store.js";
import { capabilityExclusions } from "./model-registry.js";
export function wilsonLowerBound(successes, count, z = 1.96) {
    if (!count)
        return null;
    if (successes === 0)
        return 0;
    const p = successes / count;
    const adjustment = z * z / count;
    return Math.max(0, (p + adjustment / 2 - z * Math.sqrt((p * (1 - p) + adjustment / 4) / count)) / (1 + adjustment));
}
export function rankPairs(input) {
    const limits = input.constraints ?? {};
    const threshold = limits.min_success ?? 0.50;
    const minimum = limits.min_samples ?? 4;
    const evidence = uniqueEvidence(input.evidence);
    const alternatives = [];
    for (const model of input.models) {
        for (const pack of input.packs) {
            const estimatedInput = input.task.tokens + pack.actual_tokens + (limits.reserved_input_tokens ?? 6000);
            const technical = capabilityExclusions(model, estimatedInput, limits.reserved_output_tokens ?? 2048);
            if (limits.max_context_tokens !== undefined && pack.actual_tokens > limits.max_context_tokens) {
                technical.push("Initial context exceeds the configured cap.");
            }
            const matches = pairEvidence(evidence, model.key, pack.context_id, pack.retrieval_version);
            const category = matches.filter(row => row.task_category === input.task.category);
            const rows = category.length >= minimum ? category : matches;
            const successes = rows.filter(row => row.resolved).length;
            const lowerBound = wilsonLowerBound(successes, rows.length);
            const cost = mean(rows.map(row => row.estimated_list_price_usd));
            const tokens = mean(rows.map(row => row.total_tokens));
            const latency = mean(rows.map(row => row.latency_seconds));
            const usageLimits = [];
            for (const [label, actual, limit] of [
                ["cost", cost, limits.max_cost_usd], ["total tokens", tokens, limits.max_total_tokens],
                ["latency", latency, limits.max_latency_seconds],
            ]) {
                if (limit !== undefined && (actual === null || actual > limit)) {
                    usageLimits.push(`Measured ${label} is ${actual === null ? "unavailable" : "above the configured limit"}.`);
                }
            }
            alternatives.push({
                model: model.key,
                context: pack.context_id,
                context_tokens: pack.actual_tokens,
                viable: technical.length === 0,
                constraints_met: technical.length === 0 && usageLimits.length === 0,
                exclusions: [...technical, ...usageLimits],
                evidence_scope: category.length >= minimum ? "category" : rows.length ? "pooled" : "none",
                evidence_count: rows.length,
                observed_success: rows.length ? successes / rows.length : null,
                success_lower_bound: lowerBound,
                average_cost_usd: cost,
                average_tokens: tokens,
                average_latency_seconds: latency,
                estimated_cost_per_success_usd: cost !== null && lowerBound ? cost / lowerBound : null,
                one_call_context_cost_usd: estimateCallCost(model.pricing, { input: pack.actual_tokens, output: 0, reasoning: 0, cache: { read: 0, write: 0 } }),
            });
        }
    }
    const order = (a, b) => a.model < b.model ? -1 : a.model > b.model ? 1
        : a.context < b.context ? -1 : Number(a.context !== b.context);
    const numeric = (a, b) => (a ?? Number.MAX_VALUE) - (b ?? Number.MAX_VALUE);
    alternatives.sort(order);
    const allowed = alternatives.filter(candidate => candidate.constraints_met);
    const empirical = allowed.filter(candidate => candidate.evidence_count >= minimum
        && candidate.success_lower_bound !== null && candidate.success_lower_bound >= threshold && candidate.average_cost_usd !== null);
    empirical.sort((a, b) => numeric(a.average_cost_usd, b.average_cost_usd) || numeric(a.average_tokens, b.average_tokens)
        || numeric(a.average_latency_seconds, b.average_latency_seconds) || order(a, b));
    if (empirical[0]) {
        const selected = empirical[0];
        return { selected, decision_type: "empirical", alternatives, reasons: [
                `Wilson lower bound ${selected.success_lower_bound.toFixed(3)} meets ${threshold.toFixed(3)} from ${selected.evidence_count} verified tasks (${selected.evidence_scope} evidence).`,
                `Lowest mean whole-task list price among qualifying pairs: $${selected.average_cost_usd.toFixed(6)}.`,
                `The selected repository pack uses approximately ${selected.context_tokens} tokens.`,
            ] };
    }
    const hasEvidence = alternatives.some(candidate => candidate.viable && candidate.evidence_count > 0);
    const provisionalPool = allowed.filter(candidate => candidate.model === input.original);
    const selected = (provisionalPool.length ? provisionalPool : allowed).sort((a, b) => a.context_tokens - b.context_tokens
        || numeric(a.one_call_context_cost_usd, b.one_call_context_cost_usd) || order(a, b))[0] ?? null;
    return {
        selected,
        alternatives,
        decision_type: !selected ? "no_feasible_pair" : hasEvidence ? "insufficient_evidence" : "cold_start",
        reasons: selected ? [
            hasEvidence ? "No allowed pair met the minimum sample, success, and whole-task cost criteria." : "No usable verified observations exist for these exact pairs.",
            selected.model === input.original ? "Retained the original model and chose its smallest compatible repository pack."
                : "The original model had no eligible pair. The provisional pair uses the smallest compatible repository pack.",
            ...(input.repository?.truncated ? ["Repository indexing reached its resource cap."] : []),
        ] : ["No pair satisfies the technical and configured resource limits."],
    };
}
