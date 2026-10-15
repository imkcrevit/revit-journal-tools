"""Session-level facts collected in the same pass as the index (J12).

Everything here is a reading aid with line numbers: markers are matched
literally, nothing is inferred from file names or executed. Session findings
are candidates, like rule hits.
"""

import re
from collections import Counter, deque
from datetime import datetime

from .blocks import PHASES

_MONTHS = {name: number for number, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_TIMESTAMP = re.compile(r"^\s*'[A-Z]\s+(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?;")
_HEADER = re.compile(r"^'\s*(Build|Branch|Release):\s*(.*?)\s*$")
_PID = re.compile(r"Session(\d+)_P0MainQueue")
_OPEN_PATH = re.compile(r"openFromModelPath \[([^\]]*)\]")
_CONFLICT = re.compile(r"Assembly version conflict in some references in (.+?) assembly")
_DIALOG = re.compile(r"\bTaskDialog\s*\"(.*)\"\s*$")
_USER_COMMAND = re.compile(r"^\s*Jrn\.Command\b")
_EXCEPTION_CODE = re.compile(r"\bExceptionCode\s*=\s*(0x[0-9A-Fa-f]+)")
_LIMIT = 20
_TEXT = 300
# Revit's own crash path, as written to the journal (observed in uploaded cases;
# the fatal-error dialog is matched by its Id, which is the same in every UI language).
CRASH_MARKS = {
    "exception": "异常（Revit崩溃保护捕获）",
    "exception_code": "异常代码",
    "crash_protection_failed": "崩溃保护过程中再次异常",
    "unhandled": "未处理异常（进程随后终止）",
    "fatal_dialog": "致命错误对话框",
    "crash_log": "崩溃记录（SLOG >Crash）",
    "cef_failed": "内嵌浏览器CEF初始化失败",
}
# Standard Windows/.NET exception codes; the code says what kind of failure, not why.
EXCEPTION_CODES = {
    "0xe0434352": ".NET托管异常",
    "0xe06d7363": "C++异常",
    "0xc0000005": "访问冲突",
    "0x80000003": "断点/内部断言（程序主动中断）",
    "0xc00000fd": "栈溢出",
    "0xc0000409": "快速失败/栈缓冲区溢出",
    "0xc0000374": "堆损坏",
    "0xc0000017": "内存不足",
}

END_STATES = {"clean": "正常结束", "no_clean_exit": "未正常结束（无结束标记：崩溃、被强制结束或仍未写完）",
              "live": "正在写入（会话尚未结束）"}


def parse_timestamp(match: re.Match) -> datetime | None:
    day, month, year, hour, minute, second, fraction = match.groups()
    try:
        micro = int((fraction or "0")[:6].ljust(6, "0"))
        return datetime(int(year), _MONTHS[month.title()], int(day), int(hour), int(minute), int(second), micro)
    except (KeyError, ValueError):
        return None


def _evidence(number: int, text: str) -> dict:
    return {"line_number": number, "text": text.strip()[:_TEXT]}


class SessionCollector:
    """Bounded per-file state; the full text stays in the index."""

    def __init__(self, tail: int = 20, keep: int = 10):
        self.first_ts = self.last_ts = self.start_ts = None
        self.first_ts_line = self.last_ts_line = 0
        self.header: dict[str, str] = {}
        self.pid = None
        self.finished = 0
        self.exit_lines: list[int] = []
        self.license_events = 0
        self.license_errors: list[dict] = []
        self.documents: dict[str, dict] = {}
        self.open_failures: list[dict] = []
        self.dialogs: deque = deque(maxlen=keep)
        self.dialog_count = 0
        self.conflicts: Counter[str] = Counter()
        self.conflict_first: dict[str, int] = {}
        self.steel: list[dict] = []
        self.last_lines: deque = deque(maxlen=tail)
        self.operations: deque = deque(maxlen=keep)
        self.dialog_blocks: deque = deque(maxlen=keep)
        self.addins: Counter[str] = Counter()
        self.third_party: dict[str, int] = {}
        self.addin_failures: list[dict] = []
        self.phases: dict[str, int] = {}
        self.last_command: dict | None = None
        self.crash_marks: list[dict] = []
        self.crash_counts: Counter[str] = Counter()
        self._cef_detail = False

    def _crash(self, number: int, text: str) -> None:
        """Record one crash-path line; the first ones are kept with the command before them."""
        kind = None
        if "Exception occurred during Revit crash protection" in text:
            kind = "crash_protection_failed"
        elif "Exception occurred" in text:
            kind = "exception"
        elif "ExceptionCode" in text and _EXCEPTION_CODE.search(text):
            kind = "exception_code"
        elif "unhandledExceptionFilter is executed" in text:
            kind = "unhandled"
        elif "TaskDialog_Fatal_Error_Occured" in text:
            kind = "fatal_dialog"
        elif ">Crash" in text and "SLOG" in text:
            kind = "crash_log"
        elif "CEF failed to initialize" in text:
            kind = "cef_failed"
        if kind is None:
            return
        self.crash_counts[kind] += 1
        if len(self.crash_marks) >= 3 * _LIMIT:
            return
        mark = {**_evidence(number, text), "kind": kind}
        if kind == "exception_code":
            code = _EXCEPTION_CODE.search(text).group(1).lower()
            mark["code"] = code
            mark["meaning"] = EXCEPTION_CODES.get(code, "")
        if kind == "exception" and self.last_command:
            mark["command"] = self.last_command
        self.crash_marks.append(mark)
        self._cef_detail = kind == "cef_failed"

    def feed(self, number: int, text: str, classification: tuple[str, str, str]) -> None:
        category, _, rules = classification
        self.last_lines.append((number, text[:_TEXT]))
        if self._cef_detail:
            # "CEF failed to initialize due to failure:" names the failure on the next line.
            self._cef_detail = False
            if text.startswith("'") and not _TIMESTAMP.match(text) and self.crash_marks:
                self.crash_marks[-1]["detail"] = text.strip()[:_TEXT]
        if "Jrn.Command" in text and _USER_COMMAND.match(text):
            self.last_command = _evidence(number, text)
        if "xception" in text or "rash" in text or "CEF failed" in text or "TaskDialog_Fatal" in text:
            self._crash(number, text)
        if ";" in text and "'" in text:
            match = _TIMESTAMP.match(text)
            if match:
                if self.first_ts is None:
                    self.first_ts, self.first_ts_line = match, number
                self.last_ts, self.last_ts_line = match, number
                if "started recording journal file" in text and self.start_ts is None:
                    self.start_ts = match
                if "finished recording journal file" in text:
                    self.finished = number
                if "ExitManagedInstance" in text or "Journal Exit" in text:
                    self.exit_lines.append(number)
        if number <= 200 and text.startswith("'"):
            header = _HEADER.match(text)
            if header and header.group(1) not in self.header:
                self.header[header.group(1)] = header.group(2)[:200]
        if self.pid is None and "_P0MainQueue" in text:
            pid = _PID.search(text)
            self.pid = int(pid.group(1)) if pid else None
        if "LicenseUpd(" in text or "Adlsdk" in text:
            self.license_events += 1
            if "revit.license_error" in rules and len(self.license_errors) < _LIMIT:
                self.license_errors.append(_evidence(number, text))
        if "openFromModelPath [" in text:
            path = _OPEN_PATH.search(text)
            if path:
                entry = self.documents.setdefault(path.group(1), {"path": path.group(1)[:400], "first_line": number,
                                                                  "count": 0})
                entry["count"] += 1
        if "revit.model_open_failed" in rules and len(self.open_failures) < _LIMIT:
            self.open_failures.append(_evidence(number, text))
        if "revit.steel_localdb" in rules and len(self.steel) < _LIMIT:
            self.steel.append(_evidence(number, text))
        if "Assembly version conflict" in text:
            conflict = _CONFLICT.search(text)
            if conflict:
                name = conflict.group(1).strip()[:200]
                self.conflicts[name] += 1
                self.conflict_first.setdefault(name, number)
        if category == "dialog" and "TaskDialog" in text:
            dialog = _DIALOG.search(text)
            if dialog:
                self.dialog_count += 1
                self.dialogs.append(_evidence(number, dialog.group(1)))

    def add_blocks(self, blocks) -> None:
        for block in blocks:
            if block.phase and block.phase not in self.phases:
                self.phases[block.phase] = block.start_line
            if block.category == "operation":
                self.operations.append({"start_line": block.start_line, "end_line": block.end_line,
                                        "text": block.title[:_TEXT]})
            elif block.category == "dialog":
                self.dialog_blocks.append({"start_line": block.start_line, "end_line": block.end_line,
                                           "text": block.title[:_TEXT]})
            if block.addin_origin:
                self.addins[block.addin_origin] += 1
                name = block.addin_name or block.addin_class or ""
                if block.addin_origin == "third_party" and name and len(self.third_party) < 200:
                    self.third_party.setdefault(name[:200], block.start_line)
                if block.category in {"api_error", "error"} and len(self.addin_failures) < _LIMIT:
                    self.addin_failures.append({"start_line": block.start_line, "name": name[:200],
                                                "origin": block.addin_origin})

    def finish(self) -> dict:
        start_match = self.start_ts or self.first_ts
        start = parse_timestamp(start_match) if start_match else None
        end = parse_timestamp(self.last_ts) if self.last_ts else None
        duration = (end - start).total_seconds() if start and end and end >= start else None
        return {
            "build": self.header.get("Build", ""), "branch": self.header.get("Branch", ""),
            "release": self.header.get("Release", ""), "pid": self.pid,
            "recording_started": self.start_ts is not None,
            "start_time": start.isoformat(timespec="milliseconds") if start else None,
            "end_time": end.isoformat(timespec="milliseconds") if end else None,
            "end_time_line": self.last_ts_line, "duration_seconds": round(duration, 3) if duration is not None else None,
            "clean_exit": bool(self.finished), "finished_line": self.finished or None,
            "exit_lines": self.exit_lines[:_LIMIT],
            "license": {"events": self.license_events, "errors": self.license_errors},
            "documents": list(self.documents.values())[:50], "document_count": len(self.documents),
            "open_failures": self.open_failures,
            "addins": {
                "loading_records": sum(self.addins.values()), "autodesk": self.addins["autodesk"],
                "third_party": self.addins["third_party"], "unknown": self.addins["unknown"],
                "third_party_names": [{"name": name, "first_line": line} for name, line in self.third_party.items()],
                "third_party_count": len(self.third_party), "failures": self.addin_failures,
                "conflicts": [{"assembly": name, "count": count, "first_line": self.conflict_first[name]}
                              for name, count in self.conflicts.most_common(50)],
                "conflict_lines": sum(self.conflicts.values()),
            },
            "dialogs": {"count": self.dialog_count, "last": list(self.dialogs)},
            "steel_localdb": self.steel,
            "crash": {"marks": self.crash_marks, "counts": dict(self.crash_counts)},
            "phases": [{"phase": phase, "label": PHASES[phase], "first_line": line}
                       for phase, line in sorted(self.phases.items(), key=lambda item: item[1])],
            "last_operations": list(self.operations), "last_dialogs": list(self.dialog_blocks),
            "last_lines": [{"line_number": number, "text": text} for number, text in self.last_lines],
        }


def end_state(summary: dict, live: bool) -> str:
    if summary.get("clean_exit"):
        return "clean"
    return "live" if live else "no_clean_exit"


def session_findings(summary: dict, live: bool, sidecars: list[dict], source: str, lang: str = "zh") -> list[dict]:
    """Session-level candidates with evidence; never a diagnosis. lang: zh, or en
    for the viewer page (titles and explanations only; evidence is journal text)."""
    from .i18n import EN

    findings = []
    en = lang == "en"

    def add(finding_id, severity, title, explanation, evidence):
        findings.append({"id": finding_id, "severity": severity, "title": title, "explanation": explanation,
                         "evidence": evidence, "status": "candidate"})

    state = end_state(summary, live)
    last = summary.get("last_lines") or []
    if state == "no_clean_exit":
        add("session.no_clean_exit", "warning", "Did not end normally" if en else "未正常结束",
            "No “finished recording journal file” end marker: Revit may have crashed, been forced to close or "
            "lost power; check the last operations and dialogs before the end, dmp files and CER logs." if en else
            "没有“finished recording journal file”结束标记：Revit可能崩溃、被强制结束或断电；"
            "请结合结束前最后的操作、对话框、dmp与CER日志判断。",
            [{"source": source, **last[-1]}] if last else [])
    errors = summary.get("license", {}).get("errors") or []
    if errors:
        add("session.license_error", "warning", "License validation errors" if en else "许可验证错误",
            f"{len(errors)} licensing SDK error{'' if len(errors) == 1 else 's'} in the session; license problems usually "
            "appear before other symptoms."
            if en else f"会话中有{len(errors)}条许可SDK错误；许可问题通常早于其他现象出现。",
            [{"source": source, **item} for item in errors[:5]])
    failures = summary.get("open_failures") or []
    if failures:
        add("session.model_open_failed", "error", "Model open or load failed" if en else "模型打开或载入失败",
            "Opening or loading a model reported an exception or did not finish; the exception text in the journal "
            "gives the reason." if en else "打开/载入模型时报告异常或未完成；原因见原文中的异常文字。",
            [{"source": source, **item} for item in failures[:5]])
    conflicts = summary.get("addins", {}).get("conflicts") or []
    if conflicts:
        total = summary["addins"]["conflict_lines"]
        add("session.addin_conflict", "warning", "Add-in assembly version conflicts" if en else "Add-in程序集版本冲突",
            f"{total} conflict record{'' if total == 1 else 's'} in {len(conflicts)} "
            f"{'assembly' if len(conflicts) == 1 else 'assemblies'} (grouped by assembly); in most cases the add-ins "
            "still load." if en else f"{len(conflicts)}个程序集共{total}条冲突记录（按程序集聚合）；多数情况下Add-in仍能加载。",
            [{"source": source, "line_number": item["first_line"],
              "text": f"{item['assembly']} ×{item['count']}"} for item in conflicts[:10]])
    steel = summary.get("steel_localdb") or []
    steel_logs = [item for item in sidecars if item.get("kind") == "steel_log" and item.get("entries")]
    if steel or steel_logs:
        evidence = [{"source": source, **item} for item in steel[:5]]
        for log in steel_logs:
            evidence.extend({"source": log["path"], "line_number": entry["line_number"],
                             "text": f"[{entry['time']}] {entry['text']}"} for entry in log["entries"][:10])
        add("session.steel_localdb", "error", "Steel connection / LocalDB errors" if en else "钢结构连接/LocalDB错误",
            "Steel Connections depends on SQL Server Express LocalDB; the steel connection error log or the journal has "
            "LocalDB or steel connection exceptions. The standalone repair tool RevitLocalDbFix (not a Revit add-in) "
            "can check and repair the LocalDB instance." if en else
            "Steel Connections依赖SQL Server Express LocalDB；钢连接错误日志或Journal中有LocalDB/钢连接异常。"
            "可使用独立修复工具RevitLocalDbFix（不是Revit插件）检查和修复LocalDB实例。", evidence)
    crash = summary.get("crash") or {}
    marks = crash.get("marks") or []
    if marks:
        counts = crash.get("counts") or {}
        severe = state == "no_clean_exit" or any(
            counts.get(kind) for kind in ("crash_protection_failed", "unhandled", "fatal_dialog", "crash_log"))
        first = marks[0]
        meanings = EN["exception_code"] if en else EXCEPTION_CODES
        codes = list(dict.fromkeys(mark["code"] for mark in marks if mark.get("code")))
        command = next((mark["command"] for mark in marks if mark.get("command")), None)
        if en:
            parts = [f"{EN['crash_mark'][first['kind']]} recorded from line {first['line_number']}"]
            if codes:
                parts.append("exception code " + ", ".join(
                    code + (f" ({meanings[code]})" if code in meanings else "") for code in codes))
            if command:
                parts.append(f"the last command before the exception is on line {command['line_number']}: {command['text']}")
            if severe:
                parts.append("then a fatal error, an unhandled exception or no normal end of the session: "
                             "Revit most likely crashed here")
            explanation = ("; ".join(parts) + ". An exception code tells the kind of failure, not its cause; check "
                           "the operations before the exception, dmp files and CER logs.")
        else:
            parts = [f"第{first['line_number']}行起记录到{CRASH_MARKS[first['kind']]}"]
            if codes:
                parts.append("异常代码" + "、".join(
                    code + (f"（{meanings[code]}）" if code in meanings else "") for code in codes))
            if command:
                parts.append(f"异常前最后的命令在第{command['line_number']}行：{command['text']}")
            if severe:
                parts.append("随后出现致命错误、未处理异常或会话没有正常结束：Revit很可能在此崩溃")
            explanation = "；".join(parts) + "。异常代码只说明故障类别，不说明具体原因；请结合异常前的操作、dmp与CER日志判断。"

        def meaning(mark):
            text = meanings.get(mark.get("code", ""), mark["meaning"]) if en else mark["meaning"]
            return f" ← {text}"

        evidence = ([{"source": source, **command}] if command else []) + [
            {"source": source, "line_number": mark["line_number"],
             "text": mark["text"] + (meaning(mark) if mark.get("meaning") else "")
             + (f" ← {mark['detail']}" if mark.get("detail") else "")} for mark in marks[:8]]
        add("session.crash", "fatal" if severe else "error", "Revit exceptions and crash records" if en
            else "Revit异常与崩溃记录", explanation, evidence)
    dumps = [item for item in sidecars if item.get("status") == "binary"]
    if dumps:
        add("session.crash_dump", "warning", "Crash dumps in the same session" if en else "同一会话存在崩溃转储",
            f"The session folder has {len(dumps)} journal.NNNN.NNNN.dmp file{'' if len(dumps) == 1 else 's'}; dmp files are "
            "usually written on an "
            "exception; this tool does not read them." if en else
            f"会话目录中有{len(dumps)}个journal.NNNN.NNNN.dmp；dmp通常在异常时生成，本工具不解析其内容。",
            [{"source": item["path"], "line_number": None,
              "text": f"{item['size']} bytes" if en else f"{item['size']}字节"} for item in dumps])
    return findings
