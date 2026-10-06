"""Reproducible synthetic workload. Run with PYTHONPATH=src; no real logs used."""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def peak_rss_mib():
    try:
        import resource
    except ImportError:
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024), 2)


def worker(path):
    from revit_journal_tools import analyze

    started = time.perf_counter()
    report = analyze(path)
    result = {
        "synthetic": True, "bytes": report.byte_count, "lines": report.line_count,
        "matched_lines": report.matched_line_count,
        "fatal_lines": report.counts_by_severity["fatal"],
        "scan_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": peak_rss_mib(), "sha256": report.sha256,
    }
    print(json.dumps(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mib", type=int, nargs="+", default=[8, 64])
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker is not None:
        worker(args.worker)
        return
    if any(size < 1 for size in args.mib):
        parser.error("--mib values must be positive")
    for size in args.mib:
        with tempfile.TemporaryDirectory(prefix="journal-benchmark-") as temporary:
            path = Path(temporary) / "synthetic.journal.txt"
            normal = "' 0:< DBG_INFO: Synthetic heartbeat {number:09d}; " + "x" * 80 + "\n"
            count = size * 1024 * 1024 // len(normal.format(number=0).encode())
            expected_matches = 1
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                for number in range(1, count):
                    if number % 10_000 == 0:
                        stream.write(f"' 0:< DBG_ERROR: Synthetic issue {number}\n")
                        expected_matches += 1
                    else:
                        stream.write(normal.format(number=number))
                stream.write("' 0:< fatal error at synthetic EOF\n")
            process = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker", str(path)],
                capture_output=True, text=True, encoding="utf-8", check=True,
            )
            result = json.loads(process.stdout)
            if result["lines"] != count or result["matched_lines"] != expected_matches or result["fatal_lines"] != 1:
                raise RuntimeError(f"Synthetic workload validation failed: {result}")
            result["requested_mib"] = size
            result["expected_matches"] = expected_matches
            result["verified"] = True
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
