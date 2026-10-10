"""Viewer record categories, separate from incident severity and session phases."""

import re

from .models import SEVERITY_RANK
from .rules import Rule, fold, match_rules

CATEGORIES = {
    "startup": "启动", "system": "系统数据", "error": "错误",
    "api_error": "API错误", "operation": "操作内容", "warning": "警告",
    "api": "API活动", "other": "其他记录", "dialog": "对话框",
}
# TaskDialog "..." is the message the user saw; Jrn.Data "TaskDialogResult" the answer.
_TASK_DIALOG = re.compile(r"\bTaskDialog\s*\"")
_DIALOG_RESULT = re.compile(r"\"TaskDialogResult\"")
_DIALOG_GATE = ("taskdialog",)  # both dialog regexes are case-sensitive: exact
_JOURNAL = re.compile(r"\bJrn\.(?:Command|Data|RibbonEvent|Mouse\w*|Key\w*|\w*Button|ComboBox|Edit|Directive|CheckBox|ListBox|TreeCtrl)\b", re.I)
_STARTUP = re.compile(r"started recording journal|InitApplication|InitNativeInstance|Starting (?:External|Revit)|ApplicationInitialized|^'\s*(?:Build|Branch|Release):|Set Jrn =", re.I)
_SYSTEM = re.compile(r"\b(?:VM:|RAM:|GUI Resource Usage|OSVersion|OSLanguage|OSBuild|Processor|CPU|GPU|Display adapter|Physical memory|License mode|Actual language code|Autodesk Revit \d{4}|Options::loadOptions|System Information|SystemSettings|Log Summary|ComputerName|UserName\s*:)", re.I)
_RECORD = re.compile(r"^\s*'[A-Z]\s+\d{1,2}-[A-Za-z]+-\d{4}\s")
_API_ERROR = re.compile(r"\bAPI_ERROR\s*[:{]", re.I)
# Literal gates (checked against rules.fold(text)): every match of the regex
# contains at least one of these, so skipping the regex never changes a result.
# tests/test_categories_gates.py checks each gate against its regex.
_JOURNAL_GATE = ("jrn.",)
_STARTUP_GATE = (
    "started recording journal", "initapplication", "initnativeinstance", "starting ",
    "applicationinitialized", "build:", "branch:", "release:", "set jrn =",
)
_SYSTEM_GATE = (
    "vm:", "ram:", "gui resource usage", "osversion", "oslanguage", "osbuild", "processor", "cpu", "gpu",
    "display adapter", "physical memory", "license mode", "actual language code", "autodesk revit ",
    "options::loadoptions", "system information", "systemsettings", "log summary", "computername", "username",
)


def _worst(matches) -> str:
    return max((match.severity for match in matches), key=SEVERITY_RANK.__getitem__)


class Categorizer:
    def __init__(self, rules: tuple[Rule, ...]):
        self.rules = rules
        self._api_depth = 0
        self._operation_continuation = False
        self._dialog_continuation = False

    def _matches(self, text: str, folded: str):
        # match_rules skips lines without any gate term of the rule set.
        return match_rules(self.rules, text, folded)

    def classify(self, text: str) -> tuple[str, str, str]:
        lower = text.lower()
        is_api_error = "api_error" in lower and bool(_API_ERROR.search(text))
        folded = lower if text.isascii() else fold(text)
        if is_api_error:
            self._api_depth = max(0, text.count("{") - text.count("}"))
            self._operation_continuation = self._dialog_continuation = False
            # Other rules on the same record (add-in conflict, Steel Connections)
            # are kept with the API error instead of being hidden by it.
            extra = [match for match in self._matches(text, folded) if match.rule_id != "revit.api_error"]
            if extra:
                severity = max(["error", *(match.severity for match in extra)], key=SEVERITY_RANK.__getitem__)
                return "api_error", severity, ",".join(["revit.api_error", *(match.rule_id for match in extra)])
            return "api_error", "error", "revit.api_error"
        # Pure function of the text; evaluated once instead of up to twice.
        journal = any(map(folded.__contains__, _JOURNAL_GATE)) and _JOURNAL.search(text) is not None
        if self._api_depth:
            if ("api_success" in lower or "api_info" in lower or "dbg_error:" in lower
                    or "exceptioncode=" in lower or journal or _RECORD.match(text)):
                self._api_depth = 0
            else:
                self._api_depth = max(0, self._api_depth + text.count("{") - text.count("}"))
                return "api_error", "error", "revit.api_error:continuation"
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
        if any(map(folded.__contains__, _STARTUP_GATE)) and _STARTUP.search(text):
            return "startup", "", ""
        if any(map(folded.__contains__, _SYSTEM_GATE)) and _SYSTEM.search(text):
            return "system", "", ""
        if "api_success" in lower or "api_info" in lower:
            return "api", "", ""
        return "other", "", ""
