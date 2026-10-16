# Revit Journal Tools

English | [中文](README.md)

A local Revit Journal viewer: browse large journals in a CLI terminal by whole paragraphs and events, with colours for startup, add-in, exit, error, API error and operation categories, folding/unfolding, search and jumps to original line numbers. The lines and stack trace of one error stay together in one block.

Add-in loading can be filtered by Autodesk, third-party and undetermined origin. The origin comes from the `Vendor` field of the same event and the lines that follow it, and the line numbers of that evidence are kept. Failed loads stay visible in the usual error categories.

Press `i` (or `2`) for the add-in category of the current journal: every add-in record (load, service, server, manifest, assembly and so on) is shown as `name [kind] [origin]` with an error mark and its line number. Name and origin are taken only from the record's own text (`Name(...)`, `VendorId(...)`, `AddInName` and so on), and a server registration is joined with its `[Jrn.RegisterExternalServer]` record into one paragraph. Name and class are extracted from the load event prefix; `v` filters by a name or class prefix such as `ATFTranslationServer` (add-in records only, including failed loads). The source text is read page by page only after Enter unfolds a record; Enter or Esc folds it again.

The CLI terminal viewer and the reusable parsing modules are the current focus. A desktop window (`gui`, embedded WebView2) and a browser viewer (`view`) share one page. Python 3.10 or later and an SQLite with FTS5 are required; the core has no third-party runtime dependencies, and the desktop window needs the optional pywebview (`[desktop]`). All journal data is processed on the local machine. Real cases can be dropped into `cases/inbox/` to calibrate the rules step by step.

## Quick start

Windows (no encoding option needed, even on a Chinese system):

```powershell
# Option 1: a single file, Python 3.10+ only; build once in the repository and distribute dist\revit-journal.pyz
py -3 scripts\build_zipapp.py
py dist\revit-journal.pyz summary          # summary of the latest session of every local Revit version
py dist\revit-journal.pyz browse           # terminal viewer on the latest session
py dist\revit-journal.pyz report -o journal-report.md

# Option 2: install into a virtual environment to get the revit-journal command
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\revit-journal.exe browse --revit 2024
```

Without a path the tool looks in `%LOCALAPPDATA%\Autodesk\Revit\Autodesk Revit <year>\Journals`; a file or folder can also be given directly, for example `browse "$env:LOCALAPPDATA\Autodesk\Revit\Autodesk Revit 2024\Journals\journal.0011.txt"`. The encoding defaults to `auto` and is decided per file (cp936 files from Chinese Windows are read as gb18030), so `--encoding` is rarely needed. Windows Terminal is recommended for the interactive interface; the Windows 10 console (conhost) has been tested and works, and an old console without VT control sequences prints a one-page summary instead (the same as `--print`).

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
revit-journal browse examples/paragraphs.journal.txt
```

The package also runs without installation: `PYTHONPATH=src python3 -m revit_journal_tools browse examples/paragraphs.journal.txt` (Windows: `$env:PYTHONPATH='src'; py -3 -m revit_journal_tools browse ...`).

## CLI terminal viewer

```bash
# No path: find the Journals folders of every local Revit version and open the latest session
revit-journal browse
revit-journal browse --revit 2024 --latest 3

# Session summary: clean exit or not, phases, licensing, opened documents, add-ins, dialogs, sidecar logs, last actions
revit-journal summary --latest
revit-journal summary --revit 2024 --format json

revit-journal browse cases/inbox --recursive

# Go straight to the API error category
revit-journal browse journal.0001.txt --category api_error

# Third-party add-in loads, including failed loads
revit-journal browse cases/inbox --recursive --category addin_third_party

# Add-in category and name/class prefix filter
revit-journal browse journal.0001.txt --category addin
revit-journal browse journal.0001.txt --addin-prefix ATFTranslationServer

# Journals from systems in other languages (e.g. cp1252 on a German system): auto decides per file and shows why; override only when it is wrong
revit-journal browse cases/inbox --recursive
revit-journal browse journal.0076.txt --encoding cp1252

# Hand-written example with official, third-party, missing-origin and failed loads
revit-journal browse examples/addin-loading.journal.txt

# Non-interactive summary and reusable paragraph metadata
revit-journal browse examples/paragraphs.journal.txt --print --color always
revit-journal browse examples/paragraphs.journal.txt --format json --page-size 20

