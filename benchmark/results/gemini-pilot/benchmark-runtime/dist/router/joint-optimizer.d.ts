import type { Decision, RoutingInput } from "./types.js";
export declare const policyVersion = "default-tier-fallback-v2";
export declare function wilsonLowerBound(successes: number, count: number, z?: number): number | null;
export declare function rankPairs(input: RoutingInput): Decision;
