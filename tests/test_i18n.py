"""English page texts (?lang=en) next to the unchanged Chinese default, and
the Revit version each journal is grouped under. Synthetic journals only."""

import json
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from revit_journal_tools import i18n
from revit_journal_tools.cli import plan_workspace
from revit_journal_tools.loading import ADDIN_KINDS
from revit_journal_tools.session_view import line_ref, summary_lines
from revit_journal_tools.sidecars import revit_version
from revit_journal_tools.summary import session_findings
from revit_journal_tools.viewer_index import ViewerIndex
from revit_journal_tools.viewer_server import make_server, session_view

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "paragraphs.journal.txt"
APP = Path(__file__).resolve().parent.parent / "src" / "revit_journal_tools" / "web" / "app.js"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, no system proxy


class TextTests(unittest.TestCase):
    def test_errors_translate_and_unknown_messages_stay(self):
        self.assertEqual(i18n.error_text("未知分类", "en"), "Unknown category")
        self.assertEqual(i18n.error_text("未知分类", "zh"), "未知分类")
        self.assertIn("for 2024, 2025", i18n.error_text("没有找到2024、2025的Revit Journals目录（…）；请传入文件或目录", "en"))
        self.assertIn("any version", i18n.error_text("没有找到任何版本的Revit Journals目录（…）", "en"))
        self.assertEqual(i18n.error_text("'utf-8' codec can't decode byte", "en"), "'utf-8' codec can't decode byte")
        with self.assertRaises(ValueError):
            i18n.check_lang("de")

    def test_labels_fall_back_to_chinese(self):
        self.assertEqual(i18n.label("category", "api_error", {"api_error": "API错误"}, "en"), "API error")
        self.assertEqual(i18n.label("category", "api_error", {"api_error": "API错误"}, "zh"), "API错误")
        self.assertEqual(i18n.label("exception_code", "0x1234", {"0x1234": "某异常"}, "en"), "某异常")

    def test_line_references_by_language(self):
        self.assertEqual(line_ref("  第3行 [异常前的命令] Jrn.Command"), 3)
        self.assertEqual(line_ref("  line 3 [command before the exception] Jrn.Command", "en"), 3)
        self.assertEqual(line_ref("      40 | 第7行", "zh"), 40)  # the last-lines column wins
        self.assertEqual(line_ref("  L13–15 [Operation] Jrn.Command", "en"), 13)
        self.assertIsNone(line_ref("Phases: Startup L2 → Shutdown L31", "en"))
        self.assertIsNone(line_ref("Licensing: 2 license errors (lines 5, 9)", "en"))

    def test_revit_version_from_folder_then_header(self):
        folder = Path("C:/Users/x/AppData/Local/Autodesk/Revit/Autodesk Revit 2024/Journals/journal.0001.txt")
        self.assertEqual(revit_version(folder, {"branch": "RELEASE_2026.5"}), 2024)
        copy = Path("D:/cases/upload/journal.0007.txt")
        self.assertEqual(revit_version(copy, {"branch": "RELEASE_2025.5", "release": "2025.5"}), 2025)
        self.assertEqual(revit_version(copy, {"branch": "", "release": "2023.1.9"}), 2023)
        self.assertIsNone(revit_version(copy, {}))


class PageTextTests(unittest.TestCase):
    """The page's own tables (web/app.js) against the Python ones: read as text, never run."""

    def setUp(self):
        self.source = APP.read_text(encoding="utf-8")

    def block(self, start, end):
        return self.source[self.source.index(start):self.source.index(end)]

    def test_every_page_text_has_both_languages(self):
        keys = {lang: set(re.findall(r'^\s+"([\w.]+)":|, "([\w.]+)":', self.block(start, end), re.M))
                for lang, start, end in (("zh", "  zh: {\n", "  en: {\n"), ("en", "  en: {\n", "const LABELS"))}
        zh, en = ({next(filter(None, pair)) for pair in found} for found in (keys["zh"], keys["en"]))
        self.assertEqual(zh, en)
        self.assertLessEqual({"col.kinds", "col.menu", "menu.all", "menu.none", "menu.apply"}, zh)

    def test_kind_labels_follow_addin_kinds(self):
        # The column menu lists the kinds in this order, with block_counts["kind_" + key].
        labels = self.block("const LABELS", "// Line and paragraph categories")
        tables = [dict(re.findall(r'(\w+): "([^"]*)"', table)) for table in re.findall(r"kind: \{([^}]*)\}", labels)]
        self.assertEqual(len(tables), 2)
        zh, en = tables
        self.assertEqual(list(zh), list(ADDIN_KINDS))
        self.assertEqual(zh, ADDIN_KINDS)
        self.assertEqual(en, i18n.EN["addin_kind"])


class SessionTextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.path = root / "Autodesk Revit 2027" / "Journals" / "journal.0001.txt"
        self.path.parent.mkdir(parents=True)
        shutil.copyfile(EXAMPLE, self.path)
        (self.path.parent / "journal.0001.0001.dmp").write_bytes(b"MDMP")
        self.index = ViewerIndex(root / "viewer.sqlite3", workers=1)
        self.index.build(self.index.register(plan_workspace([self.path.parent])[0], "auto"), "auto")
        self.file_id = self.index.catalog()[0]["id"]

    def test_english_summary_names_the_same_lines(self):
        chinese, english = session_view(self.index, self.file_id), session_view(self.index, self.file_id, "en")
        self.assertEqual(len(chinese["lines"]), len(english["lines"]))
        # Every linked line points to the same journal line in both languages.
        self.assertEqual(chinese["refs"], english["refs"])
        self.assertTrue(any(chinese["refs"]))
        self.assertTrue(english["label"].startswith("Session journal.0001 | "))
        self.assertIn("Main journal×1", english["label"])
        # The example ends with a synthetic marker, not Revit's "finished recording journal file".
        self.assertTrue(english["lines"][0].endswith("| Did not end normally (no end marker: crash, forced exit, "
                                                     "or still being written)"))
        self.assertTrue(any(line.startswith("Time: ") and line.endswith("| no end marker") for line in english["lines"]))
        self.assertIn("Phases: Startup L1 → Interactive L12", english["lines"])
        self.assertIn("      line 31 'C 01-Oct-2026 10:00:03.001; finished synthetic session", english["lines"])
        self.assertFalse(any("\u4e00" <= char <= "\u9fff" for line in english["lines"] for char in line),
                         "only journal text may stay untranslated, and this journal is ASCII")

    def test_chinese_stays_the_default_and_findings_keep_their_ids(self):
        session, = self.index.sessions()
        self.assertEqual(session_view(self.index, self.file_id)["lines"], summary_lines(session, session_view.__globals__[
            "main_tail"](self.index, session, self.index.catalog(), 10)))
        chinese = session_findings(session["summary"], False, session["sidecar_logs"], session["main"])
        english = session_findings(session["summary"], False, session["sidecar_logs"], session["main"], "en")
        self.assertEqual([item["id"] for item in chinese], [item["id"] for item in english])
        self.assertEqual([item["evidence"][0]["line_number"] for item in chinese],
                         [item["evidence"][0]["line_number"] for item in english])
        self.assertIn("session.crash_dump", [item["id"] for item in english])
        self.assertTrue(all(item["title"].isascii() for item in english))

    def test_http_language_parameter(self):
        server = make_server(self.index, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def get(path, **params):
            with OPENER.open(base + path + "?" + urllib.parse.urlencode(params)) as response:
                return json.load(response)

        def error(path, **params):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                OPENER.open(base + path + "?" + urllib.parse.urlencode(params))
            with raised.exception:
                return raised.exception.code, json.load(raised.exception)["error"]

        try:
            files = get("/api/files", lang="en")["files"]
            self.assertEqual(files[0]["revit_year"], 2027)
            # Only the fields the page lists: no phases, crash marks or last lines every few seconds.
            self.assertEqual(sorted(files[0]["summary"]), ["branch", "build", "end_time", "release", "start_time"])
            self.assertTrue(get("/api/session", file=self.file_id, lang="en")["label"].startswith("Session "))
            self.assertEqual(error("/api/blocks", file=self.file_id, category="lunch", lang="en"), (400, "Unknown category"))
            self.assertEqual(error("/api/blocks", file=self.file_id, category="lunch"), (400, "未知分类"))
            self.assertEqual(error("/api/session", file=999, lang="en"), (404, "The file is not in the current workspace"))
            self.assertEqual(error("/api/files", lang="de")[0], 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