# Keyword search across all files: hit lines listed per session, sharing the index cache with browse
revit-journal search "Command Jrn" --latest
revit-journal search ExceptionCode cases/inbox --recursive --format json --per-file 10
```

Up/Down select a paragraph and Enter unfolds or folds it; when unfolded, Up/Down or PgUp/PgDn scroll the source text and Left/Right scroll long lines sideways. `0` shows everything, `1` to `9` switch category (startup, add-in, exit, error, API error, operation, warning, API activity, dialog), `c` cycles through them (including "other"), `/` searches the current file, `?` searches all files, `h`/`H` jump to the next/previous hit line inside a paragraph, `g` jumps to a source line, `f` picks a file, `n`/`p` turn pages and `q` quits. `+`/`-` change the number of preview lines per paragraph; use the terminal's own zoom for font size, and the layout reflows after a resize.

Keyword search (`/`, `?`, `browse --query` and the `search` command) shares one syntax: whitespace-separated words must all appear on the same line, in any order; a `"quoted"` part is an adjacent phrase; the last unquoted word also matches as a prefix (`DBG_ERR` finds `DBG_ERROR`). Matching is based on FTS5 tokens, not substrings or regular expressions; a query of punctuation only (such as `}`) gives an error instead of an empty result. "N hit lines" in the status line counts lines, not paragraphs. With a search active, Enter on a paragraph scrolls straight to the first hit line; hit lines are shown inverted with `*` after the line number, and the current hit with `>`. `?` searches every indexed file: results list each file with its hit count and first 3 hit lines, Enter opens that file at that line and Esc returns. The `search` command groups output by session, one line `path | N hit lines` per file followed by `  line: text`, with a total at the end; `--category` restricts to one line category, `--per-file` sets how many lines are listed per file (1–20), `--format json` gives structured output, and the exit code is 2 when a file failed to index. In PowerShell 5.1, double quotes inside a query are written as `'\"open command\" Ribbon'`.

`a` filters Autodesk origin, `t` third-party origin and `u` undetermined origin (all kinds of add-in records); `c` cycles through every category. Autodesk origin is based on `ADSK` or `autodesk.com` and its subdomains in the vendor field; other explicit vendors are third-party, and missing, conflicting or incomplete information stays undetermined. The name/class prefix only filters and never replaces the vendor-based origin. An origin label does not prove that the component ships with Revit or that it loaded successfully. The word Autodesk in a DLL name or install path alone is not vendor evidence. After an update, opening an old cache rebuilds the index of the selected files automatically.

Ordinary paragraphs, system configuration, `Jrn.*` operations with their `_` continuation lines, API brace sections and one exception with its stack trace are each shown as a block. A new independent error starts a new block. Every block keeps its start and end lines, category and the reason for the split; unfolding never changes the source file. Unclosed API sections and operation continuations are marked explicitly. Blocks are a reading aid, not a confirmed root cause.

The file list opened with `f` is organised by Revit session: `journal.NNNN.txt`, `journal.NNNN.workerN.log`, `journal.NNNN.txt.copy.txt` and `journal.NNNN.NNNN.dmp` in the same folder belong to one session. Byte-identical copies are indexed once and noted under the session, `.dmp` files are only counted with their size, and 0-byte files (such as `dump.0001.txt`) are listed but not indexed; files outside this naming scheme form a separate "unrecognised names" group and are still read normally. `--print` and `--format json` use the same session grouping, with a `sessions` section in the JSON.

The index is built in the background on first start, and files that are already done can be opened meanwhile. The index cache lives in the user cache folder (Windows: `%LOCALAPPDATA%\revit-journal-tools\cache\terminal.sqlite3`; Linux: `revit-journal-tools` under `$XDG_CACHE_HOME` or `~/.cache`), never in the current folder; `revit-journal cache` shows its location and size, `revit-journal cache --clear` deletes this tool's index databases, and the `REVIT_JOURNAL_CACHE` environment variable or `--database` moves it. By default the index stores only the byte position of each line in the source journal (`--store offsets`) and reads the text from the source when viewing, so the source file must stay in place and unchanged; size, modification time and file identity are checked before reading, and on a mismatch the tool asks to reopen the file and rebuild instead of showing misaligned text. `--store text` stores the full text in the index (measured at 1.7–2.3 times the offsets store) so the source can be moved afterwards. Journals still being written (read as a snapshot) and files whose lines cannot be located by byte offset (such as UTF-16) always store the full text. Paragraphs of any size load their text page by page. A search hit on a continuation line returns the whole paragraph without listing the same block twice; the "blocks" count differs from the "hit lines" count of the plain scan.

`s` shows the summary of the current session, `e` the last operations and dialogs before the end, `P` filters by phase (startup, licensing, add-in loading, document opening, interaction, exit), `9` shows dialogs and `z` toggles "fold identical paragraphs as ×N". A journal Revit is still writing is read as a snapshot of complete lines by default (`--snapshot`) and marked as such in the interface.

See [docs/terminal.md](docs/terminal.md) for the block rules, keys and limits in detail.

## Desktop window

```powershell
# One-time install: this package with the optional pywebview (Windows uses the system WebView2 runtime)
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"

