#!/usr/bin/env python3
"""Mechanical half of /audit.

  audit.py plan  [audit args]   parse args, resolve scope, allocate + partition agents, write briefs
  audit.py merge [--combine A+B ...] [--suppress ID ...]   dedup, renumber, sort, write the report

The orchestrator only launches agents and makes the judgment calls this script lists for it.
"""
import fnmatch
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import NamedTuple

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = ".audit-run"
CHUNK_LOC = 4000            # what one agent can analyze deeply
DEFAULT_MAX_AGENTS = 16     # cost/consolidation governor
INLINE_FILE_LIMIT = 150     # longer file lists go to a side file the agent reads
TESTED_WEIGHT = 0.5         # a file some test reaches is checked against those tests, not read cold


class Dim(NamedTuple):
    prefix: str
    agent: str
    label: str
    default: bool


DIMS = {
    "perf": Dim("PERF", "audit:perf", "Performance", True),
    "security": Dim("SEC", "audit:security", "Security", True),
    "a11y": Dim("A11Y", "audit:a11y", "Accessibility", True),
    "test": Dim("TEST", "audit:test", "Test Coverage", True),
    "arch": Dim("ARCH", "audit:arch", "Architecture", True),
    "ts": Dim("TS", "audit:ts", "TypeScript", True),
    "deps": Dim("DEPS", "audit:deps", "Dependency Health", False),
    "resilience": Dim("RES", "audit:resilience", "Resilience", False),
    "ux": Dim("UX", "audit:ux", "UX / Design System", False),
    "bloat": Dim("BLOAT", "audit:bloat", "Code Bloat", False),
    "analytics": Dim("ANLT", "audit:analytics", "Analytics Coverage", False),
}
PREFIX_TO_DIM = {d.prefix: k for k, d in DIMS.items()}
DIM_ALIASES = {**PREFIX_TO_DIM, **{k.upper(): k for k in DIMS},
               "PERFORMANCE": "perf", "ACCESSIBILITY": "a11y", "TYPESCRIPT": "ts",
               "ARCHITECTURE": "arch", "RESILIENCE": "resilience", "ANALYTICS": "analytics"}

LENSES = {
    "perf": ["render", "algorithmic complexity", "network/N+1 & waterfalls"],
    "security": ["injection & input validation", "authz/authn", "secrets & data exposure"],
    "resilience": ["network-failure handling", "unhandled rejections", "error boundaries"],
    "arch": ["coupling & circular deps", "complexity hotspots / god files"],
    "analytics": ["behavioral/product coverage", "acquisition & marketing coverage", "data quality & identity"],
}
BLOAT_CROSS_LENS = "cross-file duplication, orphans & indirection"
ANLT_FIAT = ("Shared surfaces are assigned to stop triple-reporting: the purchase/paywall funnel and "
             "app-lifecycle events belong to *acquisition*; the onboarding funnel belongs to *behavioral*. "
             "Other lenses may cite them as context but must not file them.")

CODE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro", ".py", ".go",
            ".rs", ".rb", ".java", ".kt", ".kts", ".swift", ".m", ".mm", ".c", ".cc", ".cpp", ".h",
            ".hpp", ".cs", ".php", ".dart", ".scala", ".ex", ".exs", ".lua", ".sh", ".sql", ".html"}
STYLE_EXT = {".css", ".scss", ".sass", ".less", ".styl"}
JS_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro"}
TESTABLE_EXT = CODE_EXT - {".html", ".sql", ".sh"}
IMPORT_EXT = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js")
UI_EXT = {".tsx", ".jsx", ".vue", ".svelte", ".astro", ".html"}
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "out", ".next", ".expo", "coverage", "vendor",
             "__pycache__", ".venv", "venv", "target", "Pods", ".turbo", ".cache", RUN_DIR}
MANIFESTS = ["package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
             "requirements.txt", "pyproject.toml", "poetry.lock", "Pipfile", "Cargo.toml", "Cargo.lock",
             "go.mod", "go.sum", "Gemfile", "Gemfile.lock", "pubspec.yaml", "composer.json"]

IMPORT_RE = re.compile(r"""(?:\bfrom|\bimport|\brequire)\s*\(?\s*['"](\.{1,2}/[^'"]+)['"]""")
TEST_FILE_RE = re.compile(r"\.(test|spec)\.[^/]+$|_test\.go$|(^|/)test_[^/]+\.py$")
TEST_RE = re.compile(r"(^|/)(__tests__|tests?|spec|e2e)/|\.(test|spec)\.[^/]+$|_test\.go$|(^|/)test_[^/]+\.py$")
JSX_RE = re.compile(r"(return|=>)\s*\(?\s*<[A-Za-z]")
DOM_UI_RE = re.compile(r"document\.createElement\(|\.innerHTML\s*=|insertAdjacentHTML|\.tabIndex\b|"
                       r"\.setAttribute\(\s*['\"](aria-|role|tabindex)|getContext\(\s*['\"]2d")
ASYNC_RE = re.compile(r"\bawait\b|\bfetch\(|\btry\s*\{|\btry:|\.then\(|\.catch\(|\bPromise\b|\baxios\b|"
                      r"XMLHttpRequest|WebSocket|\bfs\.|\brequests\.|localStorage|sessionStorage|indexedDB|"
                      r"JSON\.parse|navigator\.serviceWorker|addEventListener\(\s*['\"](error|unhandledrejection)")
