import type { Plugin } from "@opencode/plugin";
import type { Model } from "./types.js";
type CatalogModel = Awaited<ReturnType<Plugin.Context["model"]["list"]>>["data"][number];
export declare function modelKey(model: {
    providerID: string;
    id: string;
    variant?: string;
}): string;
export declare function discoverModels(catalog: readonly CatalogModel[], original?: {
    providerID: string;
    id: string;
    variant?: string;
}): Model[];
export declare function capabilityExclusions(model: Model, tokens: number, outputReserve: number): string[];
export {};
