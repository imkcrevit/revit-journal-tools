"""Acceptance of the interactive browser in a native Windows console (J20).

Starts `revit-journal browse <journal>` in a NEW console window (conhost) or a
Windows Terminal window (ConPTY), attaches to that console, types keys with
WriteConsoleInputW and reads the visible screen with ReadConsoleOutputCharacterW.
Checks each key's visible effect, then that q (exit 0) or Ctrl+C (exit 130)
restores the console mode and leaves the alternate screen.

  py -3 scripts/windows_console_probe.py <journal.txt>                 # conhost
  py -3 scripts/windows_console_probe.py <journal.txt> --host wt       # Windows Terminal
  py -3 scripts/windows_console_probe.py <journal.txt> --ctrl-c

Opens a window on the desktop for a few seconds. Journal text is read from the
screen only to check the keys; it is printed only with --show. The temporary
index database is deleted afterwards.
"""

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from ctypes import wintypes

REPO = Path(__file__).resolve().parent.parent

k = ctypes.WinDLL("kernel32", use_last_error=True)


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD), ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD), ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class INPUT_RECORD(ctypes.Structure):
    class _E(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("pad", ctypes.c_byte * 16)]
    _fields_ = [("EventType", wintypes.WORD), ("Event", _E)]


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wintypes.WORD),
                ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]


k.CreateFileW.restype = wintypes.HANDLE
GENERIC_RW = 0xC0000000
SHARE_RW = 3
OPEN_EXISTING = 3
VKEYS = {"DOWN": (0x28, 0x50), "UP": (0x26, 0x48), "ENTER": (0x0D, 0x1C), "ESC": (0x1B, 0x01)}


class Process:
    """Minimal Popen-like wrapper for a process this probe did not start."""

    def __init__(self, pid):
        self.pid = pid
        self.handle = k.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, pid)  # SYNCHRONIZE|QUERY_LIMITED|TERMINATE

    def poll(self):
        code = wintypes.DWORD()
        k.GetExitCodeProcess(self.handle, ctypes.byref(code))
        return None if code.value == 259 else code.value

    def wait(self, timeout):
        if k.WaitForSingleObject(self.handle, int(timeout * 1000)) != 0:
            raise TimeoutError("child still running")
        return self.poll()

    def kill(self):
        k.TerminateProcess(self.handle, 1)


k.OpenProcess.restype = wintypes.HANDLE


def handle(name):
    h = k.CreateFileW(name, GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
    if h in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), name)
    return h


def send(conin, key):
    records = (INPUT_RECORD * 2)()
    for i, down in enumerate((True, False)):
        r = records[i]
        r.EventType = 1
        e = r.Event.KeyEvent
        e.bKeyDown, e.wRepeatCount = down, 1
        if key in VKEYS:
            e.wVirtualKeyCode, e.wVirtualScanCode = VKEYS[key]
            e.uChar = "\r" if key == "ENTER" else "\x1b" if key == "ESC" else "\x00"
        else:
            e.uChar = key
            e.wVirtualKeyCode = ord(key.upper()) if key.isalnum() else 0
    written = wintypes.DWORD()
    if not k.WriteConsoleInputW(conin, records, 2, ctypes.byref(written)):
        raise OSError(ctypes.get_last_error(), "WriteConsoleInputW")


def screen():
    out = handle("CONOUT$")  # the active buffer, i.e. the alternate screen while the TUI runs
    try:
        info = CSBI()
        k.GetConsoleScreenBufferInfo(out, ctypes.byref(info))
        w = info.srWindow
        width = w.Right - w.Left + 1
        rows = []
        for y in range(w.Top, w.Bottom + 1):
            buf = ctypes.create_unicode_buffer(width + 1)
            n = wintypes.DWORD()
            k.ReadConsoleOutputCharacterW(out, buf, width, COORD(w.Left, y), ctypes.byref(n))
            rows.append(buf.value[:n.value].rstrip())
        mode = wintypes.DWORD()
        k.GetConsoleMode(out, ctypes.byref(mode))
        return rows, mode.value, (width, w.Bottom - w.Top + 1)
    finally:
        k.CloseHandle(out)


def wait_for(predicate, timeout):
    end = time.time() + timeout
    while time.time() < end:
        rows, mode, size = screen()
        if predicate("\n".join(rows)):
            return rows
        time.sleep(0.2)
    raise TimeoutError("\n".join(screen()[0]))


def show(title, rows, limit=8):
    print(f"--- {title}")
    for row in [row for row in rows if row][:limit]:
        print("   ", row[:110])


def show(title, rows, args, limit=4):
    print(f"--- {title}")
    if args.show:
        for row in [row for row in rows if row][:limit]:
            print("   ", row[:110])


