"""Group Journal inputs into Revit sessions by file name; content is never guessed.

Naming observed in %LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit <year>\\Journals:
``journal.NNNN.txt`` (main), ``journal.NNNN.workerN.log`` (worker),
``journal.NNNN.NNNN.dmp`` (binary dump), ``journal.NNNN.txt.copy.txt`` (copy),
``journal.NNNN.txt.abbrev`` and ``journal.NNNN.txt.copy.txt.abbrev``
(abbreviated) and empty ``dump.NNNN.txt``.
Any other name stays an "unrecognized" single-file session and is still read.
Byte-identical inputs are indexed once; the others are listed as duplicates.
"""

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path

_NAME = re.compile(
    r"^journal\.(?P<number>\d+)(?:(?P<main>\.txt)|(?P<abbrev>\.txt(?:\.copy\.txt)?\.abbrev)|(?P<copy>\.txt\.copy\.txt)"
    r"|\.worker(?P<worker>\d+)\.log|\.(?P<dump>\d+)\.dmp)$", re.I,
)
ROLE_LABELS = {
    "main": "主Journal", "abbrev": "精简Journal", "worker": "Worker日志", "steel_log": "钢连接错误日志",
    "copy": "副本", "dump": "崩溃转储", "unrecognized": "未识别命名",
}
STATUS_LABELS = {"empty": "空文件，未索引", "duplicate": "字节相同，未重复索引", "binary": "二进制，仅计数"}
_ROLE_ORDER = {"main": 0, "abbrev": 1, "worker": 2, "steel_log": 3, "unrecognized": 4, "copy": 5, "dump": 6}


@dataclass(frozen=True)
class SessionName:
    key: str
    label: str
    directory: str
    role: str
    number: int


def describe(path: Path) -> SessionName:
    """Session key, label and role from the file name and folder only."""
    path = Path(path)
    match = _NAME.match(path.name)
    folder = path.parent
    directory = folder.parent.name if folder.name.lower() == "journals" else folder.name
    if match is None:
        return SessionName(str(path), path.name, directory, "unrecognized", -1)
    role = next(name for name in ("main", "abbrev", "copy", "worker", "dump") if match.group(name))
    number = match.group("number")
    return SessionName(f"{folder}::journal.{number}", f"journal.{number}", directory, role, int(number))


def recency_sort(items: list, info) -> list:
    """Newest session first (by its newest member), then main Journal, worker,
    sidecars. info(item) -> (SessionName, mtime_ns)."""
    groups: dict[str, list] = {}
    newest: dict[str, int] = {}
    for item in items:
        name, mtime = info(item)
        groups.setdefault(name.key, []).append(item)
        newest[name.key] = max(newest.get(name.key, 0), mtime)

    def member_key(item):
        name, _ = info(item)
        return _ROLE_ORDER[name.role], str(getattr(item, "path", item)).lower()

    ordered = []
    for key in sorted(groups, key=lambda key: (-newest[key], key)):
        ordered.extend(sorted(groups[key], key=member_key))
    return ordered


@dataclass(frozen=True)
class InputFile:
    path: Path
    size: int
    status: str = "index"  # index | empty | duplicate | binary
    duplicate_of: Path | None = None
    sha256: str = ""
    mtime_ns: int = 0
    # A sidecar log attached to a session by time (see sidecars.py), not by name.
    session_override: SessionName | None = None

    @property
    def session(self) -> SessionName:
        return self.session_override or describe(self.path)

    def sidecar(self) -> dict:
        name = self.session
        return {
            "path": str(self.path), "name": self.path.name, "session": name.key,
            "session_label": name.label, "role": name.role, "size": self.size,
            "status": self.status, "duplicate_of": str(self.duplicate_of) if self.duplicate_of else None,
            "sha256": self.sha256 or None, "mtime_ns": self.mtime_ns,
        }


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def plan_inputs(paths: list[Path], attached: dict[Path, SessionName] | None = None) -> list[InputFile]:
    """Resolve, classify and deduplicate inputs; newest session first.
    attached: extra files (sidecar logs) with the session they belong to."""
    attached = {Path(path).resolve(): name for path, name in (attached or {}).items()}
    unique = sorted({Path(path).resolve() for path in paths} | set(attached))
    planned: dict[Path, InputFile] = {}
    by_size: dict[int, list[Path]] = {}
    for path in unique:
        info = path.stat()
        override = attached.get(path)
        if path.suffix.lower() == ".dmp":
            planned[path] = InputFile(path, info.st_size, "binary", mtime_ns=info.st_mtime_ns)
        elif info.st_size == 0:
            planned[path] = InputFile(path, info.st_size, "empty", mtime_ns=info.st_mtime_ns,
                                      session_override=override)
        else:
            planned[path] = InputFile(path, info.st_size, mtime_ns=info.st_mtime_ns, session_override=override)
            by_size.setdefault(info.st_size, []).append(path)
    for group in by_size.values():
        if len(group) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for path in group:
            by_hash.setdefault(_digest(path), []).append(path)
        for digest, same in by_hash.items():
            if len(same) < 2:
                continue
            same.sort(key=lambda item: (_ROLE_ORDER[planned[item].session.role], len(str(item)), str(item).lower()))
            primary = same[0]
            planned[primary] = replace(planned[primary], sha256=digest)
            for other in same[1:]:
                planned[other] = replace(planned[other], status="duplicate", duplicate_of=primary, sha256=digest)
    return recency_sort(list(planned.values()), lambda item: (item.session, item.mtime_ns))
