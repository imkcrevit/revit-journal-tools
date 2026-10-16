"""Add-in records other than loads: kind, name and origin from the record's own
text (loading.ADDIN_RECORDS), an external server registration merged with its
journal record, and how lists, JSON, HTTP and the session summary show them.
Synthetic, anonymized lines shaped like Revit journal records; they do not
prove real-case accuracy."""

import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from revit_journal_tools.blocks import BlockBuilder
from revit_journal_tools.categories import Categorizer
from revit_journal_tools.cli import main
from revit_journal_tools.loading import ADDIN_KINDS, block_summary, describe_addin, vendor_id_origin
from revit_journal_tools.reader import JournalLine
from revit_journal_tools.rules import load_rules
from revit_journal_tools.summary import SessionCollector
from revit_journal_tools.terminal import Browser
from revit_journal_tools.viewer_index import ViewerIndex
from revit_journal_tools.viewer_server import make_server

RULES = load_rules()
SERVER_ID = "00000000-0000-0000-0000-00000000000b"
SERVER = ("' 0:< API_SUCCESS { Registered an external server: Name(Synthetic Server); VendorId(ADSK); "
          f"ServerId({SERVER_ID}); ServiceId(00000000-0000-0000-0000-00000000000a) }} ")
EMPTY = " 'E 01-Oct-2026 10:00:00.001;   0:< "
REGISTER = (" ' [Jrn.RegisterExternalServer] Rvt.Attr.ExternalServer: {id} "
            "Rvt.Attr.ExternalService: 00000000-0000-0000-0000-00000000000a ")
SERVICE = ("' 0:< API_SUCCESS { Registered an external service: Name(Synthetic Helper Service); VendorId(ADSK); "
           "ServiceId(00000000-0000-0000-0000-00000000000a) } ")
