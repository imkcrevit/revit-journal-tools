"""Bounded line reads, strict decoding, and a digest of the exact input bytes."""

import codecs
import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Iterator


class JournalReadError(ValueError):
    """The file could not be read completely and faithfully."""


@dataclass(frozen=True)
class JournalLine:
    number: int
    text: str


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


def _encoding(header: bytes, requested: str) -> str:
    if requested != "auto":
        return codecs.lookup(requested).name
    # UTF-32 BOMs must be checked before the shorter UTF-16 BOMs.
    for marks, name in (
        ((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE), "utf-32"),
        ((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE), "utf-16"),
        ((codecs.BOM_UTF8,), "utf-8-sig"),
    ):
        if any(header.startswith(mark) for mark in marks):
            return name
    return "utf-8"


def _signature(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


class JournalReader:
    def __init__(
        self, path: str | Path, encoding: str = "auto", max_line_chars: int = 1_048_576
    ):
        if max_line_chars < 1:
            raise ValueError("max_line_chars必须大于0")
        self.path = Path(path).resolve()
        self.requested_encoding = encoding
        self.max_line_chars = max_line_chars
        self.encoding = ""
        self.line_count = 0
        self.complete = False
        self._text: io.TextIOWrapper | None = None

    def __enter__(self) -> "JournalReader":
        if self.path.suffix.lower() == ".dmp":
            raise JournalReadError("暂不支持二进制.dmp文件，请提供Journal文本")
        raw = self.path.open("rb")
        try:
            self._initial_stat = os.fstat(raw.fileno())
            self.encoding = _encoding(raw.read(4), self.requested_encoding)
            raw.seek(0)
            self._reader = _DigestReader(raw)
            self._text = io.TextIOWrapper(
                io.BufferedReader(self._reader), encoding=self.encoding,
                errors="strict", newline=None,
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
                if "\x00" in text:
                    raise JournalReadError(
                        f"第{self.line_count}行含NUL字符，可能是二进制或编码不符；"
                        "无BOM的UTF-16请显式指定--encoding utf-16-le或utf-16-be"
                    )
                yield JournalLine(self.line_count, text)
        except UnicodeError as error:
            raise JournalReadError(
                f"无法按{self.encoding}严格解码文件（已交付{self.line_count}行）；"
                "请指定正确的--encoding，例如gb18030或cp1252；未忽略损坏字节"
            ) from error
        current = os.fstat(self._reader.raw.fileno())
        if (
            _signature(current) != _signature(self._initial_stat)
            or _signature(self.path.stat()) != _signature(self._initial_stat)
            or self._reader.byte_count != self._initial_stat.st_size
        ):
            raise JournalReadError("扫描期间源文件发生变化，请复制已停止写入的Journal后重试")
        self.complete = True

    @property
    def sha256(self) -> str:
        if not self.complete:
            raise RuntimeError("文件尚未完整读取，不能给出最终哈希")
        return self._reader.digest.hexdigest()

    @property
    def byte_count(self) -> int:
        return self._reader.byte_count
