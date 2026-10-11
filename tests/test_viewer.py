import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from revit_journal_tools import viewer_index
from revit_journal_tools.categories import Categorizer
from revit_journal_tools.rules import load_rules
from revit_journal_tools.viewer_index import ViewerIndex
from revit_journal_tools.viewer_server import make_server


class CategoryTests(unittest.TestCase):
    def test_requested_categories_and_error_priority(self):
        samples = [
            ("'C 04-Oct-2026 21:56:04; started recording journal file", "startup"),
            ("' Build: synthetic", "startup"),
            ("' 0:< Initial VM: Avail 100 MB", "system"),
            ("' 0:< OSLanguage : 1033", "system"),
            ("DBG_ERROR: Synthetic error", "error"),
            ("Access violation", "error"),
            ("E_OUTOFMEMORY", "error"),
            ("API_ERROR { synthetic failure }", "api_error"),
            ("Jrn.Command \"Synthetic open\"", "operation"),
            ("DBG_WARN: Synthetic warning", "warning"),
            ("API_SUCCESS { Registered synthetic event }", "api"),
            ("ordinary message", "other"),
        ]
        for text, category in samples:
            with self.subTest(text=text):
                self.assertEqual(Categorizer(load_rules()).classify(text)[0], category)

    def test_api_error_and_operation_continuations(self):
        parser = Categorizer(load_rules())
        lines = ["API_ERROR { : conflict", "'Addin's module Example conflicts", "'Last module }", "normal", "Jrn.Data _", ' "File Name", "Example.rvt"', "normal"]
        self.assertEqual([parser.classify(line)[0] for line in lines], ["api_error", "api_error", "api_error", "other", "operation", "operation", "other"])

    def test_unclosed_api_block_stops_at_next_api_event(self):
        parser = Categorizer(load_rules())
        parser.classify("API_ERROR { incomplete")
        self.assertEqual(parser.classify("API_SUCCESS { Registered event }")[0], "api")


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "journal.txt"
        self.path.write_text("' Build: synthetic\nInitial VM: memory\nJrn.Command \"Open\"\nnormal\nAPI_ERROR { problem }\nDBG_ERROR: GeomStepList unique_marker\n<script>alert('untrusted')</script>\n", encoding="utf-8")
        self.index = ViewerIndex(self.root / "viewer.sqlite3")
        queued = self.index.register([self.path], "auto")
        self.file_id = queued[0]
        self.index.build(queued, "auto")

    def test_full_index_categories_search_and_context(self):
        catalog = self.index.catalog()
        self.assertEqual(catalog[0]["status"], "ready")
        self.assertEqual(catalog[0]["line_count"], 7)
        self.assertEqual(catalog[0]["counts"]["api_error"], 1)
        error = self.index.events(self.file_id, "error")
        self.assertEqual([item["line_number"] for item in error["items"]], [6])
        search = self.index.events(self.file_id, query="GeomStep")
        self.assertEqual(search["items"][0]["line_number"], 6)
        context = self.index.context(self.file_id, 6, 1)
        self.assertEqual([line["line_number"] for line in context["items"]], [5, 6, 7])
        self.assertEqual(context["items"][-1]["text"], "<script>alert('untrusted')</script>")

    def test_keyset_pagination_has_no_gaps_or_duplicates(self):
        after, numbers = 0, []
        while True:
            result = self.index.events(self.file_id, after=after, limit=2)
            numbers.extend(item["line_number"] for item in result["items"])
            if not result["has_more"]:
                break
            after = result["next_after"]
        self.assertEqual(numbers, list(range(1, 8)))

    def test_cache_reuse_and_rebuild_remove_stale_fts_entries(self):
        self.assertEqual(self.index.register([self.path], "auto"), [])
        self.path.write_text("ERROR: new_marker\n", encoding="utf-8")
        self.assertTrue(self.index.catalog()[0]["source_changed"])
        queued = self.index.register([self.path], "auto")
        self.index.build(queued, "auto")
        self.assertEqual(self.index.events(self.file_id, query="unique_marker")["items"], [])
        self.assertEqual(self.index.events(self.file_id, query="new_marker")["items"][0]["line_number"], 1)
        with self.index.connect() as connection:
            connection.execute("INSERT INTO line_search(line_search, rank) VALUES('integrity-check',1)")

    def test_failed_decode_never_exposes_partial_index_as_complete(self):
        bad = self.root / "bad.txt"
        bad.write_bytes(b"normal\n" * 6000 + b"\xff")
        queued = self.index.register([bad], "utf-8")
        self.index.build(queued, "utf-8")
        self.assertEqual(self.index.catalog()[0]["status"], "error")
        with self.assertRaises(ValueError):
            self.index.events(queued[0])

    def test_degraded_decode_is_indexed_with_line_flags(self):
        bad = self.root / "bad.txt"
        bad.write_bytes(b"normal\n" * 6000 + b"DBG_ERROR: \x81")
        with patch("revit_journal_tools.reader.system_fallback_encoding", return_value="gb18030"):
            queued = self.index.register([bad], "auto")
            self.index.build(queued, "auto")
        file = self.index.catalog()[0]
        self.assertEqual((file["status"], file["encoding"], file["encoding_fallback"]), ("ready", "gb18030", True))
        self.assertEqual((file["degraded_lines"], file["degraded_sample"]), (1, [6001]))
        last = self.index.events(queued[0], "error")["items"][0]
        self.assertEqual((last["line_number"], last["text"], last["degraded"]), (6001, "DBG_ERROR: \\x81", 1))
        self.assertEqual(self.index.context(queued[0], 6001, 0)["decode_degraded_line_count"], 1)

    def test_unknown_file_and_invalid_query_parameters(self):
        with self.assertRaises(KeyError):
            self.index.context(999999, 1)
        with self.assertRaises(ValueError):
            self.index.events(self.file_id, category="made_up")
        with self.assertRaises(ValueError):
            self.index.events(self.file_id, limit=1000000)
        with self.assertRaises(ValueError):
            self.index.context(self.file_id, 999)

    def test_empty_file_is_listed_as_sidecar_not_indexed(self):
        self.path.write_bytes(b"")
        self.assertEqual(self.index.register([self.path], "auto"), [])
        self.assertEqual(self.index.catalog(), [])
        self.assertEqual([(item["name"], item["status"]) for item in self.index.sidecars], [("journal.txt", "empty")])
        with self.assertRaises(KeyError):
            self.index.events(self.file_id)

    def test_http_static_and_api_are_local_and_read_only(self):
        server = make_server(self.index, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # Connect directly. A system proxy (common on Windows) would otherwise
        # rewrite the Host header and hide what the loopback server receives.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with opener.open(base + "/") as response:
                self.assertIn("Journal", response.read().decode("utf-8"))
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            with opener.open(base + f"/api/events?file={self.file_id}&category=error") as response:
                self.assertEqual(json.load(response)["items"][0]["line_number"], 6)
            for request in (
                urllib.request.Request(base + "/api/files", headers={"Host": "example.com"}),
                urllib.request.Request(base + "/api/files", method="POST"),
                urllib.request.Request(base + "/../../etc/passwd"),
            ):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(request)
                raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@patch.object(viewer_index, "PARALLEL_MIN_BYTES", 0)
class ParallelBuildTests(unittest.TestCase):
    """J8: child processes parse, the parent alone writes; results equal the serial build."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        examples = Path(__file__).resolve().parent.parent / "examples"
        self.paths = []
        for number, source in enumerate(sorted(examples.glob("*.journal.txt")), 1):
            path = self.root / f"journal.{number:04d}.txt"
            path.write_bytes(source.read_bytes() * (number + 2))
            self.paths.append(path)
        self.bad = self.root / "journal.0099.txt"
        self.bad.write_bytes(b"normal\n\xff\n")
        self.paths.append(self.bad)

    def snapshot(self, index):
        result = {}
        with index.connect() as connection:
            for item in index.catalog():
                rows = connection.execute("SELECT line_number,body,category,severity,rules,degraded FROM lines "
                                          "WHERE file_id=? ORDER BY line_number", (item["id"],)).fetchall()
                blocks = connection.execute("SELECT * FROM blocks WHERE file_id=? ORDER BY start_line",
                                            (item["id"],)).fetchall()
                result[item["name"]] = (item["status"], item["error"], item["sha256"], item["line_count"],
                                        item["counts"], item["block_counts"], [tuple(row) for row in rows],
                                        [tuple(row)[1:] for row in blocks])
        return result

    def build(self, workers, name):
        index = ViewerIndex(self.root / name, workers=workers)
        index.build(index.register(self.paths, "utf-8"), "utf-8")
        return index

    def test_parallel_build_equals_serial_and_isolates_failures(self):
        serial = self.build(1, "serial.sqlite3")
        with patch.object(ViewerIndex, "_copy_scratch", autospec=True,
                          side_effect=ViewerIndex._copy_scratch) as copied:
            parallel = self.build(3, "parallel.sqlite3")
        self.assertEqual(copied.call_count, len(self.paths) - 1)  # The undecodable file never gets copied.
        self.assertEqual(self.snapshot(serial), self.snapshot(parallel))
        statuses = {item["name"]: item["status"] for item in parallel.catalog()}
        self.assertEqual(statuses.pop("journal.0099.txt"), "error")
        self.assertEqual(set(statuses.values()), {"ready"})
        # Full-text rows were built for the copied files.
        file_id = next(item["id"] for item in parallel.catalog() if item["name"] == "journal.0001.txt")
        self.assertTrue(parallel.blocks(file_id, query="Vendor")["items"])
        with parallel.connect() as connection:
            connection.execute("INSERT INTO line_search(line_search, rank) VALUES('integrity-check',1)")

    def test_rebuild_after_parallel_build_keeps_fts_consistent(self):
        index = self.build(3, "rebuild.sqlite3")
        self.paths[0].write_text("DBG_ERROR: replaced_marker\n", encoding="utf-8")
        index.build(index.register(self.paths, "utf-8"), "utf-8")
        file_id = next(item["id"] for item in index.catalog() if item["name"] == "journal.0001.txt")
        self.assertEqual(index.events(file_id, query="replaced_marker")["items"][0]["line_number"], 1)
        self.assertEqual(index.events(file_id, query="Vendor")["items"], [])
        with index.connect() as connection:
            connection.execute("INSERT INTO line_search(line_search, rank) VALUES('integrity-check',1)")

    def test_dead_workers_fall_back_to_serial_instead_of_waiting(self):
        # Workers that exit while starting (as when spawn cannot re-import the
        # main module) used to leave the build waiting forever.
        serial = self.build(1, "serial.sqlite3")
        index = ViewerIndex(self.root / "broken.sqlite3", workers=3)
        index.worker_init = (os._exit, (3,))
        index.build(index.register(self.paths, "utf-8"), "utf-8")
        self.assertEqual(self.snapshot(serial), self.snapshot(index))

    def test_stop_leaves_unfinished_files_queued(self):
        index = ViewerIndex(self.root / "stop.sqlite3", workers=3)
        queued = index.register(self.paths, "utf-8")
        stop = threading.Event()
        stop.set()
        index.build(queued, "utf-8", stop)
        self.assertEqual({item["status"] for item in index.catalog()}, {"queued"})


@patch("revit_journal_tools.reader.system_fallback_encoding", return_value="gb18030")
class MixedEncodingIndexTests(unittest.TestCase):
    """One workspace, one encoding decision per file (J2)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.files = {
            "journal.0001.txt": "' 0:< 错误: 不满足约束\r\n".encode("gb18030"),
            "journal.0002.txt": "' Name(Cloud Models)\r\n90.00°\r\nDiese Lizenz läuft\r\n".encode("cp1252"),
            "journal.0003.txt": b"' Build: synthetic\r\n",
        }
        for name, data in self.files.items():
            (self.root / name).write_bytes(data)
        self.paths = [self.root / name for name in self.files]
        self.index = ViewerIndex(self.root / "viewer.sqlite3")

    def encodings(self):
        return {item["name"]: (item["status"], item["encoding"], item["degraded_lines"]) for item in self.index.catalog()}

    def test_chain_indexes_cp1252_and_gb18030_files_together(self, _):
        self.index.build(self.index.register(self.paths, "utf-8,gb18030,cp1252"), "utf-8,gb18030,cp1252")
        self.assertEqual(self.encodings(), {"journal.0001.txt": ("ready", "gb18030", 0),
                                            "journal.0002.txt": ("ready", "cp1252", 0),
                                            "journal.0003.txt": ("ready", "utf-8", 0)})
        file = next(item for item in self.index.catalog() if item["name"] == "journal.0002.txt")
        self.assertEqual(self.index.context(file["id"], 2, 0)["items"][0]["text"], "90.00°")

    def test_auto_records_actual_encoding_and_degraded_state_per_file(self, _):
        self.index.build(self.index.register(self.paths, "auto"), "auto")
        result = self.encodings()
        self.assertEqual(result["journal.0001.txt"], ("ready", "gb18030", 0))
        self.assertEqual(result["journal.0003.txt"], ("ready", "utf-8", 0))
        # cp1252 bytes are not valid gb18030 and do not read as Chinese: cp1252, not degraded.
        self.assertEqual(result["journal.0002.txt"], ("ready", "cp1252", 0))
        file = next(item for item in self.index.catalog() if item["name"] == "journal.0002.txt")
        self.assertEqual(file["encoding_basis"], "candidate")
        self.assertEqual(self.index.context(file["id"], 3, 0)["items"][0]["text"], "Diese Lizenz läuft")

    def test_cache_reuse_follows_the_encoding_actually_used(self, _):
        self.index.build(self.index.register(self.paths[:1], "auto"), "auto")
        # Explicit gb18030 equals what auto used strictly: reuse, no rebuild;
        # the row keeps describing how it was actually built.
        self.assertEqual(self.index.register(self.paths[:1], "gb18030"), [])
        self.assertEqual(self.index.catalog()[0]["encoding_requested"], "auto")
        self.assertEqual(self.index.register(self.paths[:1], "auto"), [])
        # A different explicit codec or a different auto chain rebuilds.
        self.assertEqual(len(self.index.register(self.paths[:1], "cp1252")), 1)
        self.index.build(self.index.register(self.paths[:1], "auto"), "auto")
        with patch("revit_journal_tools.reader.system_fallback_encoding", return_value="cp1251"):
            self.assertEqual(len(self.index.register(self.paths[:1], "auto")), 1)


if __name__ == "__main__":
    unittest.main()