TOKEN_PATH_RE = re.compile(r"(^|/)(theme|tokens?|design-system|styles?)(/|\.)|tailwind\.config", re.I)
ROUTE_DIR_RE = re.compile(r"(^|/)(app|pages|routes|screens|views|api)/")
INTERACTION_RE = re.compile(r"onClick|onPress|onSubmit|handleSubmit|<form\b|\b(app|router)\.(post|put|patch|delete)\(|"
                            r"export\s+(async\s+)?function\s+(POST|PUT|PATCH|DELETE)\b|useMutation|"
                            r"addEventListener\(\s*['\"](click|pointer\w*|touch\w*|key\w*|submit|change|input)['\"]")
VENDOR_RE = re.compile(r"(?i:\bposthog\b|\bmixpanel\b|@segment/|@amplitude/|amplitude\.(getInstance|init|track)|\bgtag\(|"
                       r"\bdataLayer\b|firebase/analytics|@vercel/analytics|\bplausible\b|rudderstack|rudder-sdk)")
ANALYTICS_RE = re.compile(VENDOR_RE.pattern + r"|\.capture\(|\.track\(|logEvent\(")
ANALYTICS_PATH_RE = re.compile(r"analytics|tracking|telemetry", re.I)
SECURITY_RE = re.compile(r"\bfetch\(|localStorage|sessionStorage|indexedDB|JSON\.parse|innerHTML|"
                         r"dangerouslySetInnerHTML|\beval\(|new Function|postMessage|location\.|URLSearchParams|"
                         r"\b(req|request|env)\.|crypto\.|process\.env|child_process|\bexec\(|<script|"
                         r"\.prepare\(|new URL\(|DecompressionStream|atob\(|sha\d+|digest\(")
SERVER_DIR_RE = re.compile(r"(^|/)(worker|server|function)s?/")
# Not the product: prose, design mocks, component previews, examples. Excluded from every dimension but deps.
NOISE_RE = re.compile(r"(^|/)(docs|examples?|mocks?|__mocks__|fixtures?|stories|storybook)/|\.stories\.[jt]sx?$|"
                      r"(^|/)previews?(/|\.)")
# Code that never ships to users: tooling, migrations, build config. Excluded where runtime behaviour is the question.
NON_RUNTIME_RE = re.compile(NOISE_RE.pattern + r"|(^|/)(scripts|migrations|bench)/|(^|/)[^/]*\.config\.[cm]?[jt]s$|\.d\.ts$")
IGNORE_RE = re.compile(r"^-\s*`([^`]+)`\s*·\s*([A-Za-z0-9]+)\s*·")
FINDING_RE = re.compile(r"^-\s*\*\*([A-Z0-9]+)-(?:(\d+)-)?(\d+)\*\*\s*\(confidence:\s*(\d+)\)\s*`([^`]+)`\s*[—–-]+\s*(.*)$")
SEV_ORDER = ["Critical", "Warning", "Info"]


class UsageError(Exception):
    pass


class FileInfo(NamedTuple):
    path: str
    loc: int
    text: str


# ---------------------------------------------------------------- args

def parse_args(tokens):
    o = {"path": None, "diff": None, "staged": False, "fast": False, "save": False, "no_ignore": False,
         "all": False, "no_split": False, "max_agents": DEFAULT_MAX_AGENTS, "overrides": {}, "picked": []}
    it = iter(tokens)
    for t in it:
        if t in ("--diff", "--max-agents"):
            v = next(it, None)
            if v is None or v.startswith("--"):
                raise UsageError(f"{t} needs a value")
            if t == "--diff":
                o["diff"] = v
            else:
                o["max_agents"] = _int(v, t)
        elif t in ("--staged", "--fast", "--save", "--no-ignore", "--all", "--no-split"):
            o[t[2:].replace("-", "_")] = True
        elif t.startswith("--"):
            name, _, n = t[2:].partition("=")
            if name not in DIMS:
                raise UsageError(f"unknown flag {t}")
            o["picked"].append(name)
            if n:
                o["overrides"][name] = _int(n, t)
        elif o["path"] is None:
            o["path"] = t
        else:
            raise UsageError(f"more than one path: {o['path']}, {t}")
    if o["all"]:
        o["dims"] = list(DIMS)
    elif o["picked"]:
        o["dims"] = [k for k in DIMS if k in o["picked"]]
    else:
        o["dims"] = [k for k, d in DIMS.items() if d.default]
    del o["picked"]
    return o


def _int(v, flag):
    try:
        n = int(v)
    except ValueError:
        raise UsageError(f"{flag} needs an integer, got {v!r}")
    if n < 1:
        raise UsageError(f"{flag} must be >= 1")
    return n


# ---------------------------------------------------------------- relevant sets

def _ext(path):
    return Path(path).suffix.lower()


def is_test(path):
    return bool(TEST_RE.search(path))


def _stem(path):
    name = Path(path).name
    name = re.sub(r"\.(test|spec)(\.[^.]+)+$", "", name)
    name = re.sub(r"^test_|_test(?=\.)", "", name)
    return name.split(".")[0]


