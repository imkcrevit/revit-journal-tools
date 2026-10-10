"""Markdown/JSON report for a support ticket (J17).

Structure follows RevitLocalDbFix's ReportBuilder so both reports can be pasted
into one Autodesk case: a header list, one section per subject, two-column
tables for facts and fenced blocks for raw text. As there, the Windows user,
domain and computer names are replaced by placeholders unless the caller asks
for the full report. SHA-256 values always describe the original file bytes.

Everything here is read-only and offline; findings remain candidates.
"""

import getpass
import json
import os
import platform
import re
import socket
from datetime import datetime
from pathlib import Path

from . import __version__
from .analyzer import ScanOptions, analyze
from .models import SEVERITY_RANK
from .rules import ruleset_digest
from .session_view import duration_text, sidecar_label, state_text, summary_lines
from .sessions import ROLE_LABELS
from .terminal import encoding_label, safe_text

USER, DOMAIN, MACHINE = "<user>", "<domain>", "<machine>"
_REPORT_WORDS = {
    "revit", "autodesk", "steel", "steelconnections", "advancesteel", "sql", "localdb", "microsoft",
    "windows", "user", "users", "system", "program", "files", "data", "local", "appdata", "server",
    "instance", "version", "report", "admin", "administrator", "workgroup", "journal", "journals",
}
_SEP = r"\\{1,2}"  # Markdown has "\", JSON escapes it as "\\".
_TOKEN = r"A-Za-z0-9_\-"
_SCANNED_ROLES = {"main", "abbrev", "worker", "unrecognized"}
DISCLAIMER = [
    "本报告由离线只读分析生成：没有联网，没有启动Revit，没有执行或回放Journal内容。",
    "规则命中与会话级发现都是候选，不是诊断；需要结合实际操作、发生时间和上下文复核。",
    "侧车日志（钢连接错误日志、CER日志）与会话的关联按版本、时间或进程号推断，是候选关联。",
    "SHA-256针对原始文件字节；正在写入的Journal按快照读取，之后写入的内容不在报告中。",
    "个人信息替换只作用于报告文字，替换处的摘录与原文不同；需要原文时按行号查看源文件。",
]


class Redactor:
    """Port of RevitLocalDbFix's ReportRedactor: whole tokens only; a name that is
    also a report word (e.g. a user called "Revit") is replaced only in the
    DOMAIN\\user and \\Users\\name contexts."""

    def __init__(self, user: str | None, domain: str | None, machine: str | None):
        self.user, self.domain, self.machine = (value.strip() if value and value.strip() else None
                                                for value in (user, domain, machine))

    @classmethod
    def current(cls) -> "Redactor":
        user = os.environ.get("USERNAME") or getpass.getuser()
        machine = os.environ.get("COMPUTERNAME") or socket.gethostname()
        return cls(user, os.environ.get("USERDOMAIN"), machine)

    def __call__(self, text: str) -> str:
        if self.user:
            for qualifier, placeholder in ((self.machine, MACHINE), (self.domain, DOMAIN)):
                if qualifier:
                    text = re.sub(rf"(?<![{_TOKEN}]){re.escape(qualifier)}({_SEP}){re.escape(self.user)}(?![{_TOKEN}])",
                                  lambda m, p=placeholder: p + m.group(1) + USER, text, flags=re.I)
            text = re.sub(rf"({_SEP}Users{_SEP}){re.escape(self.user)}(?![{_TOKEN}])",
                          lambda m: m.group(1) + USER, text, flags=re.I)
            text = self._standalone(text, self.user, USER)
        if self.machine:
            text = re.sub(rf"({_SEP}{_SEP}){re.escape(self.machine)}(?![{_TOKEN}])",
                          lambda m: m.group(1) + MACHINE, text, flags=re.I)
            text = self._standalone(text, self.machine, MACHINE)
        if self.domain and (self.machine or "").lower() != self.domain.lower():
            text = self._standalone(text, self.domain, DOMAIN)
        return text

    @staticmethod
    def _standalone(text: str, name: str, placeholder: str) -> str:
        if len(name) < 3 or name.lower() in _REPORT_WORDS:
            return text
        return re.sub(rf"(?<![{_TOKEN}]){re.escape(name)}(?![{_TOKEN}])", placeholder, text, flags=re.I)


