import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from revit_journal_tools.blocks import BlockBuilder
from revit_journal_tools.categories import Categorizer
from revit_journal_tools.cli import main
from revit_journal_tools.reader import JournalLine
from revit_journal_tools.rules import load_rules
from revit_journal_tools.terminal import Browser, cell_width
from revit_journal_tools.viewer_index import ViewerIndex


def grouped(lines):
    builder, categorizer = BlockBuilder(), Categorizer(load_rules())
    blocks = []
    for number, text in enumerate(lines, 1):
        blocks.extend(builder.feed(JournalLine(number, text), categorizer.classify(text)))
    return blocks + builder.finish()


class AddinIdentityTests(unittest.TestCase):
    def test_loading_prefix_extracts_name_and_class(self):
        for prefix in ["' 0:< API_SUCCESS { Starting External DB Application:",
                       "'E 01-Oct-2026 10:00:00.001; 0:< API_ERROR { Starting External Application:",
                       "Creating Add-in:", "Loaded External Application:"]:
            with self.subTest(prefix=prefix):
                block = grouped([prefix + " ATF Translation Server, Class: ATFTranslationServer.ATFTranslationServerRegister, Vendor: ADSK() }"])[0]
                self.assertEqual(block.addin_name, "ATF Translation Server")
                self.assertEqual(block.addin_class, "ATFTranslationServer.ATFTranslationServerRegister")
                self.assertEqual(block.addin_origin, "autodesk")

    def test_names_and_classes_can_continue_on_later_lines(self):
        block = grouped(["API_SUCCESS { Starting External DB Application:",
                         "'ATF Translation Server,", "'Class:",
                         "'ATFTranslationServer.", "'ATFTranslationServerRegister,",
                         "'Vendor: ADSK() }"])[0]
        self.assertEqual(block.addin_name, "ATF Translation Server")
        self.assertEqual(block.addin_class, "ATFTranslationServer.ATFTranslationServerRegister")
        self.assertEqual((block.start_line, block.end_line), (1, 6))

    def test_prefix_is_an_event_marker_not_an_arbitrary_substring(self):
        for text in ["API_INFO { Diagnostic note: Starting External Application: Example, Vendor: ADSK() }",
                     "The requested assembly ATFTranslationServer was loaded from Autodesk.",
                     "API_SUCCESS { Registering event by ATF Translation Server, Class: Example, Vendor: ADSK() }"]:
            with self.subTest(text=text):
                self.assertTrue(all(not b.addin_origin and not b.addin_name for b in grouped([text])))

    def test_vendor_does_not_become_name_and_fields_do_not_leak(self):
        blocks = grouped(["API_SUCCESS { Starting External Application: First, Class: Synthetic.First, Vendor: ADSK() }",
                          "API_SUCCESS { Starting External Application: Class: Synthetic.Second, Vendor: DEMO() }",
                          "API_SUCCESS { Starting External Application: Vendor: DEMO() }"])
        self.assertEqual([(b.addin_name, b.addin_class) for b in blocks],
                         [("First", "Synthetic.First"), ("", "Synthetic.Second"), ("", "")])
        self.assertEqual(blocks[1].addin_origin, "third_party")

    def test_large_identity_is_bounded_and_explicitly_marked(self):
        block = grouped(["API_SUCCESS { Starting External Application: " + "N" * 2000
                         + ", Class: " + "C" * 2000 + ", Vendor: ADSK() }"])[0]
        self.assertEqual(len(block.addin_name), 512)
        self.assertEqual(len(block.addin_class), 512)
        self.assertTrue(block.addin_identity_truncated)
        self.assertEqual(block.addin_origin, "autodesk")


class AddinListTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "journal.txt"
        self.path.write_text("'E 01-Oct-2026 10:00:00.001; 0:<\n"
                             "API_SUCCESS { Starting External DB Application: ATF Translation Server,\n"
                             "'Class: ATFTranslationServer.ATFTranslationServerRegister,\n"
                             "'Vendor: ADSK(), Assembly: Synthetic.dll }\n"
                             "API_ERROR { Starting External Application: ATF Helper, Class: ATF.Helper, Vendor: DEMO() }\n"
                             "API_SUCCESS { Starting External Application: Other ATF, Class: Synthetic.ATF, Vendor: DEMO() }\n"
                             "API_SUCCESS { Registered event by ATF Translation Server }\n", encoding="utf-8")
        self.database = self.root / "index.sqlite3"
        self.index = ViewerIndex(self.database)
        queued = self.index.register([self.path], "auto")
        self.index.build(queued, "auto")
        self.file_id = queued[0]

    def test_list_and_prefix_filters_use_name_or_class_start(self):
        self.assertEqual([b["start_line"] for b in self.index.blocks(self.file_id, "addin")["items"]], [1, 5, 6])
        self.assertEqual(self.index.catalog()[0]["block_counts"]["addin"], 3)
        for prefix, expected in [("ATFTranslationServer", [1]), ("ATF ", [1, 5]),
                                 ("atftranslationserver", [1]), ("Translation", []), ("Synthetic.ATF", [6])]:
            with self.subTest(prefix=prefix):
                result = self.index.blocks(self.file_id, addin_prefix=prefix)
                self.assertEqual([b["start_line"] for b in result["items"]], expected)

    def test_prefix_search_source_filters_and_pagination_compose(self):
        result = self.index.blocks(self.file_id, "addin", query="ADSK", addin_prefix="ATFTranslationServer")
        self.assertEqual([(b["start_line"], b["end_line"]) for b in result["items"]], [(1, 4)])
        self.assertEqual(self.index.blocks(self.file_id, "addin_third_party", addin_prefix="ATFTranslationServer")["items"], [])
        first = self.index.blocks(self.file_id, "addin", query="Application", addin_prefix="ATF", limit=1)
        self.assertTrue(first["has_more"])
        second = self.index.blocks(self.file_id, "addin", query="Application", addin_prefix="ATF", after=first["next_after"], limit=1)
        self.assertEqual([b["start_line"] for b in second["items"]], [5])
        self.assertFalse(second["has_more"])

    def test_prefix_wildcards_are_literal(self):
        self.path.write_text("API_SUCCESS { Starting External Application: ATF_%Plugin, Vendor: ADSK() }\n"
                             "API_SUCCESS { Starting External Application: ATFXOther, Vendor: DEMO() }\n", encoding="utf-8")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        self.assertEqual([b["start_line"] for b in self.index.blocks(self.file_id, addin_prefix="ATF_%")["items"]], [1])

    def test_common_word_search_can_filter_the_all_addins_view(self):
        self.path.write_text("API_SUCCESS { Registered event common }\n" * 520
                             + "API_SUCCESS { Starting External Application: ATF First, Class: ATF.First, Vendor: ADSK(), common }\n"
                             + "API_ERROR { Starting External Application: ATF Second, Class: ATF.Second, Vendor: DEMO(), common }\n", encoding="utf-8")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        for prefix in ("", "ATF"):
            with self.subTest(prefix=prefix):
                result = self.index.blocks(self.file_id, "addin", query="common", addin_prefix=prefix)
                self.assertEqual([b["start_line"] for b in result["items"]], [521, 522])

    def test_collapsed_list_never_reads_or_displays_raw_records(self):
        browser = Browser(self.index, category="addin")
        with patch.object(self.index, "block_lines", side_effect=AssertionError("原文只应在展开后读取")):
            for density in (0, 1, 2):
                browser.density = density
                frame = browser.frame(100, 25, False)
                self.assertIn("ATF Translation Server", frame)
                self.assertIn("3条加载记录", frame)
                self.assertNotIn("Starting External", frame)
                self.assertNotIn("Vendor:", frame)
                self.assertNotIn("Assembly:", frame)
        browser.handle("\r")
        with patch.object(self.index, "block_lines", wraps=self.index.block_lines) as detail:
            frame = browser.frame(160, 25, False)
            detail.assert_called_once()
        self.assertIn("ATFTranslationServer.ATFTranslationServerRegister", frame)
        self.assertIn("Vendor: ADSK()", frame)
        browser.handle("\x1b")
        self.assertFalse(browser.expanded)

    def test_prefix_prompt_and_goto_keep_the_view_consistent(self):
        browser = Browser(self.index)
        browser.handle("i")
        browser.handle("v")
        for key in "ATFTranslationServer\r":
            browser.handle(key)
        self.assertEqual(len(browser.items), 1)
        self.assertEqual(browser.addin_prefix, "ATFTranslationServer")
        browser.handle("g")
        browser.handle("6")
        browser.handle("\r")
        self.assertEqual(browser.block["start_line"], 6)
        self.assertEqual(browser.addin_prefix, "")
        self.assertTrue(browser.expanded)

    def test_list_frames_fit_narrow_and_short_terminals(self):
        browser = Browser(self.index, category="addin")
        for width, height in [(100, 25), (80, 24), (40, 13), (40, 10), (25, 5)]:
            for expanded in (False, True):
                with self.subTest(width=width, height=height, expanded=expanded):
                    browser.expanded = expanded
                    frame = browser.frame(width, height, False)
                    self.assertLessEqual(len(frame.splitlines()), height)
                    self.assertTrue(all(cell_width(line) < width for line in frame.splitlines()))

    def test_cli_summaries_and_json_support_addin_prefix(self):
        for format_args in (["--print"], ["--format", "json"]):
            with self.subTest(format_args=format_args):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = main(["browse", str(self.path), "--database", str(self.database),
                                 "--addin-prefix", "ATFTranslationServer", *format_args])
                self.assertEqual(code, 0)
                if "json" in format_args:
                    file = json.loads(output.getvalue())["files"][0]
                    self.assertEqual(file["addin_count"], 3)
                    self.assertEqual(len(file["items"]), 1)
                    self.assertEqual(file["items"][0]["addin_name"], "ATF Translation Server")
                    self.assertEqual(file["items"][0]["addin_class"], "ATFTranslationServer.ATFTranslationServerRegister")
                else:
                    self.assertIn("ATF Translation Server", output.getvalue())
                    self.assertNotIn("Starting External", output.getvalue())
                    self.assertNotIn("Vendor:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
