"""Stage 4 (delivery): support report, cache location, console fallback and the
single-file build. Journals are synthetic; names below are placeholders."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from revit_journal_tools.analyzer import ScanOptions, analyze
from revit_journal_tools.cli import main
from revit_journal_tools.discovery import cache_dir
from revit_journal_tools.ticket import Redactor, fence, key_findings

from test_revit_logic import SESSION, write_journal

REPO = Path(__file__).resolve().parent.parent
CONFLICT = ("' 0:< API_ERROR {{ : Assembly version conflict in some references in Demo.Plugin{0}.dll assembly",
            "'Addin's module Demo.Core of version 2.0.0.0 conflicts with same preloaded module of version 1.0.0.0 }")
EXTRA = [
    "'C 05-Oct-2026 15:51:10.000;   0:< DBG_ERROR: cannot read C:\\Users\\jdoe\\Desktop\\a```b.rvt",
    *(line for number in range(5) for line in (CONFLICT[0].format(number), CONFLICT[1])),
]


def cli(*args):
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = main(list(args))
    return code, output.getvalue(), errors.getvalue()


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = write_journal(self.root / "in" / "journal.0001.txt", SESSION[:-2] + EXTRA + SESSION[-2:])
        # No real CER/steel logs and no real cache: everything stays in the temporary folder.
        environment = {"LOCALAPPDATA": str(self.root / "local"), "PROGRAMDATA": str(self.root / "programdata"),
                       "REVIT_JOURNAL_CACHE": str(self.root / "cache"), "USERNAME": "jdoe",
                       "USERDOMAIN": "EXAMPLEDOM", "COMPUTERNAME": "EXAMPLE-PC"}
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)


class ReportTests(Workspace):
    def test_markdown_report_has_hashes_lines_rules_and_disclaimer(self):
        output = self.root / "out" / "report.md"
        code, _, errors = cli("report", str(self.journal), "--jobs", "1", "-o", str(output))
        self.assertEqual(code, 0, errors)
        text = output.read_text(encoding="utf-8")
        sha = analyze(self.journal).sha256
        self.assertIn(sha, text)
        self.assertIn("## 会话 journal.0001 — 正常结束", text)
        self.assertIn("| journal.0001.txt | 主Journal |", text)
        self.assertIn("`revit.dbg_error`", text)
        self.assertIn("**第43行** [error]", text)  # the DBG_ERROR line keeps its source line number
        self.assertIn("## 未验证声明", text)
        self.assertIn("候选", text)
        # UI prompts are counted but not listed as findings.
        self.assertIn("Revit界面提示2行不计为错误", text)
        self.assertNotIn("`revit.ui_warning` ", text.split("### 规则候选")[1].split("按规则：")[1].split("\n", 1)[1])
        # The Journal's ``` cannot close the fence early.
        self.assertIn("````text", text)

    def test_personal_names_are_replaced_unless_asked(self):
        code, text, errors = cli("report", str(self.journal), "--jobs", "1")
        self.assertEqual(code, 0, errors)
        self.assertNotIn("jdoe", text)
        self.assertIn("C:\\Users\\<user>\\Desktop", text)
        self.assertIn("个人信息：已替换", text)
        code, text, _ = cli("report", str(self.journal), "--jobs", "1", "--include-personal-info")
        self.assertIn("C:\\Users\\jdoe\\Desktop", text)

    def test_json_report_and_output_protection(self):
        code, text, errors = cli("report", str(self.journal), "--jobs", "1", "--format", "json")
        self.assertEqual(code, 0, errors)
        document = json.loads(text)
        self.assertEqual(document["assessment"], "rule_candidates_only")
        session, = document["sessions"]
        self.assertEqual(session["end_state"], "clean")
        self.assertEqual(session["scans"][0]["sha256"], session["files"][0]["sha256"])
        self.assertNotIn("jdoe", text)
        code, _, errors = cli("report", str(self.journal), "--jobs", "1", "-o", str(self.journal))
        self.assertEqual(code, 2)
        self.assertIn("不能覆盖输入文件", errors)

    def test_examples_per_rule_combination_are_capped(self):
        report = analyze(self.journal, options=ScanOptions(context=0)).to_dict()
        findings, ui_lines = key_findings(report, limit=30)
        conflicts = [item for item in findings
                     if [match["rule_id"] for match in item["matches"]] == ["revit.api_error", "revit.addin_conflict"]]
        # 2 conflicts from SESSION (one repeat group) + 5 different ones: 3 examples, the rest counted.
        self.assertEqual(len(conflicts), 3)
        self.assertEqual(conflicts[0]["repeat_count"], 2)
        self.assertEqual(conflicts[-1]["more_lines"], 3)
        self.assertEqual(ui_lines, 2)
        self.assertFalse(any(match["rule_id"] == "revit.ui_warning" for item in findings for match in item["matches"]))


class RedactorTests(unittest.TestCase):
    def test_whole_tokens_and_report_words(self):
        redact = Redactor("bob", "CORP", "WS-01")
        self.assertEqual(redact(r"CORP\bob C:\Users\bob\x bobby WS-01 \\WS-01\share"),
                         r"<domain>\<user> C:\Users\<user>\x bobby <machine> \\<machine>\share")
        self.assertEqual(redact('{"p": "C:\\\\Users\\\\bob\\\\x"}'), '{"p": "C:\\\\Users\\\\<user>\\\\x"}')
        # A user called "Revit" is replaced only in the qualified and profile contexts.
        revit = Redactor("Revit", None, None)
        self.assertEqual(revit(r"Autodesk Revit 2024 C:\Users\Revit\a"), r"Autodesk Revit 2024 C:\Users\<user>\a")

    def test_fence_is_longer_than_any_backtick_run(self):
        self.assertTrue(fence("a ```` b").startswith("`````text\n"))
        self.assertTrue(fence("plain").startswith("```text\n"))


class CacheTests(Workspace):
    def test_default_location_per_platform(self):
        self.assertEqual(cache_dir({"REVIT_JOURNAL_CACHE": "X:\\c"}, "nt"), Path("X:\\c"))
        self.assertEqual(cache_dir({"LOCALAPPDATA": "L:\\Local"}, "nt"), Path("L:\\Local") / "revit-journal-tools" / "cache")
        self.assertEqual(cache_dir({"XDG_CACHE_HOME": "/xdg"}, "posix"), Path("/xdg") / "revit-journal-tools")
        self.assertEqual(cache_dir({}, "posix"), Path.home() / ".cache" / "revit-journal-tools")

    def test_commands_use_the_cache_folder_and_clear_removes_only_its_databases(self):
        cache = self.root / "cache"
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        code, _, errors = cli("summary", str(self.journal), "--jobs", "1")
        self.assertEqual(code, 0, errors)
        self.assertTrue((cache / "terminal.sqlite3").is_file())
        self.assertFalse((self.root / ".journal-cache").exists())
        (cache / "keep.txt").write_text("not ours", encoding="utf-8")
        code, text, _ = cli("cache")
        self.assertIn(str(cache), text)
        code, text, _ = cli("cache", "--clear")
        self.assertEqual(code, 0)
        self.assertFalse((cache / "terminal.sqlite3").exists())
        self.assertTrue((cache / "keep.txt").exists())


class ConsoleFallbackTests(Workspace):
    def test_console_without_interactive_support_prints_one_page(self):
        from revit_journal_tools import terminal

        class Console(io.StringIO):
            def isatty(self):
                return True

        @contextmanager
        def unsupported():
            raise ValueError("控制台不支持VT控制序列（旧版conhost）")
            yield

        output, errors = Console(), io.StringIO()
        with mock.patch.object(terminal, "keyboard", unsupported), mock.patch.object(sys, "stdin", Console()), \
                redirect_stdout(output), redirect_stderr(errors):
            code = terminal.run([self.journal], self.root / "db.sqlite3", "auto", jobs=1)
        self.assertEqual(code, 0)
        self.assertIn("等同--print", errors.getvalue())
        self.assertIn("L1–", output.getvalue())
        self.assertNotIn("\x1b[?1049h", output.getvalue())


class ZipappTests(unittest.TestCase):
    def test_single_file_build_runs_without_the_source_tree(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "revit-journal.pyz"
            subprocess.run([sys.executable, str(REPO / "scripts" / "build_zipapp.py"), "-o", str(target)],
                           check=True, capture_output=True)
            journal = write_journal(Path(folder) / "journal.0001.txt", SESSION)
            environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            result = subprocess.run([sys.executable, str(target), "scan", str(journal), "--format", "json"],
                                    capture_output=True, cwd=folder, env=environment)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            report = json.loads(result.stdout.decode("utf-8"))["files"][0]
            self.assertIn("revit.ui_warning", report["counts_by_rule"])  # built-in rules came from the archive


if __name__ == "__main__":
    unittest.main()
