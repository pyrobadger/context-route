import type { SourceFile } from "./repository-scanner.js";
import { sourcePathPattern } from "./task-analysis.js";

const stopWords = new Set("a add an and are as at be bug can code file fix for from how i implement in is it of on or please should task that the their then there this to use was we when where which will with you".split(" "));

export function terms(text: string): string[] {
  return (text.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [])
    .filter(term => term.length > 1 && !stopWords.has(term));
}

export interface Span {
  path: string;
  start_line: number;
  end_line: number;
  retrieval_score: number;
  lines: string[];
  reason: "lexical" | "dependency" | "caller" | "test";
}

export function rankSpans(files: SourceFile[], prompt: string, paths: string[]): Span[] {
  const query = new Set(terms(prompt.replace(sourcePathPattern, path => path.split(/[/\\]/).at(-1)!.replace(/\.[^.]+$/, ""))));
  const docs = files.flatMap(file => {
    const chunks = [];
    for (let cursor = 0; cursor < file.lines.length; cursor += 18) {
      const lines = file.lines.slice(cursor, cursor + 18);
      if (lines.every(line => !line.trim())) continue;
      const words = terms(file.path + " " + lines.join("\n"));
      const hits = new Map<string, number>();
      for (const word of words) if (query.has(word)) hits.set(word, (hits.get(word) ?? 0) + 1);
      chunks.push({ path: file.path, start_line: cursor + 1, end_line: cursor + lines.length, lines, length: words.length, hits });
    }
    return chunks;
  });
  const df = new Map<string, number>();
  for (const doc of docs) for (const term of doc.hits.keys()) df.set(term, (df.get(term) ?? 0) + 1);
  const average = docs.reduce((sum, doc) => sum + doc.length, 0) / docs.length || 1;
  return docs.map(({ hits, length, ...span }): Span => {
    let score = 0;
    for (const [term, count] of hits) {
      const frequency = df.get(term)!;
      const idf = Math.log(1 + (docs.length - frequency + 0.5) / (frequency + 0.5));
      score += idf * (count * 2.2) / (count + 1.2 * (0.25 + 0.75 * length / average));
    }
    const path = span.path.toLowerCase();
    if (paths.some(explicit => path === explicit || path.endsWith("/" + explicit))) score += 12;
    return { ...span, retrieval_score: Number(score.toFixed(6)), reason: "lexical" };
  }).sort((a, b) => b.retrieval_score - a.retrieval_score || (a.path < b.path ? -1 : a.path > b.path ? 1 : a.start_line - b.start_line));
}

