import { estimateTokens } from "./tokenizer.js";
const categories = [
    ["performance", /\b(performance|slow|latency|optimi[sz]e|bottleneck)\b/i],
    ["docs", /\b(document|documentation|readme|docstrings?)\b/i],
    ["refactor", /\b(refactor|extract|reorganize|rename|simplify)\b/i],
    ["bug", /\b(fix|bug|incorrect|wrong|regression|crash|traceback|fails|broken|error)\b/i],
    ["test", /\b(tests?|coverage|test cases?)\b/i],
    ["feature", /\b(add|implement|support|introduce|allow|feature)\b/i],
];
export const sourcePathPattern = /\b(?:[\w.-]+[/\\])*[\w-]+\.(?:py|[cm]?tsx?|[cm]?jsx?|md|rst|go|rs|java|rb|sql|html|css)\b/gi;
export function analyzeTask(prompt, relevantFiles, nearbyTests) {
    const explicitPaths = [...new Set(prompt.match(sourcePathPattern) ?? [])]
        .map(path => path.replaceAll("\\", "/").toLowerCase());
    return {
        tokens: estimateTokens(prompt),
        category: categories.find(([, pattern]) => pattern.test(prompt))?.[0] ?? "other",
        explicitPaths,
        languageIndicators: [...new Set(prompt.match(/\b(?:python|javascript|typescript|rust|golang|java|ruby|sql)\b/gi)?.map(name => name.toLowerCase()) ?? [])],
        stackTrace: /Traceback \(most recent call last\)|^\s+at .+?:\d+(?::\d+)?\)?\s*$/m.test(prompt),
        potentiallyRelevantFiles: relevantFiles,
        crossModule: explicitPaths.length > 1 || relevantFiles >= 3 || /\b(across|cross-module|callers|several modules)\b/i.test(prompt),
        nearbyTests,
    };
}
