# /audit retro

Follow `~/.claude/protocols/learning-loop.md`. This file holds only what's specific to `/audit`. You got here because `audit.py merge` printed a `RETRO` line, or because the user dismissed findings (trigger 5).

## Triggers

`audit.py merge` computes triggers 1–4. Trigger 5 is yours to count.

| # | Trigger | Threshold | What it indicts |
|---|---------|-----------|-----------------|
| 1 | `--max-agents` capped demand | trimmed >= 50% of demanded agents | `DEFAULT_MAX_AGENTS` or `CHUNK_LOC` is miscalibrated for this repo's shape |
| 2 | A slice returned zero findings | while holding >= 1,000 LOC | the relevant-set rules or the partition axis |
| 3 | Duplicates dropped at within-dimension merge | >= 3 in one dimension | the file-chunk partition cut a module seam |
| 4 | An agent broke its contract | any `BREAK` line, or findings outside its lens (you judge this one) | the contract or the brief is ambiguous |
| 5 | The user dismissed findings as intentional or already known | >= 2 within one dimension, this session | route it (see below) |

Trigger 5 usually arrives *after* the report, once the user has read it. Fire the retro then, not at output time; a retro that runs before any reaction has no signal to learn from.

## Routing

Paths are relative to the plugin root (`~/.claude/local-plugins/m`, the source; the cache copy is regenerated on update).

| Trigger | Destination |
|---|---|
| 1, allocation | `scripts/audit.py`: `CHUNK_LOC`, `DEFAULT_MAX_AGENTS`, `_demand` |
| 2, empty slice | `scripts/audit.py`: `relevant_sets` (relevant set) or `partition` (axis) |
| 3, dedup churn | `scripts/audit.py`: `chunk_by_module` (module-seam rule) or `merge` |
| 4, contract break | `audit/contract.md`, or the brief text in `scripts/audit.py` (`write_brief`, `_partition_text`) |

Script edits keep `python3 scripts/tests/test_audit.py` green. Add a test for the case that fired.

For trigger 5:

| The dismissals were about… | Destination |
|---|---|
| a convention of **this repo** (generated files, a grandfathered legacy directory, a deliberate house pattern) | `.audit-ignore.md`. One entry per pattern. No skill edit. |
| a rule that would misfire on **any** repo | `audit/focus/<dim>.md` for perf, security, a11y, test, arch, ts, ux; `agents/<name>.md` for deps, resilience, bloat, analytics |

**Never edit `~/.claude/agents/*.md` from an audit retro.** Seven dimensions (perf, security, a11y, test, arch, ts, ux) run on global agents shared with all other work. Narrowing one to kill an audit false positive changes its behavior everywhere else. Audit-only corrections go in `audit/focus/`, which is injected into the brief and affects nothing outside this command.

Only `m:bloat-auditor`, `m:deps-auditor`, `m:resilience-auditor`, and `m:analytics-auditor` are owned by this plugin, so only they may be edited directly at `agents/*.md`.

## Writing `.audit-ignore.md`

Repo root, one entry per line, newest last:

```
- `<glob>` · <DIM> · <pattern> — <why it is acceptable>. <YYYY-MM-DD>
```

`<DIM>` is the finding prefix (PERF, SEC, A11Y, TEST, ARCH, TS, DEPS, RES, UX, BLOAT, ANLT). The script parses this format, so keep it exact.

Examples:

```
- `src/generated/**` · TS · `any` in emitted types — output of openapi-typescript, regenerated on build. 2026-08-03
- `app/legacy/**` · UX · hardcoded hex colors — pre-design-system, frozen until the rewrite. 2026-08-03
```

Rules for entries:
- **Scope every entry with a glob.** An unscoped entry silences the whole repo forever.
- **The reason must be falsifiable**, because a later run re-checks whether it still holds. "This is fine" is not a reason; "regenerated on build" is.
- Propose entries under the same approval gate as a skill edit: show them, wait, then write.

## Never capture

The findings themselves. The report is the output; `.audit-ignore.md` records only the *pattern* accepted and *why*. An entry reading "SEC-03 was a false positive" teaches a future run nothing it can use.
