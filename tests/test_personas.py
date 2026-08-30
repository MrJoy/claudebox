import os
import shutil
import tempfile
import unittest

import _path  # noqa: F401

import personas
from common import ConfigError

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SHIPPED = os.path.join(REPO, "personas")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TreeBuilder:
    """Builds a PERSONA_DIR on disk. Used instead of mocking the filesystem
    because every failure this module exists to catch is a filesystem shape."""

    def __init__(self):
        self.root = tempfile.mkdtemp()

    def tree(self, mode, shared="Say nothing if you have nothing. You are {{PERSONA}}."):
        if shared is not None:
            write(os.path.join(self.root, mode, "_shared.md"), shared)
        return self

    def persona(self, mode, pid, label="Red Team", body="Attack the change."):
        fm = f"---\nlabel: {label}\nsuccess: Finds real holes.\n---\n"
        write(os.path.join(self.root, mode, f"{pid}.md"), fm + body)
        return self

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class ShippedPersonasTest(unittest.TestCase):
    def test_code_default_resolves_to_four(self):
        got = personas.resolve("code", SHIPPED, {})
        self.assertEqual([p.id for p in got], ["red_team", "adversarial", "sme", "sage"])

    def test_plan_default_resolves_to_six(self):
        got = personas.resolve("plan", SHIPPED, {})
        self.assertEqual(len(got), 6)

    def test_prompt_is_body_then_shared_contract(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "red_team"})
        self.assertEqual(len(got), 1)
        self.assertGreater(len(got[0].prompt), 100)
        self.assertNotIn("{{PERSONA}}", got[0].prompt)
        self.assertIn(got[0].label, got[0].prompt)

    def test_frontmatter_is_not_in_the_prompt(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "red_team"})
        self.assertNotIn("success:", got[0].prompt)

    def test_all_selects_every_persona_in_the_tree(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "all"})
        self.assertEqual(len(got), 6)

    def test_all_is_case_insensitive(self):
        self.assertEqual(
            len(personas.resolve("code", SHIPPED, {"PERSONAS": "ALL"})), 6
        )

    def test_selector_order_is_preserved(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "sage,red_team"})
        self.assertEqual([p.id for p in got], ["sage", "red_team"])

    def test_whitespace_separated_selector(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "sage red_team"})
        self.assertEqual([p.id for p in got], ["sage", "red_team"])

    def test_plan_selector_var_is_separate(self):
        got = personas.resolve("plan", SHIPPED, {"PERSONAS": "sage"})
        self.assertEqual(len(got), 6)


class RefusalTest(unittest.TestCase):
    def setUp(self):
        self.b = TreeBuilder()
        self.addCleanup(self.b.cleanup)

    def test_missing_persona_dir(self):
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", "/nonexistent/personas", {})
        self.assertIn("is not a directory", str(cm.exception))

    def test_flat_layout_names_the_subdirectories(self):
        write(os.path.join(self.b.root, "_shared.md"), "contract")
        write(os.path.join(self.b.root, "red_team.md"), "---\nlabel: X\n---\nbody")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {})
        self.assertIn("code/", str(cm.exception))
        self.assertIn("plan/", str(cm.exception))

    def test_missing_shared_contract(self):
        self.b.tree("code", shared=None).persona("code", "red_team")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {})
        self.assertIn("_shared.md", str(cm.exception))

    def test_empty_body_is_refused(self):
        # Judged on the persona's OWN body, before the shared contract is
        # appended: a body-plus-contract string is never empty, so judging the
        # composed prompt lets a frontmatter-only file review a PR as an
        # identity-free reviewer signing a label it has no angle behind.
        self.b.tree("code").persona("code", "hollow", body="")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {"PERSONAS": "hollow"})
        self.assertIn("empty prompt body", str(cm.exception))

    def test_whitespace_only_body_is_refused(self):
        self.b.tree("code").persona("code", "hollow", body="  \n\n\t\n")
        with self.assertRaises(ConfigError):
            personas.resolve("code", self.b.root, {"PERSONAS": "hollow"})

    def test_missing_label_is_refused(self):
        self.b.tree("code")
        write(
            os.path.join(self.b.root, "code", "nolabel.md"),
            "---\nsuccess: nothing\n---\nbody here",
        )
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {"PERSONAS": "nolabel"})
        self.assertIn("no label", str(cm.exception))

    def test_label_with_a_slash_is_refused(self):
        self.b.tree("code").persona("code", "bad", label="Red/Team")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {"PERSONAS": "bad"})
        self.assertIn("unexpected characters", str(cm.exception))

    def test_unknown_persona_is_refused(self):
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": "nosuch"})
        self.assertIn("unknown persona", str(cm.exception))

    def test_reserved_persona_is_refused(self):
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": "aggregate"})
        self.assertIn("reserved", str(cm.exception))

    def test_duplicate_persona_is_refused(self):
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": "sage,sage"})
        self.assertIn("twice", str(cm.exception))

    def test_empty_selector_is_refused(self):
        # PERSONAS set but naming nothing. The shell used ${PERSONAS-$def}, so an
        # explicitly-empty value does NOT fall back to the default; it is an error.
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": ""})
        self.assertIn("names no persona", str(cm.exception))

    def test_underscore_files_are_not_personas(self):
        with self.assertRaises(ConfigError):
            personas.resolve("code", SHIPPED, {"PERSONAS": "_shared"})

    def test_glob_is_a_name_not_a_pattern(self):
        # The bash version word-split AND glob-expanded here, so a PERSONAS of
        # '*' resolved against the current directory. Python cannot do that, and
        # this test pins the property the bash suite already asserted.
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": "*"})
        self.assertIn("unknown persona", str(cm.exception))

    def test_empty_tree_is_refused(self):
        self.b.tree("code")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {})
        self.assertIn("no persona definitions found", str(cm.exception))

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ConfigError):
            personas.resolve("aesthetic", SHIPPED, {})


if __name__ == "__main__":
    unittest.main()
