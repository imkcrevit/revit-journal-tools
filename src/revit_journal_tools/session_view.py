"""Plain-text rendering of sessions and their summaries (TUI panel, --print, summary).

Callers pass every line through terminal.safe_text before it reaches a terminal.
Chinese by default; the viewer page also asks for English (lang="en").
"""

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .blocks import PHASES
from .categories import CATEGORIES
from .i18n import EN, label
from .sessions import ROLE_LABELS, STATUS_LABELS
from .summary import CRASH_MARKS

SIDECAR_LABELS = {"worker": "Worker日志", "steel_log": "钢连接错误日志", "cer_log": "CER错误报告日志",
                  "dump": "崩溃转储", "duplicate": "字节相同的副本", "empty": "空文件"}

_TEXT = {
    "zh": {
        "no_duration": "时长未知", "hm": "{h}小时{m}分", "ms": "{m}分{s}秒", "s": "{s}秒", "no_time": "时间未知",
        "writing": "正在写入", "live": "实时快照（会话未结束）", "list": "、",
        "session": "会话 {label}", "unrecognized": "未识别命名 {label}", "dmp": "dmp×{count}（{size:.1f}MB）",
        "same_as": "（与{name}相同）", "binary_size": "，{size:.1f}MB",
        "no_state": "状态未知", "source": "来源：{path}", "no_summary": "（主Journal尚未完成索引或读取失败，暂无摘要）",
        "version": "版本：{build}", "not_recorded": "未记录", "pid": " | 进程号{pid}",
        "end_line": "结束标记第{line}行", "no_end": "无结束标记",
        "time": "时间：{start} → {end}（{duration}）| {marker}", "phases": "阶段：", "phase": "{label}L{line}",
        "crash": "异常与崩溃：{count}次被捕获的异常", "fatal": "；有致命错误对话框", "unhandled": "；有未处理异常",
        "command": "  第{line}行 [异常前的命令] {text}", "mark": "  第{line}行 [{label}] {text}",
        "license": "许可：{count}条许可记录", "license_errors": "；许可错误{count}条（第{lines}行）",
        "documents": "打开过的文档：{count}个", "document": "  第{line}行 {path}", "times": "（{count}次）",
        "no_documents": "打开过的文档：未见openFromModelPath记录", "open_failure": "  打开失败 第{line}行 {text}",
        "addins": "Add-in：加载记录{records}条（Autodesk {autodesk} / 第三方{third} / 待定{unknown}）；第三方Add-in {count}个",
        "third_party": "  第三方：{names}", "etc": "等",
        "conflicts": "  程序集冲突：{count}个程序集共{lines}条（按程序集聚合）",
        "conflict": "    {assembly} ×{count}（首见第{line}行）",
        "dialogs": "对话框：{count}个", "dialog": "  第{line}行 {text}",
        "tail": "结束前最后{count}个操作与对话框：", "last": "最后{count}行：", "sidecars": "侧车文件：",
        "lines": "（{count}行）", "size": "（{size:.1f}MB）", "basis": "；关联依据：{basis}",
        "findings": "会话级发现（候选，需结合原文复核）：{count}项", "finding": "  [{severity}] {id} {title}：{explanation}",
        "at_line": "第{line}行",
    },
    "en": {
        "no_duration": "duration unknown", "hm": "{h}h {m}min", "ms": "{m}min {s}s", "s": "{s}s",
        "no_time": "time unknown", "writing": "being written", "live": "live snapshot (session not finished)",
        "list": ", ", "session": "Session {label}", "unrecognized": "Unrecognized name {label}",
        "dmp": "dmp×{count} ({size:.1f} MB)", "same_as": " (same as {name})", "binary_size": ", {size:.1f} MB",
        "no_state": "state unknown", "source": "Source: {path}",
        "no_summary": "(The main journal is not indexed yet or could not be read: no summary)",
        "version": "Version: {build}", "not_recorded": "not recorded", "pid": " | process {pid}",
        "end_line": "end marker on line {line}", "no_end": "no end marker",
        "time": "Time: {start} → {end} ({duration}) | {marker}", "phases": "Phases: ", "phase": "{label} L{line}",
        "crash": "Exceptions and crashes: {count} caught exception{s}", "fatal": "; fatal error dialog",
        "unhandled": "; unhandled exception",
        "command": "  line {line} [command before the exception] {text}", "mark": "  line {line} [{label}] {text}",
        "license": "Licensing: {count} license record{s}", "license_errors": "; {count} license error{s} ({lines})",
        "documents": "Documents opened: {count}", "document": "  line {line} {path}", "times": " ({count} times)",
        "no_documents": "Documents opened: no openFromModelPath records",
        "open_failure": "  open failed: line {line} {text}",
        "addins": "Add-ins: {records} loading record{rs} (Autodesk {autodesk} / third-party {third} / unknown {unknown}); "
                  "{count} third-party add-in{s}",
        "third_party": "  Third-party: {names}", "etc": " and more",
        "conflicts": "  Assembly conflicts: {lines} record{ls} in {count} assembl{ies} (grouped by assembly)",
        "conflict": "    {assembly} ×{count} (first on line {line})",
        "dialogs": "Dialogs: {count}", "dialog": "  line {line} {text}",
        "tail": "Last {count} operation{s} or dialog{s} before the end:", "last": "Last {count} line{s}:",
        "sidecars": "Sidecar files:", "lines": " ({count} line{s})", "size": " ({size:.1f} MB)",
        "basis": "; linked by: {basis}",
        "findings": "Session findings (candidates, check them against the journal): {count}",
        "finding": "  [{severity}] {id} {title}: {explanation}", "at_line": "line {line}",
    },
}
# The main-journal line a summary line refers to: "  N | text" (last lines),
# "  LN–M [...]" (last operations), then "第N行" / "line N".
_LINE_REFS = {
    "zh": (re.compile(r"^\s+(\d+) \|"), re.compile(r"^\s+L(\d+)–"), re.compile(r"第(\d+)行")),
    "en": (re.compile(r"^\s+(\d+) \|"), re.compile(r"^\s+L(\d+)–"), re.compile(r"\bline (\d+)\b")),
}


