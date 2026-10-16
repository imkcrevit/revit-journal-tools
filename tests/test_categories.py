"""Line categories by session stage: add-in and exit records, and the start-up,
session and exit defaults for lines no category claims. Synthetic, anonymized
lines shaped like Revit journal records; they do not prove real-case accuracy."""

import tempfile
import unittest
from pathlib import Path

from revit_journal_tools.blocks import BlockBuilder
from revit_journal_tools.categories import CATEGORIES, Categorizer
from revit_journal_tools.i18n import EN
from revit_journal_tools.reader import JournalLine
from revit_journal_tools.rules import load_rules
from revit_journal_tools.viewer_index import ViewerIndex

RULES = load_rules()
ROOT = Path(__file__).resolve().parent.parent
COMMAND = 'Jrn.Command "Ribbon" , "Synthetic command , ID_SYNTHETIC"'
DELTA = "' 0:< ::12:: Delta VM: Avail +1 -> 100 MB, Used +0 -> 50 MB"
FINISHED = "'C 01-Oct-2026 10:00:09.001;  finished recording journal file"
ADDIN_LINES = [
    "' [Jrn.AddInManifest] Rvt.Attr.AddInManifest: Synthetic.addin Rvt.Attr.AddInType: ExternalApplication",
    "' [Jrn.RegisterExternalServer] Rvt.Attr.ExternalServer: 00000000-0000-0000-0000-000000000001",
    "  ' [Jrn.AddInDynamicUpdate] Rvt.Attr.AddInId: 00000000-0000-0000-0000-000000000002",
    "  ' [Jrn.ExternalEventExecution] Rvt.Attr.AddInName: Synthetic Tool",
    "' [Jrn.ExternalServerExecution] Rvt.Attr.ExternalServerExecutionCount: 1",
    "' [Jrn.ExternalResourceLoad] Rvt.Attr.ExternalResourceType: 00000000-0000-0000-0000-000000000003 0",
    ' Jrn.AddInEvent "AddInJournaling" , "WpfWindow(Synthetic).WpfButton(0,OkButton).Click()"',
    "' 1:< API_SUCCESS { Registered an external server: Name(Synthetic); VendorId(DEMO); ServerId(1) } ",
    "' 0:< Registered an external service: Synthetic",
    "' 0:< Unregistering all external services. ",
    "' 0:< The add-in folder 'C:\\Synthetic\\AddIns\\Tool' was registered for the context 'TOOL'. ",
    "' 0:< Add-in manifest file from: C:\\Synthetic\\Tool.addin, will not be loaded. ",
    "' 1:< The requested assembly 'Synthetic.Tool, Version=1.0.0.0' was loaded to the context 'TOOL' from 'C:\\x'",
    "' 0:< ExternalDBApplication Synthetic.Db was not loaded",
    "' 0:< AddInJournalClient load journal control: SyntheticView = C:\\Synthetic\\Tool.dll ",
    "' 0:< Third Party Updater 'Synthetic: NumberingUpdater' has been unregistered. ",
    "' 0:< Forcibly unregistering Updater with id:00000000-0000-0000-0000-000000000004 ",
    "' 0:< Forcibly removing all update triggers for all Updaters ",
    "' 0:< API_SUCCESS { Starting External Application: Synthetic Tool, Class: Synthetic.App, Vendor : DEMO() } ",
    "' 0:< Loading External DB Application: Synthetic Db",
]
# Near misses: the same words without the add-in record around them.
NOT_ADDIN_LINES = [
    ("' 0:< API_SUCCESS { Registering event by application Synthetic Tool } ", "api"),
    ("API_INFO { Diagnostic note: Starting External Application: Example }", "api"),
    ("' 0:< The requested assembly 'Synthetic' could not be found", "other"),
    ("' 0:< The add-in folders were scanned", "other"),
    ("' 0:< Updater registered for the document", "other"),
    ("' 0:< Registered external services: 3", "other"),
]
EXIT_LINES = [
    "' 0:< 1 journal(s) aged out. ",
    "' 0:< 2 worker log(s) aged out. ",
    "' 0:< Total session time spent reclaiming memory: 0.000000 seconds. ",
    "' 0:< CefSharp has been shutdown successfully! ",
]
NOT_EXIT_LINES = [
    "' 0:< CefSharp has been initialized",
    "' 0:< 1 journal(s) kept.",
    "' 0:< Total session time: 5 minutes",
    "' 0:< License check starting",
]


def classify(lines, categorizer=None):
    categorizer = categorizer or Categorizer(RULES)
    return [categorizer.classify(text)[0] for text in lines]


def grouped(lines):
    categorizer, builder = Categorizer(RULES), BlockBuilder()
    classes, result = [], []
    for number, text in enumerate(lines, 1):
        classification = categorizer.classify(text)
        classes.append(classification[0])
        result.extend(builder.feed(JournalLine(number, text), classification))
    return classes, result + builder.finish()