def fence(text: str) -> str:
    """A fenced block that Journal text cannot close early."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    marker = "`" * max(3, longest + 1)
    return f"{marker}text\n{text.rstrip()}\n{marker}"


def _cell(value) -> str:
    return safe_text("" if value is None else str(value)).replace("|", "\\|")


def _table(rows: list[tuple[str, object]], head=("项", "值")) -> list[str]:
    return [f"| {head[0]} | {head[1]} |", "|---|---|", *(f"| {_cell(key)} | {_cell(value)} |" for key, value in rows)]


def _when(value: str | None) -> str:
    return value.replace("T", " ")[:19] if value else "未知"


def _ui_only(finding: dict) -> bool:
    return all("ui_warning" in match.get("tags", ()) for match in finding["matches"])


def key_findings(scan: dict, limit: int, per_rules: int = 3) -> tuple[list[dict], int]:
    """Most severe first, one finding per exact-repeat group and at most per_rules
    examples per rule combination (33 different add-in conflicts would otherwise
    fill the list); UI prompts are left out. Each finding carries its repeat
    count and how many more lines its rule combination matched.
    Returns (findings, UI prompt lines)."""
    groups = {group["fingerprint"]: group for group in scan["groups"]}
    seen, chosen, shown = set(), [], {}
    for finding in sorted(scan["findings"], key=lambda item: (-SEVERITY_RANK[item["severity"]], item["line_number"])):
        rules = tuple(match["rule_id"] for match in finding["matches"])
        if _ui_only(finding) or finding["fingerprint"] in seen:
            continue
        seen.add(finding["fingerprint"])
        if len(shown.setdefault(rules, [])) >= per_rules:
            continue
        group = groups.get(finding["fingerprint"])
        item = {**finding, "repeat_count": group["count"] if group else 1,
                "repeat_last_line": group["last_line"] if group else finding["line_number"], "more_lines": 0}
        shown[rules].append(item)
        chosen.append(item)
    chosen = chosen[:limit]
    # Lines of the same rule combination not shown (counted over all retained findings).
    for rules, items in shown.items():
        if items and items[-1] in chosen:
            total = sum(1 for finding in scan["findings"] if tuple(m["rule_id"] for m in finding["matches"]) == rules)
            items[-1]["more_lines"] = max(0, total - sum(item["repeat_count"] for item in items))
    return chosen, scan["counts_by_rule"].get("revit.ui_warning", 0)


def collect(index, rules, options: ScanOptions, snapshot: str, tail: int, max_findings: int) -> dict:
    """Sessions from the index plus a rule scan of each session's text files."""
    catalog = index.catalog()
    by_id = {item["id"]: item for item in catalog}
    sessions, failed = [], False
    for session in index.sessions(catalog):
        files, scans = [], []
        for file_id in session["files"]:
            item = by_id[file_id]
            files.append({"path": item["path"], "role": item["role"], "sha256": item["sha256"],
                          "encoding": encoding_label(item), "line_count": item["line_count"],
                          "size": item["size"], "status": item["status"], "error": item.get("error"),
                          "live": item["live"] or item["writing"]})
            if item["status"] != "ready":
                failed = True
                continue
            if item["role"] not in _SCANNED_ROLES:
                continue
            try:
                report = analyze(item["path"], options=options, rules=rules, snapshot=snapshot).to_dict()
            except (OSError, ValueError, LookupError) as error:
                failed = True
                scans.append({"source": item["path"], "status": "error", "error": str(error)})
                continue
            findings, ui_lines = key_findings(report, max_findings)
            scans.append({**report, "key_findings": findings, "ui_prompt_lines": ui_lines})
        main = next((item for item in catalog if item["path"] == session.get("main")), None)
        last = index.tail_blocks(main["id"], tail) if main and main["status"] == "ready" else []
        sessions.append({**{key: value for key, value in session.items() if key != "files"},
                         "files": files, "scans": scans,
                         "tail": [{key: block[key] for key in ("start_line", "end_line", "category", "phase", "title")}
                                  for block in last]})
    return {"sessions": sessions, "failed": failed, "ruleset_sha256": ruleset_digest(rules)}


def _finding_lines(finding: dict) -> list[str]:
    rules = "、".join(f"`{match['rule_id']}` {match['title']}" for match in finding["matches"])
    head = f"**第{finding['line_number']}行** [{finding['severity']}] {rules}"
    if finding["repeat_count"] > 1:
        head += f"（相同行共{finding['repeat_count']}次，末次第{finding['repeat_last_line']}行）"
    if finding.get("more_lines"):
        head += f"；同一规则组合另有{finding['more_lines']}行未列出"
    body = []
    for line in [*finding["context_before"], finding["line"], *finding["context_after"]]:
        mark = ">" if line["line_number"] == finding["line_number"] else " "
        suffix = " …[已截断]" if line["text_truncated"] else ""
        if line.get("decode_degraded"):
            suffix += " [含无法解码字节]"
        body.append(f"{mark} {line['line_number']:>7} | {safe_text(line['text'])}{suffix}")
    lines = [head, "", fence("\n".join(body))]
    for match in finding["matches"]:
        lines.append(f"- 依据：{safe_text(match['basis'])}；说明：{safe_text(match['explanation'])}")
    return lines


