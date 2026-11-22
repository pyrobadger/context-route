import { parseDocument } from "yaml";
import {
  activeRecord, atomicCacheWrite, benchmarkName, benchmarkSize, cacheDirectory, evaluationDate, instancesHash, isExactCatalogKey,
  knownExclusions, object, rawUrl, readPublicCache, reviewedRuns, sha256, sourceRevision, sourceUrl,
  type CacheUpdate, type PublicCache, type PublicRecord, type ReviewedRun,
} from "./public-cache.js";

const candidateInstance = /^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-\d+$/;
type ReadSource = (url: string, maxBytes: number) => Promise<string>;

export async function fetchSource(url: string, maxBytes: number) {
  const response = await fetch(url, { redirect: "error", signal: AbortSignal.timeout(15_000) });
  if (!response.ok || !response.body) throw new Error("Source returned HTTP " + response.status);
  const read = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let content = "";
  let size = 0;
  try {
    for (;;) {
      const { done, value } = await read.read();
      if (done) return content + decoder.decode();
      size += value.length;
      if (size > maxBytes) throw new Error("Public source exceeded its size limit");
      content += decoder.decode(value, { stream: true });
    }
  } finally {
    await read.cancel().catch(() => {});
    read.releaseLock();
  }
}

function reviewedScore(run: ReviewedRun, metadata: string) {
  const parsed = parseDocument(metadata, { schema: "core", uniqueKeys: true });
  if (parsed.errors.length) throw new Error("Invalid SWE-bench metadata");
  const doc = object(parsed.toJS({ maxAliasCount: 0 }));
  const info = object(doc.info);
  const tags = object(doc.tags);
  if (tags.checked !== true || tags.agent !== "mini-SWE-agent" || tags.agent_org !== "SWE-agent" || tags.os_system !== true
    || info["mini-swe-agent_version"] !== "1.0.0" || object(tags.system).attempts !== 1
    || tags.reasoning_effort != null || !Array.isArray(tags.model) || tags.model.length !== 1 || tags.model[0] !== run.label
    || typeof info.resolved !== "number" || !Number.isFinite(info.resolved) || info.resolved < 0 || info.resolved > 100) {
    throw new Error("Benchmark verification, model identity, or harness does not match the reviewed run");
  }
  return info.resolved;
}

function verifiedRun(run: ReviewedRun, metadata: string, results: string, score: number, now: Date): PublicRecord {
  const verdicts = Object.entries(object(JSON.parse(results)));
  if (verdicts.length !== benchmarkSize || verdicts.some(([id, value]) =>
    !candidateInstance.test(id) || typeof object(value).resolved !== "boolean")) {
    throw new Error("The official result does not provide 500 exact task outcomes");
  }
  const outcomes = Object.fromEntries(verdicts.map(([id, value]) => [id, object(value).resolved as boolean]));
  const resolved = Object.values(outcomes).filter(Boolean).length;
  if (Math.abs(score - 100 * resolved / benchmarkSize) > 0.000001) {
    throw new Error("Published score and per-task verdicts do not agree");
  }
  return {
    schema: 1, run: run.id, modelKey: run.key, modelVersion: run.modelVersion,
    modelSource: run.modelSource, validUntil: run.validUntil ?? null, benchmark: benchmarkName,
    harness: "mini-SWE-agent", harnessVersion: "1.0.0", evaluationDate: evaluationDate(run), attempts: 1,
    reasoning: "unreported", sourceUrl: sourceUrl(run), revision: sourceRevision,
    metadataHash: sha256(metadata), resultsHash: sha256(results), instancesHash: instancesHash(outcomes),
    resolved, outcomes, cachedAt: now.toISOString(),
  };
}

export function importRun(run: ReviewedRun, metadata: string, results: string, now = new Date()): PublicRecord {
  return verifiedRun(run, metadata, results, reviewedScore(run, metadata), now);
}

export async function updateBenchmarks(
  available: readonly string[], directory = cacheDirectory(), readSource: ReadSource = fetchSource, now = new Date(),
) {
  const models = [...new Set(available.filter(isExactCatalogKey))].sort();
  const current = await readPublicCache(directory);
  const errors: CacheUpdate["errors"] = [];
  let imported = 0;
  let reused = 0;
  // The pinned source is content-addressed. Reuse verified files from earlier updates
  // and fetch outcomes only for exact models that have appeared since then.
  await Promise.all(reviewedRuns.filter(run => models.includes(run.key)).map(async run => {
    if (run.validUntil && Date.parse(run.validUntil) <= now.getTime()) return;
    if (current.records.some(record => record.run === run.id)) {
      reused++;
      return;
    }
    try {
      const metadata = await readSource(rawUrl(run, "metadata.yaml"), 16_384);
      // Reject malformed or unverified metadata before downloading outcomes.
      const score = reviewedScore(run, metadata);
      const results = await readSource(rawUrl(run, "per_instance_details.json"), 262_144);
      await atomicCacheWrite(directory, run.id + ".json", verifiedRun(run, metadata, results, score, now));
      imported++;
    } catch (error) {
      errors.push({ key: run.key, reason: error instanceof Error ? error.message : "Public source import failed" });
    }
  }));
  const update: CacheUpdate = { schema: 1, updatedAt: now.toISOString(), available: models,
    errors: errors.sort((a, b) => a.key.localeCompare(b.key)) };
  await atomicCacheWrite(directory, "update.json", update);
  return { cache: await readPublicCache(directory), imported, reused };
}

export function importStatus(cache: PublicCache, now = new Date()) {
  const available = cache.update?.available ?? [];
  const records = cache.records.filter(record => activeRecord(record, now));
  const matched = available.flatMap(key => records.filter(record => record.modelKey === key)
    .map(record => ({
      key, modelVersion: record.modelVersion, benchmark: record.benchmark, score: record.resolved / benchmarkSize,
      resolved: record.resolved, tasks: benchmarkSize, evaluationDate: record.evaluationDate, source: record.sourceUrl,
    })));
  const unmatched = available.filter(key => !matched.some(record => record.key === key)).map(key => ({
    key, reason: cache.update?.errors.find(error => error.key === key)?.reason
      ?? (reviewedRuns.some(run => run.key === key && run.validUntil && Date.parse(run.validUntil) <= now.getTime())
        ? "The provider's documented validity window has ended."
        : knownExclusions[key] ?? "No reviewed result for this exact OpenCode model ID."),
  }));
  return { lastUpdate: cache.update?.updatedAt ?? null, cachedRecords: cache.records.length,
    activeRecords: records.length, available: available.length, matched, unmatched, errors: cache.update?.errors ?? [] };
}

