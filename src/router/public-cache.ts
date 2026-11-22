import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export const benchmarkName = "SWE-bench Verified";
export const sourceRevision = "40f164d5b8f1d249bf95a6df8b74b577fd8e519d";
export const benchmarkSize = 500;
export const sourceRoot = "https://github.com/swe-bench/experiments";
const evaluationDir = "evaluation/verified";

export interface ReviewedRun {
  id: string;
  key: string;
  modelVersion: string;
  label: string;
  modelSource: string;
  validUntil?: string;
}

// A benchmark label is not an OpenCode alias. These mappings were reviewed against the
// named API version. Routing only matches the exact key (including any variant).
export const reviewedRuns: readonly ReviewedRun[] = [
  {
    id: "20250726_mini-v1.0.0_gemini-2.5-pro",
    key: "google/gemini-2.5-pro", modelVersion: "gemini-2.5-pro", label: "gemini-2.5-pro",
    modelSource: "https://aistudio.google.com/docs/models/gemini-2.5-pro",
    validUntil: "2026-12-01T00:00:00.000Z",
  },
  {
    id: "20250726_mini-v1.0.0_o3-2025-04-16",
    key: "openai/o3-2025-04-16", modelVersion: "o3-2025-04-16", label: "o3-20250416",
    modelSource: "https://developers.openai.com/api/docs/models/o3",
    validUntil: "2026-12-11T00:00:00.000Z",
  },
  {
    id: "20250726_mini-v1.0.0_o4-mini-2025-04-16",
    key: "openai/o4-mini-2025-04-16", modelVersion: "o4-mini-2025-04-16", label: "o4-mini-20250416",
    modelSource: "https://developers.openai.com/api/docs/models/o4-mini",
    validUntil: "2026-10-23T00:00:00.000Z",
  },
];
export const knownExclusions: Readonly<Record<string, string>> = {
  "google/gemini-2.5-flash": "The checked submission lacks a published per-task verdict file in Experiments.",
  "google/gemini-3-flash-preview": "The available submission uses high reasoning and lacks the leaderboard reproduction check.",
  "google/gemini-3.5-flash": "The provider redirects this ID to Gemini 3.6 Flash.",
  "google/gemini-3.5-flash-lite": "No verified submission for this exact model ID.",
  "opencode/big-pickle": "No verified benchmark identity for this hosted model ID.",
  "opencode/deepseek-v4.1-flash-free": "No reviewed mapping from this hosted configuration to a verified run.",
};

export interface PublicRecord {
  schema: 1;
  run: string;
  modelKey: string;
  modelVersion: string;
  modelSource: string;
  validUntil: string | null;
  benchmark: typeof benchmarkName;
  harness: "mini-SWE-agent";
  harnessVersion: "1.0.0";
  evaluationDate: string;
  attempts: 1;
  reasoning: "unreported";
  sourceUrl: string;
  revision: string;
  metadataHash: string;
  resultsHash: string;
  instancesHash: string;
  resolved: number;
  outcomes: Record<string, boolean>;
  cachedAt: string;
}

export interface CacheUpdate {
  schema: 1;
  updatedAt: string;
  available: string[];
  errors: { key: string; reason: string }[];
}

export interface PublicCache {
  records: PublicRecord[];
  update: CacheUpdate | null;
}

const digestPattern = /^[a-f0-9]{64}$/;
const instancePattern = /^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$/;
const isDigest = (value: unknown) => typeof value === "string" && digestPattern.test(value);
export const sha256 = (value: string) => createHash("sha256").update(value, "utf8").digest("hex");
export const instancesHash = (outcomes: Record<string, boolean>) => sha256(JSON.stringify(Object.keys(outcomes).sort()));
export const sourcePath = (run: ReviewedRun) => evaluationDir + "/" + run.id;
export const sourceUrl = (run: ReviewedRun) => sourceRoot + "/tree/" + sourceRevision + "/" + sourcePath(run);
export const rawUrl = (run: ReviewedRun, file: string) =>
  "https://raw.githubusercontent.com/swe-bench/experiments/" + sourceRevision + "/" + sourcePath(run) + "/" + file;
