"""Streaming Journal event boundaries; retain ranges, never entire paragraphs.

Grouping is a reading aid, not proof that messages share a root cause. Blank
lines, timestamp records, API braces and VB continuation markers are evidence
for boundaries. Source lines remain independently addressable in the index.
"""

import hashlib
import re
from dataclasses import dataclass, field

from .models import SEVERITY_RANK
from .reader import JournalLine
from .loading import START_GATE, LoadingEvidence, is_addin_start
from .rules import fold

BLOCK_VERSION = "blocks-5-phase-fold"
_TIMESTAMP = re.compile(r"^\s*'[A-Z]\s+\d{1,2}-[A-Za-z]+-\d{4}\s+[\d:.]+;\s*(.*)")
_PREFIX = re.compile(r"^\s*'\s*(?:\d+:<\s*)?")
_API = re.compile(r"\bAPI_(ERROR|SUCCESS|INFO)\s*([{:])", re.I)
_COMMAND = re.compile(r"^\s*Jrn\.\w+\b")
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
_NORMALIZE = re.compile(r"^\s*'(?:[A-Z]\s+\d{1,2}-[A-Za-z]+-\d{4}\s+[\d:.]+;)?\s*(?:\d+\.\d+\s+)?(?:\d+:<+\s*)?")
_REPEATS = re.compile(r"\s*-\s*\d+\s+times\s*$")
PHASES = {
    "startup": "启动", "licensing": "许可", "addin_loading": "Add-in加载", "document_open": "打开文档",
    "interactive": "交互操作", "shutdown": "退出",
}
_JOINED_TITLE = 300  # a list title; the full record stays in the lines table
_LICENSING_MARKS = ("license initialization", "licenseupd(", "adlsdk")
_OPEN_COMMANDS = ("id_revit_file_open", "id_file_new_choose_template", "id_family_new", "id_appmenu_project_open",
                  "id_family_open")
# Records Revit writes on its own: settings and header data ('H records),
# add-in events, answers to dialogs and the automatic first ribbon tab.
_PASSIVE_JRN = re.compile(r"^\s*Jrn\.(?:Directive|AddInEvent|Data\b|RibbonEvent\s*\"TabActivated:)")


def payload(text: str) -> str:
    return _PREFIX.sub("", text).strip()


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


