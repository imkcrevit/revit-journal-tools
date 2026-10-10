"""Bounded, event-local Add-in vendor evidence; never infer from DLL paths."""

import re
from urllib.parse import urlsplit

from .categories import CATEGORIES
from .rules import fold

ADDIN_FILTERS = {
    "addin": "Add-in列表",
    "addin_autodesk": "Autodesk加载",
    "addin_third_party": "第三方加载",
    "addin_unknown": "来源待定加载",
}
BLOCK_FILTERS = {**CATEGORIES, **ADDIN_FILTERS}
ADDIN_KEYS = {"i": "addin", "a": "addin_autodesk", "t": "addin_third_party", "u": "addin_unknown"}
_START = re.compile(
    r"^(?:Starting|Creating|Created|Loading|Loaded)\s+(?:an?\s+)?"
    r"(?:External\s+(?:DB\s+)?Application|Add-?in)\s*:", re.I,
)
_VENDOR = re.compile(r"\bVendor\s*:\s*(.*)", re.I)
_FIELD = re.compile(r",?\s*\b(?:Assembly(?:\s+Version)?|Class)\s*:", re.I)
_PREFIX = re.compile(r"^\s*'\s*(?:\d+:<\s*)?")
_TIMESTAMP = re.compile(r"^\s*'[A-Z]\s+\d{1,2}-[A-Za-z]+-\d{4}\s+[\d:.]+;\s*(?:\d+:<\s*)?")
_API_PREFIX = re.compile(r"^API_(?:SUCCESS|ERROR|INFO)\s*[:{]\s*", re.I)
_IDENTITY_FIELDS = re.compile(r"(?:^|,\s*)(Class|Vendor|Assembly(?:\s+Version)?)\s*:\s*", re.I)
_URL = re.compile(
    r"https?://[^\s,){}]+|(?<![\w@/.-])(?:[a-z0-9-]+\.)+[a-z]{2,}"
    r"(?![\w@.-])(?:[/?#][^\s,){}]*)?", re.I,
)
_UNKNOWN = {"", "none", "<none>", "unknown", "<unknown>", "n/a", "null", "?"}
_MAX_VENDOR = 4096
_MAX_EVIDENCE = 8
_MAX_IDENTITY = 512


# Every _START match contains one of these (checked against rules.fold(text)).
START_GATE = ("application", "addin", "add-in")


def loading_payload(text: str) -> str:
    text = _TIMESTAMP.sub("", text, count=1)
    return _API_PREFIX.sub("", _PREFIX.sub("", text, count=1).strip(), count=1)


def is_addin_start(text: str, folded: str | None = None) -> bool:
    if not any(map((fold(text) if folded is None else folded).__contains__, START_GATE)):
        return False
    return bool(_START.match(loading_payload(text)))


def vendor_origin(value: str) -> tuple[str, str]:
    """Vendor ID or the actual website host, not arbitrary 'Autodesk' text."""
    vendor_id = value.split("(", 1)[0].strip(" '},\t").lower()
    normalized = value.replace("\\", "/")
    normalized = re.sub(r"\b(https?):/+", r"\1://", normalized, flags=re.I)
    official_site = False
    other_site = False
    for match in _URL.finditer(normalized):
        address = match.group()
        try:
            host = urlsplit(address if "://" in address else "https://" + address).hostname
        except ValueError:
            continue
        if host:
            host = host.lower().rstrip(".")
            official_site |= host == "autodesk.com" or host.endswith(".autodesk.com")
            other_site |= not (host == "autodesk.com" or host.endswith(".autodesk.com"))
    if vendor_id == "adsk" or official_site:
        return "autodesk", "vendor_id_adsk" if vendor_id == "adsk" else "autodesk_domain"
    if other_site or vendor_id not in _UNKNOWN:
        return "third_party", "other_vendor"
    return "unknown", "vendor_missing"


