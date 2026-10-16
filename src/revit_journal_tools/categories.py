"""Viewer record categories, separate from incident severity and session phases."""

import re

from .models import SEVERITY_RANK
from .rules import Rule, fold, match_rules

CATEGORIES = {
    "startup": "启动", "addin": "Add-in", "exit": "退出", "error": "错误",
    "api_error": "API错误", "operation": "操作内容", "warning": "警告",
    "api": "API活动", "dialog": "对话框", "other": "其他记录",
}
# TaskDialog "..." is the message the user saw; Jrn.Data "TaskDialogResult" the answer.
_TASK_DIALOG = re.compile(r"\bTaskDialog\s*\"")
_DIALOG_RESULT = re.compile(r"\"TaskDialogResult\"")
_DIALOG_GATE = ("taskdialog",)  # both dialog regexes are case-sensitive: exact
_JOURNAL = re.compile(r"\bJrn\.(?:Command|Data|RibbonEvent|Mouse\w*|Key\w*|\w*Button|ComboBox|Edit|Directive|CheckBox|ListBox|TreeCtrl)\b", re.I)
_STARTUP = re.compile(r"started recording journal|InitApplication|InitNativeInstance|Starting Revit|ApplicationInitialized|^'\s*(?:Build|Branch|Release):|Set Jrn =", re.I)
# Add-in records in any phase: manifests, external servers/services, add-in
# events and updaters. Loading starts ("Starting External Application: ...")
# are is_addin_start below; Jrn.AddInEvent is the add-in's own UI, not a command.
_ADDIN = re.compile(
    r"\[Jrn\.(?:AddInManifest|RegisterExternalServer|AddInDynamicUpdate|ExternalEventExecution"
    r"|ExternalServerExecution|ExternalResourceLoad)\]|\bJrn\.AddInEvent\b"
    r"|\bRegistered an external (?:service|server)\b|\bUnregistering all external services\b"
    r"|\bThe add-in folder\b|\bAdd-in manifest file\b|\bThe requested assembly\b.*\bwas loaded to the context\b"
    r"|\bExternalDBApplication\b|\bAddInJournalClient\b|\bThird Party Updater\b"
    r"|\bForcibly unregistering Updater\b|\bForcibly removing all update triggers\b", re.I)
# What Revit writes while it shuts down (license, journal and memory summary, CEF).
_EXIT = re.compile(
    r"ExitManagedInstance|finished recording journal file|License shutdown starting"
    r"|\b(?:journal|worker log)\(s\) aged out\b|Total session time spent reclaiming memory"
    r"|CefSharp has been shutdown", re.I)
_RECORD = re.compile(r"^\s*'[A-Z]\s+\d{1,2}-[A-Za-z]+-\d{4}\s")
_API_ERROR = re.compile(r"\bAPI_ERROR\s*[:{]", re.I)
_COMMAND = re.compile(r"^\s*Jrn\.\w+\b")
# Literal gates (checked against rules.fold(text)): every match of the regex
# contains at least one of these, so skipping the regex never changes a result.
# tests/test_categories_gates.py checks each gate against its regex.
_JOURNAL_GATE = ("jrn.",)  # also the gate of the case-sensitive _COMMAND
_STARTUP_GATE = (
    "started recording journal", "initapplication", "initnativeinstance", "starting revit",
    "applicationinitialized", "build:", "branch:", "release:", "set jrn =",
)
_ADDIN_GATE = (
    "jrn.addin", "jrn.registerexternalserver", "jrn.external", "registered an external",
    "unregistering all external services", "the add-in folder", "add-in manifest file", "the requested assembly",
    "externaldbapplication", "addinjournalclient", "third party updater", "forcibly unregistering updater",
    "forcibly removing all update triggers",
)
_EXIT_GATE = (
    "exitmanagedinstance", "finished recording journal file", "license shutdown starting", "(s) aged out",
    "total session time spent reclaiming memory", "cefsharp has been shutdown",
)


def _union(*gates: tuple[str, ...]) -> tuple[str, ...]:
    """Folded terms of several gates; a term containing another is redundant."""
    terms = {term.lower() for gate in gates for term in gate}
    return tuple(sorted(term for term in terms if not any(other != term and other in term for other in terms)))


