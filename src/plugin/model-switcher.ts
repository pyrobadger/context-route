import type { Plugin } from "@opencode/plugin";
import { modelKey } from "../router/model-registry.js";

type SessionID = Parameters<Plugin.Context["session"]["get"]>[0]["sessionID"];

export async function applyModel(ctx: Plugin.Context, sessionID: SessionID, selected: string,
  catalog: Awaited<ReturnType<Plugin.Context["model"]["list"]>>["data"]) {
  const model = catalog.find(item => modelKey(item) === selected.split("#")[0]);
  if (!model) throw new Error("Selected model is absent from the current model catalog.");
  const variant = model.variants.find(item => item.id === selected.split("#")[1]);
  if (selected.includes("#") && !variant) throw new Error("Selected model variant is unavailable.");
  await ctx.session.switchModel({ sessionID, model: { providerID: model.providerID, id: model.id, variant: variant?.id } });
  const confirmed = (await ctx.session.get({ sessionID })).model;
  if (!confirmed || modelKey(confirmed) !== selected) throw new Error("OpenCode did not retain the requested session model.");
}

