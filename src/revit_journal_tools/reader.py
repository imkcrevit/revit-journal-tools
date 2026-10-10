"""Bounded line reads, per-file encoding decisions, and a digest of the exact input bytes.

Encoding policy (see docs/architecture.md):

- ``auto`` (default): a BOM decides; otherwise strict UTF-8, then the strict
  superset of the system ANSI code page (cp936 -> gb18030). Each file is decided
  on its own. If every candidate fails, the file is still read with the last
  candidate, but each undecodable byte is shown as ``\\xNN`` and the line is
  flagged ``degraded``; reports carry the count and the line numbers. Bytes are
  never silently replaced or dropped.
- a single codec name, e.g. ``cp1252``: strict, as before; any undecodable byte
  fails the whole file.
- a comma-separated chain, e.g. ``utf-8,gb18030,cp1252``: like ``auto`` with
  these candidates (``auto`` inside a chain expands to its own candidates).
"""

import codecs
import hashlib
import io
import locale
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Iterator


class JournalReadError(ValueError):
    """The file could not be read completely and faithfully."""


class SourceChangedError(JournalReadError):
    """The file grew or changed while it was read (typically a live journal)."""


@dataclass(frozen=True)
class JournalLine:
    number: int
    text: str
    degraded: bool = False


# Lone surrogates U+DC00..U+DCFF carry undecodable bytes from the error handler
# to the line loop. No strict decoder used here produces lone surrogates.
_ESCAPE_BASE = 0xDC00
_ESCAPED = re.compile("[" + chr(_ESCAPE_BASE) + "-" + chr(_ESCAPE_BASE + 0xFF) + "]")
_DEGRADED_ERRORS = "revit-journal-degraded"
_DEGRADED_SAMPLE = 20
# Supersets that strictly decode everything the system code page decodes.
_SUPERSETS = {"gbk": "gb18030", "gb2312": "gb18030", "cp936": "gb18030"}


def _degraded_handler(error: UnicodeError) -> tuple[str, int]:
    if not isinstance(error, UnicodeDecodeError):
        raise error
    bad = error.object[error.start:error.end]
    if not str(error.encoding).lower().replace("_", "-").startswith(("utf-16", "utf-32")):
        # Never swallow a line break into an escape: line numbers must not move.
        for position, value in enumerate(bad):
            if value in (10, 13) and position:
                bad = bad[:position]
                break
    return "".join(chr(_ESCAPE_BASE + value) for value in bad), error.start + len(bad)


codecs.register_error(_DEGRADED_ERRORS, _degraded_handler)


def _visible(match: re.Match) -> str:
    return "\\x%02x" % (ord(match.group()) - _ESCAPE_BASE)


def system_fallback_encoding() -> str | None:
    """Strict superset of the system ANSI code page, or None when it is UTF-8/ASCII."""
    getter = getattr(locale, "getencoding", None)  # Python 3.11+: ignores UTF-8 mode
    name = getter() if getter else locale.getpreferredencoding(False)
    try:
        name = codecs.lookup(name).name
    except LookupError:
        return None
    name = _SUPERSETS.get(name, name)
    return None if name in {"utf-8", "ascii"} else name


@dataclass(frozen=True)
class EncodingPolicy:
    requested: str
    candidates: tuple[str, ...]
    detect: bool  # BOM detection, candidate chain and degraded fallback

    @property
    def key(self) -> str:
        """Stable description used in cache signatures: what was asked, resolved."""
        return ("auto:" if self.detect else "strict:") + ",".join(self.candidates)


def resolve_encoding(requested: str) -> EncodingPolicy:
    parts = [part.strip() for part in str(requested).split(",") if part.strip()]
    if not parts:
        raise LookupError("编码不能为空；可使用auto、单个编码或逗号分隔的候选编码")
    if len(parts) == 1 and parts[0].lower() != "auto":
        return EncodingPolicy(requested, (codecs.lookup(parts[0]).name,), False)
    names: list[str] = []
    for part in parts:
        if part.lower() == "auto":
            fallback = system_fallback_encoding()
            names.extend(["utf-8", *([fallback] if fallback else [])])
        else:
            names.append(codecs.lookup(part).name)
    return EncodingPolicy(requested, tuple(dict.fromkeys(names)), True)


def _bom_encoding(header: bytes) -> str | None:
    # UTF-32 BOMs must be checked before the shorter UTF-16 BOMs.
    for marks, name in (
        ((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE), "utf-32"),
        ((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE), "utf-16"),
        ((codecs.BOM_UTF8,), "utf-8-sig"),
    ):
        if any(header.startswith(mark) for mark in marks):
            return name
    return None


def _decodes_strictly(raw: io.BufferedReader, encoding: str) -> bool:
    decoder = codecs.getincrementaldecoder(encoding)("strict")
    raw.seek(0)
    try:
        while chunk := raw.read(1 << 20):
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _signature(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


class _DigestReader(io.RawIOBase):
    def __init__(self, raw: io.BufferedReader):
        super().__init__()
        self.raw = raw
        self.digest = hashlib.sha256()
        self.byte_count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray) -> int:
        count = self.raw.readinto(buffer)
        if count:
            self.digest.update(memoryview(buffer)[:count])
            self.byte_count += count
        return count

    def close(self) -> None:
        try:
            self.raw.close()
        finally:
            super().close()


