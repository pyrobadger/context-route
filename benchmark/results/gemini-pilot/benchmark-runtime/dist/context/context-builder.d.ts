import { type Span } from "./lexical-retrieval.js";
import type { Repository } from "./repository-scanner.js";
export declare const budgets: {
    readonly C1: 1200;
    readonly C2: 3200;
    readonly C3: 6400;
};
export declare const contextIds: readonly ["C1", "C2", "C3", "L1", "L2", "L3"];
export type ContextId = typeof contextIds[number];
export declare const retrievalVersion = "bm25-deps-v2";
export interface ContextFile extends Omit<Span, "lines"> {
    text: string;
}
export interface ContextPack {
    context_id: ContextId;
    strategy: "lexical" | "dependencies";
    retrieval_version: string;
    budget_tokens: number;
    actual_tokens: number;
    files: ContextFile[];
    context_hash: string;
    text: string;
}
export declare function hashContext(text: string): string;
export declare function buildContexts(repository: Repository, prompt: string): {
    packs: ContextPack[];
    task: import("./task-analysis.js").TaskFeatures;
    repository: {
        indexed_files: number;
        languages: string[];
        truncated: boolean;
        candidate_files: string[];
        dependency_edges: number;
        retrieval_scores: {
            min: number;
            max: number;
            matched_spans: number;
        };
    };
};