# Add-in loading records ("Starting External Application: ..."), shared with
# loading.LoadingEvidence and blocks; defined here so that loading.py can
# import CATEGORIES without an import cycle.
_START = re.compile(
    r"^(?:Starting|Creating|Created|Loading|Loaded)\s+(?:an?\s+)?"
    r"(?:External\s+(?:DB\s+)?Application|Add-?in)\s*:", re.I,
)
_PREFIX = re.compile(r"^\s*'\s*(?:\d+:<\s*)?")
_TIMESTAMP = re.compile(r"^\s*'[A-Z]\s+\d{1,2}-[A-Za-z]+-\d{4}\s+[\d:.]+;\s*(?:\d+:<\s*)?")
_API_PREFIX = re.compile(r"^API_(?:SUCCESS|ERROR|INFO)\s*[:{]\s*", re.I)
# Every _START match contains one of these (checked against rules.fold(text)).
START_GATE = ("application", "addin", "add-in")
# Most lines contain no term of the loading-start, add-in, start-up and exit
# gates: one pass rules out all four tests.
_MARKER_GATE = _union(START_GATE, _ADDIN_GATE, _STARTUP_GATE, _EXIT_GATE)

# Session phase markers, shared by Categorizer (its stage) and
# blocks.BlockBuilder (PHASES), so both move on the same line.
_OPEN_COMMANDS = ("id_revit_file_open", "id_file_new_choose_template", "id_family_new", "id_appmenu_project_open",
                  "id_family_open")
# Records Revit writes on its own: settings and header data ('H records),
# add-in events, answers to dialogs and the automatic first ribbon tab.
_PASSIVE_JRN = re.compile(r"^\s*Jrn\.(?:Directive|AddInEvent|Data\b|RibbonEvent\s*\"TabActivated:)")


def loading_payload(text: str) -> str:
    text = _TIMESTAMP.sub("", text, count=1)
    return _API_PREFIX.sub("", _PREFIX.sub("", text, count=1).strip(), count=1)


def is_addin_start(text: str, folded: str | None = None) -> bool:
    if not any(map((fold(text) if folded is None else folded).__contains__, START_GATE)):
        return False
    return bool(_START.match(loading_payload(text)))


def phase_marker(text: str, folded: str, command: bool) -> str:
    """The phase a line moves to whatever the current phase ("" for none).
    folded: rules.fold(text); command: _COMMAND matches text."""
    # Spelled out instead of any(map(...)) where the terms are fixed.
    if ("exitmanagedinstance" in folded or "finished recording journal file" in folded
            or "journal exit" in folded or "license shutdown starting" in folded):
        return "shutdown"
    if "openfrommodelpath" in folded or (command and any(map(folded.__contains__, _OPEN_COMMANDS))):
        return "document_open"
    if command and not _PASSIVE_JRN.match(text):
        # A user action ends start-up; answering the open dialog (Jrn.Data) does not end opening.
        return "interactive"
    return ""


def _worst(matches) -> str:
    return max((match.severity for match in matches), key=SEVERITY_RANK.__getitem__)


# Outside API_ERROR and add-in braces the result and the next state are a
# function of the text, the two continuation flags and the stage; about half of
# a Journal's lines repeat an earlier key. Bounded: cleared when full.
_CACHE_LIMIT = 16_384
# Lines no category claims take their stage's category: before the first user
# action they belong to start-up, after a shutdown marker to the exit.
_STAGE_DEFAULT = {"startup": "startup", "session": "other", "shutdown": "exit"}


