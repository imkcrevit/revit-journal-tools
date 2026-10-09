"""Viewer record categories, separate from incident severity and session phases."""

import re

from .models import SEVERITY_RANK
from .rules import Rule

CATEGORIES = {
    "startup": "启动", "system": "系统数据", "error": "错误",
    "api_error": "API错误", "operation": "操作内容", "warning": "警告",
    "api": "API活动", "other": "其他记录",
}
_JOURNAL = re.compile(r"\bJrn\.(?:Command|Data|RibbonEvent|Mouse\w*|Key\w*|\w*Button|ComboBox|Edit|Directive|CheckBox|ListBox|TreeCtrl)\b", re.I)
_STARTUP = re.compile(r"started recording journal|InitApplication|InitNativeInstance|Starting (?:External|Revit)|ApplicationInitialized|^'\s*(?:Build|Branch|Release):|Set Jrn =", re.I)
_SYSTEM = re.compile(r"\b(?:VM:|RAM:|GUI Resource Usage|OSVersion|OSLanguage|OSBuild|Processor|CPU|GPU|Display adapter|Physical memory|License mode|Actual language code|Autodesk Revit \d{4}|Options::loadOptions|System Information|SystemSettings|Log Summary|ComputerName|UserName\s*:)", re.I)
_RECORD = re.compile(r"^\s*'[A-Z]\s+\d{1,2}-[A-Za-z]+-\d{4}\s")
_POSSIBLE_ERROR = (
    "error", "exception", "assertion failed", "out of memory", "access denied",
    "access is denied", "file not found", "could not find", "could not load",
    "failed to synchron", "cannot reconcile", "file not saved", "missing esschema",
    "access violation", "e_outofmemory", "错误", "异常", "访问冲突", "内存不足", "拒绝访问", "找不到", "无法同步", "同步失败",
)


class Categorizer:
    def __init__(self, rules: tuple[Rule, ...]):
        self.rules = rules
        self._api_depth = 0
        self._operation_continuation = False

    def classify(self, text: str) -> tuple[str, str, str]:
        lower = text.lower()
        is_api_error = "api_error" in lower and bool(re.search(r"\bAPI_ERROR\s*[:{]", text, re.I))
        if is_api_error:
            self._api_depth = max(0, text.count("{") - text.count("}"))
            self._operation_continuation = False
            return "api_error", "error", "revit.api_error"
        if self._api_depth:
            if ("api_success" in lower or "api_info" in lower or "dbg_error:" in lower
                    or "exceptioncode=" in lower or _JOURNAL.search(text) or _RECORD.match(text)):
                self._api_depth = 0
            else:
                self._api_depth = max(0, self._api_depth + text.count("{") - text.count("}"))
                return "api_error", "error", "revit.api_error:continuation"
        if _RECORD.match(text) or not text.strip():
            self._operation_continuation = False
        if any(term in lower for term in _POSSIBLE_ERROR):
            matches = [match for rule in self.rules if (match := rule.match(text))]
            errors = [match for match in matches if match.severity != "warning"]
            if errors:
                self._operation_continuation = False
                severity = max((match.severity for match in errors), key=SEVERITY_RANK.__getitem__)
                return "error", severity, ",".join(match.rule_id for match in errors)
        if "dbg_warn" in lower:
            return "warning", "warning", "revit.dbg_warn"
        if _JOURNAL.search(text) or self._operation_continuation:
            self._operation_continuation = text.rstrip().endswith("_")
            return "operation", "", ""
        if _STARTUP.search(text):
            return "startup", "", ""
        if _SYSTEM.search(text):
            return "system", "", ""
        if "api_success" in lower or "api_info" in lower:
            return "api", "", ""
        return "other", "", ""
