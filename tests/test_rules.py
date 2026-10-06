import json
import tempfile
import unittest
from pathlib import Path

from revit_journal_tools.rules import RuleConfigError, load_rules, ruleset_digest


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

    def test_api_error_brace_format_observed_in_uploaded_journals(self):
        rule = next(rule for rule in load_rules() if rule.id == "revit.api_error")
        self.assertIsNotNone(rule.match("' 0:< API_ERROR { : Assembly version conflict in Example.Library assembly"))
        self.assertIsNotNone(rule.match("API_ERROR: synthetic failed call"))
        self.assertIsNone(rule.match("API_SUCCESS { Registered an external server: Name(Example) }"))
        self.assertIsNone(rule.match("API_ERROR counter initialized"))


if __name__ == "__main__":
    unittest.main()
