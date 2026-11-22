import { importRun } from "../../dist/router/public-import.js";

// Fictional paired outcomes for transport and policy tests only. They are never imported by the CLI.
export const fixtureDate = new Date("2026-11-22T12:00:00Z");

export function submission(run, resolved, { checked = true, shift = 0 } = {}) {
  const scores = Object.fromEntries(Array.from({ length: 500 }, (_, i) => [
    "fixture__repo-" + (i + 1 + shift), { resolved: i < resolved },
  ]));
  return {
    metadata: [
      "info:", "  mini-swe-agent_version: 1.0.0", "  resolved: " + resolved / 5,
      "tags:", "  agent: mini-SWE-agent", "  agent_org: SWE-agent", "  os_system: true",
      "  checked: " + checked, "  model: [" + run.label + "]", "  system: {attempts: 1}",
    ].join("\n"),
    results: JSON.stringify(scores),
  };
}

export function fixtureRecord(run, resolved, options) {
  const files = submission(run, resolved, options);
  return importRun(run, files.metadata, files.results, fixtureDate);
}