def relevant_sets(files, repo_files):
    """files: in-scope FileInfos; repo_files: whole repo (sibling-test lookup, analytics widening)."""
    code = [f for f in files if _ext(f.path) in CODE_EXT and not f.path.endswith(".min.js") and not NOISE_RE.search(f.path)]
    src = [f for f in code if not is_test(f.path)]
    runtime = [f for f in src if not NON_RUNTIME_RE.search(f.path)]
    ui = [f for f in runtime if _ext(f.path) in UI_EXT
          or (_ext(f.path) in {".js", ".ts"} and (JSX_RE.search(f.text) or DOM_UI_RE.search(f.text)))]
    # files that import an analytics module are its event sites, whatever they call the send
    hubs = {f.path for f in src if ANALYTICS_PATH_RE.search(f.path) or VENDOR_RE.search(f.text)}
    wired = {p for p, deps in import_graph(src).items() if deps & hubs}
    styles = [f for f in files if _ext(f.path) in STYLE_EXT and not NOISE_RE.search(f.path)]
    return {
        "perf": runtime,
        "security": [f for f in src if SECURITY_RE.search(f.text)
                     or ROUTE_DIR_RE.search(f.path) or SERVER_DIR_RE.search(f.path)],
        "a11y": ui + styles,  # focus rings, contrast and reduced motion live in CSS
        "test": [f for f in runtime if _ext(f.path) in TESTABLE_EXT],
        "arch": src,
        "ts": [f for f in src if _ext(f.path) in {".ts", ".tsx"}],
        "resilience": [f for f in src if ASYNC_RE.search(f.text)],
        "ux": ui + [f for f in styles + src if f not in ui and (f in styles or TOKEN_PATH_RE.search(f.path))],
        "bloat": code,
        "analytics": [f for f in src if ROUTE_DIR_RE.search(f.path) or INTERACTION_RE.search(f.text)
                      or ANALYTICS_RE.search(f.text) or ANALYTICS_PATH_RE.search(f.path) or f.path in wired],
    }


def import_graph(files):
    """Relative JS/TS imports: path -> the in-repo paths it imports."""
    known = {f.path for f in files}
    graph = {}
    for f in files:
        out = set()
        for spec in IMPORT_RE.findall(f.text):
            base = os.path.normpath(os.path.join(os.path.dirname(f.path), spec))
            stem = re.sub(r"\.[cm]?jsx?$", "", base)
            hit = next((c for c in (base, *(stem + e for e in IMPORT_EXT)) if c in known), None)
            if hit:
                out.add(hit)
        graph[f.path] = out
    return graph


def tested_by(repo_files, graph):
    """Source path -> the tests that reach it (through test helpers). Non-JS files fall back to stem match."""
    out = defaultdict(set)
    tests = [f.path for f in repo_files if is_test(f.path)]
    named = [t for t in tests if TEST_FILE_RE.search(t)]
    for t in named or tests:  # helpers under tests/ are walked through, not counted as tests
        seen, stack = set(), [t]
        while stack:
            for dep in graph.get(stack.pop(), ()):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep) if is_test(dep) else out[dep].add(t)
    stems = defaultdict(set)
    for f in repo_files:
        if is_test(f.path):
            stems[_stem(f.path)].add(f.path)
    for f in repo_files:
        if not is_test(f.path) and _ext(f.path) not in JS_EXT and _stem(f.path) in stems:
            out[f.path] |= stems[_stem(f.path)]
    return out


# ---------------------------------------------------------------- allocation + partition

def _demand(dim, loc):
    if dim == "deps":
        return 1
    if dim == "arch":
        return 2 if loc > 8000 else 1
    if dim == "analytics":
        return min(3, max(1, math.ceil(loc / CHUNK_LOC)))  # lenses, one per CHUNK_LOC
    return max(1, math.ceil(loc / CHUNK_LOC))


def allocate(loc_by_dim, overrides, no_split, max_agents):
    if no_split:
        counts = {d: 1 for d in loc_by_dim}
    else:
        counts = {d: _demand(d, r) for d, r in loc_by_dim.items()}
        for d, n in overrides.items():
            if d in counts and d != "deps":
                counts[d] = n
    demanded = dict(counts)
    while sum(counts.values()) > max_agents:
        # trim the busiest dimension; on ties, the one with the least LOC loses the least depth
        d = max(counts, key=lambda k: (counts[k], -loc_by_dim[k]))
        if counts[d] <= 1:
            break
        counts[d] -= 1
    return counts, demanded


def _loc(fs):
    return sum(f.loc for f in fs)


