import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from revit_journal_tools.cli import main
from revit_journal_tools.terminal import Browser, cell_width, crop, safe_text
from revit_journal_tools.viewer_index import NO_SEARCH_TOKENS, ViewerIndex


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "synthetic.txt"
        self.path.write_text("System Information:\nOSLanguage : 1033\nCPU : synthetic\n\n"
                             "API_ERROR { conflict\n'private_module_keyword\n'last_detail }\n"
                             "DBG_ERROR: separate\n  at Synthetic.Run()\n"
                             "Jrn.Command _\n \"Ribbon\", \"Next\"\n", encoding="utf-8")
        self.db = self.root / "terminal.sqlite3"
        self.index = ViewerIndex(self.db)
        queued = self.index.register([self.path], "auto")
        self.index.build(queued, "auto")
        self.file_id = queued[0]

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([str(arg) for arg in args])
        return code, output.getvalue(), errors.getvalue()

    @staticmethod
    def type(browser, text, clear=0):
        for key in ["\b"] * clear + list(text) + ["\r"]:
            browser.handle(key)

    def test_paragraph_index_has_complete_ranges_and_counts(self):
        blocks = self.index.blocks(self.file_id)["items"]
        self.assertEqual([(b["start_line"], b["end_line"]) for b in blocks], [(1, 4), (5, 7), (8, 9), (10, 11)])
        self.assertEqual(self.index.catalog()[0]["block_count"], 4)
        self.assertEqual(self.index.catalog()[0]["block_counts"]["api_error"], 1)
        self.assertEqual(self.index.catalog()[0]["counts"]["api_error"], 3)

    def test_initial_selection_skips_empty_dump_text(self):
        empty = self.root / "dump.0001.txt"
        empty.write_text("", encoding="utf-8")
        self.index.build(self.index.register([empty, self.path], "auto"), "auto")
        browser = Browser(self.index)
        self.assertEqual(browser.file["name"], self.path.name)

    def test_search_inside_continuation_selects_entire_block_once(self):
        result = self.index.blocks(self.file_id, query="private_module_key")
        self.assertEqual([(b["start_line"], b["end_line"]) for b in result["items"]], [(5, 7)])
        self.assertEqual(self.index.blocks(self.file_id, "system", query="private_module_key")["items"], [])
        self.assertEqual(len(self.index.blocks(self.file_id, query="synthetic")["items"]), 2)

    def test_block_pagination_and_detail_pagination_keep_every_line(self):
        numbers, after = [], 0
        while True:
            page = self.index.blocks(self.file_id, after=after, limit=1)
            numbers.extend(b["start_line"] for b in page["items"])
            if not page["has_more"]:
                break
            after = page["next_after"]
        self.assertEqual(numbers, [1, 5, 8, 10])
        detail = self.index.block_lines(self.file_id, 5, limit=2)
        self.assertEqual([line["line_number"] for line in detail["items"]], [5, 6])
        self.assertTrue(detail["has_more"])
        self.assertEqual(self.index.block_lines(self.file_id, 5, offset=2)["items"][0]["line_number"], 7)
        self.assertEqual(self.index.block_at(self.file_id, 6)["start_line"], 5)

    def test_changed_source_rebuild_removes_old_blocks(self):
        self.path.write_text("normal\n", encoding="utf-8")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        self.assertEqual(self.index.catalog()[0]["block_count"], 1)
        self.assertEqual(self.index.blocks(self.file_id, query="private_module")["items"], [])
        self.assertEqual(self.index.block_at(self.file_id, 1)["end_line"], 1)

    def test_cli_json_filter_and_resume(self):
        code, out, err = self.cli("browse", self.path, "--database", self.db, "--format", "json", "--page-size", 1)
        self.assertEqual(code, 0, err)
        file = json.loads(out)["files"][0]
        self.assertEqual(file["items"][0]["end_line"], 4)
        self.assertTrue(file["has_more"])
        self.assertEqual(len(file["sha256"]), 64)
        code, out, err = self.cli("browse", self.path, "--database", self.db, "--format", "json", "--after", 1, "--category", "api_error")
        self.assertEqual([b["start_line"] for b in json.loads(out)["files"][0]["items"]], [5])

    def test_noninteractive_color_control_and_no_escape_in_plain_output(self):
        args = ("browse", self.path, "--database", self.db, "--print")
        code, plain, err = self.cli(*args)
        self.assertEqual(code, 0, err)
        self.assertNotIn("\x1b", plain)
        self.assertIn("L5–7", plain)
        self.assertIn("\x1b[35m", self.cli(*args, "--color", "always")[1])

    def test_browser_expand_filter_goto_search_and_file_switch(self):
        browser = Browser(self.index)
        browser.handle("4")
        self.assertEqual(len(browser.items), 1)
        browser.handle("\r")
        self.assertTrue(browser.expanded)
        self.assertIn("private_module_keyword", browser.frame(100, 25, False))
        browser.handle("g")
        browser.handle("9")
        browser.handle("\r")
        self.assertEqual(browser.block["start_line"], 8)
        self.assertEqual(browser.detail_offset, 1)
        browser.handle("/")
        for key in "private_module":
            browser.handle(key)
        browser.handle("\r")
        self.assertEqual(browser.block["start_line"], 5)
        browser.handle("f")
        self.assertEqual(browser.mode, "files")
        browser.handle("\r")
        self.assertEqual(browser.mode, "blocks")
        self.assertFalse(browser.handle("q"))

    def test_punctuation_only_search_keeps_previous_query_and_list(self):
        browser = Browser(self.index)
        browser.handle("/")
        self.type(browser, "private")
        items = browser.items
        self.assertEqual([b["start_line"] for b in items], [5])
        # The count is of matching lines, not blocks.
        self.assertIn("搜索: private 命中1行", browser.frame(100, 25, False))
        browser.handle("/")
        self.type(browser, "}", clear=len("private"))
        self.assertEqual((browser.query, browser.items, browser.message), ("private", items, NO_SEARCH_TOKENS))
        self.assertIn(NO_SEARCH_TOKENS, browser.frame(100, 25, False))
        # A query given at start-up that cannot be searched is not applied either.
        browser = Browser(self.index, query="}")
        self.assertEqual((browser.query, browser.message), ("", NO_SEARCH_TOKENS))
        browser.refresh()
        self.assertEqual(len(browser.items), 4)

    def test_hit_keys_move_between_hits_inside_a_block(self):
        self.path.write_text("API_ERROR { first marker\n" + "'filler\n" * 5 + "'second marker\n'filler\n"
                             "'third marker }\n", encoding="utf-8")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        browser = Browser(self.index)
        browser.handle("h")
        self.assertIn("先按/搜索", browser.message)
        browser.handle("/")
        self.type(browser, "second")
        self.assertEqual(browser.block["hit_line"], 7)
        browser.handle("\r")  # Opens at the first hit, three lines of context above it.
        self.assertEqual((browser.expanded, browser.detail_offset), (True, 3))
        browser.handle("/")
        self.type(browser, "marker", clear=len("second"))
        browser.handle("\r")
        self.assertEqual(browser.detail_offset, 0)
        offsets = []
        for key in "hhhH":
            browser.handle(key)
            offsets.append(browser.detail_offset)
        self.assertEqual(offsets, [3, 5, 5, 3])  # Lines 7, 9, (no later hit), back to 7.
        frame = browser.frame(100, 25, False)
        self.assertIn("       7> 'second marker", frame)
        self.assertIn("       9* 'third marker }", frame)
        self.assertIn("       8  'filler", frame)

    def test_workspace_search_opens_a_hit_in_another_file(self):
        second = self.root / "second.txt"
        second.write_text("normal start\n\nJrn.Command _\n \"Ribbon\", \"Second\"\n", encoding="utf-8")
        self.index.build(self.index.register([self.path, second], "auto"), "auto")
        browser = Browser(self.index)
        browser.handle("?")
        self.type(browser, "}")
        self.assertEqual((browser.view, browser.message), ("blocks", NO_SEARCH_TOKENS))
        browser.handle("?")
        self.type(browser, "Ribbon", clear=1)
        self.assertEqual(browser.view, "workspace")
        frame = browser.frame(100, 25, False)
        self.assertIn("全部文件搜索: Ribbon", frame)
        self.assertIn("全部文件命中2行 / 2个文件", frame)
        self.assertIn('  L11   "Ribbon", "Next"', frame)
        for width, height in ((50, 14), (25, 5)):
            frame = browser.frame(width, height, False)
            self.assertLessEqual(len(frame.splitlines()), height)
            self.assertTrue(all(cell_width(line) < width for line in frame.splitlines()))
        browser.handle("\x1b")
        self.assertEqual(browser.view, "blocks")
        browser.handle("?")
        browser.handle("\r")
        current = browser.file["id"]
        hits = browser.workspace_hits()
        target = next(number for number, (file_id, _) in enumerate(hits) if file_id != current)
        for _ in range(target):
            browser.handle("DOWN")
        browser.handle("n")  # One result page: paging keys do nothing here.
        self.assertEqual((browser.view, browser.workspace_cursor), ("workspace", target))
        file_id, line = hits[target]
        browser.handle("\r")
        self.assertEqual((browser.file["id"], browser.view, browser.query), (file_id, "blocks", "Ribbon"))
        self.assertTrue(browser.expanded)
        self.assertLessEqual(browser.block["start_line"], line)
        self.assertLessEqual(line, browser.block["end_line"])
        self.assertEqual(browser.detail_offset, line - browser.block["start_line"])
        self.assertIn(f"{line:>8}> ", browser.frame(100, 25, False))

    def test_frame_fits_resized_terminal_and_cjk_width(self):
        browser = Browser(self.index)
        for width, height in ((100, 30), (50, 14), (25, 5)):
            for density in (0, 1, 2):
                browser.density = density
                frame = browser.frame(width, height, False)
                self.assertLessEqual(len(frame.splitlines()), height)
                self.assertTrue(all(cell_width(line) < width for line in frame.splitlines()))
        self.assertEqual(crop("中文abc", 5), "中文a")

    def test_untrusted_terminal_sequences_are_visible_not_executed(self):
        unsafe = "message\x1b]52;c;clipboard\x07\r\u202e"
        shown = safe_text(unsafe)
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\r", shown)
        self.assertIn("\\u001b", shown)
        self.assertIn("\\u202e", shown)

    def test_database_and_sidecars_cannot_overwrite_source_aliases(self):
        original = self.path.read_bytes()
        code, _, err = self.cli("browse", self.path, "--database", self.path, "--print")
        self.assertEqual(code, 2)
        self.assertIn("不能覆盖", err)
        sidecar = Path(str(self.db) + "-wal")
        try:
            os.link(self.path, sidecar)
        except OSError:
            return
        code, _, err = self.cli("browse", self.path, "--database", self.db, "--print")
        self.assertEqual(code, 2)
        self.assertEqual(self.path.read_bytes(), original)

    def test_decode_failure_is_not_shown_as_complete(self):
        self.path.write_bytes(b"API_ERROR {\n\xff")
        code, out, err = self.cli("browse", self.path, "--database", self.db, "--format", "json", "--encoding", "utf-8")
        self.assertEqual(code, 2, err)
        file = json.loads(out)["files"][0]
        self.assertEqual(file["status"], "error")
        self.assertNotIn("items", file)

    def test_degraded_lines_are_visible_in_print_json_and_expanded_view(self):
        # 0x81 is invalid gb18030 before ASCII and undefined in cp1252: no candidate decodes it.
        self.path.write_bytes(b"API_ERROR {\n'bad \x81 byte }\n")
        with patch("revit_journal_tools.reader.system_fallback_encoding", return_value="gb18030"):
            code, out, err = self.cli("browse", self.path, "--database", self.db, "--format", "json")
            self.assertEqual(code, 0, err)
            file = json.loads(out)["files"][0]
            self.assertEqual((file["status"], file["encoding"]), ("ready", "gb18030"))
            self.assertEqual((file["decode_degraded_line_count"], file["decode_degraded_lines"]), (1, [2]))
            code, printed, err = self.cli("browse", self.path, "--database", self.db, "--print")
            self.assertIn("退化解码1行", printed)
            browser = Browser(self.index)
            browser.handle("\r")
            frame = browser.frame(160, 25, False)
        self.assertIn("'bad \\x81 byte }", frame)
        self.assertIn("含无法解码字节", frame)
        self.assertEqual([item["degraded"] for item in self.index.block_lines(self.file_id, 1)["items"]], [0, 1])

    def test_large_paragraph_crosses_index_batches_without_splitting(self):
        self.path.write_text("API_ERROR { synthetic\n" + "'repeat detail\n" * 11000 + "'}\n", encoding="utf-8")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        blocks = self.index.blocks(self.file_id)["items"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["end_line"], 11002)
        self.assertEqual(len(self.index.block_lines(self.file_id, 1, offset=10990)["items"]), 12)

    def test_common_search_word_skips_duplicate_hits_and_resumes_at_next_block(self):
        self.path.write_text("common detail\n" * 1100 + "\nDBG_ERROR: common error\n", encoding="utf-8")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        first = self.index.blocks(self.file_id, query="common", limit=1)
        self.assertEqual([b["start_line"] for b in first["items"]], [1])
        self.assertTrue(first["has_more"])
        second = self.index.blocks(self.file_id, query="common", after=first["next_after"], limit=1)
        self.assertEqual([b["start_line"] for b in second["items"]], [1102])
        self.assertFalse(second["has_more"])
        errors = self.index.blocks(self.file_id, "error", query="common")
        self.assertEqual([b["start_line"] for b in errors["items"]], [1102])


if __name__ == "__main__":
    unittest.main()
