# audit — `/audit` for Claude Code

`/audit` audits a codebase in parallel across up to 11 dimensions, dedups the findings, and writes one report.

| Flag | Dimension | Default |
|---|---|---|
| `--perf` | Performance | ✓ |
| `--security` | Security | ✓ |
| `--a11y` | Accessibility | ✓ |
| `--test` | Test coverage | ✓ |
| `--arch` | Architecture | ✓ |
| `--ts` | TypeScript safety | ✓ |
| `--deps` | Dependency health | |
| `--resilience` | Error handling | |
| `--ux` | Design-system compliance | |
| `--bloat` | AI-generated bloat | |
| `--analytics` | Analytics coverage | |

`--all` runs every dimension.

## Install

Requires `python3` on your PATH.

```
/plugin marketplace add MP2EZ/claude-audit
/plugin install audit@audit-plugin
```

Restart Claude Code, then run `/audit` (or `/audit:audit` if another plugin also has an `audit` command) from a project directory.

## Usage

```
/audit                      # default dimensions, whole repo
/audit src/api --security   # one path, one dimension
/audit --diff main          # only files changed vs main
/audit --all --fast         # every dimension on Sonnet instead of Opus
```

Other flags: `--staged`, `--save`, `--no-ignore`, `--no-split`, `--max-agents <N>`, `--<dim>=<N>`.

Each run launches many Opus agents (up to 16 by default), so a full audit uses a lot of tokens. Use `--fast`, a path, or `--diff` to keep it smaller.

## Accepting findings

Add an `.audit-ignore.md` at the repo root to mark patterns as intentional. The format is in [`audit/retro.md`](audit/retro.md#writing-audit-ignoremd).

## Tests

```
python3 scripts/tests/test_audit.py
```
