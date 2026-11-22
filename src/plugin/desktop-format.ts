export const routingSourceLabels: Record<string, string> = {
  empirical: "Empirical joint routing",
  public_benchmark_informed: "Public benchmark informed",
  compact_context: "Compact context",
  cold_start_fallback: "Cold-start fallback",
  forced: "Forced experimental configuration",
  baseline: "Fixed baseline",
  no_feasible_pair: "No feasible configuration",
  unknown: "Routing decision",
};

export function formatContextTokens(actual: number | null, budget: number | null, locale?: string) {
  const formatted = actual === null ? "—" : actual.toLocaleString(locale);
  return budget === null ? formatted : formatted + " / " + budget.toLocaleString(locale);
}

export function formatContextReduction(compact: number, reference: number, locale?: string) {
  const difference = reference - compact;
  if (difference === 0) return "Same estimate";
  const count = Math.abs(difference).toLocaleString(locale);
  const direction = difference > 0 ? "fewer" : "more";
  if (reference === 0) return `${count} ${direction}`;
  const change = Math.abs(difference / reference).toLocaleString(locale, { style: "percent", maximumFractionDigits: 1 });
  return `${count} ${direction} (${change})`;
}
import type { RoutingState } from "./desktop-state.js";


export function modelDisposition(state: RoutingState) {
  const route = state.route;
  if (!route?.original || !route.applied) return "unknown";
  if (state.modelCheck === "mismatch") return "mismatch";
  if (route.confirmation) return "confirmation";
  if (route.original.id === route.applied.id) return "retained";
  return state.modelCheck === "verified" && state.actual === route.applied.id ? "switched" : "unverified";
}
