import { readFile, readdir, stat } from "node:fs/promises";
import { join } from "node:path";
import { contextIds } from "../context/context-builder.js";
export function isObservation(value) {
    if (!value || typeof value !== "object")
        return false;
    const row = value;
    const textKeys = ["execution_id", "task_id", "repository_id", "repository_commit", "model_id", "context_hash", "retrieval_version"];
    const measurements = ["input_tokens", "output_tokens", "total_tokens", "latency_seconds", "estimated_list_price_usd"];
    return textKeys.every(key => typeof row[key] === "string" && row[key].length > 0)
        && /^[a-f0-9]{64}$/.test(String(row.context_hash))
        && typeof row.resolved === "boolean"
        && contextIds.some(id => id === row.context_id)
        && ["bug", "feature", "refactor", "test", "docs", "performance", "other"].includes(String(row.task_category))
        && ["task_tests", "issue_verified", "patch_applied", "execution_only"].includes(String(row.verification_type))
        && ["completed", "failed", "timeout", "invalid"].includes(String(row.execution_status))
        && measurements.every(key => row[key] === null || (typeof row[key] === "number" && Number.isFinite(row[key]) && row[key] >= 0));
}
export function uniqueEvidence(rows) {
    const found = new Map();
    for (const row of rows)
        if (isObservation(row) && !found.has(row.execution_id))
            found.set(row.execution_id, row);
    return [...found.values()];
}
function records(contents) {
    try {
        const data = JSON.parse(contents);
        return Array.isArray(data) ? data : [data];
    }
    catch {
        return contents.split(/\r?\n/).filter(line => line.trim()).map(line => JSON.parse(line));
    }
}
export async function loadEvidence(path) {
    const info = await stat(path).catch(error => {
        if (error.code === "ENOENT")
            return null;
        throw error;
    });
    if (!info)
        return [];
    if (info.isFile())
        return uniqueEvidence(records(await readFile(path, "utf8")));
    const files = (await readdir(path, { withFileTypes: true }))
        .filter(file => file.isFile() && file.name.endsWith(".json")).map(file => file.name).sort();
    const rows = [];
    for (const file of files)
        rows.push(...records(await readFile(join(path, file), "utf8")));
    return uniqueEvidence(rows);
}
export function pairEvidence(evidence, model, tier, version) {
    const tasks = new Map();
    for (const row of evidence) {
        if (row.model_id !== model || row.context_id !== tier || row.retrieval_version !== version
            || row.execution_status !== "completed" || !["task_tests", "issue_verified"].includes(row.verification_type))
            continue;
        const key = JSON.stringify([row.repository_id, row.task_id]);
        const existing = tasks.get(key);
        if (!existing || row.execution_id < existing.execution_id)
            tasks.set(key, row);
    }
    return [...tasks.values()];
}
