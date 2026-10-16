"""Add-in records: their kind, name and bounded, event-local vendor evidence,
read from the record itself; never inferred from DLL paths."""

import re
from urllib.parse import urlsplit

# The loading-record regexes live in categories (the Categorizer classifies
# loading starts as "addin"); loading_payload and is_addin_start are re-exported.
from .categories import (_ADDIN_GATE, _PREFIX, _START, CATEGORIES, START_GATE,  # noqa: F401
                         is_addin_start, loading_payload)
from .rules import fold

# Origins of add-in records (loads, failed loads, services, manifests, ...);
# the add-in records themselves are the "addin" category of CATEGORIES.
ADDIN_FILTERS = {
    "addin_autodesk": "Autodesk来源",
    "addin_third_party": "第三方来源",
    "addin_unknown": "来源待定",
}
ADDIN_ORIGINS = {"autodesk": "Autodesk", "third_party": "第三方", "unknown": "来源待定"}
# Kinds of add-in records (blocks.addin_kind); see ADDIN_RECORDS.
ADDIN_KINDS = {
    "load": "加载", "service": "服务", "server": "服务器", "manifest": "清单", "event": "外部事件",
    "updater": "更新器", "server_run": "服务器执行", "resource": "外部资源", "folder": "目录注册",
    "assembly": "程序集", "not_loaded": "未加载", "ui": "窗口操作", "control": "日志控件",
    "unregister": "注销全部服务",
}
BLOCK_FILTERS = {**CATEGORIES, **ADDIN_FILTERS}
ADDIN_KEYS = {"i": "addin", "a": "addin_autodesk", "t": "addin_third_party", "u": "addin_unknown"}
_VENDOR = re.compile(r"\bVendor\s*:\s*(.*)", re.I)
_FIELD = re.compile(r",?\s*\b(?:Assembly(?:\s+Version)?|Class)\s*:", re.I)
_IDENTITY_FIELDS = re.compile(r"(?:^|,\s*)(Class|Vendor|Assembly(?:\s+Version)?)\s*:\s*", re.I)
_URL = re.compile(
    r"https?://[^\s,){}]+|(?<![\w@/.-])(?:[a-z0-9-]+\.)+[a-z]{2,}"
    r"(?![\w@.-])(?:[/?#][^\s,){}]*)?", re.I,
)
_UNKNOWN = {"", "none", "<none>", "unknown", "<unknown>", "n/a", "null", "?"}
_MAX_VENDOR = 4096
_MAX_EVIDENCE = 8
_MAX_IDENTITY = 512


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


def vendor_id_origin(value: str) -> tuple[str, str]:
    """Origin of a declared vendor ID (VendorId(...), Rvt.Attr.VendorId): as
    vendor_origin, and Revit's own API records declare "Autodesk" or
    "Autodesk <product>" (VendorId(Autodesk Navisworks))."""
    declared = value.split("(", 1)[0].strip()
    if declared == "Autodesk" or declared.startswith("Autodesk "):
        return "autodesk", "vendor_id_autodesk"
    return vendor_origin(value)


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


def _attr(field: str, group: str) -> str:
    """An optional Rvt.Attr.<field> value, wherever it is on the line: up to
    the next Rvt.Attr. field (not always preceded by a space) or the end."""
    return rf"(?:(?=.*?Rvt\.Attr\.{field}:[ \t]*(?P<{group}>.*?)[ \t]*(?:Rvt\.Attr\.|$))|)"


# Add-in records other than loads (LoadingEvidence reads those across lines):
# (kind, literal gate, regex). The first record whose gate has a term in
# rules.fold(text) and whose regex matches describes the line; every match
# contains its gate (tests/test_categories_gates.py). Named groups: name, path
# (the name is its last segment), class, vendor and description (the vendor
# becomes "vendor(description)", like the Vendor field of a loading record).
# Names come from the record itself, never from a DLL path.
_NAMED = r"(?:.*?\bName\((?P<name>.*?)\)(?=\s*(?:;|\}|$)))?(?:.*?\bVendorId\((?P<vendor>[^)]*)\))?"
ADDIN_RECORDS = (
    # Before not_loaded: manifest lines also name the add-in type ExternalDBApplication.
    ("manifest", ("jrn.addinmanifest",), re.compile(
        r"\[Jrn\.AddInManifest\]" + _attr("AddInName", "name") + _attr("CommandClassName", "class")
        + _attr("CommandVendorId", "vendor") + _attr("CommandVendorDescription", "description"), re.I)),
    ("service", ("registered an external service",), re.compile(r"\bRegistered an external service\b" + _NAMED, re.I)),
    ("server", ("registered an external server",), re.compile(r"\bRegistered an external server\b" + _NAMED, re.I)),
    # Revit's journal record of a server registration, mostly right after its API record.
    ("server", ("jrn.registerexternalserver",), re.compile(r"\[Jrn\.RegisterExternalServer\]", re.I)),
    ("event", ("jrn.externaleventexecution",), re.compile(
        r"\[Jrn\.ExternalEventExecution\]" + _attr("AddInName", "name") + _attr("VendorId", "vendor")
        + _attr("VendorDescription", "description"), re.I)),
    ("updater", ("jrn.addindynamicupdate",), re.compile(
        r"\[Jrn\.AddInDynamicUpdate\]" + _attr("AddInName", "name") + _attr("VendorId", "vendor")
        + _attr("VendorDescription", "description"), re.I)),
    ("updater", ("third party updater",), re.compile(r"\bThird Party Updater\b(?:\s*'(?P<name>[^']*)')?", re.I)),
    ("updater", ("forcibly unregistering updater", "forcibly removing all update triggers"), re.compile(
        r"\bForcibly (?:unregistering Updater|removing all update triggers)\b", re.I)),
    ("server_run", ("jrn.externalserverexecution",), re.compile(r"\[Jrn\.ExternalServerExecution\]", re.I)),
    ("resource", ("jrn.externalresourceload",), re.compile(r"\[Jrn\.ExternalResourceLoad\]", re.I)),
    ("folder", ("the add-in folder",), re.compile(r"\bThe add-in folder\b(?:\s*'(?P<path>[^']*)')?", re.I)),
    ("assembly", ("the requested assembly",), re.compile(
        r"\bThe requested assembly\b(?:\s*'(?P<name>[^,']*))?.*?\bwas loaded to the context\b", re.I)),
    ("not_loaded", ("externaldbapplication",), re.compile(
        r"\bExternalDBApplication\s+(?P<class>[^\s(]+)(?:\s*\(.*?\))?\s*(?:was not|wasn't|could not be) loaded\b",
        re.I)),
    ("not_loaded", ("add-in manifest file",), re.compile(
        r"\bAdd-in manifest file\b(?:\s+from)?\s*:?\s*(?P<path>.+?)\s*,?\s*"
        r"(?:won't be|will not be|was not|wasn't|could not be) loaded\b", re.I)),
    ("ui", ("jrn.addinevent",), re.compile(r"\bJrn\.AddInEvent\b(?:.*?\bWpfWindow\((?P<name>[^,)]*))?", re.I)),
    ("control", ("addinjournalclient",), re.compile(
        r"\bAddInJournalClient\b(?:.*?\bcontrol\s*:\s*(?P<name>[^=]*?)\s*=)?", re.I)),
    ("unregister", ("unregistering all external services",), re.compile(
        r"\bUnregistering all external services\b", re.I)),
)
# A record names itself near its start; longer lines are read up to here, which
# also bounds the lazy scans of the regexes above on pathological lines.
_MAX_RECORD = 4096
_SERVER_ID = re.compile(r"\bServerId\(\s*([^)\s]*)\s*\)", re.I)
_REGISTERED_SERVER = re.compile(r"\[Jrn\.RegisterExternalServer\](?:.*?\bRvt\.Attr\.ExternalServer:\s*(\S*))?", re.I)