def _session_section(session: dict) -> list[str]:
    summary = session.get("summary") or {}
    state = state_text(session) or "状态未知"
    out = [f"## 会话 {session['label']} — {state}", ""]
    if not session["files"]:
        # Only sidecars (an empty dump.NNNN.txt, a crash dump): nothing was read.
        return out + ["只有未读取的侧车文件，没有可分析的Journal：", "",
                      *(f"- {_cell(sidecar_label(item)[2:])}" for item in session["sidecars"])]
    if summary.get("finished_line"):
        state += f"（结束标记第{summary['finished_line']}行）"
    build = " / ".join(part for part in (summary.get("build"), summary.get("branch"), summary.get("release")) if part)
    out += _table([
        ("目录", session["directory"]), ("主Journal", session.get("main") or "无"),
        ("版本", build or "未记录"), ("进程号", summary.get("pid") or "未记录"),
        ("开始", _when(summary.get("start_time"))), ("结束", _when(summary.get("end_time"))),
        ("时长", duration_text(summary.get("duration_seconds"))), ("结束状态", state),
        ("会话级发现", f"{len(session.get('session_findings') or [])}项（候选）"),
    ])
    out += ["", "### 文件", "", "| 文件 | 角色 | SHA-256 | 编码 | 行数 | 字节 | 状态 |",
            "|---|---|---|---|---|---|---|"]
    for item in session["files"]:
        status = "已读取" + ("（快照）" if item["live"] else "") if item["status"] == "ready" else f"读取失败：{item['error']}"
        out.append("| " + " | ".join(_cell(value) for value in (
            Path(item["path"]).name, ROLE_LABELS.get(item["role"], item["role"]), item["sha256"] or "-",
            item["encoding"], item["line_count"], item["size"], status)) + " |")
    out += ["", "### 会话摘要", "", fence("\n".join(safe_text(text) for text in summary_lines(session, session["tail"])))]
    out += ["", "### 规则候选（按严重级别，相同行只列一次）"]
    if not session["scans"]:
        out += ["", "没有可扫描的Journal文本。"]
    for scan in session["scans"]:
        out += ["", f"#### {Path(scan['source']).name}"]
        if scan.get("status") == "error":
            out += ["", f"扫描失败：{_cell(scan['error'])}"]
            continue
        counts = scan["counts_by_severity"]
        out += ["", f"全文已扫描{scan['line_count']}行；命中{scan['matched_line_count']}行"
                    f"（fatal {counts['fatal']} / error {counts['error']} / warning {counts['warning']}），"
                    f"其中Revit界面提示{scan['ui_prompt_lines']}行不计为错误、不列明细。SHA-256：`{scan['sha256']}`"]
        if scan["counts_by_rule"]:
            out.append("按规则：" + "、".join(f"`{rule}`×{count}" for rule, count in scan["counts_by_rule"].items()))
        for warning in scan["warnings"]:
            out.append(f"注意：{safe_text(warning)}")
        if not scan["key_findings"]:
            out += ["", "没有界面提示以外的规则候选。"]
        for finding in scan["key_findings"]:
            out += ["", *_finding_lines(finding)]
    return out


def to_markdown(data: dict, *, generated: datetime, redact) -> str:
    sessions = data["sessions"]
    unclean = sum(session.get("end_state") == "no_clean_exit" for session in sessions)
    personal = "已替换（<user>、<domain>、<machine>）" if redact else "保留（完整报告）"
    lines = [
        "# Revit Journal Report", "",
        f"- 生成时间：{generated.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 工具版本：revit-journal-tools {__version__} | 规则集SHA-256：`{data['ruleset_sha256']}`",
        f"- 系统：{platform.platform()} | Python {platform.python_version()}",
        f"- 个人信息：{personal}",
        f"- 范围：{len(sessions)}个会话，未正常结束{unclean}个" + ("；有文件读取失败，见各会话“文件”表" if data["failed"] else ""),
        "- 相关工具：LocalDB/钢连接问题可配合RevitLocalDbFix报告一起提交（独立修复工具，不是Revit插件）",
        "",
    ]
    for session in sessions:
        lines += [*_session_section(session), ""]
    lines += ["## 未验证声明", "", *(f"- {text}" for text in DISCLAIMER), ""]
    text = "\n".join(lines)
    return redact(text) if redact else text


def to_json(data: dict, *, generated: datetime, redact) -> str:
    document = {"schema_version": 1, "tool_version": __version__, "generated": generated.isoformat(timespec="seconds"),
                "personal_info": "redacted" if redact else "included", "ruleset_sha256": data["ruleset_sha256"],
                "assessment": "rule_candidates_only", "disclaimer": DISCLAIMER, "sessions": data["sessions"]}
    text = json.dumps(document, ensure_ascii=False, indent=2)
    return redact(text) if redact else text
