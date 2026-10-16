"""Loopback-only viewer with a read-only HTTP API and background local indexing."""

import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .blocks import PHASES
from .categories import CATEGORIES
from .i18n import check_lang, error_text
from .loading import BLOCK_FILTERS, block_label, block_summary
from .session_view import line_ref, main_tail, session_label, summary_lines
from .summary import session_findings
from .viewer_index import ViewerIndex

_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icon.png": ("icon.png", "image/png"),
}


def session_view(index: ViewerIndex, file_id: int, lang: str = "zh") -> dict:
    """The session a file belongs to, rendered by the same code as `summary`
    and the terminal summary panel. refs: the main_id line each summary line
    names (None where it names none)."""
    catalog = index.catalog()
    session = next((item for item in index.sessions(catalog) if file_id in item["files"]), None)
    if session is None:
        raise KeyError("文件未在当前工作区中")
    main = next((item for item in catalog if item["path"] == session.get("main")), None)
    if lang != "zh" and session.get("end_state"):
        session = {**session, "session_findings": session_findings(
            session["summary"], session["live"], session["sidecar_logs"], session["main"], lang)}
    lines = summary_lines(session, main_tail(index, session, catalog, 10), lang)
    return {"label": session_label(session, {item["id"]: item for item in catalog}, lang),
            "main_id": main["id"] if main else None, "end_state": session.get("end_state"),
            "end_state_label": session.get("end_state_label"),
            "finding_count": len(session.get("session_findings") or []),
            "lines": lines, "refs": [line_ref(line, lang) if main else None for line in lines]}


# The page lists files every few seconds: only the summary fields it shows.
_FILE_SUMMARY = ("start_time", "end_time", "build", "branch", "release")


def _listed(file: dict) -> dict:
    return {**file, "summary": {key: file["summary"].get(key) for key in _FILE_SUMMARY}}


def _labelled(block: dict) -> dict:
    """List text as in the terminal: category (and add-in origin) label and summary."""
    return {**block, "label": block_label(block), "summary": block_summary(block)}


def _kinds(text: str) -> tuple[str, ...]:
    """kinds=service,server: add-in record kinds (ViewerIndex.blocks checks them); empty: every kind."""
    return tuple(item.strip() for item in text.split(",") if item.strip())


def _flag(text: str) -> bool:
    if text not in ("0", "1"):
        raise ValueError("fold必须为0或1")
    return text == "1"


def make_server(index: ViewerIndex, port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, status, data, content_type="application/json; charset=utf-8"):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except ConnectionError:  # the page went away mid-request
                pass

        def do_GET(self):
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            if host not in {"localhost", "127.0.0.1", "::1"}:
                self.respond(403, {"error": "仅允许本地访问"})
                return
            route = urlsplit(self.path)
            if route.path in _ASSETS:
                name, mime = _ASSETS[route.path]
                self.respond(200, files("revit_journal_tools").joinpath("web", name).read_bytes(), mime)
                return
            params = parse_qs(route.query)

            def value(name, default):
                return params.get(name, [default])[0]

            started = time.perf_counter()
            lang = value("lang", "zh")
            try:
                check_lang(lang)
                file_id = int(value("file", "0"))
                if route.path == "/api/files":
                    result = {"files": [_listed(item) for item in index.catalog()], "categories": CATEGORIES,
                              "block_filters": BLOCK_FILTERS, "phases": PHASES}
                elif route.path == "/api/events":
                    result = index.events(file_id, value("category", "all"), value("q", ""),
                                          int(value("after", "0")), int(value("limit", "100")))
                elif route.path == "/api/context":
                    result = index.context(file_id, int(value("line", "1")), int(value("radius", "8")))
                elif route.path == "/api/session":
                    result = session_view(index, file_id, lang)
                elif route.path == "/api/blocks":
                    result = index.blocks(file_id, value("category", "all"), value("q", ""),
                                          int(value("after", "0")), int(value("limit", "100")),
                                          phase=value("phase", "all"), fold=_flag(value("fold", "1")),
                                          kinds=_kinds(value("kinds", "")))
                    result["items"] = [_labelled(item) for item in result["items"]]
                elif route.path == "/api/block":
                    result = index.block_lines(file_id, int(value("start", "1")), int(value("offset", "0")),
                                               int(value("limit", "200")))
                    result["block"] = _labelled(result["block"])
                    # Same provenance fields as /api/context (hash, actual encoding, degraded lines).
                    source = index.context(file_id, result["block"]["start_line"], 0)
                    result.update({key: item for key, item in source.items() if key not in ("line", "items")})
                elif route.path == "/api/block_at":
                    result = _labelled(index.block_at(file_id, int(value("line", "1"))))
                elif route.path == "/api/fold":
                    fingerprint = value("fingerprint", "")
                    if not 1 <= len(fingerprint) <= 64:
                        raise ValueError("折叠指纹无效")
                    result = index.fold_members(file_id, fingerprint)
                elif route.path == "/api/search":
                    # Every ready file of the workspace; rows as in /api/events.
                    result = index.search_workspace(value("q", ""), value("category", "all"),
                                                    int(value("per_file", "3")))
                else:
                    self.respond(404, {"error": error_text("资源不存在", lang)})
                    return
                if "hint" in result:  # A search hint (viewer_index.CJK_HINT) in the page's language.
                    result["hint"] = error_text(result["hint"], lang)
                result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                self.respond(200, result)
            except KeyError as error:
                self.respond(404, {"error": error_text(error.args[0] if error.args else str(error), lang)})
            except (ValueError, sqlite3.Error) as error:
                self.respond(400, {"error": error_text(str(error), lang)})

    return _Server(("127.0.0.1", port), Handler)


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port that is in use
    # and take its requests; there a taken port must be an error.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def serve(paths: list[Path], database: Path, encoding: str, port: int, jobs: int | None = None,
          snapshot: str = "auto", store: str = "offsets") -> None:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在1到65535之间")
    index = ViewerIndex(database, workers=jobs, snapshot=snapshot, store=store)
    queued = index.register(paths, encoding)
    server = make_server(index, port)
    stop = threading.Event()
    worker = threading.Thread(target=index.build, args=(queued, encoding, stop), daemon=True)
    worker.start()
    print(f"本地Journal查看器：http://127.0.0.1:{port}", flush=True)
    print(f"文件：{len(paths)}；待索引：{len(queued)}；缓存：{index.database}", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
        worker.join(timeout=5)
