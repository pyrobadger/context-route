import type { SourceFile } from "./repository-scanner.js";
export interface Relationships {
    imports: Map<string, string[]>;
    callers: Map<string, string[]>;
    nearbyTests: Map<string, string[]>;
}
export declare function analyzeDependencies(files: SourceFile[]): Relationships;
