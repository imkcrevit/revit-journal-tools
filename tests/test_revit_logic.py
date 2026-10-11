"""Stage 3 (Revit working logic): live snapshots, discovery, session summary,
phases, Revit rules, sidecar logs and folding. All journals here are
constructed in the observed formats; no real Journal text is used."""

import codecs
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from revit_journal_tools.analyzer import analyze
from revit_journal_tools.blocks import BlockBuilder
from revit_journal_tools.categories import Categorizer
from revit_journal_tools.cli import main
from revit_journal_tools.discovery import journal_folders, parse_years
from revit_journal_tools.loading import block_summary
from revit_journal_tools.live import Snapshot, take_snapshot, wants_snapshot
from revit_journal_tools.reader import JournalLine
from revit_journal_tools.session_view import main_tail, summary_lines
from revit_journal_tools.rules import load_rules, match_rules
from revit_journal_tools.sidecars import (attach_steel_logs, match_cer, match_steel_log, parse_steel_log,
                                          session_window)
from revit_journal_tools.summary import SessionCollector, session_findings
from revit_journal_tools.viewer_index import ViewerIndex
from revit_journal_tools.viewer_server import make_server

SESSION = [
    "' 0:< Initial VM: Avail 100 MB, Used 24 MB",
    "'C 05-Oct-2026 15:49:53.777;  started recording journal file ",
    "' Build: 20990101_0000(x64)",
    "' Branch: RELEASE_DEMO",
    "' Release: 2099.1",
    "Dim Jrn",
    "Set Jrn = CrsJournalScript",
    "'C 05-Oct-2026 15:49:53.781;  ->desktop InitApplication ",
    "'C 05-Oct-2026 15:49:54.000;   0:< License initialization complete ",
    "'C 05-Oct-2026 15:49:54.100;   0:< LicenseUpd(2) Adlsdk Error:(9) vendor:EXAMPLE",
    "'H 05-Oct-2026 15:49:55.000;   0:< ",
    "Jrn.Data  _",
    '        "JournalDefaultTemplate"  , "demo.rte"',
    "'H 05-Oct-2026 15:49:55.001;   0:< ",
    "Jrn.Directive  _",
    '        "Username"  , "demo"',
    "'C 05-Oct-2026 15:49:55.500;   1:< logging main created main queue Session4242_P0MainQueue ",
    "'E 05-Oct-2026 15:49:56.000;   0:< ",
    'Jrn.RibbonEvent "TabActivated:Architecture"',
    "' 0:< API_SUCCESS { Starting External DB Application: Demo Tool, Class: Demo.Tool.App, Vendor : DEMO, "
    "Assembly: C:\\addins\\Demo.dll }",
    "' 0:< API_ERROR { : Assembly version conflict in some references in Demo.Plugin.dll assembly",
    "'Addin's module Demo.Core of version 2.0.0.0 conflicts with same preloaded module of version 1.0.0.0 }",
    "' 0:< API_ERROR { : Assembly version conflict in some references in Demo.Plugin.dll assembly",
    "'Addin's module Demo.Core of version 2.0.0.0 conflicts with same preloaded module of version 1.0.0.0 }",
    "'E 05-Oct-2026 15:50:10.000;   0:< ",
    'Jrn.Command "Internal"  , "Open an existing project , ID_REVIT_FILE_OPEN"',
    "'H 05-Oct-2026 15:50:11.000;   0:< ",
    "Jrn.Data  _",
    '        "File Name"  , "IDOK" , "C:\\demo\\model.rvt"',
    "  ' 3:< TaskDialog \"model.rvt is damaged.\"",
    "  'CommonButtons : Close",
    "    'H 05-Oct-2026 15:50:12.000;   3:< ",
    "    Jrn.Data  _",
    '            "TaskDialogResult"  , "model.rvt is damaged." ,  _',
    '             "Close"  _',
    '            , "IDCLOSE" ',
    "  'C 05-Oct-2026 15:50:12.100;   3:< Load exception \"CSyntheticException\", code=7: synthetic file is damaged",
    "  '  6.500000    2:<<FormOrAbandon::openFromModelPath [C:\\demo\\model.rvt] ",
    "'E 05-Oct-2026 15:50:20.000;   0:< ",
    'Jrn.Command "Ribbon"  , "Create a wall , ID_OBJECTS_WALL"',
    "' 1:< 错误: 不满足约束",
    "' 1:< 错误: 不满足约束",
    "'C 05-Oct-2026 15:51:46.312;  ->desktop ExitManagedInstance ",
    "'C 05-Oct-2026 15:51:48.995;  finished recording journal file ",
]


