"""Declarative, locally extensible rules; no executable code in rule files."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from .models import RuleMatch, SEVERITY_RANK


class RuleConfigError(ValueError):
    pass


# Under re.IGNORECASE an ASCII letter also matches these non-ASCII characters
# (exhaustively checked over all code points, see tests/test_rules.py). str.lower()
# alone does not map them, so gate folding translates them first. The Kelvin sign
# U+212A already lowers to "k".
_IGNORECASE_EXTRA = {0x130: "i", 0x131: "i", 0x17F: "s"}
_EXTRA_CHARS = tuple(chr(code) for code in _IGNORECASE_EXTRA)


def fold(text: str) -> str:
    """Lowercase text so that a gate term found by ``in`` is a necessary
    condition for any case-insensitive regex match of that literal."""
    if not text.isascii() and any(char in text for char in _EXTRA_CHARS):
        text = text.translate(_IGNORECASE_EXTRA)
    return text.lower()


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    severity: str
    pattern: str
    explanation: str
    basis: str = "heuristic"
    source_url: str | None = None
    exclude_patterns: tuple[str, ...] = ()
    # Literal pre-filter: every line the pattern matches contains at least one
    # of these (folded) substrings. Empty means "always run the pattern".
    gate: tuple[str, ...] = ()
    _compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)
    _excludes: tuple[re.Pattern[str], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "_compiled", re.compile(self.pattern, re.IGNORECASE))
            object.__setattr__(self, "_excludes", tuple(
                re.compile(pattern, re.IGNORECASE) for pattern in self.exclude_patterns
            ))
        except re.error as error:
            raise RuleConfigError(f"规则{self.id}的正则无效：{error}") from error
        if self._compiled.search("") is not None:
            raise RuleConfigError(f"规则{self.id}不能匹配空字符串")
        if any(not isinstance(term, str) or not term for term in self.gate):
            raise RuleConfigError(f"规则{self.id}的gate必须是非空字符串数组")
        object.__setattr__(self, "gate", tuple(dict.fromkeys(fold(term) for term in self.gate)))

    def passes_gate(self, folded: str) -> bool:
        if not self.gate:
            return True
        for term in self.gate:
            if term in folded:
                return True
        return False

    def match(self, text: str, folded: str | None = None) -> RuleMatch | None:
        if self.gate and not self.passes_gate(fold(text) if folded is None else folded):
            return None
        return self.match_pattern(text)

    def match_pattern(self, text: str) -> RuleMatch | None:
        """The rule's regex and exclusions only, without the gate."""
        match = self._compiled.search(text)
        if match is None or any(pattern.search(text) for pattern in self._excludes):
            return None
        start = max(0, match.start() - 80)
        end = min(len(text), max(match.end(), match.start() + 160) + 80, start + 400)
        return RuleMatch(
            rule_id=self.id, title=self.title, severity=self.severity,
            basis=self.basis, explanation=self.explanation, source_url=self.source_url,
            column=match.start() + 1, matched_text=match.group()[:240],
            match_length=match.end() - match.start(),
            match_truncated=len(match.group()) > 240,
            excerpt=text[start:end], excerpt_start_column=start + 1,
        )

    def definition(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "severity": self.severity,
            "pattern": self.pattern, "explanation": self.explanation,
            "basis": self.basis, "source_url": self.source_url,
            "exclude_patterns": list(self.exclude_patterns),
            "gate": list(self.gate),
        }


def match_rules(rules: tuple[Rule, ...], text: str) -> list[RuleMatch]:
    """All rule matches for one line; the line is folded once for every gate."""
    folded = None
    result = []
    for rule in rules:
        if rule.gate:
            if folded is None:
                folded = fold(text)
            for term in rule.gate:
                if term in folded:
                    break
            else:
                continue
        match = rule.match_pattern(text)
        if match is not None:
            result.append(match)
    return result


def _parse_rule(value: Any) -> Rule:
    required = {"id", "title", "severity", "pattern", "explanation"}
    allowed = required | {"basis", "source_url", "exclude_patterns", "gate"}
    if not isinstance(value, dict):
        raise RuleConfigError("每条规则必须是JSON对象")
    if required - value.keys() or value.keys() - allowed:
        raise RuleConfigError(f"规则字段错误，必填：{sorted(required)}；允许：{sorted(allowed)}")
    for name in required | ({"basis"} & value.keys()):
        if not isinstance(value[name], str) or not value[name].strip():
            raise RuleConfigError(f"规则字段{name}必须是非空字符串")
    if not re.fullmatch(r"[a-z][a-z0-9_.-]*", value["id"]):
        raise RuleConfigError("规则id必须以小写字母开头，只能含小写字母、数字、点、横线、下划线")
    if value["severity"] not in SEVERITY_RANK:
        raise RuleConfigError("severity必须为warning、error或fatal")
    if value.get("basis", "heuristic") not in {"heuristic", "documented_marker", "case_verified"}:
        raise RuleConfigError("basis必须为heuristic、documented_marker或case_verified")
    url = value.get("source_url")
    if url is not None and (not isinstance(url, str) or not url.startswith(("https://", "http://"))):
        raise RuleConfigError("source_url必须是HTTP(S)链接或null")
    excludes = value.get("exclude_patterns", [])
    if not isinstance(excludes, list) or any(not isinstance(item, str) for item in excludes):
        raise RuleConfigError("exclude_patterns必须是字符串数组")
    gate = value.get("gate", [])
    if "gate" in value and (not isinstance(gate, list) or not gate
                            or any(not isinstance(item, str) or not item for item in gate)):
        raise RuleConfigError("gate必须是非空的字符串数组（每项为该规则任何命中行都必然包含的字面子串之一）")
    return Rule(**{**value, "exclude_patterns": tuple(excludes), "gate": tuple(gate)})


def _parse_document(content: str) -> list[Rule]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise RuleConfigError(f"规则文件不是有效JSON：{error}") from error
    if (
        not isinstance(data, dict) or set(data) != {"schema_version", "rules"}
        or type(data["schema_version"]) is not int or data["schema_version"] != 1
        or not isinstance(data["rules"], list)
    ):
        raise RuleConfigError("规则文件需要schema_version: 1和rules数组")
    return [_parse_rule(item) for item in data["rules"]]


def load_rules(extra_path: str | Path | None = None) -> tuple[Rule, ...]:
    builtin = files("revit_journal_tools").joinpath("data", "builtin_rules.json")
    rules = _parse_document(builtin.read_text(encoding="utf-8"))
    if extra_path is not None:
        rules.extend(_parse_document(Path(extra_path).read_text(encoding="utf-8-sig")))
    ids: set[str] = set()
    for rule in rules:
        if rule.id in ids:
            raise RuleConfigError(f"重复的规则id：{rule.id}；自定义规则不会静默覆盖内置规则")
        ids.add(rule.id)
    return tuple(rules)


def ruleset_digest(rules: tuple[Rule, ...]) -> str:
    content = json.dumps([rule.definition() for rule in rules], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
