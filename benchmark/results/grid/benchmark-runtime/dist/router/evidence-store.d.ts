import type { Observation } from "./types.js";
export declare function isObservation(value: unknown): value is Observation;
export declare function uniqueEvidence(rows: unknown[]): Observation[];
export declare function loadEvidence(path: string): Promise<Observation[]>;
export declare function pairEvidence(evidence: Observation[], model: string, tier: string, version: string): Observation[];
