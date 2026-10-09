import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from revit_journal_tools.blocks import BlockBuilder
from revit_journal_tools.categories import Categorizer
from revit_journal_tools.cli import main
from revit_journal_tools.loading import LoadingEvidence
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


class LoadingTests(unittest.TestCase):
    def test_vendor_variants_and_strict_website_hosts(self):
        cases = [
            ("ADSK()", "autodesk"),
            ("adsk(Autodesk, WWW.AUTODESK.COM)", "autodesk"),
            ("RPCA(Autodesk, https://knowledge.autodesk.com/search?q=revit)", "autodesk"),
            (r"DEMO(https:\\autodesk.com)", "autodesk"),
            ("DEMO(https://plugins.example)", "third_party"),
            ("Synthetic Vendor()", "third_party"),
            ("DEMO(https://autodesk.com.example.org)", "third_party"),
            ("DEMO(https://fake-autodesk.com)", "third_party"),
            ("DEMO(https://example.org/?next=autodesk.com)", "third_party"),
            ("DEMO(https://autodesk.com@example.org)", "third_party"),
            ("DEMO(www.autodesk.com@example.org)", "third_party"),
            ("<None>()", "unknown"),
        ]
        for vendor, origin in cases:
            with self.subTest(vendor=vendor):
                block = grouped(["API_SUCCESS { Starting External Application: Synthetic, "
                                 f"Vendor : {vendor}, Assembly: Autodesk.Synthetic.dll }}"])[0]
                self.assertEqual(block.addin_origin, origin)
                self.assertEqual(block.addin_evidence[0]["line_number"], 1)

    def test_timestamp_and_vendor_on_following_lines_form_one_event(self):
        blocks = grouped(["'E 01-Oct-2026 10:00:00.001; 0:<",
                          "API_SUCCESS { Starting External DB Application: Synthetic,",
                          "'Class: Synthetic.App,", "'Vendor : RPCA(",
                          "'Autodesk, https://www.autodesk.com),", "'Assembly: Synthetic.dll }",
                          "API_SUCCESS { Registered synthetic event, Vendor : DEMO(example.org) }"])
        self.assertEqual([(b.start_line, b.end_line) for b in blocks], [(1, 6), (7, 7)])
        self.assertEqual(blocks[0].addin_origin, "autodesk")
        self.assertEqual(blocks[0].addin_evidence[0]["line_number"], 4)
        self.assertEqual(blocks[1].addin_origin, "")

    def test_vendor_label_and_value_can_be_on_separate_lines(self):
        block = grouped(["API_SUCCESS { Starting External Application: Synthetic, Vendor:",
                         "' ADSK(https://autodesk.com), Assembly: Synthetic.dll }"])[0]
        self.assertEqual(block.addin_origin, "autodesk")

    def test_missing_vendor_never_inherits_previous_event_or_dll_name(self):
        blocks = grouped(["API_SUCCESS { Starting External Application: First, Vendor: ADSK() }",
                          "API_SUCCESS { Starting External Application: Autodesk.Second, Assembly: Autodesk.Second.dll }",
                          "API_SUCCESS { Registered event, Vendor: ADSK() }",
                          "License initialization complete"])
        self.assertEqual([b.addin_origin for b in blocks], ["autodesk", "unknown", "", ""])
        self.assertEqual(blocks[1].addin_evidence[0]["basis"], "vendor_missing")

    def test_error_category_and_severity_survive_source_classification(self):
        block = grouped(["API_ERROR { Starting External Application: Synthetic,",
                         "'Vendor: DEMO(https://plugins.example), Assembly: Synthetic.dll }"])[0]
        self.assertEqual((block.category, block.severity, block.addin_origin), ("api_error", "error", "third_party"))
        self.assertIn("revit.api_error", block.rules)

    def test_unclosed_events_do_not_take_the_next_events_vendor(self):
        blocks = grouped(["API_SUCCESS { Starting External Application: Unknown,",
                          "API_SUCCESS { Starting External Application: Official, Vendor: ADSK() }"])
        self.assertEqual([b.addin_origin for b in blocks], ["unknown", "autodesk"])
        self.assertFalse(blocks[0].complete)
        self.assertTrue(blocks[1].complete)

    def test_conflicting_or_incomplete_vendor_is_unknown(self):
        for lines in (["API_SUCCESS { Starting External Application: Synthetic,",
                       "'Vendor: ADSK(),", "'Vendor: DEMO(plugins.example) }"],
                      ["API_SUCCESS { Starting External Application: Synthetic, Vendor: DEMO("]):
            with self.subTest(lines=lines):
                self.assertEqual(grouped(lines)[0].addin_origin, "unknown")

    def test_inline_timestamp_does_not_duplicate_evidence(self):
        block = grouped(["'E 01-Oct-2026 10:00:00.001; API_SUCCESS { Starting External Application: Synthetic, Vendor: ADSK() }"])[0]
        self.assertEqual(len(block.addin_evidence), 1)

    def test_evidence_and_vendor_buffer_are_bounded(self):
        evidence = LoadingEvidence()
        evidence.feed(1, "Starting External Application: Synthetic, Vendor: DEMO(")
        for number in range(2, 20):
            evidence.feed(number, "x" * 1000)
        self.assertLessEqual(len(evidence.vendor), 4096)
        evidence.feed(20, ")")
        self.assertEqual(evidence.finish()[0], "unknown")
        block = grouped(["API_SUCCESS { Starting External Application: Synthetic,"]
                        + ["Vendor: ADSK(),"] * 20 + ["}"])[0]
        self.assertEqual(block.addin_origin, "autodesk")
        self.assertLessEqual(len(block.addin_evidence), 8)


class LoadingIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "journal.txt"
        self.path.write_text("API_SUCCESS { Starting External Application: First, Vendor: ADSK() }\n"
                             "API_ERROR { Starting External Application: Second,\n"
                             "'Vendor: DEMO(https://plugins.example),\n"
                             "'common detail }\n"
                             "API_SUCCESS { Starting External Application: Third }\n", encoding="utf-8")
        self.database = self.root / "index.sqlite3"
        self.index = ViewerIndex(self.database)
        queued = self.index.register([self.path], "auto")
        self.index.build(queued, "auto")
        self.file_id = queued[0]

    def test_filters_preserve_error_queries_and_total_block_count(self):
        self.assertEqual(self.index.catalog()[0]["block_count"], 3)
        for category, start in [("addin_autodesk", 1), ("addin_third_party", 2), ("addin_unknown", 5), ("api_error", 2)]:
            with self.subTest(category=category):
                blocks = self.index.blocks(self.file_id, category)["items"]
                self.assertEqual([b["start_line"] for b in blocks], [start])
                self.assertIsInstance(blocks[0]["addin_evidence"], list)
        self.assertEqual(self.index.catalog()[0]["block_counts"]["addin_third_party"], 1)

    def test_filtered_search_and_pagination_use_whole_events(self):
        result = self.index.blocks(self.file_id, "addin_third_party", query="plugins")
        self.assertEqual([(b["start_line"], b["end_line"]) for b in result["items"]], [(2, 4)])
        self.assertEqual(self.index.blocks(self.file_id, "addin_autodesk", query="plugins")["items"], [])
        self.assertEqual(self.index.blocks(self.file_id, "addin_third_party", after=result["next_after"])["items"], [])

    def test_common_word_search_uses_origin_in_optimized_query(self):
        self.path.write_text("API_SUCCESS { Registered event common }\n" * 520
                             + "API_SUCCESS { Starting External Application: First, Vendor: ADSK(), common }\n"
                             + "API_ERROR { Starting External Application: Second, Vendor: DEMO(), common }\n"
                             + "API_SUCCESS { Starting External Application: Third, Vendor: DEMO(), common }\n", encoding="utf-8")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        first = self.index.blocks(self.file_id, "addin_third_party", query="common", limit=1)
        self.assertEqual([b["start_line"] for b in first["items"]], [522])
        self.assertTrue(first["has_more"])
        second = self.index.blocks(self.file_id, "addin_third_party", query="common", after=first["next_after"], limit=1)
        self.assertEqual([b["start_line"] for b in second["items"]], [523])
        self.assertFalse(second["has_more"])

    def test_vendor_after_index_batch_is_applied_to_whole_event(self):
        self.path.write_text("API_SUCCESS { Starting External Application: Synthetic,\n"
                             + "'synthetic detail\n" * 5100 + "'Vendor: ADSK(), Assembly: Synthetic.dll }\n", encoding="utf-8")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        blocks = self.index.blocks(self.file_id, "addin_autodesk")["items"]
        self.assertEqual([(b["start_line"], b["end_line"]) for b in blocks], [(1, 5102)])
        self.assertEqual(blocks[0]["addin_evidence"][0]["line_number"], 5102)

    def test_legacy_database_rebuilds_with_new_columns_and_version(self):
        with sqlite3.connect(self.database) as connection:
            connection.executescript("""
                CREATE TABLE old_blocks (
                    file_id INTEGER NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                    category TEXT NOT NULL, severity TEXT NOT NULL, rules TEXT NOT NULL,
                    title TEXT NOT NULL, title_line INTEGER NOT NULL, boundary TEXT NOT NULL,
                    complete INTEGER NOT NULL, PRIMARY KEY(file_id,start_line)
                );
                INSERT INTO old_blocks SELECT file_id,start_line,end_line,category,severity,rules,
                    title,title_line,boundary,complete FROM blocks;
                DROP TABLE blocks;
                ALTER TABLE old_blocks RENAME TO blocks;
            """)
            connection.execute("UPDATE files SET signature='viewer-2:blocks-1:old'")
        migrated = ViewerIndex(self.database)
        queued = migrated.register([self.path], "auto")
        self.assertEqual(queued, [self.file_id])
        migrated.build(queued, "auto")
        self.assertEqual(migrated.blocks(self.file_id, "addin_autodesk")["items"][0]["start_line"], 1)
        self.assertEqual(migrated.register([self.path], "auto"), [])

    def test_cli_json_and_keys_expose_new_filters(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["browse", str(self.path), "--database", str(self.database), "--category", "addin_third_party", "--format", "json"])
        self.assertEqual(code, 0)
        items = json.loads(output.getvalue())["files"][0]["items"]
        self.assertEqual((items[0]["category"], items[0]["addin_origin"]), ("api_error", "third_party"))
        browser = Browser(self.index)
        for key, origin in [("a", "autodesk"), ("t", "third_party"), ("u", "unknown")]:
            browser.handle(key)
            self.assertEqual(browser.block["addin_origin"], origin)
            frame = browser.frame(100, 25, False)
            self.assertIn("来源待定加载", frame)
            self.assertLessEqual(len(frame.splitlines()), 25)
            self.assertTrue(all(cell_width(line) < 100 for line in frame.splitlines()))
        browser.handle("4")
        self.assertEqual(browser.block["category"], "api_error")


if __name__ == "__main__":
    unittest.main()
