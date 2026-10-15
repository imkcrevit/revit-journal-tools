"""Find the Journals folders of the Revit versions on this machine (J11).

Revit writes them to %LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit <year>\\Journals.
Only folders are listed; nothing is opened here.
"""

import os
import re
from pathlib import Path

_FOLDER = re.compile(r"^Autodesk Revit (?:LT )?(\d{4})$", re.I)


def journal_folders(years: list[int] | None = None, local_appdata: str | Path | None = None) -> list[tuple[int, Path]]:
    """(year, Journals folder) for every installed version, newest version first."""
    root = Path(local_appdata or os.environ.get("LOCALAPPDATA", "")) / "Autodesk" / "Revit"
    if not root.is_dir():
        return []
    found = []
    for folder in root.iterdir():
        match = _FOLDER.match(folder.name)
        journals = folder / "Journals"
        if match and journals.is_dir():
            year = int(match.group(1))
            if not years or year in years:
                found.append((year, journals))
    return sorted(found, key=lambda item: (-item[0], item[1].name))


def parse_years(values: list[str] | None) -> list[int]:
    years = []
    for value in values or []:
        for part in str(value).replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if not re.fullmatch(r"\d{4}", part):
                raise ValueError(f"--revit需要四位年份，例如2024：{part}")
            years.append(int(part))
    return years


CACHE_ENV = "REVIT_JOURNAL_CACHE"
CACHE_FILES = ("terminal.sqlite3", "viewer.sqlite3", "desktop.sqlite3")


def cache_dir(environ=None, os_name: str | None = None) -> Path:
    r"""Default index cache (J19), never the current directory: REVIT_JOURNAL_CACHE,
    else %LOCALAPPDATA%\revit-journal-tools\cache on Windows and
    $XDG_CACHE_HOME (or ~/.cache)/revit-journal-tools elsewhere."""
    environ = os.environ if environ is None else environ
    if environ.get(CACHE_ENV):
        return Path(environ[CACHE_ENV])
    if (os_name or os.name) == "nt":
        base = environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "revit-journal-tools" / "cache"
    base = environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "revit-journal-tools"


def cache_files(folder: Path) -> list[Path]:
    """Index databases of this tool in folder, with their SQLite -wal/-shm files."""
    return [path for name in CACHE_FILES for suffix in ("", "-wal", "-shm")
            if (path := folder / (name + suffix)).is_file()]
