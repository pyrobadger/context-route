import type { ContextPack } from "../context/context-builder.js";
import type { Candidate, Decision, RoutingInput } from "./types.js";
import { activeRecord, benchmarkSize, type PublicCache, type PublicRecord } from "./public-cache.js";

export const publicLimitations = "Scores describe the named model with mini-SWE-agent on SWE-bench Verified. They do not measure OpenCode, this repository, or performance at a ContextRoute context tier.";

type Score = {
  key: string;
  modelVersion: string;
  resolved: number;
  tasks: number;
  sourceUrl: string;
};

export interface PublicBenchmarkEvidence {
  benchmark: string;
  harness: string;
  evaluationDate: string;
  original: Score | null;
  reference?: Score;
  selected: Score;
  improvements: number;
  regressions: number;
  contextReason: string;
  limitations: string;
}

export type HierarchyDecision = Decision | (Omit<Decision, "decision_type" | "fallback_reason"> & {
  decision_type: "public_benchmark_informed";
  fallback_reason?: never;
  publicBenchmark: PublicBenchmarkEvidence;
});

const tiers = new Set(["C1", "C2", "C3"]);

function cohort(a: PublicRecord) {
  return JSON.stringify([a.benchmark, a.harness, a.harnessVersion, a.attempts, a.reasoning, a.evaluationDate, a.instancesHash]);
}

export function pairedComparison(original: PublicRecord, alternative: PublicRecord) {
  if (cohort(original) !== cohort(alternative)) return null;
  let improvements = 0;
  let regressions = 0;
  for (const [id, resolved] of Object.entries(original.outcomes)) {
    const other = alternative.outcomes[id];
    if (typeof other !== "boolean") return null;
    if (resolved && !other) regressions++;
    if (!resolved && other) improvements++;
  }
  const changed = improvements + regressions;
  // An exact two-sided sign test on the discordant paired verdicts.
  const lower = Math.min(improvements, regressions);
  let term = Math.pow(0.5, changed);
  let tail = term;
  for (let i = 1; i <= lower; i++) {
    term *= (changed - i + 1) / i;
    tail += term;
  }
  return { improvements, regressions, p: Math.min(1, 2 * tail) };
}

function namedFiles(pack: ContextPack, paths: readonly string[]) {
  const files = new Set(pack.files.map(file => file.path.replaceAll("\\", "/").toLowerCase()));
  // Dedupe repeated matches and count each path only once.
  return [...new Set(paths)].filter(path => [...files].some(file => file === path || file.endsWith("/" + path))).length;
}

function localPack(input: RoutingInput, candidates: Candidate[], model: string) {
  const choices = candidates.filter(pair => pair.model === model && pair.constraints_met && tiers.has(pair.context)
    && pair.context_tokens > 0).flatMap(pair => {
    const pack = input.packs.find(item => item.context_id === pair.context);
    if (!pack) return [];
    const named = namedFiles(pack, input.task.explicitPaths);
    const related = new Set(pack.files.filter(file => file.reason !== "lexical").map(file => file.path)).size;
    const lexical = new Set(pack.files.filter(file => file.retrieval_score > 0).map(file => file.path)).size;
    return [{ pair, named, related, lexical }];
  });
  if (!choices.length) return null;
  const coverage = Math.max(...choices.map(choice => choice.named));
  const best = choices.filter(choice => choice.named === coverage).sort((a, b) => a.pair.context.localeCompare(b.pair.context));
  const relevant = best.filter(choice => choice.named > 0 || choice.lexical > 0 || choice.related > 0);
  const pool = relevant.length ? relevant : best;
  const broad = input.task.crossModule || input.task.nearbyTests;
  const picked = (broad ? pool.find(choice => choice.related > 0) : undefined)
    ?? (relevant.length ? pool[0] : pool.find(choice => choice.pair.context === (input.fallbackContext ?? "C2"))) ?? pool[0]!;
  return {
    pair: picked.pair,
    explanation: picked.pair.context + " covers " + picked.named + " named paths, " + picked.lexical
      + " lexical matches and " + picked.related + " dependency, caller or test files within its context budget.",
  };
}

const conciseScore = (record: PublicRecord): Score => ({
  key: record.modelKey, modelVersion: record.modelVersion, resolved: record.resolved,
  tasks: benchmarkSize, sourceUrl: record.sourceUrl,
});