def write_journal(path: Path, lines: list[str], encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode(encoding))
    return path


def build(lines: list[str]):
    """(blocks, summary) for the given lines, as the index computes them."""
    categorizer, builder, collector = Categorizer(load_rules()), BlockBuilder(), SessionCollector()
    blocks = []
    for number, text in enumerate(lines, 1):
        classification = categorizer.classify(text)
        collector.feed(number, text, classification)
        blocks.extend(builder.feed(JournalLine(number, text, False), classification))
    blocks.extend(builder.finish())
    collector.add_blocks(blocks)
    return blocks, collector.finish()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_snapshot_keeps_complete_lines_only(self):
        source = self.root / "journal.0001.txt"
        source.write_bytes(b"first\r\nsecond\r\npartial wri")
        target = self.root / "snap"
        target.mkdir()
        info = take_snapshot(source, target)
        self.assertEqual((target / source.name).read_bytes(), b"first\r\nsecond\r\n")
        self.assertEqual((info["bytes"], info["source_size"], info["trimmed_bytes"]), (15, 26, 11))
        self.assertEqual(source.read_bytes(), b"first\r\nsecond\r\npartial wri")  # the original is untouched

    def test_snapshot_respects_utf16_code_units(self):
        source = self.root / "steel.log"
        data = codecs.BOM_UTF16_LE + "a\r\nb\r\nc".encode("utf-16-le")
        source.write_bytes(data)
        target = self.root / "snap"
        target.mkdir()
        take_snapshot(source, target)
        self.assertEqual((target / source.name).read_bytes(), codecs.BOM_UTF16_LE + "a\r\nb\r\n".encode("utf-16-le"))

    def test_analyze_always_snapshot_is_explicit_in_the_report(self):
        source = write_journal(self.root / "journal.0001.txt", ["DBG_ERROR: synthetic", "tail"])
        with source.open("ab") as stream:
            stream.write(b"half")
        report = analyze(source, snapshot="always")
        self.assertTrue(report.live)
        self.assertEqual(report.source, str(source.resolve()))
        self.assertEqual(report.snapshot["trimmed_bytes"], 4)
        self.assertEqual(report.line_count, 2)
        self.assertTrue(any("实时Journal" in warning for warning in report.warnings))
        directory = None
        with Snapshot(source) as info:
            directory = Path(info["path"]).parent
            self.assertTrue(directory.is_dir())
        self.assertFalse(directory.exists())

    @unittest.skipUnless(os.name == "nt", "write-sharing probe is Windows-only")
    def test_file_held_for_writing_is_read_from_a_snapshot(self):
        from revit_journal_tools.live import held_for_writing

        source = write_journal(self.root / "journal.0001.txt", SESSION[:-1])  # no end marker yet
        self.assertFalse(held_for_writing(source))
        with source.open("ab") as writer:  # stands in for Revit appending to its journal
            writer.write(b"half")
            writer.flush()
            self.assertTrue(held_for_writing(source))
            self.assertTrue(wants_snapshot(source, "auto"))
            report = analyze(source)
            self.assertTrue(report.live)
            self.assertEqual((report.line_count, report.snapshot["trimmed_bytes"]), (len(SESSION) - 1, 4))
            index = ViewerIndex(self.root / "cache.sqlite3", workers=1, local_appdata=self.root / "none")
            index.build(index.register([source], "auto"), "auto")
            item, = index.catalog()
            self.assertEqual((item["status"], item["live"], item["writing"]), ("ready", True, True))
            self.assertEqual(item["store"], "text")  # a snapshot is temporary: its text is kept
            session, = index.sessions()
            self.assertEqual(session["end_state"], "live")  # still being written: not a crash candidate
            self.assertNotIn("session.no_clean_exit", [item["id"] for item in session["session_findings"]])
        self.assertFalse(held_for_writing(source))
        self.assertFalse(index.catalog()[0]["writing"])

    def test_never_and_unknown_modes(self):
        source = write_journal(self.root / "journal.0001.txt", ["x"])
        self.assertFalse(wants_snapshot(source, "never"))
        self.assertTrue(wants_snapshot(source, "always"))
        with self.assertRaises(ValueError):
            wants_snapshot(source, "sometimes")
        report = analyze(source, snapshot="never")
        self.assertFalse(report.live)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.local = Path(self.temporary.name) / "Local"
        for folder in ("Autodesk Revit 2024", "Autodesk Revit 2026", "Autodesk Revit LT 2025"):
            (self.local / "Autodesk" / "Revit" / folder / "Journals").mkdir(parents=True)
        (self.local / "Autodesk" / "Revit" / "Autodesk Revit 2023").mkdir()  # no Journals folder
        (self.local / "Autodesk" / "Revit" / "Addins" / "Journals").mkdir(parents=True)

    def test_folders_newest_version_first_and_filtered(self):
        self.assertEqual([year for year, _ in journal_folders(local_appdata=self.local)], [2026, 2025, 2024])
        self.assertEqual([year for year, _ in journal_folders([2024], self.local)], [2024])
        self.assertEqual(journal_folders(local_appdata=self.local / "missing"), [])

    def test_years_are_validated(self):
        self.assertEqual(parse_years(["2024,2026", "2025"]), [2024, 2026, 2025])
        with self.assertRaises(ValueError):
            parse_years(["24"])

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def test_no_paths_uses_local_journals_and_latest_session(self):
        journals = self.local / "Autodesk" / "Revit" / "Autodesk Revit 2024" / "Journals"
        old = write_journal(journals / "journal.0001.txt", ["DBG_ERROR: old"])
        new = write_journal(journals / "journal.0002.txt", ["DBG_ERROR: new"])
        os.utime(old, (1_790_000_000, 1_790_000_000))
        os.utime(new, (1_790_000_100, 1_790_000_100))
        output = Path(self.temporary.name) / "out.jsonl"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}):
            code, _, errors = self.cli("scan", "--revit", "2024", "--latest", "--format", "jsonl",
                                       "--output", str(output))
            self.assertEqual(code, 0, errors)
            sources = [json.loads(line)["source"] for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sources, [str(new.resolve())])
            code, _, errors = self.cli("scan", "--revit", "2027")
            self.assertEqual(code, 2)
            self.assertIn("没有找到2027", errors)


