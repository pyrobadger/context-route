import type { ContextId, ContextPack } from "../context/context-builder.js";
import type { TaskCategory, TaskFeatures } from "../context/task-analysis.js";
export interface TokenUsage {
    input: number;
    output: number;
    reasoning: number;
    cache: {
        read: number;
        write: number;
    };
}
export interface Price {
    input: number;
    output: number;
    cache_read: number;
    cache_write: number;
    context_over?: number;
}
export interface Model {
    key: string;
    provider_id: string;
    model_id: string;
    display_name: string;
    variant?: string;
    enabled: boolean;
    supports_tools: boolean;
    supports_text: boolean;
    context_window: number;
    input_limit?: number;
    output_limit?: number;
    pricing: Price[];
    pricing_source: string | null;
    pricing_read_at: string | null;
}
export interface Observation {
    execution_id: string;
    task_id: string;
    task_category: TaskCategory;
    repository_id: string;
    repository_commit: string;
    model_id: string;
    context_id: ContextId;
    context_hash: string;
    retrieval_version: string;
    resolved: boolean;
    verification_type: "task_tests" | "issue_verified" | "patch_applied" | "execution_only";
    input_tokens: number | null;
    output_tokens: number | null;
    total_tokens: number | null;
    latency_seconds: number | null;
    estimated_list_price_usd: number | null;
    execution_status: "completed" | "failed" | "timeout" | "invalid";
}
export interface Constraints {
    min_success?: number;
    min_samples?: number;
    max_context_tokens?: number;
    max_total_tokens?: number;
    max_cost_usd?: number;
    max_latency_seconds?: number;
    reserved_input_tokens?: number;
    reserved_output_tokens?: number;
}
export interface Candidate {
    model: string;
    context: ContextId;
    context_tokens: number;
    viable: boolean;
    constraints_met: boolean;
    exclusions: string[];
    evidence_scope: "category" | "pooled" | "none";
    evidence_count: number;
    observed_success: number | null;
    success_lower_bound: number | null;
    average_cost_usd: number | null;
    average_tokens: number | null;
    average_latency_seconds: number | null;
    estimated_cost_per_success_usd: number | null;
    one_call_context_cost_usd: number | null;
}
export interface Decision {
    selected: Candidate | null;
    policy_version: string;
    decision_type: "empirical" | "cold_start_fallback" | "no_feasible_pair";
    fallback_reason?: "no_usable_evidence" | "insufficient_evidence";
    reasons: string[];
    alternatives: Candidate[];
}
export interface RoutingInput {
    models: Model[];
    packs: ContextPack[];
    evidence: Observation[];
    task: TaskFeatures;
    repository?: {
        indexed_files: number;
        truncated: boolean;
    };
    original: string;
    fallbackContext?: ContextId;
    constraints?: Constraints;
}
