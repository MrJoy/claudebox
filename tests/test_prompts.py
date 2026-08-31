import os
import unittest

import _path  # noqa: F401

import prompts

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


BASE = {}


class DefaultsMatchTheShellTest(unittest.TestCase):
    """The fixtures were captured from the pre-port entrypoint (Task 2).

    These are the port's real acceptance criteria: a stanza that lost a space,
    a period, or a backtick during transcription changes what every review is
    asked to do, and nothing else in the suite would notice.
    """

    def test_code_review_prompt(self):
        built = prompts.render(prompts.build(BASE).review["code"], 1)
        self.assertEqual(built, fixture("prompt-code-review.txt"))

    def test_code_followup_prompt(self):
        built = prompts.render(prompts.build(BASE).followup["code"], 1)
        self.assertEqual(built, fixture("prompt-code-followup.txt"))

    def test_plan_review_prompt(self):
        built = prompts.render(prompts.build(BASE).review["plan"], 1)
        self.assertEqual(built, fixture("prompt-plan-review.txt"))

    def test_plan_followup_prompt(self):
        built = prompts.render(prompts.build(BASE).followup["plan"], 1)
        self.assertEqual(built, fixture("prompt-plan-followup.txt"))

    def test_linear_stanza_lands_on_the_default(self):
        built = prompts.render(
            prompts.build({"LINEAR_API_KEY": "lin_test"}).review["code"], 1
        )
        self.assertEqual(built, fixture("prompt-code-review-linear.txt"))


class StanzaScopeTest(unittest.TestCase):
    def test_plan_prompts_carry_no_test_stanza(self):
        p = prompts.build(BASE)
        self.assertNotIn(prompts.TEST_STANZA, p.review["plan"])
        self.assertNotIn(prompts.TEST_STANZA, p.followup["plan"])

    def test_plan_prompts_carry_the_gh_stanza(self):
        p = prompts.build(BASE)
        self.assertIn(prompts.GH_STANZA, p.review["plan"])
        self.assertIn(prompts.GH_STANZA, p.followup["plan"])

    def test_code_prompts_carry_no_plan_stanza(self):
        p = prompts.build(BASE)
        self.assertNotIn(prompts.PLAN_STANZA, p.review["code"])
        self.assertNotIn(prompts.PLAN_STANZA, p.followup["code"])

    def test_gh_stanza_is_repeated_in_every_followup(self):
        # It is repeated rather than left to the session's history because a
        # long-resumed session's earliest turns are the first thing a context
        # summary drops.
        p = prompts.build(BASE)
        self.assertIn(prompts.GH_STANZA, p.followup["code"])
        self.assertIn(prompts.GH_STANZA, p.followup["plan"])

    def test_linear_stanza_is_absent_without_a_key(self):
        self.assertEqual(prompts.linear_stanza({}), "")
        self.assertEqual(prompts.linear_stanza({"LINEAR_API_KEY": ""}), "")


class OverrideTest(unittest.TestCase):
    def test_override_reaches_claude_verbatim(self):
        p = prompts.build({"REVIEW_PROMPT": "just look at #{{PR}}"})
        self.assertEqual(p.review["code"], "just look at #{{PR}}")
        self.assertNotIn(prompts.GH_STANZA, p.review["code"])

    def test_linear_stanza_does_not_touch_an_override(self):
        p = prompts.build(
            {"REVIEW_PROMPT": "just look at #{{PR}}", "LINEAR_API_KEY": "lin_test"}
        )
        self.assertEqual(p.review["code"], "just look at #{{PR}}")

    def test_code_override_does_not_change_plan_mode(self):
        p = prompts.build({"REVIEW_PROMPT": "just look at #{{PR}}"})
        self.assertIn(prompts.PLAN_STANZA, p.review["plan"])

    def test_plan_override_does_not_change_code_mode(self):
        p = prompts.build({"PLAN_REVIEW_PROMPT": "read the plan in #{{PR}}"})
        self.assertEqual(p.review["plan"], "read the plan in #{{PR}}")
        self.assertIn(prompts.TEST_STANZA, p.review["code"])

    def test_suffix_appends_to_a_default(self):
        p = prompts.build({"REVIEW_PROMPT_SUFFIX": "Be terse."})
        self.assertTrue(p.review["code"].endswith(" Be terse."))
        self.assertIn(prompts.TEST_STANZA, p.review["code"])

    def test_suffix_appends_to_an_override(self):
        p = prompts.build(
            {"REVIEW_PROMPT": "just look at #{{PR}}", "REVIEW_PROMPT_SUFFIX": "Be terse."}
        )
        self.assertEqual(p.review["code"], "just look at #{{PR}} Be terse.")

    def test_all_four_suffixes_are_wired(self):
        p = prompts.build(
            {
                "REVIEW_PROMPT_SUFFIX": "a",
                "FOLLOWUP_PROMPT_SUFFIX": "b",
                "PLAN_REVIEW_PROMPT_SUFFIX": "c",
                "PLAN_FOLLOWUP_PROMPT_SUFFIX": "d",
            }
        )
        self.assertTrue(p.review["code"].endswith(" a"))
        self.assertTrue(p.followup["code"].endswith(" b"))
        self.assertTrue(p.review["plan"].endswith(" c"))
        self.assertTrue(p.followup["plan"].endswith(" d"))

    def test_empty_override_is_treated_as_unset(self):
        # ${REVIEW_PROMPT:-$DEFAULT_PROMPT} in the shell: an empty value falls
        # back to the default rather than sending Claude an empty prompt.
        p = prompts.build({"REVIEW_PROMPT": ""})
        self.assertIn(prompts.TEST_STANZA, p.review["code"])



