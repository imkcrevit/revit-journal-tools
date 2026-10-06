"""Write batch reports incrementally, retaining at most one file report."""

import json
from collections.abc import Iterable
from typing import Any, TextIO


def _write_text(report: dict[str, Any], stream: TextIO) -> None:
    stream.write(f"文件：{report['source']}\n")
    if report["status"] == "error":
        stream.write(f"读取失败：{report['error']}\n\n")
        return
    counts = report["counts_by_severity"]
    stream.write(
        f"扫描完成：{report['line_count']}行 / {report['byte_count']}字节 / {report['encoding']}\n"
        f"SHA-256：{report['sha256']}\n"
        f"错误候选：{report['matched_line_count']}行 "
        f"(fatal={counts['fatal']}, error={counts['error']}, warning={counts['warning']})\n"
        f"保留明细：{len(report['findings'])}；省略明细：{report['omitted_finding_count']}\n"
        "判定范围：规则候选，需要结合实际操作、发生时间和上下文复核。\n"
    )
    for warning in report["warnings"]:
        stream.write(f"注意：{warning}\n")
    for finding in report["findings"]:
        titles = "、".join(match["title"] for match in finding["matches"])
        stream.write(f"\n[{finding['severity']}] 第{finding['line_number']}行：{titles}\n")
        for line in [*finding["context_before"], finding["line"], *finding["context_after"]]:
            prefix = ">" if line["line_number"] == finding["line_number"] else " "
            suffix = " … [显示已截断]" if line["text_truncated"] else ""
            stream.write(f"{prefix} {line['line_number']:>7} | {line['text']}{suffix}\n")
        for match in finding["matches"]:
            stream.write(
                f"  规则：{match['rule_id']}；第{match['column']}列；依据：{match['basis']}\n"
                f"  命中片段：{match['excerpt']}\n"
                f"  说明：{match['explanation']}\n"
            )
    stream.write("\n")


def write_reports(reports: Iterable[dict[str, Any]], stream: TextIO, format: str) -> None:
    if format == "json":
        stream.write('{"schema_version": 1, "files": [\n')
        first = True
        for report in reports:
            if not first:
                stream.write(",\n")
            json.dump(report, stream, ensure_ascii=False, indent=2)
            first = False
        stream.write("\n]}\n")
    elif format == "jsonl":
        for report in reports:
            stream.write(json.dumps(report, ensure_ascii=False) + "\n")
    elif format == "text":
        for report in reports:
            _write_text(report, stream)
    else:
        raise ValueError(f"不支持的报告格式：{format}")