class PhaseAndBlockTests(unittest.TestCase):
    def test_phases_follow_markers_and_header_records_do_not_end_startup(self):
        blocks, summary = build(SESSION)
        self.assertEqual([item["phase"] for item in summary["phases"]],
                         ["startup", "licensing", "addin_loading", "document_open", "interactive", "shutdown"])
        by_title = {block.title: block for block in blocks}
        self.assertEqual(by_title['Jrn.Data "JournalDefaultTemplate"  , "demo.rte"'].phase, "licensing")
        self.assertEqual(by_title['Jrn.RibbonEvent "TabActivated:Architecture"'].phase, "licensing")
        # The empty 'E record and its command form one block in the command's phase.
        opening = by_title['Jrn.Command "Internal"  , "Open an existing project , ID_REVIT_FILE_OPEN"']
        self.assertEqual((opening.phase, opening.start_line), ("document_open", 25))
        self.assertEqual(by_title['Jrn.Command "Ribbon"  , "Create a wall , ID_OBJECTS_WALL"'].phase, "interactive")

    def test_dialog_and_its_answer_are_dialog_blocks(self):
        blocks, _ = build(SESSION)
        dialogs = [(block.start_line, block.end_line, block.boundary) for block in blocks if block.category == "dialog"]
        self.assertEqual(dialogs[0], (30, 31, "dialog"))
        self.assertEqual(dialogs[1][:2], (32, 36))
        answer = next(block for block in blocks if block.start_line == 32)
        self.assertIn('"TaskDialogResult"', answer.title)

    def test_repeated_content_shares_a_fingerprint_independent_of_time_and_counter(self):
        first = build(["'C 01-Jan-2026 09:00:00.000;   0:< DBG_WARN: same thing - 2 times"])[0][0]
        second = build(["'C 02-Feb-2026 10:11:12.000;   1:< DBG_WARN: same thing - 7 times"])[0][0]
        other = build(["'C 02-Feb-2026 10:11:12.000;   1:< DBG_WARN: another thing"])[0][0]
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.fingerprint, other.fingerprint)