export function refineWithPublicBenchmark(
  input: RoutingInput, local: Decision, cache: PublicCache, now = new Date(),
): HierarchyDecision {
  if (local.decision_type === "empirical" || !localPack(input, local.alternatives, input.original)) return local;
  const evidence = cache.records.filter(record => activeRecord(record, now));
  const originals = evidence.filter(record => record.modelKey === input.original);

  const alternatives = evidence.filter(record => record.modelKey !== input.original
    && input.models.some(model => model.enabled && model.key === record.modelKey))
    .flatMap(record => {
      const pack = localPack(input, local.alternatives, record.modelKey);
      return pack ? [{ record, pack }] : [];
    });
  if (!originals.length) {
    const current = input.models.find(model => model.key === input.original);
    // With no measured baseline, avoid escalating price on the strength of a comparison among other models.
    const eligible = alternatives.filter(({ record }) => {
      const model = input.models.find(model => model.key === record.modelKey);
      return current?.pricing.length && model?.pricing.length && model.pricing.every(rate =>
        current.pricing.every(base => rate.input <= base.input && rate.output <= base.output
          && rate.cache_read <= base.cache_read && rate.cache_write <= base.cache_write));
    });
    const count = new Set(eligible.map(({ record }) => record.modelKey)).size;
    const alpha = 0.05 / Math.max(1, eligible.length * (eligible.length - 1) / 2);
    if (count >= 2 && count === eligible.length) {
      for (const candidate of eligible) {
        const competitors = eligible.filter(other => other !== candidate).sort((a, b) => b.record.resolved - a.record.resolved
          || a.record.modelKey.localeCompare(b.record.modelKey));
        const results = competitors.map(other => pairedComparison(other.record, candidate.record));
        if (results.every(result => result && result.improvements > result.regressions && result.p < alpha)) {
          return publicDecision(local, candidate, competitors[0]!.record, results[0]!, false);
        }
      }
    }
    return {
      ...local,
      reasons: [...local.reasons, count < 2
        ? `Public benchmark check found no score for the current model and ${count} of the two comparable, price-eligible alternatives needed.`
        : "Public benchmark check found no score for the current model and no clear winner among the eligible alternatives."],
    };
  }
  const comparisons = alternatives.flatMap(({ record, pack }) => originals.flatMap(original => {
    const comparison = pairedComparison(original, record);
    return comparison ? [{ record, original, pack, comparison }] : [];
  }));
  const alpha = 0.05 / Math.max(1, comparisons.length);
  const supported = comparisons.filter(item => item.comparison.improvements > item.comparison.regressions && item.comparison.p < alpha
    && comparisons.filter(other => other.record.modelKey === item.record.modelKey)
      .every(other => other.comparison.improvements > other.comparison.regressions && other.comparison.p < alpha));
  // A winner must also beat every other supported option in the same cohort. Incompatible winning cohorts
  // or contradictory submissions leave the original model in place.
  const chosen = supported.find(item => supported.every(other => {
    if (other === item) return true;
    const comparison = pairedComparison(other.record, item.record);
    return comparison && comparison.improvements > comparison.regressions && comparison.p < alpha;
  }));
  if (!chosen) return { ...local, reasons: [...local.reasons,
    "Public benchmark check found no comparable alternative with a clear advantage over the current model."] };

  return publicDecision(local, chosen, chosen.original, chosen.comparison, true);
}

function publicDecision(local: Decision, { record, pack }: { record: PublicRecord; pack: NonNullable<ReturnType<typeof localPack>> },
  reference: PublicRecord, comparison: NonNullable<ReturnType<typeof pairedComparison>>, againstOriginal: boolean): HierarchyDecision {
  return {
    selected: pack.pair, policy_version: againstOriginal ? "public-benchmark-routing-v1" : "public-benchmark-pool-v1", decision_type: "public_benchmark_informed",
    alternatives: local.alternatives, reasons: [
      againstOriginal ? "The selected model won a comparable, paired SWE-bench Verified evaluation."
        : "The selected model led the eligible alternatives on paired SWE-bench Verified results. The current model has no matching score.",
      pack.explanation,
    ],
    publicBenchmark: {
      benchmark: record.benchmark, harness: record.harness + " " + record.harnessVersion,
      evaluationDate: record.evaluationDate, original: againstOriginal ? conciseScore(reference) : null,
      ...(!againstOriginal ? { reference: conciseScore(reference) } : {}), selected: conciseScore(record),
      improvements: comparison.improvements, regressions: comparison.regressions,
      contextReason: pack.explanation, limitations: publicLimitations,
    },
  };
}

