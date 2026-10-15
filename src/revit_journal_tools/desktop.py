"""Desktop window: the Web viewer's page in an embedded WebView2 (Edge) window.

pywebview hosts the page (pip install "revit-journal-tools[desktop]"); the page
reads everything from the loopback viewer of `view`: the same index, queries
and session summary. The window adds opening local files and folders through
system dialogs. Sources are only read.
"""

import io
import sqlite3
import sys
import threading
from contextlib import nullcontext, redirect_stderr
from importlib.resources import as_file, files
from pathlib import Path

from .cli import main as cli_main, plan_workspace
from .discovery import cache_dir
from .i18n import error_text
from .viewer_index import ViewerIndex
from .viewer_server import make_server

TITLE = "revit-journal-tool"
# A stable origin keeps the page's layout and theme (localStorage) between
# runs; another free port is used when it is taken.
PORT = 8790
JOURNAL_TYPES = {"zh": ("Journal与日志 (*.txt;*.log;*.journal;*.abbrev)", "所有文件 (*.*)"),
                 "en": ("Journals and logs (*.txt;*.log;*.journal;*.abbrev)", "All files (*.*)")}
INSTALL_HINT = ("桌面窗口需要pywebview：py -m pip install \"revit-journal-tools[desktop]\"（或py -m pip install pywebview）；"
                "也可以用revit-journal view在浏览器中查看")


class Workspace:
    """The index behind the window and its background build. Opening other
    inputs stops the running build first, so two builds never write the same rows."""

    def __init__(self, database: Path, encoding: str = "auto", jobs: int | None = None,
                 snapshot: str = "auto", store: str = "offsets", years=()):
        self.index = ViewerIndex(database, workers=jobs, snapshot=snapshot, store=store)
        self.encoding, self.years = encoding, list(years)
        self.stop = threading.Event()
        self.worker: threading.Thread | None = None
        self.failures: list[str] = []
        self._lock = threading.Lock()

    @property
    def building(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def open(self, planned: list) -> None:
        with self._lock:
            self.stop.set()
            if self.worker is not None:
                self.worker.join()
            queued = self.index.register(planned, self.encoding)
            stop, failures = threading.Event(), []

            def build():
                try:
                    self.index.build(queued, self.encoding, stop)
                except Exception as error:  # noqa: BLE001 - shown by the page
                    failures.append(str(error))

            self.stop, self.failures = stop, failures
            self.worker = threading.Thread(target=build, daemon=True)
            self.worker.start()

    def open_paths(self, paths: list[Path], recursive: bool = False, lang: str = "zh") -> dict:
        """Files and folders (none: every local Revit Journals folder of the
        --revit years). Unreadable paths are listed; the others still open."""
        try:
            planned, errors, _ = plan_workspace(paths, recursive, [] if paths else self.years)
            if planned:
                self.open(planned)
        except (OSError, ValueError, LookupError, sqlite3.Error) as error:
            return {"error": error_text(str(error), lang)}
        return {"opened": len(planned),
                "errors": [f"{item['source']}: {error_text(item['error'], lang)}" for item in errors]}

    def close(self, timeout: float = 5) -> None:
        self.stop.set()
        if self.worker is not None:
            self.worker.join(timeout)


class DesktopApi:
    """window.pywebview.api of the page; pywebview runs each call on its own
    thread. Only system dialogs and opening inputs: reading stays on HTTP."""

    def __init__(self, workspace: Workspace, dialogs):
        """dialogs: webview.FileDialog (OPEN, FOLDER)."""
        # Private: pywebview exposes public attributes to the page.
        self._workspace, self._dialogs = workspace, dialogs
        self._window = None

    def _dialog(self, kind: str, **options) -> list[Path] | None:
        names = self._window.create_file_dialog(getattr(self._dialogs, kind), **options)
        if not names:
            return None
        return [Path(names)] if isinstance(names, str) else [Path(name) for name in names]

    def open_files(self, lang: str = "zh") -> dict:
        types = JOURNAL_TYPES.get(lang, JOURNAL_TYPES["zh"])
        paths = self._dialog("OPEN", allow_multiple=True, file_types=types)
        return self._workspace.open_paths(paths, lang=lang) if paths else {"cancelled": True}

    def open_folder(self, recursive: bool = True, lang: str = "zh") -> dict:
        paths = self._dialog("FOLDER")
        return self._workspace.open_paths(paths[:1], bool(recursive), lang) if paths else {"cancelled": True}

    def open_local(self, lang: str = "zh") -> dict:
        return self._workspace.open_paths([], lang=lang)


def _window_size() -> tuple[int, int]:
    """1440x900, or less on a smaller screen: most of the Windows work area."""
    width, height = 1440, 900
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        area = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(area), 0):  # SPI_GETWORKAREA
            width = min(width, int((area.right - area.left) * 0.94))
            height = min(height, int((area.bottom - area.top) * 0.94))
    return max(width, 960), max(height, 600)


def _server(index: ViewerIndex):
    try:
        return make_server(index, PORT)
    except OSError:
        return make_server(index, 0)


def run(planned: list, database: Path, encoding: str = "auto", *, jobs: int | None = None,
        snapshot: str = "auto", store: str = "offsets", years=()) -> int:
    try:
        import webview
    except ImportError as error:
        raise ValueError(INSTALL_HINT) from error
    workspace = Workspace(database, encoding, jobs, snapshot, store, years)
    server = _server(workspace.index)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    api = DesktopApi(workspace, webview.FileDialog)
    try:
        if planned:
            workspace.open(planned)
        width, height = _window_size()
        api._window = webview.create_window(
            TITLE, f"http://127.0.0.1:{server.server_port}/?desktop=1", js_api=api, width=width, height=height,
            min_size=(960, 600), text_select=True, background_color="#f5f6f8")
        # The icon is package data, also inside the single-file .pyz: a real file while the window is open.
        with as_file(files("revit_journal_tools").joinpath("web", "icon.ico")) as icon:
            try:
                webview.start(gui="edgechromium" if sys.platform == "win32" else None, private_mode=False,
                              storage_path=str(cache_dir() / "webview"), icon=str(icon))
            except Exception as error:  # noqa: BLE001 - e.g. no WebView2 runtime
                raise ValueError(f"无法打开WebView2窗口（{error}）；可以用revit-journal view在浏览器中查看") from error
    finally:
        server.shutdown()
        server.server_close()
        workspace.close()
    return 0


def _show_error(message: str) -> None:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)  # MB_ICONERROR


def main(argv: list[str] | None = None) -> int:
    """Windowed entry point (revit-journal-gui) with the options of `revit-journal
    gui`. Without a console (pythonw) an error is shown in a message box."""
    captured = io.StringIO()
    with redirect_stderr(captured) if sys.stderr is None else nullcontext():
        try:
            code = cli_main(["gui", *(sys.argv[1:] if argv is None else argv)])
        except SystemExit as stop:  # argparse usage errors
            code = stop.code if isinstance(stop.code, int) else 2
    message = captured.getvalue().strip()
    if code and message:
        _show_error(message)
    return code
