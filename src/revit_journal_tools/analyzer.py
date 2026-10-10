"""One-pass analysis with bounded evidence retention and complete hit counts."""

import hashlib
import heapq
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import Finding, FindingGroup, ScanReport, SEVERITY_RANK, SourceLine
from .reader import JournalReader
from .rules import Rule, load_rules, match_rules, ruleset_digest


@dataclass(frozen=True)
class ScanOptions:
    encoding: str = "auto"
    context: int = 3
    min_severity: str = "warning"
    max_findings: int = 500
    max_groups: int = 500
    max_excerpt_chars: int = 4096
    max_line_chars: int = 1_048_576

    def __post_init__(self) -> None:
        if not 0 <= self.context <= 100:
            raise ValueError("context必须在0到100之间")
        if self.min_severity not in SEVERITY_RANK:
            raise ValueError("min_severity必须为warning、error或fatal")
        for name in ("max_findings", "max_groups", "max_excerpt_chars", "max_line_chars"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name}必须大于0")


def decoding_warnings(decoding: dict) -> list[str]:
    """Report-visible notes for fallback and degraded decoding; never silent."""
    warnings = []
    count = decoding["decode_degraded_line_count"]
    if count:
        lines = "、".join(str(number) for number in decoding["decode_degraded_lines"])
        more = "等" if count > len(decoding["decode_degraded_lines"]) else ""
        warnings.append(
            f"退化解码：所有候选编码都无法完整解码，按{decoding['encoding']}读取；"
            f"{count}行含无法解码的字节（第{lines}行{more}），这些字节以\\xNN原样显示，"
            "该行的识别结果可能不完整。可用--encoding指定正确编码后重试。"
        )
    elif decoding["encoding_fallback"]:
        warnings.append(
            f"编码回退：未按首选编码解码，按{decoding['encoding']}严格解码成功（请求：{decoding['encoding_requested']}）。"
        )
    return warnings


def analyze(
    path: str | Path, *, options: ScanOptions | None = None,
    rules: tuple[Rule, ...] | None = None,
) -> ScanReport:
    from . import __version__

    options = options or ScanOptions()
    rules = load_rules() if rules is None else rules
    if not rules or len({rule.id for rule in rules}) != len(rules):
        raise ValueError("规则不能为空，且id必须唯一")
    active_rules = tuple(
        rule for rule in rules
        if SEVERITY_RANK[rule.severity] >= SEVERITY_RANK[options.min_severity]
    )
    before: deque[SourceLine] = deque(maxlen=options.context)
    pending: deque[Finding] = deque()
    retained: list[tuple[int, int, Finding]] = []
    groups: dict[str, FindingGroup] = {}
    by_severity: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    matched_count = 0
    ungrouped_count = 0

    with JournalReader(path, options.encoding, options.max_line_chars) as reader:
        for line in reader:
            preview = SourceLine(
                line.number, line.text[:options.max_excerpt_chars], len(line.text),
                len(line.text) > options.max_excerpt_chars, line.degraded,
            )
            for finding in pending:
                finding.context_after.append(preview)
            while pending and len(pending[0].context_after) >= options.context:
                pending.popleft()
            matches = match_rules(active_rules, line.text)
            if matches:
                matched_count += 1
                severity = max((match.severity for match in matches), key=SEVERITY_RANK.__getitem__)
                by_severity[severity] += 1
                by_rule.update(match.rule_id for match in matches)
                # Exact decoded line + rule ids only. Different IDs/paths remain distinct.
                key = "\0".join([*(match.rule_id for match in matches), line.text])
                fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
                if fingerprint in groups:
                    groups[fingerprint].count += 1
                    groups[fingerprint].last_line = line.number
                elif len(groups) < options.max_groups:
                    groups[fingerprint] = FindingGroup(
                        fingerprint, severity, [match.rule_id for match in matches],
                        line.number, line.number,
                    )
                else:
                    ungrouped_count += 1
                priority = (SEVERITY_RANK[severity], -line.number)
                if len(retained) < options.max_findings or priority > retained[0][:2]:
                    finding = Finding(
                        line.number, severity, preview, matches, fingerprint,
                        context_before=list(before),
                    )
                    item = (*priority, finding)
                    if len(retained) < options.max_findings:
                        heapq.heappush(retained, item)
                    else:
                        evicted = heapq.heapreplace(retained, item)[2]
                        pending = deque(value for value in pending if value is not evicted)
                    if options.context:
                        pending.append(finding)
            before.append(preview)

        findings = sorted((item[2] for item in retained), key=lambda item: item.line_number)
        omitted = matched_count - len(findings)
        warnings: list[str] = []
        if omitted:
            warnings.append(
                f"全文已扫描；按严重级别优先、同级按出现顺序保留{len(findings)}条候选明细，省略{omitted}条。"
                "总计数完整；需要更多同级明细时请提高--max-findings或使用--min-severity筛选。"
            )
        if ungrouped_count:
            warnings.append(f"分组容量已满，{ungrouped_count}条命中未计入分组；总命中计数仍完整。")
        decoding = reader.decoding_summary()
        warnings.extend(decoding_warnings(decoding))
        return ScanReport(
            schema_version=1, tool_version=__version__, source=str(reader.path),
            encoding=reader.encoding, sha256=reader.sha256, byte_count=reader.byte_count,
            line_count=reader.line_count, ruleset_sha256=ruleset_digest(rules),
            options=asdict(options), matched_line_count=matched_count,
            counts_by_severity={key: by_severity[key] for key in SEVERITY_RANK},
            counts_by_rule=dict(sorted(by_rule.items())), findings=findings,
            groups=list(groups.values()), omitted_finding_count=omitted,
            ungrouped_match_count=ungrouped_count, warnings=warnings,
            encoding_requested=decoding["encoding_requested"],
            encoding_fallback=decoding["encoding_fallback"],
            decode_degraded_line_count=decoding["decode_degraded_line_count"],
            decode_degraded_lines=decoding["decode_degraded_lines"],
        )
