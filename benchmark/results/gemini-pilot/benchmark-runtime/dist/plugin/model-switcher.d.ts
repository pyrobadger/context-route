import type { Plugin } from "@opencode/plugin";
type SessionID = Parameters<Plugin.Context["session"]["get"]>[0]["sessionID"];
export declare function applyModel(ctx: Plugin.Context, sessionID: SessionID, selected: string, catalog: Awaited<ReturnType<Plugin.Context["model"]["list"]>>["data"]): Promise<void>;
export {};
