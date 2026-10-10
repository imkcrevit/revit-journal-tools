import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from revit_journal_tools.cli import main
from revit_journal_tools.sessions import describe, plan_inputs
from revit_journal_tools.terminal import Browser, cell_width
from revit_journal_tools.viewer_index import ViewerIndex


class NamingTests(unittest.TestCase):
    def test_observed_revit_names_map_to_sessions_and_roles(self):
        folder = Path("C:/Users/x/AppData/Local/Autodesk/Revit/Autodesk Revit 2024/Journals")
        cases = {
            "journal.0011.txt": ("journal.0011", "main"),
            "journal.0011.worker1.log": ("journal.0011", "worker"),
            "journal.0011.0001.dmp": ("journal.0011", "dump"),
            "journal.0011.txt.copy.txt": ("journal.0011", "copy"),
            "JOURNAL.0011.TXT.ABBREV": ("journal.0011", "abbrev"),
            "dump.0001.txt": ("dump.0001.txt", "unrecognized"),
            "notes.txt": ("notes.txt", "unrecognized"),
        }
        for name, (label, role) in cases.items():
            with self.subTest(name=name):
                result = describe(folder / name)
                self.assertEqual((result.label, result.role), (label, role))
                self.assertEqual(result.directory, "Autodesk Revit 2024")
        self.assertEqual(describe(folder / "journal.0011.txt").key, describe(folder / "journal.0011.worker1.log").key)
        self.assertNotEqual(describe(folder / "journal.0011.txt").key, describe(folder / "journal.0012.txt").key)
        other = Path("C:/elsewhere/Journals/journal.0011.txt")
        self.assertNotEqual(describe(other).key, describe(folder / "journal.0011.txt").key)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Autodesk Revit 2024" / "Journals"
        self.root.mkdir(parents=True)
        files = {
            "journal.0011.txt": b"' Build: main 11\nDBG_WARN: x\n",
            "journal.0011.txt.copy.txt": b"' Build: main 11\nDBG_WARN: x\n",
            "journal.0011.worker1.log": b"' worker\n",
            "journal.0011.0001.dmp": b"MDMP\x00\x01",
            "journal.0012.txt": b"' Build: main 12\n",
            "journal.0012.txt.copy.txt": b"' Build: main 12 edited\n",
            "dump.0001.txt": b"",
            "notes.txt": b"' unrelated\n",
        }
        # Session 0012 is the newest, 0011 older (its dump written last), then the rest.
        times = {"journal.0011": 100, "journal.0011.0001.dmp": 110, "journal.0012": 200, "dump.0001.txt": 50,
                 "notes.txt": 10}
        base = 1_790_000_000
        for name, data in files.items():
            path = self.root / name
            path.write_bytes(data)
            offset = times.get(name) or times[name[:12]]
            os.utime(path, (base + offset, base + offset))

    def test_copies_are_deduplicated_by_bytes_and_sidecars_are_classified(self):
        planned = {item.path.name: item for item in plan_inputs(list(self.root.iterdir()))}
        self.assertEqual(planned["journal.0011.txt"].status, "index")
        copy = planned["journal.0011.txt.copy.txt"]
        self.assertEqual((copy.status, copy.duplicate_of.name), ("duplicate", "journal.0011.txt"))
        self.assertEqual(len(copy.sha256), 64)
        # Same naming, different bytes: still indexed on its own.
        self.assertEqual(planned["journal.0012.txt.copy.txt"].status, "index")
        self.assertEqual(planned["journal.0011.0001.dmp"].status, "binary")
        self.assertEqual(planned["dump.0001.txt"].status, "empty")
        self.assertEqual(planned["notes.txt"].status, "index")

    def test_order_is_newest_session_first_with_main_journal_first(self):
        names = [item.path.name for item in plan_inputs(list(self.root.iterdir()))]
        self.assertEqual(names, ["journal.0012.txt", "journal.0012.txt.copy.txt",
                                 "journal.0011.txt", "journal.0011.worker1.log", "journal.0011.txt.copy.txt",
                                 "journal.0011.0001.dmp", "dump.0001.txt", "notes.txt"])

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([str(arg) for arg in args])
        return code, output.getvalue(), errors.getvalue()

    def test_scan_reports_a_copy_once_and_skips_binary_dumps(self):
        code, out, err = self.cli("scan", self.root, "--format", "json")
        self.assertEqual(code, 0, err)
        files = {Path(item["source"]).name: item for item in json.loads(out)["files"]}
        self.assertNotIn("journal.0011.0001.dmp", files)
        self.assertEqual(files["journal.0011.txt.copy.txt"]["status"], "duplicate")
        self.assertTrue(files["journal.0011.txt.copy.txt"]["duplicate_of"].endswith("journal.0011.txt"))
        self.assertEqual(files["journal.0011.txt"]["status"], "ok")
        code, text, _ = self.cli("scan", self.root)
        self.assertIn("未重复扫描", text)

    def test_browse_lists_sessions_with_sidecars_and_indexes_copies_once(self):
        database = Path(self.temporary.name) / "cache" / "terminal.sqlite3"
        code, out, err = self.cli("browse", self.root, "--database", database, "--format", "json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        sources = [Path(item["source"]).name for item in data["files"]]
        self.assertEqual(sources.count("journal.0011.txt"), 1)
        self.assertNotIn("journal.0011.txt.copy.txt", sources)
        self.assertNotIn("dump.0001.txt", sources)
        sessions = {item["label"]: item for item in data["sessions"]}
        first = sessions["journal.0011"]
        self.assertEqual([Path(path).name for path in first["files"]], ["journal.0011.txt", "journal.0011.worker1.log"])
        self.assertEqual({item["name"]: item["status"] for item in first["sidecars"]},
                         {"journal.0011.txt.copy.txt": "duplicate", "journal.0011.0001.dmp": "binary"})
        self.assertEqual((first["dump_count"], first["dump_bytes"]), (1, 6))
        self.assertFalse(sessions["dump.0001.txt"]["recognized"])
        self.assertEqual(sessions["dump.0001.txt"]["sidecars"][0]["status"], "empty")
        code, printed, _ = self.cli("browse", self.root, "--database", database, "--print")
        self.assertIn("== 会话 journal.0011 | Autodesk Revit 2024", printed)
        self.assertIn("dmp×1", printed)
        self.assertIn("journal.0011.txt.copy.txt [副本] 字节相同，未重复索引（与journal.0011.txt相同）", printed)

    def test_file_list_groups_by_session_and_selects_only_indexed_files(self):
        index = ViewerIndex(Path(self.temporary.name) / "cache.sqlite3")
        index.build(index.register(list(self.root.iterdir()), "auto"), "auto")
        browser = Browser(index)
        self.assertEqual(browser.file["name"], "journal.0012.txt")  # newest main journal opens first
        self.assertEqual(index.preferred_id, browser.file["id"])
        browser.handle("f")
        rows = browser.file_rows()
        self.assertTrue(rows[0][0].startswith("会话 journal.0012"))
        self.assertIsNone(rows[0][1])
        self.assertEqual(sum(number is not None for _, number in rows), len(browser.catalog))
        for _ in range(2):
            browser.handle("DOWN")  # skips the session header of journal.0011
        browser.handle("\r")
        self.assertEqual(browser.file["name"], "journal.0011.txt")
        browser.handle("f")
        for width, height in ((100, 25), (40, 12)):
            frame = browser.frame(width, height, False)
            self.assertLessEqual(len(frame.splitlines()), height)
            self.assertTrue(all(cell_width(line) < width for line in frame.splitlines()))
        self.assertIn("未重复索引", browser.frame(160, 40, False))


if __name__ == "__main__":
    unittest.main()
