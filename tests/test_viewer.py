import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

from revit_journal_tools import viewer_index
from revit_journal_tools.categories import Categorizer
from revit_journal_tools.i18n import error_text
from revit_journal_tools.rules import load_rules
from revit_journal_tools.viewer_index import (CJK_HINT, EMPTY_QUERY, NO_SEARCH_TOKENS, SearchQuery, ViewerIndex, fts_table,
                                              parse_query)
from revit_journal_tools.viewer_server import make_server


def integrity_check(index):
    """FTS5's own consistency check on every ready file's full-text table."""
    with index.connect() as connection:
        for item in index.catalog():
            if item["status"] == "ready":
                table = fts_table(item["id"])
                connection.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check',1)")


class CategoryTests(unittest.TestCase):
    def test_requested_categories_and_error_priority(self):
        samples = [
            ("'C 04-Oct-2026 21:56:04; started recording journal file", "startup"),
            ("' Build: synthetic", "startup"),
            ("' 0:< Initial VM: Avail 100 MB", "startup"),  # no user action yet: start-up
            ("' 0:< OSLanguage : 1033", "startup"),
            ("DBG_ERROR: Synthetic error", "error"),
            ("Access violation", "error"),
            ("E_OUTOFMEMORY", "error"),
            ("API_ERROR { synthetic failure }", "api_error"),
            ("Jrn.Command \"Synthetic open\"", "operation"),
            ("DBG_WARN: Synthetic warning", "warning"),
            ("API_SUCCESS { Registered synthetic event }", "api"),
            ("ordinary message", "startup"),
        ]
        for text, category in samples:
            with self.subTest(text=text):
                self.assertEqual(Categorizer(load_rules()).classify(text)[0], category)

    def test_api_error_and_operation_continuations(self):
        parser = Categorizer(load_rules())
        lines = ["API_ERROR { : conflict", "'Addin's module Example conflicts", "'Last module }", "normal", "Jrn.Data _", ' "File Name", "Example.rvt"', "normal"]
        self.assertEqual([parser.classify(line)[0] for line in lines], ["api_error", "api_error", "api_error", "startup", "operation", "operation", "startup"])

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
        integrity_check(self.index)

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

    def test_http_search_across_files_hit_lines_and_query_errors(self):
        other = self.root / "other.txt"
        other.write_text("normal\nDBG_ERROR: GeomStepList again\nGeomStepList twice\n", encoding="utf-8")
        self.index.build(self.index.register([self.path, other], "auto"), "auto")
        server = make_server(self.index, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, no system proxy
        base = f"http://127.0.0.1:{server.server_port}"

        def get(path, **params):
            with opener.open(base + path + "?" + urllib.parse.urlencode(params)) as response:
                return json.load(response)

        def error(path, **params):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                opener.open(base + path + "?" + urllib.parse.urlencode(params))
            with raised.exception:
                return raised.exception.code, json.load(raised.exception)["error"]

        try:
            found = get("/api/search", q="GeomStepList", per_file=1)
            self.assertEqual((found["terms"], found["total_lines"], found["file_count"], found["skipped"]),
                             (["GeomStepList"], 3, 2, 0))
            self.assertIn("elapsed_ms", found)
            by_name = {file["name"]: file for file in found["files"]}
            self.assertEqual(set(by_name), {self.path.name, other.name})
            self.assertEqual((by_name[other.name]["hit_lines"], [item["line_number"] for item in by_name[other.name]["items"]]),
                             (2, [2]))
            self.assertEqual(by_name[self.path.name]["items"][0]["text"], "DBG_ERROR: GeomStepList unique_marker")
            # Every word, in any order; a line category applies to every file.
            self.assertEqual(get("/api/search", q="unique_marker DBG_ERROR")["total_lines"], 1)
            self.assertEqual(get("/api/search", q="GeomStepList", category="error")["total_lines"], 2)

            blocks = get("/api/blocks", file=self.file_id, q="GeomStep")
            block = blocks["items"][0]
            self.assertEqual((block["hit_line"], blocks["hit_lines"], blocks["terms"]), (6, 1, ["GeomStep"]))
            self.assertTrue(block["start_line"] <= block["hit_line"] <= block["end_line"])
            self.assertIn("label", block)  # _labelled keeps hit_line next to the list text
            self.assertEqual(get("/api/events", file=self.file_id, q="GeomStep")["hit_lines"], 1)

            # Only punctuation: an error, not an empty list; Chinese by default, English with lang=en.
            for path, params in (("/api/search", {}), ("/api/blocks", {"file": self.file_id}),
                                 ("/api/events", {"file": self.file_id})):
                with self.subTest(path=path):
                    self.assertEqual(error(path, q="}", **params), (400, NO_SEARCH_TOKENS))
                    self.assertEqual(error(path, q="} ;", lang="en", **params), (400, error_text(NO_SEARCH_TOKENS, "en")))
            self.assertEqual(error("/api/search", q=" "), (400, EMPTY_QUERY))
            self.assertEqual(error("/api/search", q=" ", lang="en"), (400, "Enter a search term"))
            self.assertEqual(error("/api/search", q="x", per_file=21)[0], 400)
            self.assertEqual(error("/api/search", q="x", category="addin_autodesk", lang="en"), (400, "Unknown category"))
            # The CJK hint in the page's language.
            self.assertEqual(get("/api/search", q="错误")["hint"], CJK_HINT)
            self.assertEqual(get("/api/blocks", file=self.file_id, q="错误", lang="en")["hint"], error_text(CJK_HINT, "en"))
            self.assertNotIn("hint", get("/api/events", file=self.file_id, q="GeomStep", lang="en"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# Synthetic: start-up, two add-in loads (one failed), an add-in record, one
# user action, a memory sample in the session and the shutdown records.
STAGED_JOURNAL = [
    "'C 01-Oct-2026 10:00:00.000; started recording journal file",
    "' Build: synthetic",
    "' 0:< Initial VM: Avail 100 MB, Used 10 MB",
    "' 0:< API_SUCCESS { Starting External Application: Synthetic Tool, Class: Synthetic.Tool, Vendor: ADSK() }",
    "' 0:< API_ERROR { Starting External Application: Broken Tool, Class: Broken.Tool, Vendor: DEMO() }",
    "' 0:< [Jrn.AddInManifest] Rvt.Attr.AddInType: ExternalDBApplication",
    'Jrn.Command "Ribbon" , "Synthetic command , ID_SYNTHETIC"',
    "' 0:< Delta VM: Avail 90 MB, Used 20 MB",
    "'C 01-Oct-2026 10:05:00.000; ->desktop ExitManagedInstance",
    "' 0:< Delta VM: Avail 80 MB, Used 30 MB",
    "'C 01-Oct-2026 10:05:01.000; finished recording journal file",
]


class ServedJournal(unittest.TestCase):
    """One synthetic journal (JOURNAL) indexed and served on a free loopback port."""

    JOURNAL: list[str] = []

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        path = root / "journal.0001.txt"
        path.write_text("\n".join(self.JOURNAL) + "\n", encoding="utf-8")
        self.index = ViewerIndex(root / "viewer.sqlite3")
        queued = self.index.register([path], "auto")
        self.index.build(queued, "auto")
        self.file_id = queued[0]
        server = make_server(self.index, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, no system proxy
        self.base = f"http://127.0.0.1:{server.server_port}"

    def get(self, path, **params):
        with self.opener.open(self.base + path + "?" + urllib.parse.urlencode(params)) as response:
            return json.load(response)

    def status(self, path, **params):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.opener.open(self.base + path + "?" + urllib.parse.urlencode(params))
        with raised.exception:
            return raised.exception.code

    def lines(self, path, **params):
        return [item["line_number"] for item in self.get(path, file=self.file_id, **params)["items"]]


class CategoryHttpTests(ServedJournal):
    """The Add-in and exit categories through /api/files, /api/events, /api/blocks and /api/search."""

    JOURNAL = STAGED_JOURNAL

    def test_files_list_the_new_categories_and_only_origins_as_extra_filters(self):
        files = self.get("/api/files")
        order = ["startup", "addin", "exit", "error", "api_error", "operation", "warning", "api", "dialog", "other"]
        self.assertEqual(list(files["categories"]), order)
        self.assertEqual(files["categories"]["addin"], "Add-in")
        # Add-in is a category; the origins are the only paragraph-only filters.
        self.assertEqual(list(files["block_filters"]),
                         [*order, "addin_autodesk", "addin_third_party", "addin_unknown"])
        self.assertEqual(files["block_filters"]["addin"], "Add-in")
        self.assertEqual(files["block_filters"]["exit"], "退出")
        file = files["files"][0]
        self.assertNotIn("system", file["counts"])
        self.assertEqual((file["counts"]["addin"], file["counts"]["exit"]), (2, 3))
        # The failed load is an API error with its origin: the origins overlap the categories.
        self.assertEqual((file["block_counts"]["addin_autodesk"], file["block_counts"]["addin_third_party"]), (1, 1))

    def test_events_blocks_and_search_accept_addin_and_exit(self):
        self.assertEqual(self.lines("/api/events", category="addin"), [4, 6])
        self.assertEqual(self.lines("/api/events", category="exit"), [9, 10, 11])
        self.assertEqual(self.lines("/api/events", category="startup"), [1, 2, 3])
        self.assertEqual(self.lines("/api/events", category="other"), [8])
        blocks = self.get("/api/blocks", file=self.file_id, category="addin")["items"]
        self.assertEqual([item["start_line"] for item in blocks], [4, 6])
        self.assertEqual(blocks[0]["addin_origin"], "autodesk")
        self.assertEqual([item["start_line"] for item in self.get("/api/blocks", file=self.file_id,
                                                                     category="addin_third_party")["items"]], [5])
        # One memory sample per stage: before the first action, in the session, after shutdown.
        for category, line in (("startup", 3), ("other", 8), ("exit", 10)):
            with self.subTest(category=category):
                found = self.get("/api/search", q="VM", category=category)
                self.assertEqual([item["line_number"] for item in found["files"][0]["items"]], [line])
        self.assertEqual(self.get("/api/search", q="Synthetic", category="addin")["total_lines"], 1)
        self.assertEqual(self.get("/api/search", q="Delta", category="exit", lang="en")["total_lines"], 1)
        # Origins are paragraph filters: not line categories.
        self.assertEqual(self.status("/api/events", file=self.file_id, category="addin_autodesk"), 400)
        self.assertEqual(self.status("/api/search", q="VM", category="addin_unknown"), 400)
        self.assertEqual(self.status("/api/events", file=self.file_id, category="system"), 400)


class KindFilterTests(ServedJournal):
    """The kinds of add-in records: block_counts["kind_*"] and /api/blocks?kinds=
    (the column menu of the web page) only in the Add-in views."""

    # Synthetic: a load and a failed load, a service, a server registration, a
    # third-party manifest, a server run, then an ordinary command.
    JOURNAL = [
        "' 0:< API_SUCCESS { Starting External Application: Synthetic Tool, Class: Synthetic.App, Vendor : ADSK() }",
        "' 0:< API_ERROR { Starting External Application: Broken Tool, Class: Broken.App, Vendor : DEMO() }",
        "' 0:< API_SUCCESS { Registered an external service: Name(Synthetic Helper Service); VendorId(ADSK) }",
        "' 0:< API_SUCCESS { Registered an external server: Name(Synthetic Server); VendorId(ADSK); ServerId(1) }",
        "' [Jrn.AddInManifest] Rvt.Attr.AddInName: Synthetic Partner Rvt.Attr.CommandVendorId: DEMO ",
        "' [Jrn.ExternalServerExecution] Rvt.Attr.ExternalServerName: Synthetic Server ",
        'Jrn.Command "Ribbon" , "Synthetic command , ID_SYNTHETIC"',
    ]

    def kinds(self, **params):
        items = self.get("/api/blocks", **{"file": self.file_id, "fold": 0, **params})["items"]
        return [(item["start_line"], item["addin_kind"]) for item in items]

    def test_block_counts_count_every_kind_of_add_in_record(self):
        file = self.get("/api/files")["files"][0]
        counts = file["block_counts"]
        # The failed load (an API error) has its kind too, as it has its origin.
        self.assertEqual({key: value for key, value in counts.items() if key.startswith("kind_")},
                         {"kind_load": 2, "kind_service": 1, "kind_server": 1, "kind_manifest": 1, "kind_server_run": 1})
        self.assertEqual((counts["addin"], counts["api_error"], file["block_count"]), (5, 1, 7))

    def test_kinds_keep_only_those_records_in_the_add_in_views(self):
        self.assertEqual(self.kinds(category="addin", kinds="service,server"), [(3, "service"), (4, "server")])
        self.assertEqual(self.kinds(category="addin", kinds="server,service"), [(3, "service"), (4, "server")])
        self.assertEqual(self.kinds(category="addin", kinds=""),
                         [(1, "load"), (3, "service"), (4, "server"), (5, "manifest"), (6, "server_run")])
        # Origins combine with kinds; the failed load is listed under its origin.
        self.assertEqual(self.kinds(category="addin_third_party", kinds="load"), [(2, "load")])
        self.assertEqual(self.kinds(category="addin_third_party", kinds="manifest,service"), [(5, "manifest")])
        self.assertEqual(self.kinds(category="addin_autodesk", kinds="load", fold=1), [(1, "load")])

    def test_kinds_with_a_search_or_a_prefix(self):
        found = self.get("/api/blocks", file=self.file_id, category="addin", kinds="server", q="Synthetic Server")
        self.assertEqual([(item["start_line"], item["hit_line"]) for item in found["items"]], [(4, 4)])
        self.assertEqual(found["hit_lines"], 2)  # matching lines of the file, as without kinds
        listed = self.index.blocks(self.file_id, addin_prefix="Synthetic", kinds=("service", "manifest"))["items"]
        self.assertEqual([item["start_line"] for item in listed], [3, 5])

    def test_kinds_are_ignored_outside_the_add_in_views(self):
        every = self.kinds(category="all")
        self.assertEqual(len(every), 7)
        self.assertEqual(self.kinds(category="all", kinds="service"), every)
        self.assertEqual(self.kinds(category="api_error", kinds="service"), [(2, "load")])

    def test_unknown_kinds_are_rejected(self):
        for kinds in ("bogus", "service,bogus", "Service"):
            with self.subTest(kinds=kinds):
                self.assertEqual(self.status("/api/blocks", file=self.file_id, category="addin", kinds=kinds), 400)
        # Checked in every view, not only where kinds apply.
        self.assertEqual(self.status("/api/blocks", file=self.file_id, category="all", kinds="bogus"), 400)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.opener.open(self.base + "/api/blocks?" + urllib.parse.urlencode(
                {"file": self.file_id, "category": "addin", "kinds": "bogus", "lang": "en"}))
        with raised.exception:
            self.assertEqual(json.load(raised.exception)["error"], "Unknown add-in record kind")
        with self.assertRaisesRegex(ValueError, "未知种类"):
            self.index.blocks(self.file_id, "addin", kinds=("bogus",))


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
                rows = connection.execute("SELECT line_number,body,category,severity,rules,degraded,byte_offset,"
                                          "byte_length FROM lines "
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
        integrity_check(parallel)

    def test_rebuild_after_parallel_build_keeps_fts_consistent(self):
        index = self.build(3, "rebuild.sqlite3")
        self.paths[0].write_text("DBG_ERROR: replaced_marker\n", encoding="utf-8")
        index.build(index.register(self.paths, "utf-8"), "utf-8")
        file_id = next(item["id"] for item in index.catalog() if item["name"] == "journal.0001.txt")
        self.assertEqual(index.events(file_id, query="replaced_marker")["items"][0]["line_number"], 1)
        self.assertEqual(index.events(file_id, query="Vendor")["items"], [])
        integrity_check(index)

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


STORE_SAMPLE = [
    "'C 05-Oct-2026 15:49:53.777;  started recording journal file",
    "'H 05-Oct-2026 15:50:11.000;   0:< ",
    "Jrn.Data  _",
    '        "File Name"  , "IDOK" , _',
    '        "C:\\demo\\模型.rvt"',
    "' 1:< 错误: 不满足约束",
    "API_ERROR { : Assembly version conflict in Demo.dll assembly",
    "'Addin's module Demo.Core conflicts with same preloaded module }",
    "'C 05-Oct-2026 15:51:00.000;   0:< DBG_WARN: same thing - 2 times",
    "'C 05-Oct-2026 15:51:01.000;   0:< DBG_WARN: same thing - 3 times",
    "Jrn.Command \"Ribbon\"  , \"Delete selection , ID_BUTTON_DELETE\"",
    "'C 05-Oct-2026 15:51:48.995;  finished recording journal file",
]


@patch("revit_journal_tools.reader.system_fallback_encoding", return_value="gb18030")
class StoreTests(unittest.TestCase):
    """J9: store="offsets" keeps byte ranges and reads the unchanged source back."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        examples = Path(__file__).resolve().parent.parent / "examples"
        self.paths = []
        for number, source in enumerate(sorted(examples.glob("*.journal.txt")), 1):
            self.paths.append(self.root / f"journal.{number:04d}.txt")
            self.paths[-1].write_bytes(source.read_bytes())
        sample = "\r\n".join(STORE_SAMPLE).encode("gb18030")
        (self.root / "journal.0011.txt").write_bytes(sample + b"\r\n")
        # Degraded: an undecodable byte inside a joined title and a lone CR break.
        (self.root / "journal.0012.txt").write_bytes(sample.replace(b"IDOK", b"ID\xffOK") + b"\rlast")
        self.paths += [self.root / "journal.0011.txt", self.root / "journal.0012.txt"]

    def build(self, store, name=None, paths=None):
        index = ViewerIndex(self.root / (name or f"{store}.sqlite3"), workers=1, store=store)
        index.build(index.register(paths or self.paths, "auto"), "auto")
        return index

    @staticmethod
    def answers(index):
        result = {}
        for item in index.catalog():
            file_id, lines, after = item["id"], [], 0
            while True:
                page = index.events(file_id, after=after, limit=50)
                lines += page["items"]
                if not page["has_more"]:
                    break
                after = page["next_after"]
            blocks = index.blocks(file_id, limit=200)["items"]
            result[item["name"]] = (
                item["store"] != "", lines, blocks, index.blocks(file_id, limit=200, fold=True)["items"],
                [index.block_lines(file_id, block["start_line"], 0, 200)["items"] for block in blocks],
                [index.block_at(file_id, line) for line in range(1, item["line_count"] + 1)],
                index.context(file_id, 3, 4)["items"], index.tail_blocks(file_id, 5),
                index.events(file_id, query="错误")["items"], index.blocks(file_id, query="conflict")["items"],
                index.blocks(file_id, "warning", "same")["items"])
        return result

    def test_offsets_and_text_stores_answer_identically(self, _):
        text, offsets = self.build("text"), self.build("offsets")
        self.assertEqual(self.answers(offsets), self.answers(text))
        self.assertEqual({item["store"] for item in offsets.catalog()}, {"offsets"})
        self.assertEqual({item["store"] for item in text.catalog()}, {"text"})
        sample = next(item for item in offsets.catalog() if item["name"] == "journal.0012.txt")
        self.assertEqual(sample["degraded_lines"], 1)
        with offsets.connect() as connection:
            # No line text in the offsets index; derivable titles only as a prefix.
            self.assertEqual(connection.execute("SELECT count(*) FROM lines WHERE body<>''").fetchone()[0], 0)
            self.assertLessEqual(connection.execute(
                "SELECT max(length(title)) FROM blocks WHERE title_from_line=1").fetchone()[0], 16)
            self.assertGreater(connection.execute("SELECT count(*) FROM blocks WHERE title_joins>0").fetchone()[0], 0)
        joined = next(block for block in offsets.blocks(sample["id"], limit=200)["items"] if block["title_joins"])
        self.assertIn("ID\\xffOK", joined["title"])  # read back from the source with the same escape
        integrity_check(offsets)

    def test_changed_or_missing_source_is_an_error_not_wrong_text(self, _):
        offsets, text = self.build("offsets"), self.build("text")
        path = self.paths[-2]
        file_id = next(item["id"] for item in offsets.catalog() if item["name"] == path.name)
        text_id = next(item["id"] for item in text.catalog() if item["name"] == path.name)
        path.write_bytes(path.read_bytes() + b"appended\r\n")
        for call in (lambda: offsets.events(file_id), lambda: offsets.context(file_id, 2),
                     lambda: offsets.blocks(file_id), lambda: offsets.block_lines(file_id, 1)):
            with self.assertRaisesRegex(ValueError, "源文件已移动、删除或修改"):
                call()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "源文件"):
            offsets.events(file_id)
        self.assertEqual(text.events(text_id)["items"][0]["line_number"], 1)  # the text store keeps the text

    def test_utf16_journals_keep_their_text(self, _):
        path = self.root / "journal.0013.txt"
        path.write_bytes("DBG_ERROR: 宽字节\r\nnext".encode("utf-16"))
        index = self.build("offsets", paths=[path])
        item, = index.catalog()
        self.assertEqual((item["store"], item["encoding"]), ("text", "utf-16"))
        self.assertEqual(index.events(item["id"], "error")["items"][0]["text"], "DBG_ERROR: 宽字节")

    def test_store_switch_rebuilds_and_removes_old_full_text_rows(self, _):
        path = self.paths[-2]
        database = self.root / "switch.sqlite3"
        first = ViewerIndex(database, workers=1, store="text")
        first.build(first.register([path], "auto"), "auto")
        file_id = first.catalog()[0]["id"]
        with first.connect() as connection:
            # As left by an earlier version: text in the shared line_search table.
            connection.execute(f"DROP TABLE {fts_table(file_id)}")
            connection.execute("INSERT INTO line_search(rowid, body) SELECT id, body FROM lines WHERE file_id=?",
                               (file_id,))
            connection.execute("UPDATE files SET store='' WHERE id=?", (file_id,))
        with self.assertRaisesRegex(ValueError, "旧版本"):
            first.events(file_id)
        second = ViewerIndex(database, workers=1, store="offsets")
        self.assertEqual(second.register([path], "auto"), [file_id])  # another store: rebuilt
        second.build([file_id], "auto")
        self.assertEqual(second.catalog()[0]["store"], "offsets")
        self.assertEqual(second.events(file_id, query="错误")["items"][0]["line_number"], 6)
        with second.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM line_search WHERE line_search MATCH 'conflict'").fetchone()[0], 0)
            connection.execute("INSERT INTO line_search(line_search, rank) VALUES('integrity-check',1)")
            self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE name=?",
                                                (fts_table(file_id),)).fetchone()[0], 1)
        third = ViewerIndex(database, workers=1, store="offsets")
        self.assertEqual(third.register([path], "auto"), [])  # same store: reused
        with self.assertRaises(ValueError):
            ViewerIndex(database, store="compressed")


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


SEARCH_SAMPLE = [
    "' Build: synthetic",                                   # 1
    "Jrn.Command \"Ribbon\" , \"Open project , ID_OPEN\"",  # 2
    "alpha beta together",                                  # 3  block 3-4
    "alpha then later beta",                                # 4
    "API_ERROR { gamma first",                              # 5  api_error block 5-7
    "'gamma second inside",                                 # 6
    "'gamma third }",                                       # 7
    "DBG_ERROR: alphabet soup",                             # 8  error block 8-9
    "  at Gamma.Run()",                                     # 9  (line category: other)
    "normal",                                               # 10
    "' 1:< 错误: 不满足约束",                               # 11
    "API_SUCCESS { Starting External Application: Demo Tool, Class: Demo.Tool, Vendor: DEMO(), gamma }",  # 12
]


class SearchTests(unittest.TestCase):
    """Keyword queries: AND of words, quoted phrases, last-word prefix, hit lines
    and the workspace search. Synthetic lines only; no Revit behaviour implied."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "journal.0001.txt"
        self.path.write_text("\n".join(SEARCH_SAMPLE) + "\n", encoding="utf-8")
        self.index = ViewerIndex(self.root / "viewer.sqlite3", workers=1)
        self.index.build(self.index.register([self.path], "auto"), "auto")
        self.file_id = self.index.catalog()[0]["id"]

    def lines(self, query, category="all"):
        return [item["line_number"] for item in self.index.events(self.file_id, category, query)["items"]]

    def test_parse_query_joins_quoted_terms_with_and_and_prefixes_the_last_word(self):
        self.assertEqual(parse_query("Command Jrn"), SearchQuery('"Command" AND "Jrn"*', ["Command", "Jrn"], False))
        self.assertEqual(parse_query('foo "bar , baz" qux').match, '"foo" AND "bar , baz" AND "qux"*')
        self.assertEqual(parse_query('foo "bar baz"').match, '"foo"* AND "bar baz"')
        self.assertEqual(parse_query('"only phrase"').match, '"only phrase"')
        self.assertEqual(parse_query('Jrn.Command "open pro').terms, ["Jrn.Command", "open pro"])  # unterminated quote
        self.assertEqual(parse_query("foo } ;"), SearchQuery('"foo"*', ["foo"], False))
        self.assertEqual(parse_query(" \t "), SearchQuery("", [], False))
        self.assertTrue(parse_query("错误 x").cjk)
        for query in ("}", "} ; ==", '""', '" , "'):
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, "^" + NO_SEARCH_TOKENS + "$"):
                parse_query(query)

    def test_words_match_in_any_order_and_quoted_phrases_only_when_adjacent(self):
        self.assertEqual(self.lines("alpha beta"), [3, 4])
        self.assertEqual(self.lines("beta alpha"), [3, 4])
        self.assertEqual(self.lines('"alpha beta"'), [3])
        self.assertEqual(self.lines('"beta alpha"'), [])
        self.assertEqual(self.lines("Command Jrn"), [2])
        self.assertEqual(self.lines("Jrn.Command"), [2])
        self.assertEqual(self.lines('"Command Jrn"'), [])
        self.assertEqual(self.lines("second third"), [])  # every word in the same line, not the same block

    def test_only_the_last_unquoted_word_is_a_prefix(self):
        self.assertEqual(self.lines("alph"), [3, 4, 8])
        self.assertEqual(self.lines("beta alph"), [3, 4])
        self.assertEqual(self.lines("alph beta"), [])
        self.assertEqual(self.lines('"alph"'), [])
        self.assertEqual(self.lines('"alpha"'), [3, 4])  # quoted: the word itself, not "alphabet"

    def test_punctuation_only_query_is_an_error_not_an_empty_result(self):
        for call in (lambda: self.index.events(self.file_id, query="}"),
                     lambda: self.index.blocks(self.file_id, query="} ;"),
                     lambda: self.index.block_hits(self.file_id, 1, 3, "=="),
                     lambda: self.index.search_workspace("()")):
            with self.assertRaisesRegex(ValueError, "^搜索词不含可检索的字母或数字；标点和符号不参与分词$"):
                call()
        blank = self.index.events(self.file_id, query="  ")
        self.assertEqual((len(blank["items"]), "terms" in blank, "hit_lines" in blank), (12, False, False))
        self.assertNotIn("hit_lines", self.index.blocks(self.file_id))
        self.assertIn("no letters or digits", error_text(NO_SEARCH_TOKENS, "en"))

    def test_blocks_carry_their_first_matching_line(self):
        result = self.index.blocks(self.file_id, query="gamma")
        self.assertEqual([(b["start_line"], b["end_line"], b["hit_line"]) for b in result["items"]],
                         [(5, 7, 5), (8, 9, 9), (12, 12, 12)])
        self.assertEqual([(b["start_line"], b["hit_line"]) for b in self.index.blocks(self.file_id, query="inside")["items"]],
                         [(5, 6)])
        self.assertEqual([(b["start_line"], b["hit_line"]) for b in self.index.blocks(self.file_id, "error", "gamma")["items"]],
                         [(8, 9)])
        # Selected by the add-in prefix (EXISTS test), then looked up.
        addin = self.index.blocks(self.file_id, "addin", "gamma", addin_prefix="Demo")
        self.assertEqual([(b["start_line"], b["hit_line"]) for b in addin["items"]], [(12, 12)])
        self.assertNotIn("hit_line", self.index.blocks(self.file_id)["items"][0])

    def test_hit_line_of_blocks_selected_by_the_small_category_path(self):
        # 512+ hits before the only error block: blocks() tests the few error blocks instead.
        self.path.write_text("common detail\n" * 600 + "\nDBG_ERROR: start\n  at Common.Run()\n", encoding="utf-8")
        self.index.build(self.index.register([self.path], "auto"), "auto")
        result = self.index.blocks(self.file_id, "error", query="common")
        self.assertEqual([(b["start_line"], b["end_line"], b["hit_line"]) for b in result["items"]], [(602, 603, 603)])
        self.assertEqual(self.index.blocks(self.file_id, query="common", limit=1)["items"][0]["hit_line"], 1)

    def test_hit_lines_count_matching_lines_of_the_whole_file(self):
        numbers, after = [], 0
        while True:
            page = self.index.events(self.file_id, query="gamma", after=after, limit=2)
            self.assertEqual((page["terms"], page["hit_lines"]), (["gamma"], 5))
            numbers += [item["line_number"] for item in page["items"]]
            if not page["has_more"]:
                break
            after = page["next_after"]
        self.assertEqual(numbers, [5, 6, 7, 9, 12])
        self.assertEqual(self.index.blocks(self.file_id, query="gamma", limit=1)["hit_lines"], len(numbers))
        self.assertEqual(self.index.events(self.file_id, "api_error", "gamma")["hit_lines"], 3)
        self.assertEqual(self.index.blocks(self.file_id, "api_error", "gamma")["hit_lines"], 3)
        self.assertEqual(self.index.events(self.file_id, "warning", "gamma")["hit_lines"], 0)
        self.assertEqual(self.index.blocks(self.file_id, "addin_third_party", "gamma")["hit_lines"], 5)  # every matching line
        self.assertEqual(self.index.blocks(self.file_id, "addin", "gamma")["hit_lines"], 1)  # a line category
        self.assertEqual(self.index.events(self.file_id, query="absent_word")["hit_lines"], 0)

    def test_block_hits_lists_matching_lines_inside_a_range(self):
        self.assertEqual(self.index.block_hits(self.file_id, 5, 7, "gamma"), [5, 6, 7])
        self.assertEqual(self.index.block_hits(self.file_id, 5, 12, "gamma", limit=2), [5, 6])
        self.assertEqual(self.index.block_hits(self.file_id, 1, 4, "gamma"), [])
        for start, end, limit in ((0, 3, 200), (7, 5, 200), (5, 13, 200), (1, 3, 0), (1, 3, 201)):
            with self.subTest(start=start, end=end, limit=limit), self.assertRaises(ValueError):
                self.index.block_hits(self.file_id, start, end, "gamma", limit)
        with self.assertRaises(KeyError):
            self.index.block_hits(999999, 1, 3, "gamma")

    def test_cjk_query_gets_the_hint(self):
        result = self.index.events(self.file_id, query="不满足")
        self.assertEqual(([item["line_number"] for item in result["items"]], result["hint"]), ([11], CJK_HINT))
        self.assertEqual(self.index.events(self.file_id, query="满足")["items"], [])  # only from the start of a run
        self.assertEqual(self.index.blocks(self.file_id, query="错误")["hint"], CJK_HINT)
        self.assertNotIn("hint", self.index.events(self.file_id, query="gamma"))
        self.assertIn("start of the run", error_text(CJK_HINT, "en"))

    def test_workspace_search_lists_files_with_hits_and_counts_skipped_files(self):
        other = self.root / "journal.0002.txt"
        other.write_text("normal\ngamma elsewhere\nGamma again\n", encoding="utf-8")
        quiet = self.root / "journal.0003.txt"
        quiet.write_text("nothing to see\n", encoding="utf-8")
        broken = self.root / "journal.0004.txt"
        broken.write_bytes(b"gamma\n\xff\n")
        pending = self.root / "journal.0005.txt"
        pending.write_text("gamma pending\n", encoding="utf-8")
        self.index.register([self.path, other, quiet, broken, pending], "utf-8")
        ids = {item["name"]: item["id"] for item in self.index.catalog()}
        self.index.build([file_id for name, file_id in ids.items() if name != pending.name], "utf-8")
        statuses = {item["name"]: item["status"] for item in self.index.catalog()}
        self.assertEqual((statuses[broken.name], statuses[pending.name]), ("error", "queued"))

        result = self.index.search_workspace("gamma", per_file=2)
        self.assertEqual((result["terms"], result["total_lines"], result["file_count"], result["skipped"]),
                         (["gamma"], 7, 2, 2))
        self.assertNotIn("hint", result)
        order = [item["name"] for item in self.index.catalog() if item["name"] in (self.path.name, other.name)]
        self.assertEqual([file["name"] for file in result["files"]], order)
        by_name = {file["name"]: file for file in result["files"]}
        self.assertEqual(set(by_name[other.name]),
                         {"id", "name", "path", "role", "session", "session_label", "hit_lines", "items"})
        self.assertEqual((by_name[self.path.name]["hit_lines"],
                          [item["line_number"] for item in by_name[self.path.name]["items"]]), (5, [5, 6]))
        self.assertEqual(by_name[other.name]["hit_lines"], 2)
        # The same rows as a search in that file.
        self.assertEqual(by_name[other.name]["items"],
                         self.index.events(ids[other.name], query="gamma", limit=2)["items"])
        self.assertEqual(by_name[other.name]["items"][0]["text"], "gamma elsewhere")

        filtered = self.index.search_workspace("gamma", "api_error")
        self.assertEqual([(file["name"], file["hit_lines"]) for file in filtered["files"]], [(self.path.name, 3)])
        self.assertEqual(self.index.search_workspace("错误")["hint"], CJK_HINT)
        # Not searchable now: changed source (store="offsets"), indexed by an earlier version.
        quiet.write_text("gamma changed\n", encoding="utf-8")
        self.assertEqual(self.index.search_workspace("gamma")["skipped"], 3)
        with self.index.connect() as connection:
            connection.execute("UPDATE files SET store='' WHERE id=?", (ids[other.name],))
        older = self.index.search_workspace("gamma")
        self.assertEqual(([file["name"] for file in older["files"]], older["total_lines"], older["skipped"]),
                         ([self.path.name], 5, 4))

        for arguments in ({"per_file": 0}, {"per_file": 21}, {"category": "addin_autodesk"}, {"query": "x" * 257}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.index.search_workspace(**{"query": "gamma", **arguments})
        with self.assertRaisesRegex(ValueError, "请输入搜索词"):
            self.index.search_workspace("  ")


if __name__ == "__main__":
    unittest.main()
