import _path  # noqa: F401

import unittest


from common import ConfigError
from config_load import Problems, is_int, join, read_json


class ReadJsonTest(unittest.TestCase):
    def test_an_object_is_returned(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 131.
        # Mutation: Return an empty object instead of the decoded mapping.
        self.assertEqual(read_json(b'{"a": 1}', 100, "repo file"), {"a": 1})

    def test_a_duplicate_key_is_an_error_naming_it(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 134.
        # Mutation: Keep the last duplicate trigger silently.
        with self.assertRaises(ConfigError) as ctx:
            read_json(b'{"trigger": "auto", "trigger": "requested"}', 100, "repo file")
        self.assertIn("duplicate key 'trigger'", str(ctx.exception))

    def test_a_nested_duplicate_key_is_an_error_too(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 139.
        # Mutation: Check duplicates only in the outermost object.
        with self.assertRaises(ConfigError):
            read_json(b'{"plan": {"label": "a", "label": "b"}}', 100, "repo file")

    def test_a_non_object_is_an_error(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 143.
        # Mutation: Return arrays or scalars without object validation.
        for raw in (b"[]", b'"auto"', b"null", b"3"):
            with self.subTest(raw=raw), self.assertRaises(ConfigError) as ctx:
                read_json(raw, 100, "repo file")
            self.assertIn("must hold a JSON object", str(ctx.exception))

    def test_an_empty_file_is_an_error_not_a_traceback(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 149.
        # Mutation: Return an empty mapping for an empty file.
        with self.assertRaises(ConfigError) as ctx:
            read_json(b"", 100, "repo file")
        self.assertTrue(str(ctx.exception).startswith("repo file: "))

    def test_invalid_json_names_line_and_column(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 154.
        # Mutation: Drop JSON parser location information from ConfigError.
        with self.assertRaises(ConfigError) as ctx:
            read_json(b'{\n  "a": ,\n}', 100, "repo file")
        self.assertIn("line 2", str(ctx.exception))

    def test_nan_and_infinity_are_refused(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 159.
        # Mutation: Accept non-finite JSON constants.
        for raw in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                read_json(raw, 100, "repo file")

    def test_over_the_cap_is_an_error_naming_the_cap(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 164.
        # Mutation: Ignore the byte cap.
        with self.assertRaises(ConfigError) as ctx:
            read_json(b'{"a": "' + b"x" * 200 + b'"}', 64, "repo file")
        self.assertIn("64 bytes", str(ctx.exception))

    def test_exactly_the_cap_is_accepted(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 169.
        # Mutation: Use >= instead of > for the size cap.
        raw = b'{"a": "' + b"x" * 10 + b'"}'
        self.assertEqual(read_json(raw, len(raw), "repo file"), {"a": "x" * 10})

    def test_deep_nesting_is_an_error_not_a_traceback(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 173.
        # Mutation: Let RecursionError escape instead of ConfigError.
        for raw in (b"[" * 2000, b'{"a": ' + b"[" * 1000 + b"]" * 1000 + b"}"):
            with self.subTest(size=len(raw)), self.assertRaises(ConfigError) as ctx:
                read_json(raw, 65536, "repo file")
            self.assertIn("nested too deeply", str(ctx.exception))

    def test_non_utf8_is_an_error(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 179.
        # Mutation: Decode invalid UTF-8 with replacement characters.
        with self.assertRaises(ConfigError) as ctx:
            read_json(b'{"a": "\xff"}', 100, "repo file")
        self.assertIn("UTF-8", str(ctx.exception))


class ProblemsTest(unittest.TestCase):
    def test_lines_join_path_and_message(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 186.
        # Mutation: Drop paths from accumulated problem lines.
        p = Problems()
        p.add("plan.label", "must be a non-empty string")
        p.add("", "the file is empty")
        self.assertEqual(p.lines(), ["plan.label: must be a non-empty string", "the file is empty"])

    def test_empty_is_falsy(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 192.
        # Mutation: Make an empty Problems instance truthy.
        self.assertFalse(Problems())

    def test_unknown_suggests_a_close_key(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 195.
        # Mutation: Omit close-key suggestions.
        p = Problems()
        p.unknown("", "trigers", ["trigger", "team"])
        self.assertEqual(p.lines(), ["trigers: unknown key (did you mean 'trigger'?)"])

    def test_unknown_without_a_close_key_says_only_unknown(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 200.
        # Mutation: Invent a suggestion when no key is close.
        p = Problems()
        p.unknown("plan", "zzz", ["label", "personas"])
        self.assertEqual(p.lines(), ["plan.zzz: unknown key"])

    def test_join(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 205.
        # Mutation: Always prepend a dot in join.
        self.assertEqual(join("", "a"), "a")
        self.assertEqual(join("a", "b"), "a.b")


class IsIntTest(unittest.TestCase):
    def test_a_bool_is_not_an_int(self):
        # Oracle: tester-brief.md, Task 1, Interfaces and signed starting oracle line 211.
        # Mutation: Treat bool as an integer.
        self.assertFalse(is_int(True))
        self.assertTrue(is_int(0))
        self.assertFalse(is_int(1.0))


class StrictBoundaryTest(unittest.TestCase):
    def test_utf8_cap_counts_bytes_not_characters(self):
        # Oracle: Global Constraints, size measured in bytes as read; Task 1 read_json.
        # Mutation: compare decoded character length with cap.
        raw = '{"a":"é"}'.encode("utf-8")
        self.assertEqual(read_json(raw, len(raw), "repo file"), {"a": "é"})
        with self.assertRaises(ConfigError) as ctx:
            read_json(raw, len(raw) - 1, "repo file")
        self.assertTrue(str(ctx.exception).startswith("repo file: "))

    def test_escaped_duplicate_keys_are_still_duplicates(self):
        # Oracle: Review Focus duplicate keys; Task 1 strict JSON.
        # Mutation: compare raw key spellings rather than decoded JSON keys.
        with self.assertRaises(ConfigError) as ctx:
            read_json(b'{"trigger":"auto","trig\\u0067er":"requested"}', 100, "repo file")
        self.assertIn("duplicate key 'trigger'", str(ctx.exception))

    def test_problem_extension_preserves_each_path(self):
        # Oracle: Task 1 Interfaces Problems.extend, lines, __bool__.
        # Mutation: extend drops the second accumulator or overwrites earlier errors.
        first, second = Problems(), Problems()
        first.add("plan.label", "first")
        second.add("code.personas", "second")
        first.extend(second)
        self.assertTrue(first)
        self.assertEqual(first.lines(), ["plan.label: first", "code.personas: second"])
