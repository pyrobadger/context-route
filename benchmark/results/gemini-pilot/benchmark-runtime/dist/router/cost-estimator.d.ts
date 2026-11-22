import type { Price, TokenUsage } from "./types.js";
export declare function inputTokens(usage: TokenUsage): number;
export declare function outputTokens(usage: TokenUsage): number;
export declare function estimateCallCost(prices: Price[], usage: TokenUsage): number | null;
export declare function mean(values: (number | null)[]): number | null;
