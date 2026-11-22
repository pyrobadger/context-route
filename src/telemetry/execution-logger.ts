import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

export function serializeEvent(event: Record<string, unknown>): string {
  return JSON.stringify({ time: new Date().toISOString(), ...event }) + "\n";
}

export function createLogger(path: string, enabled: boolean, strict: boolean) {
  return (event: Record<string, unknown>) => {
    if (!enabled) return;
    try {
      mkdirSync(dirname(path), { recursive: true });
      appendFileSync(path, serializeEvent(event), { encoding: "utf8", mode: 0o600 });
    } catch (error) {
      console.error("[ContextRoute] Telemetry write failed.", error instanceof Error ? error.name : "UnknownError");
      if (strict) throw error;
    }
  };
}

export type Logger = ReturnType<typeof createLogger>;

