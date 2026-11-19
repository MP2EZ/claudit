---
description: Multi-dimensional codebase audit across performance, security, accessibility, test coverage, architecture, TypeScript safety, design system compliance, and analytics coverage
argument-hint: "[path] [--diff <branch>] [--staged] [--fast] [--save] [--no-ignore] [--perf] [--security] [--a11y] [--test] [--arch] [--ts] [--deps] [--resilience] [--ux] [--bloat] [--analytics] [--all] [--no-split] [--max-agents <N>] [--<dim>=<N>]"
---

# Codebase Audit

You orchestrate a parallel audit. `audit.py` does the mechanical work: parsing arguments, resolving scope, allocating and partitioning agents, writing each agent's brief, and dedup/renumber/sort/report. Your job is to launch the agents, make the judgment calls the script lists for you, and present the result.

## 1. Plan

Run from the project directory:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/audit.py plan $ARGUMENTS
```

On `USAGE ERROR`, show it with the argument hint and stop. Otherwise relay the `SUMMARY` line and every `NOTE` line to the user verbatim. Notes cover capped allocation, skipped dimensions, and analytics widening.

## 2. Launch

Each `AGENT <id> | <subagent_type> | <model> | <description> | <prompt>` line is one Agent call. Make all of them in a single message, passing the fields through unchanged. Add nothing to the prompt: the brief it points to holds scope, partition, accepted patterns, and the output contract.

Each agent writes `.audit-run/findings/<id>.md` and replies with one line. If an agent returns its findings inline instead, Write them to that path unchanged. If an agent fails, re-launch it once.

## 3. Merge

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/audit.py merge
```

If plan printed `NOTE scope root is … merge with --dir <root>`, add that `--dir` to every merge call.

The script dedups within each dimension, renumbers, sorts, and writes the report. It then prints the decisions left for you. Make each one from the printed descriptions; don't open the findings files.

- `CROSS a + b`: the same location flagged by two dimensions. Combine them when they describe the same defect. Expected overlaps: ANLT PII-in-properties ↔ SEC data exposure; BLOAT dead exports ↔ ARCH coupling; BLOAT test bloat ↔ TEST, but only when both name the same defect.
- `IGNORE id | … | entry`: the finding sits inside an `.audit-ignore.md` entry's glob for its dimension. Suppress it if it *is* the accepted pattern.
- `BREAK`: a contract violation. Note it for the retro. Also check whether any slice reported findings outside its lens.

If you combined or suppressed anything, re-run once:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/audit.py merge --combine SEC-1-01+PERF-2-03 --suppress TS-1-04
```

Both flags take comma-separated lists. `--combine` keeps the higher-severity finding and credits both dimensions.

## 4. Output

The final merge prints `REPORT <path>`, the summary table, and the Critical findings. Show the user the summary table and the Critical findings, then link the report file for Warnings and Info. If a `SUPPRESSED` line printed, state the count and dimensions (suppression is never invisible) and mention `--no-ignore` once.

End with "Found [N] critical issues that should be addressed before merging." or, if there are none, "No critical issues found. [N] warnings worth reviewing."

## 5. Retro (conditional; most runs skip it)

If merge printed any `RETRO` line, or BREAK/lens violations occurred, or later in this session the user dismisses 2 or more findings in one dimension as intentional or already known, read `${CLAUDE_PLUGIN_ROOT}/audit/retro.md` and follow it. Otherwise the command ends at step 4.
