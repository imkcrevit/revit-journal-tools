"""Streaming Journal event boundaries; retain ranges, never entire paragraphs.

Grouping is a reading aid, not proof that messages share a root cause. Blank
lines, timestamp records, API braces and VB continuation markers are evidence
for boundaries. Source lines remain independently addressable in the index.
"""

import hashlib
import re
from dataclasses import dataclass, field

from .categories import _COMMAND, START_GATE, _union, is_addin_start, phase_marker
from .models import SEVERITY_RANK
from .reader import JournalLine
from .loading import _MAX_IDENTITY, LoadingEvidence, addin_record, registered_server, server_id, vendor_id_origin
from .rules import fold

BLOCK_VERSION = "blocks-7-addin-kinds"
_TIMESTAMP = re.compile(r"^\s*'[A-Z]\s+\d{1,2}-[A-Za-z]+-\d{4}\s+[\d:.]+;\s*(.*)")
_PREFIX = re.compile(r"^\s*'\s*(?:\d+:<\s*)?")
_API = re.compile(r"\bAPI_(ERROR|SUCCESS|INFO)\s*([{:])", re.I)
_METRIC = re.compile(r"\b(?:(?:Initial|Delta)\s+VM:|VM:\s*Avail|GUI Resource Usage)", re.I)
_SECTION = re.compile(r"\b(?:SystemSettings|System Information|Log Summary)\s*:?(?:\s*$)", re.I)
_STACK = re.compile(r"^(?:at\s+|--->|--- End of|Caused by:|Stack\s*trace\s*:|File\s+\"|\.{3}\s+\d+\s+more)", re.I)
_EXCEPTION = re.compile(r"\b\w*(?:Exception|Error)\s*:")
_STRONG_ERROR = re.compile(r"\b(?:DBG_ERROR|ERROR|ExceptionCode|Assertion failed)\s*[:=]|\bfatal error\b", re.I)
_NATIVE_START = re.compile(r"\b(?:Exception occurred|System exception in)\b", re.I)
_NATIVE_DETAIL = re.compile(r"\bException(?:Code|Flags|Address)\s*=", re.I)
_NATIVE_CODE = re.compile(r"\bExceptionCode\s*=", re.I)
_RECORD_PREFIX = re.compile(r"^\d+:<\s*")
_INDENTED = re.compile(r"^\s{2,}\S")
# Literal gates checked against rules.fold(text) before each case-insensitive
# regex: every match contains one of these, so results never change
# (tests/test_categories_gates.py). _EXCEPTION is case-sensitive.
_API_GATE = ("api_",)
_METRIC_GATE = ("vm:", "gui resource usage")
_SECTION_GATE = ("systemsettings", "system information", "log summary")
_STRONG_GATE = ("error", "exceptioncode", "assertion failed")
_NATIVE_GATE = ("exception",)
_NATIVE_CODE_GATE = ("exceptioncode",)
_EXCEPTION_GATE = ("Exception", "Error")

# Most lines contain none of the terms of any shape gate: one pass rules out
# every shape test (and the case-sensitive _EXCEPTION_GATE, whose folded terms
# are included) before the per-shape gates run.
_SHAPE_GATE = _union(_API_GATE, _METRIC_GATE, _SECTION_GATE, _STRONG_GATE, START_GATE, _NATIVE_GATE,
                     _NATIVE_CODE_GATE, _EXCEPTION_GATE)
_ERROR_CATEGORIES = frozenset({"error", "api_error", "warning"})
# Categories that outrank "addin" (categories.Categorizer): an add-in record
# that is also one of these is still described when it is a block's title line.
_OUTRANKING_ADDIN = frozenset({"error", "api_error", "warning", "dialog"})
# Stage defaults (and explicit start-up/exit markers) say where a line is, not
# what it is: a block takes its first other category and they never split it.
_WEAK = frozenset({"other", "startup", "exit"})
_NORMALIZE = re.compile(r"^\s*'(?:[A-Z]\s+\d{1,2}-[A-Za-z]+-\d{4}\s+[\d:.]+;)?\s*(?:\d+\.\d+\s+)?(?:\d+:<+\s*)?")
_REPEATS = re.compile(r"\s*-\s*\d+\s+times\s*$")
PHASES = {
    "startup": "启动", "licensing": "许可", "addin_loading": "Add-in加载", "document_open": "打开文档",
    "interactive": "交互操作", "shutdown": "退出",
}
_JOINED_TITLE = 300  # a list title; the full record stays in the lines table
_LICENSING_MARKS = ("license initialization", "licenseupd(", "adlsdk")


