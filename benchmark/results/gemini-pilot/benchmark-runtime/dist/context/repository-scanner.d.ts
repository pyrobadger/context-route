export interface SourceFile {
    path: string;
    language: string;
    lines: string[];
    isTest: boolean;
}
export interface Repository {
    root: string;
    commit: string | null;
    files: SourceFile[];
    languages: string[];
    truncated: boolean;
}
export declare function scanRepository(directory: string): Promise<Repository>;
