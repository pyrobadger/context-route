export function inputTokens(usage) {
    return usage.input + usage.cache.read + usage.cache.write;
}
export function outputTokens(usage) {
    return usage.output + usage.reasoning;
}
export function estimateCallCost(prices, usage) {
    const input = inputTokens(usage);
    const tier = [...prices].sort((a, b) => (b.context_over ?? -1) - (a.context_over ?? -1))
        .find(price => price.context_over === undefined || input > price.context_over);
    if (!tier)
        return null;
    return (usage.input * tier.input + outputTokens(usage) * tier.output
        + usage.cache.read * tier.cache_read + usage.cache.write * tier.cache_write) / 1_000_000;
}
export function mean(values) {
    return values.length && values.every(value => value !== null)
        ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}