def payload(text: str) -> str:
    return _PREFIX.sub("", text).strip()


def join_title(title: str, content: str) -> str:
    """'Jrn.Data  _' says nothing; each continuation line extends the title."""
    return (title[:-1].rstrip() + " " + content)[:_JOINED_TITLE]


def derive_title(texts: list[str]) -> str:
    """A title_from_line title from its title line and the title_joins lines after it."""
    title = payload(texts[0])[:700]
    for text in texts[1:]:
        title = join_title(title, payload(text))
    return title


def normalized(text: str) -> str:
    """Line content for fold fingerprints: no timestamp, level prefix or repeat counter."""
    text = _NORMALIZE.sub("", text, count=1)
    if "times" in text:
        text = _REPEATS.sub("", text)
    return text.strip()


def normalized_from(text: str, content: str) -> str:
    """normalized(text) given content == payload(text), skipping the regex when
    the payload cannot start with a timestamp, elapsed time or extra '<'."""
    first = content[:1]
    if first and (first.isdigit() or first == "<" or (first.isupper() and content[1:2].isspace())):
        return normalized(text)
    if "times" in content:
        return _REPEATS.sub("", content).strip()
    return content


def _shape(text: str) -> tuple:
    """Everything BlockBuilder.feed needs from the text alone (unpacked there in
    this order). Each shape test runs its regex only when its literal gate is present."""
    content = payload(text)
    folded = fold(text)
    has = folded.__contains__
    timestamp = _TIMESTAMP.match(text)
    command = _COMMAND.match(text) is not None
    api = api_open = api_error = False
    if any(map(has, _SHAPE_GATE)):
        match = _API.search(text) if any(map(has, _API_GATE)) else None
        if match:
            api, api_open, api_error = True, match.group(2) == "{", match.group(1).upper() == "ERROR"
        metric = any(map(has, _METRIC_GATE)) and bool(_METRIC.search(content))
        section = any(map(has, _SECTION_GATE)) and bool(_SECTION.search(content))
        strong = any(map(has, _STRONG_GATE)) and bool(_STRONG_ERROR.search(content))
        start_gate = any(map(has, START_GATE))
        addin_start = start_gate and is_addin_start(text, folded)
        native_gate = any(map(has, _NATIVE_GATE))
        native_start = native_gate and bool(_NATIVE_START.search(text))
        native_detail = native_gate and bool(_NATIVE_DETAIL.search(text))
        native_code = any(map(has, _NATIVE_CODE_GATE)) and bool(_NATIVE_CODE.search(text))
        exception = any(map(content.__contains__, _EXCEPTION_GATE)) and bool(_EXCEPTION.search(content))
    else:
        metric = section = strong = start_gate = addin_start = False
        native_start = native_detail = native_code = exception = False
    return (
        content, normalized_from(text, content).encode("utf-8", "surrogatepass") + b"\n",
        timestamp is not None,
        # Empty 'H/'E records introduce the following command or API block.
        timestamp is not None and not _RECORD_PREFIX.sub("", timestamp.group(1)).strip(),
        command, api, api_open, api_error, metric, section, strong, start_gate, addin_start,
        native_start, native_detail, native_code, exception,
        phase_marker(text, folded, command), any(map(has, _LICENSING_MARKS)),
        text.rstrip().endswith("_"), text.count("{") - text.count("}"), "GUI Resource Usage" in content,
    )


# Like Categorizer results, about half of a Journal's lines repeat an earlier
# text; _shape is pure, so feed caches it. Bounded: cleared when full.
_SHAPE_CACHE_LIMIT = 16_384
_NO_RECORD = object()  # not yet looked up in BlockBuilder._records
# Positions in a _shape tuple, for the lines BlockBuilder holds back.
_AT_CONTENT, _AT_DIGEST, _AT_EMPTY_RECORD, _AT_START_GATE = 0, 1, 3, 11
_AT_ADDIN_START, _AT_MARKER, _AT_LICENSING = 12, 17, 18


