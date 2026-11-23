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

    def test_test_dimension_takes_untested_source(self):
        self.assertEqual(self.paths("test"), ["src/app/page.tsx", "src/lib/math.ts"])

    def test_a11y_takes_ui_files(self):
        self.assertEqual(self.paths("a11y"), ["src/app/page.tsx"])

    def test_resilience_takes_async_files(self):
        self.assertEqual(self.paths("resilience"), ["src/lib/api.ts"])

    def test_security_takes_trust_boundary_files(self):
        self.assertEqual(self.paths("security"), ["src/app/page.tsx", "src/lib/api.ts"])

    def test_security_keeps_server_dirs_and_queries(self):
        files = [fi("worker/saves.ts", 100, "db.prepare(q)"), fi("worker/util.ts", 50, "export const x = 1"),
                 fi("src/sim/rng.ts", 50, "export const next = (s) => s * 2")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual([f.path for f in rel["security"]], ["worker/saves.ts", "worker/util.ts"])

    def test_bloat_includes_tests_perf_does_not(self):
        self.assertIn("src/lib/api.test.ts", self.paths("bloat"))
        self.assertNotIn("src/lib/api.test.ts", self.paths("perf"))

    def test_perf_skips_docs_scripts_and_config(self):
        files = [fi("docs/ERD.html", 500, "<html>"), fi("scripts/release.mjs", 200, "run()"),
                 fi("vite.config.ts", 40, "export default {}"), fi("src/ui/store.ts", 300, "export const s = 1")]
        rel = audit.relevant_sets(files, files)
        self.assertEqual([f.path for f in rel["perf"]], ["src/ui/store.ts"])
        self.assertIn("scripts/release.mjs", [f.path for f in rel["bloat"]])

    def test_ux_includes_styles(self):
        self.assertIn("src/styles/theme.css", self.paths("ux"))

    def test_analytics_takes_interaction_surface(self):
        self.assertEqual(self.paths("analytics"), ["src/app/page.tsx"])

    def test_non_code_ignored(self):
        self.assertNotIn("README.md", self.paths("security"))


class AllocateTest(unittest.TestCase):
    def test_small_scope_one_each(self):
        counts, demanded = audit.allocate({"perf": 3000, "arch": 3000, "deps": 50}, {}, False, 16)
        self.assertEqual(counts, {"perf": 1, "arch": 1, "deps": 1})

    def test_scales_with_loc_and_special_rules(self):
        counts, _ = audit.allocate(
            {"perf": 9000, "arch": 9000, "analytics": 25000, "deps": 99999}, {}, False, 16)
        self.assertEqual(counts, {"perf": 3, "arch": 2, "analytics": 3, "deps": 1})

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
