import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import audit  # noqa: E402
from audit import FileInfo  # noqa: E402


def fi(path, loc=100, text=""):
    return FileInfo(path, loc, text)


class ParseArgsTest(unittest.TestCase):
    def test_defaults_select_six_dimensions(self):
        o = audit.parse_args([])
        self.assertEqual(o["dims"], ["perf", "security", "a11y", "test", "arch", "ts"])
        self.assertEqual(o["max_agents"], 16)
        self.assertFalse(o["no_split"])

    def test_dimension_flags_select_only_those(self):
        self.assertEqual(audit.parse_args(["--ux", "--perf"])["dims"], ["perf", "ux"])

    def test_all_selects_eleven(self):
        self.assertEqual(len(audit.parse_args(["--all"])["dims"]), 11)

    def test_count_override_selects_dimension(self):
        o = audit.parse_args(["--perf=3", "--deps=4"])
        self.assertEqual(o["dims"], ["perf", "deps"])
        self.assertEqual(o["overrides"], {"perf": 3, "deps": 4})

    def test_scope_and_value_flags(self):
        o = audit.parse_args(["src/auth", "--diff", "dev", "--max-agents", "24", "--fast", "--save", "--no-ignore"])
        self.assertEqual(o["path"], "src/auth")
        self.assertEqual(o["diff"], "dev")
        self.assertEqual(o["max_agents"], 24)
        self.assertTrue(o["fast"] and o["save"] and o["no_ignore"])

    def test_unknown_flag_is_usage_error(self):
        with self.assertRaises(audit.UsageError):
            audit.parse_args(["--perff"])

    def test_missing_value_is_usage_error(self):
        with self.assertRaises(audit.UsageError):
            audit.parse_args(["--diff"])


