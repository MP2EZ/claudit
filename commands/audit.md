---
description: Multi-dimensional codebase audit across performance, security, accessibility, test coverage, architecture, TypeScript safety, and design system compliance
argument-hint: "[path] [--diff <branch>] [--staged] [--fast] [--save] [--perf] [--security] [--a11y] [--test] [--arch] [--ts] [--deps] [--resilience] [--ux] [--bloat] [--all] [--no-split] [--max-agents <N>] [--<dim>=<N>]"
---

# Codebase Audit

You are orchestrating a multi-dimensional audit using parallel SME agents. Each agent is an existing domain expert (`~/.claude/agents/`) that reviews code independently. You consolidate their findings into a single prioritized report.

## Parse Arguments

Raw arguments: `$ARGUMENTS`

Parse the following from arguments:
- **Target path**: Any bare argument (e.g., `src/auth/`) — defaults to current working directory
- **`--diff <branch>`**: Audit changes vs a branch (runs `git diff <branch>...HEAD`)
- **`--staged`**: Audit only staged changes (runs `git diff --cached`)
- **`--fast`**: Use sonnet model for all agents (faster, less depth)
- **`--save`**: Write report to `.audit-report.md` in the target directory
- **`--all`**: Run ALL agents (default 6 + all optional)

**Parallelism flags** — control within-dimension agent allocation (see Phase 1b):
- **`--no-split`**: Force exactly one agent per dimension (today's pre-split behavior). Disables auto-split.
- **`--max-agents <N>`**: Ceiling on total agents launched across all dimensions (default **16**). A cost/consolidation governor, not a concurrency limit — raise it to go deeper on large scopes (e.g. `--max-agents 24`).
- **`--<dim>=<N>`**: Manually set a dimension's agent count, bypassing the heuristic (e.g. `--perf=3`, `--test=1`). Implies that dimension is selected. DEPS is always 1. If `--no-split` is also present, `--no-split` wins (everything is 1/dim).

Auto-split is **on by default**. It only kicks in when a dimension's relevant scope exceeds the ~4k-LOC chunk budget (see Phase 1b); small scopes and git-diff audits naturally stay at 1 agent per dimension. Agent count scales with scope size (no per-dimension cap); `--max-agents` bounds the total.

**Dimension flags** — select specific agents to run:
- **`--perf`**: Performance agent
- **`--security`**: Security agent
- **`--a11y`**: Accessibility agent
- **`--test`**: Test coverage agent
- **`--arch`**: Architecture agent
- **`--ts`**: TypeScript agent
- **`--deps`**: Dependency health agent
- **`--resilience`**: Error handling / resilience agent
- **`--ux`**: UX / design system compliance agent
- **`--bloat`**: Code bloat / AI cruft agent (unnecessary comments, premature abstractions, dead code)

**Agent selection logic**:
- If **no dimension flags** are provided (and no `--all`): run the 6 default agents (performance, security, accessibility, test, architecture, typescript)
- If **one or more dimension flags** are provided: run **only** those specified agents — nothing else
- If **`--all`** is provided: run all 10 agents

If no path, `--diff`, or `--staged` is specified, auto-detect:
1. If on a feature branch (not main/master), use `git diff main...HEAD` (or master)
2. If there are unstaged/staged changes, use `git diff`
3. Otherwise, audit the current working directory

## Phase 1: Scope Resolution

**Actions**:
1. Determine the audit scope based on parsed arguments
2. If using git diff, run the diff to get the list of changed files
3. If using a path, enumerate the files in scope
4. Detect the project stack from config files (package.json, tsconfig.json, Cargo.toml, requirements.txt, go.mod, etc.)
5. Briefly state the resolved scope and detected stack to the user before proceeding

**Output to user**: "Auditing [scope description] — [N files, stack detected] — launching [M] agents..." (where M is the total after Phase 1b allocation, e.g. "launching 7 agents (perf×3, security×2, test×1, ts×1)").

## Phase 1b: Allocate Agents Within Dimensions

Decide how many agents each selected dimension gets, and how their work is divided. One signal — the dimension's **relevant set** (the files in scope it actually cares about, and their total LOC) — drives both decisions.

Skip this phase entirely (1 agent per dimension) if `--no-split` is set. Auto-split also naturally resolves to 1 agent per dimension when the scope is small (e.g. a git diff), since relevant LOC stays under the ~4k chunk budget.

### Step 1 — Compute each dimension's relevant set

For each selected dimension, determine `R` = relevant LOC and `F` = relevant file count from the resolved scope:

| Dimension | Relevant set = files in scope that… | Splittable? |
|-----------|-------------------------------------|-------------|
| Performance (PERF) | all non-test source files | yes |
| Security (SEC) | all source; weight trust-boundary files (routes/api, auth, db/query, files touching `request`/`process.env`/SQL/network) | yes |
| Accessibility (A11Y) | UI files only — `.tsx/.jsx/.vue/.svelte` or files containing markup/JSX | yes |
| Test (TEST) | source files with **no** sibling test (`foo.ts` lacking `foo.test.*`/`foo.spec.*` or a `__tests__` entry) | yes |
| TypeScript (TS) | `.ts`/`.tsx` files | yes |
| Resilience (RESILIENCE) | files with async/IO/error surface (`await`, `fetch`, `try/catch`, promises, fs/net) | yes |
| UX (UX) | UI component + style/token files | yes |
| Bloat (BLOAT) | all non-test source files | yes |
| Architecture (ARCH) | whole scope — **never file-split** (coupling/circular deps live *between* chunks) | sub-lens only |
| Deps (DEPS) | manifest files only — holistic, **always 1 agent** | no |

### Step 2 — Agent count per dimension

A chunk budget of ~4,000 LOC approximates what one agent can analyze **deeply** (read fully and trace, not skim). There is **no per-dimension cap** — depth scales with the dimension's size; total cost is governed by `--max-agents` instead (see below).

```
agents = max( 1, ceil(R / 4000) )            # 1 if R <= ~4000; grows with scope
if dimension is ARCH:  agents = (R > 8000 ? 2 : 1)   # sub-lens only, never file-split
if dimension is DEPS:  agents = 1
```

Then apply, in order:
1. **Manual overrides**: a `--<dim>=N` flag sets that dimension's count to N exactly (DEPS stays 1).
2. **Global ceiling** (`--max-agents`, default **16** — the harness concurrency limit, beyond which agents queue rather than run faster). `--max-agents` is a **cost/consolidation governor, not a concurrency limit**. If the summed demand exceeds the ceiling, repeatedly remove one agent from the dimension with the highest current count until the total fits. Never drop any dimension below 1. **Do not shave silently** — when demand was capped, tell the user what was trimmed and how to lift it, e.g.:
   > `perf wants 24 agents (95k LOC ÷ 4k); capped at 16 (~67% depth). Re-run with --max-agents 24 for full depth, or narrow the scope.`

### Step 3 — Partition strategy (for dimensions with agents > 1)

Pick the axis from the same signal:

```
if F >= 2*agents  → file-chunk   # enough files to divide cleanly; buys speed, clean dedup
else              → sub-lens     # few large files; chunking is meaningless, specialize instead
ARCH (agents = 2) → always sub-lens
```

- **file-chunk (module-seam aware)**: split the relevant files into `agents` disjoint groups, but partition **along directory/module boundaries** — keep files from the same directory/feature together in one chunk rather than splitting by raw LOC count. This keeps related files (a component and its hooks/utils) with a single agent, so cross-file findings aren't severed at a chunk boundary. Balance LOC across chunks only as a secondary goal: group whole directories, and only split a single oversized directory across agents when it alone exceeds the chunk budget. Aim for roughly equal LOC, intact modules first.
- **sub-lens**: all agents see the full relevant scope but each takes a distinct lens from the menu below (use the first `agents` entries). If a dimension has no menu, fall back to file-chunk even at low F.

Sub-lens menus:
- PERF → render · algorithmic complexity · network/N+1 & waterfalls
- SEC → injection & input validation · authz/authn · secrets & data exposure
- RESILIENCE → network-failure handling · unhandled rejections · error boundaries
- ARCH → coupling & circular deps · complexity hotspots / god files

**Output the allocation** in the Phase 1 user message (e.g. "perf×3 (file-chunk), security×2 (sub-lens), test×1").

## Phase 2: Launch Audit Agents

Launch agents **in parallel** using the Agent tool — one agent per *allocated slice* from Phase 1b, not one per dimension. A dimension allocated 3 agents launches 3 agents (each with its file-chunk or sub-lens); a dimension allocated 1 launches a single whole-scope agent as before. Each agent uses `subagent_type` to leverage existing domain agents. Provide each with its scope/lens, detected stack, and the audit output format. Launch all slices across all dimensions in a single parallel block.

### Available Agents

| Flag | Dimension | subagent_type | Default? |
|------|-----------|---------------|----------|
| `--perf` | Performance | `"performance"` | Yes |
| `--security` | Security | `"security"` | Yes |
| `--a11y` | Accessibility | `"accessibility"` | Yes |
| `--test` | Test Coverage | `"test"` | Yes |
| `--arch` | Architecture | `"architect"` | Yes |
| `--ts` | TypeScript | `"typescript"` | Yes |
| `--deps` | Dependency Health | `"m:deps-auditor"` | No |
| `--resilience` | Resilience | `"m:resilience-auditor"` | No |
| `--ux` | UX / Design System | `"ux"` | No |
| `--bloat` | Code Bloat | `"m:bloat-auditor"` | No |

### Which agents to launch

Apply this logic strictly:
1. **If `--all`**: launch ALL 10 agents
2. **If any dimension flag(s) are present** (e.g., `--ux`, `--perf --security`, etc.): launch **ONLY** the agents corresponding to those flags — do NOT include defaults
3. **If no dimension flags and no `--all`**: launch the 6 default agents (Performance, Security, Accessibility, Test Coverage, Architecture, TypeScript)

Launch all selected agents in a single parallel block.

### Agent Prompt Template

For each agent, use this prompt structure (adapt [DIMENSION], [PREFIX], and [FOCUS AREAS] per agent). Include the **Partition** block only when the dimension was split into more than one agent in Phase 1b — for single-agent dimensions, omit it and use the full resolved scope.

```
You are running as part of a /audit command. Your task is to AUDIT (analyze and report), not to fix or implement anything.

**Dimension**: [DIMENSION]
**Scope**: [this agent's scope — see Partition]
**Stack**: [detected stack]

[PARTITION — include one of these only when the dimension was split:]
  • file-chunk: This is slice [X] of [N] for this dimension. Analyze ONLY the files listed in Scope above — other slices cover the rest. Do not report on files outside your slice.
  • sub-lens: **Lens**: [SUB-LENS]. You see the full relevant scope, but report ONLY findings that fall under this lens — sibling agents cover the other lenses.

Read and analyze all code in scope. Apply your full domain expertise as an audit lens.

[DIMENSION-SPECIFIC FOCUS — see below]

Return findings in this exact structured format:

## [DIMENSION] Findings

### Critical
- **[PREFIX]-[S]-XX** (confidence: [0-100]) `file:line` — [description]
  → [specific fix recommendation]

### Warning
- **[PREFIX]-[S]-XX** (confidence: [0-100]) `file:line` — [description]
  → [recommendation]

### Info
- **[PREFIX]-[S]-XX** (confidence: [0-100]) `file:line` — [description]
  → [recommendation]

### Summary
[2-3 sentence summary of this slice's findings]

Rules:
- ID format: use your slice index [S] (1 for single-agent dimensions), e.g. PERF-2-03. The orchestrator renumbers to a flat sequence when merging.
- Only report findings with confidence >= 75
- Verify each finding is real before reporting — check for existing mitigations you may have missed
- Be specific: include file paths and line numbers
- If no issues meet the threshold, state the dimension looks healthy and note any minor observations
```

### Dimension-Specific Focus Instructions

Append these to the prompt for each agent:

**Performance** (prefix: PERF):
Focus on: render performance, algorithmic complexity (O(n^2)+), memory leaks, bundle impact, N+1 queries, missing caching, request waterfalls, unnecessary re-renders, missing memoization where data sets are large.

**Security** (prefix: SEC):
Focus on: OWASP Top 10, injection vectors, auth/authz gaps, secrets in code, input validation at boundaries, data exposure, insecure defaults. Apply your STRIDE and DREAD frameworks. Trace data flow from inputs to sensitive operations.

**Accessibility** (prefix: A11Y):
Focus on: semantic HTML/components, ARIA usage, keyboard navigation, screen reader compatibility, color contrast, touch target sizes, focus management. Apply WCAG 2.1 AA as baseline. For React Native: accessibilityLabel, accessibilityRole, accessibilityState.

**Test Coverage** (prefix: TEST):
Focus on: identifying untested code paths in scope, missing edge case tests, test quality (are tests asserting meaningful behavior or just testing mocks?), integration vs unit test gaps, critical paths without coverage. Do NOT write tests — only identify gaps.

**Architecture** (prefix: ARCH):
Focus on: coupling and cohesion, circular dependencies, complexity hotspots (god files, functions >50 lines), leaky abstractions, files that mix concerns, hidden dependencies between modules, patterns that make the code fragile to change.

**TypeScript** (prefix: TS):
Focus on: `any` type usage and type assertion abuse (`as unknown as`), missing strict mode flags, unsound patterns (unchecked index access, type narrowing gaps), overly broad types where narrower types exist, generic constraints that should be tighter.

**UX / Design System** (prefix: UX):
First, detect whether the project has a design system by checking for: a design system package in dependencies (e.g., a shared UI library, component library, or tokens package), a theme/tokens directory, a design system config, or imports from a known design system. If NO design system is detected, report a single Info finding stating no design system was found and skip the rest of this audit dimension. If a design system IS detected, focus on: whether all components and UI elements in scope use the design system's primitives (tokens, components, spacing, typography, colors) rather than raw/hardcoded values; identify instances of hardcoded colors, font sizes, spacing values, or shadows that should use design tokens; flag custom one-off components that duplicate or bypass existing design system components; check for consistent use of the design system's layout primitives vs ad-hoc styling; identify places where design system patterns are partially adopted (e.g., using the token for color but hardcoding the spacing).

**Code Bloat** (prefix: BLOAT):
You are auditing for violations of Claude Code's default code-hygiene rules — the kind of cruft that accumulates after long AI editing sessions. Focus on: comments that explain WHAT instead of WHY; multi-paragraph docstrings on trivial functions; references to the task/PR/caller in comments; `// removed`-style tombstones; abstractions with only one caller; factory/wrapper functions with one implementation; re-exports that just re-export; backwards-compat shims with no remaining callers; unused `_var` renames; validation or `try/catch` for cases that internal code can't produce (only flag at trust boundaries — NOT internal code); null checks on values guaranteed non-null by the type system; TODO/FIXME/HACK left in committed code; conditional branches with empty or stub bodies; dead `else` arms; tiny helpers wrapping a single expression used in one place. Every finding should be a deletion or simplification candidate. Distinguish bloat from legitimate complexity: validation at trust boundaries (user input, external APIs), comments explaining a non-obvious WHY (hidden constraint, subtle invariant, workaround for a specific bug), and abstractions with a clear imminent second caller are NOT bloat.

### Model Selection

- Default: Use `model: "opus"` for all agents
- If `--fast` flag: Use `model: "sonnet"` for all agents

## Phase 3: Consolidate Report

After all agents return, merge findings into a single report. When a dimension ran as multiple slices, merge in two levels:

1. **Within each dimension first**: combine that dimension's slices into one set. Dedup at boundaries — drop duplicate `file:line` findings that adjacent file-chunks or overlapping sub-lenses both reported, keeping the higher-severity/confidence one. Renumber the survivors to a single flat `[PREFIX]-NN` sequence (dropping the slice index, e.g. `PERF-2-03` → `PERF-07`).
   - **Consolidation tier** — if a dimension ran **more than 4 slices**, do this within-dimension merge with a dedicated sub-agent instead of inline, so the orchestrator's context isn't flooded by every slice's raw findings on large audits. Spawn one consolidator per such dimension (in parallel), passing it that dimension's slice outputs and these dedup+renumber rules; it returns the merged, flat-numbered `[PREFIX]` finding list. Dimensions with ≤4 slices are merged inline.
2. **Then across dimensions**: if two *different* dimensions flagged the same file:line for overlapping reasons, merge into the higher-severity finding and note both dimensions.
3. **Sort by severity**: Critical first, then Warning, then Info. Within each severity, sort by confidence (highest first)
4. **Generate summary table**

### Report Format

```markdown
## Audit Report
**Scope**: [scope description]
**Stack**: [detected stack]
**Agents**: [dimensions run, with per-dimension agent counts when split — e.g. performance×3, security×2, test×1]
**Date**: [current date]

---

### Critical ([count])

[DIMENSION] **[ID]** (confidence: [N]) `file:line`
[description]
> [fix recommendation]

...

### Warnings ([count])

...

### Info ([count])

...

---

### Summary

Only include rows for dimensions that were actually run:

| Dimension     | Critical | Warning | Info | Health |
|---------------|----------|---------|------|--------|
| [each dimension that was run] | N | N | N | [status] |
| **Total**     | **N**    | **N**   | **N**|        |

Health: pass (0 critical, <=2 warnings) | warn (1+ critical or 3+ warnings) | fail (3+ critical)
```

## Phase 4: Output

1. Display the full report inline in the conversation
2. If `--save` flag was provided, write the report to `.audit-report.md` in the target directory (or repo root)
3. If there are critical findings, end with: "Found [N] critical issues that should be addressed before merging."
4. If clean, end with: "No critical issues found. [N] warnings worth reviewing."
