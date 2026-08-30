import json
import subprocess
import unittest

import _path  # noqa: F401

import gh
from common import ConfigError


class Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def runner(*results):
    """A subprocess.run stand-in that returns each result in turn and records argv."""
    calls = []
    queue = list(results)

    def run(argv, **kwargs):
        calls.append(argv)
        return queue.pop(0) if queue else Result(0, "")

    run.calls = calls
    return run


class TruthyTest(unittest.TestCase):
    def test_accepts_the_three_spellings_any_case(self):
        for v in ("1", "true", "TRUE", "yes", "Yes"):
            self.assertTrue(gh.pr_truthy(v), v)

    def test_rejects_everything_else(self):
        for v in (None, "", "0", "false", "no", "y", "on"):
            self.assertFalse(gh.pr_truthy(v), repr(v))

    def test_does_not_strip_whitespace(self):
        # The shell version pipes through tr without trimming, so " true" was
        # falsy. Preserved rather than improved: an operator whose .env has a
        # stray space gets today's behavior, not a new one.
        self.assertFalse(gh.pr_truthy(" true"))


class ParsePrIdsTest(unittest.TestCase):
    def test_comma_separated(self):
        self.assertEqual(gh.parse_pr_ids("12,15,20"), [12, 15, 20])

    def test_whitespace_separated(self):
        self.assertEqual(gh.parse_pr_ids("12 15  20"), [12, 15, 20])

    def test_mixed_separators(self):
        self.assertEqual(gh.parse_pr_ids("12, 15,20"), [12, 15, 20])

    def test_non_numeric_is_refused(self):
        with self.assertRaises(ConfigError) as cm:
            gh.parse_pr_ids("12,abc")
        self.assertIn("non-numeric", str(cm.exception))

    def test_glob_is_refused_not_expanded(self):
        with self.assertRaises(ConfigError):
            gh.parse_pr_ids("*")

    def test_negative_is_refused(self):
        with self.assertRaises(ConfigError):
            gh.parse_pr_ids("-3")

    def test_empty_yields_nothing(self):
        self.assertEqual(gh.parse_pr_ids(""), [])


class SelectorTest(unittest.TestCase):
    def test_exactly_one_required(self):
        with self.assertRaises(ConfigError) as cm:
            gh.resolve_pr_selection({})
        self.assertIn("no PR selector set", str(cm.exception))

    def test_more_than_one_is_refused(self):
        with self.assertRaises(ConfigError) as cm:
            gh.resolve_pr_selection({"PR_ALL": "1", "PR_IDS": "3"})
        self.assertIn("multiple PR selectors", str(cm.exception))

    def test_each_selector(self):
        self.assertEqual(gh.resolve_pr_selection({"PR_ALL": "true"}), "all")
        self.assertEqual(gh.resolve_pr_selection({"PR_ASSIGNEE": "me"}), "assignee")
        self.assertEqual(gh.resolve_pr_selection({"PR_IDS": "3"}), "ids")
        self.assertEqual(gh.resolve_pr_selection({"PR_SEARCH": "is:open"}), "search")

    def test_falsy_pr_all_does_not_count_as_a_selector(self):
        with self.assertRaises(ConfigError):
            gh.resolve_pr_selection({"PR_ALL": "0"})

    def test_bad_ids_fail_at_startup_not_every_cycle(self):
        with self.assertRaises(ConfigError):
            gh.resolve_pr_selection({"PR_IDS": "12,abc"})


