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
        code, stdout, _ = self.run_cli("scan", missing, bad, self.journal, "--format", "jsonl",
                                       "--fail-on", "error", "--encoding", "utf-8")
        self.assertEqual(code, 2)
        reports = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(sorted(report["status"] for report in reports), ["error", "error", "ok"])
        self.assertTrue(all(not report["scan_complete"] for report in reports if report["status"] == "error"))

    def test_auto_encoding_degrades_visibly_instead_of_failing(self):
        bad = self.root / "bad.txt"
        bad.write_bytes("正常\n".encode("gb18030") + b"DBG_ERROR: bad\xff\n")
        with patch("revit_journal_tools.reader.system_fallback_encoding", return_value="gb18030"):
            code, stdout, _ = self.run_cli("scan", bad, "--format", "json")
            _, text, _ = self.run_cli("scan", bad)
        self.assertEqual(code, 0)
        report = json.loads(stdout)["files"][0]
        self.assertEqual((report["status"], report["encoding"], report["encoding_requested"]), ("ok", "gb18030", "auto"))
        self.assertTrue(report["encoding_fallback"])
        self.assertEqual((report["decode_degraded_line_count"], report["decode_degraded_lines"]), (1, [2]))
        self.assertTrue(any("退化解码" in warning for warning in report["warnings"]))
        finding = report["findings"][0]
        self.assertEqual(finding["line"]["text"], "DBG_ERROR: bad\\xff")
        self.assertTrue(finding["line"]["decode_degraded"])
        self.assertFalse(finding["context_before"][0]["decode_degraded"])
        self.assertIn("退化解码，1行", text)
        self.assertIn("[含无法解码字节", text)

    def test_unknown_encoding_is_an_input_error(self):
        code, _, stderr = self.run_cli("scan", self.journal, "--encoding", "no-such-codec")
        self.assertEqual(code, 2)
        self.assertIn("no-such-codec", stderr)

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

    def test_search_lists_hits_of_every_file_by_session(self):
        other = self.root / "other.log"
        other.write_text("synthetic start\nnormal\n", encoding="utf-8")
        database = self.root / "index" / "search.sqlite3"
        code, stdout, stderr = self.run_cli("search", "synthetic", self.root, "--recursive",
                                            "--database", database, "--jobs", 1)
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"{self.journal.resolve()} | 命中1行\n  2: DBG_ERROR: synthetic\n", stdout)
        self.assertIn(f"{other.resolve()} | 命中1行\n  1: synthetic start\n", stdout)
        # Two unrecognized file names: two sessions, each with its own header line.
        self.assertEqual(sum(line.startswith("== ") for line in stdout.splitlines()), 2)
        self.assertTrue(stdout.endswith("共命中2行 / 2个文件\n"))
        # Every word must occur in the line, in any order; the last one is also a prefix.
        code, stdout, stderr = self.run_cli("search", "synthetic DBG_ERR", self.root, "--database", database,
                                            "--jobs", 1)
        self.assertEqual(code, 0, stderr)
        self.assertIn("  2: DBG_ERROR: synthetic\n", stdout)
        self.assertNotIn(str(other.resolve()), stdout)
        self.assertTrue(stdout.endswith("共命中1行 / 1个文件\n"))

    def test_search_json_and_failed_files(self):
        database = self.root / "search.sqlite3"
        code, stdout, stderr = self.run_cli("search", "synthetic", self.journal, "--database", database,
                                            "--format", "json")
        self.assertEqual(code, 0, stderr)
        data = json.loads(stdout)
        self.assertEqual((data["schema_version"], data["query"], data["total_lines"], data["file_count"]),
                         (1, "synthetic", 1, 1))
        self.assertEqual(data["terms"], ["synthetic"])
        self.assertEqual([item["line_number"] for item in data["files"][0]["items"]], [2])
        self.assertEqual((data["skipped"], data["errors"]), (0, []))
        bad = self.root / "bad.txt"
        bad.write_bytes(b"synthetic\xff\n")
        code, stdout, stderr = self.run_cli("search", "synthetic", self.journal, bad, "--database", database,
                                            "--encoding", "utf-8", "--jobs", 1)
        self.assertEqual(code, 2, stderr)
        self.assertIn("跳过1个未就绪或失败的文件", stdout)
        self.assertIn(f"索引失败：{bad.resolve()}", stdout)

    def test_search_accepts_the_addin_and_exit_categories(self):
        journal = self.root / "journal.0001.txt"
        journal.write_text("' Build: synthetic\n"
                           "' 0:< API_SUCCESS { Starting External Application: Synthetic Tool, Class: Synthetic.Tool, "
                           "Vendor: ADSK() }\n"
                           'Jrn.Command "Ribbon" , "Synthetic command , ID_SYNTHETIC"\n'
                           "'C 01-Oct-2026 10:05:00.000; ->desktop ExitManagedInstance\n"
                           "' 0:< Synthetic shutdown note\n", encoding="utf-8")
        database = self.root / "search.sqlite3"
        for category, line in (("startup", 1), ("addin", 2), ("operation", 3), ("exit", 5)):
            with self.subTest(category=category):
                code, stdout, stderr = self.run_cli("search", "synthetic", journal, "--database", database,
                                                    "--category", category, "--format", "json")
                self.assertEqual(code, 0, stderr)
                self.assertEqual([item["line_number"] for item in json.loads(stdout)["files"][0]["items"]], [line])
        # Add-in origins are paragraph filters of browse, not line categories.
        for category in ("addin_autodesk", "system"):
            with self.subTest(category=category), self.assertRaises(SystemExit):
                self.run_cli("search", "synthetic", journal, "--database", database, "--category", category)

    def test_browse_kind_lists_only_those_add_in_records(self):
        journal = self.root / "journal.0001.txt"
        journal.write_text(
            "' 0:< API_SUCCESS { Registered an external service: Name(Synthetic Helper Service); VendorId(ADSK) }\n"
            "' 0:< API_SUCCESS { Registered an external server: Name(Synthetic Server); VendorId(ADSK); ServerId(1) }\n"
            "' [Jrn.AddInManifest] Rvt.Attr.AddInName: Synthetic Partner Rvt.Attr.CommandVendorId: DEMO \n"
            'Jrn.Command "Ribbon" , "Synthetic command , ID_SYNTHETIC"\n', encoding="utf-8")
        database = self.root / "kinds.sqlite3"
        code, stdout, stderr = self.run_cli("browse", journal, "--database", database, "--category", "addin",
                                            "--kind", "service", "--kind", "server", "--format", "json")
        self.assertEqual(code, 0, stderr)
        file = json.loads(stdout)["files"][0]
        self.assertEqual(file["kinds"], ["service", "server"])
        self.assertEqual([item["addin_kind"] for item in file["items"]], ["service", "server"])
        self.assertEqual(file["addin_count"], 3)  # the file's counts stay unfiltered
        code, stdout, stderr = self.run_cli("browse", journal, "--database", database, "--category", "addin_third_party",
                                            "--kind", "manifest", "--print")
        self.assertEqual(code, 0, stderr)
        self.assertIn("名称/Class前缀: 不限 | 种类: 清单", stdout)
        self.assertIn("Synthetic Partner [清单] [第三方]", stdout)
        self.assertNotIn("Synthetic Server", stdout)
        # Outside the Add-in views the kinds are not applied, and the user is told so.
        code, stdout, stderr = self.run_cli("browse", journal, "--database", database, "--kind", "service",
                                            "--format", "json")
        self.assertEqual(code, 0, stderr)
        self.assertIn("--kind只在addin分类", stderr)
        self.assertEqual(len(json.loads(stdout)["files"][0]["items"]), 4)
        with self.assertRaises(SystemExit):
            self.run_cli("browse", journal, "--database", database, "--kind", "bogus")

    def test_browser_keeps_the_kinds_of_browse_kind(self):
        from revit_journal_tools.terminal import Browser
        from revit_journal_tools.viewer_index import ViewerIndex

        journal = self.root / "journal.0001.txt"
        journal.write_text(
            "' 0:< API_SUCCESS { Registered an external service: Name(Synthetic Helper Service); VendorId(ADSK) }\n"
            "' 0:< API_SUCCESS { Registered an external server: Name(Synthetic Server); VendorId(ADSK); ServerId(1) }\n",
            encoding="utf-8")
        index = ViewerIndex(self.root / "browser.sqlite3")
        index.build(index.register([journal], "auto"), "auto")
        browser = Browser(index, category="addin", kinds=("server",))
        frame = browser.frame(120, 25, False)
        self.assertIn("名称/Class前缀: 不限 | 种类: 服务器 | v修改前缀", frame)
        self.assertIn("Synthetic Server [服务器] [Autodesk]", frame)
        self.assertNotIn("Synthetic Helper Service", frame)
        browser.handle("0")  # all categories: the kinds do not apply there
        self.assertEqual(len(browser.items), 2)

    def test_search_rejects_punctuation_only_query_before_indexing(self):
        database = self.root / "search.sqlite3"
        code, stdout, stderr = self.run_cli("search", "}", self.journal, "--database", database)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("标点和符号不参与分词", stderr)
        self.assertFalse(database.exists())
        code, _, stderr = self.run_cli("search", "synthetic", self.journal, "--database", database, "--per-file", 0)
        self.assertEqual(code, 2)
        self.assertIn("--per-file", stderr)

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
