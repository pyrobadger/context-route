import type { SourceFile } from "./repository-scanner.js";
export declare function terms(text: string): string[];
export interface Span {
    path: string;
    start_line: number;
    end_line: number;
    retrieval_score: number;
    lines: string[];
    reason: "lexical" | "dependency" | "caller" | "test";
}
export declare function rankSpans(files: SourceFile[], prompt: string, paths: string[]): Span[];