COMMAND = 'Jrn.Command "Ribbon" , "Synthetic command , ID_SYNTHETIC"'
# (line, kind, name, vendor, class); every kind of ADDIN_KINDS at least once.
RECORDS = [
    (SERVICE, "service", "Synthetic Helper Service", "ADSK", ""),
    (SERVER, "server", "Synthetic Server", "ADSK", ""),
    ("' 0:< API_SUCCESS { Registered an external server: Name(Synthetic (Beta)); VendorId(USERADDIN); ServerId(1) } ",
     "server", "Synthetic (Beta)", "USERADDIN", ""),
    (REGISTER.format(id=SERVER_ID), "server", "", "", ""),
    ("' [Jrn.AddInManifest] Rvt.Attr.AddInManifest: Synthetic.Tool.addin Rvt.Attr.AddInType: ExternalDBApplication "
     "Rvt.Attr.AddInName: Synthetic Tool Rvt.Attr.AddInId: 0000 Rvt.Attr.CommandClassName: Synthetic.Tool.App "
     "Rvt.Attr.AddInVersion: 1.0 Rvt.Attr.CommandVendorId: DEMO Rvt.Attr.CommandVendorDescription: Example vendor, "
     "https://plugins.example Rvt.Attr.AddInCodeSigningStatus: Unsigned 1Rvt.Attr.AddInLoadFailureMessage: NoError  , "
     "0.010000 , 0 , 0 ", "manifest", "Synthetic Tool", "DEMO(Example vendor, https://plugins.example)",
     "Synthetic.Tool.App"),
    ("  ' [Jrn.ExternalEventExecution] Rvt.Attr.AddInId: 0000 Rvt.Attr.AddInName: Synthetic Bridge "
     "Rvt.Attr.AddInVersion: 1.0 Rvt.Attr.ExternalEventName: Run Rvt.Attr.VendorDescription: Autodesk, "
     "www.autodesk.com Rvt.Attr.VendorId: ADSK ", "event", "Synthetic Bridge", "ADSK(Autodesk, www.autodesk.com)", ""),
    ("  ' [Jrn.AddInDynamicUpdate] Rvt.Attr.AddInId: 0000 Rvt.Attr.AddInName: Synthetic Results "
     "Rvt.Attr.NumberOfExecutions: 1 Rvt.Attr.UpdaterId: 0001 Rvt.Attr.UpdaterName: Synthetic Updater "
     "Rvt.Attr.VendorId: adsk ", "updater", "Synthetic Results", "adsk", ""),
    ("  ' [Jrn.AddInDynamicUpdate] Rvt.Attr.AddInId: 0000 Rvt.Attr.NumberOfExecutions: 1 Rvt.Attr.UpdaterId: 0001 "
     "Rvt.Attr.UpdaterName: Synthetic Updater ", "updater", "", "", ""),
    ("' 0:< Third Party Updater 'Synthetic: NumberingUpdater' has been unregistered. ",
     "updater", "Synthetic: NumberingUpdater", "", ""),
    ("' 0:< Forcibly unregistering Updater with id:00000000-0000-0000-0000-000000000004 ", "updater", "", "", ""),
    ("' 0:< Forcibly removing all update triggers for all Updaters ", "updater", "", "", ""),
    ("' [Jrn.ExternalServerExecution] Rvt.Attr.AddInId: 0000 Rvt.Attr.ExternalServerExecutionCount: 1 "
     "Rvt.Attr.ExternalServerId: 0001 Rvt.Attr.ExternalServerName: Synthetic Server ", "server_run", "", "", ""),
    ("' [Jrn.ExternalResourceLoad] Rvt.Attr.ExternalResourceType: 00000000-0000-0000-0000-000000000003 0",
     "resource", "", "", ""),
    ("' 0:< The add-in folder 'C:\\Synthetic\\AddIns\\Tool' was registered for the context 'TOOL'. ",
     "folder", "Tool", "", ""),
    ("' 1:< The requested assembly 'Synthetic.Tool, Version=1.0.0.0, Culture=neutral' was loaded to the context "
     "'TOOL' from 'C:\\Synthetic\\Other.dll'", "assembly", "Synthetic.Tool", "", ""),
    ("' 0:< ExternalDBApplication Synthetic.Db (C:\\Program Files (x86)\\Synthetic\\Db.dll) was not loaded ",
     "not_loaded", "", "", "Synthetic.Db"),
    ("' 0:< Add-in manifest file from: C:\\ProgramData\\Synthetic\\Tool.addin, won't be loaded. All-users Add-in "
     "manifest files must be installed to: C:\\Synthetic", "not_loaded", "Tool.addin", "", ""),
    (' Jrn.AddInEvent "AddInJournaling" , "WpfWindow(SyntheticDialog,Synthetic Title).WpfButton(0,OkButton).Click()"',
     "ui", "SyntheticDialog", "", ""),
    (' Jrn.AddInEvent "AddInJournaling" , "FileDialog(Win7Open,Open).Cancel()"', "ui", "", "", ""),
    ("' 0:< AddInJournalClient load journal control: SyntheticView = C:\\Synthetic\\Tool.dll ",
     "control", "SyntheticView", "", ""),
    ("' 0:< AddInJournalClient is loading journal controls from path C:\\Synthetic ", "control", "", "", ""),
    ("' 0:< Unregistering all external services. ", "unregister", "", "", ""),
    ("' 0:< API_SUCCESS { Starting External Application: Synthetic Tool, Class: Synthetic.App, Vendor : DEMO() } ",
     "load", "Synthetic Tool", "DEMO()", "Synthetic.App"),
]
# The same words without the record around them.
NOT_RECORDS = [
    "' 0:< API_SUCCESS { Registering event by application Synthetic Tool } ",
    "' 0:< Registered external services: 3",
    "' 0:< Name(Synthetic); VendorId(ADSK); ServerId(1)",
    "' 0:< The requested assembly 'Synthetic' could not be found",
    "' 0:< The add-in folders were scanned",
    "' 0:< Updater registered for the document",
    "' 0:< Synthetic ExternalDBApplication interface loaded",
    "' 0:< Add-in manifest file list refreshed",
    "' [Jrn.ExternalResourceLocalPath] Rvt.Attr.ExternalResourceType: 0000",
]


def grouped(lines):
    builder, categorizer = BlockBuilder(), Categorizer(RULES)
    blocks = []
    for number, text in enumerate(lines, 1):
        blocks.extend(builder.feed(JournalLine(number, text), categorizer.classify(text)))
    return blocks + builder.finish()


def shape(blocks):
    return [(b.start_line, b.end_line, b.category, b.addin_kind, b.addin_origin) for b in blocks]


