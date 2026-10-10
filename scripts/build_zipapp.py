"""Build a single-file Windows/Linux distribution: dist/revit-journal.pyz (J21).

The archive holds only src/revit_journal_tools (code, built-in rules and the
Web viewer files) and needs nothing but Python 3.10 or newer:

  py -3 scripts/build_zipapp.py
  py dist\\revit-journal.pyz summary

No dependencies are downloaded; the build is reproducible from the source tree.
"""

import argparse
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "src" / "revit_journal_tools"
# Guarded like the package's own __main__: Windows "spawn" workers of the index
# build import this module and must not start the CLI again.
MAIN = """import multiprocessing

from revit_journal_tools.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
"""


def build(output: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="rjt-zipapp-") as staging:
        root = Path(staging)
        shutil.copytree(PACKAGE, root / PACKAGE.name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "__main__.py").write_text(MAIN, encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(root, output, interpreter="/usr/bin/env python3", compressed=True)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", type=Path, default=REPO / "dist" / "revit-journal.pyz")
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        print("需要Python 3.10或更新版本", file=sys.stderr)
        return 2
    path = build(args.output.resolve())
    print(f"{path} ({path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
