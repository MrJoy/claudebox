import contextlib
import io
import json
import subprocess
import unittest

import _path  # noqa: F401

import gh
import signals
from common import ConfigError


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


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

    def test_a_digit_int_refuses_is_a_config_error(self):
        # str.isdigit() is True for '\u00b2' and int() then raises, so a bare
        # isdigit() gate turned a typo into a ValueError traceback instead of
        # the startup error the docs promise.
        self.assertTrue("\u00b2".isdigit())
        with self.assertRaises(ConfigError):
            gh.parse_pr_ids("\u00b2")

    def test_a_non_ascii_numeral_is_refused(self):
        # The shell's `*[!0-9]*` guard died on these; int() would quietly read
        # '\u0661\u0662' as 12 and review a PR nobody named.
        with self.assertRaises(ConfigError):
            gh.parse_pr_ids("\u0661\u0662")


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


def snap(number, mode, head_oid="", updated_at=""):
    return gh.PRSnapshot(number=number, mode=mode, head_oid=head_oid, updated_at=updated_at)


class PrModesTest(unittest.TestCase):
    def test_list_payload_routes_by_label(self):
        payload = [
            {"number": 12, "labels": [{"name": "plan"}]},
            {"number": 13, "labels": [{"name": "bug"}]},
        ]
        self.assertEqual(gh.pr_modes(payload, "plan"), [snap(12, "plan"), snap(13, "code")])

    def test_single_object_payload(self):
        self.assertEqual(
            gh.pr_modes({"number": 12, "labels": []}, "plan"), [snap(12, "code")]
        )

    def test_missing_labels_key_is_code_mode(self):
        # `.labels[]?` in the jq version read a missing key as no labels, so a PR
        # object arriving without one is code mode: the same answer an unlabeled
        # PR gets.
        self.assertEqual(gh.pr_modes({"number": 12}, "plan"), [snap(12, "code")])

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
        self.assertEqual(gh.pr_modes(payload, "design"), [snap(12, "plan")])

    def test_label_without_a_name_key_does_not_crash(self):
        payload = [{"number": 12, "labels": [{"color": "f00"}]}]
        self.assertEqual(gh.pr_modes(payload, "plan"), [snap(12, "code")])

    def test_head_and_update_time_ride_along(self):
        payload = [{"number": 12, "labels": [], "headRefOid": "abc123",
                     "updatedAt": "2026-08-31T12:00:00Z"}]
        self.assertEqual(
            gh.pr_modes(payload, "plan"),
            [snap(12, "code", head_oid="abc123", updated_at="2026-08-31T12:00:00Z")],
        )


class SnapshotTest(unittest.TestCase):
    ENV = {"GITHUB_REPOSITORY": "o/r", "PLAN_LABEL": "plan"}

    def test_list_payload_carries_head_and_update_time(self):
        payload = json.dumps([
            {"number": 12, "labels": [], "headRefOid": "abc123",
             "updatedAt": "2026-08-31T12:00:00Z"},
        ])
        run = runner(Result(0, payload))
        got = gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run)
        self.assertEqual(
            got,
            [gh.PRSnapshot(number=12, mode="code", head_oid="abc123",
                           updated_at="2026-08-31T12:00:00Z")],
        )

    def test_stage_one_asks_for_the_new_fields(self):
        run = runner(Result(0, "[]"))
        gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run)
        self.assertIn("number,labels,headRefOid,updatedAt", run.calls[0])

    def test_ids_selector_asks_for_the_new_fields(self):
        run = runner(Result(0, json.dumps(
            {"number": 12, "labels": [], "headRefOid": "abc123",
             "updatedAt": "2026-08-31T12:00:00Z"})))
        gh.enumerate_candidate_prs("ids", dict(self.ENV, PR_IDS="12"), run=run)
        self.assertIn("number,labels,headRefOid,updatedAt", run.calls[0])

    def test_missing_fields_become_empty_strings(self):
        # A gh that answers without them must not crash the cycle; an empty
        # head_oid simply never matches a recorded one, so the PR is reviewed.
        payload = json.dumps([{"number": 12, "labels": []}])
        run = runner(Result(0, payload))
        got = gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run)
        self.assertEqual(got[0].head_oid, "")
        self.assertEqual(got[0].updated_at, "")


