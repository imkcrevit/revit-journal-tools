"""Loopback-only viewer with a read-only HTTP API and background local indexing."""

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .categories import CATEGORIES
from .viewer_index import ViewerIndex

_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


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
            except (BrokenPipeError, ConnectionResetError):
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
            try:
                if route.path == "/api/files":
                    result = {"files": index.catalog(), "categories": CATEGORIES}
                elif route.path == "/api/events":
                    result = index.events(
                        int(value("file", "0")), value("category", "all"),
                        value("q", ""), int(value("after", "0")), int(value("limit", "100")),
                    )
                elif route.path == "/api/context":
                    result = index.context(int(value("file", "0")), int(value("line", "1")), int(value("radius", "8")))
                else:
                    self.respond(404, {"error": "资源不存在"})
                    return
                result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                self.respond(200, result)
            except KeyError as error:
                self.respond(404, {"error": str(error)})
            except (ValueError, sqlite3.Error) as error:
                self.respond(400, {"error": str(error)})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(paths: list[Path], database: Path, encoding: str, port: int) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在1到65535之间")
    index = ViewerIndex(database)
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
