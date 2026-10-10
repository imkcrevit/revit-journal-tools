"""Logs written next to a Revit session but outside the Journals folder (J15).

- Steel Connections: %ProgramData%\\Autodesk\\Revit Steel Connections <year>\\<lang>\\
  DatabaseConnectionErrors.log (UTF-16 with BOM; entries "[n][HH:MM:SS] text").
  When LocalDB fails the journal may show nothing; this log holds the evidence.
- Customer Error Reporting: %LOCALAPPDATA%\\Autodesk\\CER\\<id>\\cer.log, one per
  process; the head names product/release/pid ("RVT", "2027", 31576), which the
  journal repeats as "Session<pid>_P0MainQueue".

Association is by year and time (and pid for CER) and is reported with its
basis; it is a candidate link, not proof. Files are only read.
"""

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from .reader import JournalReader
from .sessions import SessionName, describe

_YEAR = re.compile(r"(?:^|\s)(\d{4})$")
_STEEL_ENTRY = re.compile(r"^\[(\d+)\]\[(\d{1,2}):(\d{2}):(\d{2})\]\s*(.*)$")
_CER_PROCESS = re.compile(r"pid\W{1,6}(\d+)\W{1,8}product\W{1,6}RVT\W{1,8}release\W{1,6}(\d{4})")
_CER_TIME = re.compile(r"^\[(\d\d)/(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d)\]", re.M)
_JOURNAL_TIME = re.compile(rb"'[A-Z]\s+(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})")
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_SLACK = timedelta(minutes=2)


def revit_year(path: Path) -> int | None:
    """Year of the "Autodesk Revit <year>" folder a journal lives in."""
    for part in reversed(Path(path).parts):
        if part.lower().startswith("autodesk revit"):
            match = _YEAR.search(part)
            return int(match.group(1)) if match else None
    return None


def _journal_time(match) -> datetime | None:
    day, month, year, hour, minute, second = match.groups()
    try:
        return datetime(int(year), _MONTHS[month.decode().lower()], int(day), int(hour), int(minute), int(second))
    except (KeyError, ValueError):
        return None


def session_window(path: Path, chunk: int = 65536) -> tuple[datetime, datetime] | None:
    """(first, last) timestamp of a journal from its head and tail bytes only."""
    try:
        size = Path(path).stat().st_size
        with Path(path).open("rb") as stream:
            head = stream.read(chunk)
            stream.seek(max(0, size - chunk))
            tail = stream.read(chunk)
    except OSError:
        return None
    first = next((time for time in map(_journal_time, _JOURNAL_TIME.finditer(head)) if time), None)
    last = None
    for match in _JOURNAL_TIME.finditer(tail):
        last = _journal_time(match) or last
    if first is None or last is None:
        return None
    return first, max(first, last)


