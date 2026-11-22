import { inputTokens, outputTokens } from "../router/cost-estimator.js";
import type { TokenUsage } from "../router/types.js";

export function usageMetrics(tokens: TokenUsage | undefined) {
  if (!tokens) return { input_tokens: null, output_tokens: null, total_tokens: null, cache_read_tokens: null, cache_write_tokens: null };
  const input = inputTokens(tokens);
  const output = outputTokens(tokens);
  return {
    input_tokens: input,
    output_tokens: output,
    total_tokens: input + output,
    cache_read_tokens: tokens.cache.read,
    cache_write_tokens: tokens.cache.write,
  };
}

export class TaskMetrics {
  private readonly steps = new Map<string, { tokens: TokenUsage; cost: number | null }>();
  private readonly tools = new Set<string>();
  primaryRequests = 0;
  auxiliaryRequests = 0;
  finish: string | null = null;

  step(id: string, tokens: TokenUsage, finish: string, cost: number | null = null) {
    if (this.steps.has(id)) return;
    this.steps.set(id, { tokens, cost });
    this.finish = finish;
  }

  tool(id: string) {
    this.tools.add(id);
  }

  get hasStep() {
    return this.steps.size > 0;
  }

  summary() {
    const total: TokenUsage = { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } };
    let cost: number | null = this.steps.size ? 0 : null;
    for (const { tokens: usage, cost: stepCost } of this.steps.values()) {
      total.input += usage.input;
      total.output += usage.output;
      total.reasoning += usage.reasoning;
      total.cache.read += usage.cache.read;
      total.cache.write += usage.cache.write;
      cost = cost === null || stepCost === null ? null : cost + stepCost;
    }
    return {
      ...usageMetrics(this.steps.size ? total : undefined),
      primary_model_calls: this.primaryRequests,
      auxiliary_model_calls: this.auxiliaryRequests,
      total_model_calls: this.primaryRequests + this.auxiliaryRequests,
      tool_calls: this.tools.size,
      estimated_list_price_usd: cost,
      token_scope: "completed primary steps in this session",
      final_model_finish: this.finish,
    };
  }
}

