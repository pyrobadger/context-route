import { execFile } from "node:child_process";
import { lstat, readFile, readdir, realpath } from "node:fs/promises";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { promisify } from "node:util";
const git = promisify(execFile);
const extensions = new Map(Object.entries({
    py: "Python", ts: "TypeScript", tsx: "TypeScript", mts: "TypeScript", cts: "TypeScript",
    js: "JavaScript", jsx: "JavaScript", mjs: "JavaScript", cjs: "JavaScript",
    md: "Markdown", rst: "reStructuredText", html: "HTML", css: "CSS", sql: "SQL",
    go: "Go", java: "Java", rs: "Rust", rb: "Ruby", c: "C", h: "C", cpp: "C++",
}));
const blockedDirectory = /^(?:[.]|node_modules$|venv$|env$|dist$|build$|out$|target$|vendor$|coverage$|__pycache__$|generated$|assets$|secrets?$|credentials?$)/i;
const blockedName = /(?:^\.|(?:^|[._-])(?:secrets?|credentials?|passwords?|id_rsa|id_ed25519|keystore|private[-_]?keys?|env(?:ironment)?|generated)(?:[._-]|$)|\.min\.)/i;
const secretContents = /-----BEGIN [^-\r\n]*(?:PRIVATE KEY|CERTIFICATE)-----|\b(?:gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|sk-(?:proj-)?[a-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|AIza[a-z0-9_-]{35}|xox[baprs]-[a-z0-9-]{12,}|eyJ[a-z0-9_-]{8,}\.eyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,})\b|(?:api[_-]?key|aws_secret_access_key|secret(?:[_-]?key)?|pass(?:word|wd)|(?:access|auth|refresh)[_-]?token|client[_-]?secret)\s*["']?\s*[:=]\s*(?:["'][^"'\r\n]+["']|[A-Za-z0-9/+_.-]{12,})|[a-z][a-z0-9+.-]*:\/\/[^\s/:@]+:[^\s/@]+@/i;
const maxFileBytes = 96 * 1024;
const maxFiles = 4000;
const maxIndexBytes = 32 * 1024 * 1024;
function inside(root, path) {
    const name = relative(root, path);
    return name !== ".." && !name.startsWith(".." + sep) && !isAbsolute(name);
}
async function listPlainDirectory(root, prefix = "") {
    const paths = [];
    const entries = await readdir(join(root, prefix), { withFileTypes: true });
    for (const entry of entries.sort((a, b) => a.name < b.name ? -1 : Number(a.name !== b.name))) {
        const name = prefix ? prefix + "/" + entry.name : entry.name;
        if (entry.isDirectory() && !blockedDirectory.test(entry.name)) {
            paths.push(...await listPlainDirectory(root, name));
        }
        else if (entry.isFile()) {
            paths.push(name);
        }
        if (paths.length > maxFiles * 4)
            break;
    }
    return paths;
}
export async function scanRepository(directory) {
    let root = await realpath(directory);
    let names;
    let commit = null;
    const repository = await git("git", ["rev-parse", "--show-toplevel"], { cwd: root }).catch(() => null);
    if (repository) {
        root = await realpath(repository.stdout.trimEnd());
        const [index, head] = await Promise.all([
            git("git", ["ls-files", "-z", "--cached"], { cwd: root, maxBuffer: 32 * 1024 * 1024 }),
            git("git", ["rev-parse", "HEAD"], { cwd: root }).catch(() => null),
        ]);
        commit = head?.stdout.trim() ?? null;
        const files = !commit && !index.stdout
            ? await git("git", ["ls-files", "-z", "--others", "--exclude-standard"], { cwd: root, maxBuffer: 32 * 1024 * 1024 })
            : index;
        names = files.stdout.split("\0").filter(Boolean);
    }
    else {
        names = await listPlainDirectory(root);
    }
    const files = [];
    let bytes = 0;
    let truncated = false;
    for (const path of [...new Set(names)].sort()) {
        const segments = path.split("/");
        const filename = segments.pop();
        if (segments.some(part => blockedDirectory.test(part)) || blockedName.test(filename) || /[\x00-\x1f]/.test(path))
            continue;
        const language = extensions.get(filename.split(".").at(-1).toLowerCase());
        if (!language)
            continue;
        const absolute = resolve(root, path);
        if (!inside(root, absolute))
            continue;
        try {
            const stat = await lstat(absolute);
            if (!stat.isFile() || stat.size > maxFileBytes)
                continue;
            const canonical = await realpath(absolute);
            if (!inside(root, canonical) || relative(root, canonical).split(sep).join("/") !== path)
                continue;
            const data = await readFile(canonical);
            if (data.length > maxFileBytes || data.includes(0))
                continue;
            const content = new TextDecoder("utf-8", { fatal: true }).decode(data);
            if (secretContents.test(content))
                continue;
            if (files.length >= maxFiles || bytes + data.length > maxIndexBytes) {
                truncated = true;
                break;
            }
            bytes += data.length;
            const lines = content.split(/\r?\n/);
            if (lines.at(-1) === "")
                lines.pop();
            files.push({ path, language, lines, isTest: /(?:^|\/)(?:tests?|__tests__)\/|(?:^|\/)test_|(?:[._-](?:test|spec))\.[^.]+$/i.test(path) });
        }
        catch (error) {
            if (error instanceof TypeError || ["ENOENT", "EACCES", "EPERM", "ENOTDIR"].includes(error.code ?? ""))
                continue;
            throw error;
        }
    }
    return { root, commit, files, truncated, languages: [...new Set(files.map(file => file.language))].sort() };
}