class LoadingEvidence:
    """Keep bounded evidence until the entire loading event has been read."""

    def __init__(self):
        self.active = False
        self.start_line = 0
        self.last_line = 0
        self.origins: set[str] = set()
        self.evidence: list[dict] = []
        self.vendor = ""
        self.vendor_line = 0
        self.pending = False
        self.truncated = False
        self.name = ""
        self.class_name = ""
        self.identity_truncated = False
        self.identity_field = ""
        self.identity_value = ""

    def _finish_identity(self) -> None:
        value = self.identity_value.strip(" ,}")
        if self.identity_field == "name" and not self.name:
            self.name = value
        elif self.identity_field == "class" and not self.class_name:
            self.class_name = value
        self.identity_field = self.identity_value = ""

    def _append_identity(self, value: str) -> None:
        if not self.identity_field:
            return
        joiner = " " if self.identity_field == "name" and self.identity_value else ""
        joined = self.identity_value + joiner + value.strip()
        self.identity_truncated |= len(joined) > _MAX_IDENTITY
        self.identity_value = joined[:_MAX_IDENTITY]

    def _read_identity(self, text: str) -> None:
        previous = 0
        for match in _IDENTITY_FIELDS.finditer(text):
            self._append_identity(text[previous:match.start()])
            self._finish_identity()
            if match.group(1).lower() == "class":
                self.identity_field = "class"
            previous = match.end()
        self._append_identity(text[previous:])
        if text.rstrip().endswith((",", "}")):
            self._finish_identity()

    def _finish_vendor(self) -> None:
        origin, reason = vendor_origin(self.vendor)
        if self.truncated or ("(" in self.vendor and self.vendor.count("(") > self.vendor.count(")")):
            origin, reason = "unknown", "vendor_incomplete"
        self.origins.add(origin)
        if len(self.evidence) < _MAX_EVIDENCE:
            self.evidence.append({"line_number": self.vendor_line, "value": self.vendor[:400], "basis": reason})
        self.vendor, self.pending, self.truncated = "", False, False

    def feed(self, number: int, text: str, folded: str | None = None) -> None:
        if number == self.last_line:
            return
        self.last_line = number
        if not self.active and not any(map((fold(text) if folded is None else folded).__contains__, START_GATE)):
            return  # Inactive and no loading prefix possible: nothing to record.
        content = loading_payload(text)
        start = _START.match(content)
        if start and not self.active:
            self.active = True
            self.start_line = number
            self.identity_field = "name"
            content = content[start.end():].lstrip()
        if not self.active:
            return
        self._read_identity(content)
        match = _VENDOR.search(text)
        if match:
            if self.pending:
                self._finish_vendor()
            self.vendor_line = number
            part = match.group(1)
            self.vendor = ""
        elif self.pending:
            part = _PREFIX.sub("", text).strip()
        else:
            return
        field = _FIELD.search(part)
        if field:
            part = part[:field.start()]
        combined = (self.vendor + " " + part).strip()
        self.truncated |= len(combined) > _MAX_VENDOR
        self.vendor = combined[:_MAX_VENDOR].rstrip(" }")
        self.pending = not field and (not self.vendor or self.vendor.count("(") > self.vendor.count(")"))
        if not self.pending:
            self._finish_vendor()

    def finish(self) -> tuple[str, list[dict]]:
        if not self.active:
            return "", []
        self._finish_identity()
        if self.pending:
            self._finish_vendor()
        if not self.evidence:
            self.evidence.append({"line_number": self.start_line, "value": "", "basis": "vendor_missing"})
        known = self.origins - {"unknown"}
        origin = next(iter(known)) if len(known) == 1 and "unknown" not in self.origins else "unknown"
        return origin, self.evidence


def block_filter(category: str, alias: str = "b") -> tuple[str, str]:
    if category == "addin":
        return f"{alias}.addin_origin<>?", ""
    if category in ADDIN_FILTERS:
        return f"{alias}.addin_origin=?", category.removeprefix("addin_")
    return f"{alias}.category=?", category


def matches_filter(block, category: str) -> bool:
    if category == "all":
        return True
    if category == "addin":
        return bool(block["addin_origin"])
    if category in ADDIN_FILTERS:
        return block["addin_origin"] == category.removeprefix("addin_")
    return block["category"] == category


def block_label(block: dict) -> str:
    label = CATEGORIES[block["category"]]
    if block.get("addin_origin"):
        label += " / " + ADDIN_FILTERS["addin_" + block["addin_origin"]]
    return label


def block_summary(block: dict) -> str:
    """Names first; original loading records are fetched only on expansion."""
    if not block.get("addin_origin"):
        return f"[{block_label(block)}] {block['title']}"
    name = block.get("addin_name") or block.get("addin_class") or "未提供名称的Add-in"
    summary = name + " [" + ADDIN_FILTERS["addin_" + block["addin_origin"]] + "]"
    if block.get("severity") or block["category"] in {"error", "api_error", "warning"}:
        summary += " [" + CATEGORIES[block["category"]] + "]"
    if block.get("addin_identity_truncated"):
        summary += " [名称/Class摘要截断]"
    return summary
