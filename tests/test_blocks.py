import unittest

from revit_journal_tools.blocks import BlockBuilder
from revit_journal_tools.categories import Categorizer
from revit_journal_tools.reader import JournalLine
from revit_journal_tools.rules import load_rules


def grouped(lines):
    builder, categorizer = BlockBuilder(), Categorizer(load_rules())
    result = []
    for number, text in enumerate(lines, 1):
        result.extend(builder.feed(JournalLine(number, text), categorizer.classify(text)))
    result.extend(builder.finish())
    return result


class BlockTests(unittest.TestCase):
    def test_paragraphs_cover_every_original_line_once(self):
        lines = ["", "", "first", "continued", "", "second", "continued", ""]
        blocks = grouped(lines)
        self.assertEqual([(b.start_line, b.end_line) for b in blocks], [(1, 2), (3, 5), (6, 8)])
        self.assertEqual([n for b in blocks for n in range(b.start_line, b.end_line + 1)], list(range(1, 9)))

    def test_settings_section_and_resource_sample_are_whole(self):
        blocks = grouped(["' 0:< SystemSettings:", "' 0:<   12=cores", "' 0:<   true=installed",
                          "' 0:< Delta VM: Avail 100", "' 0:< GUI Resource Usage GDI: 5",
                          "' 0:< License mode: User", "' 0:< Status: synthetic", "' 0:< Type: USER"])
        self.assertEqual([(b.start_line, b.end_line, b.category) for b in blocks],
                         [(1, 3, "startup"), (4, 5, "startup"), (6, 8, "startup")])

    def test_timestamp_command_and_all_continuations_form_one_operation(self):
        blocks = grouped(["'H 01-Oct-2026 10:00:00.001; 0:<", "Jrn.Command _",
                          ' "Ribbon", _', ' "Synthetic command"',
                          "'H 01-Oct-2026 10:00:01.001; 0:<", 'Jrn.Data "Next"'])
        self.assertEqual([(b.start_line, b.end_line, b.category) for b in blocks], [(1, 4, "operation"), (5, 6, "operation")])
        self.assertEqual(blocks[0].title, 'Jrn.Command "Ribbon", "Synthetic command"')
        self.assertEqual(blocks[0].title_line, 2)

    def test_api_error_is_one_block_and_next_error_is_separate(self):
        blocks = grouped(["'E 01-Oct-2026 10:00:00.001; 0:<", "API_ERROR { module conflict",
                          "'first module", "'second module }", "API_ERROR { separate failure }"])
        self.assertEqual([(b.start_line, b.end_line, b.category) for b in blocks], [(1, 4, "api_error"), (5, 5, "api_error")])
        self.assertIn("revit.api_error", blocks[0].rules)
        self.assertEqual(blocks[0].severity, "error")

    def test_multi_line_success_and_nested_braces(self):
        blocks = grouped(["API_SUCCESS { Register {synthetic-id}", "'command name", "'event }", "normal"])
        self.assertEqual([(b.start_line, b.end_line, b.category) for b in blocks], [(1, 3, "api"), (4, 4, "startup")])

    def test_error_message_exception_and_stack_remain_together(self):
        blocks = grouped(["DBG_ERROR: first operation failed", "System.InvalidOperationException: synthetic",
                          "  at Synthetic.Run()", "  at Synthetic.Main()", "DBG_ERROR: independent failure", "normal message"])
        self.assertEqual([(b.start_line, b.end_line, b.category) for b in blocks],
                         [(1, 4, "error"), (5, 5, "error"), (6, 6, "startup")])
        self.assertIn("generic.exception", blocks[0].rules)

    def test_independent_exceptions_split_but_inner_exception_is_a_continuation(self):
        blocks = grouped(["System.Exception: first", "  ---> InnerException: cause", "  at Synthetic.Run()",
                          "System.Exception: second", "  at Synthetic.Other()"])
        self.assertEqual([(b.start_line, b.end_line) for b in blocks], [(1, 3), (4, 5)])

    def test_unclosed_api_recovers_at_timestamp_and_marks_incomplete(self):
        blocks = grouped(["API_ERROR { unfinished", "'related detail", "'C 01-Oct-2026 10:00:01.001; InitApplication", "' Build: synthetic"])
        self.assertEqual([(b.start_line, b.end_line, b.category) for b in blocks], [(1, 2, "api_error"), (3, 4, "startup")])
        self.assertFalse(blocks[0].complete)
        self.assertTrue(blocks[1].complete)

    def test_unclosed_api_recovers_at_independent_error(self):
        blocks = grouped(["API_ERROR { unfinished", "DBG_ERROR: separate"])
        self.assertEqual([b.category for b in blocks], ["api_error", "error"])
        self.assertFalse(blocks[0].complete)

    def test_long_api_block_is_not_cut_at_arbitrary_line_limit(self):
        blocks = grouped(["API_ERROR { synthetic"] + ["'related module"] * 600 + ["'}"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].end_line, 602)
        self.assertTrue(blocks[0].complete)
        self.assertLessEqual(len(blocks[0].title), 700)

    def test_open_api_and_operation_at_eof_are_marked_incomplete(self):
        for lines in (["API_ERROR { synthetic"], ["Jrn.Command _", ' "Ribbon", _']):
            with self.subTest(lines=lines):
                self.assertFalse(grouped(lines)[0].complete)

    def test_timestamp_inline_api_keeps_its_continuation(self):
        blocks = grouped(["'C 01-Oct-2026 10:00:00.001; API_ERROR { synthetic", "'details }"])
        self.assertEqual([(b.start_line, b.end_line, b.category) for b in blocks], [(1, 2, "api_error")])

    def test_empty_file_has_no_blocks(self):
        self.assertEqual(grouped([]), [])

    def test_native_exception_timestamp_code_and_fields_stay_together(self):
        blocks = grouped(["' 0:< Exception occurred", "'C 01-Oct-2026 10:00:00.001; 0:< ExceptionCode=0xe0434352",
                          "' 0:< ExceptionAddress=00000001", "'C 01-Oct-2026 10:00:01.001; Next event"])
        self.assertEqual([(b.start_line, b.end_line) for b in blocks], [(1, 3), (4, 4)])
        self.assertEqual(blocks[0].category, "error")
        self.assertEqual(len(grouped(["ExceptionCode=0xe0434352", "ExceptionCode=0xe06d7363"])), 2)
        self.assertEqual(grouped(["ExceptionCode=0xe0434352", "ExceptionFlags=0x81"])[0].end_line, 2)


if __name__ == "__main__":
    unittest.main()