# Open the window: without a path it opens the Journals folders of every local Revit version; a file or folder also works
.\.venv\Scripts\revit-journal-gui.exe
.\.venv\Scripts\revit-journal.exe gui cases\inbox --recursive
```

The window is titled `revit-journal-tool`; window, taskbar and page use the `web/icon.ico` and `web/icon.png` icons (taken from ImagePusher's `selfpush.ico`), and the window appears in the taskbar under its own AppUserModelID (`revit-journal-tools.gui`) instead of being grouped with Python. It embeds WebView2 and shows the same page as `view`, plus three buttons: "Open files…", "Open folder…" and "Local Journals" (Ctrl+O opens files). Newly opened files replace the current workspace: the running index build is stopped first, then a new one starts. The splitters between the workspace and the content, and between the paragraph list and the source text, can be dragged (arrow keys for fine steps, double-click to reset); widths and theme (light/dark/system) are remembered. The page reads only from `127.0.0.1`, and source files are read-only.

`revit-journal-gui.exe` has no console window; errors before start (such as an invalid encoding option) are shown in a message box. Without pywebview, `gui` prints the install command, and `view` can be used in a browser instead. The index cache is `desktop.sqlite3` in the cache folder; the window's layout and theme live in the `webview` subfolder of the cache folder (WebView2 user data, about 10MB), which `cache --clear` leaves alone.

## Web viewer

```bash
# View the first batch of uploaded English/German cases (cp1252); the disk index is built in the background on first run
revit-journal view cases/inbox --recursive --encoding cp1252