class RelevantSetsTest(unittest.TestCase):
    def setUp(self):
        self.files = [
            fi("src/app/page.tsx", 200, "export default () => { return (<div onClick={go}/>) }"),
            fi("src/lib/api.ts", 300, "export async function get() { await fetch(u) }"),
            fi("src/lib/api.test.ts", 50, "test()"),
            fi("src/lib/math.ts", 80, "export const add = (a, b) => a + b"),
            fi("src/styles/theme.css", 40, ""),
            fi("README.md", 10, ""),
        ]
        self.rel = audit.relevant_sets(self.files, self.files)

    def paths(self, dim):
        return [f.path for f in self.rel[dim]]

    def test_test_dimension_takes_all_runtime_source(self):
        self.assertEqual(self.paths("test"), ["src/app/page.tsx", "src/lib/api.ts", "src/lib/math.ts"])

    def test_test_dimension_skips_tooling_and_markup(self):
        files = [fi("scripts/store/film.mjs", 300, "run()"), fi("ios/App/App/AppDelegate.swift", 40, "class A {}"),
                 fi("migrations/0001.sql", 20, "create table t"), fi("index.html", 40, "<html>"),
                 fi("src/sim/state.ts", 900, "export const s = 1")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual([f.path for f in rel["test"]], ["ios/App/App/AppDelegate.swift", "src/sim/state.ts"])

    def test_tests_map_to_sources_by_import_not_stem(self):
        files = [fi("src/sim/state.ts", 900, "export const s = 1"),
                 fi("src/sim/resolve.ts", 90, "export const r = 1"),
                 fi("src/ui/store.ts", 900, "import { s } from '../sim/state.js'"),
                 fi("tests/support/world.ts", 50, "import { s } from '../../src/sim/state.js'"),
                 fi("tests/invariants/INV-12-growth.test.ts", 80, "import { world } from '../support/world'"),
                 fi("tests/resolve.test.ts", 30, "import { x } from '../src/ui/store'"),
                 fi("app/models.py", 40, "x = 1"), fi("tests/test_models.py", 20, "from app.models import x")]
        m = audit.tested_by(files, audit.import_graph(files))
        self.assertEqual(m["src/sim/state.ts"], {"tests/invariants/INV-12-growth.test.ts"})
        self.assertEqual(m["src/ui/store.ts"], {"tests/resolve.test.ts"})
        self.assertNotIn("src/sim/resolve.ts", m)   # stem matches resolve.test.ts, but no test imports it
        self.assertEqual(m["app/models.py"], {"tests/test_models.py"})

    def test_a11y_takes_ui_files_and_styles(self):
        self.assertEqual(self.paths("a11y"), ["src/app/page.tsx", "src/styles/theme.css"])

    def test_a11y_takes_dom_built_ui(self):
        files = [fi("src/admin/card.ts", 150, "const b = document.createElement('button')"),
                 fi("src/ui/game/canvas.ts", 240, "const ctx = el.getContext('2d')"),
                 fi("src/sim/state.ts", 900, "export const s = 1")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual([f.path for f in rel["a11y"]], ["src/admin/card.ts", "src/ui/game/canvas.ts"])

    def test_resilience_takes_storage_and_parsing(self):
        files = [fi("src/ui/save.ts", 80, "const s = JSON.parse(localStorage.getItem(k))")]
        self.assertEqual([f.path for f in audit.relevant_sets(files, files)["resilience"]], ["src/ui/save.ts"])

    def test_resilience_takes_async_files(self):
        self.assertEqual(self.paths("resilience"), ["src/lib/api.ts"])

    def test_security_takes_trust_boundary_files(self):
        self.assertEqual(self.paths("security"), ["src/app/page.tsx", "src/lib/api.ts"])

    def test_security_keeps_server_dirs_and_queries(self):
        files = [fi("worker/saves.ts", 100, "db.prepare(q)"), fi("worker/util.ts", 50, "export const x = 1"),
                 fi("src/sim/rng.ts", 50, "export const next = (s) => s * 2")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual([f.path for f in rel["security"]], ["worker/saves.ts", "worker/util.ts"])

    def test_security_takes_what_server_files_import(self):
        files = [fi("worker/ingest.ts", 200, "import { validSave } from '../src/admin/valid-save.js'"),
                 fi("src/admin/valid-save.ts", 120, "export const validSave = (s) => s.v === 1"),
                 fi("src/admin/chart.ts", 80, "export const bar = 1")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual([f.path for f in rel["security"]], ["worker/ingest.ts", "src/admin/valid-save.ts"])

    def test_security_takes_attack_surface_config_and_nothing_else_does(self):
        files = [fi("public/_headers", 29, "Content-Security-Policy: default-src 'self'"),
                 fi("wrangler.jsonc", 75, "{}"), fi(".github/workflows/deploy.yml", 52, "secrets.CF_TOKEN"),
                 fi("ios/App/App/Info.plist", 76, "<plist>"), fi("src/sim/state.ts", 900, "export const s = 1")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual([f.path for f in rel["security"]],
                         ["public/_headers", "wrangler.jsonc", ".github/workflows/deploy.yml", "ios/App/App/Info.plist"])
        for dim in ("perf", "arch", "bloat", "ts", "test"):
            self.assertNotIn("wrangler.jsonc", [f.path for f in rel[dim]], dim)

    def test_load_reads_security_config(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "wrangler.jsonc").write_text("{}\n")
            (Path(d) / "notes.txt").write_text("x\n")
            self.assertEqual([f.path for f in audit.load(Path(d), ["wrangler.jsonc", "notes.txt"])], ["wrangler.jsonc"])

    def test_bloat_includes_tests_perf_does_not(self):
        self.assertIn("src/lib/api.test.ts", self.paths("bloat"))
        self.assertNotIn("src/lib/api.test.ts", self.paths("perf"))

    def test_perf_skips_docs_scripts_and_config(self):
        files = [fi("docs/ERD.html", 500, "<html>"), fi("scripts/release.mjs", 200, "run()"),
                 fi("eslint.config.js", 40, "export default {}"), fi("src/ui/store.ts", 300, "export const s = 1")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual([f.path for f in rel["perf"]], ["src/ui/store.ts"])
        self.assertIn("scripts/release.mjs", [f.path for f in rel["bloat"]])

    def test_docs_mocks_and_previews_leave_every_dimension(self):
        files = [fi("docs/audits/x/mocks/brand.html", 400, "<button onclick=go()>"),
                 fi("design-library/components/Chip/preview.html", 40, "<div>"),
                 fi("design-library/components/preview.css", 40, ""),
                 fi("src/ui/chip.tsx", 100, "export default () => { return (<b onClick={() => fetch(u)}/>) }")]
        rel = audit.relevant_sets(files, files)
        for dim in ("perf", "security", "a11y", "test", "arch", "ux", "bloat", "analytics"):
            self.assertEqual([f.path for f in rel[dim]], ["src/ui/chip.tsx"], dim)

    def test_ts_skips_tests(self):
        self.assertNotIn("src/lib/api.test.ts", self.paths("ts"))
        self.assertIn("src/lib/api.ts", self.paths("ts"))

    def test_perf_takes_styles_and_bundler_config(self):
        files = [fi("src/ui/game.css", 900, ".hud { transition: top 1s }"), fi("vite.config.ts", 200, "build: {}"),
                 fi("vitest.config.ts", 20, "{}"), fi("src/ui/store.ts", 300, "export const s = 1")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual(sorted(f.path for f in rel["perf"]), ["src/ui/game.css", "src/ui/store.ts", "vite.config.ts"])

    def test_ux_includes_styles(self):
        self.assertIn("src/styles/theme.css", self.paths("ux"))

    def test_analytics_takes_interaction_surface(self):
        self.assertEqual(self.paths("analytics"), ["src/app/page.tsx"])

    def test_analytics_takes_importers_of_the_event_module_and_skips_lookalikes(self):
        files = [fi("src/ui/usage.ts", 250, "// sent on to PostHog by the worker\nexport type Usage = {}"),
                 fi("src/ui/game/port.ts", 100, "import type { Sent } from '../usage.js'"),
                 fi("src/ui/game/canvas.ts", 240, "el.addEventListener('pointerdown', down)"),
                 fi("src/ui/game/sound.ts", 640, "// a fast amplitude wobble"),
                 fi("src/sim/state.ts", 900, "export const s = 1")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual([f.path for f in rel["analytics"]],
                         ["src/ui/usage.ts", "src/ui/game/port.ts", "src/ui/game/canvas.ts"])

    def test_non_code_ignored(self):
        self.assertNotIn("README.md", self.paths("security"))


class GraphFactsTest(unittest.TestCase):
    def facts(self):
        files = [fi("src/a.ts", 300, "import { b } from './b.js'\nexport const a = 1"),
                 fi("src/b.ts", 200, "import { a } from './a.js'\nexport const b = 2\nexport const only = 3"),
                 fi("src/main.ts", 50, "import { a } from './a.js'\nimport './boot.js'\nexport function helper() {}\nhelper()"),
                 fi("src/lonely.ts", 40, "export const x = 1"),
                 fi("src/sw.ts", 40, "self.addEventListener('fetch', f)"),
                 fi("src/boot.ts", 20, "navigator.serviceWorker.register('/sw.js')"),
                 fi("tests/b.test.ts", 30, "import { only } from '../src/b.js'")]
        src = [f for f in files if not audit.is_test(f.path)]
        return audit.graph_facts(src, files, [("index.html", '<script src="/src/main.ts">')])

    def section(self, title):
        text = self.facts()
        body = text.split(f"## {title}", 1)[1].split("\n## ", 1)[0]
        return [line[2:] for line in body.splitlines() if line.startswith("- ")]

    def test_cycles_listed_with_members(self):
        self.assertEqual(self.section("Import cycles"), ["src/a.ts · src/b.ts"])

    def test_orphans_skip_files_named_by_html_or_a_registered_url(self):
        self.assertEqual(self.section("Orphan candidates"), ["src/lonely.ts"])

    def test_export_buckets(self):
        self.assertEqual(self.section("Exports only tests use"), ["src/b.ts: only"])
        self.assertIn("src/main.ts: helper", self.section("Exports used only inside their own file"))
        self.assertIn("src/lonely.ts: x", self.section("Exports nothing uses"))

    def test_no_js_no_facts(self):
        self.assertIsNone(audit.graph_facts([fi("app/x.py", 10, "x = 1")], [fi("app/x.py", 10, "x = 1")]))


class AllocateTest(unittest.TestCase):
    def test_small_scope_one_each(self):
        counts, demanded = audit.allocate({"perf": 3000, "arch": 3000, "deps": 50}, {}, False, 16)
        self.assertEqual(counts, {"perf": 1, "arch": 1, "deps": 1})

    def test_scales_with_loc_and_special_rules(self):
        counts, _ = audit.allocate(
            {"perf": 9000, "arch": 9000, "analytics": 25000, "deps": 99999}, {}, False, 16)
        self.assertEqual(counts, {"perf": 3, "arch": 2, "analytics": 3, "deps": 1})
        self.assertEqual(audit.allocate({"analytics": 5143}, {}, False, 16)[0], {"analytics": 2})

    def test_override_wins_except_deps(self):
        counts, _ = audit.allocate({"perf": 100, "deps": 100}, {"perf": 4, "deps": 3}, False, 16)
        self.assertEqual(counts, {"perf": 4, "deps": 1})

    def test_no_split_wins_over_override(self):
        counts, _ = audit.allocate({"perf": 99000}, {"perf": 4}, True, 16)
        self.assertEqual(counts, {"perf": 1})

    def test_cap_trims_highest_first_never_below_one(self):
        counts, demanded = audit.allocate({"perf": 95000, "security": 20000, "ts": 100}, {}, False, 16)
        self.assertEqual(demanded, {"perf": 24, "security": 5, "ts": 1})
        self.assertEqual(sum(counts.values()), 16)
        self.assertEqual(counts["ts"], 1)
        self.assertGreaterEqual(counts["perf"], counts["security"])

    def test_cap_ties_trim_smallest_dimension_first(self):
        counts, _ = audit.allocate({"perf": 31000, "ts": 15000}, {}, False, 5)
        self.assertEqual(counts, {"perf": 3, "ts": 2})

    def test_cap_below_dimension_count_keeps_one_each(self):
        counts, _ = audit.allocate({"perf": 1, "ts": 1, "arch": 1}, {}, False, 2)
        self.assertEqual(counts, {"perf": 1, "ts": 1, "arch": 1})


class PartitionTest(unittest.TestCase):
    def many_files(self):
        return [fi(f"src/{d}/f{i}.ts", 500) for d in ("a", "b", "c", "d") for i in range(4)]

    def test_file_chunk_keeps_directories_together(self):
        slices = audit.partition("perf", self.many_files(), 4)
        self.assertEqual([s["mode"] for s in slices], ["file-chunk"] * 4)
        for s in slices:
            self.assertEqual(len({p.split("/")[1] for p in s["files"]}), 1)

    def test_security_keeps_a_route_with_what_it_imports(self):
        files = [fi("worker/ingest.ts", 1000, "import { v } from '../src/admin/valid-save.js'"),
                 fi("worker/index.ts", 500, "import { i } from './ingest.js'"),
                 fi("src/admin/valid-save.ts", 500, "export const v = 1"),
                 fi("src/admin/a.ts", 2500, "x"), fi("src/ui/b.ts", 1000, "x"), fi("src/ui/c.ts", 1000, "x")]
        slices = audit.partition("security", files, 2, audit.boundary_bundles(files))
        with_route = next(s for s in slices if "worker/ingest.ts" in s["files"])
        self.assertIn("src/admin/valid-save.ts", with_route["files"])
        self.assertIn("worker/index.ts", with_route["files"])

    def test_oversized_directory_is_split(self):
        files = [fi(f"src/big/f{i}.ts", 1000) for i in range(8)]
        slices = audit.partition("perf", files, 2)
        self.assertEqual(len(slices), 2)
        self.assertEqual(sorted(len(s["files"]) for s in slices), [4, 4])

    def test_few_large_files_use_sub_lens(self):
        files = [fi("a.ts", 6000), fi("b.ts", 6000)]
        slices = audit.partition("perf", files, 3)
        self.assertEqual([s["mode"] for s in slices], ["sub-lens"] * 3)
        self.assertEqual(slices[0]["lens"], "render")

    def test_sub_lens_capped_at_menu_length(self):
        self.assertEqual(len(audit.partition("security", [fi("a.ts", 99000)], 5)), 3)

    def test_arch_always_sub_lens(self):
        slices = audit.partition("arch", self.many_files(), 2)
        self.assertEqual([s["mode"] for s in slices], ["sub-lens", "sub-lens"])

    def test_dimension_without_menu_falls_back_to_file_chunk(self):
        slices = audit.partition("ts", [fi("a.ts", 6000), fi("b.ts", 6000)], 2)
        self.assertEqual([s["mode"] for s in slices], ["file-chunk", "file-chunk"])

    def test_file_chunk_brief_allows_reading_across_the_seam(self):
        s = {"slice": 1, "mode": "file-chunk", "lens": None}
        text = audit._partition_text("perf", s, [s, dict(s, slice=2)])
        self.assertIn("even outside this list", text)
        self.assertIn("report ONLY on the files below", text)

    def test_bloat_reserves_cross_file_slice(self):
        slices = audit.partition("bloat", self.many_files(), 3)
        self.assertEqual(slices[0]["lens"], audit.BLOAT_CROSS_LENS)
        self.assertEqual(len(slices[0]["files"]), 16)
        self.assertEqual([s["mode"] for s in slices[1:]], ["file-chunk", "file-chunk"])

    def test_analytics_two_agents_fold_data_quality_into_first(self):
        slices = audit.partition("analytics", self.many_files(), 2)
        self.assertIn("data quality", slices[0]["lens"])


FINDINGS = """## Performance Findings

### Critical
- **PERF-1-01** (confidence: 90) `src/a.ts:10` — slow loop
  → use a map

### Warning
- **PERF-1-02** (confidence: 80) `src/b.ts:5` — re-render
  → memoize

### Info

### Summary
Mostly fine.
"""

FINDINGS_2 = """## Performance Findings

### Critical

### Warning
- **PERF-2-01** (confidence: 95) `src/a.ts:10` — slow loop again
  → use a map
- **PERF-2-02** (confidence: 85) `src/c.ts:1` — waterfall
  → parallelize

### Info

### Summary
Ok.
"""

SEC = """## Security Findings

### Critical
- **SEC-1-01** (confidence: 88) `src/b.ts:5` — leaks token
  → redact

### Warning

### Info
- **SEC-1-02** (confidence: 80) `gen/x.ts:1` — [stale-acceptance] generator removed
  → drop the ignore entry
- **SEC-1-03** (confidence: 80) `gen/y.ts:1` — raw html
  → escape

### Summary
One leak.
"""


def plan_for(slices, ignore=None, demanded=None, launched=None):
    return {
        "scope": "src/", "stack": "TypeScript", "date": "2026-10-04",
        "slices": slices, "ignore": ignore or {},
        "demanded_total": demanded or len(slices), "launched_total": launched or len(slices),
    }


def sl(sid, dim, files=None, mode="whole", loc=500):
    prefix, i = sid.rsplit("-", 1)
    return {"id": sid, "dim": dim, "prefix": prefix, "slice": int(i), "mode": mode,
            "files": files or [], "loc": loc}


class ParseFindingsTest(unittest.TestCase):
    def test_parses_findings_and_summary(self):
        parsed = audit.parse_findings(FINDINGS)
        self.assertEqual([(f["sev"], f["anchor"], f["conf"]) for f in parsed["findings"]],
                         [("Critical", "src/a.ts:10", 90), ("Warning", "src/b.ts:5", 80)])
        self.assertEqual(parsed["findings"][0]["rec"], "use a map")
        self.assertEqual(parsed["summary"], "Mostly fine.")

    def test_collects_extra_sections(self):
        text = FINDINGS.replace("### Summary", "### Coverage Map\n\n| S | I |\n|---|---|\n| x | no |\n\n### Summary")
        self.assertIn("Coverage Map", audit.parse_findings(text)["extras"])


class MergeTest(unittest.TestCase):
    def run_merge(self, **kw):
        plan = plan_for([sl("PERF-1", "perf", ["src/a.ts", "src/b.ts"], "file-chunk"),
                         sl("PERF-2", "perf", ["src/a.ts", "src/c.ts"], "file-chunk"),
                         sl("SEC-1", "security")],
                        ignore={"security": [("gen/**", "- `gen/**` · SEC · raw html — generated. 2026-01-01")]})
        texts = {"PERF-1": FINDINGS, "PERF-2": FINDINGS_2, "SEC-1": SEC}
        return audit.merge(plan, texts, **kw)

    def test_within_dimension_dedup_keeps_higher_severity(self):
        r = self.run_merge()
        perf = [f for f in r["findings"] if f["prefix"] == "PERF"]
        self.assertEqual(len(perf), 3)
        a = next(f for f in perf if f["anchor"] == "src/a.ts:10")
        self.assertEqual(a["sev"], "Critical")

    def test_renumbers_flat_by_severity_then_confidence(self):
        perf = [f for f in self.run_merge()["findings"] if f["prefix"] == "PERF"]
        self.assertEqual([(f["id"], f["anchor"]) for f in perf],
                         [("PERF-01", "src/a.ts:10"), ("PERF-02", "src/c.ts:1"), ("PERF-03", "src/b.ts:5")])

    def test_cross_dimension_candidates(self):
        self.assertEqual(self.run_merge()["cross"], [("PERF-1-02", "SEC-1-01")])

    def test_combine_credits_both_dimensions(self):
        r = self.run_merge(combine=[("PERF-1-02", "SEC-1-01")])
        merged = [f for f in r["findings"] if f["anchor"] == "src/b.ts:5"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sev"], "Critical")
        self.assertEqual(merged[0]["dims"], ["SEC", "PERF"])
        self.assertEqual(r["cross"], [])

    def test_ignore_candidates_skip_stale_acceptances(self):
        self.assertEqual([c[0] for c in self.run_merge()["ignore_candidates"]], ["SEC-1-03"])

    def test_suppress_drops_and_counts(self):
        r = self.run_merge(suppress=["SEC-1-03"])
        self.assertNotIn("gen/y.ts:1", [f["anchor"] for f in r["findings"]])
        self.assertEqual(r["suppressed"], {"SEC": 1})

    def test_out_of_slice_anchor_is_contract_break(self):
        plan = plan_for([sl("PERF-1", "perf", ["src/a.ts"], "file-chunk")])
        r = audit.merge(plan, {"PERF-1": FINDINGS})
        self.assertTrue(any("src/b.ts" in b for b in r["breaks"]))
        self.assertTrue(any(t.startswith("4") for t in r["retro"]))

    def test_missing_file_is_contract_break(self):
        r = audit.merge(plan_for([sl("PERF-1", "perf")]), {"PERF-1": None})
        self.assertEqual(len(r["breaks"]), 1)

    def test_empty_large_slice_fires_trigger_2(self):
        empty = "## X Findings\n\n### Critical\n\n### Warning\n\n### Info\n\n### Summary\nHealthy.\n"
        r = audit.merge(plan_for([sl("TS-1", "ts", loc=2000)]), {"TS-1": empty})
        self.assertTrue(any(t.startswith("2") for t in r["retro"]))

    def test_heavy_cap_fires_trigger_1(self):
        r = audit.merge(plan_for([sl("TS-1", "ts")], demanded=30, launched=14), {"TS-1": FINDINGS.replace("PERF", "TS")})
        self.assertTrue(any(t.startswith("1") for t in r["retro"]))

    def test_dedup_churn_fires_trigger_3(self):
        dup = "\n".join(f"- **PERF-2-0{i}** (confidence: 80) `src/a.ts:{i}` — d\n  → r" for i in range(1, 4))
        one = "\n".join(f"- **PERF-1-0{i}** (confidence: 80) `src/a.ts:{i}` — d\n  → r" for i in range(1, 4))
        plan = plan_for([sl("PERF-1", "perf"), sl("PERF-2", "perf")])
        r = audit.merge(plan, {"PERF-1": "### Warning\n" + one, "PERF-2": "### Warning\n" + dup})
        self.assertTrue(any(t.startswith("3") for t in r["retro"]))

    def test_same_slice_findings_at_one_anchor_both_survive(self):
        two = ("### Warning\n"
               "- **ANLT-1-02** (confidence: 80) `src/store.ts:602` — visit has no push attribution\n  → add source\n"
               "- **ANLT-1-03** (confidence: 80) `src/store.ts:602` — events carry no platform\n  → add platform\n")
        r = audit.merge(plan_for([sl("ANLT-1", "analytics")]), {"ANLT-1": two})
        self.assertEqual(sorted(f["desc"] for f in r["findings"]),
                         ["events carry no platform", "visit has no push attribution"])

    def test_sub_lens_overlap_is_kept_and_offered_for_combine(self):
        a = "### Warning\n- **ARCH-1-01** (confidence: 80) `src/a.ts:3` — cycle a↔b\n  → split\n"
        b = "### Warning\n- **ARCH-2-01** (confidence: 90) `src/a.ts:3` — god file\n  → split\n"
        plan = plan_for([sl("ARCH-1", "arch", ["src/a.ts"], "sub-lens"), sl("ARCH-2", "arch", ["src/a.ts"], "sub-lens")])
        r = audit.merge(plan, {"ARCH-1": a, "ARCH-2": b})
        self.assertEqual(len(r["findings"]), 2)
        self.assertIn(("ARCH-2-01", "ARCH-1-01"), r["cross"])

    def test_sub_lens_overlap_does_not_fire_trigger_3(self):
        lens = "\n".join(f"- **BLOAT-1-0{i}** (confidence: 80) `src/a.ts:{i}` — d\n  → r" for i in range(1, 4))
        chunk = "\n".join(f"- **BLOAT-2-0{i}** (confidence: 80) `src/a.ts:{i}` — d\n  → r" for i in range(1, 4))
        plan = plan_for([sl("BLOAT-1", "bloat", ["src/a.ts"], "sub-lens"),
                         sl("BLOAT-2", "bloat", ["src/a.ts"], "file-chunk")])
        r = audit.merge(plan, {"BLOAT-1": "### Warning\n" + lens, "BLOAT-2": "### Warning\n" + chunk})
        self.assertFalse(any(t.startswith("3") for t in r["retro"]))


class ReportTest(unittest.TestCase):
    def test_health(self):
        self.assertEqual(audit.health(0, 2), "pass")
        self.assertEqual(audit.health(0, 3), "warn")
        self.assertEqual(audit.health(1, 0), "warn")
        self.assertEqual(audit.health(3, 0), "fail")

    def test_report_has_summary_rows_for_run_dimensions_only(self):
        plan = plan_for([sl("PERF-1", "perf"), sl("SEC-1", "security")])
        r = audit.merge(plan, {"PERF-1": FINDINGS, "SEC-1": SEC})
        report = audit.render_report(plan, r)
        self.assertIn("| Performance", report)
        self.assertIn("| Security", report)
        self.assertNotIn("| TypeScript", report)
        self.assertIn("### Critical (2)", report)

    def test_analytics_tables_unioned_keeping_higher_priority(self):
        t1 = "### Coverage Map\n| Surface | Instrumented | Gap | Priority |\n|---|---|---|---|\n| signup | no | all | Critical |\n| search | partial | x | Info |\n"
        t2 = "### Coverage Map\n| Surface | Instrumented | Gap | Priority |\n|---|---|---|---|\n| search | no | y | Warning |\n"
        plan = plan_for([sl("ANLT-1", "analytics"), sl("ANLT-2", "analytics")])
        r = audit.merge(plan, {"ANLT-1": t1, "ANLT-2": t2})
        report = audit.render_report(plan, r)
        self.assertIn("| search | no | y | Warning |", report)
        self.assertNotIn("| search | partial", report)
        self.assertIn("| signup |", report)


def _git_repo(path, ignore=""):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if ignore:
        (path / ".gitignore").write_text(ignore)


class ScopeTest(unittest.TestCase):
    # outer/ ignores inner/, which is its own repo: the bare-root + worktree layout.
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.outer, self.inner = self.tmp / "outer", self.tmp / "outer" / "inner"
        _git_repo(self.outer, "inner/\nbuild/\n")
        _git_repo(self.inner)
        (self.inner / "app").mkdir()
        (self.inner / "app" / "a.ts").write_text("export const a = 1;\n")
        (self.outer / "build").mkdir()
        (self.outer / "build" / "b.ts").write_text("export const b = 1;\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_path_in_nested_repo_resolves_root_from_target(self):
        o = audit.parse_args(["inner/app"])
        root, mode, desc, paths, _, _ = audit.resolve_scope(o, self.outer)
        self.assertEqual((root, desc, paths), (self.inner, "app/", ["app/a.ts"]))

    def test_gitignored_path_names_the_ignore_rule(self):
        o = audit.parse_args(["build"])
        with self.assertRaisesRegex(audit.UsageError, r"build/ is gitignored \(\.gitignore:2"):
            audit.cmd_plan(["build"], self.outer)

    def test_missing_path_is_usage_error(self):
        with self.assertRaisesRegex(audit.UsageError, "no such path"):
            audit.resolve_scope(audit.parse_args(["nope"]), self.outer)

    def test_plan_in_other_repo_tells_merge_where_to_look(self):
        out = io.StringIO()
        with redirect_stdout(out):
            audit.cmd_plan(["inner/app", "--analytics"], self.outer)
        self.assertIn(f"NOTE scope root is {self.inner}; merge with --dir {self.inner}", out.getvalue())
        self.assertTrue((self.inner / audit.RUN_DIR / "plan.json").exists())

    def test_merge_without_plan_is_usage_error(self):
        with self.assertRaisesRegex(audit.UsageError, f"no plan in {self.outer}"):
            audit.cmd_merge([], self.outer)



class BundledAgentsTest(unittest.TestCase):
    def test_every_plugin_agent_has_a_definition(self):
        for key, d in audit.DIMS.items():
            self.assertTrue(d.agent.startswith("audit:"), f"{key} uses a non-bundled agent {d.agent}")
            path = audit.PLUGIN_ROOT / "agents" / f"{d.agent[6:]}.md"
            self.assertTrue(path.exists(), f"{key}: missing {path}")
            self.assertIn(f"name: {d.agent[6:]}\n", path.read_text())

if __name__ == "__main__":
    unittest.main()
