import type { Plugin } from "@opencode/plugin";
import { registerHooks } from "./hooks.js";

export default {
  id: "contextroute",
  setup: ctx => ctx.options.desktop_only === true ? undefined : registerHooks(ctx),
} satisfies Plugin.Plugin;

