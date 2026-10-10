import codecs
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from revit_journal_tools import reader as reader_module
from revit_journal_tools.reader import JournalReader, JournalReadError, resolve_encoding


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "journal.txt"

    def test_bom_encodings_preserve_lines_and_raw_digest(self):
        source = "普通操作\r\nDBG_ERROR: 测试错误\r\n最后一行"
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                data = source.encode(encoding)
                self.path.write_bytes(data)
                with JournalReader(self.path) as reader:
                    lines = list(reader)
                    self.assertEqual([line.text for line in lines], source.splitlines())
                    self.assertEqual([line.number for line in lines], [1, 2, 3])
                    self.assertEqual(reader.sha256, hashlib.sha256(data).hexdigest())
                    self.assertEqual(reader.byte_count, len(data))
                    self.assertTrue(reader.complete)

    def test_big_endian_bom(self):
        self.path.write_bytes(codecs.BOM_UTF16_BE + "ERROR: 中文\n".encode("utf-16-be"))
        with JournalReader(self.path) as reader:
            self.assertEqual(list(reader)[0].text, "ERROR: 中文")

    def test_explicit_gb18030(self):
        self.path.write_bytes("错误: 模型读取失败\n".encode("gb18030"))
        with JournalReader(self.path, encoding="gb18030") as reader:
            self.assertEqual(list(reader)[0].text, "错误: 模型读取失败")

    def test_cp1252_nonbreaking_space_degree_and_german_text(self):
        source = "Name(Cloud\u00a0Models)\r\n90.00°\r\nDiese Lizenz läuft\r\n"
        self.path.write_bytes(source.encode("cp1252"))
        with JournalReader(self.path, encoding="cp1252") as reader:
            self.assertEqual([line.text for line in reader], source.splitlines())

    def test_invalid_bytes_after_large_valid_prefix_fail_with_explicit_encoding(self):
        self.path.write_bytes(b"normal\n" * 2000 + b"\xff\xfeinvalid\n")
        with JournalReader(self.path, encoding="utf-8") as reader:
            with self.assertRaisesRegex(JournalReadError, "严格解码"):
                list(reader)
            self.assertFalse(reader.complete)
            with self.assertRaises(RuntimeError):
                _ = reader.sha256

    def test_empty_file_and_line_endings(self):
        for source, expected in ((b"", []), (b"\n\r\n\r", ["", "", ""]), (b"a\nb", ["a", "b"])):
            with self.subTest(source=source):
                self.path.write_bytes(source)
                with JournalReader(self.path) as reader:
                    self.assertEqual([line.text for line in reader], expected)
                    self.assertEqual(reader.sha256, hashlib.sha256(source).hexdigest())

    def test_standalone_carriage_returns_count_as_logical_newlines(self):
        self.path.write_bytes(b"field first\rfield second\r\nDBG_ERROR: synthetic\n")
        with JournalReader(self.path) as reader:
            lines = list(reader)
            self.assertEqual(lines[-1].number, 3)
            self.assertEqual(lines[-1].text, "DBG_ERROR: synthetic")

    def test_line_limit_at_boundary_and_above(self):
        for ending in ("", "\n", "\r\n"):
            with self.subTest(ending=ending):
                self.path.write_bytes(("x" * 20 + ending).encode())
                with JournalReader(self.path, max_line_chars=20) as reader:
                    self.assertEqual(list(reader)[0].text, "x" * 20)
                self.path.write_bytes(("x" * 21 + ending).encode())
                with JournalReader(self.path, max_line_chars=20) as reader:
                    with self.assertRaisesRegex(JournalReadError, "超过20字符"):
                        list(reader)

    def test_binary_and_missing_bom_are_explicit_errors(self):
        self.path.write_bytes("plain text".encode("utf-16-le"))
        with JournalReader(self.path) as reader:
            with self.assertRaisesRegex(JournalReadError, "NUL"):
                list(reader)
        with JournalReader(self.path, encoding="utf-16-le") as reader:
            self.assertEqual(list(reader)[0].text, "plain text")

    def test_source_modified_during_scan_is_rejected(self):
        self.path.write_text("first\nsecond\n", encoding="utf-8")
        with JournalReader(self.path) as reader:
            iterator = iter(reader)
            next(iterator)
            with self.path.open("ab") as stream:
                stream.write(b"third\n")
            with self.assertRaisesRegex(JournalReadError, "发生变化"):
                list(iterator)

    def test_digest_unavailable_before_eof(self):
        self.path.write_text("one\ntwo\n", encoding="utf-8")
        with JournalReader(self.path) as reader:
            next(iter(reader))
            with self.assertRaises(RuntimeError):
                _ = reader.sha256