def _s(count) -> str:
    """English plural ending; the Chinese templates have no {s}."""
    return "" if count == 1 else "s"


def line_ref(text: str, lang: str = "zh") -> int | None:
    for pattern in _LINE_REFS[lang]:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def duration_text(seconds, lang: str = "zh") -> str:
    text = _TEXT[lang]
    if seconds is None:
        return text["no_duration"]
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return text["hm"].format(h=hours, m=minutes)
    return text["ms"].format(m=minutes, s=secs) if minutes else text["s"].format(s=secs)


def _when(value: str | None, lang: str = "zh") -> str:
    return value.replace("T", " ")[:19] if value else _TEXT[lang]["no_time"]


def file_date(item: dict) -> str:
    return datetime.fromtimestamp(item["mtime_ns"] / 1e9).strftime("%Y-%m-%d %H:%M") if item.get("mtime_ns") else ""


def state_text(session: dict, lang: str = "zh") -> str:
    if session.get("writing"):
        return _TEXT[lang]["writing"]
    if session.get("end_state") == "live":
        return _TEXT[lang]["live"]
    if lang == "en" and session.get("end_state"):
        return EN["end_state"][session["end_state"]]
    return session.get("end_state_label") or ""


def sidecar_label(item: dict, lang: str = "zh") -> str:
    text = _TEXT[lang]
    entry = (f"· {item['name']} [{label('role', item['role'], ROLE_LABELS, lang)}] "
             f"{label('status', item['status'], STATUS_LABELS, lang)}")
    if item["status"] == "binary":
        entry += text["binary_size"].format(size=item["size"] / 1024 / 1024)
    if item.get("duplicate_of"):
        entry += text["same_as"].format(name=Path(item["duplicate_of"]).name)
    return entry