@dataclass
class JournalBlock:
    start_line: int
    end_line: int
    category: str
    severity: str = ""
    rules: set[str] = field(default_factory=set)
    title: str = ""
    title_line: int = 0
    boundary: str = "paragraph"
    complete: bool = True
    addin_origin: str = ""
    addin_evidence: list[dict] = field(default_factory=list)
    addin_name: str = ""
    addin_class: str = ""
    addin_identity_truncated: bool = False
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

    def _open(self, line: JournalLine, category: str, boundary: str) -> None:
        self.current = JournalBlock(line.number, line.number, category, boundary=boundary, phase=self.phase)
        self._digest = hashlib.blake2b(digest_size=8)

    def _advance_phase(self, text: str, folded: str, command, addin_start: bool) -> None:
        """Session phase from explicit markers; a reading aid, not a diagnosis."""
        phase = self.phase
        if phase == "shutdown":
            return
        has = folded.__contains__
        # Spelled out instead of any(map(...)): this runs on every line.
        if ("exitmanagedinstance" in folded or "finished recording journal file" in folded
                or "journal exit" in folded or "license shutdown starting" in folded):
            self.phase = "shutdown"
        elif "openfrommodelpath" in folded or (command and any(map(has, _OPEN_COMMANDS))):
            self.phase = "document_open"
        elif command and not _PASSIVE_JRN.match(text):
            # A user action ends start-up; answering the open dialog (Jrn.Data) does not end opening.
            self.phase = "interactive"
        elif phase == "startup" and any(map(has, _LICENSING_MARKS)):
            self.phase = "licensing"
        elif phase in ("startup", "licensing") and addin_start:
            self.phase = "addin_loading"

    def _flush(self, complete: bool = True) -> list[JournalBlock]:
        if self.current is None:
            return []
        block = self.current
        block.complete = complete
        block.addin_origin, block.addin_evidence = self.loading.finish()
        block.addin_name = self.loading.name
        block.addin_class = self.loading.class_name
        block.addin_identity_truncated = self.loading.identity_truncated
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
        self.loading = LoadingEvidence()
        return [block]

    def _add(self, line: JournalLine, category: str, severity: str, rules: str,
             content: str | None = None, folded: str | None = None) -> None:
        """content/folded are payload(line.text)/fold(line.text) when the caller has them."""
        block = self.current
        assert block is not None
        block.end_line = line.number
        if content is None:
            content = payload(line.text)
        self._digest.update(normalized_from(line.text, content).encode("utf-8", "surrogatepass") + b"\n")
        self.loading.feed(line.number, line.text, folded)
        if block.category == "other" and category != "other":
            block.category = category
        elif category == "dialog" and block.category == "operation":
            # Jrn.Data  _ whose continuation names "TaskDialogResult": the dialog's answer.
            block.category = "dialog"
        if severity and SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(block.severity, 0):
            block.severity = severity
        if rules:
            block.rules.update(rule for rule in rules.split(",") if rule)
        if not block.title:
            if content is None:
                content = payload(line.text)
            if content:
                block.title = content[:700]
                block.title_line = line.number
        elif self.continued and block.title.endswith("_") and len(block.title) < _JOINED_TITLE:
            # "Jrn.Data  _" says nothing; the continuation lines name the record.
            if content is None:
                content = payload(line.text)
            block.title = (block.title[:-1].rstrip() + " " + content)[:_JOINED_TITLE]

    def feed(self, line: JournalLine, classification: tuple[str, str, str]) -> list[JournalBlock]:
        category, severity, rules = classification
        text = line.text
        content = payload(text)
        folded = fold(text)
        # Each shape test runs its regex only when its literal gate is present;
        # all of them are pure functions of this line, so evaluating them once
        # up front is equivalent to the original lazy, repeated evaluation.
        has = folded.__contains__
        timestamp = _TIMESTAMP.match(text)
        api = _API.search(text) if any(map(has, _API_GATE)) else None
        command = _COMMAND.match(text)
        metric = any(map(has, _METRIC_GATE)) and bool(_METRIC.search(content))
        section = any(map(has, _SECTION_GATE)) and bool(_SECTION.search(content))
        strong = any(map(has, _STRONG_GATE)) and bool(_STRONG_ERROR.search(content))
        addin_start = any(map(has, START_GATE)) and is_addin_start(text, folded)
        native_start = any(map(has, _NATIVE_GATE)) and bool(_NATIVE_START.search(text))
        self._advance_phase(text, folded, command, addin_start)
        if self.pending_timestamp and self.current is not None:
            # The empty timestamp record belongs to the command it introduces.
            self.current.phase = self.phase
        blank = not content
        result: list[JournalBlock] = []

        # Native exception records often put the code on a timestamped next
        # line; that timestamp must not break the same exception into two rows.
        native_code = any(map(has, _NATIVE_CODE_GATE)) and bool(_NATIVE_CODE.search(text))
        if (self.current and self.native_pending and any(map(has, _NATIVE_GATE)) and _NATIVE_DETAIL.search(text)
                and not (native_code and self.native_code)):
            self._add(line, category, severity, rules, content, folded)
            self.native_code |= native_code
            return result

        # Explicit new records recover an unclosed brace/continuation block.
        if self.current and (self.api_depth or self.continued):
            if not (timestamp or api or command or strong or addin_start):
                self._add(line, category, severity, rules, content, folded)
                if self.api_depth:
                    self.api_depth += text.count("{") - text.count("}")
                    if self.api_depth <= 0:
                        result += self._flush()
                else:
                    self.continued = text.rstrip().endswith("_")
                    if not self.continued:
                        result += self._flush()
                return result
            result += self._flush(complete=False)

        if timestamp:
            result += self._flush()
            self._open(line, category, "timestamp")
            self.kind = "paragraph"
            self._add(line, category, severity, rules, content, folded)
            self.native_pending = native_start or native_code
            self.native_code = native_code
            # Empty 'H/'E records introduce the following command or API block.
            rest = _RECORD_PREFIX.sub("", timestamp.group(1)).strip()
            self.pending_timestamp = not rest
            if not rest:
                self.current.title = ""
            if not api:
                return result
            self.pending_timestamp = True

        if blank:
            if self.current is None:
                self._open(line, "other", "blank")
                self.kind = "blank"
            self._add(line, category, severity, rules, content, folded)
            if self.kind != "blank":
                result += self._flush()
            return result

        exception = any(map(content.__contains__, _EXCEPTION_GATE)) and bool(_EXCEPTION.search(content))
        stack = bool(_STACK.search(content))
        indented = bool(_INDENTED.match(text))
        error_followup = bool(
            self.current and self.current.category in {"error", "api_error", "warning"}
            and not api and not command and not strong
            and (stack or (exception and not self.has_exception) or (indented and category == "other"))
        )
        boundary = "paragraph"
        if api:
            boundary = "api_braces" if api.group(2) == "{" else "api_record"
            category = "api_error" if api.group(1).upper() == "ERROR" else "api"
        elif command:
            boundary = "journal_command"
        elif metric:
            boundary = "resource_sample"
        elif section:
            boundary = "system_section"
            category = "system"
        elif addin_start:
            boundary = "addin_load"
        elif category == "dialog":
            boundary = "dialog"
        elif category in {"error", "api_error", "warning"} and not error_followup:
            boundary = "error_record"

        new_record = boundary != "paragraph"
        if metric and self.kind == "resource_sample" and "GUI Resource Usage" in content:
            new_record = False
        if self.current and not self.pending_timestamp:
            changed_category = category != "other" and category != self.current.category
            if (self.kind == "blank" or new_record or (changed_category and not error_followup)
                    or (self.current.category in {"error", "api_error", "warning"} and not error_followup)):
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
        self._add(line, category, severity, rules, content, folded)
        self.has_exception |= exception
        self.native_pending |= native_start or native_code
        self.native_code |= native_code

        if api:
            self.api_depth = max(0, text.count("{") - text.count("}")) if api.group(2) == "{" else 0
            if not self.api_depth and api.group(2) == "{":
                result += self._flush()
        elif command:
            self.continued = text.rstrip().endswith("_")
            if not self.continued:
                result += self._flush()
        elif self.kind == "resource_sample" and "GUI Resource Usage" in content:
            result += self._flush()
        return result

    def finish(self) -> list[JournalBlock]:
        return self._flush(complete=not (self.api_depth or self.continued))
