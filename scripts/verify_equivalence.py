"""Check that a code change keeps results identical on the local Revit journals.

Read-only: journals under %LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit <year>\\Journals
are read in place. Dumps go to a temporary directory and are deleted. Only counts,
file labels, line numbers and field names are printed - never journal text.

Modes:
  scan    compare analyze() results (no retention cap) of --old-src and --new-src
  blocks  compare the index tables (lines: category/severity/rules/body hash;
          blocks: range, category, severity, rules, title, boundary, complete,
          add-in fields; phase, fingerprint and session summary when both
          trees write them) built by --old-src and --new-src
  gates   in --new-src only: gated rule matching vs the same rules without gates

Run from the repository root. Extract the old tree outside the repository, e.g.
  git archive 4ac90a4 src | tar -x -C %TEMP%\\rjt-old
  py -3 scripts/verify_equivalence.py blocks --old-src %TEMP%\\rjt-old\\src
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".txt", ".log", ".journal"}
BLOCK_COLUMNS = ("start_line", "end_line", "category", "severity", "rules", "title", "title_line",
                 "boundary", "complete", "addin_origin", "addin_evidence", "addin_name", "addin_class",
                 "addin_identity_truncated")
# Written by later index versions; compared only when both trees write them.
OPTIONAL_BLOCK_COLUMNS = ("phase", "fingerprint")
OPTIONAL_KEYS = {"file": ("summary",), "block": OPTIONAL_BLOCK_COLUMNS}


def local_journals():
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Autodesk" / "Revit"
    files = []
    for journals in sorted(root.glob("Autodesk Revit */Journals")):
        files.extend(sorted(p for p in journals.iterdir() if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES))
    return files


def label(path):
    path = Path(path)
    return f"{path.parent.parent.name}/{path.name}" if path.parent.name == "Journals" else path.name


def digest(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


# ------------------------------------------------------------------ dumps

def dump_scan(paths, encoding, out):
    from revit_journal_tools import ScanOptions, analyze

    options = ScanOptions(encoding=encoding, max_findings=10**9, max_groups=10**9)
    with open(out, "w", encoding="utf-8") as stream:
        for path in paths:
            try:
                report = analyze(path, options=options).to_dict()
            except (OSError, ValueError, LookupError) as error:
                record = {"file": label(path), "error": type(error).__name__}
            else:
                record = {"file": label(path)}
                for key in ("line_count", "sha256", "matched_line_count", "counts_by_rule",
                            "counts_by_severity", "groups", "omitted_finding_count"):
                    record[key] = report[key]
                record["findings"] = [
                    [f["line_number"], f["severity"], f["fingerprint"],
                     [[m["rule_id"], m["column"], digest(m["matched_text"]), m["match_length"],
                       m["excerpt_start_column"], digest(m["excerpt"])] for m in f["matches"]],
                     [line["line_number"] for line in f["context_before"] + f["context_after"]]]
                    for f in report["findings"]
                ]
            stream.write(json.dumps(record, sort_keys=True) + "\n")


def dump_blocks(paths, encoding, out, together=False):
    from revit_journal_tools.viewer_index import ViewerIndex

    seconds = 0.0
    with tempfile.TemporaryDirectory(prefix="rjt-verify-") as temporary, open(out, "w", encoding="utf-8") as stream:
        database = Path(temporary) / "verify.sqlite3"
        index = ViewerIndex(database)
        if together:
            # One workspace: newer trees build it with worker processes.
            started = time.perf_counter()
            index.build(index.register([Path(p) for p in paths], encoding), encoding)
            seconds = time.perf_counter() - started
        for path in map(Path, paths):
            if not together:
                started = time.perf_counter()
                queued = index.register([path], encoding)
                index.build(queued, encoding)
                seconds += time.perf_counter() - started
            connection = sqlite3.connect(database)
            try:
                row = connection.execute("SELECT id,status,line_count,sha256 FROM files WHERE path=?",
                                         (str(Path(path).resolve()),)).fetchone()
                if row is None or row[1] != "ready" or row[0] not in index.file_ids:
                    stream.write(json.dumps({"file": label(path), "kind": "file", "status": row and row[1]}) + "\n")
                    continue
                file_id = row[0]
                counts = connection.execute("SELECT counts FROM files WHERE id=?", (file_id,)).fetchone()[0]
                summary = {}
                if "summary" in {column[1] for column in connection.execute("PRAGMA table_info(files)")}:
                    summary["summary"] = digest(connection.execute(
                        "SELECT summary FROM files WHERE id=?", (file_id,)).fetchone()[0])
                block_columns = {column[1] for column in connection.execute("PRAGMA table_info(blocks)")}
                columns = BLOCK_COLUMNS + tuple(name for name in OPTIONAL_BLOCK_COLUMNS if name in block_columns)
                stats = connection.execute("SELECT block_count,counts FROM block_stats WHERE file_id=?",
                                           (file_id,)).fetchone()
                stream.write(json.dumps({"file": label(path), "kind": "file", "status": row[1],
                                         "line_count": row[2], "sha256": row[3], "counts": json.loads(counts),
                                         "block_count": stats and stats[0],
                                         "block_counts": stats and json.loads(stats[1]), **summary},
                                        sort_keys=True) + "\n")
                for number, body, category, severity, rules in connection.execute(
                        "SELECT line_number,body,category,severity,rules FROM lines WHERE file_id=? "
                        "ORDER BY line_number", (file_id,)):
                    stream.write(json.dumps({"file": label(path), "kind": "line", "line": number,
                                             "body": digest(body), "category": category,
                                             "severity": severity, "rules": rules}) + "\n")
                for values in connection.execute(
                        "SELECT " + ",".join(columns) + " FROM blocks WHERE file_id=? ORDER BY start_line",
                        (file_id,)):
                    record = dict(zip(columns, values))
                    record["title"] = digest(record["title"])
                    stream.write(json.dumps({"file": label(path), "kind": "block", **record}, sort_keys=True) + "\n")
            finally:
                connection.close()
    print(json.dumps({"build_seconds": round(seconds, 2)}), file=sys.stderr)


def run_gates(paths, encoding):
    from revit_journal_tools.reader import JournalReader
    from revit_journal_tools.rules import fold, load_rules, match_rules

    rules = load_rules()
    lines = hits = mismatches = gated_calls = 0
    reported = []
    for path in paths:
        with JournalReader(path, encoding) as reader:
            for line in reader:
                lines += 1
                ungated = [m for rule in rules if (m := rule.match_pattern(line.text))]
                gated = match_rules(rules, line.text)
                per_rule = [m for rule in rules if (m := rule.match(line.text))]
                folded = fold(line.text)
                gated_calls += sum(rule.passes_gate(folded) for rule in rules)
                hits += len(ungated)
                if gated != ungated or per_rule != ungated:
                    mismatches += 1
                    if len(reported) < 20:
                        reported.append([label(path), line.number])
    print(json.dumps({"files": len(paths), "lines": lines, "rule_hits": hits, "mismatched_lines": mismatches,
                      "first_mismatches": reported, "regex_calls_ungated": lines * len(rules),
                      "regex_calls_gated": gated_calls}))
    return 1 if mismatches else 0


# ------------------------------------------------------------------ compare

def spawn_dump(mode, src, paths, encoding, out, together=False):
    command = [sys.executable, str(Path(__file__).resolve()), mode, "--dump", mode, "--out", str(out),
               "--encoding", encoding, *(["--together"] if together else []), *map(str, paths)]
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                             env={**os.environ, "PYTHONPATH": str(Path(src).resolve())})
    if process.returncode:
        raise SystemExit(f"dump failed for {src}:\n{process.stderr}")
    return process.stderr.strip()


def compare(old_file, new_file):
    rows = mismatches = 0
    first = []
    counts = {}
    with open(old_file, encoding="utf-8") as old, open(new_file, encoding="utf-8") as new:
        for old_line, new_line in zip(old, new):
            rows += 1
            a, b = json.loads(old_line), json.loads(new_line)
            for key in OPTIONAL_KEYS.get(a.get("kind"), ()):
                if (key in a) != (key in b):
                    a.pop(key, None)
                    b.pop(key, None)
            counts[a.get("kind", "file")] = counts.get(a.get("kind", "file"), 0) + 1
            if a != b:
                mismatches += 1
                if len(first) < 20:
                    fields = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
                    where = a.get("line") or a.get("start_line") or ""
                    first.append([a.get("file"), a.get("kind", "file"), where, fields])
        extra_old, extra_new = sum(1 for _ in old), sum(1 for _ in new)
    return {"rows": rows, "rows_by_kind": counts, "mismatched_rows": mismatches,
            "extra_rows_old": extra_old, "extra_rows_new": extra_new, "first_mismatches": first}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", choices=("scan", "blocks", "gates"))
    parser.add_argument("--old-src", help="source tree before the change (outside the repository)")
    parser.add_argument("--new-src", default=str(REPO / "src"))
    parser.add_argument("--encoding", default="gb18030", help="gb18030 works for every local file and old trees")
    parser.add_argument("--files", nargs="*", help="default: every local text journal")
    parser.add_argument("--together", action="store_true",
                        help="blocks: register all files in one workspace (exercises parallel builds)")
    parser.add_argument("--dump", choices=("scan", "blocks"), help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    parser.add_argument("paths", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.dump:
        if args.dump == "scan":
            dump_scan(args.paths, args.encoding, args.out)
        else:
            dump_blocks(args.paths, args.encoding, args.out, args.together)
        return 0
    paths = [Path(p) for p in args.files] if args.files else local_journals()
    if not paths:
        raise SystemExit("No local journals found.")
    if args.mode == "gates":
        sys.path.insert(0, str(Path(args.new_src).resolve()))
        return run_gates(paths, args.encoding)
    if not args.old_src or not args.mode:
        parser.error("scan/blocks need --old-src")
    with tempfile.TemporaryDirectory(prefix="rjt-compare-") as temporary:
        old_out, new_out = Path(temporary) / "old.jsonl", Path(temporary) / "new.jsonl"
        old_note = spawn_dump(args.mode, args.old_src, paths, args.encoding, old_out, args.together)
        new_note = spawn_dump(args.mode, args.new_src, paths, args.encoding, new_out, args.together)
        result = compare(old_out, new_out)
    result.update(mode=args.mode, files=len(paths), encoding=args.encoding, together=args.together,
                  old=old_note, new=new_note)
    print(json.dumps(result))
    return 1 if result["mismatched_rows"] or result["extra_rows_old"] or result["extra_rows_new"] else 0


if __name__ == "__main__":
    sys.exit(main())
