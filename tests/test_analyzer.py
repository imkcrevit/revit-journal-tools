import tempfile
import unittest
from pathlib import Path

from revit_journal_tools import ScanOptions, analyze


class AnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "journal.txt"

    def scan(self, text, **options):
        self.path.write_text(text, encoding="utf-8")
        return analyze(self.path, options=ScanOptions(**options))

    def test_context_boundaries_overlap_and_no_final_newline(self):
        report = self.scan("DBG_WARN: first\nnormal\nDBG_ERROR: middle\nnormal\nERROR: last", context=2)
        self.assertEqual(report.line_count, 5)
        first, middle, last = report.findings
        self.assertEqual(first.context_before, [])
        self.assertEqual([line.line_number for line in first.context_after], [2, 3])
        self.assertEqual([line.line_number for line in middle.context_before], [1, 2])
        self.assertEqual([line.line_number for line in middle.context_after], [4, 5])
        self.assertEqual([line.line_number for line in last.context_before], [3, 4])
        self.assertEqual(last.context_after, [])

    def test_multiple_rules_make_one_candidate(self):
        report = self.scan("' 0:< DBG_ERROR: fatal error\n")
        self.assertEqual(report.matched_line_count, 1)
        self.assertEqual(report.findings[0].severity, "fatal")
        self.assertEqual({match.rule_id for match in report.findings[0].matches}, {"revit.dbg_error", "generic.fatal"})
        self.assertEqual(sum(report.counts_by_rule.values()), 2)
        self.assertEqual(report.findings[0].status, "candidate")

    def test_common_normal_mentions_do_not_match(self):
        report = self.scan("0 errors\nno exception was raised\nError reporting initialized\nJrn.Command \"Show Error Report\"\nSystem.Exception handler registered\n")
        self.assertEqual(report.matched_line_count, 0)

    def test_counts_and_groups_continue_after_retention_cap(self):
        report = self.scan("DBG_WARN: repeat\n" * 20 + "fatal error at EOF\n", max_findings=2, max_groups=1)
        self.assertTrue(report.scan_complete)
        self.assertEqual(report.matched_line_count, 21)
        self.assertEqual(report.counts_by_severity, {"warning": 20, "error": 0, "fatal": 1})
        self.assertEqual(len(report.findings), 2)
        self.assertEqual([finding.line_number for finding in report.findings], [1, 21])
        self.assertEqual(report.omitted_finding_count, 19)
        self.assertEqual(report.groups[0].count, 20)
        self.assertEqual(report.groups[0].last_line, 20)
        self.assertEqual(report.ungrouped_match_count, 1)
        self.assertEqual(len(report.warnings), 2)

    def test_per_group_limit_keeps_later_distinct_candidates(self):
        # Observed shape: one renderer error line repeated >100,000 times before the crash.
        text = "USD: Runtime Error: GL error\n" * 50 + "DBG_ERROR: later and different\n"
        plain = self.scan(text, max_findings=5, context=0)
        self.assertEqual([finding.line_number for finding in plain.findings], [1, 2, 3, 4, 5])
        limited = self.scan(text, max_findings=5, context=0, max_per_group=1)
        self.assertEqual([finding.line_number for finding in limited.findings], [1, 51])
        self.assertEqual((limited.matched_line_count, limited.omitted_finding_count), (51, 49))
        self.assertEqual(limited.groups[0].count, 50)
        self.assertEqual(limited.retention_policy, "severity_then_first,max_per_group=1")
        with self.assertRaises(ValueError):
            ScanOptions(max_per_group=-1)

    def test_distinct_element_ids_do_not_merge(self):
        report = self.scan("DBG_WARN: element 123\nDBG_WARN: element 456\n")
        self.assertEqual(len(report.groups), 2)

    def test_retention_prioritizes_severity_and_keeps_overlapping_context_correct(self):
        report = self.scan("DBG_WARN: first\nDBG_WARN: second\nERROR: later\nfatal error last\nnormal\n", max_findings=2, context=2)
        self.assertEqual([finding.line_number for finding in report.findings], [3, 4])
        self.assertEqual([line.line_number for line in report.findings[0].context_after], [4, 5])
        self.assertEqual([line.line_number for line in report.findings[1].context_before], [2, 3])
        self.assertEqual(report.retention_policy, "severity_then_first")

    def test_min_severity_filters_rules_and_zero_context(self):
        report = self.scan("DBG_WARN: warning\nDBG_WARN: fatal error\nERROR: issue\n", min_severity="error", context=0)
        self.assertEqual([finding.line_number for finding in report.findings], [2, 3])
        self.assertNotIn("revit.dbg_warn", report.counts_by_rule)
        for finding in report.findings:
            self.assertEqual(finding.context_before, [])
            self.assertEqual(finding.context_after, [])

    def test_match_beyond_display_limit_is_preserved(self):
        report = self.scan("x" * 10_000 + " DBG_ERROR: late marker\n", max_excerpt_chars=200)
        finding = report.findings[0]
        self.assertTrue(finding.line.text_truncated)
        self.assertEqual(len(finding.line.text), 200)
        match = finding.matches[0]
        self.assertEqual(match.column, 10_002)
        self.assertIn("DBG_ERROR: late marker", match.excerpt)

    def test_provenance_and_options_are_serializable(self):
        report = self.scan("错误: 中文测试\n", context=1)
        data = report.to_dict()
        self.assertEqual(data["source"], str(self.path.resolve()))
        self.assertEqual(len(data["sha256"]), 64)
        self.assertEqual(len(data["ruleset_sha256"]), 64)
        self.assertEqual(data["options"]["context"], 1)
        self.assertEqual(data["assessment"], "rule_candidates_only")

    def test_invalid_options(self):
        for options in ({"context": -1}, {"context": 101}, {"max_findings": 0}, {"max_groups": 0}, {"min_severity": "critical"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ScanOptions(**options)


if __name__ == "__main__":
    unittest.main()
