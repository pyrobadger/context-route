export declare function serializeEvent(event: Record<string, unknown>): string;
export declare function createLogger(path: string, enabled: boolean, strict: boolean): (event: Record<string, unknown>) => void;
export type Logger = ReturnType<typeof createLogger>;