class WorktreeStanzaTest(unittest.TestCase):
    def test_absent_when_no_mode_is_concurrent(self):
        p = prompts.build({})
        for table in (p.review, p.followup):
            for mode in ("code", "plan"):
                self.assertNotIn(prompts.WORKTREE_STANZA, table[mode])

    def test_present_on_both_prompts_of_a_concurrent_mode(self):
        p = prompts.build({}, shared_worktree_modes=frozenset({"code"}))
        self.assertIn(prompts.WORKTREE_STANZA, p.review["code"])
        self.assertIn(prompts.WORKTREE_STANZA, p.followup["code"])

    def test_absent_from_a_mode_that_is_not_concurrent(self):
        p = prompts.build({}, shared_worktree_modes=frozenset({"code"}))
        self.assertNotIn(prompts.WORKTREE_STANZA, p.review["plan"])
        self.assertNotIn(prompts.WORKTREE_STANZA, p.followup["plan"])

    def test_appended_to_an_operator_override_too(self):
        # THE ONE PLACE THE VERBATIM GUARANTEE IS BROKEN, deliberately. A prompt
        # that lets a persona check out a branch under concurrency corrupts the
        # other personas' reviews, so it is not an operator-level opt-out.
        p = prompts.build(
            {"REVIEW_PROMPT": "just look at #{{PR}}"},
            shared_worktree_modes=frozenset({"code"}),
        )
        self.assertTrue(p.review["code"].startswith("just look at #{{PR}}"))
        self.assertIn(prompts.WORKTREE_STANZA, p.review["code"])

    def test_appended_to_a_followup_override_too(self):
        p = prompts.build(
            {"FOLLOWUP_PROMPT": "re-check #{{PR}}"},
            shared_worktree_modes=frozenset({"code"}),
        )
        self.assertTrue(p.followup["code"].startswith("re-check #{{PR}}"))
        self.assertIn(prompts.WORKTREE_STANZA, p.followup["code"])

    def test_lands_after_the_suffix(self):
        p = prompts.build(
            {"REVIEW_PROMPT_SUFFIX": "Be terse."},
            shared_worktree_modes=frozenset({"code"}),
        )
        self.assertTrue(p.review["code"].endswith(prompts.WORKTREE_STANZA))
        self.assertIn(" Be terse. ", p.review["code"])

    def test_plan_mode_is_wired_on_its_own(self):
        p = prompts.build({}, shared_worktree_modes=frozenset({"plan"}))
        self.assertIn(prompts.WORKTREE_STANZA, p.review["plan"])
        self.assertIn(prompts.WORKTREE_STANZA, p.followup["plan"])
        self.assertNotIn(prompts.WORKTREE_STANZA, p.review["code"])

    def test_forbids_the_commands_that_take_index_lock(self):
        for verb in ("checkout", "fetch", "branch", "stash"):
            self.assertIn(verb, prompts.WORKTREE_STANZA)

    def test_names_the_read_path(self):
        self.assertIn("gh pr diff", prompts.WORKTREE_STANZA)
        self.assertIn("gh pr view", prompts.WORKTREE_STANZA)

    def test_forbids_editing_the_files_directly(self):
        # A persona told only "no git writes" can route around the constraint by
        # rewriting a file in place, which corrupts a sibling's read the same way.
        self.assertIn("edit", prompts.WORKTREE_STANZA)


class RenderTest(unittest.TestCase):
    def test_replaces_every_occurrence(self):
        self.assertEqual(prompts.render("#{{PR}} and #{{PR}}", 12), "#12 and #12")

    def test_leaves_other_text_alone(self):
        self.assertEqual(prompts.render("no token here", 12), "no token here")

    def test_pr_number_is_stringified(self):
        self.assertEqual(prompts.render("{{PR}}", 7), "7")


if __name__ == "__main__":
    unittest.main()