class PrModesTest(unittest.TestCase):
    def test_list_payload_routes_by_label(self):
        payload = [
            {"number": 12, "labels": [{"name": "plan"}]},
            {"number": 13, "labels": [{"name": "bug"}]},
        ]
        self.assertEqual(gh.pr_modes(payload, "plan"), [(12, "plan"), (13, "code")])

    def test_single_object_payload(self):
        self.assertEqual(
            gh.pr_modes({"number": 12, "labels": []}, "plan"), [(12, "code")]
        )

    def test_missing_labels_key_is_code_mode(self):
        # `.labels[]?` in the jq version read a missing key as no labels, so a PR
        # object arriving without one is code mode: the same answer an unlabeled
        # PR gets.
        self.assertEqual(gh.pr_modes({"number": 12}, "plan"), [(12, "code")])

    def test_null_number_is_dropped(self):
        # An unguarded .number once produced a candidate PR literally named
        # `null`, which the loop would then review in YOLO mode.
        self.assertEqual(gh.pr_modes([{"number": None, "labels": []}], "plan"), [])

    def test_missing_number_is_dropped(self):
        self.assertEqual(gh.pr_modes([{"labels": []}], "plan"), [])

    def test_empty_list_yields_nothing(self):
        self.assertEqual(gh.pr_modes([], "plan"), [])

    def test_custom_plan_label(self):
        payload = [{"number": 12, "labels": [{"name": "design"}]}]
        self.assertEqual(gh.pr_modes(payload, "design"), [(12, "plan")])

    def test_label_without_a_name_key_does_not_crash(self):
        payload = [{"number": 12, "labels": [{"color": "f00"}]}]
        self.assertEqual(gh.pr_modes(payload, "plan"), [(12, "code")])


class EnumerateTest(unittest.TestCase):
    ENV = {"GITHUB_REPOSITORY": "owner/repo", "PLAN_LABEL": "plan"}

    def test_all_selector_calls_gh_pr_list_once(self):
        run = runner(Result(0, json.dumps([{"number": 12, "labels": []}])))
        got = gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run)
        self.assertEqual(got, [(12, "code")])
        self.assertEqual(len(run.calls), 1)
        self.assertIn("--json", run.calls[0])
        self.assertIn("number,labels", run.calls[0])

    def test_failed_list_yields_nothing(self):
        run = runner(Result(1, ""))
        self.assertEqual(
            gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run), []
        )

    def test_unparseable_list_yields_nothing(self):
        run = runner(Result(0, "not json"))
        self.assertEqual(
            gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run), []
        )

    def test_ids_selector_looks_each_pr_up(self):
        run = runner(
            Result(0, json.dumps({"number": 12, "labels": [{"name": "plan"}]})),
            Result(0, json.dumps({"number": 13, "labels": []})),
        )
        got = gh.enumerate_candidate_prs("ids", dict(self.ENV, PR_IDS="12,13"), run=run)
        self.assertEqual(got, [(12, "plan"), (13, "code")])
        self.assertEqual(len(run.calls), 2)

    def test_ids_failed_lookup_skips_that_pr_only(self):
        # It is never guessed into code mode: a wrong-mode review posts real
        # comments on a real PR and cannot be taken back, where a skip is one
        # log line and a retry next cycle.
        run = runner(Result(1, ""), Result(0, json.dumps({"number": 13, "labels": []})))
        got = gh.enumerate_candidate_prs("ids", dict(self.ENV, PR_IDS="12,13"), run=run)
        self.assertEqual(got, [(13, "code")])

    def test_ids_empty_but_successful_lookup_skips(self):
        # A gh that exits 0 with empty stdout must not drop the PR silently.
        run = runner(Result(0, "   "))
        self.assertEqual(
            gh.enumerate_candidate_prs("ids", dict(self.ENV, PR_IDS="12"), run=run), []
        )

    def test_ids_object_without_a_number_skips(self):
        run = runner(Result(0, json.dumps({"labels": []})))
        self.assertEqual(
            gh.enumerate_candidate_prs("ids", dict(self.ENV, PR_IDS="12"), run=run), []
        )

    def test_assignee_selector_passes_the_assignee(self):
        run = runner(Result(0, "[]"))
        gh.enumerate_candidate_prs("assignee", dict(self.ENV, PR_ASSIGNEE="me"), run=run)
        self.assertIn("--assignee", run.calls[0])
        self.assertIn("me", run.calls[0])

    def test_search_selector_passes_the_query(self):
        run = runner(Result(0, "[]"))
        gh.enumerate_candidate_prs("search", dict(self.ENV, PR_SEARCH="is:open"), run=run)
        self.assertIn("--search", run.calls[0])
        self.assertIn("is:open", run.calls[0])


if __name__ == "__main__":
    unittest.main()