@patch.object(reader_module, "system_fallback_encoding", return_value="gb18030")
class AutoEncodingTests(unittest.TestCase):
    """Per-file decisions on a Chinese Windows code page (cp936 -> gb18030)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "journal.txt"

    def read(self, data, encoding="auto"):
        self.path.write_bytes(data)
        with JournalReader(self.path, encoding) as reader:
            lines = list(reader)
        return reader, lines

    def test_utf8_file_is_not_a_fallback(self, _):
        reader, lines = self.read("错误: 中文\n".encode("utf-8"))
        self.assertEqual((reader.encoding, reader.encoding_fallback, reader.degraded_line_count), ("utf-8", False, 0))
        self.assertEqual(lines[0].text, "错误: 中文")

    def test_gbk_journal_falls_back_to_gb18030_strictly(self, _):
        data = "' 0:< 错误: 不满足约束\r\nJrn.Command \"Ribbon\"\r\n".encode("gb18030")
        reader, lines = self.read(data)
        self.assertEqual((reader.encoding, reader.encoding_fallback, reader.degraded_line_count), ("gb18030", True, 0))
        self.assertEqual([line.text for line in lines], ["' 0:< 错误: 不满足约束", "Jrn.Command \"Ribbon\""])
        self.assertFalse(any(line.degraded for line in lines))
        self.assertEqual(reader.sha256, hashlib.sha256(data).hexdigest())

    def test_byte_valid_only_in_gb18030_superset(self, _):
        # Observed in real journals: cp936 rejects one byte sequence that gb18030 accepts.
        data = "Jrn.SessionOptions ß 错误\n".encode("gb18030")
        with self.assertRaises(UnicodeDecodeError):
            data.decode("cp936")
        reader, lines = self.read(data)
        self.assertEqual((reader.encoding, reader.degraded_line_count), ("gb18030", 0))
        self.assertEqual(lines[0].text, "Jrn.SessionOptions ß 错误")

    def test_degraded_lines_are_marked_counted_and_keep_numbering(self, _):
        data = "正常\r\n".encode("gb18030") + b"bad\xff\rnext\r\nlead\x81\nend"
        reader, lines = self.read(data)
        self.assertEqual(reader.encoding, "gb18030")
        self.assertTrue(reader.encoding_fallback)
        self.assertEqual([line.text for line in lines], ["正常", "bad\\xff", "next", "lead\\x81", "end"])
        self.assertEqual([line.degraded for line in lines], [False, True, False, True, False])
        self.assertEqual((reader.degraded_line_count, reader.degraded_lines), (2, [2, 4]))
        self.assertEqual(reader.sha256, hashlib.sha256(data).hexdigest())
        summary = reader.decoding_summary()
        self.assertEqual(summary["decode_degraded_line_count"], 2)
        self.assertEqual(summary["encoding_requested"], "auto")

    def test_degraded_sample_is_bounded_but_count_is_complete(self, _):
        reader, lines = self.read(b"x\xff\n" * 50)
        self.assertEqual(reader.degraded_line_count, 50)
        self.assertEqual(len(reader.degraded_lines), 20)

    def test_bom_with_invalid_bytes_degrades_with_bom_encoding(self, _):
        reader, lines = self.read(codecs.BOM_UTF8 + b"ok\nbad\xff\n")
        self.assertEqual((reader.encoding, reader.degraded_lines), ("utf-8-sig", [2]))
        self.assertEqual(lines[1].text, "bad\\xff")

    def test_degraded_mode_still_enforces_line_limit(self, _):
        self.path.write_bytes(b"\xff" * 30 + b"\n")
        with JournalReader(self.path, max_line_chars=20) as reader:
            with self.assertRaisesRegex(JournalReadError, "超过20字符"):
                list(reader)

    def test_candidate_chain_decides_each_file(self, _):
        cp1252 = "Name(Cloud Models)\r\n90.00°\r\n".encode("cp1252")
        reader, lines = self.read(cp1252, "utf-8,gb18030,cp1252")
        self.assertEqual((reader.encoding, reader.encoding_fallback), ("cp1252", True))
        self.assertEqual(lines[1].text, "90.00°")
        reader, lines = self.read("错误\n".encode("gb18030"), "utf-8,gb18030,cp1252")
        self.assertEqual(reader.encoding, "gb18030")

    def test_policy_resolution(self, _):
        self.assertEqual(resolve_encoding("auto").candidates, ("utf-8", "gb18030"))
        self.assertEqual(resolve_encoding("auto,cp1252").candidates, ("utf-8", "gb18030", "cp1252"))
        self.assertFalse(resolve_encoding("cp936").detect)
        self.assertEqual(resolve_encoding("cp936").candidates, ("gbk",))
        with self.assertRaises(LookupError):
            resolve_encoding("no-such-codec")
        with self.assertRaises(LookupError):
            resolve_encoding(" , ")


class SystemFallbackTests(unittest.TestCase):
    def test_ansi_code_page_supersets(self):
        for system, expected in (("cp936", "gb18030"), ("GBK", "gb18030"), ("cp1252", "cp1252"),
                                 ("UTF-8", None), ("ascii", None)):
            with self.subTest(system=system), patch("locale.getencoding", create=True, return_value=system):
                self.assertEqual(reader_module.system_fallback_encoding(), expected)


if __name__ == "__main__":
    unittest.main()