export const cacheDirectory = () => process.env.CONTEXTROUTE_BENCHMARK_CACHE || join(homedir(), ".contextroute", "benchmarks");
export const evaluationDate = (run: ReviewedRun) => run.id.slice(0, 4) + "-" + run.id.slice(4, 6) + "-" + run.id.slice(6, 8);

export function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function isExactCatalogKey(value: unknown): value is string {
  return typeof value === "string" && /^[\w.+-]+\/[\w./+:@-]+(?:#[\w.+-]+)?$/.test(value);
}

export function activeRecord(record: PublicRecord, now = new Date()) {
  return record.validUntil === null || Date.parse(record.validUntil) > now.getTime();
}

function validateRecord(value: unknown, run: ReviewedRun): PublicRecord | null {
  const row = object(value) as Partial<PublicRecord>;
  if (row.schema !== 1 || row.run !== run.id || row.modelKey !== run.key || row.modelVersion !== run.modelVersion
    || row.modelSource !== run.modelSource || row.validUntil !== (run.validUntil ?? null)
    || row.benchmark !== benchmarkName || row.harness !== "mini-SWE-agent" || row.harnessVersion !== "1.0.0"
    || row.evaluationDate !== evaluationDate(run) || row.attempts !== 1 || row.reasoning !== "unreported"
    || row.sourceUrl !== sourceUrl(run) || row.revision !== sourceRevision || !isDigest(row.metadataHash)
    || !isDigest(row.resultsHash) || typeof row.cachedAt !== "string" || !Number.isFinite(Date.parse(row.cachedAt))) return null;
  const outcomes = object(row.outcomes);
  const pairs = Object.entries(outcomes);
  if (pairs.length !== benchmarkSize || pairs.some(([id, outcome]) => !instancePattern.test(id) || typeof outcome !== "boolean")
    || row.resolved !== pairs.filter(([, resolved]) => resolved).length
    || row.instancesHash !== instancesHash(outcomes as Record<string, boolean>)) return null;
  return row as PublicRecord;
}

function validateUpdate(value: unknown): CacheUpdate | null {
  const data = object(value) as Partial<CacheUpdate>;
  if (data.schema !== 1 || typeof data.updatedAt !== "string" || !Number.isFinite(Date.parse(data.updatedAt))
    || !Array.isArray(data.available) || !data.available.every(isExactCatalogKey) || !Array.isArray(data.errors)) return null;
  if (data.errors.some(value => {
    const error = object(value);
    return !isExactCatalogKey(error.key) || typeof error.reason !== "string";
  })) return null;
  return data as CacheUpdate;
}

async function readJson(path: string): Promise<unknown> {
  try {
    return JSON.parse(await readFile(path, "utf8")) as unknown;
  } catch {
    // An absent, corrupt, or inaccessible cache carries no routing authority.
    return null;
  }
}

export async function readPublicCache(directory = cacheDirectory()): Promise<PublicCache> {
  const [files, update] = await Promise.all([
    Promise.all(reviewedRuns.map(async run => validateRecord(await readJson(join(directory, run.id + ".json")), run))),
    readJson(join(directory, "update.json")),
  ]);
  return { records: files.filter(record => record !== null), update: validateUpdate(update) };
}

export async function atomicCacheWrite(directory: string, filename: string, value: PublicRecord | CacheUpdate) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const target = join(directory, filename);
  const temporary = target + "." + randomUUID() + ".tmp";
  try {
    await writeFile(temporary, JSON.stringify(value) + "\n", { encoding: "utf8", flag: "wx", mode: 0o600 });
    await rename(temporary, target);
  } finally {
    await unlink(temporary).catch(() => {});
  }
}

export function benchmarkCoverage(available: readonly string[], cache: PublicCache, now = new Date()) {
  const models = [...new Set(available)].sort();
  const matched = new Set(cache.records.filter(record => activeRecord(record, now)).map(record => record.modelKey));
  return {
    available: models.length, matched: models.filter(key => matched.has(key)).length,
    lastUpdate: cache.update?.updatedAt ?? null,
  };
}

