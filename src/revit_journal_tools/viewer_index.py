"""Persistent full-line index. Pagination never depends on retained scan findings."""

import json
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .categories import CATEGORIES, Categorizer
from .reader import JournalReader
from .rules import load_rules, ruleset_digest

INDEX_VERSION = "viewer-1"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
 id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 size INTEGER NOT NULL, stamp TEXT NOT NULL, signature TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', encoding TEXT, sha256 TEXT,
 line_count INTEGER NOT NULL DEFAULT 0, indexed_bytes INTEGER NOT NULL DEFAULT 0,
 counts TEXT NOT NULL DEFAULT '{}', error TEXT
);
CREATE TABLE IF NOT EXISTS lines (
 id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, line_number INTEGER NOT NULL,
 body TEXT NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL, rules TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS lines_position ON lines(file_id, line_number);
CREATE INDEX IF NOT EXISTS lines_category ON lines(file_id, category, line_number);
CREATE VIRTUAL TABLE IF NOT EXISTS line_search USING fts5(
 body, content='lines', content_rowid='id', columnsize=0
);
CREATE TRIGGER IF NOT EXISTS lines_insert AFTER INSERT ON lines BEGIN
 INSERT INTO line_search(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS lines_delete AFTER DELETE ON lines BEGIN
 INSERT INTO line_search(line_search, rowid, body) VALUES ('delete', old.id, old.body);
END;
"""


def _stamp(path: Path) -> str:
    info = path.stat()
    return json.dumps([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])


def _kind(path: Path) -> str:
    if path.name.lower().endswith(".txt.abbrev"):
        return "精简Journal"
    if path.suffix.lower() == ".log":
        return "Worker"
    return "Journal"


class ViewerIndex:
    def __init__(self, database: str | Path):
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.rules = load_rules()
        self.signature = INDEX_VERSION + ":" + ruleset_digest(self.rules)
        self.file_ids: list[int] = []
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=15)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA cache_size=-16384")
            with connection:
                yield connection
        finally:
            connection.close()

    def register(self, paths: list[Path], encoding: str) -> list[int]:
        queued = []
        self.file_ids = []
        signature = self.signature + ":" + encoding
        with self.connect() as connection:
            for path in sorted(set(path.resolve() for path in paths), key=lambda item: item.stat().st_size):
                if path == self.database:
                    raise ValueError("索引数据库不能覆盖输入文件")
                stamp = _stamp(path)
                previous = connection.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
                if previous is None:
                    cursor = connection.execute(
                        "INSERT INTO files(path,name,size,stamp,signature) VALUES(?,?,?,?,?)",
                        (str(path), path.name, path.stat().st_size, stamp, signature),
                    )
                    file_id = cursor.lastrowid
                    queued.append(file_id)
                else:
                    file_id = previous["id"]
                    if previous["status"] != "ready" or previous["stamp"] != stamp or previous["signature"] != signature:
                        connection.execute(
                            "UPDATE files SET status='queued',stamp=?,size=?,signature=?,error=NULL WHERE id=?",
                            (stamp, path.stat().st_size, signature, file_id),
                        )
                        queued.append(file_id)
                self.file_ids.append(file_id)
        return queued

    def build(self, file_ids: list[int], encoding: str, stop: threading.Event | None = None) -> None:
        for file_id in file_ids:
            if stop and stop.is_set():
                return
            try:
                self._build_file(file_id, encoding, stop)
            except (OSError, ValueError, LookupError, sqlite3.Error) as error:
                with self.connect() as connection:
                    connection.execute("UPDATE files SET status='error',error=? WHERE id=?", (str(error), file_id))

    def _build_file(self, file_id: int, encoding: str, stop: threading.Event | None) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            path = Path(row["path"])
            connection.execute("DELETE FROM lines WHERE file_id=?", (file_id,))
            connection.execute("UPDATE files SET status='indexing',line_count=0,indexed_bytes=0,counts='{}',error=NULL WHERE id=?", (file_id,))
            connection.commit()
            counts: Counter[str] = Counter()
            categorizer = Categorizer(self.rules)
            batch = []
            with JournalReader(path, encoding) as reader:
                for line in reader:
                    category, severity, rules = categorizer.classify(line.text)
                    counts[category] += 1
                    batch.append((file_id, line.number, line.text, category, severity, rules))
                    if len(batch) >= 5000:
                        self._write_batch(connection, file_id, batch, counts, reader)
                        batch.clear()
                        if stop and stop.is_set():
                            connection.execute("UPDATE files SET status='queued' WHERE id=?", (file_id,))
                            return
                self._write_batch(connection, file_id, batch, counts, reader)
                if _stamp(path) != row["stamp"]:
                    raise ValueError("源文件自任务创建后发生变化，请重新索引")
                connection.execute(
                    "UPDATE files SET status='ready',encoding=?,sha256=?,line_count=?,indexed_bytes=?,counts=? WHERE id=?",
                    (reader.encoding, reader.sha256, reader.line_count, reader.byte_count, json.dumps(counts), file_id),
                )

    @staticmethod
    def _write_batch(connection, file_id, batch, counts, reader):
        connection.executemany("INSERT INTO lines(file_id,line_number,body,category,severity,rules) VALUES(?,?,?,?,?,?)", batch)
        connection.execute(
            "UPDATE files SET line_count=?,indexed_bytes=?,counts=? WHERE id=?",
            (reader.line_count, reader.byte_count, json.dumps(counts), file_id),
        )
        connection.commit()

    def catalog(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM files ORDER BY path").fetchall()
        allowed = set(self.file_ids)
        result = []
        for row in rows:
            if row["id"] not in allowed:
                continue
            item = dict(row)
            item["counts"] = json.loads(item["counts"])
            item["kind"] = _kind(Path(item["path"]))
            try:
                item["source_changed"] = _stamp(Path(item["path"])) != item["stamp"]
            except OSError:
                item["source_changed"] = True
            result.append(item)
        return result

    def _ready(self, connection, file_id):
        if file_id not in self.file_ids:
            raise KeyError("文件未在当前工作区中")
        row = connection.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row["status"] != "ready":
            raise ValueError("文件尚未完成索引，请查看文件列表中的进度或错误")
        return row

    def events(self, file_id: int, category: str = "all", query: str = "", after: int = 0, limit: int = 100) -> dict:
        if category != "all" and category not in CATEGORIES:
            raise ValueError("未知分类")
        if not 1 <= limit <= 200 or after < 0 or len(query) > 256:
            raise ValueError("分页或搜索参数超出范围")
        where = ["l.file_id=?", "l.line_number>?"]
        values: list = [file_id, after]
        if category != "all":
            where.append("l.category=?")
            values.append(category)
        if query.strip():
            # Treat input as one literal token phrase, with a prefix on the last token.
            phrase = '"' + query.strip().replace('"', '""') + '"*'
            where.append("l.id IN (SELECT rowid FROM line_search WHERE line_search MATCH ?)")
            values.append(phrase)
        with self.connect() as connection:
            self._ready(connection, file_id)
            rows = connection.execute(
                "SELECT l.line_number,l.category,l.severity,l.rules,substr(l.body,1,700) AS text,length(l.body) AS text_length FROM lines l WHERE "
                + " AND ".join(where) + " ORDER BY l.line_number LIMIT ?",
                [*values, limit + 1],
            ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {"items": items, "has_more": len(rows) > limit, "next_after": items[-1]["line_number"] if items else after}

    def context(self, file_id: int, line: int, radius: int = 8) -> dict:
        if line < 1 or not 0 <= radius <= 50:
            raise ValueError("行号或上下文范围无效")
        with self.connect() as connection:
            file = self._ready(connection, file_id)
            if line > file["line_count"]:
                raise ValueError("行号超过文件范围")
            rows = connection.execute(
                "SELECT line_number,category,severity,rules,substr(body,1,16384) AS text,length(body) AS text_length FROM lines WHERE file_id=? AND line_number BETWEEN ? AND ? ORDER BY line_number",
                (file_id, max(1, line - radius), line + radius),
            ).fetchall()
        return {"line": line, "sha256": file["sha256"], "encoding": file["encoding"], "source": file["path"], "items": [dict(row) for row in rows]}
