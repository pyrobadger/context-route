import type { ContextPack } from "../context/context-builder.js";
import { TaskMetrics } from "../telemetry/metrics.js";
export interface RoutedTask {
    taskId: string;
    messageId: string;
    sessionId: string;
    startedAt: number;
    original: string;
    expected: string;
    pack?: ContextPack;
    delivered: boolean;
    contextPrepared: boolean;
    wireChecked?: boolean;
    pricedModels: Set<string>;
    firstModel?: string;
    finished: boolean;
    metrics: TaskMetrics;
}
export declare class SessionManager {
    private readonly pending;
    private readonly active;
    readonly admissions: Set<string>;
    add(task: RoutedTask): void;
    get(session: string): RoutedTask | undefined;
    busy(session: string): boolean;
    deliver(session: string, message: string): RoutedTask | undefined;
    latest(session: string): RoutedTask | undefined;
    remove(session: string): void;
    cancel(message: string): void;
}