def chunk_by_module(files, n):
    """n disjoint chunks; whole directories stay together unless one alone exceeds the target."""
    target = _loc(files) / n
    groups = defaultdict(list)
    for f in sorted(files, key=lambda f: f.path):
        groups[str(Path(f.path).parent)].append(f)
    units = []
    for fs in groups.values():
        if _loc(fs) > target and len(fs) > 1:
            piece = []
            for f in fs:
                if piece and _loc(piece) + f.loc > target:
                    units.append(piece)
                    piece = []
                piece.append(f)
            units.append(piece)
        else:
            units.append(fs)
    while len(units) < n:
        splittable = [u for u in units if len(u) > 1]
        if not splittable:
            break
        u = max(splittable, key=_loc)
        units.remove(u)
        units += [u[: len(u) // 2], u[len(u) // 2:]]
    bins = [[] for _ in range(min(n, len(units)))]
    for u in sorted(units, key=_loc, reverse=True):
        min(bins, key=_loc).extend(u)
    return [sorted(b, key=lambda f: f.path) for b in bins]


def _slice(mode, files, lens=None):
    return {"mode": mode, "files": [f.path for f in files], "loc": _loc(files), "lens": lens}


def partition(dim, files, n):
    if n <= 1:
        return [_slice("whole", files)]
    if dim == "bloat":
        rest = chunk_by_module(files, n - 1) if n > 2 else [files]
        return [_slice("sub-lens", files, BLOAT_CROSS_LENS)] + [_slice("file-chunk", c) for c in rest]
    if dim in ("arch", "analytics") or (len(files) < 2 * n and dim in LENSES):
        lenses = LENSES[dim][:n]
        if dim == "analytics" and n == 2:
            lenses = [f"{lenses[0]} + data quality & identity", lenses[1]]
        return [_slice("sub-lens", files, lens) for lens in lenses]
    return [_slice("file-chunk", c) for c in chunk_by_module(files, n)]


# ---------------------------------------------------------------- scope (git / filesystem)

def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _toplevel(path):
    out = _git(["rev-parse", "--show-toplevel"], path if path.is_dir() else path.parent)
    return Path(out).resolve() if out else None


def _ignore_rule(root, target):
    out = _git(["check-ignore", "-v", "--", str(target)], root)
    return out.split("\t")[0] if out else None


def _keep(path):
    return not any(part in SKIP_DIRS for part in Path(path).parts)


def list_repo(root):
    out = _git(["ls-files", "-co", "--exclude-standard"], root)
    if out is None:
        paths = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    else:
        paths = out.splitlines()
    return sorted(p for p in paths if _keep(p) and (root / p).is_file())


def load(root, paths):
    infos = []
    for p in paths:
        if _ext(p) not in CODE_EXT | STYLE_EXT:
            continue
        try:
            text = (root / p).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        infos.append(FileInfo(p, text.count("\n") + 1, text))
    return infos


def resolve_scope(o, cwd):
    # Root comes from the target, not cwd: a path in a nested repo or linked worktree
    # is invisible (often gitignored) from the enclosing repo's ls-files.
    target = (cwd / o["path"]).resolve() if o["path"] else None
    if target and not target.exists():
        raise UsageError(f"no such path: {o['path']}")
    root = _toplevel(target or cwd) or cwd
    repo = list_repo(root)
    base = None

    def changed(args):
        out = _git(["diff", "--name-only", "--diff-filter=d", *args], root) or ""
        return [p for p in out.splitlines() if _keep(p)]

    if target:
        rel = target.relative_to(root) if target != root else Path(".")
        paths = [p for p in repo if rel == Path(".") or Path(p).is_relative_to(rel)]
        return root, "path", f"{rel}/", paths, None, target
    if o["diff"]:
        base = o["diff"]
        return root, "diff", f"git diff {base}...HEAD", changed([f"{base}...HEAD"]), base, root
    if o["staged"]:
        return root, "diff", "staged changes", changed(["--cached"]), "--cached", root
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    if branch and branch not in ("main", "master", "HEAD"):
        for cand in ("main", "master", "origin/main", "origin/master"):
            if _git(["rev-parse", "--verify", "--quiet", cand], root) is not None:
                paths = changed([f"{cand}...HEAD"])
                if paths:
                    return root, "diff", f"git diff {cand}...HEAD", paths, cand, root
                break
    paths = changed(["HEAD"])
    if paths:
        return root, "diff", "uncommitted changes", paths, "HEAD", root
    rel = cwd.resolve().relative_to(root)
    paths = [p for p in repo if rel == Path(".") or Path(p).is_relative_to(rel)]
    return root, "path", f"{rel}/", paths, None, cwd.resolve()


def detect_stack(root, repo_paths=()):
    stack = []
    pkgs = [root / p for p in repo_paths if Path(p).name == "package.json" and len(Path(p).parts) <= 3]
    pkg = root / "package.json" if (root / "package.json").exists() else (pkgs[0] if pkgs else root / "package.json")
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
        except ValueError:
            data = {}
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        known = ["next", "react-native", "expo", "react", "vue", "nuxt", "svelte", "@sveltejs/kit", "astro",
                 "express", "fastify", "hono", "@nestjs/core", "electron", "tailwindcss", "zustand",
                 "@supabase/supabase-js", "prisma", "drizzle-orm"]
        stack += [k for k in known if k in deps] or ["Node"]
    if (root / "tsconfig.json").exists() or (pkg.parent / "tsconfig.json").exists():
        stack.insert(0, "TypeScript")
    for f, name in [("pyproject.toml", "Python"), ("requirements.txt", "Python"), ("Cargo.toml", "Rust"),
                    ("go.mod", "Go"), ("Gemfile", "Ruby"), ("pubspec.yaml", "Dart/Flutter")]:
        if (root / f).exists() and name not in stack:
            stack.append(name)
    return ", ".join(stack) or "unknown"


def load_ignore(root):
    entries = defaultdict(list)
    f = root / ".audit-ignore.md"
    if f.exists():
        for line in f.read_text().splitlines():
            m = IGNORE_RE.match(line.strip())
            if m and m.group(2).upper() in DIM_ALIASES:
                entries[DIM_ALIASES[m.group(2).upper()]].append((m.group(1), line.strip()))
    return entries


# ---------------------------------------------------------------- plan

def _partition_text(dim, s, slices):
    i, n = s["slice"], len(slices)
    if s["mode"] == "whole":
        return "You are the only agent for this dimension: cover every file listed below."
    if dim == "bloat" and s["lens"] == BLOAT_CROSS_LENS:
        return (f"Lens: **{BLOAT_CROSS_LENS}**. Run the structural sweep over all files below and report ONLY "
                "cross-file findings (duplication, orphan modules, dead exports, indirection chains, "
                "over-parameterization). Sibling slices cover local file hygiene.")
    if dim == "bloat":
        return (f"File-chunk {i}/{n} for local bloat: report ONLY on the files below, and do NOT report "
                "duplication, orphan, dead-export, or indirection findings: slice 1 owns them.")
    if s["mode"] == "file-chunk":
        return (f"File-chunk {i}/{n}: report ONLY on the files below; sibling slices cover the rest of this dimension. "
                "Read what these files import or are called by, even outside this list, when a finding depends on it "
                "(a render cost set by a store, an error swallowed by a caller); anchor the finding here.")
    others = ", ".join(o["lens"] for o in slices if o is not s)
    text = (f"Lens: **{s['lens']}**. You see the full relevant scope, but report ONLY findings under this lens; "
            f"sibling agents cover: {others}. Use Grep to find lens-relevant code and read only that.")
    return text + (" " + ANLT_FIAT if dim == "analytics" else "")


def _file_line(p, tests):
    if tests is None:
        return p
    if not tests:
        return f"{p} — no test reaches it"
    t = sorted(tests)
    return f"{p} — tests: {', '.join(t[:3])}" + (f" (+{len(t) - 3} more)" if len(t) > 3 else "")


def write_brief(run, root, stack, scope_desc, base, dim, s, slices, ignore, contract, widened_note, tests_of=None):
    d = DIMS[dim]
    lines = [f"# /audit brief: {d.label}, slice {s['slice']} of {len(slices)}", "",
             f"- Dimension: {d.label}. Finding prefix `{d.prefix}`, slice index `{s['slice']}` "
             f"(IDs look like `{d.prefix}-{s['slice']}-01`)",
             f"- Repo root: `{root}` (anchors are paths relative to it)",
             f"- Stack: {stack}",
             f"- Scope: {scope_desc}",
             f"- Findings file: `{run / 'findings' / (s['id'] + '.md')}`"]
    if base and dim not in ("deps", "analytics"):
        diff = "git diff --cached" if base == "--cached" else f"git diff {base}...HEAD" if base != "HEAD" else "git diff HEAD"
        lines.append(f"- Scope is a diff: concentrate on changed code (`{diff} -- <file>`); "
                     "use unchanged code as context only.")
    if widened_note and dim == "analytics":
        lines.append(f"- {widened_note}")
    lines += ["", "## Partition", "", _partition_text(dim, s, slices), "",
              f"## Files ({len(s['files'])} files, {s['loc']} LOC" +
              ("; tested files count half" if dim == "test" else "") + ")", ""]
    shown = [_file_line(p, tests_of.get(p, ()) if dim == "test" else None) for p in s["files"]]
    if len(s["files"]) > INLINE_FILE_LIMIT:
        side = run / "scope" / f"{s['id']}.txt"
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text("\n".join(shown) + "\n")
        lines.append(f"Listed in `{side}`: read it first.")
    else:
        lines += [f"- {p}" for p in shown]
    if ignore.get(dim):
        lines += ["", "## Accepted patterns: do not report", ""] + [e for _, e in ignore[dim]]
    lines += ["", contract]
    path = run / "briefs" / f"{s['id']}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def cmd_plan(tokens, cwd):
    o = parse_args(tokens)
    root, mode, scope_desc, paths, base, target = resolve_scope(o, cwd)
    if mode == "path" and not paths:
        rule = _ignore_rule(root, target)
        raise UsageError(f"{scope_desc} is gitignored ({rule}) in {root}" if rule
                         else f"nothing to audit in {scope_desc}")
    run = root / RUN_DIR
    shutil.rmtree(run, ignore_errors=True)
    for sub in ("briefs", "findings"):
        (run / sub).mkdir(parents=True)
    exclude = root / (_git(["rev-parse", "--git-path", "info/exclude"], root) or ".git/info/exclude")
    if exclude.exists() and RUN_DIR not in exclude.read_text():
        with exclude.open("a") as fh:
            fh.write(f"\n{RUN_DIR}/\n")

    repo_paths = list_repo(root)
    files = load(root, paths)
    repo_files = load(root, repo_paths) if mode == "diff" or target != root else files
    rel = relevant_sets(files, repo_files)
    tests_of = tested_by(repo_files, import_graph(repo_files)) if "test" in o["dims"] else {}
    rel["test"] = [f._replace(loc=math.ceil(f.loc * TESTED_WEIGHT)) if f.path in tests_of else f for f in rel["test"]]
    notes = []
    if mode == "diff" and "analytics" in o["dims"]:
        rel["analytics"] = relevant_sets(repo_files, repo_files)["analytics"]
        notes.append("analytics widened to full repo: coverage can't be judged from a diff.")
    manifests = [FileInfo(m, 1, "") for m in MANIFESTS if (root / m).exists()]
    rel["deps"] = manifests

    dims = []
    for d in o["dims"]:
        if rel[d]:
            dims.append(d)
        else:
            notes.append(f"{d} skipped: nothing in scope it audits.")
    if not dims:
        raise UsageError(f"nothing to audit in {scope_desc}")

    loc_by_dim = {d: _loc(rel[d]) for d in dims}
    counts, demanded = allocate(loc_by_dim, o["overrides"], o["no_split"], o["max_agents"])
    capped = [f"{d} {demanded[d]}→{counts[d]} ({loc_by_dim[d] / 1000:.0f}k LOC)"
              for d in dims if counts[d] < demanded[d]]
    if capped:
        notes.append(f"capped at --max-agents {o['max_agents']} (demand {sum(demanded.values())}): "
                     f"{', '.join(capped)}. Re-run with --max-agents {sum(demanded.values())} "
                     "for full depth, or narrow the scope.")
    if len(dims) > o["max_agents"]:
        notes.append(f"--max-agents {o['max_agents']} is below the dimension count; running 1 per dimension.")

    ignore = {} if o["no_ignore"] else load_ignore(root)
    stack = detect_stack(root, repo_paths)
    contract = (PLUGIN_ROOT / "audit" / "contract.md").read_text().strip()
    model = "sonnet" if o["fast"] else "opus"
    slices, agent_lines, alloc = [], [], []
    for d in dims:
        dslices = partition(d, rel[d], counts[d])
        modes = {s["mode"] for s in dslices} - {"whole"}
        alloc.append(f"{d}×{len(dslices)}" + (f" ({'/'.join(sorted(modes))})" if modes else ""))
        for i, s in enumerate(dslices, 1):
            s.update(id=f"{DIMS[d].prefix}-{i}", dim=d, prefix=DIMS[d].prefix, slice=i)
        for s in dslices:
            brief = write_brief(run, root, stack, scope_desc, base, d, s, dslices, ignore, contract,
                                notes[0] if mode == "diff" and d == "analytics" else None, tests_of)
            agent_lines.append(f"AGENT {s['id']} | {DIMS[d].agent} | {model} | Audit {s['id']} | "
                               f"Read {brief} and follow it exactly.")
        slices += dslices

    n_ignore = sum(len(v) for v in ignore.values())
    summary = (f"Auditing {scope_desc} ({len(files)} files, {stack}), launching {len(slices)} agents "
               f"({', '.join(alloc)})")
    summary += " — .audit-ignore.md bypassed" if o["no_ignore"] else f" — {n_ignore} accepted patterns loaded" if n_ignore else ""
    plan = {"root": str(root), "scope": scope_desc, "stack": stack, "date": date.today().isoformat(),
            "save_to": str(target / ".audit-report.md") if o["save"] else None, "no_ignore": o["no_ignore"],
            "ignore": {d: v for d, v in ignore.items()}, "demanded_total": sum(demanded.values()),
            "launched_total": len(slices), "slices": slices}
    (run / "plan.json").write_text(json.dumps(plan, indent=1))
    if root != (_toplevel(cwd) or cwd):
        notes.append(f"scope root is {root}; merge with --dir {root}")
    print(f"SUMMARY {summary}")
    for n in notes:
        print(f"NOTE {n}")
    print("\n".join(agent_lines))


# ---------------------------------------------------------------- merge

def parse_findings(text):
    findings, extras, summary = [], {}, []
    sev = section = None
    for line in text.splitlines():
        h = re.match(r"^###\s+(.+?)\s*$", line)
        if h:
            name = h.group(1)
            sev = next((s for s in SEV_ORDER if name.lower().startswith(s.lower())), None)
            section = None if sev else "summary" if name.lower().startswith("summary") else name
            if section and section != "summary":
                extras[section] = []
            continue
        if sev:
            m = FINDING_RE.match(line.strip())
            if m:
                findings.append({"prefix": m.group(1), "slice": int(m.group(2)) if m.group(2) else None,
                                 "nn": int(m.group(3)), "conf": int(m.group(4)), "anchor": m.group(5).strip(),
                                 "desc": m.group(6).strip(), "rec": "", "sev": sev})
            elif findings and line.strip() and line.startswith((" ", "\t")):
                findings[-1]["rec"] = (findings[-1]["rec"] + " " + line.strip().lstrip("→").strip()).strip()
        elif section == "summary":
            summary.append(line)
        elif section:
            extras[section].append(line)
    return {"findings": findings, "extras": extras, "summary": "\n".join(summary).strip()}


def anchor_path(anchor, root=None):
    p = re.sub(r":\d+(-\d+)?(:\d+)?$", "", anchor).removeprefix("./")
    if root and p.startswith(root.rstrip("/") + "/"):
        p = p[len(root.rstrip("/")) + 1:]
    return p


def _norm_anchor(anchor, root):
    path = anchor_path(anchor)
    return anchor_path(anchor, root) + anchor[len(path) + (2 if anchor.startswith("./") else 0):]


def _rank(f):
    return (SEV_ORDER.index(f["sev"]), -f["conf"])


def merge(plan, texts, combine=(), suppress=()):
    breaks, retro, by_dim, extras = [], [], defaultdict(list), defaultdict(list)
    root = plan.get("root")
    for s in plan["slices"]:
        text = texts.get(s["id"])
        if text is None:
            breaks.append(f"{s['id']}: findings file missing")
            continue
        parsed = parse_findings(text)
        bullets = sum(1 for line in text.splitlines() if line.startswith("- **"))
        if bullets > len(parsed["findings"]):
            breaks.append(f"{s['id']}: {bullets - len(parsed['findings'])} finding line(s) not in the contract format")
        for f in parsed["findings"]:
            f["key"] = f"{f['prefix']}-{f['slice'] or s['slice']}-{f['nn']:02d}"
            f["dims"] = [f["prefix"]]
            f["sid"], f["smode"] = s["id"], s["mode"]
            if f["prefix"] != s["prefix"]:
                breaks.append(f"{s['id']}: {f['key']} uses prefix {f['prefix']}, expected {s['prefix']}")
            if s["mode"] == "file-chunk" and anchor_path(f["anchor"], root) not in s["files"]:
                breaks.append(f"{s['id']}: {f['key']} at {f['anchor']} is outside its file-chunk")
        if not parsed["findings"] and not parsed["extras"] and s["loc"] >= 1000:
            retro.append(f"2 empty slice: {s['id']} returned nothing from {s['loc']} LOC")
        by_dim[s["dim"]].extend(parsed["findings"])
        for name, rows in parsed["extras"].items():
            extras[name].append(rows)

    findings, dup_candidates = [], []
    for dim, fs in by_dim.items():
        at = defaultdict(list)
        for f in fs:
            at[_norm_anchor(f["anchor"], root)].append(f)
        dups = 0
        for group in at.values():
            win = min(group, key=_rank)
            # One agent's findings at one line are distinct defects; only another slice can duplicate them.
            for f in group:
                if f["sid"] == win["sid"]:
                    findings.append(f)
                elif "sub-lens" in (f["smode"], win["smode"]):
                    findings.append(f)  # lenses overlap by design: the orchestrator decides, via --combine
                    dup_candidates.append((win["key"], f["key"]))
                else:
                    dups += 1
        if dups >= 3:
            retro.append(f"3 dedup churn: {dups} duplicates dropped in {DIMS[dim].prefix}")

    by_anchor = defaultdict(list)
    for f in findings:
        by_anchor[_norm_anchor(f["anchor"], root)].append(f)
    cross = [tuple(f["key"] for f in fs) for fs in by_anchor.values() if len({f["prefix"] for f in fs}) > 1]

    ignore_candidates = []
    for f in findings:
        if f["desc"].startswith("[stale-acceptance]"):
            continue
        for glob, entry in plan.get("ignore", {}).get(PREFIX_TO_DIM.get(f["prefix"], ""), []):
            if fnmatch.fnmatch(anchor_path(f["anchor"], root), glob):
                ignore_candidates.append((f["key"], entry))
                break

    keyed = {f["key"]: f for f in findings}
    for group in combine:
        members = [keyed[k] for k in group if k in keyed]
        if len(members) < 2:
            continue
        members.sort(key=_rank)
        win = members[0]
        for lose in members[1:]:
            win["dims"] += [d for d in lose["dims"] if d not in win["dims"]]
            win["desc"] += f" (also {lose['prefix']}: {lose['desc']})"
            findings.remove(lose)
    cross = [g for g in cross if all(keyed[k] in findings for k in g)]
    suppressed = defaultdict(int)
    for k in suppress:
        if k in keyed and keyed[k] in findings:
            findings.remove(keyed[k])
            suppressed[keyed[k]["prefix"]] += 1

    order = [DIMS[d].prefix for d in DIMS]
    for prefix in order:
        fs = sorted((f for f in findings if f["prefix"] == prefix), key=lambda f: (*_rank(f), f["anchor"]))
        for i, f in enumerate(fs, 1):
            f["id"] = f"{prefix}-{i:02d}"
    findings.sort(key=lambda f: (*_rank(f), order.index(f["prefix"]), f["id"]))

    if plan["demanded_total"] and (plan["demanded_total"] - plan["launched_total"]) / plan["demanded_total"] >= 0.5:
        retro.append(f"1 allocation: launched {plan['launched_total']} of {plan['demanded_total']} demanded agents")
    if breaks:
        retro.append(f"4 contract break: {len(breaks)} violation(s), see BREAK lines")
    dup_candidates = [g for g in dup_candidates if all(keyed[k] in findings for k in g)]
    return {"findings": findings, "cross": cross + dup_candidates, "ignore_candidates": ignore_candidates,
            "suppressed": dict(suppressed), "breaks": breaks, "retro": retro, "extras": dict(extras)}


def health(c, w):
    return "fail" if c >= 3 else "warn" if c >= 1 or w >= 3 else "pass"


def _union_tables(tables):
    """Union markdown tables by first cell; on conflict keep the row with the higher priority."""
    def score(cells):
        for i, word in enumerate(["critical", "warning", "info"]):
            if any(c.lower() == word for c in cells):
                return i
        return {"no": 0, "partial": 1, "yes": 2}.get(cells[1].lower() if len(cells) > 1 else "", 3)

    header, rows = [], {}
    for lines in tables:
        tl = [line for line in lines if line.strip().startswith("|")]
        if len(tl) < 2:
            continue
        header = header or tl[:2]
        for line in tl[2:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            k = cells[0].lower()
            if k not in rows or score(cells) < score(rows[k][0]):
                rows[k] = (cells, line.strip())
    return "\n".join(header + [line for _, line in rows.values()]) if header else ""


def _finding_md(f):
    return (f"[{'+'.join(f['dims'])}] **{f['id']}** (confidence: {f['conf']}) `{f['anchor']}`\n"
            f"{f['desc']}\n> {f['rec']}\n")


def render_report(plan, r):
    fs = r["findings"]
    labels = {DIMS[d].prefix: DIMS[d].label for d in DIMS}
    counts = defaultdict(list)
    for s in plan["slices"]:
        counts[s["prefix"]].append(s["id"])
    agents = ", ".join(f"{labels[p]}×{len(v)}" if len(v) > 1 else labels[p] for p, v in counts.items())
    out = ["## Audit Report", f"**Scope**: {plan['scope']}", f"**Stack**: {plan['stack']}",
           f"**Agents**: {agents}", f"**Date**: {plan['date']}"]
    n_sup = sum(r["suppressed"].values())
    if n_sup and not plan.get("no_ignore"):
        out.append(f"**Suppressed**: {n_sup} findings matched `.audit-ignore.md`")
    out += ["", "---", ""]
    for sev, title in zip(SEV_ORDER, ["Critical", "Warnings", "Info"]):
        group = [f for f in fs if f["sev"] == sev]
        out += [f"### {title} ({len(group)})", ""] + [_finding_md(f) for f in group]
    out += ["---", "", "### Summary", "", "| Dimension | Critical | Warning | Info | Health |",
            "|---|---|---|---|---|"]
    tot = [0, 0, 0]
    for p in counts:
        c = [sum(1 for f in fs if f["prefix"] == p and f["sev"] == s) for s in SEV_ORDER]
        tot = [a + b for a, b in zip(tot, c)]
        out.append(f"| {labels[p]} | {c[0]} | {c[1]} | {c[2]} | {health(c[0], c[1])} |")
    out += [f"| **Total** | **{tot[0]}** | **{tot[1]}** | **{tot[2]}** | |", "",
            "Health: pass (0 critical, <=2 warnings) | warn (1+ critical or 3+ warnings) | fail (3+ critical)"]
    tables = [t for name in ("Coverage Map", "Metric Readiness")
              if (t := _union_tables(r["extras"].get(name, [])))]
    if tables:
        out += ["", "---", "", "### Appendix: Analytics Coverage", ""] + [t + "\n" for t in tables]
    return "\n".join(out) + "\n"


def cmd_merge(tokens, cwd):
    combine, suppress, where = [], [], cwd
    it = iter(tokens)
    for t in it:
        v = next(it, None)
        if t == "--combine" and v:
            combine += [tuple(g.split("+")) for g in v.split(",")]
        elif t == "--suppress" and v:
            suppress += v.split(",")
        elif t == "--dir" and v:
            where = (cwd / v).resolve()
        else:
            raise UsageError(f"merge takes --combine A+B[,C+D], --suppress ID[,ID] and --dir PATH; got {t}")
    root = _toplevel(where) or where
    run = root / RUN_DIR
    if not (run / "plan.json").exists():
        raise UsageError(f"no plan in {run}; run plan first, or pass the --dir its NOTE names")
    plan = json.loads((run / "plan.json").read_text())
    texts = {}
    for s in plan["slices"]:
        p = run / "findings" / f"{s['id']}.md"
        texts[s["id"]] = p.read_text() if p.exists() else None
    r = merge(plan, texts, combine, suppress)
    report = render_report(plan, r)
    (run / "report.md").write_text(report)
    saved = ""
    if plan.get("save_to"):
        Path(plan["save_to"]).write_text(report)
        saved = f" (saved: {plan['save_to']})"
    print(f"REPORT {run / 'report.md'}{saved}")
    print(report[report.index("| Dimension"):report.index("\nHealth:")].strip())
    crit = [f for f in r["findings"] if f["sev"] == "Critical"]
    print(f"CRITICAL {len(crit)}")
    for f in crit:
        print(_finding_md(f).rstrip())
    keyed = {f["key"]: f for f in r["findings"]}
    for group in r["cross"]:
        print("CROSS " + " + ".join(group) + " | " + " | ".join(
            f"{k}: {keyed[k]['sev']}: {keyed[k]['desc']}" for k in group if k in keyed))
    for k, entry in r["ignore_candidates"]:
        f = keyed.get(k)
        if f:
            print(f"IGNORE {k} | {f['anchor']}: {f['desc']} | entry: {entry}")
    for b in r["breaks"]:
        print(f"BREAK {b}")
    for t in r["retro"]:
        print(f"RETRO {t}")
    if r["suppressed"]:
        print("SUPPRESSED " + ", ".join(f"{p}: {n}" for p, n in r["suppressed"].items()))


def main(argv):
    if not argv or argv[0] not in ("plan", "merge"):
        print("usage: audit.py plan [args] | audit.py merge [--combine A+B] [--suppress ID] [--dir PATH]", file=sys.stderr)
        return 2
    try:
        (cmd_plan if argv[0] == "plan" else cmd_merge)(argv[1:], Path.cwd())
    except UsageError as e:
        print(f"USAGE ERROR {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