class FingerprintShortcutTests(unittest.TestCase):
    def test_shortcut_equals_full_normalization(self):
        try:
            from test_categories_gates import STRUCTURE, fuzz_journal
        except ImportError:  # run as tests.test_revit_logic
            from tests.test_categories_gates import STRUCTURE, fuzz_journal

        from revit_journal_tools.blocks import normalized, normalized_from, payload

        corpus = [*SESSION, *STRUCTURE, "'  6.5   2:<<Form [x]", "' 0:< 12.5 thing", "' C 05-Oct-2026 1:2:3;x",
                  "'E 05-Oct-2026 15:49:56.000;   0:< ", "  ' 1:< Error: x. - 3 times  ", "' <<< y", "Ā 1 z",
                  "times", "' 0:< - 2 times", *fuzz_journal(7, 4000)]
        for text in corpus:
            self.assertEqual(normalized_from(text, payload(text)), normalized(text), text)


class RevitRuleTests(unittest.TestCase):
    """Each J14 rule: constructed positives in the observed format and close negatives."""

    CASES = {
        "revit.license_error": (
            ["'C 05-Oct-2026 15:49:54.100;   0:< LicenseUpd(2) Adlsdk Error:(9) vendor:EXAMPLE",
             "' 0:< LicenseUpd(3) renewal Error reported"],
            ["' 0:< LicenseUpd(1) renewal complete", "' 0:< License initialization complete",
             "' 0:< Adlsdk status: OK"]),
        "revit.model_open_failed": (
            ["  'C 05-Oct-2026 15:50:12.100;   3:< Load exception \"CSyntheticException\", code=7: damaged",
             "DBG_INFO: openFromModelPath did not complete.: line 1 of synthetic.cpp.",
             "'  3.1     3:<<exceptionsInOnOpenDocument reportSaveLoadException bSaving=no [C:\\demo\\model.rvt]"],
            ["'  6.5    2:<<FormOrAbandon::openFromModelPath [C:\\demo\\model.rvt]", "Load exception without quote"]),
        "revit.steel_localdb": (
            ["SQLLocalDB instance is malfunctioning",
             "' 1:< API_ERROR { System.Exception exception(synthetic) was thrown from a handler of Demo event. "
             "The API event handler was registered by application Steel Connections DB extension (0000). }"],
            ["' 0:< API_SUCCESS { Registering DocumentCreating event by application Steel Connections DB extension "
             "(0000). }", "SQLLocalDB instance started"]),
        "revit.addin_conflict": (
            ["' 0:< API_ERROR { : Assembly version conflict in some references in Demo.Plugin.dll assembly"],
            ["'Addin's module Demo.Core of version 2.0.0.0 conflicts with same preloaded module of version 1.0.0.0 }"]),
        "revit.crash_protection": (
            ["    ' 0:< unhandledExceptionFilter is executed in a non-main thread (id = 1, tag = <blank> : [was ]).",
             "' 0:< Exception occurred during Revit crash protection. UI might be locked.",
             "'Id : TaskDialog_Fatal_Error_Occured",
             "' 0:< SLOG $0000abcd 2026-10-07 15:16:17.155 >Crash   "],
            ["' 0:< SLOG $0000abcd 2026-10-07 15:16:17.155 <Crash ", "' 0:< Exception occurred ",
             "'Id : TaskDialog_Workset_File_Already_Exists", "unhandledExceptionFilter registered"]),
        "revit.cef_init_failed": (
            ["' 0:< CEF failed to initialize due to failure: "],
            ["' 0:< CEF initialized", "'The type initializer for '<Module>' threw an exception."]),
        "revit.ui_warning": (
            ["' 1:< 错误: 不满足约束", "  ' 2:< Error: Can't create Demo. - 3 times", "    ' 1:< ERROR : spaced prompt"],
            ["' 0:< see Error: not a prompt", "' 1:< Warning: not an error prompt", "DBG_ERROR: not a prompt"]),
    }

    def test_positive_and_negative_cases(self):
        rules = load_rules()
        for rule_id, (positives, negatives) in self.CASES.items():
            for text in positives:
                with self.subTest(rule=rule_id, positive=text):
                    self.assertIn(rule_id, {match.rule_id for match in match_rules(rules, text)})
            for text in negatives:
                with self.subTest(rule=rule_id, negative=text):
                    self.assertNotIn(rule_id, {match.rule_id for match in match_rules(rules, text)})

    def test_ui_prompts_and_license_errors_are_warnings_not_errors(self):
        categorizer = Categorizer(load_rules())
        for text in ("' 1:< 错误: 不满足约束", "' 0:< LicenseUpd(2) Adlsdk Error:(9) vendor:EXAMPLE"):
            with self.subTest(text=text):
                category, severity, rules = categorizer.classify(text)
                self.assertEqual((category, severity), ("warning", "warning"))
                self.assertNotIn("generic.error_marker", rules)
        # Every level-prefixed "N:< ERROR:" line observed locally is a modelling prompt;
        # other error markers stay errors.
        self.assertEqual(categorizer.classify("' 0:< worker> ERROR: synthetic failure")[0], "error")
        match = next(match for match in match_rules(load_rules(), "' 1:< 错误: 不满足约束")
                     if match.rule_id == "revit.ui_warning")
        self.assertEqual(match.tags, ("ui_warning",))

    def test_add_in_conflict_stays_with_the_api_error(self):
        category, severity, rules = Categorizer(load_rules()).classify(
            "' 0:< API_ERROR { : Assembly version conflict in some references in Demo.Plugin.dll assembly")
        self.assertEqual((category, severity), ("api_error", "error"))
        self.assertEqual(rules.split(","), ["revit.api_error", "revit.addin_conflict"])


