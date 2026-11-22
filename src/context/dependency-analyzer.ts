import { posix } from "node:path";
import type { SourceFile } from "./repository-scanner.js";

function jsImports(file: SourceFile): string[] {
  const source = file.lines.join("\n");
  const imports = source.matchAll(/(?:^\s*(?:import|export)\s+(?:[\w\s,*{}]+?\s+from\s+)?|\b(?:require|import)\(\s*)["'](\.[^"'\r\n]+)["']/gm);
  return [...imports].flatMap(match => {
    const base = posix.normalize(posix.join(posix.dirname(file.path), match[1]!));
    const stem = base.replace(/\.[cm]?jsx?$/, "");
    return [base, ...["ts", "tsx", "mts", "cts", "js", "jsx", "mjs", "cjs"].flatMap(ext => [stem + "." + ext, base + "/index." + ext])];
  });
}

function pythonImports(file: SourceFile): string[] {
  const candidates: string[] = [];
  const add = (module: string) => {
    if (module && !module.startsWith("../")) {
      for (const prefix of ["", "src/"]) candidates.push(prefix + module + ".py", prefix + module + "/__init__.py");
    }
  };
  for (const line of file.lines) {
    const from = /^\s*from\s+([.\w]+)\s+import\s+(.*)/.exec(line);
    const direct = /^\s*import\s+([^#;]+)/.exec(line);
    if (from) {
      const target = from[1]!;
      const dots = target.match(/^\.+/)?.[0].length ?? 0;
      let folder = posix.dirname(file.path);
      for (let parent = 1; parent < dots; parent++) folder = posix.dirname(folder);
      const module = posix.normalize(posix.join(dots ? folder : "", target.slice(dots).replaceAll(".", "/")));
      add(module);
      for (const member of from[2]!.replace(/[()]/g, "").split(",")) {
        const name = /^\s*(\w+)/.exec(member)?.[1];
        if (name) add(posix.join(module, name));
      }
    } else if (direct) {
      for (const member of direct[1]!.split(",")) {
        const name = /^\s*([\w.]+)/.exec(member)?.[1];
        if (name) add(name.replaceAll(".", "/"));
      }
    }
  }
  return candidates;
}

export interface Relationships {
  imports: Map<string, string[]>;
  callers: Map<string, string[]>;
  nearbyTests: Map<string, string[]>;
}

function testStem(path: string): string {
  return posix.basename(path).replace(/\.[^.]+$/, "").replace(/^test[_-]|[._-](?:test|spec)$/gi, "");
}

export function analyzeDependencies(files: SourceFile[]): Relationships {
  const byPath = new Map(files.map(file => [file.path, file]));
  const testsByStem = new Map<string, string[]>();
  for (const file of files.filter(file => file.isTest)) {
    const stem = testStem(file.path);
    testsByStem.set(stem, [...(testsByStem.get(stem) ?? []), file.path]);
  }
  const imports = new Map<string, string[]>();
  const callers = new Map<string, string[]>();
  const nearbyTests = new Map<string, string[]>();
  for (const file of files) {
    let candidates: string[] = [];
    if (file.language === "Python") candidates = pythonImports(file);
    if (file.language === "TypeScript" || file.language === "JavaScript") candidates = jsImports(file);
    const found = [...new Set(candidates.filter(path => path !== file.path && byPath.has(path)))].sort();
    imports.set(file.path, found);
    for (const path of found) callers.set(path, [...(callers.get(path) ?? []), file.path]);
  }
  for (const file of files) {
    const adjacent = [...(callers.get(file.path) ?? []), ...(imports.get(file.path) ?? []), ...(testsByStem.get(testStem(file.path)) ?? [])];
    nearbyTests.set(file.path, [...new Set(adjacent.filter(path => path !== file.path && byPath.get(path)?.isTest))].sort());
  }
  return { imports, callers, nearbyTests };
}

