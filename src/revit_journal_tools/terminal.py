"""Offline, dependency-free terminal browser over the shared paragraph index."""

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
from collections import Counter
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .blocks import PHASES
from .loading import ADDIN_FILTERS, ADDIN_KEYS, BLOCK_FILTERS, block_label, block_summary
from .session_view import (duration_text, file_date, main_tail, phase_label, session_label, sidecar_label,
                           state_text, summary_lines)
from .reader import basis_label
from .viewer_index import ViewerIndex

COLORS = {"startup": 34, "system": 36, "error": 31, "api_error": 35,
          "operation": 32, "warning": 33, "api": 34, "other": 90, "dialog": 96}
PHASE_CYCLE = ["all", *PHASES]


def safe_text(text: str) -> str:
    """A Journal must never be able to send commands/OSC escapes to a terminal."""
    result = []
    for char in str(text):
        if char == "\t":
            result.append("    ")
        elif unicodedata.category(char).startswith("C"):
            result.append(f"\\u{ord(char):04x}")
        else:
            result.append(char)
    return "".join(result)


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def crop(text: str, width: int, offset: int = 0) -> str:
    result, position, used = [], 0, 0
    for char in safe_text(text):
        size = cell_width(char)
        position += size
        if position <= offset:
            continue
        if used + size > max(0, width):
            break
        result.append(char)
        used += size
    return "".join(result)


def encoding_label(file: dict) -> str:
    """Actual per-file encoding plus fallback/degraded state; never hidden."""
    text = file.get("encoding") or "未知编码"
    if file.get("degraded_lines"):
        text += f"（退化解码{file['degraded_lines']}行，字节以\\xNN显示）"
    elif file.get("encoding_basis") == "unlikely_chinese":
        text += "（编码存疑：内容不像中文，可能是乱码，请用--encoding指定）"
    elif file.get("encoding_fallback"):
        text += f"（{basis_label(file.get('encoding_basis', '')) or '自动回退'}）"
    return text


def live_label(file: dict) -> str:
    """Revit still writes this file / it was indexed from a snapshot."""
    if file.get("writing"):
        snapshot = file.get("snapshot") or {}
        when = f"，快照{snapshot['taken_at'][11:19]}" if snapshot.get("taken_at") else ""
        return f" [正在写入{when}]"
    if file.get("live"):
        return " [实时快照]"
    return ""


def colored(text: str, category: str, enabled: bool, selected: bool = False) -> str:
    if not enabled:
        return text
    style = f"{COLORS.get(category, 0)}" + (";7" if selected else "")
    return f"\x1b[{style}m{text}\x1b[0m"


@contextmanager
def keyboard():
    """Restore the console on normal exit, Ctrl+C, and exceptions."""
    if os.name == "nt":
        import ctypes
        import msvcrt

        kernel = ctypes.windll.kernel32
        kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel.GetStdHandle.restype = ctypes.c_void_p
        kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            raise ValueError("当前输出不是Windows控制台，不能进入交互模式")
        if not kernel.SetConsoleMode(handle, mode.value | 4):
            raise ValueError("控制台不支持VT控制序列（旧版conhost），不能进入交互模式；可改用Windows Terminal")

        def read_key():
            if not msvcrt.kbhit():
                time.sleep(0.05)
                return ""
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT", "I": "PGUP", "Q": "PGDN"}.get(msvcrt.getwch(), "")
            return key

        try:
            yield read_key
        finally:
            kernel.SetConsoleMode(handle, mode.value)
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        tty.setcbreak(fd)

        def read_key():
            if not select.select([fd], [], [], 0.15)[0]:
                return ""
            data = os.read(fd, 1)
            if data == b"\x1b":
                for _ in range(8):
                    if not select.select([fd], [], [], 0.015)[0]:
                        break
                    data += os.read(fd, 1)
                    if data[-1:] in b"ABCD~":
                        break
                return {b"\x1b[A": "UP", b"\x1b[B": "DOWN", b"\x1b[C": "RIGHT",
                        b"\x1b[D": "LEFT", b"\x1b[5~": "PGUP", b"\x1b[6~": "PGDN"}.get(data, "\x1b")
            if data and data[0] >= 0xC0:
                count = 1 if data[0] < 0xE0 else 2 if data[0] < 0xF0 else 3
                for _ in range(count):
                    if select.select([fd], [], [], 0.05)[0]:
                        data += os.read(fd, 1)
            return data.decode("utf-8", errors="replace") if data else "\x04"

        try:
            yield read_key
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)


