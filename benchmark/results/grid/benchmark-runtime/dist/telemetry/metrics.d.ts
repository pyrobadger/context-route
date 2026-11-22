import type { TokenUsage } from "../router/types.js";
export declare function usageMetrics(tokens: TokenUsage | undefined): {
    input_tokens: null;
    output_tokens: null;
    total_tokens: null;
    cache_read_tokens: null;
    cache_write_tokens: null;
} | {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
};
export declare class TaskMetrics {
    private readonly steps;
    private readonly tools;
    primaryRequests: number;
    auxiliaryRequests: number;
    finish: string | null;
    step(id: string, tokens: TokenUsage, finish: string, cost?: number | null): void;
    tool(id: string): void;
    get hasStep(): boolean;
    summary(): {
        input_tokens: null;
        output_tokens: null;
        total_tokens: null;
        cache_read_tokens: null;
        cache_write_tokens: null;
        primary_model_calls: number;
        auxiliary_model_calls: number;
        total_model_calls: number;
        tool_calls: number;
        estimated_list_price_usd: number | null;
        token_scope: string;
        final_model_finish: string | null;
    } | {
        input_tokens: number;
        output_tokens: number;
        total_tokens: number;
        cache_read_tokens: number;
        cache_write_tokens: number;
        primary_model_calls: number;
        auxiliary_model_calls: number;
        total_model_calls: number;
        tool_calls: number;
        estimated_list_price_usd: number | null;
        token_scope: string;
        final_model_finish: string | null;
    };
}
