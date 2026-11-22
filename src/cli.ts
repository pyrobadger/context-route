#!/usr/bin/env node
import { execFile } from "node:child_process";
import { access } from "node:fs/promises";
import { delimiter, join } from "node:path";
import { promisify } from "node:util";
import { cacheDirectory, isExactCatalogKey, readPublicCache } from "./router/public-cache.js";
import { importStatus, updateBenchmarks } from "./router/public-import.js";

async function opencodeBinary() {
  if (process.env.CONTEXTROUTE_OPENCODE) return process.env.CONTEXTROUTE_OPENCODE;
  const path = (process.env.PATH ?? "").split(delimiter);
  for (const directory of path.filter(Boolean)) {
    const candidates = process.platform === "win32"
      ? [join(directory, "opencode.exe"), join(directory, "node_modules", "@opencode", "cli", "bin", "opencode.exe")]
      : [join(directory, "opencode")];
    for (const candidate of candidates) {
      try {
        await access(candidate);
        return candidate;
      } catch {
        // Try the next native executable on PATH.
      }
    }
  }
  throw new Error("OpenCode executable not found. Set CONTEXTROUTE_OPENCODE to the native binary.");
}

async function discoverCatalog() {
  const { stdout } = await promisify(execFile)(await opencodeBinary(), ["models"], {
    encoding: "utf8", timeout: 45_000, windowsHide: true, maxBuffer: 1024 * 1024,
  }).catch(() => { throw new Error("OpenCode could not list available models."); });
  return [...new Set(stdout.split(/\r?\n/).map(line => line.trim()).filter(isExactCatalogKey))];
}

async function main() {
  const [group, action, ...options] = process.argv.slice(2);
  if (group !== "benchmarks" || (action !== "update" && action !== "status")
    || options.some(option => option !== "--json")) {
    console.error("Usage: contextroute benchmarks {update|status} [--json]");
    process.exitCode = 2;
    return;
  }
  const directory = cacheDirectory();
  const updated = action === "update" ? await updateBenchmarks(await discoverCatalog(), directory) : null;
  const status = importStatus(updated?.cache ?? await readPublicCache(directory));
  if (options.includes("--json")) {
    console.log(JSON.stringify({ directory, ...status, ...updated && { imported: updated.imported, reused: updated.reused } }, null, 2));
  } else {
    console.log(status.matched.length + " of " + status.available + " OpenCode model IDs matched; "
      + status.cachedRecords + " verified records cached in " + directory);
    console.log("Last update: " + (status.lastUpdate ?? "never"));
    if (updated) console.log(updated.imported + " imported, " + updated.reused + " reused");
    for (const item of status.matched) {
      console.log(item.key + "  " + (100 * item.score).toFixed(1) + "% (" + item.resolved + "/" + item.tasks + ")  "
        + item.benchmark + "  " + item.evaluationDate + "\n  " + item.source);
    }
    for (const item of status.unmatched) console.log(item.key + "  " + item.reason);
  }
  if (updated && status.errors.length) process.exitCode = 1;
}

void main().catch(error => {
  console.error(error instanceof Error ? error.message : "ContextRoute benchmark command failed.");
  process.exitCode = 1;
});

