import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from revit_journal_tools.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = self.root / "journal.txt"
        self.journal.write_text("normal\nDBG_ERROR: synthetic\n", encoding="utf-8")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(arg) for arg in args])
        return code, out.getvalue(), err.getvalue()

    def test_json_output_and_default_exit_code(self):
        code, stdout, stderr = self.run_cli("scan", self.journal, "--format", "json")
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)["files"][0]
        self.assertEqual(report["matched_line_count"], 1)
        self.assertEqual(report["findings"][0]["line_number"], 2)

    def test_fatal_after_retention_cap_still_affects_exit_code(self):
        self.journal.write_text("DBG_WARN: repeated\n" * 20 + "fatal error\n", encoding="utf-8")
        code, stdout, _ = self.run_cli("scan", self.journal, "--max-findings", "1", "--fail-on", "error", "--format", "json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout)["files"][0]["counts_by_severity"]["fatal"], 1)

    def test_batch_continues_past_missing_and_undecodable_files(self):
        bad = self.root / "bad.txt"
        bad.write_bytes(b"bad\xff")
        missing = self.root / "missing.txt"
        code, stdout, _ = self.run_cli("scan", missing, bad, self.journal, "--format", "jsonl", "--fail-on", "error")
        self.assertEqual(code, 2)
        reports = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(sorted(report["status"] for report in reports), ["error", "error", "ok"])
        self.assertTrue(all(not report["scan_complete"] for report in reports if report["status"] == "error"))

    def test_directory_recursion_and_deduplication(self):
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "nested.LOG").write_text("fatal error\n", encoding="utf-8")
        (nested / "dump.dmp").write_bytes(b"binary")
        code, stdout, _ = self.run_cli("scan", self.root, self.journal, "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(stdout)["files"]), 1)
        code, stdout, _ = self.run_cli("scan", self.root, "--recursive", "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(stdout)["files"]), 2)

    def test_empty_directory_is_an_explicit_input_error(self):
        empty = self.root / "empty"
        empty.mkdir()
        code, stdout, _ = self.run_cli("scan", empty, "--format", "json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["files"][0]["status"], "error")

    def test_abbreviated_journals_are_included_but_binary_dumps_are_not(self):
        (self.root / "journal.0001.txt.abbrev").write_text("ExceptionCode=0xe06d7363\n", encoding="utf-8")
        (self.root / "journal.0001.dmp").write_bytes(b"binary\x00")
        code, stdout, _ = self.run_cli("scan", self.root, "--format", "json")
        self.assertEqual(code, 0)
        files = json.loads(stdout)["files"]
        self.assertEqual(len(files), 2)
        self.assertTrue(any(item["source"].endswith(".txt.abbrev") and item["matched_line_count"] == 1 for item in files))

    def test_report_file_and_no_source_modification(self):
        before = self.journal.read_bytes()
        output = self.root / "reports" / "report.json"
        code, stdout, stderr = self.run_cli("scan", self.journal, "--format", "json", "-o", output)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["files"][0]["line_count"], 2)
        self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_output_cannot_overwrite_source_or_aliases(self):
        before = self.journal.read_bytes()
        aliases = [self.journal]
        hardlink = self.root / "hardlink.txt"
        try:
            os.link(self.journal, hardlink)
            aliases.append(hardlink)
        except OSError:
            pass
        symlink = self.root / "symlink.txt"
        try:
            symlink.symlink_to(self.journal)
            aliases.append(symlink)
        except OSError:
            pass
        for output in aliases:
            with self.subTest(output=output):
                code, _, stderr = self.run_cli("scan", self.journal, "-o", output)
                self.assertEqual(code, 2)
                self.assertIn("不能覆盖", stderr)
                self.assertEqual(self.journal.read_bytes(), before)

    def test_interrupted_report_preserves_previous_output(self):
        output = self.root / "report.json"
        output.write_text("previous report", encoding="utf-8")
        with patch("revit_journal_tools.cli.write_reports", side_effect=KeyboardInterrupt):
            code, _, _ = self.run_cli("scan", self.journal, "-o", output)
        self.assertEqual(code, 130)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous report")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_rule_file_is_protected(self):
        rules = self.root / "custom.json"
        before = '{"schema_version": 1, "rules": []}'
        rules.write_text(before, encoding="utf-8")
        code, _, _ = self.run_cli("scan", self.journal, "--rules", rules, "-o", rules)
        self.assertEqual(code, 2)
        self.assertEqual(rules.read_text(encoding="utf-8"), before)

    def test_invalid_rules_do_not_create_output(self):
        rules = self.root / "bad.json"
        rules.write_text("not JSON", encoding="utf-8")
        output = self.root / "out.json"
        code, _, _ = self.run_cli("scan", self.journal, "--rules", rules, "-o", output)
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())

    def test_module_entrypoint(self):
        result = subprocess.run(
            [sys.executable, "-m", "revit_journal_tools", "scan", str(self.journal), "--format", "json"],
            capture_output=True, text=True, encoding="utf-8", timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["schema_version"], 1)

    def test_redirected_output_is_utf8_even_in_ascii_environment(self):
        result = subprocess.run(
            [sys.executable, "-m", "revit_journal_tools", "scan", str(self.journal)],
            env={**os.environ, "PYTHONIOENCODING": "ascii"},
            capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("错误候选", result.stdout.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
