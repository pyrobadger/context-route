import { createHash } from "node:crypto";
import { access, mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = new URL("../", import.meta.url);
const providers = {
  default: { provider: "route-demo-default", model: "default", name: "Demo Default" },
  routed: { provider: "route-demo-routed", model: "routed", name: "Demo Routed" },
};
const key = ({ provider, model }) => provider + "/" + model;
export const demoOriginal = key(providers.default);
export const demoSelected = key(providers.routed);
export const demoPrompt = "Review inventory.ts for a stock calculation bug and explain the logic in one sentence without editing files.";

const digest = (text) => createHash("sha256").update(text, "utf8").digest("hex");
const contextBlock = /<contextroute_repository>[\s\S]*?<\/contextroute_repository>/;
const zero = { input: 0, output: 0, cache: { read: 0, write: 0 } };

function contextHash(value) {
  if (typeof value === "string") {
    const pack = value.match(contextBlock)?.[0];
    return pack ? digest(pack) : null;
  }
  if (value && typeof value === "object") {
    for (const child of Object.values(value)) {
      const found = contextHash(child);
      if (found) return found;
    }
  }
  return null;
}

function loopbackProvider(receive) {
  return createServer(async (request, response) => {
    const endpoint = /^\/(default|routed)\/v1\/chat\/completions$/.exec(request.url ?? "")?.[1];
    if (request.method !== "POST" || !endpoint) {
      response.writeHead(404).end();
      return;
    }
    let size = 0;
    const chunks = [];
    try {
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 2_000_000) {
          response.writeHead(413).end();
          return;
        }
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      if (body.model !== providers[endpoint].model) {
        response.writeHead(400).end();
        return;
      }
      const hash = contextHash(body.messages);
      receive({ model: key(providers[endpoint]), contextHash: hash });
      const frame = (choices, usage) => JSON.stringify({
        id: "contextroute-loopback", object: "chat.completion.chunk", model: body.model, choices, usage,
      });
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      const content = hash ? "Available stock is the counted quantity minus the number of reserved units." : "Inventory routing demo";
      response.write("data: " + frame([{ index: 0, delta: { role: "assistant", content }, finish_reason: null }]) + "\n\n");
      response.write("data: " + frame([{ index: 0, delta: {}, finish_reason: "stop" }]) + "\n\n");
      response.end("data: " + frame([], { prompt_tokens: 40, completion_tokens: 13, total_tokens: 53 }) + "\n\ndata: [DONE]\n\n");
    } catch {
      response.writeHead(400).end();
    }
  });
}

function providerConfig(entry, endpoint, port) {
  return {
    name: entry.name,
    package: "@opencode/ai/providers/openai-compatible",
    settings: { baseURL: "http://127.0.0.1:" + port + "/" + endpoint + "/v1", apiKey: "context-route-demo" },
    models: {
      [entry.model]: {
        name: entry.name, cost: [zero], limit: { context: 32_000, output: 4_096 },
      },
    },
  };
}

async function prepareWorkspace(workspace, port, withEvidence, retrievalVersion) {
  const metadata = join(workspace, ".contextroute");
  await mkdir(metadata, { recursive: true });
  const evidence = join(metadata, "sample-evidence.json");
  const rows = withEvidence ? Array.from({ length: 6 }, (_, index) => ({
    execution_id: "fixture-execution-" + index, task_id: "fixture-task-" + index,
    task_category: "bug", repository_id: "context-route-demo", repository_commit: "synthetic",
    model_id: demoSelected, context_id: "C1", context_hash: digest("synthetic-context-" + index),
    retrieval_version: retrievalVersion, resolved: true, verification_type: "task_tests",
    input_tokens: 100, output_tokens: 20, total_tokens: 120, latency_seconds: 1,
    estimated_list_price_usd: 0, execution_status: "completed",
  })) : [];
  await Promise.all([
    writeFile(evidence, JSON.stringify(rows), "utf8"),
    writeFile(join(workspace, "inventory.ts"), "import { reserve } from './ledger.js';\nexport function stock(counted: number) { return counted - reserve; }\n", "utf8"),
    writeFile(join(workspace, "ledger.ts"), "export const reserve = 4;\n", "utf8"),
    writeFile(join(workspace, "opencode.json"), JSON.stringify({
      model: demoOriginal,
      agents: { title: { model: demoOriginal } },
      plugins: [{ package: root.href.replace(/\/$/, ""), options: {
        mode: "auto", models: [demoOriginal, demoSelected], evidence, demo: "loopback_fixture",
      } }],
      policies: [
        { action: "provider.use", resource: "*", effect: "deny" },
        ...Object.values(providers).map(({ provider }) => ({ action: "provider.use", resource: provider, effect: "allow" })),
        { action: "websearch.use", resource: "*", effect: "deny" },
      ],
      permissions: ["edit", "shell", "subagent", "external_directory"].map(action => ({ action, resource: "*", effect: "deny" })),
      providers: Object.fromEntries(Object.entries(providers).map(([endpoint, entry]) => [entry.provider, providerConfig(entry, endpoint, port)])),
    }, null, 2) + "\n", "utf8"),
  ]);
}

export async function startDesktopDemo(receive = () => {}) {
  await access(new URL("dist/gui-experimental.cjs", root)).catch(() => {
    throw new Error("Build ContextRoute first with npm run build.");
  });
  const { retrievalVersion } = await import(new URL("dist/context/context-builder.js", root));
  const directory = await mkdtemp(join(tmpdir(), "contextroute-demo-"));
  const fallback = join(directory, "1-retain-model");
  const switched = join(directory, "2-switch-model");
  const server = loopbackProvider(receive);
  await new Promise((ready, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", ready);
  });
  const stop = () => new Promise(done => {
    server.close(done);
    server.closeAllConnections();
  });
  try {
    const { port } = server.address();
    await Promise.all([
      prepareWorkspace(fallback, port, false, retrievalVersion),
      prepareWorkspace(switched, port, true, retrievalVersion),
    ]);
    return { directory, fallback, switched, stop };
  } catch (error) {
    await stop();
    throw error;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const demo = await startDesktopDemo(receipt => {
      if (receipt.contextHash) console.log("Loopback received " + receipt.model + " · context " + receipt.contextHash.slice(0, 12));
    });
    console.log("ContextRoute Desktop demo · local model responses and sample evidence");
    console.log("Open these folders in Desktop, with a fresh session starting on Demo Default.");
    console.log("Retain the model     " + demo.fallback);
    console.log("Verify a switch      " + demo.switched);
    console.log("\nPrompt for both:\n" + demoPrompt + "\n");
    console.log("Keep this terminal open. Ctrl+C stops both local endpoints.");
    process.once("SIGINT", () => void demo.stop());
    process.once("SIGTERM", () => void demo.stop());
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}

