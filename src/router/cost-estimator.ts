import type { Price, TokenUsage } from "./types.js";

export function inputTokens(usage: TokenUsage): number {
  return usage.input + usage.cache.read + usage.cache.write;
}

export function outputTokens(usage: TokenUsage): number {
  return usage.output + usage.reasoning;
}

export function estimateCallCost(prices: Price[], usage: TokenUsage): number | null {
  const input = inputTokens(usage);
  const tier = [...prices].sort((a, b) => (b.context_over ?? -1) - (a.context_over ?? -1))
    .find(price => price.context_over === undefined || input > price.context_over);
  if (!tier) return null;
  return (usage.input * tier.input + outputTokens(usage) * tier.output
    + usage.cache.read * tier.cache_read + usage.cache.write * tier.cache_write) / 1_000_000;
}

export function mean(values: (number | null)[]): number | null {
  return values.length && values.every(value => value !== null)
    ? values.reduce<number>((sum, value) => sum + value!, 0) / values.length : null;
}

