# /audit blind spots: what each dimension can and can't see

2026-11-22. Test case: Citytown (`~/dev/citytown`, HEAD on 2026-11-22). That is 468 loaded files and 60.8k LOC. Of those, 28.6k are tests and 24.8k are runtime source.

**Status: all six applied 2026-11-22 on approval, one commit each (8261aaa…de3ebb8).** The patches in [`blind-spots-2026-11-22/`](blind-spots-2026-11-22/) are kept as the record of what was proposed. `scripts/tests/test_audit.py` passes 65 tests (57 before), and every new test failed on the pre-patch code.

All numbers come from `audit.py plan ~/dev/citytown …` run before and after the patches. LOC is read from disk.

## Ranked recommendation

| # | Fix | Patch | Size | Why first |
|---|---|---|---|---|
| 1 | Merge stops dropping distinct findings at one line | `1-merge` | +33/−9 | Data loss in the report. Tiny diff, no allocation change. |
| 2 | Test: audit all runtime source, map tests by import, two depths | `3-test` (needs `2-noise`) | +92/−12 | 77% of runtime LOC is never examined today, and the skipped files hold 72% of recent churn. |
| 3 | One noise filter for docs, mocks and previews; ts drops tests | `2-noise` | +24/−6 | 41–44% of the test, a11y and ux slices are noise. It caused the false `RETRO 2`, and doubled ts demand (14→7). |
| 4 | a11y sees DOM-built UI and CSS; resilience sees storage and parsing | `4-ui` | +20/−5 | a11y misses the canvas, the admin pages and every stylesheet. |
| 5 | Analytics finds event sites by import; demand per `CHUNK_LOC` | `5-analytics` | +21/−5 | The event vocabulary, the wire and the ingest endpoint are all outside the set today. |
| 6 | File-chunk briefs may read across the seam | `6-seam` | +9/−1 | App.tsx and store.ts land in different slices in four dimensions. |

Allocation, cumulative as the patches apply. (The suffixes mean: after 1-merge, after 2-noise, and so on.)

| Run | Before | +1 | +2 | +3 | +4 | +5 (all) |
|---|---|---|---|---|---|---|
| `--test --analytics --max-agents 24` (the run that prompted this) | 4 (test×3, anlt×1) | 4 | 3 | 5 (test×4) | 5 | **6** (test×4, anlt×2) |
| `--all --max-agents 200` (uncapped demand) | 53 | 53 | 44 | 46 | 47 | **48** |
| `--all --max-agents 24` | 24 (test×2, ts×3, bloat×3) | 24 | 24 | 24 (test×3) | 24 | 24 (test×2, anlt×2) |

The agent count barely moves. What changes is what those agents read: noise out, real source in.

---

## 1. Merge drops a distinct finding when two share a line *(observed: ANLT-1-02 / ANLT-1-03)*

**Evidence.** `merge()` dedups within a dimension on the normalized anchor alone (`scripts/audit.py:603-613`). It keeps one finding per `path:line` and drops the rest, even when both come from the same agent. Analytics ran as one slice. ANLT-1-02 ("visit carries no push attribution") and ANLT-1-03 ("events carry no platform") both anchor `src/ui/game/store.ts:602`, so one was silently deleted. The dedup-churn retro excludes same-slice and sub-lens pairs from its count, but the drop still happens. Nothing in the output says a finding was lost.

