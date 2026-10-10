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
