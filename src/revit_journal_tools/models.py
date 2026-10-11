"""Small, JSON-serializable evidence objects. Columns and lines are 1-based."""

from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITY_RANK = {"warning": 1, "error": 2, "fatal": 3}


@dataclass(frozen=True)
class SourceLine:
    line_number: int
    text: str
    text_length: int
    text_truncated: bool = False
    # True when some bytes of this line could not be decoded and are shown as \\xNN.
    decode_degraded: bool = False


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    title: str
    severity: str
    basis: str
    explanation: str
    source_url: str | None
    column: int
    matched_text: str
    match_length: int
    match_truncated: bool
    excerpt: str
    excerpt_start_column: int
    # Rule-declared labels, e.g. ui_warning for Revit modelling prompts.
    tags: tuple[str, ...] = ()


@dataclass
class Finding:
    line_number: int
    severity: str
    line: SourceLine
    matches: list[RuleMatch]
    fingerprint: str
    context_before: list[SourceLine] = field(default_factory=list)
    context_after: list[SourceLine] = field(default_factory=list)
    status: str = "candidate"


@dataclass
class FindingGroup:
    fingerprint: str
    severity: str
    rule_ids: list[str]
    first_line: int
    last_line: int
    count: int = 1


@dataclass
class ScanReport:
    schema_version: int
    tool_version: str
    source: str
    encoding: str
    sha256: str
    byte_count: int
    line_count: int
    ruleset_sha256: str
    options: dict[str, Any]
    matched_line_count: int
    counts_by_severity: dict[str, int]
    counts_by_rule: dict[str, int]
    findings: list[Finding]
    groups: list[FindingGroup]
    omitted_finding_count: int
    ungrouped_match_count: int
    warnings: list[str]
    status: str = "ok"
    scan_complete: bool = True
    assessment: str = "rule_candidates_only"
    retention_policy: str = "severity_then_first"
    # Decoding provenance: what was requested, whether a fallback candidate or
    # degraded decoding was needed, and which lines contain escaped bytes.
    encoding_requested: str = "auto"
    encoding_fallback: bool = False
    # Why this encoding was chosen (reader.ENCODING_BASIS), e.g. "language_code:DEU".
    encoding_basis: str = ""
    decode_degraded_line_count: int = 0
    decode_degraded_lines: list[int] = field(default_factory=list)
    # A journal Revit was still writing: the report covers a snapshot of it.
    live: bool = False
    snapshot: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