class RecordTests(unittest.TestCase):
    def test_every_kind_has_its_name_vendor_and_class(self):
        self.assertEqual({kind for _, kind, *_ in RECORDS}, set(ADDIN_KINDS))
        for text, kind, name, vendor, class_name in RECORDS:
            with self.subTest(kind=kind, text=text[:60]):
                self.assertEqual(describe_addin(text), (kind, name, vendor, class_name))

    def test_near_misses_are_no_records(self):
        for text in NOT_RECORDS:
            with self.subTest(text=text):
                self.assertIsNone(describe_addin(text))
                self.assertTrue(all(not b.addin_kind and not b.addin_origin for b in grouped([text])))

    def test_declared_vendor_ids(self):
        self.assertEqual(vendor_id_origin("Autodesk"), ("autodesk", "vendor_id_autodesk"))
        self.assertEqual(vendor_id_origin("Autodesk Navisworks"), ("autodesk", "vendor_id_autodesk"))
        self.assertEqual(vendor_id_origin("ADSK"), ("autodesk", "vendor_id_adsk"))
        self.assertEqual(vendor_id_origin("USERADDIN"), ("third_party", "other_vendor"))
        self.assertEqual(vendor_id_origin("AutodeskSynthetic"), ("third_party", "other_vendor"))
        self.assertEqual(vendor_id_origin(""), ("unknown", "vendor_missing"))
        cases = [("VendorId(Autodesk); ", "autodesk", "Autodesk", "vendor_id_autodesk"),
                 ("VendorId(USERADDIN); ", "third_party", "USERADDIN", "other_vendor"),
                 ("", "unknown", "", "vendor_missing")]
        for vendor, origin, value, basis in cases:
            with self.subTest(vendor=vendor):
                block, = grouped([f"' 0:< API_SUCCESS {{ Registered an external server: Name(Synthetic); {vendor}"
                                  "ServerId(1) } "])
                self.assertEqual((block.addin_kind, block.addin_origin), ("server", origin))
                self.assertEqual(block.addin_evidence, [{"line_number": 1, "value": value, "basis": basis}])

    def test_loading_vendor_keeps_its_own_rules(self):
        # "Autodesk" is a declared ID only in VendorId records; a load's Vendor needs ADSK or autodesk.com.
        block, = grouped(["API_SUCCESS { Starting External Application: Synthetic, Vendor : Autodesk() }"])
        self.assertEqual((block.addin_kind, block.addin_origin), ("load", "third_party"))

    def test_blocks_carry_kind_name_class_and_origin(self):
        for text, kind, name, vendor, class_name in RECORDS:
            with self.subTest(kind=kind, text=text[:60]):
                block = grouped([text])[0]
                self.assertEqual((block.category, block.addin_kind, block.addin_name, block.addin_class),
                                 ("addin", kind, name, class_name))
                self.assertEqual(block.addin_origin, vendor_id_origin(vendor)[0])
                self.assertEqual(block.addin_evidence[0]["line_number"], 1)

    def test_each_record_is_its_own_block(self):
        # Consecutive manifests or assemblies are listed by name one by one;
        # an unclaimed line after a record stays with it.
        blocks = grouped([RECORDS[4][0], RECORDS[4][0].replace("AddInName: Synthetic Tool", "AddInName: Other Tool"),
                          RECORDS[13][0], "' 0:< The add-in ID '0000' is registered in the context 'TOOL'. ",
                          RECORDS[14][0]])
        self.assertEqual([(b.start_line, b.end_line, b.addin_kind, b.addin_name, b.boundary) for b in blocks],
                         [(1, 1, "manifest", "Synthetic Tool", "addin_record"),
                          (2, 2, "manifest", "Other Tool", "addin_record"),
                          (3, 4, "folder", "Tool", "addin_record"),
                          (5, 5, "assembly", "Synthetic.Tool", "addin_record")])

    def test_names_never_come_from_dll_paths(self):
        block, = grouped(["' 1:< The requested assembly was loaded to the context 'TOOL' from 'C:\\Synthetic\\Tool.dll'"])
        self.assertEqual((block.addin_kind, block.addin_name, block.addin_origin), ("assembly", "", "unknown"))

    def test_record_inside_an_error_block_does_not_describe_it(self):
        blocks = grouped(["API_ERROR { Synthetic failure",
                          "' The requested assembly 'Synthetic.Tool, Version=1' was loaded to the context 'TOOL'", "}"])
        self.assertEqual(shape(blocks), [(1, 3, "api_error", "", "")])

    def test_an_error_title_line_is_still_described(self):
        block, = grouped(["' 0:< API_ERROR { Registered an external server: Name(Synthetic); VendorId(DEMO); ServerId(1) }"])
        self.assertEqual((block.category, block.addin_kind, block.addin_origin), ("api_error", "server", "third_party"))

    def test_pathological_lines_are_read_up_to_a_bound(self):
        # Unclosed Name( and missing context markers repeated over a long line:
        # the lazy scans see at most loading._MAX_RECORD characters.
        for text in ("Registered an external service: " + "Name(x " * 40000,
                     "The requested assembly 'x, " * 20000, "ExternalDBApplication x (" * 20000):
            with self.subTest(text=text[:30]):
                record = describe_addin(text)
                self.assertTrue(record is None or record[1] == "")

    def test_large_identity_is_bounded_and_marked(self):
        block, = grouped([f"' 0:< API_SUCCESS {{ Registered an external service: Name({'N' * 2000}); VendorId(ADSK) }}"])
        self.assertEqual((len(block.addin_name), block.addin_identity_truncated), (512, True))


