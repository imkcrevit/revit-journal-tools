"""J6: literal gates in classification, paragraph and loading code never change results."""

import random
import unittest
from pathlib import Path
from unittest.mock import patch

from revit_journal_tools import blocks, categories, loading
from revit_journal_tools.blocks import BlockBuilder, payload
from revit_journal_tools.categories import Categorizer
from revit_journal_tools.reader import JournalLine
from revit_journal_tools.rules import fold, load_rules

ROOT = Path(__file__).resolve().parent.parent
# (regex, gate, what the production code searches, "search" or "match")
REGEX_GATES = [
    (categories._JOURNAL, categories._JOURNAL_GATE, "text", "search"),
    (categories._STARTUP, categories._STARTUP_GATE, "text", "search"),
    (categories._SYSTEM, categories._SYSTEM_GATE, "text", "search"),
    (blocks._API, blocks._API_GATE, "text", "search"),
    (blocks._METRIC, blocks._METRIC_GATE, "content", "search"),
    (blocks._SECTION, blocks._SECTION_GATE, "content", "search"),
    (blocks._STRONG_ERROR, blocks._STRONG_GATE, "content", "search"),
    (blocks._NATIVE_START, blocks._NATIVE_GATE, "text", "search"),
    (blocks._NATIVE_DETAIL, blocks._NATIVE_GATE, "text", "search"),
    (blocks._NATIVE_CODE, blocks._NATIVE_CODE_GATE, "text", "search"),
    (loading._START, loading.START_GATE, "loading", "match"),
]
UNGATED = {
    "revit_journal_tools.categories": ["_JOURNAL_GATE", "_STARTUP_GATE", "_SYSTEM_GATE"],
    "revit_journal_tools.blocks": ["_API_GATE", "_METRIC_GATE", "_SECTION_GATE", "_STRONG_GATE", "_NATIVE_GATE",
                                   "_NATIVE_CODE_GATE", "_EXCEPTION_GATE", "START_GATE"],
    "revit_journal_tools.loading": ["START_GATE"],
}
STRUCTURE = [
    "'H 01-Oct-2026 10:00:00.001; 0:<", "'E 01-Oct-2026 10:00:00.002; 0:< ", "'C 01-Oct-2026 10:00:01.001; ",
    "' 0:< ", "' ", "  ", "", "API_ERROR { ", "API_SUCCESS { ", "API_INFO: ", "}", "{", "Jrn.Command _",
    ' "Ribbon", _', 'Jrn.Data "File Name"', "Jrn.RibbonEvent ", "Starting External Application: ",
    "Starting External DB Application: ", "Loaded Add-in: ", "Creating Addin: ", "Vendor : ADSK()",
    "Vendor: DEMO(https://plugins.example)", "Class: Synthetic.App, ", "Assembly: Synthetic.dll }",
    "Exception occurred", "System exception in Run", "ExceptionCode=0xe0434352", "ExceptionFlags=0x81",
    "ExceptionAddress=0001", "DBG_ERROR: ", "DBG_WARN: ", "ERROR: ", "Error: ", "错误: 不满足约束",
    "fatal error", "Assertion failed: ", "System.InvalidOperationException: ", "Exception: ", "InnerError: ",
    "at Synthetic.Run()", "---> Inner", "--- End of stack", "Caused by: x", "Stack trace:", 'File "x.py"',
    "... 3 more", "Initial VM: Avail 100 MB", "Delta VM: Avail 5", "VM: Avail 1", "GUI Resource Usage GDI: 5",
    "SystemSettings:", "System Information", "Log Summary :", "License mode: User", "OSLanguage : 2052",
    "CPU : x", "Autodesk Revit 2024", "Options::loadOptions", "ComputerName: X", "UserName : y",
    "' Build: 24.0", "' Branch: RELEASE", "' Release: 2024", "started recording journal file",
    "finished recording journal file", "InitApplication", "InitNativeInstance", "Starting Revit",
    "ApplicationInitialized", "Set Jrn = CrsJournalScript", "Assembly version conflict", "normal text",
    "x", "_", ": ", "= ", "123",
]
LOOKALIKES = {"i": ["ı", "İ"], "s": ["ſ"], "k": ["K"]}


def mutate(generator, token):
    chars = []
    for char in token:
        roll = generator.random()
        if roll < 0.2:
            char = char.swapcase()
        elif roll < 0.27 and char.lower() in LOOKALIKES:
            char = generator.choice(LOOKALIKES[char.lower()])
        chars.append(char)
    return "".join(chars)


def fuzz_journal(seed, count):
    generator = random.Random(seed)
    lines = []
    for _ in range(count):
        parts = [generator.choice(STRUCTURE) for _ in range(generator.randint(1, 3))]
        if generator.random() < 0.5:
            parts = [mutate(generator, part) for part in parts]
        lines.append("".join(parts))
    return lines


def sample_journals():
    journals = [path.read_text(encoding="utf-8").splitlines() for path in sorted((ROOT / "examples").glob("*.txt"))]
    tests = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        tests.extend(path.read_text(encoding="utf-8").splitlines())
    journals.append(tests)
    journals.extend(fuzz_journal(seed, 4000) for seed in (1, 2, 3))
    return journals


def run(lines):
    categorizer, builder = Categorizer(load_rules()), BlockBuilder()
    classes, result = [], []
    for number, text in enumerate(lines, 1):
        classification = categorizer.classify(text)
        classes.append(classification)
        result.extend(builder.feed(JournalLine(number, text), classification))
    result.extend(builder.finish())
    blocks_ = [(b.start_line, b.end_line, b.category, b.severity, sorted(b.rules), b.title, b.title_line,
                b.boundary, b.complete, b.addin_origin, b.addin_evidence, b.addin_name, b.addin_class,
                b.addin_identity_truncated) for b in result]
    return classes, blocks_


class ShapeGateTests(unittest.TestCase):
    def test_every_regex_match_contains_its_gate(self):
        lines = [line for journal in sample_journals() for line in journal]
        matched = {id(regex): 0 for regex, *_ in REGEX_GATES}
        for text in lines:
            folded = fold(text)
            targets = {"text": text, "content": payload(text), "loading": loading.loading_payload(text)}
            for regex, gate, target, method in REGEX_GATES:
                if getattr(regex, method)(targets[target]):
                    matched[id(regex)] += 1
                    self.assertTrue(any(term in folded for term in gate), (regex.pattern, text))
            if blocks._EXCEPTION.search(targets["content"]):  # case-sensitive regex, case-sensitive gate
                self.assertTrue(any(term in targets["content"] for term in blocks._EXCEPTION_GATE), text)
        # Each regex is exercised by the corpus.
        self.assertTrue(all(matched.values()), matched)

    def test_gated_and_ungated_classification_and_blocks_are_identical(self):
        for number, lines in enumerate(sample_journals()):
            with self.subTest(journal=number):
                gated = run(lines)
                patches = [patch.multiple(module, **{name: ("",) for name in names})
                           for module, names in UNGATED.items()]
                for item in patches:
                    item.start()
                try:
                    ungated = run(lines)
                finally:
                    for item in patches:
                        item.stop()
                self.assertEqual(gated[0], ungated[0])
                self.assertEqual(gated[1], ungated[1])
                self.assertTrue(gated[1])


if __name__ == "__main__":
    unittest.main()
