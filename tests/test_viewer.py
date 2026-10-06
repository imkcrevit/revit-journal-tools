import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

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
        queued = self.index.register([bad], "auto")
        self.index.build(queued, "auto")
        self.assertEqual(self.index.catalog()[0]["status"], "error")
        with self.assertRaises(ValueError):
            self.index.events(queued[0])

    def test_unknown_file_and_invalid_query_parameters(self):
        with self.assertRaises(KeyError):
            self.index.context(999999, 1)
        with self.assertRaises(ValueError):
            self.index.events(self.file_id, category="made_up")
        with self.assertRaises(ValueError):
            self.index.events(self.file_id, limit=1000000)
        with self.assertRaises(ValueError):
            self.index.context(self.file_id, 999)

    def test_empty_file_is_ready_with_no_events(self):
        self.path.write_bytes(b"")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        self.assertEqual(self.index.events(self.file_id)["items"], [])
        self.assertEqual(self.index.catalog()[0]["line_count"], 0)

    def test_http_static_and_api_are_local_and_read_only(self):
        server = make_server(self.index, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/") as response:
                self.assertIn("Journal", response.read().decode("utf-8"))
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            with urllib.request.urlopen(base + f"/api/events?file={self.file_id}&category=error") as response:
                self.assertEqual(json.load(response)["items"][0]["line_number"], 6)
            for request in (
                urllib.request.Request(base + "/api/files", headers={"Host": "example.com"}),
                urllib.request.Request(base + "/api/files", method="POST"),
                urllib.request.Request(base + "/../../etc/passwd"),
            ):
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(request)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