def _last_segment(path: str) -> str:
    return re.split(r"[\\/]+", path.strip().strip("'\"").rstrip("\\/"))[-1]


def addin_record(text: str, folded: str | None = None) -> tuple[str, str, str, str] | None:
    """(kind, name, vendor, class) of an add-in record other than a load, or None."""
    folded = fold(text) if folded is None else folded
    if not any(map(folded.__contains__, _ADDIN_GATE)):
        return None
    text = text[:_MAX_RECORD]
    for kind, gate, regex in ADDIN_RECORDS:
        match = any(map(folded.__contains__, gate)) and regex.search(text)
        if match:
            groups = match.groupdict()
            name = (groups.get("name") or "").strip()
            if not name and groups.get("path"):
                name = _last_segment(groups["path"])
            vendor = (groups.get("vendor") or "").strip()
            description = (groups.get("description") or "").strip()
            if description:
                vendor += "(" + description + ")"
            return kind, name, vendor, (groups.get("class") or "").strip()
    return None


def describe_addin(text: str) -> tuple[str, str, str, str] | None:
    """(kind, name, vendor, class) of the add-in record on the line (kinds:
    ADDIN_KINDS), or None. A load's later lines are read by LoadingEvidence."""
    if is_addin_start(text):
        evidence = LoadingEvidence()
        evidence.feed(1, text)
        _, found = evidence.finish()
        return "load", evidence.name, found[0]["value"], evidence.class_name
    return addin_record(text)


def server_id(text: str) -> str:
    """ServerId(...) of a "Registered an external server" record, or ""."""
    match = _SERVER_ID.search(text) if "serverid" in text.lower() else None
    return match.group(1) if match else ""


def registered_server(text: str) -> str | None:
    """None unless the line is a [Jrn.RegisterExternalServer] record, else its
    Rvt.Attr.ExternalServer ID ("" when it has none)."""
    match = _REGISTERED_SERVER.search(text) if "registerexternalserver" in text.lower() else None
    return None if match is None else match.group(1) or ""


def block_filter(category: str, alias: str = "b") -> tuple[str, str]:
    if category in ADDIN_FILTERS:
        return f"{alias}.addin_origin=?", category.removeprefix("addin_")
    return f"{alias}.category=?", category


def matches_filter(block, category: str) -> bool:
    if category == "all":
        return True
    if category in ADDIN_FILTERS:
        return block["addin_origin"] == category.removeprefix("addin_")
    return block["category"] == category


def block_label(block: dict) -> str:
    label = CATEGORIES[block["category"]]
    if block.get("addin_origin"):
        label += " / " + ADDIN_FILTERS["addin_" + block["addin_origin"]]
    return label


def block_summary(block: dict) -> str:
    """Names first, "name [kind] [origin]"; the original records are fetched
    only on expansion. An unnamed record other than a load keeps its title."""
    if not block.get("addin_origin"):
        return f"[{block_label(block)}] {block['title']}"
    kind = block.get("addin_kind") or "load"
    name = block.get("addin_name") or block.get("addin_class") or ""
    tags = "[" + ADDIN_KINDS.get(kind, kind) + "] [" + ADDIN_ORIGINS[block["addin_origin"]] + "]"
    summary = name + " " + tags if name else tags
    if block.get("severity") or block["category"] in {"error", "api_error", "warning"}:
        summary += " [" + CATEGORIES[block["category"]] + "]"
    if block.get("addin_identity_truncated"):
        summary += " [名称/Class摘要截断]"
    if not name and kind != "load" and block.get("title"):
        summary += " " + block["title"]
    return summary
