import json
import random
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from revit_journal_tools.rules import RuleConfigError, fold, load_rules, match_rules, ruleset_digest


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "rules.json"
        self.rule = {
            "id": "custom.failure", "title": "Custom error", "severity": "error",
            "pattern": "CUSTOM_FAILURE:", "explanation": "Synthetic rule test",
        }

    def write_rule(self, rule):
        self.path.write_text(json.dumps({"schema_version": 1, "rules": [rule]}), encoding="utf-8")

    def test_extension_and_rule_scoped_exclusions(self):
        self.write_rule({**self.rule, "exclude_patterns": ["expected test"]})
        rules = load_rules(self.path)
        custom = rules[-1]
        self.assertIsNotNone(custom.match("custom_failure: actual problem"))
        self.assertIsNone(custom.match("CUSTOM_FAILURE: expected test"))
        self.assertIsNone(custom.match("CUSTOM_FAILURE: expected test DBG_ERROR: other problem"))
        self.assertTrue(any(rule.match("CUSTOM_FAILURE: expected test DBG_ERROR: other problem") for rule in rules[:-1]))

    def test_invalid_definitions_are_rejected(self):
        for changes in (
            {"pattern": "["}, {"severity": "major"}, {"id": "revit.dbg_warn"},
            {"pattern": ".*"}, {"typo_field": "value"}, {"pattern": 12},
            {"exclude_patterns": "ignore"}, {"basis": "verified_by_magic"},
            {"source_url": "file:///local"}, {"explanation": ""},
        ):
            with self.subTest(changes=changes):
                self.write_rule({**self.rule, **changes})
                with self.assertRaises(RuleConfigError):
                    load_rules(self.path)

    def test_invalid_json_and_schema(self):
        for content in ("{bad", "[]", '{"schema_version": true, "rules": []}', '{"schema_version": 2, "rules": []}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(RuleConfigError):
                    load_rules(self.path)

    def test_digest_changes_with_rule_content(self):
        original = ruleset_digest(load_rules())
        self.write_rule(self.rule)
        extended = ruleset_digest(load_rules(self.path))
        self.assertNotEqual(original, extended)
        self.assertEqual(original, ruleset_digest(load_rules()))

    def test_native_exception_and_assertion_events(self):
        rules = {rule.id: rule for rule in load_rules()}
        native = rules["revit.exception_event"]
        for message in ("ExceptionCode=0xe06d7363", "Exception occurred", "System exception in Synthetic::run"):
            with self.subTest(message=message):
                self.assertIsNotNone(native.match(message))
        for message in ("ExceptionCode=0x00000000", "exception handler ready", "ExceptionCode documentation"):
            with self.subTest(message=message):
                self.assertIsNone(native.match(message))
        assertion = rules["revit.assertion"]
        self.assertIsNotNone(assertion.match("Assertion failed: line 123 of Synthetic.cpp"))
        self.assertIsNone(assertion.match("Assertion checks enabled"))

    def test_managed_base_exception_and_long_namespace_noise(self):
        rule = next(rule for rule in load_rules() if rule.id == "generic.exception")
        self.assertIsNotNone(rule.match("System.Exception: synthetic failure"))
        self.assertIsNotNone(rule.match("Exception: synthetic failure"))
        self.assertIsNone(rule.match("System.Exception handler ready"))
        # Do not restart matching at every dot in a long non-exception identifier.
        self.assertIsNone(rule.match("Synthetic." * 10_000 + "NormalMessage:"))

    def test_gate_definitions_are_validated_and_optional(self):
        for gate in ([], [""], [3], "error"):
            with self.subTest(gate=gate):
                self.write_rule({**self.rule, "gate": gate})
                with self.assertRaises(RuleConfigError):
                    load_rules(self.path)
        self.write_rule({**self.rule, "gate": ["Custom_Failure", "custom_failure"]})
        custom = load_rules(self.path)[-1]
        self.assertEqual(custom.gate, ("custom_failure",))
        self.assertIsNotNone(custom.match("x CUSTOM_FAILURE: y"))
        self.assertIsNone(custom.match("CUSTOM FAILURE: gate absent"))
        self.write_rule(self.rule)  # Custom rules may omit the gate.
        ungated = load_rules(self.path)[-1]
        self.assertEqual(ungated.gate, ())
        self.assertIsNotNone(ungated.match("custom_failure: no gate"))

    def test_api_error_brace_format_observed_in_uploaded_journals(self):
        rule = next(rule for rule in load_rules() if rule.id == "revit.api_error")
        self.assertIsNotNone(rule.match("' 0:< API_ERROR { : Assembly version conflict in Example.Library assembly"))
        self.assertIsNotNone(rule.match("API_ERROR: synthetic failed call"))
        self.assertIsNone(rule.match("API_SUCCESS { Registered an external server: Name(Example) }"))
        self.assertIsNone(rule.match("API_ERROR counter initialized"))


ROOT = Path(__file__).resolve().parent.parent
# Lines that each builtin regex must or must not match, plus near misses.
GATE_SAMPLES = [
    "' 0:< DBG_ERROR: fatal error", "DBG_ERROR : spaced", "DBG_ERRORS: plural", "dbg_error:",
    "DBG_WARN: Synthetic warning", "DBG_WARNING: no", "Unrecoverable Error in module", "FATAL ERROR",
    "unhandled exception at 0x0", "Access violation reading", "无法恢复的错误", "致命错误", "未处理的异常", "访问冲突",
    "ERROR: last", "Error : spaced", ">ERROR:", "]错误：全角冒号", "错误: 不满足约束", "0 errors", "Error reporting initialized",
    "' 0:< API_ERROR { : Assembly version conflict", "API_ERROR: failed call", "API_ERROR counter initialized",
    "API_SUCCESS { Registered an external server }", "System.Exception: synthetic", "Exception: plain",
    "System.Exception handler ready", "Synthetic." * 50 + "NormalMessage:", "ExceptionCode=0xe06d7363",
    "ExceptionCode=0x00000000", "Exception occurred", "System exception in Synthetic::run", "exception handler ready",
    "Assertion failed: line 1", "Assertion checks enabled", "Out of memory", "OutOfMemoryException",
    "E_OUTOFMEMORY", "内存不足", "Access is denied", "access denied", "UnauthorizedAccessException", "File not found",
    "FileNotFoundException", "could not find the file", "Could not load file", "could not finish", "拒绝访问",
    "找不到指定的文件", "找不到文件", "Failed to synchronise", "failed to synchronize with central",
    "cannot reconcile the differences", "Merge exception", "File not saved", "无法同步", "同步失败",
    "Missing ESSchema", "MissingESSchema", "Jrn.Command \"Show Error Report\"", "",
    # IGNORECASE also matches i/s/k with U+0130, U+0131, U+017F and U+212A.
    "Exceptıon: dotless i", "EXCEPTİON: dotted I", "Aſſertion failed: long s", "dbg_errOr:", "mıssıng esschema",
    "ExceptıonCode=0x1", "file not ſaved", "Fatal errOR", "API_ERRORK", "outofmemoryexception",
]
FUZZ_TOKENS = [
    "DBG_ERROR", "DBG_WARN", "API_ERROR", "API_SUCCESS", "ERROR", "Error", "错误", "Exception", "System.Exception",
    "ExceptionCode", "Exception occurred", "System exception in", "Assertion failed", "out of memory",
    "OutOfMemoryException", "E_OUTOFMEMORY", "内存不足", "access", "access denied", "access is denied",
    "UnauthorizedAccessException", "file not found", "FileNotFoundException", "could not find the file",
    "could not load file", "拒绝访问", "找不到指定的文件", "failed to synchronise", "failed to synchronize",
    "cannot reconcile the differences", "merge exception", "file not saved", "无法同步", "同步失败",
    "Missing ESSchema", "fatal error", "unrecoverable error", "unhandled exception", "access violation",
    "无法恢复的错误", "致命错误", "未处理的异常", "访问冲突", "' 0:< ", ">", "]", ":", "：", "=", " = ", "{", "0x1",
    "0x0", " ", ".", "_", "x", "123", "Synthetic.",
]
LOOKALIKES = {"i": ["ı", "İ"], "s": ["ſ"], "k": ["K"]}


def gate_corpus():
    lines = list(GATE_SAMPLES)
    for name in ("examples", "tests"):
        for path in sorted((ROOT / name).glob("*.*")):
            if path.suffix in {".txt", ".py"}:
                lines.extend(path.read_text(encoding="utf-8").splitlines())
    generator = random.Random(20261010)
    for _ in range(6000):
        line = ""
        for _ in range(generator.randint(1, 6)):
            token = generator.choice(FUZZ_TOKENS)
            chars = []
            for char in token:
                roll = generator.random()
                if roll < 0.25:
                    char = char.swapcase()
                elif roll < 0.32 and char.lower() in LOOKALIKES:
                    char = generator.choice(LOOKALIKES[char.lower()])
                chars.append(char)
            line += "".join(chars) + generator.choice(["", " ", ":", " : ", "=", "{", "\t"])
        lines.append(line)
    return lines


class GateEquivalenceTests(unittest.TestCase):
    """J5: gates are only a pre-filter; with and without them the hits are identical."""

    def test_every_builtin_rule_declares_a_gate(self):
        for rule in load_rules():
            with self.subTest(rule=rule.id):
                self.assertTrue(rule.gate)
                self.assertTrue(all(term == fold(term) for term in rule.gate))

    def test_gated_and_ungated_hits_are_identical(self):
        rules = load_rules()
        ungated = tuple(replace(rule, gate=()) for rule in rules)
        corpus = gate_corpus()
        self.assertGreater(len(corpus), 6000)
        hits = 0
        for text in corpus:
            expected = [match for rule in ungated if (match := rule.match(text))]
            hits += len(expected)
            self.assertEqual(match_rules(rules, text), expected, text)
            self.assertEqual([match for rule in rules if (match := rule.match(text))], expected, text)
        # The corpus must exercise every rule, positively, many times.
        self.assertGreater(hits, 1000)
        for rule in ungated:
            with self.subTest(rule=rule.id):
                self.assertTrue(any(rule.match(text) for text in corpus))

    def test_each_regex_hit_contains_a_gate_term(self):
        rules = load_rules()
        for text in gate_corpus():
            folded = fold(text)
            for rule in rules:
                if rule.match_pattern(text):
                    self.assertTrue(any(term in folded for term in rule.gate), (rule.id, text))

    def test_ignorecase_extra_letters_are_exactly_those_folded(self):
        # Exhaustive over all code points: which non-ASCII characters does
        # re.IGNORECASE treat as an ASCII letter? fold() must map each of them.
        letter = re.compile("[a-z]", re.IGNORECASE)
        extra = [chr(code) for code in range(128, 0x110000)
                 if not 0xD800 <= code <= 0xDFFF and letter.fullmatch(chr(code))]
        self.assertEqual(sorted(extra), sorted(["İ", "ı", "ſ", "K"]))
        for char in extra:
            folded = fold(char)
            self.assertEqual(len(folded), 1)
            self.assertTrue(re.fullmatch(folded, char, re.IGNORECASE))
            self.assertTrue("a" <= folded <= "z")


if __name__ == "__main__":
    unittest.main()
