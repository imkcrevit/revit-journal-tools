import codecs
import hashlib
import tempfile
import unittest
from pathlib import Path

from revit_journal_tools.reader import JournalReader, JournalReadError


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

    def test_invalid_bytes_after_large_valid_prefix_fail(self):
        self.path.write_bytes(b"normal\n" * 2000 + b"\xff\xfeinvalid\n")
        with JournalReader(self.path) as reader:
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


if __name__ == "__main__":
    unittest.main()
