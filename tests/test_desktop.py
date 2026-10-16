"""Desktop window (`gui`): the workspace, the page's pywebview API and the
server it loads, driven by a stand-in for pywebview (no window is opened).
Uses the handwritten examples/paragraphs.journal.txt only."""

import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from revit_journal_tools import cli, desktop

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "paragraphs.journal.txt"
DIALOGS = types.SimpleNamespace(OPEN=10, FOLDER=20)
# Direct connections: a system proxy would rewrite the loopback Host header.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_ready(workspace, timeout=30):
    deadline = time.monotonic() + timeout
    while workspace.building and time.monotonic() < deadline:
        time.sleep(0.02)
    return workspace.index.catalog()


class FakeWindow:
    def __init__(self, answers):
        self.answers, self.dialogs = list(answers), []

    def create_file_dialog(self, kind, **options):
        self.dialogs.append((kind, options))
        return self.answers.pop(0)


class DesktopTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = self.root / "a" / "journal.0001.txt"
        self.second = self.root / "b" / "nested" / "journal.0002.txt"
        for path in (self.first, self.second):
            path.parent.mkdir(parents=True)
            shutil.copyfile(EXAMPLE, path)


class WorkspaceTests(DesktopTestCase):
    def setUp(self):
        super().setUp()
        self.workspace = desktop.Workspace(self.root / "desktop.sqlite3", jobs=1)
        self.addCleanup(self.workspace.close)

    def test_opening_other_inputs_replaces_the_workspace(self):
        self.assertEqual(self.workspace.open_paths([self.first]), {"opened": 1, "errors": []})
        self.assertEqual([item["name"] for item in wait_ready(self.workspace)], ["journal.0001.txt"])
        # A folder without recursion has no journals; with it, the nested one opens.
        self.assertIn("--recursive", self.workspace.open_paths([self.root / "b"])["errors"][0])
        self.assertEqual(self.workspace.open_paths([self.root / "b"], recursive=True)["opened"], 1)
        catalog = wait_ready(self.workspace)
        self.assertEqual([(item["name"], item["status"]) for item in catalog], [("journal.0002.txt", "ready")])

    def test_missing_local_journals_is_reported_not_raised(self):
        with patch("revit_journal_tools.cli.journal_folders", return_value=[]):
            self.assertIn("Revit Journals", self.workspace.open_paths([])["error"])

    def test_page_api_opens_dialog_choices_and_ignores_cancel(self):
        api = desktop.DesktopApi(self.workspace, DIALOGS)
        api._window = FakeWindow([None, [str(self.first)], (str(self.root / "b"),)])
        self.assertEqual(api.open_files(), {"cancelled": True})
        self.assertEqual(api.open_files(), {"opened": 1, "errors": []})
        self.assertEqual(api.open_folder(True), {"opened": 1, "errors": []})
        self.assertEqual([kind for kind, _ in api._window.dialogs], [10, 10, 20])
        self.assertTrue(api._window.dialogs[0][1]["allow_multiple"])
        self.assertEqual([item["name"] for item in wait_ready(self.workspace)], ["journal.0002.txt"])
        # Only the three actions are public: pywebview exposes public attributes to the page.
        self.assertEqual(sorted(name for name in dir(api) if not name.startswith("_")),
                         ["open_files", "open_folder", "open_local"])


