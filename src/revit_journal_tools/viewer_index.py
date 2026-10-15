"""Persistent full-line index. Pagination never depends on retained scan findings."""

import json
import multiprocessing
import os
import re
import shutil
import signal
import sqlite3
import tempfile
import threading
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .categories import CATEGORIES, Categorizer
from .blocks import BLOCK_VERSION, PHASES, BlockBuilder, derive_title
from .loading import BLOCK_FILTERS, block_filter, matches_filter
from .live import Snapshot, held_for_writing, wants_snapshot
from .reader import JournalReader, SourceChangedError, basis_label, decode_span, resolve_encoding
from .rules import load_rules, ruleset_digest
from .summary import END_STATES, SessionCollector, end_state, session_findings
from .sessions import ROLE_LABELS, InputFile, SessionName, describe, plan_inputs, recency_sort
from .sidecars import (BASIS_TEXT, cer_logs, match_cer, match_steel_log, parse_steel_log, revit_version, revit_year,
                       session_window)

INDEX_VERSION = "viewer-6:" + BLOCK_VERSION
# offsets (default): `lines` keeps each line's byte range in the source file and
# the text is read back from it, which must stay where it is and unchanged.
# text: `lines` keeps the full text (larger, independent of the source).
STORE_MODES = ("offsets", "text")
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
 degraded INTEGER NOT NULL DEFAULT 0, byte_offset INTEGER NOT NULL DEFAULT 0,
 byte_length INTEGER NOT NULL DEFAULT 0
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
 phase TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '',
 title_from_line INTEGER NOT NULL DEFAULT 0, title_joins INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(file_id,start_line)
);
CREATE INDEX IF NOT EXISTS blocks_category ON blocks(file_id,category,start_line);
CREATE TABLE IF NOT EXISTS block_folds (
 file_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, first_line INTEGER NOT NULL,
 last_line INTEGER NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(file_id,fingerprint)
);
CREATE TABLE IF NOT EXISTS block_stats (
 file_id INTEGER PRIMARY KEY, block_count INTEGER NOT NULL, counts TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS line_search USING fts5(
 body, content='lines', content_rowid='id', columnsize=0
);
DROP TRIGGER IF EXISTS lines_insert;
DROP TRIGGER IF EXISTS lines_delete;
"""
# Full-text search: one contentless FTS5 table per file (fts_table(id), rowid =
# line number), created with the file's rows in one transaction and dropped
# with them, so rebuilding never needs the old text. The shared line_search
# table only still holds entries of files indexed by earlier versions (store
# ''); they are deleted when such a file is rebuilt. Per-row triggers of even
# earlier versions cost ~80% of the SQLite time; they are dropped on open.
_BATCH = 20_000
_FTS = "CREATE VIRTUAL TABLE {name} USING fts5(body, content='', columnsize=0)"
# store="offsets" collects the text in a plain table (TEMP, or the worker's
# scratch database) and builds the full-text table from it in one statement.
_TEXTS = "CREATE {temp}TABLE {name}(line_number INTEGER PRIMARY KEY, body TEXT NOT NULL)"
# A title the source can give back (title_from_line) keeps only this prefix,
# enough for SQL filters such as tail_blocks' "Jrn.Directive".
_TITLE_PREFIX = 16


def fts_table(file_id: int) -> str:
    return f"line_search_{int(file_id)}"


NO_SEARCH_TOKENS = "搜索词不含可检索的字母或数字；标点和符号不参与分词"
CJK_HINT = "中文、日文等连续文字按整段匹配，只能从段首开始查找"
EMPTY_QUERY = "请输入搜索词"
# A "phrase" (to the closing quote, or to the end when it is missing) or a word.
_QUERY_PART = re.compile(r'"([^"]*)(?:"|$)|([^\s"]+)')
# Kana, CJK ideographs (with extension A, compatibility and the supplementary
# planes) and Hangul: unicode61 keeps a run of them as one token.
_CJK = re.compile("[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
                  "\uff66-\uff9f\U00020000-\U0003134f]")


@dataclass
class SearchQuery:
    """A keyword query (parse_query). match: FTS5 MATCH expression ('' when
    there is no query); terms: the words and phrases as typed, in order, for
    highlighting; cjk: a term contains CJK text (matched per run, see CJK_HINT)."""
    match: str
    terms: list[str]
    cjk: bool


def parse_query(query: str) -> SearchQuery:
    """Keyword input to an FTS5 expression. Every whitespace-separated word must
    occur in a line, in any order (AND); "quoted text" is one phrase whose words
    must be adjacent. A word with punctuation is a phrase of its tokens
    (Jrn.Command: jrn followed by command). The last unquoted word also matches
    as a prefix (search while typing). Words without a letter or digit have no
    tokens (unicode61) and are left out; if nothing is left: ValueError. Blank
    input is no query (match '')."""
    parts = []
    for found in _QUERY_PART.finditer(query):
        phrase, word = found.groups()
        text, quoted = (phrase.strip(), True) if word is None else (word, False)
        if any(character.isalnum() for character in text):
            parts.append((text, quoted))
    if not parts:
        if query.strip():
            raise ValueError(NO_SEARCH_TOKENS)
        return SearchQuery("", [], False)
    last = max((number for number, (_, quoted) in enumerate(parts) if not quoted), default=-1)
    match = " AND ".join('"' + text.replace('"', '""') + '"' + ("*" if number == last else "")
                         for number, (text, _) in enumerate(parts))
    terms = [text for text, _ in parts]
    return SearchQuery(match, terms, any(_CJK.search(term) for term in terms))


def _search_fields(search: SearchQuery, hit_lines: int) -> dict:
    """Keys a result with a keyword query adds: terms, hit_lines and (CJK) hint."""
    fields = {"terms": list(search.terms), "hit_lines": hit_lines}
    if search.cjk:
        fields["hint"] = CJK_HINT
    return fields


def _stamp(path: Path) -> str:
    info = path.stat()
    return json.dumps([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])


_INSERT_LINES = ("INSERT INTO lines(file_id,line_number,body,category,severity,rules,degraded,byte_offset,byte_length) "
                 "VALUES(?,?,?,?,?,?,?,?,?)")
_INSERT_BLOCKS = (
    "INSERT INTO blocks(file_id,start_line,end_line,category,severity,rules,title,title_line,boundary,complete,"
    "addin_origin,addin_evidence,addin_name,addin_class,addin_identity_truncated,phase,fingerprint,title_from_line,"
    "title_joins) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _insert_batch(connection, file_id, lines, blocks, block_counts, texts=None, text_table="") -> None:
    """texts: (line number, text) rows for text_table (store="offsets"); their
    titles that the source gives back are stored as a prefix only."""
    connection.executemany(_INSERT_LINES, lines)
    if texts is not None:
        connection.executemany(f"INSERT INTO {text_table}(rowid, body) VALUES(?,?)", texts)
    for block in blocks:
        block_counts[block.category] += 1
        if block.addin_origin:
            block_counts["addin_" + block.addin_origin] += 1
            block_counts["addin"] += 1
    connection.executemany(_INSERT_BLOCKS, [
        (file_id, b.start_line, b.end_line, b.category, b.severity, ",".join(sorted(b.rules)),
         b.title[:_TITLE_PREFIX] if texts is not None and b.title_from_line else b.title,
         b.title_line, b.boundary, int(b.complete), b.addin_origin,
         json.dumps(b.addin_evidence, ensure_ascii=False) if b.addin_evidence else "[]", b.addin_name, b.addin_class,
         int(b.addin_identity_truncated), b.phase, b.fingerprint, int(b.title_from_line), b.title_joins)
        for b in blocks])


def parse_into(connection, file_id, path, encoding, rules, stop=None, progress=None,
               text_table: tuple[str, str] | None = None) -> dict | None:
    """Read, classify and group one file into `lines`/`blocks` of an open
    transaction (no commit). Returns the file metadata, or None when stopped.
    text_table: (name, CREATE statement) for store="offsets": `lines` gets each
    line's byte range in the source and the text goes only to that table (rowid
    = line number). Encodings whose lines are not byte-addressable (UTF-16/32)
    keep the text in `lines` (meta store="text")."""
    counts: Counter[str] = Counter()
    block_counts: Counter[str] = Counter()
    categorizer, builder, collector = Categorizer(rules), BlockBuilder(), SessionCollector()
    classify, feed, collect = categorizer.classify, builder.feed, collector.feed
    batch: list[tuple] = []
    block_batch: list = []
    with JournalReader(path, encoding, track_spans=text_table is not None) as reader:
        texts: list[tuple] | None = None
        if reader.span_codec is not None:
            texts = []
            connection.execute(text_table[1])
        for line in reader:
            classification = classify(line.text)
            counts[classification[0]] += 1
            if texts is None:
                batch.append((file_id, line.number, line.text, *classification, line.degraded, 0, 0))  # bool binds as 0/1
            else:
                batch.append((file_id, line.number, "", *classification, line.degraded, *reader.span))
                texts.append((line.number, line.text))
            collect(line.number, line.text, classification)
            block_batch.extend(feed(line, classification))
            if len(batch) >= _BATCH:
                collector.add_blocks(block_batch)
                _insert_batch(connection, file_id, batch, block_batch, block_counts, texts, text_table and text_table[0])
                batch.clear()
                block_batch.clear()
                if texts is not None:
                    texts.clear()
                if progress:
                    progress(reader.byte_count, reader.line_count)
                if stop and stop.is_set():
                    return None
        block_batch.extend(builder.finish())
        collector.add_blocks(block_batch)
        _insert_batch(connection, file_id, batch, block_batch, block_counts, texts, text_table and text_table[0])
        return {**reader.decoding_summary(), "sha256": reader.sha256, "line_count": reader.line_count,
                "byte_count": reader.byte_count, "counts": dict(counts), "block_counts": dict(block_counts),
                "summary": collector.finish(), "live": False, "snapshot": None,
                "store": "text" if texts is None else "offsets", "span_codec": reader.span_codec or ""}


def parse_file(connection, file_id, path, encoding, rules, stop=None, progress=None,
               snapshot: str = "auto", text_table: tuple[str, str] | None = None) -> dict | None:
    """parse_into on the file itself, or on a snapshot when Revit is still
    writing it (or it changed while read and snapshot is not "never"). A
    snapshot is a temporary copy, so its text is always stored (store="text")."""
    if not wants_snapshot(path, snapshot):
        try:
            return parse_into(connection, file_id, path, encoding, rules, stop, progress, text_table)
        except SourceChangedError:
            if snapshot == "never":
                raise
            # Discard the rows of the attempt and retry.
            connection.execute("DELETE FROM lines WHERE file_id=?", (file_id,))
            connection.execute("DELETE FROM blocks WHERE file_id=?", (file_id,))
            if text_table is not None:
                connection.execute(f"DROP TABLE IF EXISTS {text_table[0]}")
    with Snapshot(path) as info:
        meta = parse_into(connection, file_id, Path(info["path"]), encoding, rules, stop, progress)
    if meta is not None:
        meta["live"] = True
        meta["snapshot"] = {key: info[key] for key in ("bytes", "source_size", "trimmed_bytes", "taken_at")}
    return meta


# Child processes parse into a private scratch database (same columns, no
# indexes, no journal); the parent copies it with one INSERT ... SELECT. Only the
# parent writes the shared index, and children never touch its connection.
_SCRATCH_SCHEMA = """
CREATE TABLE lines (
 id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, line_number INTEGER NOT NULL,
 body TEXT NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL, rules TEXT NOT NULL,
 degraded INTEGER NOT NULL DEFAULT 0, byte_offset INTEGER NOT NULL, byte_length INTEGER NOT NULL
);
CREATE TABLE blocks (
 file_id INTEGER NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 category TEXT NOT NULL, severity TEXT NOT NULL, rules TEXT NOT NULL,
 title TEXT NOT NULL, title_line INTEGER NOT NULL, boundary TEXT NOT NULL,
 complete INTEGER NOT NULL, addin_origin TEXT NOT NULL, addin_evidence TEXT NOT NULL,
 addin_name TEXT NOT NULL, addin_class TEXT NOT NULL, addin_identity_truncated INTEGER NOT NULL,
 phase TEXT NOT NULL, fingerprint TEXT NOT NULL, title_from_line INTEGER NOT NULL, title_joins INTEGER NOT NULL
);
"""
_BLOCK_COLUMNS = ("start_line,end_line,category,severity,rules,title,title_line,boundary,complete,"
                  "addin_origin,addin_evidence,addin_name,addin_class,addin_identity_truncated,phase,fingerprint,"
                  "title_from_line,title_joins")
# Below this much work, starting processes costs more than it saves.
PARALLEL_MIN_BYTES = 2 * 1024 * 1024
_OWN_FORMAT_ROLES = {"steel_log"}
_worker_rules = None


def _parse_to_scratch(task: tuple) -> tuple[int, dict | None, str | None]:
    """Runs in a child process. Returns (file_id, metadata or None, error or None)."""
    global _worker_rules
    file_id, path, encoding, stamp, scratch, snapshot, store = task
    try:
        if _worker_rules is None:
            _worker_rules = load_rules()
        connection = sqlite3.connect(scratch)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.executescript(_SCRATCH_SCHEMA)
            # store="offsets": the text goes to a plain scratch table, from which
            # the parent fills the file's full-text table.
            texts = ("texts", _TEXTS.format(temp="", name="texts")) if store == "offsets" else None
            meta = parse_file(connection, file_id, Path(path), encoding, _worker_rules, snapshot=snapshot,
                              text_table=texts)
            connection.commit()
        finally:
            connection.close()
        if not meta["live"] and _stamp(Path(path)) != stamp:
            return file_id, None, "源文件自任务创建后发生变化，请重新索引"
        return file_id, meta, None
    except (OSError, ValueError, LookupError, sqlite3.Error) as error:
        return file_id, None, str(error)


def _terminate_workers(executor) -> None:
    """Stop worker processes now (stop request or broken pool)."""
    terminate = getattr(executor, "terminate_workers", None)  # Python 3.14+
    if terminate is not None:
        terminate()
        return
    for process in list((getattr(executor, "_processes", None) or {}).values()):
        process.terminate()


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


class _Source:
    """Line text read back from its source file (store="offsets"), which must be
    unchanged since it was indexed: same size, modification time and file id."""

    def __init__(self, file):
        path = Path(file["path"])
        try:
            changed = _stamp(path) != file["stamp"]
        except OSError:
            changed = True
        if changed:
            raise ValueError("源文件已移动、删除或修改，无法按索引中的位置读取原文；重新打开即可重建索引"
                             "（--store text会把全文保存在索引中）")
        self.codec = file["span_codec"]
        self.stream = path.open("rb")

    def text(self, offset: int, length: int, degraded) -> str:
        self.stream.seek(offset)
        data = self.stream.read(length)
        if len(data) != length:
            raise ValueError("源文件比索引时短，无法读取原文；请重新索引")
        return decode_span(data, self.codec, bool(degraded))

    def close(self) -> None:
        self.stream.close()


def _with_text(file, rows, limit: int) -> list[dict]:
    """Rows as dicts with text (first `limit` characters) and text_length; for
    store="offsets" both come from the source file."""
    items = [dict(row) for row in rows]
    source = _Source(file) if file["store"] == "offsets" and items else None
    try:
        for item in items:
            offset, length = item.pop("byte_offset"), item.pop("byte_length")
            if source is not None:
                text = source.text(offset, length, item["degraded"])
                item["text"], item["text_length"] = text[:limit], len(text)
    finally:
        if source is not None:
            source.close()
    return items


def _with_titles(connection, file, items: list[dict]) -> list[dict]:
    """Titles stored as a prefix (store="offsets", title_from_line) are derived
    again from their title line and joined continuation lines in the source."""
    derived = [item for item in items if item["title_from_line"]]
    if file["store"] != "offsets" or not derived:
        return items
    wanted = sorted({number for item in derived
                     for number in range(item["title_line"], item["title_line"] + item["title_joins"] + 1)})
    rows = []
    for start in range(0, len(wanted), 500):  # within every SQLite's parameter limit
        chunk = wanted[start:start + 500]
        rows += connection.execute(
            f"SELECT line_number,byte_offset,byte_length,degraded FROM lines WHERE file_id=? "
            f"AND line_number IN ({','.join('?' * len(chunk))})", (file["id"], *chunk)).fetchall()
    source = _Source(file)
    try:
        texts = {row[0]: source.text(row[1], row[2], row[3]) for row in rows}
    finally:
        source.close()
    for item in derived:
        first = item["title_line"]
        item["title"] = derive_title([texts[number] for number in range(first, first + item["title_joins"] + 1)])
    return items


# Added after the first release; existing databases get them via ALTER TABLE.
_FILE_COLUMNS = {
    "encoding_request": "TEXT", "encoding_requested": "TEXT",
    "encoding_fallback": "INTEGER NOT NULL DEFAULT 0", "encoding_basis": "TEXT NOT NULL DEFAULT ''",
    "degraded_lines": "INTEGER NOT NULL DEFAULT 0", "degraded_sample": "TEXT NOT NULL DEFAULT '[]'",
    "live": "INTEGER NOT NULL DEFAULT 0", "snapshot": "TEXT NOT NULL DEFAULT ''",
    "summary": "TEXT NOT NULL DEFAULT '{}'",
    # '' for files indexed by earlier versions (text in the shared line_search).
    "store": "TEXT NOT NULL DEFAULT ''", "span_codec": "TEXT NOT NULL DEFAULT ''",
}


class ViewerIndex:
    def __init__(self, database: str | Path, workers: int | None = None, snapshot: str = "auto",
                 local_appdata: str | Path | None = None, store: str = "offsets"):
        """workers: processes for multi-file builds (default: cores-1, at most 8; 1 = serial).
        snapshot: auto/always/never for journals Revit is still writing (live.py).
        local_appdata: root for CER logs (default %LOCALAPPDATA%).
        store: offsets/text, see STORE_MODES; changing it rebuilds the files."""
        if store not in STORE_MODES:
            raise ValueError("store必须为offsets或text")
        self.store = store
        self.workers = default_workers() if workers is None else max(1, workers)
        # (initializer, args) run in each worker process; tests replace it.
        self.worker_init: tuple = (_ignore_interrupts, ())
        self.snapshot = snapshot
        self.local_appdata = local_appdata
        self._cer_logs: list[dict] | None = None
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.rules = load_rules()
        self.signature = INDEX_VERSION + ":" + store + ":" + ruleset_digest(self.rules)
        self.file_ids: list[int] = []
        self.sidecars: list[dict] = []
        self.overrides: dict[str, SessionName] = {}
        self.encoding_overrides: dict[int, str] = {}  # file id -> request for sidecar logs  # sidecar logs attached to a session by time
        self.preferred_id: int | None = None  # newest main journal: opened and indexed first
        # file id -> (bytes read, lines read) while its single transaction is open.
        self.progress: dict[int, tuple[int, int]] = {}
        with self.connect() as connection:
            # Only a new, empty database takes it (existing ones keep theirs):
            # about 4% faster full builds than the 4096 default, same size.
            connection.execute("PRAGMA page_size=8192")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            file_columns = {row["name"] for row in connection.execute("PRAGMA table_info(files)")}
            for name, definition in _FILE_COLUMNS.items():
                if name not in file_columns:
                    connection.execute(f"ALTER TABLE files ADD COLUMN {name} {definition}")
            line_columns = {row["name"] for row in connection.execute("PRAGMA table_info(lines)")}
            for name in ("degraded", "byte_offset", "byte_length"):
                if name not in line_columns:
                    connection.execute(f"ALTER TABLE lines ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(blocks)")}
            if "addin_origin" not in columns:
                connection.execute("ALTER TABLE blocks ADD COLUMN addin_origin TEXT NOT NULL DEFAULT ''")
            if "addin_evidence" not in columns:
                connection.execute("ALTER TABLE blocks ADD COLUMN addin_evidence TEXT NOT NULL DEFAULT '[]'")
            for name in ("addin_name", "addin_class", "phase", "fingerprint"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE blocks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            for name in ("addin_identity_truncated", "title_from_line", "title_joins"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE blocks ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
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
        policies = {encoding: resolve_encoding(encoding), "auto": resolve_encoding("auto")}
        planned = [item if isinstance(item, InputFile) else None for item in paths]
        if any(item is None for item in planned):
            planned = plan_inputs([getattr(item, "path", item) for item in paths])
        queued = []
        self.file_ids = []
        self.sidecars = [item.sidecar() for item in planned if item.status != "index"]
        self.overrides = {str(item.path): item.session_override for item in planned if item.session_override}
        # planned is newest session first; the queue follows it, newest main journal first.
        indexable = [item.path for item in planned if item.status == "index"]
        # Sidecar logs keep their own format (the Steel Connections log is
        # UTF-16 with a BOM): the journals' encoding request does not apply.
        own_format = {item.path for item in planned if item.session.role in _OWN_FORMAT_ROLES}
        self.encoding_overrides = {}
        main = next((item.path for item in planned if item.status == "index" and item.session.role == "main"), None)
        if main is not None:
            indexable.remove(main)
            indexable.insert(0, main)
        self.preferred_id = None
        with self.connect() as connection:
            for path in indexable:
                if path == self.database:
                    raise ValueError("索引数据库不能覆盖输入文件")
                stamp = _stamp(path)
                request = "auto" if path in own_format else encoding
                policy = policies[request]
                previous = connection.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
                if previous is None:
                    cursor = connection.execute(
                        "INSERT INTO files(path,name,size,stamp,signature,encoding_request,encoding_requested) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (str(path), path.name, path.stat().st_size, stamp, self.signature, policy.key, request),
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
                            (stamp, path.stat().st_size, self.signature, policy.key, request, file_id),
                        )
                        queued.append(file_id)
                    # On reuse the row keeps the request it was built with.
                self.file_ids.append(file_id)
                if request != encoding:
                    self.encoding_overrides[file_id] = request
                if path == main:
                    self.preferred_id = file_id
        return queued

    def build(self, file_ids: list[int], encoding: str, stop: threading.Event | None = None,
              snapshot: str | None = None) -> None:
        """snapshot: auto (default) / always / never, see live.py."""
        if snapshot is not None:
            self.snapshot = snapshot
        if self._parallel(file_ids):
            self._build_parallel(file_ids, encoding, stop)
        else:
            self._build_serial(file_ids, encoding, stop)

    def _build_serial(self, file_ids: list[int], encoding: str, stop: threading.Event | None) -> None:
        for file_id in file_ids:
            if stop and stop.is_set():
                return
            try:
                self._build_file(file_id, self.encoding_overrides.get(file_id, encoding), stop)
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
        if self.preferred_id in order:  # The file the viewer opens first starts first.
            order.remove(self.preferred_id)
            order.insert(0, self.preferred_id)
        tasks = [(file_id, rows[file_id]["path"], self.encoding_overrides.get(file_id, encoding),
                  rows[file_id]["stamp"], str(scratch_dir / f"{file_id}.sqlite3"), self.snapshot, self.store)
                 for file_id in order]
        context = multiprocessing.get_context("spawn")  # Windows-safe; no inherited connections
        remaining = list(order)
        try:
            try:
                executor = ProcessPoolExecutor(min(self.workers, len(tasks)), mp_context=context,
                                               initializer=self.worker_init[0], initargs=self.worker_init[1])
            except OSError:
                self._build_serial(order, encoding, stop)  # Processes unavailable: same result, slower.
                return
            broken = stopped = False
            try:
                futures = {executor.submit(_parse_to_scratch, task): task[0] for task in tasks}
                pending = set(futures)
                while pending and not broken:
                    if stop and stop.is_set():
                        stopped = True  # Unfinished files stay queued.
                        return
                    done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in done:
                        file_id = futures[future]
                        try:
                            _, meta, error = future.result()
                        except BrokenProcessPool:
                            # A worker died (e.g. the main module cannot be
                            # re-imported by spawn): finish here instead of waiting.
                            broken = True
                            continue
                        except Exception as failure:  # noqa: BLE001 - reported per file
                            meta, error = None, f"{type(failure).__name__}: {failure}"
                        remaining.remove(file_id)
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
                if broken or stopped:
                    _terminate_workers(executor)
                executor.shutdown(wait=not (broken or stopped), cancel_futures=True)
            if broken:
                self._build_serial(remaining, encoding, stop)
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

    def _copy_scratch(self, file_id: int, scratch: Path, meta: dict) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA cache_size=-65536")
            self._start_file(connection, file_id)
            connection.execute("ATTACH DATABASE ? AS scratch", (str(scratch),))
            try:
                connection.execute(
                    "INSERT INTO lines(file_id,line_number,body,category,severity,rules,degraded,byte_offset,byte_length) "
                    "SELECT ?,line_number,body,category,severity,rules,degraded,byte_offset,byte_length "
                    "FROM scratch.lines ORDER BY id", (file_id,))
                connection.execute(f"INSERT INTO blocks(file_id,{_BLOCK_COLUMNS}) "
                                   f"SELECT ?,{_BLOCK_COLUMNS} FROM scratch.blocks ORDER BY rowid", (file_id,))
                self._finish_file(connection, file_id, meta, "scratch.texts")
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
        if not row["store"]:
            # Indexed by an earlier version: its full-text rows are in the shared table.
            connection.execute("INSERT INTO line_search(line_search, rowid, body) "
                               "SELECT 'delete', id, body FROM lines WHERE file_id=?", (file_id,))
        connection.execute(f"DROP TABLE IF EXISTS {fts_table(file_id)}")
        connection.execute("DELETE FROM lines WHERE file_id=?", (file_id,))
        connection.execute("DELETE FROM blocks WHERE file_id=?", (file_id,))
        connection.execute("DELETE FROM block_stats WHERE file_id=?", (file_id,))
        connection.execute("DELETE FROM block_folds WHERE file_id=?", (file_id,))
        connection.execute("UPDATE files SET status='indexing',line_count=0,indexed_bytes=0,counts='{}',error=NULL,"
                           "encoding=NULL,encoding_fallback=0,encoding_basis='',degraded_lines=0,degraded_sample='[]',"
                           "live=0,snapshot='',summary='{}' WHERE id=?", (file_id,))
        connection.commit()
        self.progress[file_id] = (0, 0)
        return row

    @staticmethod
    def _finish_file(connection, file_id: int, meta: dict, texts: str = "") -> None:
        """Same transaction as the rows: full-text rows, folds, statistics, ready state.
        texts: the table holding the text when meta["store"] == "offsets"."""
        table = fts_table(file_id)
        connection.execute(_FTS.format(name=table))
        if meta["store"] == "offsets":
            connection.execute(f"INSERT INTO {table}(rowid, body) SELECT line_number, body FROM {texts}")
        else:
            connection.execute(f"INSERT INTO {table}(rowid, body) SELECT line_number, body FROM lines WHERE file_id=?",
                               (file_id,))
        # Only fingerprints that repeat; the list shows their first block with the count.
        connection.execute(
            "INSERT INTO block_folds(file_id,fingerprint,first_line,last_line,count) "
            "SELECT file_id,fingerprint,MIN(start_line),MAX(start_line),COUNT(*) FROM blocks "
            "WHERE file_id=? AND fingerprint<>'' GROUP BY fingerprint HAVING COUNT(*)>1", (file_id,))
        block_counts = meta["block_counts"]
        connection.execute("INSERT OR REPLACE INTO block_stats VALUES(?,?,?)",
                           (file_id, sum(block_counts.get(key, 0) for key in CATEGORIES), json.dumps(block_counts)))
        connection.execute(
            "UPDATE files SET status='ready',encoding=?,sha256=?,line_count=?,indexed_bytes=?,counts=?,"
            "encoding_fallback=?,encoding_basis=?,degraded_lines=?,degraded_sample=?,live=?,snapshot=?,summary=?,"
            "store=?,span_codec=? WHERE id=?",
            (meta["encoding"], meta["sha256"], meta["line_count"], meta["byte_count"], json.dumps(meta["counts"]),
             int(meta["encoding_fallback"]), meta["encoding_basis"], meta["decode_degraded_line_count"],
             json.dumps(meta["decode_degraded_lines"]), int(meta["live"]),
             json.dumps(meta["snapshot"]) if meta["snapshot"] else "",
             json.dumps(meta["summary"], ensure_ascii=False), meta["store"], meta["span_codec"], file_id),
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
            name = f"texts_{int(file_id)}"  # TEMP: gone with this connection
            texts = ("temp." + name, _TEXTS.format(temp="TEMP ", name=name)) if self.store == "offsets" else None
            meta = parse_file(connection, file_id, path, encoding, self.rules, stop, progress, self.snapshot, texts)
            if meta is None:
                connection.rollback()
                connection.execute("UPDATE files SET status='queued' WHERE id=?", (file_id,))
                return
            if not meta["live"] and _stamp(path) != row["stamp"]:
                raise ValueError("源文件自任务创建后发生变化，请重新索引")
            self._finish_file(connection, file_id, meta, texts and texts[0])

    def describe(self, path: str | Path) -> SessionName:
        """Session of a workspace file, honouring sidecar logs attached by time."""
        return self.overrides.get(str(path)) or describe(Path(path))

    def catalog(self) -> list[dict]:
        """Workspace files, newest session first; live/writing state is explicit."""
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
            item["live"] = bool(item["live"])
            item["snapshot"] = json.loads(item["snapshot"]) if item["snapshot"] else None
            item["summary"] = json.loads(item["summary"] or "{}")
            name = self.describe(item["path"])
            item["kind"] = ROLE_LABELS[name.role] if name.role != "unrecognized" else _kind(Path(item["path"]))
            item.update(session=name.key, session_label=name.label, session_directory=name.directory,
                        role=name.role, mtime_ns=json.loads(item["stamp"])[3],
                        revit_year=revit_version(Path(item["path"]), item["summary"]))
            # Revit still holds the file open for writing right now (read-only probe).
            item["writing"] = held_for_writing(item["path"])
            try:
                item["source_changed"] = _stamp(Path(item["path"])) != item["stamp"]
            except OSError:
                item["source_changed"] = True
            result.append(item)
        return recency_sort(result, lambda item: (self.describe(item["path"]), item["mtime_ns"]))

    def _cer(self) -> list[dict]:
        if self._cer_logs is None:
            self._cer_logs = cer_logs(self.local_appdata)
        return self._cer_logs

    def sessions(self, catalog: list[dict] | None = None) -> list[dict]:
        """Workspace grouped by Revit session (newest first), with the session
        summary, sidecar logs and session-level findings (all candidates)."""
        catalog = self.catalog() if catalog is None else catalog
        groups: dict[str, dict] = {}
        # Session order follows the catalog (newest indexed file first); sessions
        # made only of sidecars (e.g. an empty dump.NNNN.txt) by their own time.
        with_files = {self.describe(item["path"]).key for item in catalog}
        members = [(item, "file") for item in catalog] + [(item, "sidecar") for item in self.sidecars]
        ordered = recency_sort(members, lambda member: (
            self.describe(member[0]["path"]),
            0 if member[1] == "sidecar" and self.describe(member[0]["path"]).key in with_files
            else member[0].get("mtime_ns", 0)))
        for item, kind in ordered:
            name = self.describe(item["path"])
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
        by_id = {item["id"]: item for item in catalog}
        for group in groups.values():
            self._summarize(group, [by_id[file_id] for file_id in group["files"]])
        return list(groups.values())

    def _summarize(self, group: dict, files: list[dict]) -> None:
        main = next((item for item in files if item["role"] == "main"), files[0] if files else None)
        group["main"] = main["path"] if main else None
        group["writing"] = any(item["writing"] for item in files)
        group["live"] = group["writing"] or bool(main and main["live"])
        summary = main["summary"] if main and main["status"] == "ready" else {}
        group["summary"] = summary
        sidecars = []
        for item in files:
            if item["role"] == "worker":
                sidecars.append({"kind": "worker", "path": item["path"], "size": item["size"],
                                 "line_count": item["line_count"], "status": item["status"]})
            elif item["role"] == "steel_log":
                entries = []
                try:
                    entries = parse_steel_log(Path(item["path"]))
                except (OSError, ValueError, LookupError):
                    pass
                window = session_window(Path(main["path"])) if main else None
                basis = match_steel_log(Path(item["path"]), entries, window) if window else ""
                sidecars.append({"kind": "steel_log", "path": item["path"], "size": item["size"],
                                 "entries": entries, "basis": BASIS_TEXT.get(basis, "同一Revit版本，按时间关联"),
                                 "basis_key": basis})
        for item in group["sidecars"]:
            sidecars.append({"kind": "dump" if item["status"] == "binary" else item["status"],
                             "path": item["path"], "size": item["size"], "status": item["status"],
                             "duplicate_of": item.get("duplicate_of")})
        if summary and main:
            sidecars.extend(match_cer(summary, revit_year(Path(main["path"])), self._cer()))
        group["sidecar_logs"] = sidecars
        if summary.get("recording_started"):  # only real session journals have an end state
            group["end_state"] = end_state(summary, group["live"])
            group["end_state_label"] = END_STATES[group["end_state"]]
            group["session_findings"] = session_findings(summary, group["live"], sidecars, main["path"])
        else:
            group["end_state"] = group["end_state_label"] = None
            group["session_findings"] = []

    def _ready(self, connection, file_id):
        if file_id not in self.file_ids:
            raise KeyError("文件未在当前工作区中")
        row = connection.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row["status"] != "ready":
            raise ValueError("文件尚未完成索引，请查看文件列表中的进度或错误")
        if not row["store"]:
            raise ValueError("该文件的索引来自旧版本，请重新打开以重建")
        return row

    @staticmethod
    def _line_rows(connection, file_id: int, category: str, match: str, after: int, limit: int) -> list:
        """Lines after `after` (category "all" or a line category; match: FTS5
        expression or '' for every line), for _with_text(..., 700)."""
        where = ["l.file_id=?", "l.line_number>?"]
        values: list = [file_id, after]
        if category != "all":
            where.append("l.category=?")
            values.append(category)
        if match:
            table = fts_table(file_id)
            if category == "all":
                # The hits in line order are the page: no need to visit every
                # hit of a common word, as the IN (subquery) form would.
                numbers = [row[0] for row in connection.execute(
                    f"SELECT rowid FROM {table} WHERE {table} MATCH ? AND rowid>? ORDER BY rowid LIMIT ?",
                    (match, after, limit))]
                where.append(f"l.line_number IN ({','.join('?' * len(numbers))})")
                values.extend(numbers)
            else:
                where.append(f"l.line_number IN (SELECT rowid FROM {table} WHERE {table} MATCH ?)")
                values.append(match)
        return connection.execute(
            "SELECT l.line_number,l.category,l.severity,l.rules,substr(l.body,1,700) AS text,length(l.body) AS text_length,l.degraded,"
            "l.byte_offset,l.byte_length FROM lines l WHERE "
            + " AND ".join(where) + " ORDER BY l.line_number LIMIT ?",
            [*values, limit],
        ).fetchall()

    @staticmethod
    def _hit_lines(connection, file_id: int, category: str, match: str) -> int:
        """Matching lines of a file; with a line category only lines of it."""
        table = fts_table(file_id)
        if category in CATEGORIES:
            return connection.execute(
                f"SELECT COUNT(*) FROM lines l WHERE l.file_id=? AND l.category=? "
                f"AND l.line_number IN (SELECT rowid FROM {table} WHERE {table} MATCH ?)",
                (file_id, category, match)).fetchone()[0]
        return connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (match,)).fetchone()[0]

    def events(self, file_id: int, category: str = "all", query: str = "", after: int = 0, limit: int = 100) -> dict:
        """Lines in file order. With a query (parse_query) also terms, hit_lines
        (matching lines of the category in the whole file) and, for CJK, hint."""
        if category != "all" and category not in CATEGORIES:
            raise ValueError("未知分类")
        if not 1 <= limit <= 200 or after < 0 or len(query) > 256:
            raise ValueError("分页或搜索参数超出范围")
        search = parse_query(query) if query.strip() else None
        with self.connect() as connection:
            file = self._ready(connection, file_id)
            rows = self._line_rows(connection, file_id, category, search.match if search else "", after, limit + 1)
            hit_lines = 0
            if search:
                # A first page that holds every hit is its own count.
                hit_lines = (len(rows) if after == 0 and len(rows) <= limit
                             else self._hit_lines(connection, file_id, category, search.match))
        items = _with_text(file, rows[:limit], 700)
        result = {"items": items, "has_more": len(rows) > limit,
                  "next_after": items[-1]["line_number"] if items else after}
        if search:
            result.update(_search_fields(search, hit_lines))
        return result

    def search_workspace(self, query: str, category: str = "all", per_file: int = 3) -> dict:
        """One keyword query (parse_query) over every file of the workspace, in
        catalog order: per file its hit_lines and first `per_file` matching lines
        (rows as in events); only files with hits are listed. Files that cannot
        be searched now (not ready, indexed by an earlier version, source changed
        for store="offsets") are counted in `skipped`."""
        if category != "all" and category not in CATEGORIES:
            raise ValueError("未知分类")
        if not 1 <= per_file <= 20 or len(query) > 256:
            raise ValueError("分页或搜索参数超出范围")
        search = parse_query(query)
        if not search.match:
            raise ValueError(EMPTY_QUERY)
        catalog = self.catalog()
        files, total, skipped = [], 0, 0
        with self.connect() as connection:
            # One read snapshot (WAL) for every file: a file rebuilt meanwhile
            # keeps the state read here instead of losing its table mid-search.
            connection.execute("BEGIN")
            rows_by_id = {row["id"]: row for row in connection.execute("SELECT * FROM files")}
            for item in catalog:
                file = rows_by_id.get(item["id"])
                if (file is None or file["status"] != "ready" or not file["store"]
                        or (file["store"] == "offsets" and item["source_changed"])):
                    skipped += 1
                    continue
                rows = self._line_rows(connection, file["id"], category, search.match, 0, per_file + 1)
                if not rows:
                    continue
                hit_lines = (len(rows) if len(rows) <= per_file
                             else self._hit_lines(connection, file["id"], category, search.match))
                try:
                    lines = _with_text(file, rows[:per_file], 700)
                except ValueError:  # The source changed after catalog() looked at it.
                    skipped += 1
                    continue
                total += hit_lines
                files.append({"id": item["id"], "name": item["name"], "path": item["path"], "role": item["role"],
                              "session": item["session"], "session_label": item["session_label"],
                              "hit_lines": hit_lines, "items": lines})
        result = {"terms": list(search.terms)}
        if search.cjk:
            result["hint"] = CJK_HINT
        result.update(total_lines=total, file_count=len(files), skipped=skipped, files=files)
        return result

    def context(self, file_id: int, line: int, radius: int = 8) -> dict:
        if line < 1 or not 0 <= radius <= 50:
            raise ValueError("行号或上下文范围无效")
        with self.connect() as connection:
            file = self._ready(connection, file_id)
            if line > file["line_count"]:
                raise ValueError("行号超过文件范围")
            rows = connection.execute(
                "SELECT line_number,category,severity,rules,substr(body,1,16384) AS text,length(body) AS text_length,degraded,"
                "byte_offset,byte_length FROM lines WHERE file_id=? AND line_number BETWEEN ? AND ? ORDER BY line_number",
                (file_id, max(1, line - radius), line + radius),
            ).fetchall()
        return {"line": line, "sha256": file["sha256"], "encoding": file["encoding"],
                "encoding_fallback": bool(file["encoding_fallback"]), "encoding_basis": file["encoding_basis"],
                "encoding_basis_label": basis_label(file["encoding_basis"]),
                "decode_degraded_line_count": file["degraded_lines"],
                "source": file["path"], "items": _with_text(file, rows, 16384)}

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

    def blocks(self, file_id: int, category: str = "all", query: str = "", after: int = 0, limit: int = 50, *,
               addin_prefix: str = "", phase: str = "all", fold: bool = False) -> dict:
        """Page by entire event; FTS hits anywhere inside it select the event once.
        fold: blocks with the same fingerprint (same category and content apart
        from timestamps/counters) are listed once, at their first occurrence,
        with fold_count; a keyword search never folds.
        With a query (parse_query) every block has hit_line (its first matching
        line) and the result has terms, hint (CJK) and hit_lines: matching LINES
        (not blocks) of the whole file; with a line category only lines of that
        category, while add-in filters, the phase and addin_prefix are ignored."""
        if category != "all" and category not in BLOCK_FILTERS:
            raise ValueError("未知分类")
        if phase != "all" and phase not in PHASES:
            raise ValueError("未知阶段")
        if not 1 <= limit <= 200 or after < 0 or len(query) > 256 or len(addin_prefix) > 256:
            raise ValueError("分页或搜索参数超出范围")
        addin_prefix = addin_prefix.strip()
        search = parse_query(query) if query.strip() else None
        where, values = self._block_conditions(file_id, category, after, addin_prefix)
        if phase != "all":
            where.append("b.phase=?")
            values.append(phase)
        with self.connect() as connection:
            file = self._ready(connection, file_id)
            found: list[tuple] = []
            if search:
                found = self._search_blocks(connection, file_id, category, search.match, after, limit + 1,
                                            addin_prefix, phase)
                rows = [row for row, _ in found]
            elif fold and phase == "all":
                # Precomputed groups are exact here: category and add-in fields are
                # part of the content, so every filter keeps or drops a group whole.
                rows = connection.execute(
                    "SELECT b.*,coalesce(f.count,1) AS fold_count,f.last_line AS fold_last_line FROM blocks b "
                    "LEFT JOIN block_folds f ON f.file_id=b.file_id AND f.fingerprint=b.fingerprint WHERE "
                    + " AND ".join(where) + " AND (f.first_line IS NULL OR f.first_line=b.start_line) "
                    "ORDER BY b.start_line LIMIT ?", [*values, limit + 1]).fetchall()
            elif fold:
                # The same content can occur in several phases: fold within the filter.
                inner = [condition for condition in where if condition != "b.start_line>?"]
                inner_values = [value for value, condition in zip(values, where) if condition != "b.start_line>?"]
                rows = connection.execute(
                    "SELECT * FROM (SELECT b.*,COUNT(*) OVER w AS fold_count,MIN(b.start_line) OVER w AS fold_first,"
                    "MAX(b.start_line) OVER w AS fold_last_line FROM blocks b WHERE " + " AND ".join(inner)
                    + " WINDOW w AS (PARTITION BY b.fingerprint)) WHERE start_line=fold_first AND start_line>? "
                    "ORDER BY start_line LIMIT ?", [*inner_values, after, limit + 1]).fetchall()
            else:
                rows = connection.execute("SELECT b.* FROM blocks b WHERE " + " AND ".join(where)
                                          + " ORDER BY b.start_line LIMIT ?", [*values, limit + 1]).fetchall()
            items = _with_titles(connection, file, [self._block_dict(row) for row in rows[:limit]])
            if search:
                table = fts_table(file_id)
                for item, (_, hit_line) in zip(items, found):
                    if hit_line is None:  # Selected by an EXISTS test: look the first hit up.
                        hit_line = connection.execute(
                            f"SELECT rowid FROM {table} WHERE {table} MATCH ? AND rowid BETWEEN ? AND ? "
                            "ORDER BY rowid LIMIT 1", (search.match, item["start_line"], item["end_line"])).fetchone()[0]
                    item["hit_line"] = hit_line
                hit_lines = self._hit_lines(connection, file_id, category, search.match)
        result = {"items": items, "has_more": len(rows) > limit,
                  "next_after": items[-1]["start_line"] if items else after}
        if search:
            result.update(_search_fields(search, hit_lines))
        return result

    def block_hits(self, file_id: int, start_line: int, end_line: int, query: str, limit: int = 200) -> list[int]:
        """Matching line numbers (parse_query) between start_line and end_line,
        ascending, at most `limit`: next/previous hit inside an expanded block."""
        if start_line < 1 or end_line < start_line:
            raise ValueError("行号或上下文范围无效")
        if not 1 <= limit <= 200 or len(query) > 256:
            raise ValueError("分页或搜索参数超出范围")
        search = parse_query(query)
        with self.connect() as connection:
            file = self._ready(connection, file_id)
            if end_line > file["line_count"]:
                raise ValueError("行号超过文件范围")
            if not search.match:
                return []
            table = fts_table(file_id)
            return [row[0] for row in connection.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH ? AND rowid BETWEEN ? AND ? ORDER BY rowid LIMIT ?",
                (search.match, start_line, end_line, limit))]

    def fold_members(self, file_id: int, fingerprint: str, limit: int = 50) -> dict:
        """Start lines of every block sharing a fingerprint (expanding a folded entry)."""
        with self.connect() as connection:
            self._ready(connection, file_id)
            total = connection.execute("SELECT COUNT(*) FROM blocks WHERE file_id=? AND fingerprint=?",
                                       (file_id, fingerprint)).fetchone()[0]
            lines = [row[0] for row in connection.execute(
                "SELECT start_line FROM blocks WHERE file_id=? AND fingerprint=? ORDER BY start_line LIMIT ?",
                (file_id, fingerprint, limit))]
        return {"count": total, "start_lines": lines, "truncated": total > len(lines)}

    def tail_blocks(self, file_id: int, limit: int = 20, categories=("operation", "dialog")) -> list[dict]:
        """The last operations and dialogs before the journal ends, in file order.
        Jrn.Directive settings records are left out: Revit writes them itself."""
        if not 1 <= limit <= 200:
            raise ValueError("分页或搜索参数超出范围")
        with self.connect() as connection:
            file = self._ready(connection, file_id)
            rows = connection.execute(
                f"SELECT * FROM blocks WHERE file_id=? AND category IN ({','.join('?' * len(categories))}) "
                "AND title NOT LIKE 'Jrn.Directive%' ORDER BY start_line DESC LIMIT ?",
                (file_id, *categories, limit)).fetchall()
            return _with_titles(connection, file, [self._block_dict(row) for row in reversed(rows)])

    @staticmethod
    def _search_blocks(connection, file_id, category, phrase, after, limit, addin_prefix="", phase="all"):
        """(block row, first matching line in it) pairs; the line is None where
        an EXISTS test selected the block (blocks() looks it up)."""
        table = fts_table(file_id)  # rowid = line number
        condition, value = block_filter(category)
        phase_sql, phase_values = (" AND b.phase=?", [phase]) if phase != "all" else ("", [])
        hit_inside = f"EXISTS (SELECT 1 FROM {table} WHERE {table} MATCH ? AND rowid BETWEEN b.start_line AND b.end_line)"
        if addin_prefix:
            where, values = ViewerIndex._block_conditions(file_id, category, after, addin_prefix)
            return [(row, None) for row in connection.execute(
                "SELECT b.* FROM blocks b WHERE " + " AND ".join(where) + phase_sql + " AND " + hit_inside
                + " ORDER BY b.start_line LIMIT ?", [*values, *phase_values, phrase, limit])]
        cursor, result, previous = after, [], None
        while True:
            # The full-text hits drive the search: a few hits in millions of
            # lines are mapped to their blocks without scanning the lines.
            hits = [row[0] for row in connection.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH ? AND rowid>? ORDER BY rowid LIMIT 512",
                (phrase, cursor))]
            if category != "all" and cursor == after and len(hits) == 512:
                stats = connection.execute("SELECT counts FROM block_stats WHERE file_id=?", (file_id,)).fetchone()
                if stats and json.loads(stats[0]).get(category, 0) <= 5000:
                    # For a common word and a small category, test only those
                    # blocks instead of mapping every matching ordinary line.
                    return [(row, None) for row in connection.execute(
                        f"SELECT b.* FROM blocks b WHERE b.file_id=? AND {condition} AND b.start_line>?{phase_sql} "
                        f"AND {hit_inside} ORDER BY b.start_line LIMIT ?",
                        (file_id, value, after, *phase_values, phrase, limit))]
            for line in hits:
                if previous is not None and previous["start_line"] <= line <= previous["end_line"]:
                    continue
                previous = connection.execute(
                    "SELECT * FROM blocks WHERE file_id=? AND start_line<=? ORDER BY start_line DESC LIMIT 1",
                    (file_id, line),
                ).fetchone()
                # Hits come in order, and earlier hits in this block would have
                # mapped to it already: `line` is its first hit.
                if (previous and previous["start_line"] > after and matches_filter(previous, category)
                        and (phase == "all" or previous["phase"] == phase)):
                    result.append((previous, line))
                    if len(result) >= limit:
                        return result
            if len(hits) < 512:
                return result
            # Skip the rest of the last mapped block, even if it contains
            # millions of repetitions; it has already been accepted/rejected.
            cursor = max(hits[-1], previous["end_line"])

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
            return _with_titles(connection, file, [self._block_dict(row)])[0]

    def block_lines(self, file_id: int, start_line: int, offset: int = 0, limit: int = 50) -> dict:
        """Bound detail reads so expanding an enormous paragraph stays cheap."""
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("段落分页参数超出范围")
        block = self.block_at(file_id, start_line)
        if block["start_line"] != start_line:
            raise ValueError("请提供段落起始行")
        begin = start_line + offset
        with self.connect() as connection:
            file = self._ready(connection, file_id)
            rows = connection.execute(
                "SELECT line_number,substr(body,1,16384) AS text,length(body) AS text_length,degraded,"
                "byte_offset,byte_length FROM lines WHERE file_id=? AND line_number BETWEEN ? AND ? ORDER BY line_number",
                (file_id, begin, min(block["end_line"], begin + limit - 1)),
            ).fetchall()
        return {"block": block, "items": _with_text(file, rows, 16384),
                "has_more": begin + limit <= block["end_line"]}