class SummaryTests(unittest.TestCase):
    def test_session_facts_with_line_numbers(self):
        _, summary = build(SESSION)
        self.assertEqual((summary["build"], summary["branch"], summary["release"]),
                         ("20990101_0000(x64)", "RELEASE_DEMO", "2099.1"))
        self.assertEqual(summary["pid"], 4242)
        self.assertEqual(summary["start_time"], "2026-10-05T15:49:53.777")
        self.assertEqual(summary["duration_seconds"], 115.218)
        self.assertEqual((summary["clean_exit"], summary["finished_line"]), (True, 44))
        self.assertEqual([item["line_number"] for item in summary["license"]["errors"]], [10])
        self.assertEqual(summary["documents"], [{"path": "C:\\demo\\model.rvt", "first_line": 38, "count": 1}])
        self.assertEqual([item["line_number"] for item in summary["open_failures"]], [37])
        self.assertEqual(summary["addins"]["conflicts"],
                         [{"assembly": "Demo.Plugin.dll", "count": 2, "first_line": 21}])
        self.assertEqual(summary["addins"]["third_party_count"], 1)
        self.assertEqual(summary["dialogs"]["count"], 1)
        self.assertEqual(summary["dialogs"]["last"], [{"line_number": 30, "text": "model.rvt is damaged."}])

    def test_findings_are_candidates_and_no_clean_exit_is_flagged(self):
        _, summary = build(SESSION[:-1])
        self.assertFalse(summary["clean_exit"])
        findings = session_findings(summary, False, [], "journal.0001.txt")
        self.assertEqual([item["id"] for item in findings],
                         ["session.no_clean_exit", "session.license_error", "session.model_open_failed",
                          "session.addin_conflict"])
        self.assertTrue(all(item["status"] == "candidate" for item in findings))
        self.assertEqual(findings[0]["evidence"][0]["line_number"], 43)
        # A journal still being written has no end yet: that is not a crash candidate.
        self.assertNotIn("session.no_clean_exit", [item["id"] for item in session_findings(summary, True, [], "j")])
        _, clean = build(SESSION)
        self.assertNotIn("session.no_clean_exit", [item["id"] for item in session_findings(clean, False, [], "j")])


