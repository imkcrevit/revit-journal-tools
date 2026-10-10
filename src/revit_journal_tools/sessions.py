"""Group Journal inputs into Revit sessions by file name; content is never guessed.

Naming observed in %LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit <year>\\Journals:
``journal.NNNN.txt`` (main), ``journal.NNNN.workerN.log`` (worker),
``journal.NNNN.NNNN.dmp`` (binary dump), ``journal.NNNN.txt.copy.txt`` (copy),
``journal.NNNN.txt.abbrev`` (abbreviated) and empty ``dump.NNNN.txt``.
Any other name stays an "unrecognized" single-file session and is still read.
Byte-identical inputs are indexed once; the others are listed as duplicates.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_NAME = re.compile(
    r"^journal\.(?P<number>\d+)(?:(?P<main>\.txt)|(?P<abbrev>\.txt\.abbrev)|(?P<copy>\.txt\.copy\.txt)"
    r"|\.worker(?P<worker>\d+)\.log|\.(?P<dump>\d+)\.dmp)$", re.I,
)
ROLE_LABELS = {
    "main": "主Journal", "abbrev": "精简Journal", "worker": "Worker日志", "copy": "副本",
    "dump": "崩溃转储", "unrecognized": "未识别命名",
}
STATUS_LABELS = {"empty": "空文件，未索引", "duplicate": "字节相同，未重复索引", "binary": "二进制，仅计数"}
_ROLE_ORDER = {"main": 0, "abbrev": 1, "worker": 2, "unrecognized": 3, "copy": 4, "dump": 5}


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


def sort_key(path: Path) -> tuple:
    name = describe(path)
    # Sessions grouped by folder and number; unrecognized names after them.
    return (str(Path(path).parent).lower(), name.number < 0, name.number, name.key,
            _ROLE_ORDER[name.role], Path(path).name.lower())


@dataclass(frozen=True)
class InputFile:
    path: Path
    size: int
    status: str = "index"  # index | empty | duplicate | binary
    duplicate_of: Path | None = None
    sha256: str = ""

    @property
    def session(self) -> SessionName:
        return describe(self.path)

    def sidecar(self) -> dict:
        name = self.session
        return {
            "path": str(self.path), "name": self.path.name, "session": name.key,
            "session_label": name.label, "role": name.role, "size": self.size,
            "status": self.status, "duplicate_of": str(self.duplicate_of) if self.duplicate_of else None,
            "sha256": self.sha256 or None,
        }


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def plan_inputs(paths: list[Path]) -> list[InputFile]:
    """Resolve, classify and deduplicate inputs; order by session then role."""
    unique = sorted({Path(path).resolve() for path in paths}, key=sort_key)
    planned: dict[Path, InputFile] = {}
    by_size: dict[int, list[Path]] = {}
    for path in unique:
        size = path.stat().st_size
        if path.suffix.lower() == ".dmp":
            planned[path] = InputFile(path, size, "binary")
        elif size == 0:
            planned[path] = InputFile(path, size, "empty")
        else:
            planned[path] = InputFile(path, size)
            by_size.setdefault(size, []).append(path)
    for group in by_size.values():
        if len(group) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for path in group:
            by_hash.setdefault(_digest(path), []).append(path)
        for digest, same in by_hash.items():
            if len(same) < 2:
                continue
            same.sort(key=lambda item: (_ROLE_ORDER[describe(item).role], len(str(item)), str(item).lower()))
            primary = same[0]
            planned[primary] = InputFile(primary, planned[primary].size, sha256=digest)
            for other in same[1:]:
                planned[other] = InputFile(other, planned[other].size, "duplicate", primary, digest)
    return [planned[path] for path in unique]