class RunTests(DesktopTestCase):
    def fake_webview(self, visit):
        """A pywebview stand-in: start() loads the page as the window would."""
        module = types.SimpleNamespace(FileDialog=DIALOGS, windows=[])

        def create_window(title, url, **options):
            module.windows.append((title, url, options))
            return FakeWindow([])

        def start(**options):
            module.start_options = options
            module.icon_exists = os.path.isfile(options["icon"])
            visit(module.windows[0][1])

        module.create_window, module.start = create_window, start
        return module

    def test_window_loads_the_viewer_page_and_api_in_desktop_mode(self):
        seen = {}

        def visit(url):
            with OPENER.open(url) as response:
                seen["page"] = response.read().decode("utf-8")
            base = url.split("/?")[0]
            with OPENER.open(base + "/icon.png") as response:
                seen["icon"] = (response.headers["Content-Type"], response.read()[:8])
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with OPENER.open(base + "/api/files") as response:
                    files = json.load(response)["files"]
                if files and all(item["status"] == "ready" for item in files):
                    break
                time.sleep(0.05)
            seen["files"] = files
            with OPENER.open(f"{base}/api/blocks?file={files[0]['id']}&category=api_error") as response:
                seen["blocks"] = json.load(response)["items"]

        module = self.fake_webview(visit)
        planned, _, _ = cli.plan_workspace([self.first])
        with patch.dict(sys.modules, {"webview": module}), \
                patch.dict(os.environ, {"REVIT_JOURNAL_CACHE": str(self.root / "cache")}):
            self.assertEqual(desktop.run(planned, self.root / "desktop.sqlite3", jobs=1), 0)
        title, url, options = module.windows[0]
        self.assertEqual(title, "revit-journal-tool")
        self.assertTrue(title.isascii())
        # The window icon is a real .ico file while the window is open.
        self.assertTrue(module.start_options["icon"].endswith("icon.ico"))
        self.assertTrue(module.icon_exists)
        self.assertEqual(seen["icon"], ("image/png", bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])))
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/\?desktop=1$")
        self.assertTrue(options["text_select"])
        self.assertIsInstance(options["js_api"], desktop.DesktopApi)
        self.assertFalse(module.start_options["private_mode"])  # remembers layout and theme
        self.assertIn('id="desktop-actions"', seen["page"])
        self.assertEqual([item["name"] for item in seen["files"]], ["journal.0001.txt"])
        self.assertEqual([(item["start_line"], item["end_line"]) for item in seen["blocks"]], [(16, 19)])
        with self.assertRaises(OSError):  # the server stopped with the window
            OPENER.open(url, timeout=2)

    def test_window_claims_its_own_taskbar_identity_before_it_exists(self):
        module = self.fake_webview(lambda url: None)
        order, create_window = [], module.create_window
        module.create_window = lambda *args, **options: order.append("window") or create_window(*args, **options)
        with patch.dict(sys.modules, {"webview": module}),                 patch.dict(os.environ, {"REVIT_JOURNAL_CACHE": str(self.root / "cache")}),                 patch("revit_journal_tools.desktop._claim_taskbar_identity",
                      side_effect=lambda: order.append("app-id")) as claimed:
            self.assertEqual(desktop.run([], self.root / "desktop.sqlite3", jobs=1), 0)
        self.assertEqual(claimed.call_count, 1)
        self.assertEqual(order, ["app-id", "window"])

    @unittest.skipUnless(sys.platform == "win32", "Windows taskbar only")
    def test_taskbar_identity_is_the_process_app_user_model_id(self):
        import ctypes
        from ctypes import wintypes

        self.assertEqual(desktop._claim_taskbar_identity(), desktop.APP_ID)
        self.assertTrue(desktop.APP_ID.isascii() and " " not in desktop.APP_ID and len(desktop.APP_ID) < 128)
        current = ctypes.c_wchar_p()
        self.assertEqual(ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(current)), 0)
        self.assertEqual(current.value, desktop.APP_ID)
        ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(current, wintypes.LPVOID))

    def test_taken_port_falls_back_to_a_free_one(self):
        workspace = desktop.Workspace(self.root / "desktop.sqlite3", jobs=1)
        self.addCleanup(workspace.close)
        first = desktop._server(workspace.index)
        self.addCleanup(first.server_close)
        second = desktop._server(workspace.index)
        self.addCleanup(second.server_close)
        self.assertNotEqual(first.server_port, second.server_port)

    def test_without_pywebview_the_error_says_how_to_install_it(self):
        with patch.dict(sys.modules, {"webview": None}), self.assertRaises(ValueError) as raised:
            desktop.run([], self.root / "desktop.sqlite3")
        self.assertIn("revit-journal-tools[desktop]", str(raised.exception))


class CommandTests(DesktopTestCase):
    def test_gui_command_plans_inputs_and_uses_its_own_cache_database(self):
        with patch.dict(os.environ, {"REVIT_JOURNAL_CACHE": str(self.root / "cache")}), \
                patch("revit_journal_tools.desktop.run", return_value=0) as run:
            self.assertEqual(cli.main(["gui", str(self.first)]), 0)
        planned, database, encoding = run.call_args.args
        self.assertEqual([item.path for item in planned], [self.first.resolve()])
        self.assertEqual((database, encoding), (self.root / "cache" / "desktop.sqlite3", "auto"))

    def test_gui_without_paths_or_local_journals_opens_an_empty_window(self):
        with patch("revit_journal_tools.cli.journal_folders", return_value=[]), \
                patch("revit_journal_tools.desktop.run", return_value=0) as run:
            self.assertEqual(cli.main(["gui", "--database", str(self.root / "d.sqlite3")]), 0)
        self.assertEqual(run.call_args.args[0], [])

    def test_windowed_entry_point_shows_errors_without_a_console(self):
        with patch.object(sys, "stderr", None), patch("revit_journal_tools.desktop._show_error") as shown:
            self.assertEqual(desktop.main(["--encoding", "no-such-codec", str(self.first)]), 2)
        self.assertIn("no-such-codec", shown.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