# Default address http://127.0.0.1:8765; change the port when it is taken
revit-journal view --revit 2024 --port 8766
```

Open the local address printed by the command in a browser. Files are switched on the left and categories at the top; paragraphs are listed by default (the same blocks, phase filter and duplicate folding as `browse`), or the view can be switched to single lines. Keyword search shares the CLI syntax (all words on one line, quotes for adjacent phrases, prefix on the last word); the "current file / all files" toggle next to the search box sets its scope. In the current file, the bottom of the list shows "N hit lines" and clicking a paragraph result shows it from the first hit line on the right; all files searches every ready file (`/api/search`), showing the hit count and first 3 hit lines per file, and a click switches to that file at that line with the query kept. Every occurrence of every search word is highlighted in the list, in paragraph text and in context. Lists show at most 100 items per page; click a paragraph for its full text (long paragraphs load 200 lines at a time), click a line for its context, or jump to any valid line number. "Session summary" shows the same content as the `summary` command, with clickable line numbers that jump to the main journal. Categories are startup, add-in, exit, error, API error, operation, warning, API activity, dialog and other: lines before the first user operation that match no other category are startup, lines after the exit marker are exit, and the rest are other. The page itself never scrolls (category chips wrap tightly in narrow windows, and the file overview collapses to one line in short windows); clicking a column header filters by category, origin or add-in record kind, the latter matching `browse --kind`.

The first index build is done file by file, and ready files can be browsed meanwhile. The index cache is `viewer.sqlite3` in the cache folder and is reused for unchanged files on the next start. Storage works like `browse` (`--store offsets` by default, see above), without the CLI's 500-detail limit, and stays out of Git by default. Indexes are rebuilt when file content, encoding or indexing rules change.

The page switches between 中文 and English (session summary and error messages included; the command line and ticket report stay Chinese) and between light and dark themes; the workspace, paragraph list and source text columns can be resized by dragging. The left side groups by Revit version (collapsible), sorts sessions by time within a version (newest or oldest first) and lists each session's files. Add-ins take one category; in paragraph view it can be narrowed to Autodesk/third-party/undetermined origin (failed loads included). The first paragraph is shown automatically after opening a file; Up/Down step through the list, Ctrl+F searches and Ctrl+G jumps to a line; the source area can copy the text or `path:line`. The web interface's paragraphs, folding, phases and session summary call the same index queries and summary rendering as the CLI, with no second implementation; interactive fold/unfold and add-in name prefix filtering remain CLI-only. See [docs/viewer.md](docs/viewer.md) for the web viewer.

## Support ticket report

```powershell
# Latest local session (--latest 1 by default without a path); writes UTF-8 Markdown
revit-journal report -o reports\journal-report.md
# A journal or folder; JSON format; keep personal information (replaced by default)
revit-journal report "$env:LOCALAPPDATA\Autodesk\Revit\Autodesk Revit 2024\Journals" --revit 2024 --latest 3
revit-journal report journal.0011.txt --format json --include-personal-info
```

The report is organised by session so it can be pasted straight into an Autodesk support ticket, in the same structure as a RevitLocalDbFix report (key points at the top, two-column fact table, source code blocks): SHA-256, actual encoding, line and byte counts of every file; the session summary (end state, phases, licensing, documents, add-ins, dialogs, last operations, last lines, sidecar logs and the evidence linking them); rule candidates by severity with source line numbers, context, rule ID and evidence, each line listed once, at most 3 examples per rule combination with the remaining count, and Revit UI prompts only counted; and a disclaimer at the end. Windows user name, domain and machine name are replaced with `<user>`, `<domain>` and `<machine>` by default (report text only; SHA-256 is still that of the original file). Exit codes: `0` success, `2` a file could not be read (listed explicitly in the report) or an argument error.

## Scan and export

```bash
# One file, keeping 5 lines of context around each hit
revit-journal scan /path/to/journal.0001.txt --context 5

# Several files or folders; folders select .txt, .log, .journal and .txt.abbrev
revit-journal scan cases/inbox --recursive --format json -o reports/cases.json

# auto decides the encoding per file by default; give one explicitly to read strictly
revit-journal scan journal.0001.txt --encoding gb18030

# Mixed folders: candidate encodings tried in order
revit-journal scan cases/inbox --recursive --encoding utf-8,gb18030,cp1252

# Keep only error and fatal candidates, with a larger detail capacity
revit-journal scan journal.0001.txt --min-severity error --max-findings 2000

# Extend the built-in rules with custom ones; JSONL writes one report per file
revit-journal scan cases/inbox --recursive --rules examples/custom-rules.json --format jsonl

# For scripts: exit code 1 when error or fatal is reached
revit-journal scan journal.0001.txt --fail-on error

