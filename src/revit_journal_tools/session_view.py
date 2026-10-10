"""Plain-text rendering of sessions and their summaries (TUI panel, --print, summary).

Callers pass every line through terminal.safe_text before it reaches a terminal.
"""

from collections import Counter
from datetime import datetime
from pathlib import Path

from .blocks import PHASES
from .categories import CATEGORIES
from .sessions import ROLE_LABELS, STATUS_LABELS

SIDECAR_LABELS = {"worker": "Worker日志", "steel_log": "钢连接错误日志", "cer_log": "CER错误报告日志",
                  "dump": "崩溃转储", "duplicate": "字节相同的副本", "empty": "空文件"}


def duration_text(seconds) -> str:
    if seconds is None:
        return "时长未知"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分{secs}秒" if minutes else f"{secs}秒"


def _when(value: str | None) -> str:
    return value.replace("T", " ")[:19] if value else "时间未知"


def file_date(item: dict) -> str:
    return datetime.fromtimestamp(item["mtime_ns"] / 1e9).strftime("%Y-%m-%d %H:%M") if item.get("mtime_ns") else ""


def state_text(session: dict) -> str:
    if session.get("writing"):
        return "正在写入"
    if session.get("end_state") == "live":
        return "实时快照（会话未结束）"
    return session.get("end_state_label") or ""


def sidecar_label(item: dict) -> str:
    text = f"· {item['name']} [{ROLE_LABELS[item['role']]}] {STATUS_LABELS[item['status']]}"
    if item["status"] == "binary":
        text += f"，{item['size'] / 1024 / 1024:.1f}MB"
    if item.get("duplicate_of"):
        text += f"（与{Path(item['duplicate_of']).name}相同）"
    return text


def session_label(session: dict, files: dict) -> str:
    roles = Counter(files[file_id]["role"] for file_id in session["files"] if file_id in files)
    roles.update(item["role"] for item in session["sidecars"] if item["status"] != "binary")
    parts = [f"{ROLE_LABELS[role]}×{count}" for role, count in roles.items()]
    if session["dump_count"]:
        parts.append(f"dmp×{session['dump_count']}（{session['dump_bytes'] / 1024 / 1024:.1f}MB）")
    head = f"会话 {session['label']}" if session["recognized"] else f"未识别命名 {session['label']}"
    summary = session.get("summary") or {}
    facts = [session["directory"]]
    if summary.get("start_time"):
        facts.append(_when(summary["start_time"])[:16])
        facts.append(duration_text(summary.get("duration_seconds")))
    state = state_text(session)
    if state:
        facts.append(state)
    return f"{head} | " + " | ".join(facts) + " | " + "、".join(parts)


def summary_lines(session: dict, tail: list[dict] | None = None) -> list[str]:
    """Readable summary of one session; every fact keeps its line number."""
    summary = session.get("summary") or {}
    lines = [f"会话 {session['label']} | {session['directory']} | {state_text(session) or '状态未知'}"]
    if session.get("main"):
        lines.append(f"来源：{session['main']}")
    if not summary:
        lines.append("（主Journal尚未完成索引或读取失败，暂无摘要）")
        return lines
    build = " / ".join(part for part in (summary.get("build"), summary.get("branch"), summary.get("release")) if part)
    lines.append(f"版本：{build or '未记录'}" + (f" | 进程号{summary['pid']}" if summary.get("pid") else ""))
    end = f"结束标记第{summary['finished_line']}行" if summary.get("finished_line") else "无结束标记"
    lines.append(f"时间：{_when(summary.get('start_time'))} → {_when(summary.get('end_time'))}"
                 f"（{duration_text(summary.get('duration_seconds'))}）| {end}")
    if summary.get("phases"):
        lines.append("阶段：" + " → ".join(f"{item['label']}L{item['first_line']}" for item in summary["phases"]))
    license = summary.get("license") or {}
    errors = license.get("errors") or []
    text = f"许可：{license.get('events', 0)}条许可记录"
    if errors:
        text += f"；许可错误{len(errors)}条（第{'、'.join(str(item['line_number']) for item in errors[:5])}行）"
    lines.append(text)
    documents = summary.get("documents") or []
    if documents:
        lines.append(f"打开过的文档：{summary.get('document_count', len(documents))}个")
        lines.extend(f"  第{item['first_line']}行 {item['path']}" + (f"（{item['count']}次）" if item["count"] > 1 else "")
                     for item in documents[:8])
    else:
        lines.append("打开过的文档：未见openFromModelPath记录")
    for item in (summary.get("open_failures") or [])[:5]:
        lines.append(f"  打开失败 第{item['line_number']}行 {item['text']}")
    addins = summary.get("addins") or {}
    lines.append(f"Add-in：加载记录{addins.get('loading_records', 0)}条（Autodesk {addins.get('autodesk', 0)} / "
                 f"第三方{addins.get('third_party', 0)} / 待定{addins.get('unknown', 0)}）；"
                 f"第三方Add-in {addins.get('third_party_count', 0)}个")
    names = addins.get("third_party_names") or []
    if names:
        lines.append("  第三方：" + "、".join(item["name"] for item in names[:12]) + ("等" if len(names) > 12 else ""))
    conflicts = addins.get("conflicts") or []
    if conflicts:
        lines.append(f"  程序集冲突：{len(conflicts)}个程序集共{addins.get('conflict_lines', 0)}条（按程序集聚合）")
        lines.extend(f"    {item['assembly']} ×{item['count']}（首见第{item['first_line']}行）" for item in conflicts[:8])
    dialogs = summary.get("dialogs") or {}
    lines.append(f"对话框：{dialogs.get('count', 0)}个")
    lines.extend(f"  第{item['line_number']}行 {item['text']}" for item in (dialogs.get("last") or [])[-5:])
    if tail:
        lines.append(f"结束前最后{len(tail)}个操作与对话框：")
        lines.extend(f"  L{block['start_line']}–{block['end_line']} [{CATEGORIES[block['category']]}] {block['title']}"
                     for block in tail)
    last = summary.get("last_lines") or []
    if last:
        lines.append(f"最后{min(len(last), 8)}行：")
        lines.extend(f"  {item['line_number']:>7} | {item['text']}" for item in last[-8:])
    sidecars = session.get("sidecar_logs") or []
    if sidecars:
        lines.append("侧车文件：")
        for item in sidecars:
            label = SIDECAR_LABELS.get(item["kind"], item["kind"])
            text = f"  · {label} {item['path']}"
            if item["kind"] == "worker":
                text += f"（{item.get('line_count', 0)}行）"
            elif item["kind"] in ("dump", "cer_log"):
                text += f"（{item['size'] / 1024 / 1024:.1f}MB）"
            if item.get("basis"):
                text += f"；关联依据：{item['basis']}"
            lines.append(text)
            for entry in (item.get("entries") or [])[:10]:
                lines.append(f"      [{entry['index']}][{entry['time']}] {entry['text']}")
    findings = session.get("session_findings") or []
    lines.append(f"会话级发现（候选，需结合原文复核）：{len(findings)}项")
    for finding in findings:
        lines.append(f"  [{finding['severity']}] {finding['id']} {finding['title']}：{finding['explanation']}")
        for evidence in finding["evidence"][:3]:
            where = f"第{evidence['line_number']}行" if evidence.get("line_number") else Path(evidence["source"]).name
            lines.append(f"      {where} {evidence['text']}")
    return lines


def phase_label(phase: str) -> str:
    return "全部阶段" if phase == "all" else PHASES[phase]
