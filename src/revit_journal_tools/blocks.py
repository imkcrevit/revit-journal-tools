"""Streaming Journal event boundaries; retain ranges, never entire paragraphs.

Grouping is a reading aid, not proof that messages share a root cause. Blank
lines, timestamp records, API braces and VB continuation markers are evidence
for boundaries. Source lines remain independently addressable in the index.
"""

import re
from dataclasses import dataclass, field

from .models import SEVERITY_RANK
from .reader import JournalLine
from .loading import START_GATE, LoadingEvidence, is_addin_start
from .rules import fold

BLOCK_VERSION = "blocks-3-addin-list"
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


def payload(text: str) -> str:
    return _PREFIX.sub("", text).strip()


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

    def _flush(self, complete: bool = True) -> list[JournalBlock]:
        if self.current is None:
            return []
        block = self.current
        block.complete = complete
        block.addin_origin, block.addin_evidence = self.loading.finish()
        block.addin_name = self.loading.name
        block.addin_class = self.loading.class_name
        block.addin_identity_truncated = self.loading.identity_truncated
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
        self.loading.feed(line.number, line.text, folded)
        if block.category == "other" and category != "other":
            block.category = category
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
            self.current = JournalBlock(line.number, line.number, category, boundary="timestamp")
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
                self.current = JournalBlock(line.number, line.number, "other", boundary="blank")
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
            self.current = JournalBlock(line.number, line.number, category, boundary=boundary)
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
