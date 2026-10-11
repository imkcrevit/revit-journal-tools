"""Load-time check for the classic catastrophic-backtracking shape (J23).

Python's re has no timeout, and one rule runs on every gated line (up to
max_line_chars). An unbounded quantifier inside a repeated group, such as
``(a+)+`` or ``(\\w+\\s?)*``, lets the engine split the same text between
iterations in exponentially many ways when the line does not match.

The check works on the parsed pattern. Inside a group that can repeat, an
unbounded greedy or lazy quantifier is accepted only when it repeats a single
character class and every character that can come right after it (later in
the group, or at the start of the next iteration) is outside that class, as in
``(?:[A-Za-z_]\\w*\\.)*``. Anything the check cannot prove is reported, so it
is conservative; possessive quantifiers and atomic groups (Python 3.11+) never
backtrack and are accepted. Overlapping alternatives such as ``(a|ab)*`` and
polynomial shapes such as ``\\w*\\w*x`` are not detected.
"""

import re
from functools import lru_cache

try:  # Python 3.11+
    from re import _parser as _sre_parse
except ImportError:  # Python 3.10
    import sre_parse as _sre_parse  # type: ignore[no-redef]

_MAXREPEAT = _sre_parse.MAXREPEAT
_SINGLE = {"LITERAL", "NOT_LITERAL", "ANY", "IN"}
_REPEATS = {"MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"}
_CATEGORIES = {
    "CATEGORY_DIGIT": r"\d", "CATEGORY_NOT_DIGIT": r"\D", "CATEGORY_SPACE": r"\s",
    "CATEGORY_NOT_SPACE": r"\S", "CATEGORY_WORD": r"\w", "CATEGORY_NOT_WORD": r"\W",
}
# Characters tried when deciding whether two single-character classes overlap:
# Latin-1 and Latin Extended, the letters IGNORECASE maps to ASCII, Unicode
# spaces and digits, CJK and an astral character.
_UNIVERSE = tuple(map(chr, range(0x250))) + tuple(
    "İıſK  　٣０中ア\U0001f600")


def _name(op) -> str:
    return getattr(op, "name", str(op))


def _class_source(name: str, value) -> str | None:
    """Regex source for one single-character node; None when not reconstructible."""
    if name == "LITERAL":
        return re.escape(chr(value))
    if name == "NOT_LITERAL":
        return "[^" + re.escape(chr(value)) + "]"
    if name == "ANY":
        return r"[\s\S]"  # with or without DOTALL: the wider set is the safe choice
    parts = []
    for op, item in value:
        kind = _name(op)
        if kind == "NEGATE":
            parts.insert(0, "^")
        elif kind == "LITERAL":
            parts.append(re.escape(chr(item)))
        elif kind == "RANGE":
            parts.append(re.escape(chr(item[0])) + "-" + re.escape(chr(item[1])))
        elif kind == "CATEGORY" and _name(item) in _CATEGORIES:
            parts.append(_CATEGORIES[_name(item)])
        else:
            return None
    return "[" + "".join(parts) + "]"


@lru_cache(maxsize=256)
def _members(source: str) -> frozenset[str]:
    pattern = re.compile(source, re.IGNORECASE)  # rules ignore case: the wider set again
    return frozenset(char for char in _UNIVERSE if pattern.fullmatch(char))


def _overlap(first: tuple, second: tuple) -> bool:
    sources = _class_source(*first), _class_source(*second)
    if None in sources:
        return True
    return not _members(sources[0]).isdisjoint(_members(sources[1]))


def _single(items: list) -> tuple | None:
    """The node when the sequence is exactly one single-character class."""
    if len(items) != 1:
        return None
    op, value = items[0]
    name = _name(op)
    if name in _SINGLE:
        return name, value
    if name == "SUBPATTERN":
        return _single(list(value[-1].data))
    return None


def _firsts(items: list, tail: list | None) -> list | None:
    """Single-character classes one of which every match of items+tail starts
    with; None when that is not known (lookarounds, alternatives, ...)."""
    result = []
    for op, value in items:
        name = _name(op)
        if name in _SINGLE:
            return result + [(name, value)]
        if name in _REPEATS:
            inner = _firsts(list(value[2].data), None)
            if inner is None:
                return None
            result += inner
            if value[0] >= 1:
                return result
            continue  # optional: what follows may come first
        if name == "SUBPATTERN":
            inner = _firsts(list(value[-1].data), None)
            if inner is None:
                return None
            return result + inner
        return None
    return None if tail is None else result + tail


def _walk(items: list, repeated: bool, follower: list | None, problems: list) -> None:
    """repeated: inside a group that can match more than once."""
    for index, (op, value) in enumerate(items):
        name = _name(op)
        after = _firsts(items[index + 1:], follower)
        if name in _REPEATS:
            low, high, item = value
            body = list(item.data)
            if name == "POSSESSIVE_REPEAT":
                _walk(body, False, None, problems)
                continue
            if repeated and high == _MAXREPEAT:
                single = _single(body)
                if single is None or after is None or any(_overlap(single, other) for other in after):
                    problems.append(name)
            if high > 1:
                # The end of one iteration meets the start of the next.
                _walk(body, True, _firsts(body, None), problems)
            else:
                _walk(body, repeated, after, problems)
        elif name == "SUBPATTERN":
            _walk(list(value[-1].data), repeated, after, problems)
        elif name == "BRANCH":
            for branch in value[1]:
                _walk(list(branch.data), repeated, after, problems)
        elif name == "ATOMIC_GROUP":
            _walk(list(value.data), False, None, problems)
        elif name in ("ASSERT", "ASSERT_NOT"):
            _walk(list(value[1].data), repeated, None, problems)
        elif name == "GROUPREF_EXISTS":
            for branch in value[1:]:
                if branch is not None:
                    _walk(list(branch.data), repeated, after, problems)


def nested_quantifier_risk(pattern: str) -> bool:
    """True when a repeated group contains an unbounded quantifier that the
    check cannot show to be unambiguous (see the module docstring)."""
    problems: list = []
    _walk(list(_sre_parse.parse(pattern, re.IGNORECASE).data), False, None, problems)
    return bool(problems)
