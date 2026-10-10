"""Persistent full-line index. Pagination never depends on retained scan findings."""

import json
import multiprocessing
import os
import shutil
import signal
import sqlite3
import tempfile
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from .categories import CATEGORIES, Categorizer
from .blocks import BLOCK_VERSION, BlockBuilder
from .loading import BLOCK_FILTERS, block_filter, matches_filter
from .reader import JournalReader, resolve_encoding
from .rules import load_rules, ruleset_digest
from .sessions import ROLE_LABELS, InputFile, describe, plan_inputs, sort_key

INDEX_VERSION = "viewer-3:" + BLOCK_VERSION
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
 body TEXT NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL, rules TEXT NOT NULL,
 degraded INTEGER NOT NULL DEFAULT 0
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
DROP TRIGGER IF EXISTS lines_insert;
DROP TRIGGER IF EXISTS lines_delete;
"""
# Full-text rows are maintained per file, not per line: a file's lines and its
# FTS rows are inserted in the same transaction (one per file) and deleted
# together, so every row in `lines` always has its FTS entry. Earlier versions
# kept the same invariant with per-row triggers, which cost ~80% of the
# SQLite time; they are dropped on open.
_BATCH = 20_000


def _stamp(path: Path) -> str:
    info = path.stat()
    return json.dumps([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])


_INSERT_LINES = ("INSERT INTO lines(file_id,line_number,body,category,severity,rules,degraded) "
                 "VALUES(?,?,?,?,?,?,?)")
_INSERT_BLOCKS = (
    "INSERT INTO blocks(file_id,start_line,end_line,category,severity,rules,title,title_line,boundary,complete,"
    "addin_origin,addin_evidence,addin_name,addin_class,addin_identity_truncated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _insert_batch(connection, file_id, lines, blocks, block_counts) -> None:
    connection.executemany(_INSERT_LINES, lines)
    for block in blocks:
        block_counts[block.category] += 1
        if block.addin_origin:
            block_counts["addin_" + block.addin_origin] += 1
            block_counts["addin"] += 1
    connection.executemany(_INSERT_BLOCKS, [
        (file_id, b.start_line, b.end_line, b.category, b.severity, ",".join(sorted(b.rules)),
         b.title, b.title_line, b.boundary, int(b.complete), b.addin_origin,
         json.dumps(b.addin_evidence, ensure_ascii=False), b.addin_name, b.addin_class,
         int(b.addin_identity_truncated)) for b in blocks])


def parse_into(connection, file_id, path, encoding, rules, stop=None, progress=None) -> dict | None:
    """Read, classify and group one file into `lines`/`blocks` of an open
    transaction (no commit). Returns the file metadata, or None when stopped."""
    counts: Counter[str] = Counter()
    block_counts: Counter[str] = Counter()
    categorizer, builder = Categorizer(rules), BlockBuilder()
    classify, feed = categorizer.classify, builder.feed
    batch: list[tuple] = []
    block_batch: list = []
    with JournalReader(path, encoding) as reader:
        for line in reader:
            classification = classify(line.text)
            counts[classification[0]] += 1
            batch.append((file_id, line.number, line.text, *classification, int(line.degraded)))
            block_batch.extend(feed(line, classification))
            if len(batch) >= _BATCH:
                _insert_batch(connection, file_id, batch, block_batch, block_counts)
                batch.clear()
                block_batch.clear()
                if progress:
                    progress(reader.byte_count, reader.line_count)
                if stop and stop.is_set():
                    return None
        block_batch.extend(builder.finish())
        _insert_batch(connection, file_id, batch, block_batch, block_counts)
        return {**reader.decoding_summary(), "sha256": reader.sha256, "line_count": reader.line_count,
                "byte_count": reader.byte_count, "counts": dict(counts), "block_counts": dict(block_counts)}


# Child processes parse into a private scratch database (same columns, no
# indexes, no journal); the parent copies it with one INSERT ... SELECT. Only the
# parent writes the shared index, and children never touch its connection.
_SCRATCH_SCHEMA = """
CREATE TABLE lines (
 id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, line_number INTEGER NOT NULL,
 body TEXT NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL, rules TEXT NOT NULL,
 degraded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE blocks (
 file_id INTEGER NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 category TEXT NOT NULL, severity TEXT NOT NULL, rules TEXT NOT NULL,
 title TEXT NOT NULL, title_line INTEGER NOT NULL, boundary TEXT NOT NULL,
 complete INTEGER NOT NULL, addin_origin TEXT NOT NULL, addin_evidence TEXT NOT NULL,
 addin_name TEXT NOT NULL, addin_class TEXT NOT NULL, addin_identity_truncated INTEGER NOT NULL
);
"""
_BLOCK_COLUMNS = ("start_line,end_line,category,severity,rules,title,title_line,boundary,complete,"
                  "addin_origin,addin_evidence,addin_name,addin_class,addin_identity_truncated")
# Below this much work, starting processes costs more than it saves.
PARALLEL_MIN_BYTES = 2 * 1024 * 1024
_worker_rules = None


def _parse_to_scratch(task: tuple) -> tuple[int, dict | None, str | None]:
    """Runs in a child process. Returns (file_id, metadata or None, error or None)."""
    global _worker_rules
    file_id, path, encoding, stamp, scratch = task
    try:
        if _worker_rules is None:
            _worker_rules = load_rules()
        connection = sqlite3.connect(scratch)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.executescript(_SCRATCH_SCHEMA)
            meta = parse_into(connection, file_id, Path(path), encoding, _worker_rules)
            connection.commit()
        finally:
            connection.close()
        if _stamp(Path(path)) != stamp:
            return file_id, None, "源文件自任务创建后发生变化，请重新索引"
        return file_id, meta, None
    except (OSError, ValueError, LookupError, sqlite3.Error) as error:
        return file_id, None, str(error)


def _ignore_interrupts() -> None:
    """Child processes leave Ctrl+C to the parent, which terminates the pool."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def _kind(path: Path) -> str:
    role = describe(path).role
    if role == "unrecognized":
        return "Worker" if path.suffix.lower() == ".log" else "Journal"
    return ROLE_LABELS[role]


# Added after the first release; existing databases get them via ALTER TABLE.
_FILE_COLUMNS = {
    "encoding_request": "TEXT", "encoding_requested": "TEXT",
    "encoding_fallback": "INTEGER NOT NULL DEFAULT 0",
    "degraded_lines": "INTEGER NOT NULL DEFAULT 0", "degraded_sample": "TEXT NOT NULL DEFAULT '[]'",
}


class ViewerIndex:
    def __init__(self, database: str | Path, workers: int | None = None):
        """workers: processes for multi-file builds (default: cores-1, at most 8; 1 = serial)."""
        self.workers = default_workers() if workers is None else max(1, workers)
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.rules = load_rules()
        self.signature = INDEX_VERSION + ":" + ruleset_digest(self.rules)
        self.file_ids: list[int] = []
        self.sidecars: list[dict] = []
        # file id -> (bytes read, lines read) while its single transaction is open.
        self.progress: dict[int, tuple[int, int]] = {}
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            file_columns = {row["name"] for row in connection.execute("PRAGMA table_info(files)")}
            for name, definition in _FILE_COLUMNS.items():
                if name not in file_columns:
                    connection.execute(f"ALTER TABLE files ADD COLUMN {name} {definition}")
            if "degraded" not in {row["name"] for row in connection.execute("PRAGMA table_info(lines)")}:
                connection.execute("ALTER TABLE lines ADD COLUMN degraded INTEGER NOT NULL DEFAULT 0")
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

    @staticmethod
    def _same_decoding(row, policy) -> bool:
        """The cached rows were decoded exactly as the current request would."""
        if row["encoding_request"] == policy.key:
            return True
        # An explicit codec equal to the encoding actually used before, strictly.
        return (not policy.detect and row["encoding"] == policy.candidates[0]
                and not row["degraded_lines"])

    def register(self, paths: list[Path | InputFile], encoding: str) -> list[int]:
        """Register indexable inputs. Empty, binary and duplicate files become sidecars."""
        policy = resolve_encoding(encoding)
        planned = [item if isinstance(item, InputFile) else None for item in paths]
        if any(item is None for item in planned):
            planned = plan_inputs([getattr(item, "path", item) for item in paths])
        queued = []
        self.file_ids = []
        self.sidecars = [item.sidecar() for item in planned if item.status != "index"]
        indexable = [item.path for item in planned if item.status == "index"]
        with self.connect() as connection:
            for path in sorted(indexable, key=lambda item: item.stat().st_size):
                if path == self.database:
                    raise ValueError("索引数据库不能覆盖输入文件")
                stamp = _stamp(path)
                previous = connection.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
                if previous is None:
                    cursor = connection.execute(
                        "INSERT INTO files(path,name,size,stamp,signature,encoding_request,encoding_requested) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (str(path), path.name, path.stat().st_size, stamp, self.signature, policy.key, encoding),
                    )
                    file_id = cursor.lastrowid
                    queued.append(file_id)
                else:
                    file_id = previous["id"]
                    if (previous["status"] != "ready" or previous["stamp"] != stamp
                            or previous["signature"] != self.signature or not self._same_decoding(previous, policy)):
                        connection.execute(
                            "UPDATE files SET status='queued',stamp=?,size=?,signature=?,encoding_request=?,"
                            "encoding_requested=?,error=NULL WHERE id=?",
                            (stamp, path.stat().st_size, self.signature, policy.key, encoding, file_id),
                        )
                        queued.append(file_id)
                    # On reuse the row keeps the request it was built with.
                self.file_ids.append(file_id)
        return queued

    def build(self, file_ids: list[int], encoding: str, stop: threading.Event | None = None) -> None:
        if self._parallel(file_ids):
            self._build_parallel(file_ids, encoding, stop)
        else:
            self._build_serial(file_ids, encoding, stop)

    def _build_serial(self, file_ids: list[int], encoding: str, stop: threading.Event | None) -> None:
        for file_id in file_ids:
            if stop and stop.is_set():
                return
            try:
                self._build_file(file_id, encoding, stop)
            except (OSError, ValueError, LookupError, sqlite3.Error) as error:
                self._fail(file_id, error)
            finally:
                self.progress.pop(file_id, None)

    def _parallel(self, file_ids: list[int]) -> bool:
        if self.workers < 2 or len(file_ids) < 2:
            return False
        with self.connect() as connection:
            total = sum(row[0] for row in connection.execute(
                f"SELECT size FROM files WHERE id IN ({','.join('?' * len(file_ids))})", file_ids))
        return total >= PARALLEL_MIN_BYTES

    def _build_parallel(self, file_ids: list[int], encoding: str, stop: threading.Event | None) -> None:
        """Children parse whole files into scratch databases; this process copies
        each finished file in one transaction, so finished files are usable first."""
        with self.connect() as connection:
            rows = {row["id"]: row for row in connection.execute(
                f"SELECT * FROM files WHERE id IN ({','.join('?' * len(file_ids))})", file_ids)}
        # Largest first keeps every worker busy until the end (shortest makespan).
        order = sorted(file_ids, key=lambda file_id: -rows[file_id]["size"])
        scratch_dir = Path(tempfile.mkdtemp(prefix="revit-journal-build-"))
        tasks = [(file_id, rows[file_id]["path"], encoding, rows[file_id]["stamp"],
                  str(scratch_dir / f"{file_id}.sqlite3")) for file_id in order]
        context = multiprocessing.get_context("spawn")  # Windows-safe; no inherited connections
        try:
            try:
                pool = context.Pool(min(self.workers, len(tasks)), initializer=_ignore_interrupts)
            except OSError:
                self._build_serial(order, encoding, stop)  # Processes unavailable: same result, slower.
                return
            with pool:
                results = pool.imap_unordered(_parse_to_scratch, tasks)
                pending = len(tasks)
                while pending:
                    if stop and stop.is_set():
                        pool.terminate()  # Unfinished files stay queued.
                        return
                    try:
                        file_id, meta, error = results.next(timeout=0.2)
                    except multiprocessing.TimeoutError:
                        continue
                    pending -= 1
                    scratch = scratch_dir / f"{file_id}.sqlite3"
                    try:
                        if error is not None:
                            raise ValueError(error)
                        self._copy_scratch(file_id, scratch, meta)
                    except (OSError, ValueError, LookupError, sqlite3.Error) as failure:
                        self._fail(file_id, failure)
                    finally:
                        self.progress.pop(file_id, None)
                        scratch.unlink(missing_ok=True)
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    def _copy_scratch(self, file_id: int, scratch: Path, meta: dict) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA cache_size=-65536")
            self._start_file(connection, file_id)
            connection.execute("ATTACH DATABASE ? AS scratch", (str(scratch),))
            try:
                connection.execute(
                    "INSERT INTO lines(file_id,line_number,body,category,severity,rules,degraded) "
                    "SELECT ?,line_number,body,category,severity,rules,degraded FROM scratch.lines ORDER BY id",
                    (file_id,))
                connection.execute(f"INSERT INTO blocks(file_id,{_BLOCK_COLUMNS}) "
                                   f"SELECT ?,{_BLOCK_COLUMNS} FROM scratch.blocks ORDER BY rowid", (file_id,))
                self._finish_file(connection, file_id, meta)
                connection.commit()
            finally:
                connection.rollback()
                connection.execute("DETACH DATABASE scratch")

    def _fail(self, file_id: int, error: BaseException) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE files SET status='error',error=? WHERE id=?", (str(error), file_id))

    def _start_file(self, connection, file_id: int):
        """Short committed transaction: drop the old rows (with their FTS entries)."""
        row = connection.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        connection.execute("INSERT INTO line_search(line_search, rowid, body) "
                           "SELECT 'delete', id, body FROM lines WHERE file_id=?", (file_id,))
        connection.execute("DELETE FROM lines WHERE file_id=?", (file_id,))
        connection.execute("DELETE FROM blocks WHERE file_id=?", (file_id,))
        connection.execute("DELETE FROM block_stats WHERE file_id=?", (file_id,))
        connection.execute("UPDATE files SET status='indexing',line_count=0,indexed_bytes=0,counts='{}',error=NULL,"
                           "encoding=NULL,encoding_fallback=0,degraded_lines=0,degraded_sample='[]' WHERE id=?", (file_id,))
        connection.commit()
        self.progress[file_id] = (0, 0)
        return row

    @staticmethod
    def _finish_file(connection, file_id: int, meta: dict) -> None:
        """Same transaction as the rows: full-text rows, statistics, ready state."""
        connection.execute("INSERT INTO line_search(rowid, body) SELECT id, body FROM lines WHERE file_id=?", (file_id,))
        block_counts = meta["block_counts"]
        connection.execute("INSERT OR REPLACE INTO block_stats VALUES(?,?,?)",
                           (file_id, sum(block_counts.get(key, 0) for key in CATEGORIES), json.dumps(block_counts)))
        connection.execute(
            "UPDATE files SET status='ready',encoding=?,sha256=?,line_count=?,indexed_bytes=?,counts=?,"
            "encoding_fallback=?,degraded_lines=?,degraded_sample=? WHERE id=?",
            (meta["encoding"], meta["sha256"], meta["line_count"], meta["byte_count"], json.dumps(meta["counts"]),
             int(meta["encoding_fallback"]), meta["decode_degraded_line_count"],
             json.dumps(meta["decode_degraded_lines"]), file_id),
        )

    def _build_file(self, file_id: int, encoding: str, stop: threading.Event | None) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA cache_size=-65536")
            row = self._start_file(connection, file_id)
            path = Path(row["path"])

            def progress(byte_count, line_count):
                self.progress[file_id] = (byte_count, line_count)

            # One transaction for the whole file: a failure or stop rolls back
            # every row, so a file is either complete and ready or absent.
            meta = parse_into(connection, file_id, path, encoding, self.rules, stop, progress)
            if meta is None:
                connection.rollback()
                connection.execute("UPDATE files SET status='queued' WHERE id=?", (file_id,))
                return
            if _stamp(path) != row["stamp"]:
                raise ValueError("源文件自任务创建后发生变化，请重新索引")
            self._finish_file(connection, file_id, meta)

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
            if item["status"] == "indexing" and item["id"] in self.progress:
                # Rows become visible only at commit; report in-process progress.
                item["indexed_bytes"], item["line_count"] = self.progress[item["id"]]
            item["counts"] = json.loads(item["counts"])
            item["block_counts"] = json.loads(item["block_counts"])
            item["degraded_sample"] = json.loads(item["degraded_sample"] or "[]")
            item["encoding_fallback"] = bool(item["encoding_fallback"])
            item["kind"] = _kind(Path(item["path"]))
            name = describe(Path(item["path"]))
            item.update(session=name.key, session_label=name.label, session_directory=name.directory,
                        role=name.role)
            try:
                item["source_changed"] = _stamp(Path(item["path"])) != item["stamp"]
            except OSError:
                item["source_changed"] = True
            result.append(item)
        result.sort(key=lambda item: sort_key(Path(item["path"])))
        return result

    def sessions(self, catalog: list[dict] | None = None) -> list[dict]:
        """Indexed files and sidecars (empty/binary/duplicate) grouped by Revit session."""
        catalog = self.catalog() if catalog is None else catalog
        groups: dict[str, dict] = {}
        members = [(Path(item["path"]), "file", item) for item in catalog]
        members += [(Path(item["path"]), "sidecar", item) for item in self.sidecars]
        for path, kind, item in sorted(members, key=lambda member: sort_key(member[0])):
            name = describe(path)
            group = groups.setdefault(name.key, {
                "session": name.key, "label": name.label, "directory": name.directory,
                "recognized": name.role != "unrecognized", "files": [], "sidecars": [],
                "dump_count": 0, "dump_bytes": 0,
            })
            if kind == "file":
                group["files"].append(item["id"])
            else:
                group["sidecars"].append(item)
                if item["status"] == "binary":
                    group["dump_count"] += 1
                    group["dump_bytes"] += item["size"]
        return list(groups.values())

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
                "SELECT l.line_number,l.category,l.severity,l.rules,substr(l.body,1,700) AS text,length(l.body) AS text_length,l.degraded FROM lines l WHERE "
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
                "SELECT line_number,category,severity,rules,substr(body,1,16384) AS text,length(body) AS text_length,degraded FROM lines WHERE file_id=? AND line_number BETWEEN ? AND ? ORDER BY line_number",
                (file_id, max(1, line - radius), line + radius),
            ).fetchall()
        return {"line": line, "sha256": file["sha256"], "encoding": file["encoding"],
                "encoding_fallback": bool(file["encoding_fallback"]),
                "decode_degraded_line_count": file["degraded_lines"],
                "source": file["path"], "items": [dict(row) for row in rows]}

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
                "SELECT line_number,substr(body,1,16384) AS text,length(body) AS text_length,degraded "
                "FROM lines WHERE file_id=? AND line_number BETWEEN ? AND ? ORDER BY line_number",
                (file_id, begin, min(block["end_line"], begin + limit - 1)),
            ).fetchall()
        return {"block": block, "items": [dict(row) for row in rows],
                "has_more": begin + limit <= block["end_line"]}