class Browser:
    def __init__(self, index: ViewerIndex, category="all", query="", page_size=100, after=0, addin_prefix="",
                 phase="all", fold=True):
        self.index = index
        self.category, self.query, self.page_size, self.after = category, query, page_size, after
        self.addin_prefix = addin_prefix.strip()
        if self.addin_prefix and self.category == "all":
            self.category = "addin"
        self.phase, self.fold = phase, fold
        self.view = "blocks"  # blocks | summary | tail
        self.sessions: list[dict] = []
        self.catalog = index.catalog()
        # Newest main journal first (J11); otherwise the first non-empty file.
        self.file_position = next((i for i, item in enumerate(self.catalog) if item["id"] == index.preferred_id),
                                  next((i for i, item in enumerate(self.catalog) if item["size"] > 0), 0))
        self.file_cursor = self.file_position
        self.history: list[int] = []
        self.items: list[dict] = []
        self.selected = 0
        self.has_more = False
        self.expanded = False
        self.detail_offset = 0
        self.horizontal = 0
        self.density = 0
        self.mode = "blocks"
        self.prompt = ""
        self.message = "Enter展开/折叠；字体缩放使用终端自身快捷键。"
        self.refresh()

    @property
    def file(self):
        return self.catalog[self.file_position] if self.catalog else None

    @property
    def block(self):
        return self.items[self.selected] if self.items else None

    def file_rows(self) -> list[tuple[str, int | None]]:
        """Session headers, selectable indexed files and display-only sidecars."""
        positions = {item["id"]: number for number, item in enumerate(self.catalog)}
        files = {item["id"]: item for item in self.catalog}
        rows: list[tuple[str, int | None]] = []
        for session in self.sessions:
            rows.append((session_label(session, files), None))
            for file_id in session["files"]:
                if file_id not in positions:
                    continue
                number, item = positions[file_id], files[file_id]
                marker = ">" if number == self.file_cursor else " "
                rows.append((f"{marker} {number + 1}. [{item['status']}] {item['name']} [{item['kind']}] "
                             f"{file_date(item)} {item['size'] / 1024 / 1024:.1f}MB{live_label(item)} | "
                             f"{encoding_label(item)}", number))
            rows.extend(("    " + sidecar_label(side), None) for side in session["sidecars"])
        return rows

    def summary_tail(self, limit: int = 10) -> list[dict]:
        session = self.session or {}
        main = next((item for item in self.catalog if item["path"] == session.get("main")), None)
        if not main or main["status"] != "ready":
            return []
        key = (main["id"], main["sha256"])
        if getattr(self, "_tail_key", None) != key:
            self._tail_key, self._tail = key, self.index.tail_blocks(main["id"], limit)
        return self._tail

    def occurrences(self, block: dict) -> dict:
        key = (self.file["id"], block["fingerprint"])
        if getattr(self, "_occurrence_key", None) != key:
            self._occurrence_key = key
            self._occurrences = self.index.fold_members(self.file["id"], block["fingerprint"], 12)
        return self._occurrences

    @property
    def session(self) -> dict | None:
        if not self.file:
            return None
        return next((item for item in self.sessions if self.file["id"] in item["files"]), None)

    def refresh(self):
        self.catalog = self.index.catalog()
        self.sessions = self.index.sessions(self.catalog)
        self.items, self.has_more = [], False
        if self.file and self.file["status"] == "ready":
            if self.view == "tail":
                self.items = self.index.tail_blocks(self.file["id"], min(200, self.page_size))
            else:
                result = self.index.blocks(self.file["id"], self.category, self.query, self.after, self.page_size,
                                           addin_prefix=self.addin_prefix, phase=self.phase, fold=self.fold)
                self.items, self.has_more = result["items"], result["has_more"]
        self.selected = min(self.selected, max(0, len(self.items) - 1))

    def reset(self):
        self.after, self.selected, self.detail_offset, self.horizontal = 0, 0, 0, 0
        self.history.clear()
        self.expanded = False
        self.refresh()

    def next_page(self):
        if self.has_more and self.items:
            self.history.append(self.after)
            self.after = self.items[-1]["start_line"]
            self.selected = 0
            self.refresh()

    def previous_page(self):
        if self.history:
            self.after = self.history.pop()
            self.selected = 0
            self.refresh()

    def handle(self, key: str, height=24) -> bool:
        if key in ("\x03", "\x04"):
            return False
        if self.mode in {"search", "goto", "prefix"}:
            if key == "\x1b":
                self.mode = "blocks"
            elif key in ("\r", "\n"):
                mode, value = self.mode, self.prompt
                self.mode = "blocks"
                if mode == "search":
                    self.query = value
                    self.reset()
                elif mode == "prefix":
                    self.addin_prefix = value.strip()
                    if self.addin_prefix and self.category == "all":
                        self.category = "addin"
                    self.reset()
                else:
                    if not value.isdecimal() or not self.file:
                        raise ValueError("请输入有效原始行号")
                    target = self.index.block_at(self.file["id"], int(value))
                    self.category, self.query = "all", ""
                    self.addin_prefix, self.phase, self.view = "", "all", "blocks"
                    if self.fold:
                        self.fold = False  # The target may be a later copy of a folded block.
                        self.message = "已关闭折叠以显示原位置；z重新折叠。"
                    self.reset()
                    self.after = target["start_line"] - 1
                    self.refresh()
                    self.expanded = True
                    self.detail_offset = int(value) - target["start_line"]
            elif key in ("\x7f", "\b"):
                self.prompt = self.prompt[:-1]
            elif len(key) == 1 and key.isprintable() and len(self.prompt) < 256:
                self.prompt += key
            return True
        if self.mode == "files":
            if key in ("q", "\x1b", "f"):
                self.mode = "blocks"
            elif key in ("UP", "k", "DOWN", "j"):
                # Move in visual (session) order over selectable rows only.
                order = [number for _, number in self.file_rows() if number is not None]
                if order:
                    position = order.index(self.file_cursor) if self.file_cursor in order else 0
                    position += -1 if key in ("UP", "k") else 1
                    self.file_cursor = order[max(0, min(len(order) - 1, position))]
            elif key in ("\r", "\n"):
                self.file_position = self.file_cursor
                self.mode = "blocks"
                self.reset()
            return True
        if key == "q":
            return False
        if key == "f":
            self.mode, self.file_cursor = "files", self.file_position
        elif key in ("[", "]") and self.catalog:
            self.file_position = (self.file_position + (1 if key == "]" else -1)) % len(self.catalog)
            self.reset()
        elif key == "/":
            self.mode, self.prompt = "search", self.query
        elif key == "v":
            self.mode, self.prompt = "prefix", self.addin_prefix
        elif key == "g":
            self.mode, self.prompt = "goto", ""
        elif key in ADDIN_KEYS or key == "c" or (len(key) == 1 and key in "0123456789"):
            categories = ["all", *BLOCK_FILTERS]
            if key in ADDIN_KEYS:
                self.category = ADDIN_KEYS[key]
            else:
                self.category = categories[(categories.index(self.category) + 1) % len(categories)] if key == "c" else categories[int(key)]
            self.view = "blocks"
            self.reset()
        elif key == "z":
            self.fold = not self.fold
            self.message = "已折叠内容相同的段落（×N为次数）。" if self.fold else "已展开为逐条段落。"
            self.reset()
        elif key == "P":
            self.phase = PHASE_CYCLE[(PHASE_CYCLE.index(self.phase) + 1) % len(PHASE_CYCLE)]
            self.view = "blocks"
            self.reset()
        elif key in ("s", "e"):
            target = "summary" if key == "s" else "tail"
            self.view = "blocks" if self.view == target else target
            self.reset()
        elif key in ("\r", "\n", " ") and self.block:
            self.expanded = not self.expanded
            self.detail_offset, self.horizontal = 0, 0
        elif key == "\x1b":
            self.expanded = False
        elif key in ("+", "=", "-"):
            self.density = max(0, min(2, self.density + (-1 if key == "-" else 1)))
            self.message = f"预览级别{self.density}；Add-in原文按Enter展开。"
        elif key in ("RIGHT", "LEFT"):
            self.horizontal = max(0, self.horizontal + (16 if key == "RIGHT" else -16))
        elif key in ("n", "p"):
            (self.next_page if key == "n" else self.previous_page)()
            self.detail_offset = 0
        elif key in ("UP", "DOWN", "j", "k", "PGUP", "PGDN"):
            direction = -1 if key in ("UP", "k", "PGUP") else 1
            step = max(1, height - 8) if key in ("PGUP", "PGDN") else 1
            if self.view == "summary":
                self.detail_offset = max(0, self.detail_offset + direction * step)
            elif self.expanded and self.block:
                self.detail_offset = max(0, min(self.block["end_line"] - self.block["start_line"], self.detail_offset + direction * step))
            else:
                self.selected = max(0, min(len(self.items) - 1, self.selected + direction * step))
        return True

    def frame(self, width: int, height: int, color: bool) -> str:
        width = max(1, width - 1)  # Avoid autowrap at the right edge of a terminal.
        height = max(1, height)
        rows = []

        def add(text, category="other", selected=False):
            rows.append(colored(crop(text, width), category, color, selected))

        ready = sum(item["status"] == "ready" for item in self.catalog)
        add(f"Revit Journal | 段落查看器 | 已索引{ready}/{len(self.catalog)} | q退出", "startup")
        if height < 10 or width < 35:
            add("窗口较小，请放大终端。q退出。")
            return "\r\n".join(rows[:height])
        if not self.file:
            add("没有可查看的文件")
            return "\r\n".join(rows)
        session = self.session or {}
        state = state_text(session)
        add(f"{self.file_position + 1}/{len(self.catalog)} {self.file['name']}{live_label(self.file)} | "
            f"{self.file['line_count']:,}行 / {self.file['block_count']:,}块 | {encoding_label(self.file)}"
            + (f" | 会话：{state}" if state else ""), "warning" if session.get("end_state") == "no_clean_exit" else "other")
        add(self.file["path"])
        add(f"分类: {BLOCK_FILTERS.get(self.category, '全部')} | 阶段: {phase_label(self.phase)} | "
            f"折叠: {'开' if self.fold else '关'} | 搜索: {self.query or '无'} | {'展开原文' if self.expanded else '折叠段落'}")
        addin_view = self.category in ADDIN_FILTERS or bool(self.addin_prefix)
        if addin_view and height >= 13:
            counts = self.file["block_counts"]
            add(f"当前日志Add-in: {counts.get('addin', 0)}条加载记录 | Autodesk {counts.get('addin_autodesk', 0)} / 第三方{counts.get('addin_third_party', 0)} / 待定{counts.get('addin_unknown', 0)}")
            add(f"名称/Class前缀: {self.addin_prefix or '不限'} | v修改前缀 | Enter展开原始数据")
        content_rows = max(1, height - len(rows) - 5)
        if self.mode == "files":
            listing = self.file_rows()
            cursor_row = next((row for row, (_, number) in enumerate(listing) if number == self.file_cursor), 0)
            start = max(0, cursor_row - content_rows + 1)
            for text, number in listing[start:start + content_rows]:
                if number is None:
                    add(text, "system")
                else:
                    add(text, selected=number == self.file_cursor)
        elif self.view == "summary":
            lines = summary_lines(session, self.summary_tail()) if session else ["当前文件不属于任何会话"]
            self.detail_offset = min(self.detail_offset, max(0, len(lines) - content_rows))
            for text in lines[self.detail_offset:self.detail_offset + content_rows]:
                add(crop(text, width, self.horizontal), "warning" if text.startswith("  [") else "other")
        elif self.file["status"] != "ready":
            add(f"{self.file['status']}: {self.file.get('error') or '正在后台建立索引，可按f切换已完成的文件。'}", "warning")
        elif self.expanded and self.block:
            block = self.block
            add(f"L{block['start_line']}–{block['end_line']} [{block_label(block)}] {block['boundary']} | "
                f"{PHASES.get(block.get('phase'), '')} | {'完整边界' if block['complete'] else '未闭合'}", block["category"])
            extra = 0
            if block.get("fold_count", 1) > 1 and content_rows >= 3:
                members = self.occurrences(block)
                more = "等" if members["truncated"] else ""
                add(f"同内容共{members['count']}处：" + "、".join(f"L{line}" for line in members["start_lines"]) + more
                    + "（g跳到某行可逐条查看）", block["category"])
                extra += 1
            if block.get("addin_origin") and content_rows >= 3:
                add("Class: " + crop(block.get("addin_class") or "未提供", width - 7, self.horizontal), block["category"])
                extra += 1
            detail = self.index.block_lines(self.file["id"], block["start_line"], self.detail_offset, min(200, max(1, content_rows - 1 - extra)))
            for line in detail["items"]:
                text = line["text"] + (" [单行预览截断]" if line["text_length"] > len(line["text"]) else "")
                text += " [含无法解码字节，以\\xNN显示]" if line.get("degraded") else ""
                # Keep source line numbers fixed while scrolling the body.
                prefix = f"{line['line_number']:>8}  "
                add(prefix + crop(text, width - len(prefix), self.horizontal), block["category"])
        elif self.items:
            if self.view == "tail":
                add(f"结束前最后{len(self.items)}个操作与对话框（e返回段落列表）", "startup")
                content_rows = max(1, content_rows - 1)
            item_height = 1 if addin_view else 1 + self.density * 2
            visible = max(1, content_rows // item_height)
            start = max(0, self.selected - visible + 1)
            for number in range(start, min(len(self.items), start + visible)):
                block = self.items[number]
                prefix = f"{'>' if number == self.selected else ' '} " + ("!未闭合 " if not block["complete"] else "")
                if block.get("fold_count", 1) > 1:
                    prefix += f"×{block['fold_count']} "
                position = f" | L{block['start_line']}–{block['end_line']}"
                title = crop(block_summary(block), max(1, width - cell_width(prefix + position)), self.horizontal)
                add(prefix + title + position, block["category"], number == self.selected)
                if self.density and not block.get("addin_origin"):
                    detail = self.index.block_lines(self.file["id"], block["start_line"], limit=self.density * 2)
                    for line in detail["items"]:
                        add(f"    {line['line_number']}: " + crop(line["text"], width - 15, self.horizontal), block["category"])
        else:
            add("没有匹配段落；0全部分类，/清空搜索，v清空前缀。")
        while len(rows) < height - 5:
            add("")
        rows = rows[:height - 5]
        add("0全部 1启动 2系统 3错误 4API错误 5操作 6警告 7API 8其他 9对话框")
        add("i Add-in列表 a Autodesk加载 t第三方加载 u来源待定加载 v前缀 | s摘要 e结束前 P阶段 z折叠")
        add("↑↓选择/滚动 Enter展开/折叠 n/p翻页 /搜索 g行号 f文件 +/-预览")
        if self.file.get("writing") or self.file.get("live"):
            notice = "实时Journal：显示的是建立索引时的快照，之后写入的内容需重新打开browse读取。"
        elif self.file["source_changed"]:
            notice = "源文件已变化，请退出后重新打开以重建索引。"
        else:
            notice = self.message
        add(notice, "warning" if self.file["source_changed"] else "other")
        if self.mode in {"search", "goto", "prefix"}:
            label = {"search": "搜索关键词: ", "goto": "跳到原始行: ", "prefix": "名称/Class前缀: "}[self.mode]
            add(label + self.prompt + "_", "operation")
        else:
            if self.expanded and self.block:
                add(f"段内第{self.detail_offset + 1}/{self.block['end_line'] - self.block['start_line'] + 1}行 | PgUp/PgDn翻原文 | Enter折叠")
            else:
                add(f"本页{len(self.items)}块 | {'还有下一页(n)' if self.has_more else '已到末页'} | 错误为候选，需结合原文判断")
        return "\r\n".join(rows)


def _session_json(session: dict, sources: dict, tail: list[dict] | None = None) -> dict:
    result = {key: value for key, value in session.items() if key != "files"}
    result["files"] = [sources[file_id] for file_id in session["files"] if file_id in sources]
    if tail is not None:
        result["tail"] = [{key: block[key] for key in ("start_line", "end_line", "category", "phase", "title")}
                          for block in tail]
    return result


def _print_summary(index, format_name, tail_limit=10):
    """`summary`: one entry per session, newest first."""
    catalog = index.catalog()
    sessions = index.sessions(catalog)
    sources = {item["id"]: item["path"] for item in catalog}
    failed = any(item["status"] != "ready" for item in catalog)
    if format_name == "json":
        print(json.dumps({"schema_version": 1, "sessions": [
            _session_json(session, sources, main_tail(index, session, catalog, tail_limit)) for session in sessions],
            "errors": [{"source": item["path"], "error": item["error"]} for item in catalog
                       if item["status"] != "ready"]}, ensure_ascii=False))
        return 2 if failed else 0
    unclean = sum(session.get("end_state") == "no_clean_exit" for session in sessions)
    print(f"共{len(sessions)}个会话；未正常结束{unclean}个；会话级发现均为候选，需结合原文复核。")
    for session in sessions:
        print()
        for text in summary_lines(session, main_tail(index, session, catalog, tail_limit)):
            print(safe_text(text))
    for item in catalog:
        if item["status"] != "ready":
            print(safe_text(f"索引失败：{item['path']}：{item['error']}"))
    return 2 if failed else 0


def _print_pages(index, category, query, after, limit, color, format_name, addin_prefix="", phase="all",
                 fold=True):
    files, failed = {}, False
    catalog = index.catalog()
    for file in catalog:
        result = {"source": file["path"], "status": file["status"], "sha256": file["sha256"],
                  "encoding": file["encoding"], "encoding_requested": file["encoding_requested"],
                  "encoding_fallback": file["encoding_fallback"], "encoding_basis": file["encoding_basis"],
                  "decode_degraded_line_count": file["degraded_lines"],
                  "decode_degraded_lines": file["degraded_sample"],
                  "live": file["live"], "writing": file["writing"], "snapshot": file["snapshot"],
                  "session": file["session_label"], "role": file["role"],
                  "line_count": file["line_count"], "block_count": file["block_count"],
                  "addin_count": file["block_counts"].get("addin", 0), "addin_prefix": addin_prefix,
                  "phase": phase, "fold": fold and not query.strip()}
        if file["status"] != "ready":
            result["error"] = file["error"]
            failed = True
        else:
            result.update(index.blocks(file["id"], category, query, after, limit, addin_prefix=addin_prefix,
                                       phase=phase, fold=fold))
        files[file["id"]] = result
    sessions = index.sessions(catalog)
    if format_name == "json":
        sources = {file_id: result["source"] for file_id, result in files.items()}
        print(json.dumps({"schema_version": 1, "grouping": "paragraphs", "files": list(files.values()),
                          "sessions": [_session_json(session, sources) for session in sessions]},
                         ensure_ascii=False))
        return 2 if failed else 0
    by_id = {item["id"]: item for item in catalog}
    for session in sessions:
        print(safe_text("== " + session_label(session, by_id)))
        for file_id in session["files"]:
            file = files[file_id]
            print(safe_text(f"{file['source']}{live_label(by_id[file_id])} | {file['line_count']}行 / "
                            f"{file['block_count']}块 | {encoding_label(by_id[file_id])}"))
            if by_id[file_id]["degraded_lines"]:
                print("  提示：其他语言系统产生的文件可指定其代码页，如 --encoding cp1252（西欧）、cp1250、cp1251")
            if category in ADDIN_FILTERS or addin_prefix:
                print(safe_text(f"当前日志Add-in: {file['addin_count']}条加载记录 | 名称/Class前缀: {addin_prefix or '不限'}"))
            if "error" in file:
                print(safe_text(f"索引失败：{file['error']}"))
                continue
            for block in file["items"]:
                label = f"L{block['start_line']}–{block['end_line']} "
                if block.get("fold_count", 1) > 1:
                    label += f"×{block['fold_count']}（末次L{block['fold_last_line']}） "
                label += ("[未闭合] " if not block["complete"] else "") + block_summary(block)
                print(colored(safe_text(label), block["category"], color))
            print(f"本页{len(file['items'])}块；" + (f"仍有结果，续页使用--after {file['next_after']}" if file["has_more"] else "已到末页"))
        for side in session["sidecars"]:
            print(safe_text("  " + sidecar_label(side)))
    return 2 if failed else 0


def run(paths, database, encoding, *, category="all", query="", color="auto", print_only=False,
        format_name="text", after=0, page_size=100, addin_prefix="", jobs=None, snapshot="auto", phase="all",
        fold=True, summary_tail=None):
    """browse (interactive or one page per file) or, with summary_tail, the session summary."""
    if not 1 <= page_size <= 200 or after < 0 or len(query) > 256 or len(addin_prefix) > 256:
        raise ValueError("分页或搜索参数超出范围")
    addin_prefix = addin_prefix.strip()
    if addin_prefix and category == "all":
        category = "addin"
    interactive = (summary_tail is None and not print_only and format_name == "text"
                   and sys.stdin.isatty() and sys.stdout.isatty())
    use_color = color == "always" or (color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")
    index = ViewerIndex(database, workers=jobs, snapshot=snapshot)
    queued = index.register(paths, encoding)
    stop = threading.Event()
    failures = []

    def build():
        try:
            index.build(queued, encoding, stop)
        except Exception as error:
            failures.append(str(error))

    worker = threading.Thread(target=build, daemon=True)
    worker.start()
    try:
        if not interactive:
            while worker.is_alive():
                worker.join(0.2)
            if failures:
                raise ValueError(failures[0])
            if summary_tail is not None:
                return _print_summary(index, format_name, summary_tail)
            return _print_pages(index, category, query, after, page_size, use_color, format_name, addin_prefix,
                                phase, fold)
        browser = Browser(index, category, query, page_size, after, addin_prefix, phase, fold)
        console = ExitStack()
        try:
            read_key = console.enter_context(keyboard())
        except (ValueError, OSError) as error:
            # Old conhost without VT processing, or no real console (J20): one page instead.
            print(f"提示：{error}；改为输出一页段落摘要（等同--print）。", file=sys.stderr)
            while worker.is_alive():
                worker.join(0.2)
            if failures:
                raise ValueError(failures[0])
            return _print_pages(index, category, query, after, page_size, False, format_name, addin_prefix,
                                phase, fold)
        with console:
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            try:
                last_frame, last_refresh = "", 0.0
                while True:
                    if time.monotonic() - last_refresh > 1:
                        browser.refresh()
                        last_refresh = time.monotonic()
                    if failures:
                        raise ValueError(failures[0])
                    try:
                        width, height = os.get_terminal_size(sys.stdout.fileno())
                    except OSError:
                        width, height = shutil.get_terminal_size((100, 30))
                    frame = browser.frame(width, height, use_color)
                    if frame != last_frame:
                        sys.stdout.write("\x1b[H\x1b[2J" + frame)
                        sys.stdout.flush()
                        last_frame = frame
                    key = read_key()
                    if key:
                        try:
                            if not browser.handle(key, height):
                                break
                        except (ValueError, KeyError, sqlite3.Error) as error:
                            browser.message = safe_text(str(error))
            finally:
                sys.stdout.write("\x1b[0m\x1b[?25h\x1b[?1049l")
                sys.stdout.flush()
        return 0
    finally:
        stop.set()
        worker.join(timeout=2)
