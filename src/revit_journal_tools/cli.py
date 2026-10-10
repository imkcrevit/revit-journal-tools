"""Local CLI: no network, Revit process, model mutation, or Journal replay."""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .analyzer import ScanOptions, analyze
from .blocks import PHASES
from .discovery import cache_dir, cache_files, journal_folders, parse_years
from .live import SNAPSHOT_MODES
from .models import SEVERITY_RANK
from .loading import BLOCK_FILTERS
from .reader import resolve_encoding
from .reporting import write_reports
from .rules import load_rules
from .sessions import InputFile, plan_inputs
from .sidecars import attach_steel_logs, revit_year

TEXT_SUFFIXES = {".txt", ".log", ".journal"}
ENCODING_HELP = (
    "auto（默认）逐文件判定：BOM→严格UTF-8→系统代码页的超集（cp936用gb18030）；"
    "全部失败时按该编码读取，无法解码的字节以\\xNN显示并在报告中标记退化行。"
    "单个编码（如cp1252）为严格模式；逗号分隔的列表（如utf-8,gb18030,cp1252）按顺序尝试"
)


def _is_text_input(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name.lower().endswith(".txt.abbrev")


def _workspace_options(command: argparse.ArgumentParser, index: bool) -> None:
    command.add_argument("paths", nargs="*", type=Path,
                         help="文件或目录，可传多个；省略时自动查找本机各版本Revit的Journals目录")
    command.add_argument("--recursive", action="store_true", help="递归扫描目录")
    command.add_argument("--encoding", default="auto", help=ENCODING_HELP)
    command.add_argument("--revit", action="append", metavar="YEAR",
                         help="只看指定版本，如--revit 2024；可重复或用逗号分隔")
    command.add_argument("--latest", nargs="?", type=int, const=1, default=0, metavar="N",
                         help="只看最新的N个会话（默认1个）")
    command.add_argument("--snapshot", choices=SNAPSHOT_MODES, default="auto",
                         help="正在写入的Journal：auto对Revit占用或读取中变化的文件读快照（默认），always总是读快照，never视为读取失败")
    if index:
        command.add_argument("--jobs", type=int, default=None, metavar="N",
                             help="多文件建索引的进程数；默认逻辑核数减1、最多8，1为顺序")
        command.add_argument("--database", type=Path, default=None,
                             help="索引数据库；默认在缓存目录（revit-journal cache查看位置）")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Revit Journal读取与错误候选定位")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="扫描文本文件或目录")
    _workspace_options(scan, index=False)
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
    browse = commands.add_parser("browse", help="打开CLI终端段落查看器（颜色、折叠、搜索）")
    _workspace_options(browse, index=True)
    browse.add_argument("--category", choices=("all", *BLOCK_FILTERS), default="all")
    browse.add_argument("--phase", choices=("all", *PHASES), default="all", help="只看某个会话阶段的段落")
    browse.add_argument("--query", default="", help="搜索段落内任意行；词语/末词前缀")
    browse.add_argument("--addin-prefix", default="", help="按Add-in名称或Class前缀筛选，如ATFTranslationServer")
    browse.add_argument("--no-fold", dest="fold", action="store_false", help="不折叠内容相同的段落")
    browse.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    browse.add_argument("--print", dest="print_only", action="store_true", help="输出一页段落摘要，不进入交互界面")
    browse.add_argument("--format", choices=("text", "json"), default="text", help="JSON始终以非交互方式输出")
    browse.add_argument("--page-size", type=int, default=100, help="每页1到200个完整段落")
    browse.add_argument("--after", type=int, default=0, help="从此起始行之后的段落继续分页")
    browse.set_defaults(rules=None)
    summary = commands.add_parser("summary", help="会话摘要：怎么结束的、最后在做什么、许可/插件/对话框/侧车日志")
    _workspace_options(summary, index=True)
    summary.add_argument("--format", choices=("text", "json"), default="text")
    summary.add_argument("--tail", type=int, default=10, help="列出结束前最后N个操作与对话框，1到200")
    summary.set_defaults(rules=None)
    view = commands.add_parser("view", help="启动原有本地Web查看器")
    _workspace_options(view, index=True)
    view.add_argument("--port", type=int, default=8765)
    view.set_defaults(rules=None)
    report = commands.add_parser("report", help="生成可贴进支持工单的Markdown报告（SHA-256、编码、会话摘要、规则候选、侧车、声明）")
    _workspace_options(report, index=True)
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")
    report.add_argument("-o", "--output", type=Path, help="输出UTF-8报告；省略则写到标准输出")
    report.add_argument("--min-severity", choices=SEVERITY_RANK, default="warning")
    report.add_argument("--max-findings", type=int, default=30, help="每个文件最多列出多少条规则候选（相同行只算一条）")
    report.add_argument("--context", type=int, default=2, help="候选前后各附几行原文，0到20")
    report.add_argument("--tail", type=int, default=10, help="列出结束前最后N个操作与对话框，1到200")
    report.add_argument("--include-personal-info", action="store_true",
                        help="保留Windows用户名、域名和计算机名；默认替换为<user>、<domain>、<machine>")
    report.add_argument("--rules", type=Path, help="追加自定义JSON规则")
    cache = commands.add_parser("cache", help="显示索引缓存位置与大小；--clear删除本工具的索引数据库")
    cache.add_argument("--clear", action="store_true", help="删除缓存目录中本工具的索引数据库（不动其他文件）")
    cache.set_defaults(rules=None)
    return parser


def _error(path: Path, message: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "source": str(path.absolute()), "status": "error",
        "scan_complete": False, "error": message,
    }


def _discover(paths: list[Path], recursive: bool) -> tuple[list[Path], list[dict[str, Any]], list[Path]]:
    """Text inputs, input errors, and binary .dmp sidecars found in directories."""
    found: set[Path] = set()
    dumps: set[Path] = set()
    errors: list[dict[str, Any]] = []
    for path in paths:
        try:
            if path.is_file():
                found.add(path.resolve())
            elif path.is_dir():
                iterator = path.rglob("*") if recursive else path.iterdir()
                files = [item for item in iterator if item.is_file()]
                members = {item.resolve() for item in files if _is_text_input(item)}
                if members:
                    found.update(members)
                    dumps.update(item.resolve() for item in files if item.suffix.lower() == ".dmp")
                else:
                    errors.append(_error(path, "目录内没有.txt、.log、.journal或.txt.abbrev文件；子目录需--recursive"))
            else:
                errors.append(_error(path, "路径不存在或不是普通文件/目录"))
        except OSError as error:
            errors.append(_error(path, str(error)))
    return sorted(found), errors, sorted(dumps - found)


def _workspace(args, years: list[int], attach: bool) -> tuple[list[InputFile], list[dict[str, Any]], list[Path]]:
    """Plan the inputs: explicit paths, or every local Revit Journals folder.
    Returns (planned files, input errors, every input path to protect)."""
    paths = list(args.paths)
    if not paths:
        folders = journal_folders(years)
        if not folders:
            where = "、".join(map(str, years)) if years else "任何版本"
            raise ValueError(f"没有找到{where}的Revit Journals目录（%LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit <year>\\Journals）；请传入文件或目录")
        paths = [folder for _, folder in folders]
    files, errors, dumps = _discover(paths, args.recursive)
    if years and args.paths:
        files = [path for path in files if revit_year(path) in years]
        dumps = [path for path in dumps if revit_year(path) in years]
    attached = attach_steel_logs(files) if attach else {}
    planned = plan_inputs([*files, *dumps], attached)
    if args.latest:
        keep = []
        for item in planned:  # planned is newest session first
            if item.session.key not in keep:
                keep.append(item.session.key)
        keep = set(keep[:args.latest])
        planned = [item for item in planned if item.session.key in keep]
    return planned, errors, [*files, *dumps, *attached, *args.paths]


def _duplicate(item: InputFile) -> dict[str, Any]:
    return {
        "schema_version": 1, "source": str(item.path), "status": "duplicate",
        "scan_complete": False, "duplicate_of": str(item.duplicate_of), "sha256": item.sha256,
        "byte_count": item.size, "session": item.session.label,
    }


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


def _cache(clear: bool) -> int:
    folder = cache_dir()
    found = cache_files(folder)
    size = sum(path.stat().st_size for path in found)
    print(f"缓存目录：{folder}")
    print(f"索引数据库：{len(found)}个文件，共{size / 1024 / 1024:.1f}MB（含完整Journal文本）")
    if clear:
        for path in found:
            path.unlink()
        print(f"已删除{len(found)}个文件；目录中的其他文件未改动。")
    elif found:
        print("删除：revit-journal cache --clear；也可设置REVIT_JOURNAL_CACHE或用--database改变位置。")
    return 0


def _report(args, years: list[int], rules) -> int:
    from .ticket import Redactor, collect, to_json, to_markdown
    from .viewer_index import ViewerIndex

    if not 0 <= args.context <= 20 or not 1 <= args.tail <= 200 or args.max_findings < 1:
        raise ValueError("--context需要0到20，--tail需要1到200，--max-findings需要大于0")
    if not args.paths and not args.latest:
        args.latest = 1  # Without paths: the newest session on this machine.
    planned, errors, inputs = _workspace(args, years, attach=True)
    if errors:
        raise ValueError("；".join(item["error"] for item in errors))
    protected = [*inputs, *([args.rules] if args.rules is not None else [])]
    for target in (args.database, Path(str(args.database) + "-wal"), Path(str(args.database) + "-shm"), args.output):
        _protect_inputs(target, protected)
    index = ViewerIndex(args.database, workers=args.jobs, snapshot=args.snapshot)
    index.build(index.register(planned, args.encoding), args.encoding)
    # Keep enough candidates to pick one per repeat group after UI prompts are set aside.
    options = ScanOptions(encoding=args.encoding, context=args.context, min_severity=args.min_severity,
                          max_findings=max(500, args.max_findings * 20))
    data = collect(index, rules, options, args.snapshot, args.tail, args.max_findings)
    render = to_json if args.format == "json" else to_markdown
    text = render(data, generated=datetime.now(), redact=None if args.include_personal_info else Redactor.current())
    with _destination(args.output) as stream:
        stream.write(text)
    if args.output is not None:
        print(f"报告已写入：{args.output.resolve()}", file=sys.stderr)
    return 2 if data["failed"] else 0


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
        if args.command == "cache":
            return _cache(args.clear)
        resolve_encoding(args.encoding)  # Reject unknown codecs before any work.
        years = parse_years(args.revit)
        if getattr(args, "jobs", None) is not None and args.jobs < 1:
            raise ValueError("--jobs必须大于0")
        if getattr(args, "database", "") is None:
            args.database = cache_dir() / ("viewer.sqlite3" if args.command == "view" else "terminal.sqlite3")
        if args.command == "report":
            return _report(args, years, rules)
        if args.command in {"view", "browse", "summary"}:
            planned, errors, inputs = _workspace(args, years, attach=True)
            if errors:
                raise ValueError("；".join(item["error"] for item in errors))
            for target in (args.database, Path(str(args.database) + "-wal"), Path(str(args.database) + "-shm")):
                _protect_inputs(target, inputs)
            if args.command in {"browse", "summary"}:
                from .terminal import run

                if args.command == "summary":
                    if not 1 <= args.tail <= 200:
                        raise ValueError("--tail需要在1到200之间")
                    return run(planned, args.database, args.encoding, format_name=args.format, print_only=True,
                               jobs=args.jobs, snapshot=args.snapshot, summary_tail=args.tail)
                return run(planned, args.database, args.encoding, category=args.category, query=args.query,
                           addin_prefix=args.addin_prefix,
                           color=args.color, print_only=args.print_only, format_name=args.format,
                           after=args.after, page_size=args.page_size, jobs=args.jobs, snapshot=args.snapshot,
                           phase=args.phase, fold=args.fold)
            from .viewer_server import serve

            serve(planned, args.database, args.encoding, args.port, jobs=args.jobs, snapshot=args.snapshot)
            return 0
        options = ScanOptions(
            encoding=args.encoding, context=args.context, min_severity=args.min_severity,
            max_findings=args.max_findings, max_groups=args.max_groups,
            max_line_chars=args.max_line_chars,
        )
        planned, input_errors, inputs = _workspace(args, years, attach=False)
        explicit = {path.resolve() for path in args.paths}
        # Directory .dmp files are session sidecars, not scan inputs; an explicit one is reported.
        planned = [item for item in planned if item.status != "binary" or item.path in explicit]
        # Also protect the custom rule document, including symlinks and hard links.
        protected = list(inputs)
        if args.rules is not None:
            protected.append(args.rules)
        _protect_inputs(args.output, protected)
        exit_code = 2 if input_errors else 0

        def reports() -> Iterator[dict[str, Any]]:
            nonlocal exit_code
            yield from input_errors
            for item in planned:
                path = item.path
                if item.status == "duplicate":
                    # Byte-identical copy (e.g. journal.NNNN.txt.copy.txt): report once.
                    yield _duplicate(item)
                    continue
                try:
                    report = analyze(path, options=options, rules=rules, snapshot=args.snapshot)
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
    except (OSError, ValueError, LookupError, sqlite3.Error) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