def steel_logs(program_data: str | Path | None = None) -> list[Path]:
    root = Path(program_data or os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Autodesk"
    if not root.is_dir():
        return []
    return sorted(path for folder in root.glob("Revit Steel Connections *") if folder.is_dir()
                  for path in folder.glob("*/DatabaseConnectionErrors.log") if path.is_file())


def parse_steel_log(path: Path) -> list[dict]:
    """Entries "[n][HH:MM:SS] text" with their line numbers (BOM decides the encoding)."""
    entries = []
    with JournalReader(path, "auto") as reader:
        for line in reader:
            match = _STEEL_ENTRY.match(line.text.strip())
            if match:
                index, hour, minute, second, text = match.groups()
                entries.append({"index": int(index), "time": f"{int(hour):02d}:{minute}:{second}",
                                "text": text.strip()[:400], "line_number": line.number})
    return entries


BASIS_RANK = {"entry_time": 0, "modified": 1, "time_of_day": 2}
BASIS_TEXT = {
    "entry_time": "条目时间（按文件修改日期）落在会话时间范围内",
    "modified": "文件修改时间落在会话时间范围内",
    "time_of_day": "条目时刻落在会话时间范围内（日志不记日期，按会话日期比较；文件修改时间不在会话内，关联较弱）",
}


def match_steel_log(log: Path, entries: list[dict], window: tuple[datetime, datetime]) -> str:
    """Basis key (see BASIS_TEXT) for linking a steel log to a session window, or ""."""
    start, end = window
    modified = datetime.fromtimestamp(log.stat().st_mtime)
    times = [tuple(map(int, entry["time"].split(":"))) for entry in entries]

    def inside(day) -> bool:
        return any(start - _SLACK <= datetime.combine(day, datetime.min.time()).replace(
            hour=hour, minute=minute, second=second) <= end + _SLACK for hour, minute, second in times)

    # Entries carry only a time of day; the file's last write gives the date.
    if inside(modified.date()):
        return "entry_time"
    if start - _SLACK <= modified <= end + timedelta(minutes=5):
        return "modified"
    # The modified time is not always the last entry's day (observed: a copied
    # or re-stamped log); fall back to the session's own dates.
    day, days = start.date(), set()
    while day <= end.date() and len(days) < 7:
        days.add(day)
        day += timedelta(days=1)
    if any(inside(day) for day in days):
        return "time_of_day"
    return ""


def steel_year(log: Path) -> int | None:
    match = _YEAR.search(log.parent.parent.name)
    return int(match.group(1)) if match else None


def attach_steel_logs(journals: list[Path], program_data: str | Path | None = None) -> dict[Path, SessionName]:
    """Steel Connections logs linked to the session of a main journal in the
    workspace (same Revit year, entry/modified time inside the session window)."""
    logs = steel_logs(program_data)
    if not logs:
        return {}
    windows = []
    for path in journals:
        name = describe(path)
        year = revit_year(path)
        if name.role == "main" and year:
            window = session_window(path)
            if window:
                windows.append((year, window, name))
    attached = {}
    for log in logs:
        year = steel_year(log)
        try:
            entries = parse_steel_log(log)
        except (OSError, ValueError, LookupError):
            continue
        matches = [(basis, window, name) for session_year, window, name in windows
                   if session_year == year and (basis := match_steel_log(log, entries, window))]
        if matches:
            # Strongest basis first, then the session nearest the log's last write.
            modified = datetime.fromtimestamp(log.stat().st_mtime)
            basis, window, name = min(matches, key=lambda item: (
                BASIS_RANK[item[0]], abs((item[1][0] - modified).total_seconds())))
            attached[log] = SessionName(name.key, name.label, name.directory, "steel_log", name.number)
    return attached


def cer_logs(local_appdata: str | Path | None = None, limit: int = 65536) -> list[dict]:
    """cer.log files whose head names a Revit process (product RVT)."""
    root = Path(local_appdata or os.environ.get("LOCALAPPDATA", "")) / "Autodesk" / "CER"
    if not root.is_dir():
        return []
    found = []
    for path in root.glob("*/cer.log"):
        try:
            with path.open("rb") as stream:
                head = stream.read(limit).decode("utf-8", "replace")
            info = path.stat()
        except OSError:
            continue
        process = _CER_PROCESS.search(head)
        first = _CER_TIME.search(head)
        if not process:
            continue
        started = None
        if first:
            month, day, year, hour, minute, second = map(int, first.groups())
            try:
                started = datetime(2000 + year, month, day, hour, minute, second)
            except ValueError:
                started = None
        found.append({"path": str(path), "pid": int(process.group(1)), "release": int(process.group(2)),
                      "first_time": started.isoformat() if started else None,
                      "modified": datetime.fromtimestamp(info.st_mtime).isoformat(timespec="seconds"),
                      "size": info.st_size})
    return found


def match_cer(summary: dict, year: int | None, logs: list[dict]) -> list[dict]:
    """CER logs of the same Revit process (pid) or, without a pid, overlapping time."""
    if not year:
        return []
    start, end = summary.get("start_time"), summary.get("end_time")
    result = []
    for log in logs:
        if log["release"] != year:
            continue
        basis = ""
        if summary.get("pid") and log["pid"] == summary["pid"]:
            basis = f"进程号{log['pid']}与Journal中的Session{log['pid']}一致"
        elif not summary.get("pid") and start and end and log["first_time"]:
            if log["first_time"] <= end and log["modified"] >= start:
                basis = "时间范围重叠（Journal中没有进程号）"
        if basis:
            result.append({**log, "kind": "cer_log", "basis": basis})
    return result