def session_label(session: dict, files: dict, lang: str = "zh") -> str:
    text = _TEXT[lang]
    roles = Counter(files[file_id]["role"] for file_id in session["files"] if file_id in files)
    roles.update(item["role"] for item in session["sidecars"] if item["status"] != "binary")
    parts = [f"{label('role', role, ROLE_LABELS, lang)}×{count}" for role, count in roles.items()]
    if session["dump_count"]:
        parts.append(text["dmp"].format(count=session["dump_count"], size=session["dump_bytes"] / 1024 / 1024))
    head = text["session" if session["recognized"] else "unrecognized"].format(label=session["label"])
    summary = session.get("summary") or {}
    facts = [session["directory"]]
    if summary.get("start_time"):
        facts.append(_when(summary["start_time"], lang)[:16])
        facts.append(duration_text(summary.get("duration_seconds"), lang))
    state = state_text(session, lang)
    if state:
        facts.append(state)
    return f"{head} | " + " | ".join(facts) + " | " + text["list"].join(parts)


def main_tail(index, session: dict, catalog: list[dict], limit: int) -> list[dict]:
    """The last operations and dialogs of the session's main journal (summary_lines' tail)."""
    main = next((item for item in catalog if item["path"] == session.get("main")), None)
    return index.tail_blocks(main["id"], limit) if main and main["status"] == "ready" else []


def _basis(item: dict, lang: str) -> str:
    """Why a sidecar log belongs to the session (sidecars.py writes the Chinese text)."""
    if lang != "en" or "basis_key" not in item:
        return item.get("basis") or ""
    if item["kind"] == "cer_log":
        if item["basis_key"] == "pid":
            return f"process id {item['pid']} matches Session{item['pid']} in the journal"
        return "time ranges overlap (the journal has no process id)"
    return EN["steel_basis"].get(item["basis_key"], item.get("basis") or "")