class EnumerateTest(unittest.TestCase):
    ENV = {"GITHUB_REPOSITORY": "owner/repo", "PLAN_LABEL": "plan"}

    def test_all_selector_calls_gh_pr_list_once(self):
        run = runner(Result(0, json.dumps([{"number": 12, "labels": []}])))
        got = gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run)
        self.assertEqual(got, [snap(12, "code")])
        self.assertEqual(len(run.calls), 1)
        self.assertIn("--json", run.calls[0])
        self.assertIn("number,labels,headRefOid,updatedAt", run.calls[0])

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
        self.assertEqual(got, [snap(12, "plan"), snap(13, "code")])
        self.assertEqual(len(run.calls), 2)

    def test_ids_failed_lookup_skips_that_pr_only(self):
        # It is never guessed into code mode: a wrong-mode review posts real
        # comments on a real PR and cannot be taken back, where a skip is one
        # log line and a retry next cycle.
        run = runner(Result(1, ""), Result(0, json.dumps({"number": 13, "labels": []})))
        got = gh.enumerate_candidate_prs("ids", dict(self.ENV, PR_IDS="12,13"), run=run)
        self.assertEqual(got, [snap(13, "code")])

    def test_ids_empty_but_successful_lookup_skips(self):
        # A gh that exits 0 with empty stdout must not drop the PR silently.
        run = runner(Result(0, "   "))
        self.assertEqual(
            gh.enumerate_candidate_prs("ids", dict(self.ENV, PR_IDS="12"), run=run), []
        )

    def test_ids_unparseable_output_skips(self):
        # gh exits 0 but stdout is not JSON. Skipping is the only safe answer:
        # guessing code mode would post a code review on a plan PR, which cannot
        # be taken back, where a skip costs one WARN and a retry next cycle.
        run = runner(Result(0, "not json"))
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


def enumerate_log(selector, env, run):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = gh.enumerate_candidate_prs(selector, env, run=run)
    return got, buf.getvalue()


class WarningTest(unittest.TestCase):
    """The log lines are the entire observable effect of two decisions, so
    nothing else can pin them."""

    ENV = {"GITHUB_REPOSITORY": "owner/repo", "PLAN_LABEL": "plan"}

    def test_a_failed_list_says_so(self):
        # The one deliberate departure from a faithful port: the shell logged
        # the same "No candidate PRs" line whether gh failed or there simply
        # were none, so a broken token read as a quiet repo.
        _, out = enumerate_log("all", dict(self.ENV, PR_ALL="1"), runner(Result(1, "")))
        self.assertIn("`gh pr list` failed or returned nothing usable", out)
        self.assertIn("'all'", out)

    def test_a_list_that_simply_found_nothing_does_not_warn(self):
        _, out = enumerate_log("all", dict(self.ENV, PR_ALL="1"), runner(Result(0, "[]")))
        self.assertNotIn("WARN", out)

    def test_an_ids_lookup_that_yields_nothing_says_so(self):
        # Without the WARN the PR vanishes from the cycle silently, which reads
        # exactly like having been reviewed clean.
        _, out = enumerate_log("ids", dict(self.ENV, PR_IDS="12"), runner(Result(0, "   ")))
        self.assertIn("could not read labels for PR #12", out)

    def test_ghs_own_stderr_reaches_the_log(self):
        # capture_output means gh no longer writes into the container log
        # itself, so a rate limit, a 401 and a bad --search query would
        # otherwise be one indistinguishable generic WARN.
        run = runner(Result(1, "", "gh: API rate limit exceeded for user"))
        _, out = enumerate_log("all", dict(self.ENV, PR_ALL="1"), run)
        self.assertIn("API rate limit exceeded", out)

    def test_ghs_stderr_reaches_the_log_on_the_ids_arm_too(self):
        run = runner(Result(1, "", "gh: could not resolve to a PullRequest"))
        _, out = enumerate_log("ids", dict(self.ENV, PR_IDS="12"), run)
        self.assertIn("could not resolve to a PullRequest", out)

    def test_a_long_stderr_line_is_truncated(self):
        # gh's stderr is not a stream that can be assumed credential-free, the
        # same reason the usage-limit line is truncated.
        run = runner(Result(1, "", "x" * 900))
        _, out = enumerate_log("all", dict(self.ENV, PR_ALL="1"), run)
        self.assertIn("x" * 400, out)
        self.assertNotIn("x" * 401, out)


class SpawnFailureTest(unittest.TestCase):
    """A gh that cannot be spawned at all -- EAGAIN against --pids-limit,
    ENOMEM against --memory, a gh that is not on PATH. The shell's `|| true`
    made that an empty cycle; an OSError out of PID 1 restarts the container and
    loses the in-memory session map, so every pair re-posts what it posted."""

    ENV = {"GITHUB_REPOSITORY": "owner/repo", "PLAN_LABEL": "plan"}

    def _raising(self, *a, **k):
        raise OSError(11, "Resource temporarily unavailable")

    def test_the_list_arm_degrades_to_an_empty_cycle(self):
        got, out = enumerate_log("all", dict(self.ENV, PR_ALL="1"), self._raising)
        self.assertEqual(got, [])
        self.assertIn("could not run gh", out)

    def test_the_ids_arm_skips_the_pr(self):
        got, out = enumerate_log("ids", dict(self.ENV, PR_IDS="12"), self._raising)
        self.assertEqual(got, [])
        self.assertIn("could not read labels for PR #12", out)


