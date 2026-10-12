import _path  # noqa: F401

import importlib.util
import json
import os
import unittest


import personas
import repo_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(ROOT, "schema", "claudebox.schema.json")

spec = importlib.util.spec_from_file_location("gen", os.path.join(ROOT, "tools", "gen-config-schema.py"))
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def keys_of(schema):
    out = {}
    for name, sub in schema["properties"].items():
        out[name] = keys_of(sub) if sub.get("type") == "object" else None
    return out


class SchemaTest(unittest.TestCase):
    def setUp(self):
        with open(SCHEMA, encoding="utf-8") as fh:
            self.schema = json.load(fh)

    def test_the_committed_file_is_what_the_generator_writes(self):
        # Oracle: tester-brief.md, Task 7, Interfaces and signed starting oracle line 997.
        # Mutation: Leave the committed schema stale after changing generated schema.
        self.assertEqual(self.schema, gen.build(os.path.join(ROOT, "personas")))

    def test_its_keys_are_the_loaders_keys(self):
        # Oracle: tester-brief.md, Task 7, Interfaces and signed starting oracle line 1000.
        # Mutation: Remove one property only from the generated schema.
        self.assertEqual(keys_of(self.schema), repo_config.KEYS)

    def test_every_object_refuses_unknown_keys(self):
        # Oracle: tester-brief.md, Task 7, Interfaces and signed starting oracle line 1003.
        # Mutation: Allow additional properties on a nested schema object.
        def walk(node):
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False)
                for sub in node["properties"].values():
                    walk(sub)
        walk(self.schema)

    def test_the_persona_description_names_every_shipped_persona(self):
        # Oracle: tester-brief.md, Task 7, Interfaces and signed starting oracle line 1011.
        # Mutation: Omit a shipped persona from a selector description.
        for mode in ("code", "plan"):
            prop = self.schema["properties"][mode]["properties"]["personas"]
            self.assertEqual(prop["type"], "string")
            # No grammar: personas.resolve is the one validator (decision 9).
            self.assertNotIn("pattern", prop)
            for name in personas._available(os.path.join(ROOT, "personas", mode)):
                self.assertIn(name, prop["description"])

    def test_the_string_patterns_agree_with_the_loader(self):
        # Oracle: tester-brief.md, Task 7, Interfaces and signed starting oracle line 1020.
        # Mutation: Widen the schema team pattern to permit slashes while loader rejects them.
        # A JSON Schema pattern is ECMA-262, where `$` does not match before a
        # final newline; Python's does. Swap the end anchor for \Z so this
        # test judges the pattern the way an editor will.
        import re

        def ecma(pattern, value):
            return re.search(pattern[:-1] + r"\Z" if pattern.endswith("$") else pattern, value)

        props = self.schema["properties"]
        cases = [
            (("plan", "label"), props["plan"]["properties"]["label"]["pattern"],
             ("plan", "needs plan", " plan", "plan ", " ", "", "plan\n", "plan\rOK", "pl\x1bn",
              "plan\x9b")),
            (("team",), props["team"]["pattern"], ("rev", "a/b", "", "rev\n", "r v")),
            (("profile",), props["profile"]["pattern"], ("ollama", "", "ollama\n")),
            (("model",), props["model"]["pattern"], ("glm-5.2:cloud", "a b", "", "glm\n",
                                                      "glm\x07", "glm\x1b]0;x", "glm\x85")),
        ]
        for path, pattern, values in cases:
            for value in values:
                doc = {path[0]: {path[1]: value}} if len(path) == 2 else {path[0]: value}
                _, problems = repo_config.check(doc, os.path.join(ROOT, "personas"))
                self.assertEqual(bool(ecma(pattern, value)), not problems, (path, value))

    def test_enums_come_from_the_loader(self):
        # Oracle: tester-brief.md, Task 7, Interfaces and signed starting oracle line 1043.
        # Mutation: Add an extra schema trigger enum value without changing the loader.
        props = self.schema["properties"]
        self.assertEqual(props["trigger"]["enum"], list(repo_config.TRIGGERS))
        self.assertEqual(props["kindex"]["properties"]["store"]["enum"], list(repo_config.KINDEX_STORES))


class SchemaContractTest(unittest.TestCase):
    def test_schema_shape_is_pinned_to_signed_interface(self):
        # Oracle: Task 2 KEYS and Task 7 Interfaces schema; Decided here 3, 8, 9.
        # Mutation: remove the same property from loader AND generator, or widen both enums.
        schema = gen.build(os.path.join(ROOT, "personas"))
        expected = {"$schema": None, "version": None, "trigger": None, "team": None,
                    "profile": None, "model": None, "plan": {"label": None, "personas": None},
                    "code": {"personas": None}, "max_concurrent_personas": None,
                    "rounds": {"cap": None}, "kindex": {"store": None}}
        self.assertEqual(keys_of(schema), expected)
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["title"], "claudebox repo config")
        self.assertEqual(schema["type"], "object")
        props = schema["properties"]
        self.assertEqual(props["version"]["const"], 1)
        self.assertEqual(props["trigger"]["enum"], ["auto", "requested"])
        self.assertEqual(props["kindex"]["properties"]["store"]["enum"], ["none", "org"])
        for prop in (props["max_concurrent_personas"], props["rounds"]["properties"]["cap"]):
            self.assertEqual((prop["type"], prop["minimum"]), ("integer", 0))
        for name in ("$schema", "team", "profile", "model"):
            self.assertEqual(props[name]["type"], "string")

    def test_persona_descriptions_follow_each_supplied_tree(self):
        # Oracle: Task 7 build(persona_dir), exact persona description; decision 9.
        # Mutation: hard-code shipped personas or read code's tree for both modes.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            for mode, names in (("code", ("beta", "alpha")), ("plan", ("zeta",))):
                tree = Path(directory) / mode
                tree.mkdir()
                for name in names:
                    (tree / (name + ".md")).write_text("fixture")
                (tree / "_shared.md").write_text("fixture")
            schema = gen.build(directory)
        for mode, names in (("code", "alpha, beta"), ("plan", "zeta")):
            prop = schema["properties"][mode]["properties"]["personas"]
            self.assertEqual(prop["description"],
                f"{mode.capitalize()}-mode personas: a comma or space separated list, or all. Shipped: {names}.")
            self.assertNotIn("pattern", prop)
            self.assertNotIn("enum", prop)
