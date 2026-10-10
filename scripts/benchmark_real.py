"""Benchmark on the local Revit journals of this machine (read-only).

Journals are read from %LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit <year>\\Journals.
They are never copied, modified or stored. Results are printed as JSON lines;
temporary index databases live in the system temp directory and are deleted.
Do not commit the output: it describes local files.

Run from the repository root with PYTHONPATH=src, for example:
  py -3 scripts/benchmark_real.py list
  py -3 scripts/benchmark_real.py scan --encoding gb18030
  py -3 scripts/benchmark_real.py index --file <path>
  py -3 scripts/benchmark_real.py parse
  py -3 scripts/benchmark_real.py dir --revit 2024
  py -3 scripts/benchmark_real.py all

Each measurement runs in a fresh subprocess so peak memory is per run.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
from benchmark_streaming import peak_rss_mib  # noqa: E402

TEXT_SUFFIXES = {".txt", ".log", ".journal"}
# The product prints this (Chinese) marker for a file whose index failed.
INDEX_FAILED = "".join(map(chr, (0x7D22, 0x5F15, 0x5931, 0x8D25)))


def journal_dirs():
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Autodesk" / "Revit"
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        journals = child / "Journals"
        if child.name.startswith("Autodesk Revit ") and journals.is_dir():
            found.append(journals)
    return found


def text_files(directory):
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES)


def default_file():
    candidates = [p for d in journal_dirs() for p in text_files(d)]
    if not candidates:
        raise SystemExit("No local Revit journals were found under %LOCALAPPDATA%.")
    return max(candidates, key=lambda p: p.stat().st_size)


def resolve_dir(args):
    if args.dir:
        return Path(args.dir)
    for directory in journal_dirs():
        if directory.parent.name.endswith(str(args.revit)):
            return directory
    raise SystemExit(f"No Journals directory for Revit {args.revit}.")


def label(path):
    """Version folder plus file name; enough to identify the input locally."""
    path = Path(path)
    return f"{path.parent.parent.name}/{path.name}" if path.parent.name == "Journals" else path.name


def db_size(database):
    return sum(Path(str(database) + suffix).stat().st_size
               for suffix in ("", "-wal", "-shm") if Path(str(database) + suffix).exists())


# ---------------------------------------------------------------- workers

def work_scan(path, encoding, profile):
    from revit_journal_tools import ScanOptions, analyze

    started = time.perf_counter()
    report = analyze(path, options=ScanOptions(encoding=encoding))
    elapsed = time.perf_counter() - started
    return {
        "lines": report.line_count, "bytes": report.byte_count, "encoding": report.encoding,
        "matched_lines": report.matched_line_count, "seconds": round(elapsed, 3),
        "mb_per_s": round(report.byte_count / 1e6 / elapsed, 2),
        "extra": {key: getattr(report, key) for key in ("encoding_fallback", "decode_degraded_line_count")
                  if hasattr(report, key)},
    }


def work_parse(path, encoding, profile):
    """Read only, read+classify, read+classify+blocks: no database."""
    from revit_journal_tools.blocks import BlockBuilder
    from revit_journal_tools.categories import Categorizer
    from revit_journal_tools.reader import JournalReader
    from revit_journal_tools.rules import load_rules

    rules = load_rules()
    result = {}
    started = time.perf_counter()
    with JournalReader(path, encoding) as reader:
        for _ in reader:
            pass
    result["read_seconds"] = round(time.perf_counter() - started, 3)
    started = time.perf_counter()
    categorizer = Categorizer(rules)
    with JournalReader(path, encoding) as reader:
        for line in reader:
            categorizer.classify(line.text)
    result["read_classify_seconds"] = round(time.perf_counter() - started, 3)
    started = time.perf_counter()
    categorizer, builder, blocks = Categorizer(rules), BlockBuilder(), 0
    with JournalReader(path, encoding) as reader:
        for line in reader:
            blocks += len(builder.feed(line, categorizer.classify(line.text)))
        blocks += len(builder.finish())
        lines = reader.line_count
    result["read_classify_blocks_seconds"] = round(time.perf_counter() - started, 3)
    result["feed_classify_seconds"] = round(result["read_classify_blocks_seconds"] - result["read_seconds"], 3)
    result.update(lines=lines, blocks=blocks)
    return result


def work_index(path, encoding, profile):
    from revit_journal_tools.viewer_index import ViewerIndex

    with tempfile.TemporaryDirectory(prefix="rjt-bench-") as temporary:
        database = Path(temporary) / "bench.sqlite3"
        started = time.perf_counter()
        index = ViewerIndex(database)
        queued = index.register([Path(path)], encoding)
        index.build(queued, encoding)
        elapsed = time.perf_counter() - started
        catalog = index.catalog()
        item = catalog[0]
        size = db_size(database)
        del index
    source = Path(path).stat().st_size
    return {
        "status": item["status"], "error": item.get("error"), "encoding": item.get("encoding"),
        "lines": item["line_count"], "blocks": item.get("block_count"), "seconds": round(elapsed, 3),
        "lines_per_s": round(item["line_count"] / elapsed), "mb_per_s": round(source / 1e6 / elapsed, 2),
        "index_bytes": size, "index_multiple": round(size / max(source, 1), 2),
    }


WORKERS = {"scan": work_scan, "parse": work_parse, "index": work_index}


def run_worker(kind, path, encoding, profile):
    if profile:
        import cProfile
        import io
        import pstats

        profiler = cProfile.Profile()
        result = profiler.runcall(WORKERS[kind], path, encoding, profile)
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(25)
        print(stream.getvalue(), file=sys.stderr)
    else:
        result = WORKERS[kind](path, encoding, profile)
    result["peak_rss_mib"] = peak_rss_mib()
    print(json.dumps(result))


def spawn(kind, path, encoding, profile=False):
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", kind, str(path), encoding]
    if profile:
        command.append("--profile")
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                             env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    if process.returncode:
        raise SystemExit(f"worker failed:\n{process.stderr}")
    if profile:
        print(process.stderr, file=sys.stderr)
    return json.loads(process.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- commands

def summarize(kind, name, runs, extra):
    keys = [k for k, v in runs[0].items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    best = min(runs, key=lambda r: r.get("seconds", r.get("read_classify_blocks_seconds", 0)))
    record = {"benchmark": kind, "input": name, "runs": len(runs), **extra, **best}
    if len(runs) > 1:
        record["median"] = {k: round(statistics.median(r[k] for r in runs), 3) for k in keys if "seconds" in k}
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def bench_file(kind, args):
    path = Path(args.file) if args.file else default_file()
    runs = [spawn(kind, path, args.encoding, args.profile and i == 0) for i in range(args.repeat)]
    return summarize(kind, label(path), runs, {"encoding_requested": args.encoding,
                                               "source_bytes": path.stat().st_size})


def bench_dir(args):
    directory = resolve_dir(args)
    files = text_files(directory)
    total = sum(p.stat().st_size for p in files)
    runs = []
    for _ in range(args.repeat):
        with tempfile.TemporaryDirectory(prefix="rjt-bench-dir-") as temporary:
            database = Path(temporary) / "terminal.sqlite3"
            command = [sys.executable, "-m", "revit_journal_tools", "browse", str(directory),
                       "--print", "--color", "never", "--database", str(database)]
            if args.encoding != "auto":
                command += ["--encoding", args.encoding]
            started = time.perf_counter()
            process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                     env={**os.environ, "PYTHONPATH": str(REPO / "src")})
            elapsed = time.perf_counter() - started
            size = db_size(database)
        failed = [line for line in process.stdout.splitlines() if INDEX_FAILED in line]
        runs.append({"seconds": round(elapsed, 3), "exit_code": process.returncode,
                     "failed_files": len(failed), "index_bytes": size,
                     "index_multiple": round(size / max(total, 1), 2),
                     "mb_per_s": round(total / 1e6 / elapsed, 2)})
    return summarize("dir", directory.parent.name, runs,
                     {"encoding_requested": args.encoding, "text_files": len(files), "source_bytes": total})


def cmd_list(args):
    for directory in journal_dirs():
        files = list(directory.iterdir())
        texts = text_files(directory)
        print(json.dumps({
            "dir": directory.parent.name, "files": len(files), "text_files": len(texts),
            "text_bytes": sum(p.stat().st_size for p in texts),
            "largest": max(texts, key=lambda p: p.stat().st_size).name if texts else None,
        }))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", nargs=3, metavar=("KIND", "PATH", "ENCODING"), help=argparse.SUPPRESS)
    parser.add_argument("--profile", action="store_true", help="print cProfile top 25 for the first run")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="list local journal directories")
    for name in ("scan", "parse", "index"):
        item = sub.add_parser(name)
        item.add_argument("--file", help="journal path; default is the largest local text journal")
        item.add_argument("--encoding", default="auto")
        item.add_argument("--repeat", type=int, default=3)
        item.add_argument("--profile", action="store_true")
    for name in ("dir", "all"):
        item = sub.add_parser(name)
        item.add_argument("--revit", default="2024")
        item.add_argument("--dir")
        item.add_argument("--file")
        item.add_argument("--encoding", default="auto")
        item.add_argument("--repeat", type=int, default=1 if name == "dir" else 3)
        item.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    if args.worker:
        kind, path, encoding = args.worker
        run_worker(kind, path, encoding, args.profile)
        return
    if args.command == "list":
        cmd_list(args)
    elif args.command in ("scan", "parse", "index"):
        bench_file(args.command, args)
    elif args.command == "dir":
        bench_dir(args)
    elif args.command == "all":
        for kind in ("scan", "parse", "index"):
            bench_file(kind, args)
        args.repeat = 1
        bench_dir(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
