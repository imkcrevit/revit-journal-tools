"""Persistent full-line index. Pagination never depends on retained scan findings."""

import json
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .categories import CATEGORIES, Categorizer
from .blocks import BLOCK_VERSION, BlockBuilder
from .loading import BLOCK_FILTERS, block_filter, matches_filter
from .reader import JournalReader
from .rules import load_rules, ruleset_digest

INDEX_VERSION = "viewer-2:" + BLOCK_VERSION
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
CREATE TABLE IF NOT EXISTS blocks (
 file_id INTEGER NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 category TEXT NOT NULL, severity TEXT NOT NULL, rules TEXT NOT NULL,
 title TEXT NOT NULL, title_line INTEGER NOT NULL, boundary TEXT NOT NULL,
 complete INTEGER NOT NULL, addin_origin TEXT NOT NULL DEFAULT '',
 addin_evidence TEXT NOT NULL DEFAULT '[]', addin_name TEXT NOT NULL DEFAULT '',
 addin_class TEXT NOT NULL DEFAULT '', addin_identity_truncated INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(file_id,start_line)
);
CREATE INDEX IF NOT EXISTS blocks_category ON blocks(file_id,category,start_line);
CREATE TABLE IF NOT EXISTS block_stats (
 file_id INTEGER PRIMARY KEY, block_count INTEGER NOT NULL, counts TEXT NOT NULL
);
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
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(blocks)")}
            if "addin_origin" not in columns:
                connection.execute("ALTER TABLE blocks ADD COLUMN addin_origin TEXT NOT NULL DEFAULT ''")
            if "addin_evidence" not in columns:
                connection.execute("ALTER TABLE blocks ADD COLUMN addin_evidence TEXT NOT NULL DEFAULT '[]'")
            for name in ("addin_name", "addin_class"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE blocks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            if "addin_identity_truncated" not in columns:
                connection.execute("ALTER TABLE blocks ADD COLUMN addin_identity_truncated INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE INDEX IF NOT EXISTS blocks_addin ON blocks(file_id,addin_origin,start_line)")
            connection.execute("CREATE INDEX IF NOT EXISTS blocks_addin_names ON blocks(file_id,addin_name COLLATE NOCASE) WHERE addin_origin<>''")
            connection.execute("CREATE INDEX IF NOT EXISTS blocks_addin_classes ON blocks(file_id,addin_class COLLATE NOCASE) WHERE addin_origin<>''")

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
            connection.execute("DELETE FROM blocks WHERE file_id=?", (file_id,))
            connection.execute("DELETE FROM block_stats WHERE file_id=?", (file_id,))
            connection.execute("UPDATE files SET status='indexing',line_count=0,indexed_bytes=0,counts='{}',error=NULL WHERE id=?", (file_id,))
            connection.commit()
            counts: Counter[str] = Counter()
            categorizer = Categorizer(self.rules)
            builder = BlockBuilder()
            block_counts: Counter[str] = Counter()
            block_batch = []
            batch = []
            with JournalReader(path, encoding) as reader:
                for line in reader:
                    category, severity, rules = categorizer.classify(line.text)
                    counts[category] += 1
                    batch.append((file_id, line.number, line.text, category, severity, rules))
                    block_batch.extend(builder.feed(line, (category, severity, rules)))
                    if len(batch) >= 5000:
                        self._write_blocks(connection, file_id, block_batch, block_counts)
                        self._write_batch(connection, file_id, batch, counts, reader)
                        batch.clear()
                        block_batch.clear()
                        if stop and stop.is_set():
                            connection.execute("UPDATE files SET status='queued' WHERE id=?", (file_id,))
                            return
                block_batch.extend(builder.finish())
                self._write_blocks(connection, file_id, block_batch, block_counts)
                self._write_batch(connection, file_id, batch, counts, reader)
                if _stamp(path) != row["stamp"]:
                    raise ValueError("源文件自任务创建后发生变化，请重新索引")
                connection.execute(
                    "UPDATE files SET status='ready',encoding=?,sha256=?,line_count=?,indexed_bytes=?,counts=? WHERE id=?",
                    (reader.encoding, reader.sha256, reader.line_count, reader.byte_count, json.dumps(counts), file_id),
                )

    @staticmethod
    def _write_blocks(connection, file_id, batch, counts):
        for block in batch:
            counts[block.category] += 1
            if block.addin_origin:
                counts["addin_" + block.addin_origin] += 1
                counts["addin"] += 1
        connection.executemany(
            "INSERT INTO blocks(file_id,start_line,end_line,category,severity,rules,title,title_line,boundary,complete,addin_origin,addin_evidence,addin_name,addin_class,addin_identity_truncated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(file_id, b.start_line, b.end_line, b.category, b.severity, ",".join(sorted(b.rules)),
              b.title, b.title_line, b.boundary, int(b.complete), b.addin_origin,
              json.dumps(b.addin_evidence, ensure_ascii=False), b.addin_name, b.addin_class,
              int(b.addin_identity_truncated)) for b in batch],
        )
        connection.execute(
            "INSERT OR REPLACE INTO block_stats VALUES(?,?,?)",
            (file_id, sum(counts[key] for key in CATEGORIES), json.dumps(counts)),
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
            rows = connection.execute(
                "SELECT f.*,coalesce(b.block_count,0) AS block_count,coalesce(b.counts,'{}') AS block_counts "
                "FROM files f LEFT JOIN block_stats b ON f.id=b.file_id ORDER BY f.path"
            ).fetchall()
        allowed = set(self.file_ids)
        result = []
        for row in rows:
            if row["id"] not in allowed:
                continue
            item = dict(row)
            item["counts"] = json.loads(item["counts"])
            item["block_counts"] = json.loads(item["block_counts"])
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

    @staticmethod
    def _block_conditions(file_id, category, after, addin_prefix):
        where, values = ["b.file_id=?", "b.start_line>?"], [file_id, after]
        if category != "all":
            condition, value = block_filter(category)
            where.append(condition)
            values.append(value)
        if addin_prefix:
            pattern = addin_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.extend(["b.addin_origin<>''", "(b.addin_name LIKE ? ESCAPE '\\' OR b.addin_class LIKE ? ESCAPE '\\')"])
            values.extend([pattern, pattern])
        return where, values

    def blocks(self, file_id: int, category: str = "all", query: str = "", after: int = 0, limit: int = 50, *, addin_prefix: str = "") -> dict:
        """Page by entire event; FTS hits anywhere inside it select the event once."""
        if category != "all" and category not in BLOCK_FILTERS:
            raise ValueError("未知分类")
        if not 1 <= limit <= 200 or after < 0 or len(query) > 256 or len(addin_prefix) > 256:
            raise ValueError("分页或搜索参数超出范围")
        addin_prefix = addin_prefix.strip()
        where, values = self._block_conditions(file_id, category, after, addin_prefix)
        with self.connect() as connection:
            self._ready(connection, file_id)
            if query.strip():
                phrase = '"' + query.strip().replace('"', '""') + '"*'
                rows = self._search_blocks(connection, file_id, category, phrase, after, limit + 1, addin_prefix)
            else:
                rows = connection.execute("SELECT b.* FROM blocks b WHERE " + " AND ".join(where)
                                          + " ORDER BY b.start_line LIMIT ?", [*values, limit + 1]).fetchall()
        items = [self._block_dict(row) for row in rows[:limit]]
        return {"items": items, "has_more": len(rows) > limit,
                "next_after": items[-1]["start_line"] if items else after}

    @staticmethod
    def _search_blocks(connection, file_id, category, phrase, after, limit, addin_prefix=""):
        condition, value = block_filter(category)
        first = connection.execute("SELECT id FROM lines WHERE file_id=? AND line_number>? "
                                   "ORDER BY line_number LIMIT 1", (file_id, after)).fetchone()
        if first is None:
            return []
        last_id = connection.execute("SELECT id FROM lines WHERE file_id=? ORDER BY line_number DESC LIMIT 1", (file_id,)).fetchone()[0]
        if addin_prefix:
            where, values = ViewerIndex._block_conditions(file_id, category, after, addin_prefix)
            return connection.execute(
                "SELECT b.* FROM blocks b WHERE " + " AND ".join(where)
                + " AND EXISTS (SELECT 1 FROM lines l INDEXED BY lines_position WHERE l.file_id=b.file_id "
                "AND l.line_number BETWEEN b.start_line AND b.end_line AND l.id IN "
                "(SELECT rowid FROM line_search WHERE line_search MATCH ? AND rowid>=? AND rowid<=?)) "
                "ORDER BY b.start_line LIMIT ?", [*values, phrase, first[0], last_id, limit],
            ).fetchall()
        cursor, result, previous = first[0] - 1, [], None
        while True:
            # Force FTS to drive the join. The opposite plan scanned millions
            # of source lines even when only nine lines matched the term.
            hits = connection.execute(
                "SELECT l.id,l.line_number FROM line_search s CROSS JOIN lines l ON l.id=s.rowid "
                "WHERE line_search MATCH ? AND s.rowid>? AND s.rowid<=? AND l.file_id=? "
                "ORDER BY s.rowid LIMIT 512", (phrase, cursor, last_id, file_id),
            ).fetchall()
            if category != "all" and cursor == first[0] - 1 and len(hits) == 512:
                stats = connection.execute("SELECT counts FROM block_stats WHERE file_id=?", (file_id,)).fetchone()
                if stats and json.loads(stats[0]).get(category, 0) <= 5000:
                    # For a common word and a small category, test only those
                    # blocks instead of mapping every matching ordinary line.
                    return connection.execute(
                        f"SELECT b.* FROM blocks b WHERE b.file_id=? AND {condition} AND b.start_line>? "
                        "AND EXISTS (SELECT 1 FROM lines l INDEXED BY lines_position WHERE l.file_id=b.file_id "
                        "AND l.line_number BETWEEN b.start_line AND b.end_line AND l.id IN "
                        "(SELECT rowid FROM line_search WHERE line_search MATCH ? AND rowid>? AND rowid<=?)) "
                        "ORDER BY b.start_line LIMIT ?", (file_id, value, after, phrase, cursor, last_id, limit),
                    ).fetchall()
            for hit in hits:
                if previous is not None and previous["start_line"] <= hit["line_number"] <= previous["end_line"]:
                    continue
                previous = connection.execute(
                    "SELECT * FROM blocks WHERE file_id=? AND start_line<=? ORDER BY start_line DESC LIMIT 1",
                    (file_id, hit["line_number"]),
                ).fetchone()
                if previous and previous["start_line"] > after and matches_filter(previous, category):
                    result.append(previous)
                    if len(result) >= limit:
                        return result
            if len(hits) < 512:
                return result
            # Skip the rest of the last mapped block, even if it contains
            # millions of repetitions; it has already been accepted/rejected.
            cursor = connection.execute("SELECT id FROM lines WHERE file_id=? AND line_number=?",
                                        (file_id, previous["end_line"])).fetchone()[0]

    @staticmethod
    def _block_dict(row) -> dict:
        result = dict(row)
        result["addin_evidence"] = json.loads(result["addin_evidence"])
        return result

    def block_at(self, file_id: int, line: int) -> dict:
        with self.connect() as connection:
            file = self._ready(connection, file_id)
            if not 1 <= line <= file["line_count"]:
                raise ValueError("行号超过文件范围")
            row = connection.execute("SELECT * FROM blocks WHERE file_id=? AND start_line<=? "
                                     "ORDER BY start_line DESC LIMIT 1", (file_id, line)).fetchone()
        if row is None or row["end_line"] < line:
            raise ValueError("段落索引不完整，请重新索引")
        return self._block_dict(row)

    def block_lines(self, file_id: int, start_line: int, offset: int = 0, limit: int = 50) -> dict:
        """Bound detail reads so expanding an enormous paragraph stays cheap."""
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("段落分页参数超出范围")
        block = self.block_at(file_id, start_line)
        if block["start_line"] != start_line:
            raise ValueError("请提供段落起始行")
        begin = start_line + offset
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT line_number,substr(body,1,16384) AS text,length(body) AS text_length "
                "FROM lines WHERE file_id=? AND line_number BETWEEN ? AND ? ORDER BY line_number",
                (file_id, begin, min(block["end_line"], begin + limit - 1)),
            ).fetchall()
        return {"block": block, "items": [dict(row) for row in rows],
                "has_more": begin + limit <= block["end_line"]}