class JournalReader:
    def __init__(
        self, path: str | Path, encoding: str = "auto", max_line_chars: int = 1_048_576
    ):
        if max_line_chars < 1:
            raise ValueError("max_line_chars必须大于0")
        self.path = Path(path).resolve()
        self.requested_encoding = encoding
        self.policy = resolve_encoding(encoding)
        self.max_line_chars = max_line_chars
        self.encoding = ""
        self.encoding_fallback = False
        self.degraded = False
        self.degraded_line_count = 0
        self.degraded_lines: list[int] = []
        self.line_count = 0
        self.complete = False
        self._text: io.TextIOWrapper | None = None

    def _choose(self, raw: io.BufferedReader) -> tuple[str, bool]:
        """Return (encoding, degraded). Explicit single codecs stay strict."""
        if not self.policy.detect:
            return self.policy.candidates[0], False
        bom = _bom_encoding(raw.read(4))
        candidates = (bom,) if bom else self.policy.candidates
        for encoding in candidates:
            if _decodes_strictly(raw, encoding):
                return encoding, False
        return candidates[-1], True

    def __enter__(self) -> "JournalReader":
        if self.path.suffix.lower() == ".dmp":
            raise JournalReadError("暂不支持二进制.dmp文件，请提供Journal文本")
        raw = self.path.open("rb")
        try:
            self._initial_stat = os.fstat(raw.fileno())
            self.encoding, self.degraded = self._choose(raw)
            first = self.policy.candidates[0]
            self.encoding_fallback = self.degraded or (
                self.policy.detect and self.encoding not in {first, "utf-8-sig", "utf-16", "utf-32"})
            raw.seek(0)
            self._reader = _DigestReader(raw)
            self._text = io.TextIOWrapper(
                io.BufferedReader(self._reader), encoding=self.encoding,
                errors=_DEGRADED_ERRORS if self.degraded else "strict", newline=None,
            )
        except BaseException:
            raw.close()
            raise
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        if self._text is not None:
            self._text.close()

    def __iter__(self) -> Iterator[JournalLine]:
        if self._text is None:
            raise RuntimeError("请通过with JournalReader(...)读取文件")
        if self.complete:
            raise RuntimeError("读取器只能遍历一次")
        degraded_mode = self.degraded
        try:
            while True:
                value = self._text.readline(self.max_line_chars + 2)
                if not value:
                    break
                self.line_count += 1
                text = value.removesuffix("\n")
                if len(text) > self.max_line_chars:
                    raise JournalReadError(
                        f"第{self.line_count}行超过{self.max_line_chars}字符；"
                        "扫描未完成，请增大--max-line-chars后重试"
                    )
                flagged = False
                if degraded_mode and not text.isascii() and _ESCAPED.search(text):
                    # Each undecodable byte becomes the visible text \xNN.
                    text = _ESCAPED.sub(_visible, text)
                    flagged = True
                    self.degraded_line_count += 1
                    if len(self.degraded_lines) < _DEGRADED_SAMPLE:
                        self.degraded_lines.append(self.line_count)
                if "\x00" in text:
                    raise JournalReadError(
                        f"第{self.line_count}行含NUL字符，可能是二进制或编码不符；"
                        "无BOM的UTF-16请显式指定--encoding utf-16-le或utf-16-be"
                    )
                yield JournalLine(self.line_count, text, flagged) if flagged else JournalLine(self.line_count, text)
        except UnicodeError as error:
            raise JournalReadError(
                f"无法按{self.encoding}严格解码文件（已交付{self.line_count}行）；"
                "请指定正确的--encoding，例如gb18030或cp1252，或使用默认的auto；未忽略损坏字节"
            ) from error
        current = os.fstat(self._reader.raw.fileno())
        if (
            _signature(current) != _signature(self._initial_stat)
            or _signature(self.path.stat()) != _signature(self._initial_stat)
            or self._reader.byte_count != self._initial_stat.st_size
        ):
            raise SourceChangedError("扫描期间源文件发生变化；正在写入的Journal请使用默认的--snapshot auto或--snapshot always")
        self.complete = True

    def decoding_summary(self) -> dict:
        """Explicit, serializable decoding state for reports and the index."""
        return {
            "encoding": self.encoding, "encoding_requested": self.requested_encoding,
            "encoding_fallback": self.encoding_fallback,
            "decode_degraded_line_count": self.degraded_line_count,
            "decode_degraded_lines": list(self.degraded_lines),
        }

    @property
    def sha256(self) -> str:
        if not self.complete:
            raise RuntimeError("文件尚未完整读取，不能给出最终哈希")
        return self._reader.digest.hexdigest()

    @property
    def byte_count(self) -> int:
        return self._reader.byte_count