def start(args, database):
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    env.pop("TERM", None)
    # Processes started from some tool shells inherit "ignore Ctrl+C"; a user's
    # console does not, so the child re-enables it before running the CLI.
    boot = ("import ctypes,sys,runpy;ctypes.windll.kernel32.SetConsoleCtrlHandler(None,False);"
            "sys.argv=['revit-journal',*sys.argv[1:]];runpy.run_module('revit_journal_tools',run_name='__main__')")
    command = [sys.executable, "-c", boot, "browse", str(args.journal), "--database", str(database)]
    if args.host == "conhost":
        return subprocess.Popen(command, creationflags=subprocess.CREATE_NEW_CONSOLE, env=env)
    # Windows Terminal starts the command itself (";" separates wt commands); find it by its database path.
    subprocess.Popen(["wt.exe", "-w", "new", *(part.replace(";", "\\;") for part in command)], env=env)
    query = ("(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object "
             f"{{ $_.CommandLine -like '*{database.parent.name}*' -and $_.CommandLine -like '*SetConsoleCtrlHandler*' }}).ProcessId")
    for _ in range(100):
        found = subprocess.run(["powershell", "-NoProfile", "-Command", query], capture_output=True, text=True).stdout.split()
        if found:
            return Process(int(found[0]))
        time.sleep(0.2)
    raise SystemExit("没有找到Windows Terminal中的进程")


def drive(child, args) -> bool:
    time.sleep(1.0)
    k.FreeConsole()
    for _ in range(50):
        if k.AttachConsole(child.pid):
            break
        time.sleep(0.1)
    else:
        raise OSError(ctypes.get_last_error(), "AttachConsole")
    k.SetConsoleCtrlHandler(None, True)  # the probe itself ignores the Ctrl+C it sends
    conin = handle("CONIN$")
    first = wait_for(lambda text: "L1" in text and "块" in text, args.timeout)
    _, mode_running, size = screen()
    print(f"console {size[0]}x{size[1]}; output mode while running {mode_running:#x} "
          f"(VT {'on' if mode_running & 4 else 'off'})")
    show("start", first, args, 8)
    if args.ctrl_c:
        k.GenerateConsoleCtrlEvent(0, 0)
        code = child.wait(15)
    else:
        name = args.journal.name
        steps = [("s", "版本："), ("s", None), ("e", "结束前最后"), ("e", None), ("P", "阶段: 启动"), ("P", None),
                 ("z", "已展开为逐条段落"), ("z", "已折叠"), ("DOWN", None), ("ENTER", "展开原文"), ("ESC", None),
                 ("f", f"[ready] {name}"), ("ESC", None),
                 ("g", None), ("1", None), ("0", None), ("ENTER", "10"), ("ESC", None),
                 ("/", None), *((char, None) for char in args.search), ("ENTER", f"搜索: {args.search}")]
        for key, expect in steps:
            send(conin, key)
            time.sleep(0.6)
            rows, _, _ = screen()
            if expect and expect not in "\n".join(rows):
                print(f"FAIL key {key!r}: {expect!r} not on screen")
                show("screen", rows, args, 30)
                return False
            if expect:
                print(f"ok   key {key!r} -> {expect!r}")
        send(conin, "q")
        code = child.wait(15)
    rows, mode_after, _ = screen()
    left = not any("L1" in row and "块" in row for row in rows)
    expected = 130 if args.ctrl_c else 0
    print(f"{'Ctrl+C' if args.ctrl_c else 'q'}: exit {code} (expected {expected}); output mode after {mode_after:#x}; "
          f"alternate screen left: {left}")
    # conhost starts without VT processing, so the restored mode has it cleared again.
    restored = not mode_after & 4 if args.host == "conhost" else True
    return code == expected and left and restored


def main() -> int:
    parser = argparse.ArgumentParser(description="原生Windows控制台中的交互界面验收")
    parser.add_argument("journal", type=Path)
    parser.add_argument("--host", choices=("conhost", "wt"), default="conhost")
    parser.add_argument("--ctrl-c", action="store_true", help="用Ctrl+C退出，而不是逐个按键检查后按q")
    parser.add_argument("--search", default="Command", help="检查搜索用的关键词")
    parser.add_argument("--timeout", type=float, default=60, help="等待首屏的秒数（含建索引）")
    parser.add_argument("--show", action="store_true", help="打印屏幕内容（含Journal原文）")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("只能在Windows上运行")
    args.journal = args.journal.resolve()
    folder = Path(tempfile.mkdtemp(prefix="rjt-console-"))
    child = None
    try:
        child = start(args, folder / "probe.sqlite3")
        passed = drive(child, args)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            print("超时：已结束被测进程")
        time.sleep(0.5)
        shutil.rmtree(folder, ignore_errors=True)
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