class ServerMergeTests(unittest.TestCase):
    def test_registration_and_its_journal_record_are_one_block(self):
        blocks = grouped([SERVER, EMPTY, REGISTER.format(id=SERVER_ID), SERVICE])
        self.assertEqual(shape(blocks), [(1, 3, "addin", "server", "autodesk"), (4, 4, "addin", "service", "autodesk")])
        self.assertEqual((blocks[0].boundary, blocks[0].addin_name, blocks[0].complete),
                         ("api_braces", "Synthetic Server", True))
        self.assertTrue(blocks[0].title.startswith("API_SUCCESS { Registered an external server"))

    def test_journal_record_without_id_is_merged_and_ids_ignore_case(self):
        for register in (" ' [Jrn.RegisterExternalServer] ", REGISTER.format(id=SERVER_ID.upper())):
            with self.subTest(register=register):
                self.assertEqual(shape(grouped([SERVER, EMPTY, register])), [(1, 3, "addin", "server", "autodesk")])

    def test_journal_record_right_after_the_api_record_is_merged(self):
        # Worker logs: no timestamp line, and the journal record has no ID.
        blocks = grouped([SERVER, " ' [Jrn.RegisterExternalServer] ", SERVICE])
        self.assertEqual(shape(blocks), [(1, 2, "addin", "server", "autodesk"), (3, 3, "addin", "service", "autodesk")])
        other = grouped([SERVER, REGISTER.format(id="00000000-0000-0000-0000-00000000000c")])
        self.assertEqual(shape(other), [(1, 1, "addin", "server", "autodesk"), (2, 2, "addin", "server", "unknown")])

    def test_identical_registrations_fold(self):
        later = EMPTY.replace("10:00:00.001", "10:00:05.250")
        blocks = grouped([SERVER, EMPTY, REGISTER.format(id=SERVER_ID), SERVER, later, REGISTER.format(id=SERVER_ID)])
        self.assertEqual([(b.start_line, b.end_line) for b in blocks], [(1, 3), (4, 6)])
        self.assertEqual(blocks[0].fingerprint, blocks[1].fingerprint)

    def test_another_server_id_is_its_own_block(self):
        blocks = grouped([SERVER, EMPTY, REGISTER.format(id="00000000-0000-0000-0000-00000000000c")])
        self.assertEqual(shape(blocks), [(1, 1, "addin", "server", "autodesk"), (2, 3, "addin", "server", "unknown")])
        self.assertEqual(blocks[1].boundary, "timestamp+addin_record")

    def test_other_next_line_opens_the_timestamp_block_as_before(self):
        # The command after the held timestamp line moves the phase: the timestamp
        # block takes it, as when the lines are read without holding.
        blocks = grouped([SERVER, EMPTY, COMMAND])
        self.assertEqual([(b.start_line, b.end_line, b.category, b.boundary, b.phase) for b in blocks],
                         [(1, 1, "addin", "api_braces", "startup"),
                          (2, 3, "operation", "timestamp+journal_command", "interactive")])

    def test_file_ending_on_the_empty_timestamp_line(self):
        blocks = grouped([SERVER, EMPTY])
        self.assertEqual([(b.start_line, b.end_line, b.boundary, b.complete) for b in blocks],
                         [(1, 1, "api_braces", True), (2, 2, "timestamp", True)])

    def test_only_an_empty_timestamp_record_is_held(self):
        for second in ("", "'E 01-Oct-2026 10:00:00.001; 0:< Synthetic note", "'C 01-Oct-2026 10:00:00.001; 0:<",
                       SERVICE):
            with self.subTest(second=second):
                blocks = grouped([SERVER, second, REGISTER.format(id=SERVER_ID)])
                self.assertEqual(blocks[0].end_line, 1)

    def test_journal_record_before_the_api_record_is_not_merged(self):
        blocks = grouped([EMPTY, REGISTER.format(id=SERVER_ID), SERVER])
        self.assertEqual(shape(blocks), [(1, 2, "addin", "server", "unknown"), (3, 3, "addin", "server", "autodesk")])


