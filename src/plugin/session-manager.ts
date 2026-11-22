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
  contextPrepared: boolean;
  wireChecked?: boolean;
  pricedModels: Set<string>;
  firstModel?: string;
  finished: boolean;
  metrics: TaskMetrics;
}

export class SessionManager {
  private readonly pending = new Map<string, RoutedTask>();
  private readonly active = new Map<string, RoutedTask>();
  private readonly primary = new Map<string, RoutedTask | null>();
  readonly admissions = new Set<string>();

  add(task: RoutedTask) {
    this.pending.set(task.messageId, task);
  }

  get(session: string): RoutedTask | undefined {
    return this.active.get(session);
  }

  busy(session: string): boolean {
    return this.active.get(session)?.finished === false
      || Array.from(this.pending.values()).some(task => task.sessionId === session);
  }

  deliver(session: string, message: string): RoutedTask | undefined {
    const task = this.pending.get(message);
    if (task?.sessionId === session) {
      this.pending.delete(message);
      this.active.set(session, task);
      return task;
    }
  }

  bindPrimary(session: string, message?: string): RoutedTask | undefined {
    const active = this.active.get(session);
    const pending = message ? this.pending.get(message) : undefined;
    const continuing = !message || active?.messageId === message || Boolean(active?.firstModel);
    const task = pending?.sessionId === session ? pending : continuing ? active : undefined;
    this.primary.set(session, task ?? null);
    return task;
  }

  primaryRequest(session: string): RoutedTask | undefined {
    const task = this.primary.get(session);
    return task === undefined ? this.active.get(session) : task ?? undefined;
  }

  latest(session: string): RoutedTask | undefined {
    return this.active.get(session)
      ?? Array.from(this.pending.values()).find(task => task.sessionId === session);
  }

  remove(session: string) {
    this.active.delete(session);
    this.primary.delete(session);
    for (const [id, task] of this.pending) if (task.sessionId === session) this.pending.delete(id);
  }

  cancel(message: string) {
    const task = this.pending.get(message);
    if (task && this.primary.get(task.sessionId) === task) this.primary.set(task.sessionId, null);
    this.pending.delete(message);
  }
}

