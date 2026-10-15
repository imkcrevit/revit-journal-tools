"""English texts of the viewer page (`view`, `gui`, ?lang=en). Chinese stays
the default everywhere; the CLI and the support report are Chinese only.

Each table is keyed like its Chinese original (CATEGORIES, PHASES, ROLE_LABELS,
END_STATES, CRASH_MARKS, EXCEPTION_CODES, ...); `label` falls back to Chinese.
"""

import re

LANGS = ("zh", "en")

EN = {
    "category": {
        "startup": "Startup", "system": "System data", "error": "Error", "api_error": "API error",
        "operation": "Operation", "warning": "Warning", "api": "API activity", "other": "Other", "dialog": "Dialog",
    },
    "phase": {
        "startup": "Startup", "licensing": "Licensing", "addin_loading": "Add-in loading",
        "document_open": "Opening documents", "interactive": "Interactive", "shutdown": "Shutdown",
    },
    "role": {
        "main": "Main journal", "abbrev": "Abbreviated journal", "worker": "Worker log",
        "steel_log": "Steel connection error log", "copy": "Copy", "dump": "Crash dump",
        "unrecognized": "Unrecognized name",
    },
    "status": {"empty": "empty, not indexed", "duplicate": "byte-identical, not indexed again",
               "binary": "binary, counted only"},
    "end_state": {
        "clean": "Ended normally",
        "no_clean_exit": "Did not end normally (no end marker: crash, forced exit, or still being written)",
        "live": "Being written (session not finished)",
    },
    "crash_mark": {
        "exception": "Exception (caught by Revit crash protection)", "exception_code": "Exception code",
        "crash_protection_failed": "Another exception during crash protection",
        "unhandled": "Unhandled exception (the process then ends)", "fatal_dialog": "Fatal error dialog",
        "crash_log": "Crash record (SLOG >Crash)", "cef_failed": "Embedded browser (CEF) failed to initialize",
    },
    "exception_code": {
        "0xe0434352": ".NET managed exception", "0xe06d7363": "C++ exception", "0xc0000005": "Access violation",
        "0x80000003": "Breakpoint / internal assertion (deliberate stop)", "0xc00000fd": "Stack overflow",
        "0xc0000409": "Fail fast / stack buffer overrun", "0xc0000374": "Heap corruption",
        "0xc0000017": "Out of memory",
    },
    "sidecar": {"worker": "Worker log", "steel_log": "Steel connection error log", "cer_log": "CER error report log",
                "dump": "Crash dump", "duplicate": "Byte-identical copy", "empty": "Empty file"},
    "steel_basis": {
        "entry_time": "entry times (dated by the file's modification day) fall within the session",
        "modified": "the file's modification time falls within the session",
        "time_of_day": "entry times of day fall within the session (the log has no dates and is compared with the "
                       "session date; its modification time is outside the session: a weaker link)",
        "": "same Revit version, linked by time",
    },
}

# Messages the page can receive (errors, and search hints such as
# viewer_index.CJK_HINT), Chinese pattern -> English (first match wins).
_ERRORS = [
    (r"源文件已移动、删除或修改", "The source file was moved, deleted or modified, so its text cannot be read at the "
                         "indexed positions; open it again to rebuild the index (--store text keeps the text in the index)"),
    (r"源文件比索引时短", "The source file is shorter than when it was indexed; index it again"),
    (r"源文件自任务创建后发生变化", "The source file changed after indexing started; index it again"),
    (r"文件未在当前工作区中", "The file is not in the current workspace"),
    (r"文件尚未完成索引", "The file is not indexed yet; see its progress or error in the file list"),
    (r"该文件的索引来自旧版本", "The file was indexed by an earlier version; open it again to rebuild the index"),
    (r"未知分类", "Unknown category"),
    (r"未知阶段", "Unknown phase"),
    (r"段落分页参数超出范围", "Paragraph paging parameter out of range"),
    (r"分页或搜索参数超出范围", "Paging or search parameter out of range"),
    (r"搜索词不含可检索的字母或数字", "The search has no letters or digits to look for; punctuation and symbols are "
                           "not indexed as words"),
    (r"请输入搜索词", "Enter a search term"),
    (r"中文、日文等连续文字按整段匹配", "Chinese, Japanese and similar text is matched as one whole run of characters: "
                            "a search finds it only from the start of the run"),
    (r"行号或上下文范围无效", "Invalid line number or context range"),
    (r"行号超过文件范围", "The line number is beyond the end of the file"),
    (r"段落索引不完整", "The paragraph index is incomplete; index the file again"),
    (r"请提供段落起始行", "Give the first line of a paragraph"),
    (r"fold必须为0或1", "fold must be 0 or 1"),
    (r"折叠指纹无效", "Invalid fold fingerprint"),
    (r"资源不存在", "Not found"),
    (r"仅允许本地访问", "Local access only"),
    (r"lang必须为zh或en", "lang must be zh or en"),
    (r"索引数据库不能覆盖输入文件", "The index database cannot be one of the inputs"),
    (r"目录内没有", "The folder has no .txt, .log, .journal or .txt.abbrev files (subfolders are included only "
              "when that option is on)"),
    (r"路径不存在或不是普通文件/目录", "The path does not exist or is not a regular file or folder"),
    (r"没有找到(?P<years>.*?)的Revit Journals目录", "No Revit Journals folder found for {years} "
     "(%LOCALAPPDATA%\\Autodesk\\Revit\\Autodesk Revit <year>\\Journals); open files or folders instead"),
]


def check_lang(lang: str) -> str:
    if lang not in LANGS:
        raise ValueError("lang必须为zh或en")
    return lang


def label(kind: str, key: str, chinese: dict, lang: str) -> str:
    """The text of a key in `lang`; Chinese (or the key) when no English exists."""
    if lang == "en" and key in EN[kind]:
        return EN[kind][key]
    return chinese.get(key, key)


def error_text(message: str, lang: str) -> str:
    """An error message in `lang`; messages without a translation stay as they are."""
    if lang != "en":
        return message
    for pattern, english in _ERRORS:
        match = re.search(pattern, message)
        if match:
            years = (match.groupdict().get("years") or "").replace("、", ", ").replace("任何版本", "any version")
            return english.format(years=years)
    return message
