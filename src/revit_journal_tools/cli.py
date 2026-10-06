"""Local CLI: no network, Revit process, model mutation, or Journal replay."""

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .analyzer import ScanOptions, analyze
from .models import SEVERITY_RANK
from .reporting import write_reports
from .rules import load_rules

TEXT_SUFFIXES = {".txt", ".log", ".journal"}


def _is_text_input(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name.lower().endswith(".txt.abbrev")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Revit Journal读取与错误候选定位")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="扫描文本文件或目录")
    scan.add_argument("paths", nargs="+", type=Path, help="文件或目录，可传多个")
    scan.add_argument("--recursive", action="store_true", help="递归扫描目录")
    scan.add_argument("--encoding", default="auto", help="auto识别BOM，否则严格UTF-8；可指定gb18030等")
    scan.add_argument("--context", type=int, default=3, help="命中行前后各保留多少行，0到100")
    scan.add_argument("--min-severity", choices=SEVERITY_RANK, default="warning")
    scan.add_argument("--max-findings", type=int, default=500, help="每文件最多保留多少条候选明细")
    scan.add_argument("--max-groups", type=int, default=500, help="每文件最多跟踪多少个精确重复组")
    scan.add_argument("--max-line-chars", type=int, default=1_048_576, help="单行读取上限，超出时报错")
    scan.add_argument("--rules", type=Path, help="追加自定义JSON规则")
    scan.add_argument("--format", choices=("text", "json", "jsonl"), default="text")
    scan.add_argument("-o", "--output", type=Path, help="输出UTF-8报告；省略则写到标准输出")
    scan.add_argument("--fail-on", choices=SEVERITY_RANK, help="达到指定候选级别时返回退出码1")
    rules = commands.add_parser("rules", help="列出规则及其来源")
    rules.add_argument("--rules", type=Path, help="追加自定义JSON规则")
    view = commands.add_parser("view", help="启动本地Journal查看器")
    view.add_argument("paths", nargs="+", type=Path, help="需要打开的日志文件或目录")
    view.add_argument("--recursive", action="store_true", help="包括子目录中的日志")
    view.add_argument("--encoding", default="auto")
    view.add_argument("--port", type=int, default=8765)
    view.add_argument("--database", type=Path, default=Path(".journal-cache/viewer.sqlite3"))
    view.set_defaults(rules=None)
    return parser


def _error(path: Path, message: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "source": str(path.absolute()), "status": "error",
        "scan_complete": False, "error": message,
    }


def _discover(paths: list[Path], recursive: bool) -> tuple[list[Path], list[dict[str, Any]]]:
    found: set[Path] = set()
    errors: list[dict[str, Any]] = []
    for path in paths:
        try:
            if path.is_file():
                found.add(path.resolve())
            elif path.is_dir():
                iterator = path.rglob("*") if recursive else path.iterdir()
                members = {item.resolve() for item in iterator if item.is_file() and _is_text_input(item)}
                if members:
                    found.update(members)
                else:
                    errors.append(_error(path, "目录内没有.txt、.log、.journal或.txt.abbrev文件；子目录需--recursive"))
            else:
                errors.append(_error(path, "路径不存在或不是普通文件/目录"))
        except OSError as error:
            errors.append(_error(path, str(error)))
    return sorted(found), errors


def _protect_inputs(output: Path | None, inputs: list[Path]) -> None:
    if output is None:
        return
    target = output.resolve()
    for path in inputs:
        if target == path.resolve() or (target.exists() and path.exists() and os.path.samefile(target, path)):
            raise ValueError(f"报告输出不能覆盖输入文件：{path}")


@contextmanager
def _destination(output: Path | None) -> Iterator[TextIO]:
    if output is None:
        yield sys.stdout
        return
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=output.parent,
            prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            yield stream
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    # Keep redirected reports readable on Windows code pages as well as terminals.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        rules = load_rules(args.rules)
        if args.command == "rules":
            print(json.dumps({"schema_version": 1, "rules": [rule.definition() for rule in rules]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "view":
            from .viewer_server import serve

            paths, errors = _discover(args.paths, args.recursive)
            if errors:
                raise ValueError("；".join(item["error"] for item in errors))
            _protect_inputs(args.database, [*paths, *args.paths])
            serve(paths, args.database, args.encoding, args.port)
            return 0
        options = ScanOptions(
            encoding=args.encoding, context=args.context, min_severity=args.min_severity,
            max_findings=args.max_findings, max_groups=args.max_groups,
            max_line_chars=args.max_line_chars,
        )
        paths, input_errors = _discover(args.paths, args.recursive)
        # Also protect the custom rule document, including symlinks and hard links.
        protected = [*paths, *args.paths]
        if args.rules is not None:
            protected.append(args.rules)
        _protect_inputs(args.output, protected)
        exit_code = 2 if input_errors else 0

        def reports() -> Iterator[dict[str, Any]]:
            nonlocal exit_code
            yield from input_errors
            for path in paths:
                try:
                    report = analyze(path, options=options, rules=rules)
                except (OSError, ValueError, LookupError) as error:
                    exit_code = 2
                    yield _error(path, str(error))
                    continue
                if args.fail_on and any(
                    count and SEVERITY_RANK[level] >= SEVERITY_RANK[args.fail_on]
                    for level, count in report.counts_by_severity.items()
                ):
                    exit_code = max(exit_code, 1)
                yield report.to_dict()

        with _destination(args.output) as stream:
            write_reports(reports(), stream, args.format)
        return exit_code
    except (OSError, ValueError, LookupError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
