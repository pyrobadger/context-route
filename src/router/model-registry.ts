import type { Plugin } from "@opencode/plugin";
import type { Model, Price } from "./types.js";

type CatalogModel = Awaited<ReturnType<Plugin.Context["model"]["list"]>>["data"][number];

export function modelKey(model: { providerID: string; id: string; variant?: string }): string {
  return model.providerID + "/" + model.id + (model.variant && model.variant !== "default" ? "#" + model.variant : "");
}

export function discoverModels(catalog: readonly CatalogModel[], original?: { providerID: string; id: string; variant?: string }): Model[] {
  const models: Model[] = [];
  for (const item of catalog) {
    const base = {
      key: modelKey(item),
      provider_id: item.providerID,
      model_id: item.id,
      display_name: item.name,
      enabled: item.enabled && item.status !== "deprecated",
      supports_tools: item.capabilities.tools === true,
      supports_text: item.capabilities.input.includes("text") && item.capabilities.output.includes("text"),
      context_window: item.limit.context,
      input_limit: item.limit.input,
      output_limit: item.limit.output,
    };
    const pricing: Price[] = item.cost.map(tier => ({
      input: tier.input, output: tier.output, cache_read: tier.cache.read, cache_write: tier.cache.write,
      context_over: tier.tier?.size,
    })).filter(tier => [tier.input, tier.output, tier.cache_read, tier.cache_write]
      .every(value => Number.isFinite(value) && value >= 0));
    const model = { ...base, pricing, pricing_source: pricing.length ? "OpenCode active model catalog" : null,
      pricing_read_at: pricing.length ? new Date().toISOString() : null };
    models.push(model);
    for (const variant of item.variants) {
      if (variant.id !== "default") models.push({ ...model, key: modelKey({ ...item, variant: variant.id }), variant: variant.id });
    }
    if (original?.id === item.id && original.providerID === item.providerID && original.variant && original.variant !== "default"
      && !item.variants.some(variant => variant.id === original.variant)) {
      models.push({ ...model, key: modelKey(original), variant: original.variant });
    }
  }
  return models;
}

export function capabilityExclusions(model: Model, tokens: number, outputReserve: number): string[] {
  const reasons: string[] = [];
  if (!model.enabled) reasons.push("Model is disabled or deprecated.");
  if (!model.supports_tools || !model.supports_text) reasons.push("The catalog does not advertise text and tool support.");
  if (!(model.context_window > 0)) reasons.push("The model's context window is unknown.");
  if (tokens + outputReserve > model.context_window || (model.input_limit !== undefined && tokens > model.input_limit)) {
    reasons.push("Prompt, context, and reserves exceed a catalog limit.");
  }
  if (model.output_limit !== undefined && model.output_limit > 0 && outputReserve > model.output_limit) {
    reasons.push("The output reserve exceeds the model's output limit.");
  }
  return reasons;
}

