export type TaskCategory = "bug" | "feature" | "refactor" | "test" | "docs" | "performance" | "other";
export interface TaskFeatures {
    tokens: number;
    category: TaskCategory;
    explicitPaths: string[];
    languageIndicators: string[];
    stackTrace: boolean;
    potentiallyRelevantFiles: number;
    crossModule: boolean;
    nearbyTests: boolean;
}
export declare const sourcePathPattern: RegExp;
export declare function analyzeTask(prompt: string, relevantFiles: number, nearbyTests: boolean): TaskFeatures;