class SummaryTests(unittest.TestCase):
    def test_session_summary_counts_loading_records_only(self):
        lines = ["API_SUCCESS { Starting External Application: Synthetic Partner, Vendor : DEMO(https://plugins.example) }",
                 *(text for text, kind, *_ in RECORDS if kind != "load")]
        collector = SessionCollector()
        collector.add_blocks(grouped(lines))
        addins = collector.finish()["addins"]
        self.assertEqual((addins["loading_records"], addins["autodesk"], addins["third_party"], addins["unknown"]),
                         (1, 0, 1, 0))
        self.assertEqual([item["name"] for item in addins["third_party_names"]], ["Synthetic Partner"])

    def test_list_summaries_name_kind_and_origin(self):
        server, = grouped([SERVER])
        self.assertEqual(block_summary(vars(server)), "Synthetic Server [服务器] [Autodesk]")
        unnamed, = grouped([RECORDS[11][0]])
        self.assertEqual(block_summary(vars(unnamed)),
                         "[服务器执行] [来源待定] [Jrn.ExternalServerExecution] Rvt.Attr.AddInId: 0000 "
                         "Rvt.Attr.ExternalServerExecutionCount: 1 Rvt.Attr.ExternalServerId: 0001 "
                         "Rvt.Attr.ExternalServerName: Synthetic Server")
        # A load never shows its raw record in the list, named or not.
        load, = grouped(["API_ERROR { Starting External Application: Vendor: DEMO() }"])
        self.assertEqual(block_summary(vars(load)), "[加载] [第三方] [API错误]")


class IndexOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "journal.0001.txt"
        self.path.write_text("\n".join([
            "API_SUCCESS { Starting External Application: Synthetic Tool, Class: Synthetic.App, Vendor : ADSK() }",
            SERVICE, SERVER, EMPTY, REGISTER.format(id=SERVER_ID),
            RECORDS[4][0], RECORDS[11][0],
            SERVER, EMPTY.replace("10:00:00.001", "10:00:09.001"), REGISTER.format(id=SERVER_ID),
        ]) + "\n", encoding="utf-8")
        self.database = self.root / "index.sqlite3"
        self.index = ViewerIndex(self.database)
        queued = self.index.register([self.path], "auto")
        self.index.build(queued, "auto")
        self.file_id = queued[0]

    def kinds(self, **options):
        return [(b["start_line"], b["addin_kind"]) for b in self.index.blocks(self.file_id, **options)["items"]]

    def test_index_stores_kinds_and_filters_cover_every_kind(self):
        self.assertEqual(self.kinds(category="addin", fold=False),
                         [(1, "load"), (2, "service"), (3, "server"), (6, "manifest"), (7, "server_run"), (8, "server")])
        self.assertEqual(self.kinds(category="addin", fold=True),
                         [(1, "load"), (2, "service"), (3, "server"), (6, "manifest"), (7, "server_run")])
        self.assertEqual(self.kinds(category="addin_autodesk", fold=False),
                         [(1, "load"), (2, "service"), (3, "server"), (8, "server")])
        self.assertEqual(self.kinds(category="addin_third_party"), [(6, "manifest")])
        self.assertEqual(self.kinds(addin_prefix="Synthetic Server", fold=False), [(3, "server"), (8, "server")])
        self.assertEqual(self.kinds(addin_prefix="Synthetic.Tool.App"), [(6, "manifest")])
        counts = self.index.catalog()[0]["block_counts"]
        self.assertEqual((counts["addin"], counts["addin_autodesk"], counts["addin_third_party"],
                          counts["addin_unknown"]), (6, 4, 1, 1))

    def test_json_and_print_show_kind_name_and_origin(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["browse", str(self.path), "--database", str(self.database), "--category", "addin",
                         "--no-fold", "--format", "json"])
        self.assertEqual(code, 0)
        items = json.loads(output.getvalue())["files"][0]["items"]
        self.assertEqual([item["addin_kind"] for item in items],
                         ["load", "service", "server", "manifest", "server_run", "server"])
        self.assertEqual((items[2]["start_line"], items[2]["end_line"], items[2]["addin_name"]), (3, 5, "Synthetic Server"))
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["browse", str(self.path), "--database", str(self.database), "--category", "addin", "--print"])
        self.assertEqual(code, 0)
        printed = output.getvalue()
        self.assertIn("当前日志Add-in: 6块 | 来源: Autodesk 4 / 第三方1 / 来源待定1", printed)
        for summary in ("L1–1 Synthetic Tool [加载] [Autodesk]", "L2–2 Synthetic Helper Service [服务] [Autodesk]",
                        "L3–5 ×2（末次L8） Synthetic Server [服务器] [Autodesk]",
                        "L6–6 Synthetic Tool [清单] [第三方]", "L7–7 [服务器执行] [来源待定] [Jrn.ExternalServerExecution]"):
            self.assertIn(summary, printed)

    def test_http_blocks_and_block_carry_the_kind(self):
        server = make_server(self.index, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, no system proxy

        def get(path, **params):
            url = f"http://127.0.0.1:{server.server_port}{path}?" + urllib.parse.urlencode({"file": self.file_id, **params})
            with opener.open(url) as response:
                return json.load(response)

        listed = get("/api/blocks", category="addin_autodesk", fold=0)["items"]
        self.assertEqual([(item["addin_kind"], item["summary"]) for item in listed][1:3],
                         [("service", "Synthetic Helper Service [服务] [Autodesk]"),
                          ("server", "Synthetic Server [服务器] [Autodesk]")])
        detail = get("/api/block", start=3)
        self.assertEqual((detail["block"]["addin_kind"], detail["block"]["end_line"]), ("server", 5))
        self.assertEqual([item["line_number"] for item in detail["items"]], [3, 4, 5])

    def test_terminal_lists_one_name_row_per_record(self):
        browser = Browser(self.index, category="addin")
        with patch.object(self.index, "block_lines", side_effect=AssertionError("原文只应在展开后读取")):
            for density in (0, 1, 2):
                browser.density = density
                frame = browser.frame(120, 25, False)
                self.assertIn("Synthetic Helper Service [服务] [Autodesk] | L2–2", frame)
                self.assertIn("×2 Synthetic Server [服务器] [Autodesk] | L3–5", frame)
                self.assertNotIn("Registered an external", frame)
        browser.selected = 2
        browser.handle("\r")
        frame = browser.frame(120, 25, False)
        self.assertIn("种类: 服务器 | Class: 未提供", frame)
        self.assertIn("[Jrn.RegisterExternalServer]", frame)

    def test_old_index_without_the_kind_column_is_migrated_and_rebuilt(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("ALTER TABLE blocks DROP COLUMN addin_kind")
            connection.execute("UPDATE files SET signature='viewer-7:blocks-6-categories:old'")
        migrated = ViewerIndex(self.database)
        queued = migrated.register([self.path], "auto")
        self.assertEqual(queued, [self.file_id])
        migrated.build(queued, "auto")
        self.assertEqual([b["addin_kind"] for b in migrated.blocks(self.file_id, "addin_third_party")["items"]],
                         ["manifest"])


if __name__ == "__main__":
    unittest.main()