**Severity: high.** It loses real findings without a trace. It hits hardest where findings cluster: a hub like `store.ts:602` (where every event is built), a god-file line in arch, or one `track()` call with several data-quality defects. Sub-lens dimensions (arch, analytics, bloat's cross-file lens) lose a second lens's finding whenever two lenses meet on one line. That is exactly when their findings differ.

**Fix (`1-merge.patch`).** A slice never duplicates itself. Same-anchor findings from one slice all survive. Across sub-lens slices both survive, and the pair is printed as a `CROSS` candidate, so the orchestrator uses the existing `--combine` if they are one defect. Only cross-slice file-chunk duplicates are dropped, and those are already contract breaks.

```diff
-    findings = []
+    findings, dup_candidates = [], []
     for dim, fs in by_dim.items():
-        best = {}
+        at = defaultdict(list)
         for f in fs:
-            a = _norm_anchor(f["anchor"], root)
-            if a not in best or _rank(f) < _rank(best[a]):
-                best[a] = f
-        dups = sum(1 for f in fs if (b := best[_norm_anchor(f["anchor"], root)]) is not f
-                   and f["sid"] != b["sid"] and "sub-lens" not in (f["smode"], b["smode"]))
+            at[_norm_anchor(f["anchor"], root)].append(f)
+        dups = 0
+        for group in at.values():
+            win = min(group, key=_rank)
+            # One agent's findings at one line are distinct defects; only another slice can duplicate them.
+            for f in group:
+                if f["sid"] == win["sid"]:
+                    findings.append(f)
+                elif "sub-lens" in (f["smode"], win["smode"]):
+                    findings.append(f)  # lenses overlap by design: the orchestrator decides, via --combine
+                    dup_candidates.append((win["key"], f["key"]))
+                else:
+                    dups += 1
         if dups >= 3:
             retro.append(f"3 dedup churn: {dups} duplicates dropped in {DIMS[dim].prefix}")
-        findings += best.values()
```

`commands/audit.md` changes so a `CROSS` line can also mean "two lenses of one dimension". New tests: `test_same_slice_findings_at_one_anchor_both_survive` and `test_sub_lens_overlap_is_kept_and_offered_for_combine`. **Allocation:** unchanged.

---

## 2. `--test` skips any file whose stem matches a test, however thin the test

**Evidence.** `relevant_sets` uses `untested or src`, where `untested` means no test anywhere shares the file's stem.

| | Files | LOC |
|---|---|---|
| Runtime source (`src/`, `worker/`, `public/`, `ios/`; no docs, scripts, migrations or config) | 158 | 24,802 |
| Today's test set | 91 | 9,486 |
| …of which noise (docs mocks, `design-library/**/preview.html`, `scripts/store`, migrations, Vite config) | 54 | 3,897 (41%) |
| …of which runtime source | 37 | 5,589 (**23%** of runtime) |
| Runtime source never examined | 121 | 19,213 |

The stem rule fails both ways:

- **False "tested".** `src/sim/*` is tested by invariant files named `INV-*`, so stems rarely match. Yet `src/sim/resolve.ts`, `public/sw.js`, `src/ui/game/report.ts` and `worker/flags.ts` have a same-stem test that **doesn't import them**. They are skipped as tested, though no test touches them.
- **False "untested".** 22 files / 4.5k LOC have no same-stem test but are imported by tests. They are sent for a cold "untested" audit, and the agent must rediscover their tests.
- **Weak tests are never examined.** Once a file counts as tested it is out of scope. `store.ts` (2,310 LOC) has 6 direct tests totalling 1,127 LOC, and `render/draw/props.ts` (349 LOC) has one 51-LOC test. Neither is in today's set. The agent definition already asks for "weak tests" and "branch gaps", but it is never handed the files where those live.
- **Churn.** Over the last 90 days (`git log --numstat`, runtime source), today's set holds 28% of the line churn. Of the 10 most-churned TS files, 3 are in it. `store.ts`, `state.ts`, `board.ts`, `sound.ts`, `placement.ts` and `guide.ts` are not.
- **Scope bug, same root.** In path mode the sibling-test lookup uses only in-scope files (`repo_files = … if mode == "diff" else files`). So `/audit src/` sees none of `tests/` and calls everything untested.

**Severity: high.** On a well-tested repo the test dimension audits the leftovers. It is blind to the code that changes most, and to the thin-test failure mode it was written to catch.

**Options weighed (Citytown, uncapped):**

| Option | Set | Agents | Verdict |
|---|---|---|---|
| A. Today (stem, untested only) | 91 f / 9.5k | 3 | Blind to tested code; 41% noise |
| B. A minus noise | 37 f / 5.6k | 2 | Cleaner, still blind |
| C. Import-graph mapping, untested only | 19 f / 1.6k | 1 | Accurate but tiny: Citytown is well tested, so "untested" is the wrong axis |
| D. All runtime source at full depth | 158 f / 24.8k | 7 | Right coverage. Costs 7, and is capped to 2 under `--all --max-agents 24` |
| **E. All runtime source, two depths (proposed)** | 158 f / 24.8k (13.2k weighted) | **4** | Untested files at full depth; tested files at half weight, checked against their mapped tests |
| F. E plus churn weighting | same | 4 | Not proposed. Churn would only reorder files inside slices, and partitions are disjoint, so nothing would be dropped or added. It's worth revisiting if the cap ever drops files. |

Measured coverage (`vitest --coverage`) would beat any heuristic, but `plan` shouldn't run a test suite. A later step could read an existing `coverage/coverage-summary.json` if one is present.

**Fix (`3-test.patch`, on top of `2-noise`).**
- `import_graph(files)` resolves relative JS/TS imports (`./x.js` → `x.ts`, index files).
- `tested_by(repo_files, graph)` walks from each named test file (`*.test.*`, `*.spec.*`, `test_*.py`, `*_test.go`) through test helpers (`tests/support/…`) to the sources they reach. Non-JS files fall back to stem match.
- The test set is all runtime source in testable languages (`.html`, `.sql` and `.sh` are dropped).
- Files some test reaches count at `TESTED_WEIGHT = 0.5` for allocation and partition.
- Briefs mark each file: `src/x.ts — no test reaches it`, or `src/x.ts — tests: a.test.ts, b.test.ts (+n more)`.
- Path-mode scopes now load the whole repo for the lookup.
- `agents/test.md` gets two modes in place of "files with no sibling test". For an untested file: grep, then report. For a tested file: list its branches, read the named tests' assertions, and report what nothing asserts. Never call it untested.

```diff
-        "test": untested or src,
+        "test": [f for f in runtime if _ext(f.path) in TESTABLE_EXT],
…
-    repo_files = load(root, repo_paths) if mode == "diff" else files
+    repo_files = load(root, repo_paths) if mode == "diff" or target != root else files
     rel = relevant_sets(files, repo_files)
+    tests_of = tested_by(repo_files, import_graph(repo_files)) if "test" in o["dims"] else {}
+    rel["test"] = [f._replace(loc=math.ceil(f.loc * TESTED_WEIGHT)) if f.path in tests_of else f for f in rel["test"]]
```

New tests: `test_test_dimension_takes_all_runtime_source`, `test_test_dimension_skips_tooling_and_markup`, and `test_tests_map_to_sources_by_import_not_stem`. The last one covers an INV-style test reached through a helper, a same-stem test that doesn't import its namesake, and the Python stem fallback.

**Allocation:** test 3 agents / 91 files / 9.5k LOC (5.6k real) → **4 agents / 158 files / 24.8k LOC** (3.3k weighted per agent). Under `--all --max-agents 24` it gets 2–3, as every dimension is trimmed.

*Weakness to know:* an index/barrel file that a test imports marks everything it re-exports as tested. That only changes the depth, never exclusion, so the cost is half-weight on those files.

---

## 3. Docs, mocks and previews fill slices in seven dimensions; tests double ts demand

**Evidence.** `NON_RUNTIME_RE` (docs/scripts/config) applies to perf only. Every other dimension takes `docs/audits/**/mocks/*.html|js|py`, `docs/ERD.html` and `design-library/components/*/preview.html`. Each preview sits in its own directory, so `chunk_by_module` scatters them one or two per slice as bin-packing filler. In the uncapped plan, `design-library` or `docs` strays sit in 7 of 7 perf file-chunks, 3 of 3 security file-chunks and 12 of 14 bloat file-chunks.

| Dim | Today (files / LOC) | Noise in it | After `2-noise` |
|---|---|---|---|
| a11y | 42 / 5.6k | docs + previews 2.4k (**44%**) | (see §4) |
| ux | 50 / 7.8k | docs + previews 3.2k (**42%**) | 32 / 7.3k |
| test | 91 / 9.5k | 3.9k (**41%**) | (see §2) |
| security | 92 / 11.2k | 1.7k (15%) | 69 / 9.5k |
| arch | 229 / 30.2k | 2.7k (9%) | 195 / 27.4k |
| bloat | 462 / 59.0k | 2.8k (5%) | 428 / 56.2k |
| perf | 189 / 26.1k | previews, migrations, `.d.ts` 1.0k (4%) | 163 / 25.1k |
| **ts** | **390 / 53.8k** | **tests 28.9k (53%)** | **157 / 24.9k** |

**Severity: medium.** Three costs:
- **False retros.** TEST-2 (3,152 LOC, mostly mocks, previews and store scripts) returned nothing in 20 s and fired `RETRO 2 empty slice`. That is a false signal pointing the retro at the wrong rule.
- **Inflated demand.** ts demanded 14 agents, half of them for test files. Under a cap, the trim loop takes from the busiest dimensions, so ts and bloat inflation drags perf down with them.
- **Diluted slices.** Real files share their slice with HTML mockups.

**Fix (`2-noise.patch`).**
- `NOISE_RE` (docs, examples, mocks, fixtures, stories, `preview(s)`) is dropped from every dimension except deps.
- `NON_RUNTIME_RE` = noise + scripts, migrations, bench, `*.config.*` and `.d.ts`. It applies where runtime behavior is the question: perf and test, plus a11y and ux via `ui` in §4.
- ts takes source only.
- Security keeps `scripts/`, because `update-signing`, `vapid` and `update-key` handle keys. Arch keeps it too, since its lib modules are real code.

```diff
-NON_RUNTIME_RE = re.compile(r"(^|/)(docs|scripts)/|(^|/)[^/]*\.config\.[cm]?[jt]s$")
+NOISE_RE = re.compile(r"(^|/)(docs|examples?|mocks?|__mocks__|fixtures?|stories|storybook)/|\.stories\.[jt]sx?$|"
+                      r"(^|/)previews?(/|\.)")
+NON_RUNTIME_RE = re.compile(NOISE_RE.pattern + r"|(^|/)(scripts|migrations|bench)/|(^|/)[^/]*\.config\.[cm]?[jt]s$|\.d\.ts$")
…
-    code = [f for f in files if _ext(f.path) in CODE_EXT and not f.path.endswith(".min.js")]
+    code = [f for f in files if _ext(f.path) in CODE_EXT and not f.path.endswith(".min.js") and not NOISE_RE.search(f.path)]
…
-        "ts": [f for f in code if _ext(f.path) in {".ts", ".tsx"}],
+        "ts": [f for f in src if _ext(f.path) in {".ts", ".tsx"}],
```

New tests: `test_docs_mocks_and_previews_leave_every_dimension` and `test_ts_skips_tests`.

**Allocation:** uncapped demand 53 → 44 (ts 14→7, test 3→2, a11y 2→1). `--test --analytics`: 4 → 3, since test shrinks to its real 5.6k, until §2 lands.

*Judgment call:* dropping tests from ts means `as any` in fixtures goes unreported. That is the usual trade, but if you want test typing audited, add tests back at a weight (as §2 does) rather than at full LOC.

---

## 4. a11y can't see DOM-built UI, the canvas, or CSS

**Evidence.** `ui` is `.tsx/.jsx/.vue/.svelte/.html`, plus `.js/.ts` with a JSX return. Citytown renders the board to a canvas and builds admin, guide and harness pages with `document.createElement`. The a11y set had 7 `.tsx` files (2.8k LOC) of real UI. It had none of `canvas.ts`, `render/draw/board.ts`, `src/admin/page/*`, `src/ui/sheet.ts`, `src/ui/brand.ts` or `harness.ts`, and no stylesheet. The other 2.4k of its 5.6k was mocks (§3). Focus rings, contrast, `prefers-reduced-motion` and target sizes live in `game.css` (the 5th most-churned file), and a11y can't see it. Resilience has a smaller gap: `ASYNC_RE` misses `localStorage`, `JSON.parse` and service-worker registration, which are this app's save and boot failure points.

**Severity: high for a11y** on any canvas or vanilla-DOM app. A canvas game with no accessible alternative, or a focus outline removed in CSS, is unreachable by the auditor. **Low for resilience**, which gains 3 files / 125 LOC (`crash-wire.ts`, `src/ui/main.ts` and `vite.library.config.ts`).

**Fix (`4-ui.patch`).** `DOM_UI_RE` (`createElement`, `innerHTML =`, `insertAdjacentHTML`, `tabIndex`, `setAttribute('aria-…'|role|tabindex)`, `getContext('2d')`) marks `.js/.ts` files as UI. The ui list is built from `runtime`, and a11y adds styles. `ASYNC_RE` gains storage, `JSON.parse`, `navigator.serviceWorker` and global `error`/`unhandledrejection` listeners. ux inherits the wider `ui`.

New tests: `test_a11y_takes_ui_files_and_styles`, `test_a11y_takes_dom_built_ui` and `test_resilience_takes_storage_and_parsing`.

**Allocation:** a11y 42 f / 5.6k (2.8k real) → **32 f / 7.3k**: 2.8k tsx, 2.8k DOM-built ts, 1.4k CSS and 0.3k HTML pages. That is 1 → 2 agents. ux 50 f / 7.8k → 32 f / 7.3k.

---

## 5. Analytics: 1 agent, 5 files, and the event pipeline isn't in them

**Why 1 agent.** Analytics doesn't use `ceil(loc / CHUNK_LOC)`. `_demand` has a step function: `3 if loc > 20000 else 2 if loc > 8000 else 1`. That made sense when each extra agent was one more lens over the same files, but it means 8k LOC of event code gets one agent covering three competencies. Every other dimension would give that 2.

**What the set missed.** The set holds `App.tsx` and `Guide.tsx` (via `onClick`), `store.ts` (via `.track(`), `worker/usage.ts` (via `posthog`) and **`sound.ts`, a false positive**: bare `amplitude` matched "a fast amplitude wobble". It misses the pipeline:
- `src/ui/usage.ts`, the event vocabulary (254 LOC);
- `usage-wire.ts`, the client sender;
- `port.ts`, which defines `track`;
- `native.ts` and `production.ts`, which wire usage on;
- `worker/index.ts`, the ingest route;
- `canvas.ts`, where the pointer gestures that become `place_*` events start.

None match because the vocabulary is the app's own ("usage"), the sends are `port.track(sent)` property definitions, and interactions are `addEventListener('pointerdown')`, not `onClick`.

**Severity: medium.** The data-quality and identity lens can't check the vocabulary's types or the ingest validation, and the behavioral lens can't see the gesture surface. Findings at `store.ts` still come, so the gap shows only as missing findings.

**Fix (`5-analytics.patch`).**
- `VENDOR_RE`: case-insensitive, word-bounded vendor names; bare `amplitude` becomes `@amplitude/` or `amplitude.init|track|getInstance`.
- An **import rule**: any file importing an analytics hub (path matches `analytics|tracking|telemetry`, or mentions a vendor) is an event site, whatever it names its send.
- `INTERACTION_RE` gains DOM `addEventListener` click, pointer, touch, key, submit and change.
- `_demand` becomes `min(3, ceil(loc / CHUNK_LOC))`.

```diff
+    # files that import an analytics module are its event sites, whatever they call the send
+    hubs = {f.path for f in src if ANALYTICS_PATH_RE.search(f.path) or VENDOR_RE.search(f.text)}
+    wired = {p for p, deps in import_graph(src).items() if deps & hubs}
…
-        return 3 if loc > 20000 else 2 if loc > 8000 else 1
+        return min(3, max(1, math.ceil(loc / CHUNK_LOC)))  # lenses, one per CHUNK_LOC
```

New tests: `test_analytics_takes_importers_of_the_event_module_and_skips_lookalikes`, plus a 5,143-LOC case in `test_scales_with_loc_and_special_rules`.

**Allocation:** 1 agent / 5 files / 5.2k (0.6k false positive) → **2 agents / 30 files / 7.5k**. It now includes the whole pipeline above. It also pulls in admin and dev-harness click handlers (`src/admin/page/*`, `harness.ts`, `sound-demo.ts`, ~1k LOC) and the PostHog-backed flag modules. The flags are relevant (experiment exposure); the admin pages are noise.

*Weakness to know:* on Citytown the hub rule fires partly because `usage.ts` mentions "PostHog" in a comment. A repo with its own vocabulary, no vendor name and no `analytics` path still needs a seed. A one-line `.audit-ignore.md`-style "analytics module: `<glob>`" hint would close that. It's not proposed here.

---

## 6. File-chunk seams split tightly coupled files

**Evidence.** `chunk_by_module` keeps directories whole unless one exceeds the target. It then splits the directory in alphabetical order and spreads the pieces by LPT bin-packing. In the uncapped `--all` plan, `src/ui/game/App.tsx` (1.9k) and `store.ts` (2.3k), the view and the store it renders from, land in different slices in perf, ts, resilience and bloat. perf×7 cuts 78 of 210 same-directory import edges. I prototyped an import-ordered split (start at the largest file, pull in its neighbours) and roll-up of small sibling directories. Cut edges fell only 78 → 68, because App and store together (4.2k) exceed the 3.6k target. Re-partitioning can't fix this at this chunk size.

**Severity: low to medium.** A perf agent holding App.tsx can't see that a store update triggers a full re-render. A resilience agent holding store.ts can't see the caller that swallows its throw. The contract's "stay inside your partition" reads as "don't look outside" too.

**Fix (`6-seam.patch`).** Change the brief, not the partition. File-chunk agents may read what their files import or are called by, outside their list, when a finding depends on it. They still anchor only in their own files, so the out-of-chunk `BREAK` check stays as is.

New test: `test_file_chunk_brief_allows_reading_across_the_seam`. **Allocation:** unchanged.

---

## Round 2: security, architecture, performance, bloat (applied, 211db1e…4737f2c)

My first pass called these four "noise only". With the import graph from §2 they had real gaps. All five fixes are applied, one commit each. The suite passes 75 tests.

| # | Gap on Citytown | Fix | Effect |
|---|---|---|---|
| 7 | Security: `worker/ingest.ts` went to SEC-3 and its validator `src/admin/valid-save.ts` to SEC-2. `src/admin/digest.ts` (run by the server on client saves), `src/ui/usage.ts` (the worker validates event names against it) and `src/ui/pushes.ts` were outside the set: no trigger words. | Whatever a server or route file imports joins the set (`is_boundary`). `boundary_bundles` keeps each server directory together with its imports, the way `chunk_by_module` keeps a directory together. | The worker and every module it imports are now in one slice (SEC-1). Security goes from 69 files / 9.5k LOC to 82 / 11.8k, still 3 agents. |
| 8 | Arch was told to rebuild the import graph with grep, with 2 agents each facing 27k LOC. Bloat's cross-file agent swept 428 files by reading. | `graph_facts` writes `.audit-run/graph.md`: import cycles, fan-in/out, largest files, imports across areas, orphan and test-only modules, and unused exports. The arch briefs and bloat's cross-file brief point to it. `agents/arch.md` and `agents/bloat.md` start from it and verify entries before reporting. | Citytown has 1 cycle (`render/draw`: along · board · frame · motion), `worker/` importing `src/ui` 4 times and `src/admin` 5 times, 3 orphan candidates, 1 test-only module, 133 exports only tests use, and 109 needless `export`s. Files named by an HTML script tag, a `package.json` script, wrangler `main` or a registered URL don't count as orphans, which cut the false alarms from 13 to 3. |
| 9 | Security config was never loaded: `public/_headers` (CSP), `wrangler.jsonc`, `capacitor.config.json`, `Info.plist` and three CI workflows, about 350 lines. | `SECURITY_CONFIG_RE` files are loaded and go to security only. | 7 more files in security, none anywhere else. |
| 10 | Perf excluded CSS and `vite.config.ts`, though its agent asks about layout animations and code splitting. | Perf adds stylesheets and bundler config (`BUNDLER_RE`). | Perf goes from 163 files / 25.1k LOC to 170 / 26.7k, still 7 agents. |
| 11 | Tests were 49% of bloat's LOC and drove demand to 15 agents. | `weigh()` counts test files at `BLOAT_TEST_WEIGHT = 0.5`, as it already does for tested sources in the test dimension. | Bloat goes from 15 agents to 11 with the same files. |

Allocation after round 2:
- **`--all` uncapped:** 48 → **44** agents.
- **`--all --max-agents 24`:** still 24 agents. Demand drops from 48 to 44, so the cap now trims 45% instead of 55%, and `RETRO 1 allocation` (threshold 50%) no longer fires.
- **`--test --analytics`:** unchanged at 6.

## Checked, no change proposed

- **deps.** One agent over the manifests. No blind spot found.
- **Cap trimming** (`allocate`) trims the dimension with the most agents. Under `--all --max-agents 24`, depth ends up very uneven: bloat runs at 19.7k LOC per agent and a11y at 2.8k. I prototyped a water-fill (trim where the post-trim LOC per agent stays lowest). It shifted agents from security and ux to bloat and ts, with no clear win, so I'm not proposing it.
- **`.audit-ignore.md` whole-dimension acceptances** still fill slices. For example, `scripts/store/** · TEST` and `vite.config.ts · TEST` were sent to TEST-2 though accepted. §3 makes this moot for Citytown. In general, a file whose glob is accepted for a dimension could leave that dimension's demand and appear only for the stale-acceptance check.

## Reproduce

```bash
python3 scripts/audit.py plan ~/dev/citytown --all --max-agents 200   # before
# (patches already applied; to compare, check out 9015c13 for "before")
python3 scripts/tests/test_audit.py
python3 scripts/audit.py plan ~/dev/citytown --all --max-agents 200   # after
```

`plan` overwrites `~/dev/citytown/.audit-run/`, which is scratch. The briefs there show the new `tests:` annotations, and `graph.md` holds the module-graph facts.