class CrashSummaryTests(unittest.TestCase):
    """Crash path in the shape of the uploaded cases (synthetic text)."""

    CRASH = SESSION[:-2] + [
        "    'E 05-Oct-2026 15:51:43.527;   0:< ",
        '    Jrn.Command "Internal"  , "Manage Links , ID_LINKED_DWG" ',
        "    ' 0:< Exception occurred ",
        "    'C 05-Oct-2026 15:51:44.446;   0:< ExceptionCode=0x80000003 ExceptionFlags=0x00000000 "
        "ExceptionAddress=00007FFA00000000 ",
        "    'C 05-Oct-2026 15:51:45.458;  Revit worker 1 closed cleanly ",
        "    ' 0:< unhandledExceptionFilter is executed in a non-main thread (id = 1, tag = <blank> : [was ]). ",
    ]

    def test_crash_marks_keep_the_command_before_the_exception(self):
        _, summary = build(self.CRASH)
        marks = summary["crash"]["marks"]
        self.assertEqual([mark["kind"] for mark in marks], ["exception", "exception_code", "unhandled"])
        self.assertEqual(marks[0]["command"]["line_number"], 44)
        self.assertIn("ID_LINKED_DWG", marks[0]["command"]["text"])
        self.assertEqual((marks[1]["code"], marks[1]["meaning"]), ("0x80000003", "断点/内部断言（程序主动中断）"))
        findings = session_findings(summary, False, [], "journal.0001.txt")
        crash = next(item for item in findings if item["id"] == "session.crash")
        self.assertEqual((crash["severity"], crash["status"]), ("fatal", "candidate"))
        self.assertIn("ID_LINKED_DWG", crash["explanation"])
        self.assertEqual(crash["evidence"][0]["line_number"], 44)

    def test_fatal_dialog_by_id_and_cef_detail(self):
        lines = SESSION[:-2] + [
            "' 0:< CEF failed to initialize due to failure: ",
            "'The type initializer for '<Module>' threw an exception. ",
            "' 0:< TaskDialog \"In Revit ist ein schwerwiegender Fehler aufgetreten. \"",
            "'Id : TaskDialog_Fatal_Error_Occured",
        ] + SESSION[-2:]
        _, summary = build(lines)
        marks = summary["crash"]["marks"]
        self.assertEqual([mark["kind"] for mark in marks], ["cef_failed", "fatal_dialog"])
        self.assertIn("type initializer", marks[0]["detail"])
        # A clean end does not hide a fatal-error dialog.
        crash = next(item for item in session_findings(summary, False, [], "j") if item["id"] == "session.crash")
        self.assertEqual(crash["severity"], "fatal")

    def test_a_recovered_exception_alone_is_an_error_candidate(self):
        lines = SESSION[:-2] + ["' 0:< Exception occurred ",
                                "'C 05-Oct-2026 15:51:44.446;   0:< ExceptionCode=0xe0434352 ExceptionFlags=0x81 "]             + SESSION[-2:]
        _, summary = build(lines)
        crash = next(item for item in session_findings(summary, False, [], "j") if item["id"] == "session.crash")
        self.assertEqual(crash["severity"], "error")
        self.assertIn(".NET托管异常", crash["explanation"])
        _, clean = build(SESSION)
        self.assertNotIn("session.crash", [item["id"] for item in session_findings(clean, False, [], "j")])


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal = write_journal(self.root / "Local" / "Autodesk Revit 2024" / "Journals" / "journal.0011.txt",
                                     SESSION)
        self.steel = self.root / "ProgramData" / "Autodesk" / "Revit Steel Connections 2024" / "en-US"
        self.steel.mkdir(parents=True)

    def write_steel(self, entries: str, modified: datetime) -> Path:
        log = self.steel / "DatabaseConnectionErrors.log"
        log.write_bytes(codecs.BOM_UTF16_LE + entries.encode("utf-16-le"))
        os.utime(log, (modified.timestamp(), modified.timestamp()))
        return log

    def test_steel_log_entries_and_time_basis(self):
        log = self.write_steel("[1][15:51:10] LocalDB failed.\r\n\r\n\r\n[2][15:51:10] Version missing.\r\n",
                               datetime(2026, 10, 5, 15, 51, 20))
        entries = parse_steel_log(log)
        self.assertEqual([(item["index"], item["time"], item["line_number"]) for item in entries],
                         [(1, "15:51:10", 1), (2, "15:51:10", 4)])
        window = session_window(self.journal)
        self.assertEqual(window, (datetime(2026, 10, 5, 15, 49, 53), datetime(2026, 10, 5, 15, 51, 48)))
        self.assertEqual(match_steel_log(log, entries, window), "entry_time")
        # Modified on another day (observed on a real machine): time of day only, a weaker basis.
        os.utime(log, (datetime(2026, 10, 4, 21, 51, 10).timestamp(),) * 2)
        self.assertEqual(match_steel_log(log, entries, window), "time_of_day")
        self.assertEqual(match_steel_log(log, entries, (datetime(2026, 10, 5, 9), datetime(2026, 10, 5, 10))), "")

    def test_steel_log_attaches_to_the_session_of_the_same_year(self):
        self.write_steel("[1][15:51:10] LocalDB failed.\r\n", datetime(2026, 10, 5, 15, 51, 20))
        attached = attach_steel_logs([self.journal], self.root / "ProgramData")
        (log, name), = attached.items()
        self.assertEqual((name.label, name.role), ("journal.0011", "steel_log"))
        other_year = write_journal(self.root / "Local" / "Autodesk Revit 2025" / "Journals" / "journal.0011.txt",
                                   SESSION)
        self.assertEqual(attach_steel_logs([other_year], self.root / "ProgramData"), {})

    def test_cer_log_matches_by_process_id(self):
        logs = [{"path": "a", "pid": 4242, "release": 2024, "first_time": None, "modified": "", "size": 1},
                {"path": "b", "pid": 1, "release": 2024, "first_time": None, "modified": "", "size": 1},
                {"path": "c", "pid": 4242, "release": 2025, "first_time": None, "modified": "", "size": 1}]
        _, summary = build(SESSION)
        self.assertEqual([item["path"] for item in match_cer(summary, 2024, logs)], ["a"])

    def test_index_session_lists_steel_log_and_findings(self):
        self.write_steel("[1][15:51:10] LocalDB failed.\r\n", datetime(2026, 10, 5, 15, 51, 20))
        attached = attach_steel_logs([self.journal], self.root / "ProgramData")
        from revit_journal_tools.sessions import plan_inputs

        index = ViewerIndex(self.root / "cache.sqlite3", workers=1, local_appdata=self.root / "none")
        index.build(index.register(plan_inputs([self.journal], attached), "auto"), "auto")
        session, = index.sessions()
        self.assertEqual(session["end_state"], "clean")
        steel = [item for item in session["sidecar_logs"] if item["kind"] == "steel_log"]
        self.assertEqual(len(steel), 1)
        self.assertIn("条目时间", steel[0]["basis"])
        ids = [item["id"] for item in session["session_findings"]]
        self.assertIn("session.steel_localdb", ids)
        self.assertNotIn("session.no_clean_exit", ids)

    def test_explicit_journal_encoding_does_not_apply_to_the_utf16_steel_log(self):
        write_journal(self.journal, SESSION, "gb18030")
        self.write_steel("[1][15:51:10] LocalDB failed.\r\n", datetime(2026, 10, 5, 15, 51, 20))
        from revit_journal_tools.sessions import plan_inputs

        planned = plan_inputs([self.journal], attach_steel_logs([self.journal], self.root / "ProgramData"))
        index = ViewerIndex(self.root / "cache.sqlite3", workers=1, local_appdata=self.root / "none")
        index.build(index.register(planned, "gb18030"), "gb18030")
        encodings = {item["role"]: (item["status"], item["encoding"]) for item in index.catalog()}
        self.assertEqual(encodings, {"main": ("ready", "gb18030"), "steel_log": ("ready", "utf-16")})
        # Registering again with the same request reuses both files.
        self.assertEqual(index.register(planned, "gb18030"), [])


class IndexViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        lines = SESSION[:-2] + ["'C 05-Oct-2026 15:51:00.000;   0:< DBG_WARN: same thing - 2 times",
                                "'C 05-Oct-2026 15:51:01.000;   0:< DBG_WARN: same thing - 3 times",
                                "'C 05-Oct-2026 15:51:02.000;   0:< DBG_WARN: same thing - 4 times"] + SESSION[-2:]
        self.journal = write_journal(root / "journal.0001.txt", lines)
        self.index = ViewerIndex(root / "cache.sqlite3", workers=1, local_appdata=root / "none")
        self.index.build(self.index.register([self.journal], "auto"), "auto")
        self.file_id = self.index.catalog()[0]["id"]

    def test_fold_lists_repeats_once_with_count(self):
        folded = self.index.blocks(self.file_id, "warning", fold=True)["items"]
        same = [block for block in folded if "same thing" in block["title"]]
        self.assertEqual([(block["start_line"], block["fold_count"], block["fold_last_line"]) for block in same],
                         [(43, 3, 45)])
        # The two UI prompts are also one entry, counted twice.
        self.assertIn(2, [block["fold_count"] for block in folded if "不满足约束" in block["title"]])
        plain = self.index.blocks(self.file_id, "warning")["items"]
        self.assertEqual(len([block for block in plain if "same thing" in block["title"]]), 3)
        members = self.index.fold_members(self.file_id, same[0]["fingerprint"])
        self.assertEqual((members["count"], members["start_lines"]), (3, [43, 44, 45]))
        searched = self.index.blocks(self.file_id, "all", "same thing", fold=True)["items"]
        self.assertEqual(len(searched), 3)  # a keyword search never folds

    def test_phase_filter_and_tail(self):
        opening = self.index.blocks(self.file_id, phase="document_open")["items"]
        self.assertTrue(opening)
        self.assertTrue(all(block["phase"] == "document_open" for block in opening))
        with self.assertRaises(ValueError):
            self.index.blocks(self.file_id, phase="lunch")
        tail = self.index.tail_blocks(self.file_id, 3)
        self.assertEqual([block["category"] for block in tail], ["dialog", "dialog", "operation"])
        self.assertEqual(tail[-1]["start_line"], 39)
        self.assertFalse(any(block["title"].startswith("Jrn.Directive")
                             for block in self.index.tail_blocks(self.file_id, 50)))

    def test_browser_keys_for_summary_tail_phase_fold_and_dialogs(self):
        from revit_journal_tools.terminal import Browser

        browser = Browser(self.index)
        browser.refresh()
        self.assertTrue(browser.fold)
        browser.handle("s")
        browser.refresh()
        frame = browser.frame(120, 80, False)
        self.assertIn("正常结束", frame)
        self.assertIn("会话级发现", frame)
        browser.handle("e")
        browser.refresh()
        self.assertEqual(browser.view, "tail")
        self.assertIn("结束前最后", browser.frame(120, 40, False))
        browser.handle("P")
        browser.refresh()
        self.assertEqual((browser.view, browser.phase), ("blocks", "startup"))
        self.assertTrue(all(item["phase"] == "startup" for item in browser.items))
        browser.handle("9")
        self.assertEqual(browser.category, "dialog")
        browser.handle("z")
        self.assertFalse(browser.fold)
        for width, height in ((100, 25), (40, 12)):
            frame = browser.frame(width, height, False)
            self.assertLessEqual(len(frame.split("\r\n")), height)

    def test_web_api_serves_the_same_paragraphs_session_and_folds(self):
        # J24: the Web viewer reuses ViewerIndex.blocks/block_lines/fold_members and summary_lines.
        server = make_server(self.index, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, no system proxy
        base = f"http://127.0.0.1:{server.server_port}"

        def url(path, params):
            return base + path + "?" + urllib.parse.urlencode({"file": self.file_id, **params})

        def get(path, **params):
            with opener.open(url(path, params)) as response:
                return json.load(response)

        try:
            listed = get("/api/blocks", category="warning", fold=1)["items"]
            expected = self.index.blocks(self.file_id, "warning", limit=100, fold=True)["items"]
            self.assertEqual([{key: value for key, value in item.items() if key not in ("label", "summary")}
                              for item in listed], expected)
            self.assertEqual([item["summary"] for item in listed], [block_summary(item) for item in expected])
            same = next(item for item in listed if "same thing" in item["title"])
            self.assertEqual(same["fold_count"], 3)
            self.assertEqual(get("/api/fold", fingerprint=same["fingerprint"])["start_lines"], [43, 44, 45])
            opening = get("/api/blocks", phase="document_open", fold=0)["items"]
            self.assertTrue(opening and all(item["phase"] == "document_open" for item in opening))
            block = get("/api/block_at", line=30)
            detail = get("/api/block", start=block["start_line"])
            self.assertEqual([item["line_number"] for item in detail["items"]],
                             list(range(block["start_line"], block["end_line"] + 1)))
            self.assertEqual(detail["sha256"], self.index.catalog()[0]["sha256"])
            self.assertIn("encoding_basis_label", detail)
            session = get("/api/session")
            catalog = self.index.catalog()
            expected_session, = self.index.sessions(catalog)
            self.assertEqual(session["lines"],
                             summary_lines(expected_session, main_tail(self.index, expected_session, catalog, 10)))
            self.assertEqual((session["main_id"], session["end_state"]), (self.file_id, "clean"))
            for path, params in (("/api/blocks", {"fold": "yes"}), ("/api/blocks", {"phase": "lunch"}),
                                 ("/api/fold", {"fingerprint": ""}), ("/api/session", {"file": 999})):
                with self.subTest(path=path, params=params), self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(url(path, params))
                self.assertIn(raised.exception.code, (400, 404))
                raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_summary_command_reports_end_state(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["summary", str(self.journal), "--database", str(Path(self.temporary.name) / "s.sqlite3"),
                         "--format", "json", "--jobs", "1", "--tail", "2"])
        self.assertEqual(code, 0, errors.getvalue())
        session, = json.loads(output.getvalue())["sessions"]
        self.assertEqual(session["end_state"], "clean")
        self.assertEqual(session["summary"]["finished_line"], 47)
        self.assertEqual(len(session["tail"]), 2)


if __name__ == "__main__":
    unittest.main()
