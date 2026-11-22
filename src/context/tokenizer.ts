// Provider-independent approximation. Used only for initial context budgeting.
export function estimateTokens(text: string): number {
  const pieces = text.match(/[\p{L}\p{N}_]+|[^\p{L}\p{N}_\s]/gu)?.length ?? 0;
  return Math.max(pieces, Math.ceil(Buffer.byteLength(text, "utf8") / 4));
}