def summary_lines(session: dict, tail: list[dict] | None = None, lang: str = "zh") -> list[str]:
    """Readable summary of one session; every fact keeps its line number."""
    text = _TEXT[lang]
    summary = session.get("summary") or {}
    lines = [f"{text['session'].format(label=session['label'])} | {session['directory']} | "
             f"{state_text(session, lang) or text['no_state']}"]
    if session.get("main"):
        lines.append(text["source"].format(path=session["main"]))
    if not summary:
        lines.append(text["no_summary"])
        return lines
    build = " / ".join(part for part in (summary.get("build"), summary.get("branch"), summary.get("release")) if part)
    lines.append(text["version"].format(build=build or text["not_recorded"])
                 + (text["pid"].format(pid=summary["pid"]) if summary.get("pid") else ""))
    end = text["end_line"].format(line=summary["finished_line"]) if summary.get("finished_line") else text["no_end"]
    lines.append(text["time"].format(start=_when(summary.get("start_time"), lang), end=_when(summary.get("end_time"), lang),
                                     duration=duration_text(summary.get("duration_seconds"), lang), marker=end))
    if summary.get("phases"):
        lines.append(text["phases"] + " → ".join(
            text["phase"].format(label=label("phase", item["phase"], {item["phase"]: item["label"]}, lang),
                                 line=item["first_line"]) for item in summary["phases"]))
    marks = (summary.get("crash") or {}).get("marks") or []
    if marks:
        counts = summary["crash"].get("counts") or {}
        lines.append(text["crash"].format(count=counts.get("exception", 0), s=_s(counts.get("exception", 0)))
                     + (text["fatal"] if counts.get("fatal_dialog") else "")
                     + (text["unhandled"] if counts.get("unhandled") else ""))
        for mark in marks[:12]:
            if mark.get("command"):
                lines.append(text["command"].format(line=mark["command"]["line_number"], text=mark["command"]["text"]))
            entry = text["mark"].format(line=mark["line_number"], label=label("crash_mark", mark["kind"], CRASH_MARKS, lang),
                                        text=mark["text"])
            if mark.get("meaning"):
                entry += f" ← {label('exception_code', mark.get('code', ''), {mark.get('code', ''): mark['meaning']}, lang)}"
            if mark.get("detail"):
                entry += f" ← {mark['detail']}"
            lines.append(entry)
    license = summary.get("license") or {}
    errors = license.get("errors") or []
    entry = text["license"].format(count=license.get("events", 0), s=_s(license.get("events", 0)))
    if errors:
        numbers = [str(item["line_number"]) for item in errors[:5]]
        where = "、".join(numbers) if lang == "zh" else ("line " if len(numbers) == 1 else "lines ") + ", ".join(numbers)
        entry += text["license_errors"].format(count=len(errors), lines=where, s=_s(len(errors)))
    lines.append(entry)
    documents = summary.get("documents") or []
    if documents:
        lines.append(text["documents"].format(count=summary.get("document_count", len(documents))))
        lines.extend(text["document"].format(line=item["first_line"], path=item["path"])
                     + (text["times"].format(count=item["count"]) if item["count"] > 1 else "")
                     for item in documents[:8])
    else:
        lines.append(text["no_documents"])
    for item in (summary.get("open_failures") or [])[:5]:
        lines.append(text["open_failure"].format(line=item["line_number"], text=item["text"]))
    addins = summary.get("addins") or {}
    lines.append(text["addins"].format(records=addins.get("loading_records", 0), autodesk=addins.get("autodesk", 0),
                                       third=addins.get("third_party", 0), unknown=addins.get("unknown", 0),
                                       count=addins.get("third_party_count", 0), s=_s(addins.get("third_party_count", 0)),
                                       rs=_s(addins.get("loading_records", 0))))
    names = addins.get("third_party_names") or []
    if names:
        lines.append(text["third_party"].format(names=text["list"].join(item["name"] for item in names[:12]))
                     + (text["etc"] if len(names) > 12 else ""))
    conflicts = addins.get("conflicts") or []
    if conflicts:
        lines.append(text["conflicts"].format(count=len(conflicts), lines=addins.get("conflict_lines", 0),
                                               ls=_s(addins.get("conflict_lines", 0)),
                                               ies="y" if len(conflicts) == 1 else "ies"))
        lines.extend(text["conflict"].format(assembly=item["assembly"], count=item["count"], line=item["first_line"])
                     for item in conflicts[:8])
    dialogs = summary.get("dialogs") or {}
    lines.append(text["dialogs"].format(count=dialogs.get("count", 0)))
    lines.extend(text["dialog"].format(line=item["line_number"], text=item["text"]) for item in (dialogs.get("last") or [])[-5:])
    if tail:
        lines.append(text["tail"].format(count=len(tail), s=_s(len(tail))))
        lines.extend(f"  L{block['start_line']}–{block['end_line']} [{label('category', block['category'], CATEGORIES, lang)}] "
                     f"{block['title']}" for block in tail)
    last = summary.get("last_lines") or []
    if last:
        lines.append(text["last"].format(count=min(len(last), 8), s=_s(min(len(last), 8))))
        lines.extend(f"  {item['line_number']:>7} | {item['text']}" for item in last[-8:])
    sidecars = session.get("sidecar_logs") or []
    if sidecars:
        lines.append(text["sidecars"])
        for item in sidecars:
            entry = f"  · {label('sidecar', item['kind'], SIDECAR_LABELS, lang)} {item['path']}"
            if item["kind"] == "worker":
                entry += text["lines"].format(count=item.get("line_count", 0), s=_s(item.get("line_count", 0)))
            elif item["kind"] in ("dump", "cer_log"):
                entry += text["size"].format(size=item["size"] / 1024 / 1024)
            basis = _basis(item, lang)
            if basis:
                entry += text["basis"].format(basis=basis)
            lines.append(entry)
            for log_entry in (item.get("entries") or [])[:10]:
                lines.append(f"      [{log_entry['index']}][{log_entry['time']}] {log_entry['text']}")
    findings = session.get("session_findings") or []
    lines.append(text["findings"].format(count=len(findings)))
    for finding in findings:
        lines.append(text["finding"].format(severity=finding["severity"], id=finding["id"], title=finding["title"],
                                            explanation=finding["explanation"]))
        for evidence in finding["evidence"][:3]:
            where = (text["at_line"].format(line=evidence["line_number"]) if evidence.get("line_number")
                     else Path(evidence["source"]).name)
            lines.append(f"      {where} {evidence['text']}")
    return lines


def phase_label(phase: str) -> str:
    return "全部阶段" if phase == "all" else PHASES[phase]