# Show rule definitions, severities and sources
revit-journal rules
```

`python -m revit_journal_tools` is equivalent to `revit-journal`. Output files are replaced atomically as UTF-8 and can never overwrite an input journal or custom rules file. A file that fails to read in a batch is recorded as `status: error` on its own while the other files continue. A file byte-identical to another input (such as `journal.NNNN.txt.copy.txt`) is scanned once; the copy gets `status: duplicate` with `duplicate_of` and its SHA-256, and `.dmp` files in folders are not scanned.

Exit codes: `0` the scan succeeded; `1` it succeeded and reached the explicitly requested `--fail-on` severity; `2` an argument, rule, input or output error; `130` interrupted. Error candidates alone never cause a non-zero exit code.

## Report contents

- Source: absolute path, SHA-256 of the raw bytes, actual decoding, requested encoding, whether a fallback was used, degraded line count and numbers, file size and line count.
- Location: 1-based text line and character column, the matched fragment and its context. CRLF, LF and lone CR all count as line breaks; files with mixed line endings may count differently from tools that only count LF.
- Evidence: rule ID, candidate severity, explanation, source links and the `heuristic`/`documented_marker`/`case_verified` tags.
- Statistics: hit lines per severity, hits per rule, duplicate groups of identical lines.
- Limits: whether the scan completed, details kept, details omitted, hits not grouped.

A line matching several rules produces one candidate whose severity is the highest among the enabled rules. The sum of per-rule counts can exceed the number of hit lines. A rule's severity is a review priority, not the real severity of an incident.

`scan_complete: true` only means the input was fully decoded and scanned, not that every error was recognised. No hits means the current rules found no candidate. All hits remain `candidate`; there is no automatic root-cause diagnosis.

## Large files and encodings

Files are read line by line and never loaded whole into memory. By default at most 500 candidate details and 500 duplicate groups are kept; beyond that the scan still covers the whole file and counts candidates. Details are kept by `fatal > error > warning`, earlier candidates first within a severity, and shown in line order; a severe error that appears late can thus replace a stored warning. The report states how many were omitted; use `--min-severity` to cut warnings or raise `--max-findings`. Grouping merges only identical decoded lines with identical rule sets, never element IDs, paths or times fuzzily.

Each shown line keeps at most 4096 characters and is marked `text_truncated` beyond that. Matching looks at the whole line, and the fragments and columns near the match are kept. The default single-line limit is 1,048,576 characters; beyond it an explicit error is returned, adjustable with `--max-line-chars`, and the rest of the line is never dropped silently.

`--encoding auto` (the default) decides per file: a BOM wins; otherwise strict UTF-8 first; then the UI language declared in the journal header (`Actual language code=DEU` and so on: German, French, Italian, Spanish, Portuguese and Dutch give cp1252, Polish, Czech and Hungarian cp1250, Russian cp1251, Simplified Chinese gb18030, Traditional Chinese cp950, Japanese cp932, Korean cp949; English ENU/ENG says nothing about the system code page); then gb18030, a superset of the system ANSI code page and cp1252 in turn. gb18030 "decodes" almost any bytes, and a cp1252 file from a Western system would come out as plausible-looking Chinese (`Außenanlagen` becomes `Au遝nanlagen`), so it is accepted only when at least half of the decoded non-ASCII characters are common Chinese characters (GB2312): 95 real Chinese journals on this machine score 0.83–1.0, uploaded cp1252 journals at most 0.003. cp1252 likewise decodes almost anything: content that looks Chinese is not switched to cp1252 because of a few damaged bytes but decoded as degraded gb18030 and marked. Files in one batch can each use a different encoding; reports, `browse --format json` and the index record each file's actual `encoding`, the reason `encoding_basis` (such as `language_code:DEU`, `chinese_text`), the requested `encoding_requested` and `encoding_fallback`; only a file that gb18030 decodes but that does not look Chinese is marked "encoding doubtful".

When no candidate decodes the whole file, `auto` no longer fails the file: it reads with the last candidate, shows undecodable bytes as `\xNN`, marks the line `decode_degraded`, reports `decode_degraded_line_count` with the first 20 line numbers at file level and notes this in the report. Matches on degraded lines may be incomplete; retry with the correct encoding. Bytes are never replaced or dropped silently, and SHA-256 is still computed on the raw bytes.

A single explicit encoding (such as `--encoding cp1252`) is strict: any undecodable byte fails that file, as in earlier versions. A comma-separated candidate list such as `--encoding utf-8,gb18030,cp1252` is tried in order per file (gb18030 still has to pass the Chinese-text check) and falls back to degraded decoding with the last candidate. A candidate list ignores the journal's declared UI language. Files that look binary (containing NUL), have overlong lines or change during the scan are still read failures. Prefer a copy of a journal that is no longer being written.

The first batch of uploaded English/German cases decodes strictly as cp1252; their non-breaking spaces, degree signs and German umlauts are not valid UTF-8. Use `--encoding cp1252` explicitly for that batch; this says nothing about the encoding of files uploaded later.

Measured on this machine (Chinese Windows, cp936): all 113 real files work without `--encoding`, 96 of them strictly decoded by the gb18030 fallback and 8 as UTF-8, with no degraded lines; 7 `.dmp` files are only counted, and one byte-identical `.copy.txt` and one empty `dump.0001.txt` are not indexed twice.

## Python API

```python
from revit_journal_tools import ScanOptions, analyze, load_rules

report = analyze(
    "examples/synthetic.journal.txt",
    options=ScanOptions(context=3, min_severity="error"),
    rules=load_rules(),
)
for finding in report.findings:
    print(finding.line_number, finding.severity, finding.line.text)