class CategoryTableTests(unittest.TestCase):
    def test_categories_replace_system_data(self):
        self.assertEqual(list(CATEGORIES), ["startup", "addin", "exit", "error", "api_error", "operation", "warning",
                                            "api", "dialog", "other"])
        self.assertEqual(set(EN["category"]), set(CATEGORIES))


class AddinCategoryTests(unittest.TestCase):
    def test_each_addin_record_family_is_addin_in_every_stage(self):
        for prefix, stage in (([], "startup"), ([COMMAND], "session"), ([FINISHED], "shutdown")):
            for text in ADDIN_LINES:
                with self.subTest(stage=stage, text=text):
                    self.assertEqual(classify(prefix + [text])[-1], "addin")

    def test_near_misses_are_not_addin(self):
        for text, category in NOT_ADDIN_LINES:
            with self.subTest(text=text):
                self.assertEqual(classify([COMMAND, text])[-1], category)

    def test_loading_braces_are_one_addin_record(self):
        lines = ["'E 01-Oct-2026 10:00:00.001; 0:<",
                 "' 0:< API_SUCCESS { Starting External Application: Synthetic Tool,",
                 "'Class: Synthetic.App,", "'Vendor : ADSK(Autodesk, https://www.autodesk.com),",
                 "'Assembly: Synthetic.dll }", DELTA]
        classes, blocks = grouped(lines)
        self.assertEqual(classes, ["startup", "addin", "addin", "addin", "addin", "startup"])
        self.assertEqual([(b.start_line, b.end_line, b.category, b.addin_origin) for b in blocks],
                         [(1, 5, "addin", "autodesk"), (6, 6, "startup", "")])

    def test_failed_loading_stays_an_api_error_with_its_origin(self):
        classes, blocks = grouped(["' 0:< API_ERROR { Starting External Application: Synthetic Tool,",
                                   "'Vendor : ADSK(Autodesk, https://www.autodesk.com),", "'Synthetic failure }", DELTA])
        self.assertEqual(classes, ["api_error", "api_error", "api_error", "startup"])
        self.assertEqual([(b.category, b.addin_origin) for b in blocks], [("api_error", "autodesk"), ("startup", "")])

    def test_unclosed_loading_braces_end_at_a_new_record(self):
        start = "API_SUCCESS { Starting External Application: Synthetic Tool,"
        self.assertEqual(classify([start, "'Vendor : DEMO()", "'C 01-Oct-2026 10:00:01.001;   0:< ", DELTA]),
                         ["addin", "addin", "startup", "startup"])
        self.assertEqual(classify([start, COMMAND, DELTA]), ["addin", "operation", "other"])

    def test_external_server_record_is_an_addin_block(self):
        # ServerId(1) is not the ID of the journal record: two server blocks
        # (tests/test_addin_records.py has the merged case).
        _, blocks = grouped([ADDIN_LINES[7], "'E 01-Oct-2026 10:00:00.001; 0:<", ADDIN_LINES[1]])
        self.assertEqual([(b.start_line, b.end_line, b.category, b.addin_kind, b.addin_origin) for b in blocks],
                         [(1, 1, "addin", "server", "third_party"), (2, 3, "addin", "server", "unknown")])


