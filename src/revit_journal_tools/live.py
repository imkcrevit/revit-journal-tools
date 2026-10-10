"""Journals that Revit is still writing (J3): detect them and read a stable snapshot.

Revit keeps the current session's journal open for writing and appends to it.
Reading it in place can see the file grow mid-read. A snapshot copies the
complete lines present at one moment (the bytes up to the last line break) to a
private temporary file; the copy is indexed and hashed, the original is only
read. Detection opens the file for reading without write sharing: Windows then
refuses with a sharing violation while another process holds it for writing.
Nothing is locked, changed or kept open.
"""

import codecs
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

SNAPSHOT_MODES = ("auto", "always", "never")
_SHARING_VIOLATION = 32


def held_for_writing(path: str | Path) -> bool:
    """True when another process has the file open for writing (Windows only)."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, share_read, open_existing = 0x80000000, 0x00000001, 3
    handle = kernel32.CreateFileW(str(path), generic_read, share_read, None, open_existing, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        return ctypes.get_last_error() == _SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return False


def _line_break_end(data: bytes, unit: int, big_endian: bool) -> int:
    """Length of the prefix that ends with a complete line break."""
    if unit == 1:
        return max(data.rfind(b"\n"), data.rfind(b"\r")) + 1
    marks = [codecs.encode(char, "utf-16-be" if big_endian else "utf-16-le") if unit == 2
             else codecs.encode(char, "utf-32-be" if big_endian else "utf-32-le") for char in ("\n", "\r")]
    best = 0
    for mark in marks:
        position = data.rfind(mark)
        while position >= 0 and position % unit:
            position = data.rfind(mark, 0, position)
        best = max(best, position + unit if position >= 0 else 0)
    return best


def take_snapshot(path: str | Path, directory: str | Path) -> dict:
    """Copy the complete lines currently in `path` into `directory`."""
    path = Path(path)
    source_size = path.stat().st_size
    taken_at = datetime.now().isoformat(timespec="seconds")
    with path.open("rb") as stream:
        data = stream.read(source_size)
    header = data[:4]
    unit, big_endian = 1, False
    if header.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        unit, big_endian = 4, header.startswith(codecs.BOM_UTF32_BE)
    elif header.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        unit, big_endian = 2, header.startswith(codecs.BOM_UTF16_BE)
    end = _line_break_end(data, unit, big_endian)
    target = Path(directory) / path.name
    target.write_bytes(data[:end])
    return {"path": str(target), "bytes": end, "source_size": source_size, "read_bytes": len(data),
            "trimmed_bytes": len(data) - end, "taken_at": taken_at}


class Snapshot:
    """Context manager: a private snapshot directory removed afterwards."""

    def __init__(self, path: str | Path):
        self.source = Path(path)
        self.directory = None
        self.info: dict = {}

    def __enter__(self) -> dict:
        self.directory = tempfile.mkdtemp(prefix="revit-journal-snapshot-")
        self.info = take_snapshot(self.source, self.directory)
        return self.info

    def __exit__(self, *exc) -> None:
        if self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)


def wants_snapshot(path: str | Path, mode: str) -> bool:
    if mode not in SNAPSHOT_MODES:
        raise ValueError("snapshot必须为auto、always或never")
    return mode == "always" or (mode == "auto" and held_for_writing(path))