@dataclass
class JournalBlock:
    start_line: int
    end_line: int
    category: str
    severity: str = ""
    rules: set[str] = field(default_factory=set)
    title: str = ""
    title_line: int = 0
    # The title is derive_title() of title_line and the title_joins lines after
    # it: an index that does not store line text reads it back from the source (J9).
    title_from_line: bool = False
    title_joins: int = 0
    boundary: str = "paragraph"
    complete: bool = True
    addin_origin: str = ""
    addin_evidence: list[dict] = field(default_factory=list)
    addin_name: str = ""
    addin_class: str = ""
    addin_identity_truncated: bool = False
    addin_kind: str = ""  # loading.ADDIN_KINDS; "" for blocks that are no add-in record
    phase: str = ""
    fingerprint: str = ""


class BlockBuilder:
    """One active range and a bounded title/rules set, even for huge blocks."""

    def __init__(self):
        self.current: JournalBlock | None = None
        self.kind = ""
        self.api_depth = 0
        self.continued = False
        self.pending_timestamp = False
        self.has_exception = False
        self.native_pending = False
        self.native_code = False
        self.loading = LoadingEvidence()
        self.phase = "startup"
        self._digest = None
        self._shapes: dict[str, tuple] = {}
        self._records: dict[str, tuple | None] = {}  # loading.addin_record of add-in lines, bounded like _shapes
        # A closed "Registered an external server" API record stays current for
        # its [Jrn.RegisterExternalServer] record, which follows it directly or
        # after an empty 'E/'H timestamp line (held meanwhile as (line,
        # classification, shape)).
        self.server_closed = False
        self.server_id = ""
        self.held: tuple | None = None

    def _open(self, line: JournalLine, category: str, boundary: str) -> None:
        self.current = JournalBlock(line.number, line.number, category, boundary=boundary, phase=self.phase)
        self._digest = hashlib.blake2b(digest_size=8)

    def _advance_phase(self, marker: str, licensing: bool, addin_start: bool) -> None:
        """Session phase from explicit markers; a reading aid, not a diagnosis."""
        phase = self.phase
        if phase == "shutdown":
            return
        if marker:
            self.phase = marker
        elif phase == "startup" and licensing:
            self.phase = "licensing"
        elif phase in ("startup", "licensing") and addin_start:
            self.phase = "addin_loading"

    def _flush(self, complete: bool = True) -> list[JournalBlock]:
        if self.current is None:
            return []
        block = self.current
        block.complete = complete
        loading = self.loading
        if loading.active:
            # An inactive LoadingEvidence holds nothing (the block keeps its
            # empty add-in defaults) and is reused for the next block.
            block.addin_origin, block.addin_evidence = loading.finish()
            block.addin_name = loading.name
            block.addin_class = loading.class_name
            block.addin_identity_truncated = loading.identity_truncated
            block.addin_kind = "load"
            self.loading = LoadingEvidence()
        # Same category and same content apart from timestamps, level prefixes
        # and "- N times" counters: the list folds such blocks into one entry.
        block.fingerprint = hashlib.blake2b(block.category.encode() + b"\0" + self._digest.digest(),
                                            digest_size=8).hexdigest()
        self.current = None
        self.kind = ""
        self.api_depth = 0
        self.continued = False
        self.pending_timestamp = False
        self.has_exception = False
        self.native_pending = False
        self.native_code = False
        self.server_closed = False
        self.server_id = ""
        return [block]

    def _record(self, text: str) -> tuple | None:
        record = self._records.get(text, _NO_RECORD)
        if record is _NO_RECORD:
            record = addin_record(text)
            if len(self._records) >= _SHAPE_CACHE_LIMIT:
                self._records.clear()
            self._records[text] = record
        return record

    def _describe(self, block: JournalBlock, line: JournalLine) -> None:
        """Kind, name, class and origin of an add-in record other than a load
        (LoadingEvidence reads those): its own text only, never a DLL path."""
        record = self._record(line.text)
        if record is None:
            return
        kind, name, vendor, class_name = record
        origin, basis = vendor_id_origin(vendor)
        block.addin_kind, block.addin_origin = kind, origin
        block.addin_evidence = [{"line_number": line.number, "value": vendor[:400], "basis": basis}]
        block.addin_name, block.addin_class = name[:_MAX_IDENTITY], class_name[:_MAX_IDENTITY]
        block.addin_identity_truncated = len(name) > _MAX_IDENTITY or len(class_name) > _MAX_IDENTITY
        if kind == "server":
            self.server_id = server_id(line.text)

    def _close_api(self) -> list[JournalBlock]:
        """The braces of the current API record closed: a server registration
        waits for its journal record (feed), anything else ends here."""
        if self.current.addin_kind == "server":
            self.server_closed = True
            return []
        return self._flush()

    def _add(self, line: JournalLine, category: str, severity: str, rules: str,
             content: str, digest: bytes, start_gate: bool) -> None:
        """content: payload(line.text); digest: its fold-fingerprint bytes;
        start_gate: the line contains a START_GATE term."""
        block = self.current
        assert block is not None
        block.end_line = line.number
        self._digest.update(digest)
        if start_gate or self.loading.active:
            # Without the gate an inactive LoadingEvidence.feed returns at once.
            self.loading.feed(line.number, line.text)
        if block.category in _WEAK and category not in _WEAK:
            block.category = category
        elif category == "dialog" and block.category == "operation":
            # Jrn.Data  _ whose continuation names "TaskDialogResult": the dialog's answer.
            block.category = "dialog"
        if severity and SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(block.severity, 0):
            block.severity = severity
        if rules:
            block.rules.update(rule for rule in rules.split(",") if rule)
        if not block.title:
            if content:
                block.title = content[:700]
                block.title_line = line.number
                block.title_from_line = True
        elif self.continued and block.title.endswith("_") and len(block.title) < _JOINED_TITLE:
            # "Jrn.Data  _" says nothing; the continuation lines name the record.
            block.title = join_title(block.title, content)
            block.title_joins += 1
        # The block's first add-in record line describes it: an add-in line, or
        # its title line when that is also an error, warning or dialog.
        if (not block.addin_kind and not self.loading.active
                and (category == "addin" or (category in _OUTRANKING_ADDIN and block.title_line == line.number))):
            self._describe(block, line)

    def feed(self, line: JournalLine, classification: tuple[str, str, str]) -> list[JournalBlock]:
        text = line.text
        # All of these are pure functions of the text, so evaluating them once
        # up front is equivalent to the original lazy, repeated evaluation.
        shape = self._shapes.get(text)
        if shape is None:
            shape = _shape(text)
            if len(self._shapes) >= _SHAPE_CACHE_LIMIT:
                self._shapes.clear()
            self._shapes[text] = shape
        if self.server_closed:
            return self._after_server(line, classification, shape)
        return self._feed(line, classification, shape)

    def _after_server(self, line: JournalLine, classification: tuple[str, str, str], shape: tuple) -> list[JournalBlock]:
        """API_SUCCESS { Registered an external server: ...; ServerId(X) ... },
        'E <timestamp>; 0:< and [Jrn.RegisterExternalServer] ... ExternalServer: X
        are one block, as is the record followed directly by its journal record
        (worker logs, mostly without ID). The held line's phase moves when it
        arrives; only its block waits for the next line."""
        held = self.held
        if held is None and shape[_AT_EMPTY_RECORD] and line.text.lstrip()[1:2] in ("E", "H"):
            self._advance_phase(shape[_AT_MARKER], shape[_AT_LICENSING], shape[_AT_ADDIN_START])
            self.held = (line, classification, shape)
            return []
        self.held = None
        registered = registered_server(line.text)
        if registered is not None and (not registered or registered.casefold() == self.server_id.casefold()):
            self._advance_phase(shape[_AT_MARKER], shape[_AT_LICENSING], shape[_AT_ADDIN_START])
            lines = [held, (line, classification, shape)] if held else [(line, classification, shape)]
            for item, (category, severity, rules), item_shape in lines:
                self._add(item, category, severity, rules, item_shape[_AT_CONTENT], item_shape[_AT_DIGEST],
                          item_shape[_AT_START_GATE])
            return self._flush()
        result = self._flush()
        if held is not None:
            # Not its journal record: the timestamp line opens the next block as usual.
            result += self._feed(*held, advance=False)
        return result + self._feed(line, classification, shape)

    def _feed(self, line: JournalLine, classification: tuple[str, str, str], shape: tuple,
              advance: bool = True) -> list[JournalBlock]:
        category, severity, rules = classification
        text = line.text
        (content, digest, timestamp, empty_record, command, api, api_open, api_error, metric, section, strong,
         start_gate, addin_start, native_start, native_detail, native_code, exception, marker, licensing,
         continued, braces, gui_usage) = shape
        if advance:
            self._advance_phase(marker, licensing, addin_start)
        if self.pending_timestamp and self.current is not None:
            # The empty timestamp record belongs to the command it introduces.
            self.current.phase = self.phase
        blank = not content
        result: list[JournalBlock] = []

        # Native exception records often put the code on a timestamped next
        # line; that timestamp must not break the same exception into two rows.
        if self.current and self.native_pending and native_detail and not (native_code and self.native_code):
            self._add(line, category, severity, rules, content, digest, start_gate)
            self.native_code |= native_code
            return result

        # Explicit new records recover an unclosed brace/continuation block.
        if self.current and (self.api_depth or self.continued):
            if not (timestamp or api or command or strong or addin_start):
                self._add(line, category, severity, rules, content, digest, start_gate)
                if self.api_depth:
                    self.api_depth += braces
                    if self.api_depth <= 0:
                        result += self._close_api()
                else:
                    self.continued = continued
                    if not self.continued:
                        result += self._flush()
                return result
            result += self._flush(complete=False)

        if timestamp:
            result += self._flush()
            self._open(line, category, "timestamp")
            self.kind = "paragraph"
            self._add(line, category, severity, rules, content, digest, start_gate)
            self.native_pending = native_start or native_code
            self.native_code = native_code
            self.pending_timestamp = empty_record
            if empty_record:
                self.current.title = ""
                self.current.title_from_line = False
                self.current.title_joins = 0
            if not api:
                return result
            self.pending_timestamp = True

        if blank:
            if self.current is None:
                self._open(line, "other", "blank")
                self.kind = "blank"
            self._add(line, category, severity, rules, content, digest, start_gate)
            if self.kind != "blank":
                result += self._flush()
            return result

        # The stack and indentation tests only matter after an error record.
        error_followup = bool(
            self.current and self.current.category in _ERROR_CATEGORIES
            and not api and not command and not strong
            and (_STACK.search(content) or (exception and not self.has_exception)
                 or (category in _WEAK and _INDENTED.match(text)))
        )
        boundary = "paragraph"
        if api:
            boundary = "api_braces" if api_open else "api_record"
            # Add-in records (loading starts, external servers) stay add-in blocks.
            category = "api_error" if api_error else ("addin" if addin_start or category == "addin" else "api")
        elif command:
            boundary = "journal_command"
        elif metric:
            boundary = "resource_sample"
        elif section:
            boundary = "system_section"
        elif addin_start:
            boundary = "addin_load"
        elif category == "addin" and self._record(text) is not None:
            # Every add-in record (manifest, assembly, updater, ...) is its own
            # block, named after it; unclaimed lines after it stay with it.
            boundary = "addin_record"
        elif category == "dialog":
            boundary = "dialog"
        elif category in _ERROR_CATEGORIES and not error_followup:
            boundary = "error_record"

        new_record = boundary != "paragraph"
        if metric and self.kind == "resource_sample" and gui_usage:
            new_record = False
        if self.current and not self.pending_timestamp:
            changed_category = category not in _WEAK and category != self.current.category
            if (self.kind == "blank" or new_record or (changed_category and not error_followup)
                    or (self.current.category in _ERROR_CATEGORIES and not error_followup)):
                result += self._flush()

        if self.current is None:
            self._open(line, category, boundary)
            self.kind = boundary
        elif self.pending_timestamp:
            if category != "other":
                self.current.category = category
            if new_record:
                self.current.boundary = "timestamp+" + boundary
                self.kind = boundary
        self.pending_timestamp = False
        self._add(line, category, severity, rules, content, digest, start_gate)
        self.has_exception |= exception
        self.native_pending |= native_start or native_code
        self.native_code |= native_code

        if api:
            self.api_depth = max(0, braces) if api_open else 0
            if not self.api_depth and api_open:
                result += self._close_api()
        elif command:
            self.continued = continued
            if not self.continued:
                result += self._flush()
        elif self.kind == "resource_sample" and gui_usage:
            result += self._flush()
        return result

    def finish(self) -> list[JournalBlock]:
        result = []
        if self.held is not None:
            # The file ended on the empty timestamp line: it is its own block.
            held, self.held = self.held, None
            result += self._flush() + self._feed(*held, advance=False)
        return result + self._flush(complete=not (self.api_depth or self.continued))