class StageTests(unittest.TestCase):
    def test_unclaimed_lines_follow_the_stage(self):
        self.assertEqual(classify([DELTA, COMMAND, DELTA, FINISHED, DELTA, COMMAND, DELTA]),
                         ["startup", "operation", "other", "exit", "exit", "operation", "exit"])

    def test_blank_lines_are_always_other(self):
        self.assertEqual(classify(["", COMMAND, "   ", FINISHED, "\t"]), ["other", "operation", "other", "exit", "other"])

    def test_exit_markers_are_exit_before_the_shutdown_stage(self):
        for text in EXIT_LINES:
            with self.subTest(text=text):
                # Explicit markers, not the stage: the next unclaimed line is still "other".
                self.assertEqual(classify([COMMAND, text, DELTA]), ["operation", "exit", "other"])
        for text in NOT_EXIT_LINES:
            with self.subTest(text=text):
                self.assertEqual(classify([COMMAND, text]), ["operation", "other"])

    def test_shutdown_markers_start_the_exit_stage(self):
        for text in ("'C 01-Oct-2026 10:00:05.001;  ->desktop ExitManagedInstance ",
                     "'C 01-Oct-2026 10:00:06.001;   0:< License shutdown starting ",
                     "'C 01-Oct-2026 10:00:07.001;  Journal Exit ", FINISHED):
            with self.subTest(text=text):
                self.assertEqual(classify([COMMAND, text, DELTA]), ["operation", "exit", "exit"])

    def test_passive_records_do_not_end_startup_but_opening_does(self):
        self.assertEqual(classify(['Jrn.Directive "DebugMode" , "Synthetic" , 1', 'Jrn.Data "File Name" , "x"', DELTA]),
                         ["operation", "operation", "startup"])
        self.assertEqual(classify(["' 0:< openFromModelPath [C:\\Synthetic\\Model.rvt]", DELTA]), ["other", "other"])

    def test_explicit_startup_markers_keep_their_category_later(self):
        self.assertEqual(classify([COMMAND, "' 0:< Starting Revit", "' 0:< Starting External Revit text"]),
                         ["operation", "startup", "other"])

    def test_stage_and_block_phase_agree_on_marker_lines(self):
        lines = ["'C 01-Oct-2026 10:00:00.001;  started recording journal file", "' Build: synthetic", DELTA,
                 "'C 01-Oct-2026 10:00:01.001;   0:< ", COMMAND, DELTA, FINISHED, DELTA]
        classes, blocks = grouped(lines)
        self.assertEqual(classes, ["startup", "startup", "startup", "startup", "operation", "other", "exit", "exit"])
        self.assertEqual([(b.start_line, b.end_line, b.category, b.phase) for b in blocks],
                         [(1, 2, "startup", "startup"), (3, 3, "startup", "startup"),
                          (4, 5, "operation", "interactive"), (6, 6, "other", "interactive"),
                          (7, 7, "exit", "shutdown"), (8, 8, "exit", "shutdown")])

    def test_stage_follows_block_phase_on_every_line(self):
        stage_of = {"startup": "startup", "licensing": "startup", "addin_loading": "startup",
                    "document_open": "session", "interactive": "session", "shutdown": "shutdown"}
        journals = [path.read_text(encoding="utf-8").splitlines() for path in sorted((ROOT / "examples").glob("*.txt"))]
        journals.append(["' 0:< License initialization", *ADDIN_LINES, "'C 01-Oct-2026 10:00:01.001;   0:< ",
                         'Jrn.Data "File Name" , "x"', "' 0:< openFromModelPath [C:\\x.rvt]", DELTA, COMMAND, DELTA,
                         *EXIT_LINES, FINISHED, DELTA, COMMAND, ""])
        for number, lines in enumerate(journals):
            categorizer, builder = Categorizer(RULES), BlockBuilder()
            for line_number, text in enumerate(lines, 1):
                with self.subTest(journal=number, line=line_number):
                    builder.feed(JournalLine(line_number, text), categorizer.classify(text))
                    self.assertEqual(categorizer._stage, stage_of[builder.phase])

    def test_memo_cache_does_not_keep_an_earlier_stage(self):
        categorizer = Categorizer(RULES)
        before = categorizer.classify(DELTA)[0]
        categorizer.classify(COMMAND)
        during = [categorizer.classify(DELTA)[0] for _ in range(2)]
        categorizer.classify(FINISHED)
        after = categorizer.classify(DELTA)[0]
        self.assertEqual((before, during, after), ("startup", ["other", "other"], "exit"))
        # A cached entry restores the stage it moved to, not only the category.
        first, second = Categorizer(RULES), Categorizer(RULES)
        first._cache = second._cache
        self.assertEqual(classify([COMMAND, DELTA], first), ["operation", "other"])
        self.assertEqual(classify([COMMAND, DELTA], second), ["operation", "other"])
        self.assertEqual(second._stage, "session")


class IndexCategoryTests(unittest.TestCase):
    def test_index_counts_and_filters_use_the_new_categories(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "journal.0001.txt"
            path.write_text("\n".join([
                "'C 01-Oct-2026 10:00:00.001;  started recording journal file", DELTA,
                "' 0:< API_SUCCESS { Starting External Application: Synthetic Tool, Vendor : ADSK() } ",
                "' 0:< API_ERROR { Starting External Application: Broken Tool, Vendor : DEMO() } ",
                "'C 01-Oct-2026 10:00:01.001;   0:< ", COMMAND, DELTA, ADDIN_LINES[15],
                "'C 01-Oct-2026 10:00:05.001;  ->desktop ExitManagedInstance ", DELTA, "", FINISHED,
            ]) + "\n", encoding="utf-8")
            index = ViewerIndex(root / "viewer.sqlite3", workers=1)
            index.build(index.register([path], "auto"), "auto")
            file_id = index.catalog()[0]["id"]
            counts, block_counts = index.catalog()[0]["counts"], index.catalog()[0]["block_counts"]
            self.assertNotIn("system", counts)
            self.assertEqual((counts["startup"], counts["addin"], counts["exit"], counts["other"]), (3, 2, 3, 2))
            self.assertEqual([item["line_number"] for item in index.events(file_id, "addin")["items"]], [3, 8])
            self.assertEqual([item["line_number"] for item in index.events(file_id, "exit")["items"]], [9, 10, 12])
            self.assertEqual([b["start_line"] for b in index.blocks(file_id, "addin")["items"]], [3, 8])
            self.assertEqual([(b["start_line"], b["addin_origin"]) for b in index.blocks(file_id, "addin_third_party")["items"]],
                             [(4, "third_party")])
            self.assertEqual((block_counts["addin"], block_counts["addin_autodesk"], block_counts["api_error"]), (2, 1, 1))
            shutdown = [b for b in index.blocks(file_id, phase="shutdown")["items"]]
            self.assertEqual([(b["start_line"], b["category"]) for b in shutdown],
                             [(9, "exit"), (10, "exit"), (12, "exit")])


if __name__ == "__main__":
    unittest.main()