class Categorizer:
    def __init__(self, rules: tuple[Rule, ...]):
        self.rules = rules
        self._api_depth = 0
        self._addin_depth = 0
        self._operation_continuation = False
        self._dialog_continuation = False
        # startup -> session -> shutdown (sticky), on the markers of BlockBuilder phases.
        self._stage = "startup"
        self._cache: dict[tuple[str, bool, bool, str], tuple] = {}

    def _matches(self, text: str, folded: str):
        # match_rules skips lines without any gate term of the rule set.
        return match_rules(self.rules, text, folded)

    def classify(self, text: str) -> tuple[str, str, str]:
        if self._api_depth or self._addin_depth:
            return self._classify(text)
        key = (text, self._operation_continuation, self._dialog_continuation, self._stage)
        cached = self._cache.get(key)
        if cached is not None:
            (result, self._api_depth, self._operation_continuation, self._dialog_continuation,
             self._stage, self._addin_depth) = cached
            return result
        result = self._classify(text)
        if len(self._cache) >= _CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = (result, self._api_depth, self._operation_continuation, self._dialog_continuation,
                            self._stage, self._addin_depth)
        return result

    def _classify(self, text: str) -> tuple[str, str, str]:
        lower = text.lower()
        is_api_error = "api_error" in lower and bool(_API_ERROR.search(text))
        folded = lower if text.isascii() else fold(text)
        jrn = any(map(folded.__contains__, _JOURNAL_GATE))
        # The stage moves before the line is classified, like BlockBuilder's
        # phase. Only start-up can end at a command, and shutdown is final.
        stage = self._stage
        if stage != "shutdown":
            marker = phase_marker(text, folded, stage == "startup" and jrn and _COMMAND.match(text) is not None)
            if marker == "shutdown":
                self._stage = "shutdown"
            elif marker and stage == "startup":
                self._stage = "session"
        if is_api_error:
            self._api_depth = max(0, text.count("{") - text.count("}"))
            self._addin_depth = 0
            self._operation_continuation = self._dialog_continuation = False
            # Other rules on the same record (add-in conflict, Steel Connections)
            # are kept with the API error instead of being hidden by it.
            extra = [match for match in self._matches(text, folded) if match.rule_id != "revit.api_error"]
            if extra:
                severity = max(["error", *(match.severity for match in extra)], key=SEVERITY_RANK.__getitem__)
                return "api_error", severity, ",".join(["revit.api_error", *(match.rule_id for match in extra)])
            return "api_error", "error", "revit.api_error"
        # Pure function of the text; evaluated once instead of up to twice.
        journal = jrn and _JOURNAL.search(text) is not None
        new_record = False
        if self._api_depth or self._addin_depth:
            # Explicit new records end an unclosed API_ERROR or add-in brace block.
            new_record = ("api_success" in lower or "api_info" in lower or "dbg_error:" in lower
                          or "exceptioncode=" in lower or journal or _RECORD.match(text) is not None)
        if self._api_depth:
            if new_record:
                self._api_depth = 0
            else:
                self._api_depth = max(0, self._api_depth + text.count("{") - text.count("}"))
                return "api_error", "error", "revit.api_error:continuation"
        # Class, vendor and assembly lines inside the braces of
        # "API_SUCCESS { Starting External Application: ..." belong to that record.
        addin_continuation = False
        if self._addin_depth:
            if new_record:
                self._addin_depth = 0
            else:
                self._addin_depth = max(0, self._addin_depth + text.count("{") - text.count("}"))
                addin_continuation = True
        marked = any(map(folded.__contains__, _MARKER_GATE))
        addin_start = (marked and any(map(folded.__contains__, START_GATE))
                       and _START.match(loading_payload(text)) is not None)
        if addin_start:
            self._addin_depth = max(0, text.count("{") - text.count("}"))
        if _RECORD.match(text) or not text.strip():
            self._operation_continuation = self._dialog_continuation = False
        dialog = (any(map(lower.__contains__, _DIALOG_GATE))
                  and bool(_TASK_DIALOG.search(text) or _DIALOG_RESULT.search(text)))
        if dialog or self._dialog_continuation:
            # What the user saw (TaskDialog "...") and answered (Jrn.Data
            # "TaskDialogResult" with its continuation lines).
            self._operation_continuation = False
            self._dialog_continuation = (journal or self._dialog_continuation) and text.rstrip().endswith("_")
            matches = self._matches(text, folded)
            return "dialog", _worst(matches) if matches else "", ",".join(match.rule_id for match in matches)
        matches = self._matches(text, folded)
        errors = [match for match in matches if match.severity != "warning"]
        if errors:
            self._operation_continuation = False
            return "error", _worst(errors), ",".join(match.rule_id for match in errors)
        if matches:  # warning-level rules only, e.g. UI prompts and license warnings
            return "warning", "warning", ",".join(match.rule_id for match in matches)
        if "dbg_warn" in lower:
            return "warning", "warning", "revit.dbg_warn"
        if journal or self._operation_continuation:
            self._operation_continuation = text.rstrip().endswith("_")
            return "operation", "", ""
        if not text.strip():
            return "other", "", ""
        if addin_start or addin_continuation:
            return "addin", "", ""
        if marked and any(map(folded.__contains__, _ADDIN_GATE)) and _ADDIN.search(text):
            return "addin", "", ""
        # An API record naming a start-up event ("Unregistering ApplicationInitialized
        # event ...") is API activity, so API comes before the start-up and exit markers.
        if "api_success" in lower or "api_info" in lower:
            return "api", "", ""
        if marked:
            if any(map(folded.__contains__, _STARTUP_GATE)) and _STARTUP.search(text):
                return "startup", "", ""
            if any(map(folded.__contains__, _EXIT_GATE)) and _EXIT.search(text):
                return "exit", "", ""
        return _STAGE_DEFAULT[self._stage], "", ""