class PRSignalTest(unittest.TestCase):
    ENV = {"GITHUB_REPOSITORY": "o/r"}
    SNAP = None  # set in setUp, because gh.PRSnapshot arrives in Task 2

    def setUp(self):
        self.SNAP = gh.PRSnapshot(
            number=12, mode="code", head_oid="abc123",
            updated_at="2026-08-31T12:00:00Z")

    def test_unsigned_conversation_comment_sets_newest_human(self):
        view = json.dumps({
            "comments": [
                {"createdAt": "2026-08-31T09:00:00Z", "body": "-claudebox (sage)"},
                {"createdAt": "2026-08-31T10:00:00Z", "body": "I disagree."},
            ],
            "reviews": [],
        })
        run = runner(Result(0, view), Result(0, "[]"))
        got = gh.pr_signal(self.SNAP, self.ENV, run=run)
        self.assertEqual(got.newest_human, "2026-08-31T10:00:00Z")
        self.assertEqual(got.head_oid, "abc123")
        self.assertEqual(got.mode, "code")

    def test_only_signed_comments_leave_newest_human_empty(self):
        view = json.dumps({
            "comments": [{"createdAt": "2026-08-31T10:00:00Z",
                          "body": "Nit here.\n\n-claudebox (red_team)"}],
            "reviews": [],
        })
        run = runner(Result(0, view), Result(0, "[]"))
        self.assertEqual(gh.pr_signal(self.SNAP, self.ENV, run=run).newest_human, "")

    def test_a_review_with_an_empty_body_counts_as_a_human_event(self):
        view = json.dumps({
            "comments": [],
            "reviews": [{"submittedAt": "2026-08-31T11:00:00Z", "body": ""}],
        })
        run = runner(Result(0, view), Result(0, "[]"))
        self.assertEqual(
            gh.pr_signal(self.SNAP, self.ENV, run=run).newest_human,
            "2026-08-31T11:00:00Z",
        )

    def test_inline_review_comments_are_read_over_the_rest_api(self):
        view = json.dumps({"comments": [], "reviews": []})
        inline = json.dumps([
            {"created_at": "2026-08-31T13:00:00Z", "body": "this line is wrong"},
        ])
        run = runner(Result(0, view), Result(0, inline))
        got = gh.pr_signal(self.SNAP, self.ENV, run=run)
        self.assertEqual(got.newest_human, "2026-08-31T13:00:00Z")
        self.assertEqual(run.calls[1], [
            "gh", "api",
            "repos/o/r/pulls/12/comments?sort=created&direction=desc&per_page=30",
        ])

    def test_the_inline_lookup_is_capped_rather_than_paginated(self):
        # One max() does not justify walking every inline comment a long-lived
        # PR ever collected, once per poll, at a 60s poll interval. The newest
        # page is enough to find the newest unsigned comment, and an
        # unbounded walk fails into a rate limit, which degrades to an empty
        # candidate list and a reviewer that silently reviews nothing.
        run = runner(Result(0, json.dumps({"comments": [], "reviews": []})),
                     Result(0, "[]"))
        gh.pr_signal(self.SNAP, self.ENV, run=run)
        self.assertNotIn("--paginate", run.calls[1])
        self.assertIn("per_page=30", run.calls[1][-1])
        self.assertIn("direction=desc", run.calls[1][-1])

    def test_stage_two_never_asks_for_status_checks(self):
        run = runner(Result(0, json.dumps({"comments": [], "reviews": []})),
                     Result(0, "[]"))
        gh.pr_signal(self.SNAP, self.ENV, run=run)
        self.assertNotIn("statusCheckRollup", " ".join(run.calls[0]))

    def test_a_failed_view_returns_none(self):
        run = runner(Result(1, "", "gh: HTTP 502"), Result(0, "[]"))
        self.assertIsNone(gh.pr_signal(self.SNAP, self.ENV, run=run))

    def test_a_failed_inline_lookup_returns_none(self):
        view = json.dumps({"comments": [], "reviews": []})
        run = runner(Result(0, view), Result(1, "", "gh: HTTP 502"))
        self.assertIsNone(gh.pr_signal(self.SNAP, self.ENV, run=run))

    def test_a_gh_that_cannot_be_spawned_returns_none(self):
        def run(argv, **kwargs):
            raise OSError("no such file")
        self.assertIsNone(gh.pr_signal(self.SNAP, self.ENV, run=run))


if __name__ == "__main__":
    unittest.main()
