# claudit

A codebase audit command for [Claude Code](https://claude.com/claude-code). `/audit` splits your code across parallel agents, each focused on one area — security, performance, accessibility, and so on. It then merges and de-duplicates their findings into one report sorted by severity.

## Install

You need Claude Code and `python3` on your PATH.

In Claude Code, run:

```
/plugin marketplace add MP2EZ/claudit
/plugin install audit@audit-plugin
```

Restart Claude Code. The command is `/audit`. If another plugin also has an `audit` command, use `/audit:audit`.

To get updates later:

```
/plugin marketplace update audit-plugin
```

## Quick start

Run these from inside your project:

```
/audit .            # audit the whole repo
/audit              # audit what you're working on (see below)
/audit src/api      # audit one folder
```

## What gets audited

**With a path**, `/audit` covers every tracked file under it. Use `/audit .` for the whole repo.

**Without a path**, it picks the work in progress, checking in this order:

1. On a branch other than `main`/`master`: everything the branch changed compared with `main`.
2. Otherwise, if you have uncommitted changes: just those files.
3. Otherwise: everything under the current directory.

So on a clean `main`, `/audit` and `/audit .` are the same. On a feature branch, `/audit` only looks at your branch's changes. Use `/audit .` there if you want the whole repo.

To pick the comparison yourself:

| Command | Audits |
|---|---|
| `/audit --diff main` | changes on this branch since it split from `main` |
| `/audit --diff v2.3.0` | changes since a tag or commit |
| `/audit --staged` | only what's staged (`git add`ed) |

A path takes priority over `--diff` and `--staged`, so don't combine them.

Files ignored by git, along with `node_modules`, build output, and similar folders, are always skipped.

## Choosing dimensions

With no dimension flags, `/audit` runs the six defaults. Name one or more flags to run only those, or use `--all` for every dimension.

| Flag | Looks for | Default |
|---|---|:---:|
| `--security` | injection, auth gaps, secrets in code, data exposure | ✓ |
| `--perf` | slow renders, O(n²) loops, memory leaks, N+1 queries, request waterfalls | ✓ |
| `--a11y` | missing labels, keyboard traps, contrast, focus handling (WCAG 2.1 AA) | ✓ |
| `--test` | untested critical paths, missing edge cases, weak assertions | ✓ |
| `--arch` | circular dependencies, god files, tight coupling | ✓ |
| `--ts` | `any`, unsafe casts, missing strict flags | ✓ |
| `--deps` | outdated, vulnerable, unused, or deprecated packages | |
| `--resilience` | unhandled errors, missing error boundaries, network failure handling | |
| `--ux` | hardcoded colors/spacing, components that bypass your design system | |
| `--bloat` | dead code, duplicate implementations, needless abstractions | |
| `--analytics` | missing or broken event tracking, PII in events | |

Dimensions that don't apply are skipped. For example, `--a11y` with no UI files in scope runs no agent.

## Examples

Flags combine freely, in any order.

```
# Before opening a PR: check your branch for the things that bite in review
/audit --diff main --security --test --ts

# One folder, two dimensions
/audit src/payments --security --resilience

# Everything, everywhere
/audit . --all

# Quick, cheaper pass over the whole repo
/audit . --fast

# Dependency check only
/audit --deps

# Clean up after a burst of AI-written code
/audit --diff main --bloat --arch

# Audit staged changes and keep the report in the repo
/audit --staged --save
```

## Controlling cost

A full audit runs many agents (up to 16 by default) on Opus, so it can use a lot of tokens on a large repo. Ways to keep it smaller:

- **Narrow the scope:** a path, `--diff`, or `--staged`.
- **Pick fewer dimensions:** name only the flags you need.
- **`--fast`:** runs agents on Sonnet instead of Opus.
- **`--max-agents <N>`:** caps the total number of agents (default 16). Large dimensions get fewer, bigger slices.
- **`--no-split`:** one agent per dimension, no matter how much code there is.
- **`--<dim>=<N>`:** sets the agent count for one dimension, e.g. `--security=4` to go deeper on security.

## Reading the report

When the audit finishes, Claude shows a summary table and any Critical findings, and links the full report for Warnings and Info. Each finding has an ID (like `SEC-01`), a `file:line` location, and a suggested fix.

The report is written to `.audit-run/report.md` in your repo root and replaced on the next run. `.audit-run/` is added to `.git/info/exclude` automatically, so it never shows up in `git status`. Add `--save` to also write `.audit-report.md` in the audited folder. That file isn't ignored, so commit it or add it to `.gitignore`.

## Marking findings as intentional

If the audit keeps flagging something you've decided is fine, add it to `.audit-ignore.md` at your repo root. Each entry is one line: a file glob, the dimension, the pattern, and why it's acceptable.

```
- `src/generated/**` · TS · `any` in emitted types — output of openapi-typescript, regenerated on build. 2026-08-03
- `app/legacy/**` · UX · hardcoded hex colors — pre-design-system, frozen until the rewrite. 2026-08-03
```

Keep the format exact, since a script parses it. Always scope entries with a glob, and give a reason that could later turn out false ("regenerated on build", not "this is fine"). Future runs check whether the reason still holds.

Findings that match an entry are suppressed, and the report tells you how many were hidden. Run with `--no-ignore` to see everything.

Dimension codes: `PERF`, `SEC`, `A11Y`, `TEST`, `ARCH`, `TS`, `DEPS`, `RES`, `UX`, `BLOAT`, `ANLT`.

## Contributing

The orchestration logic is in `scripts/audit.py`, and each dimension's agent is in `agents/<dim>.md`. Run the tests with:

```
python3 scripts/tests/test_audit.py
```

## License

MIT
