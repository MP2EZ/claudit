## Audit contract

These rules bind every /audit agent and override any output format in your own agent definition.

**Job.** Audit: analyze and report. Do not edit, create, or delete any file except your findings file.

**Method**
- Stay inside your partition (above). Sibling agents cover everything else.
- Locate before reading: Grep/Glob for candidates, then read what the findings need. If your agent definition or focus calls for a deeper pass (e.g. reading files end-to-end), do it.
- Verify each finding is real before reporting it; check for mitigations elsewhere.
- Report only findings with confidence >= 75.
- A cross-file finding is anchored at ONE location; list the other locations in its description.
- Accepted patterns listed in this brief are not findings. If an acceptance's stated reason no longer holds, report ONE Info finding whose description starts with `[stale-acceptance]`, not every instance.

**Output.** Write the findings file named in this brief in exactly this format (a script parses it):

```
## <Dimension> Findings

### Critical
- **<PREFIX>-<S>-<NN>** (confidence: <0-100>) `<anchor>` — <description>
  → <specific fix>

### Warning
<same bullet format>

### Info
<same bullet format>

### Summary
<2-3 sentences>
```

- `<S>` is your slice index; `<NN>` counts from 01 within your file.
- `<anchor>` is `path:line` relative to the repo root (DEPS: `package@version`).
- Each finding is exactly one bullet line plus one `→` line.
- Keep empty severity headings. If nothing meets the threshold, say the dimension looks healthy in Summary.
- If your agent definition adds sections (e.g. Coverage Map, Metric Readiness), place them between Info and Summary.

Then reply with exactly one line and nothing else: `<slice id>: <c> critical, <w> warning, <i> info`