result = report.to_dict()
```

API calls raise on failure; the CLI isolates per-file errors in batch jobs.

## Project layout

```text
src/revit_journal_tools/
  reader.py              per-file encoding decision, strict/degraded decoding, streaming reads, hashes and source change checks
  sessions.py            group main journal, worker logs, copies and dmp files into sessions by name; copies deduplicated by bytes
  rules.py               rule loading, validation and matching
  regex_safety.py        catastrophic-backtracking (nested quantifier) check when rule regexes load
  data/builtin_rules.json built-in rules
  analyzer.py            context, counts, bounded details and groups
  models.py              structured report
  reporting.py           text, JSON and JSONL export
  ticket.py              support ticket report (Markdown/JSON) and personal information replacement
  cli.py                 batch scan entry point
  discovery.py           local Revit Journals folders and the index cache folder
  live.py                snapshot reads of a journal still being written
  summary.py             session summary, end state and session-level candidates
  session_view.py        text rendering of the session summary
  sidecars.py            linking steel connection error logs and CER logs to sessions
  categories.py          startup, add-in, exit, error, API and operation line categories and session phase marks
  blocks.py              streaming paragraph boundaries, multi-line errors and operation grouping
  loading.py             add-in record kinds, names, vendor evidence and origin filters
  terminal.py            CLI colours, folding, file switching and source paging
  viewer_index.py        full disk index, full-text search (query parsing, hit lines, all-file search), paging and context queries
  viewer_server.py       viewer server listening on the local machine only
  desktop.py             desktop window: pywebview with embedded WebView2, opening files/folders into the workspace
  i18n.py                English texts of the viewer page (?lang=en) and error message translation; Chinese by default
  web/                   local interface shared by view and gui, no external front-end dependencies
cases/                   case notes, registration template and inbox
examples/                clearly labelled hand-written examples
tests/                   reading, matching, evidence and command-line regression tests
docs/                    architecture, rule extension and reference sources
reports/                 local output folder
```

## Development and next steps

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v

# Generate 8MiB and 64MiB journals to verify full scans, hits at the end and memory use
PYTHONPATH=src python3 scripts/benchmark_streaming.py --mib 8 64
```

On Windows, real local journals can be used for read-only benchmarks and equivalence checks (results are only printed; nothing is stored and no journal is copied):

```powershell
$env:PYTHONPATH='src'
py -3 scripts/benchmark_real.py all --revit 2024          # scan, categories and blocks, single-file index, whole-folder browse
# After parser/rule changes: line-by-line comparison with the source tree before the change (old tree extracted outside the repository with git archive)
py -3 scripts/verify_equivalence.py gates
py -3 scripts/verify_equivalence.py blocks --old-src <old src folder>
py -3 scripts/verify_equivalence.py blocks --together --old-src <old src folder>
# Compare the offsets and text stores page by page over all lines, paragraphs and queries
py -3 scripts/verify_equivalence.py stores --encoding auto
# Acceptance of the interactive interface in a native console (a window pops up for a few seconds): conhost or Windows Terminal, key by key, with restore on exit
py -3 scripts/windows_console_probe.py <journal.txt> [--host wt] [--ctrl-c]
```

Measured on this machine (Windows 10, Python 3.12, 2026-10-10): a real 6.47MB/85,387-line journal scans in about 0.4s (about 17MB/s) and indexes in about 2.5s (about 34,000 lines/s, index 2.2 times the source; `--store text` about 2.4s and 3.6 times); `browse --print` over the 22 files (about 11MB) of a Revit 2024 folder takes about 2.8s.

After installing the package, Windows can run the same tests with `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`. The desktop window tests use a stand-in for pywebview, open no window and run without pywebview installed.

Single-file distribution: `py -3 scripts/build_zipapp.py` builds `dist/revit-journal.pyz` (about 90KB: this package's code, built-in rules and web files); the target machine needs only Python 3.10+, and its `gui` command additionally needs pywebview in that Python (`py -m pip install pywebview`). Not implemented in this version: browser drag-and-drop import, an installer bundling a Python runtime, AI analysis, journal replay or model repair.

See [cases/README.md](cases/README.md) for submitting cases and [docs/rules.md](docs/rules.md) for extending rules. Autodesk's [guide to reading journal files](https://www.autodesk.com/support/technical/article/How-to-read-the-journal-file-Revit/) recommends relating entries to the specific operation, the observed behaviour and the approximate time; this project keeps context for that reason, and other references are in [docs/sources.md](docs/sources.md).
