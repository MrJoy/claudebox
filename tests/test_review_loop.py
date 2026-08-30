import contextlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest

import _path  # noqa: F401

import review_loop
from common import ConfigError, Pair
from passes import PassResult


def ok(sid="S1"):
    return PassResult(rc=0, session_id=sid, limited=False, limit_line="")


def limited(sid="S1"):
    return PassResult(rc=1, session_id=sid, limited=True, limit_line="429 rate limit")


def failed():
    return PassResult(rc=1, session_id=None, limited=False, limit_line="")


class FakeSupervisor(review_loop.Supervisor):
    """Supervisor with run_pass replaced by a scripted sequence, so the cycle's
    control flow can be tested without a subprocess."""

    def __init__(self, results, **kwargs):
        super().__init__(**kwargs)
        self._results = list(results)
        self.attempted = []

    def _run_one(self, pair, prompt, session_id):
        self.attempted.append(pair)
        return self._results.pop(0) if self._results else ok()


def supervisor(results, **kwargs):
    defaults = dict(
        personas={"code": ["red_team", "sage"], "plan": ["red_team"]},
        persona_prompts={
            ("code", "red_team"): "rt", ("code", "sage"): "sg", ("plan", "red_team"): "rt",
        },
        review_prompts={"code": "review #{{PR}}", "plan": "plan #{{PR}}"},
        followup_prompts={"code": "recheck #{{PR}}", "plan": "replan #{{PR}}"},
        model="m",
        mcp_args=[],
        cwd=".",
        max_passes_per_session=0,
        max_concurrent=1,
    )
    defaults.update(kwargs)
    return FakeSupervisor(results, **defaults)


class BuildPairsTest(unittest.TestCase):
    def test_one_pair_per_pr_per_persona_of_that_prs_mode(self):
        s = supervisor([])
        got = s.build_pairs([(12, "code"), (13, "plan")])
        self.assertEqual(
            got,
            [
                Pair(12, "code", "red_team"),
                Pair(12, "code", "sage"),
                Pair(13, "plan", "red_team"),
            ],
        )

    def test_no_candidates_yields_no_pairs(self):
        self.assertEqual(supervisor([]).build_pairs([]), [])


class SessionTest(unittest.TestCase):
    def test_first_pass_starts_a_new_session_and_records_it(self):
        s = supervisor([ok("S9")])
        s.run_cycle([Pair(12, "code", "red_team")])
        self.assertEqual(s.sessions[Pair(12, "code", "red_team")], "S9")
        self.assertEqual(s.passes_done[Pair(12, "code", "red_team")], 1)

    def test_second_pass_resumes(self):
        s = supervisor([ok("S9"), ok("S9")])
        pair = Pair(12, "code", "red_team")
        s.run_cycle([pair])
        s.run_cycle([pair])
        self.assertEqual(s.passes_done[pair], 2)

    def test_the_same_pr_in_two_modes_holds_two_sessions(self):
        s = supervisor([ok("Sc"), ok("Sp")])
        s.run_cycle([Pair(12, "code", "red_team"), Pair(12, "plan", "red_team")])
        self.assertEqual(s.sessions[Pair(12, "code", "red_team")], "Sc")
        self.assertEqual(s.sessions[Pair(12, "plan", "red_team")], "Sp")

    def test_a_non_limit_failure_drops_the_session(self):
        pair = Pair(12, "code", "red_team")
        s = supervisor([ok("S9"), failed()])
        s.run_cycle([pair])
        s.run_cycle([pair])
        self.assertNotIn(pair, s.sessions)
        self.assertEqual(s.passes_done.get(pair, 0), 0)

    def test_a_limit_keeps_the_session(self):
        pair = Pair(12, "code", "red_team")
        s = supervisor([limited("S9")])
        s.run_cycle([pair])
        self.assertEqual(s.sessions[pair], "S9")

    def test_max_passes_rotates_the_session(self):
        pair = Pair(12, "code", "red_team")
        s = supervisor([ok("S9"), ok("S9")], max_passes_per_session=2)
        s.run_cycle([pair])
        self.assertIn(pair, s.sessions)
        s.run_cycle([pair])
        self.assertNotIn(pair, s.sessions)
        self.assertEqual(s.passes_done[pair], 0)

    def test_zero_max_passes_never_rotates(self):
        pair = Pair(12, "code", "red_team")
        s = supervisor([ok("S9")] * 5, max_passes_per_session=0)
        for _ in range(5):
            s.run_cycle([pair])
        self.assertIn(pair, s.sessions)


class CutAndResumeTest(unittest.TestCase):
    PAIRS = [
        Pair(12, "code", "red_team"),
        Pair(12, "code", "sage"),
        Pair(13, "code", "red_team"),
        Pair(13, "code", "sage"),
    ]

    def test_a_limit_ends_the_cycle_and_sets_the_resume_point(self):
        s = supervisor([ok(), limited()])
        was_limited = s.run_cycle(self.PAIRS)
        self.assertTrue(was_limited)
        self.assertEqual(len(s.attempted), 2)
        self.assertEqual(s.resume_at, self.PAIRS[2])

    def test_the_skipped_pairs_are_named_in_the_log(self):
        # An operator has to be able to read a stall, not infer one from
        # comments that never arrive.
        s = supervisor([ok(), limited()])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(self.PAIRS)
        line = [l for l in buf.getvalue().splitlines() if "Not reviewed" in l]
        self.assertEqual(len(line), 1)
        for pair in self.PAIRS[2:]:
            self.assertIn(str(pair), line[0])

    def test_the_next_cycle_starts_where_the_last_one_stopped(self):
        s = supervisor([ok(), limited(), ok(), ok(), ok(), ok()])
        s.run_cycle(self.PAIRS)
        s.attempted.clear()
        s.run_cycle(self.PAIRS)
        self.assertEqual(s.attempted[0], self.PAIRS[2])

    def test_the_resumed_cycle_wraps_around(self):
        s = supervisor([ok(), limited()] + [ok()] * 4)
        s.run_cycle(self.PAIRS)
        s.attempted.clear()
        s.run_cycle(self.PAIRS)
        self.assertEqual(
            s.attempted, [self.PAIRS[2], self.PAIRS[3], self.PAIRS[0], self.PAIRS[1]]
        )

    def test_a_complete_cycle_clears_the_resume_point(self):
        s = supervisor([ok()] * 4)
        s.run_cycle(self.PAIRS)
        self.assertIsNone(s.resume_at)

    def test_a_stale_resume_point_falls_back_to_the_head(self):
        s = supervisor([ok(), limited()] + [ok()] * 2)
        s.run_cycle(self.PAIRS)
        s.attempted.clear()
        shorter = self.PAIRS[:2]
        s.run_cycle(shorter)
        self.assertEqual(s.attempted[0], shorter[0])

    def test_an_empty_candidate_list_keeps_the_resume_point(self):
        # Enumeration failures are swallowed, so "no candidate PRs" can mean gh
        # had a bad minute. That must not silently send the next cycle back to
        # the head.
        s = supervisor([ok(), limited()])
        s.run_cycle(self.PAIRS)
        s.run_cycle([])
        self.assertEqual(s.resume_at, self.PAIRS[2])


class ConsecutiveFailureTest(unittest.TestCase):
    PAIRS = [Pair(n, "code", "red_team") for n in (12, 13, 14, 15, 16)]

    def test_three_non_limit_failures_abandon_the_cycle(self):
        s = supervisor([failed(), failed(), failed()])
        was_limited = s.run_cycle(self.PAIRS)
        self.assertFalse(was_limited)
        self.assertEqual(len(s.attempted), 3)
        self.assertEqual(s.resume_at, self.PAIRS[3])

    def test_a_success_resets_the_counter(self):
        s = supervisor([failed(), failed(), ok(), failed(), failed()])
        s.run_cycle(self.PAIRS)
        self.assertEqual(len(s.attempted), 5)

    def test_limits_do_not_feed_the_non_limit_counter(self):
        s = supervisor([failed(), failed(), limited()])
        s.run_cycle(self.PAIRS)
        self.assertEqual(len(s.attempted), 3)
        self.assertEqual(s.resume_at, self.PAIRS[3])


class PromptChoiceTest(unittest.TestCase):
    def test_new_session_gets_the_review_prompt_rendered(self):
        seen = []

        class Recorder(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                seen.append((prompt, session_id))
                return ok("S1")

        s = supervisor([])
        s.__class__ = Recorder
        s.run_cycle([Pair(12, "code", "red_team")])
        self.assertEqual(seen[0], ("review #12", None))

    def test_resumed_session_gets_the_followup_prompt(self):
        seen = []

        class Recorder(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                seen.append((prompt, session_id))
                return ok("S1")

        s = supervisor([])
        s.__class__ = Recorder
        pair = Pair(12, "code", "red_team")
        s.run_cycle([pair])
        s.run_cycle([pair])
        self.assertEqual(seen[1], ("recheck #12", "S1"))

    def test_plan_mode_uses_the_plan_prompts(self):
        seen = []

        class Recorder(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                seen.append(prompt)
                return ok("S1")

        s = supervisor([])
        s.__class__ = Recorder
        s.run_cycle([Pair(13, "plan", "red_team")])
        self.assertEqual(seen[0], "plan #13")


class PromptTokenWarningTest(unittest.TestCase):
    """Ports entrypoint.sh:567-572. An override that drops {{PR}} makes every
    review ask about an unnamed PR, which reads in the log as the reviewer
    ignoring instructions rather than as a config mistake."""

    def test_warns_for_a_prompt_with_no_token(self):
        got = review_loop.prompt_token_warnings(
            review={"code": "review it", "plan": "plan #{{PR}}"},
            followup={"code": "recheck #{{PR}}", "plan": "replan #{{PR}}"},
        )
        self.assertEqual(len(got), 1)
        self.assertIn("code review prompt", got[0])

    def test_warns_separately_for_review_and_followup(self):
        got = review_loop.prompt_token_warnings(
            review={"code": "a", "plan": "b"}, followup={"code": "c", "plan": "d"}
        )
        self.assertEqual(len(got), 4)

    def test_silent_when_every_prompt_names_the_pr(self):
        got = review_loop.prompt_token_warnings(
            review={"code": "#{{PR}}", "plan": "#{{PR}}"},
            followup={"code": "#{{PR}}", "plan": "#{{PR}}"},
        )
        self.assertEqual(got, [])


class MaxCyclesTest(unittest.TestCase):
    def test_unset_means_forever(self):
        self.assertIsNone(review_loop.parse_max_cycles({}))

    def test_zero_means_forever(self):
        self.assertIsNone(review_loop.parse_max_cycles({"MAX_CYCLES": "0"}))

    def test_a_positive_value_is_taken(self):
        self.assertEqual(review_loop.parse_max_cycles({"MAX_CYCLES": "2"}), 2)

    def test_a_non_integer_is_refused(self):
        from common import ConfigError

        with self.assertRaises(ConfigError):
            review_loop.parse_max_cycles({"MAX_CYCLES": "soon"})

    def test_a_negative_value_is_refused(self):
        from common import ConfigError

        with self.assertRaises(ConfigError):
            review_loop.parse_max_cycles({"MAX_CYCLES": "-1"})

    def test_a_digit_int_refuses_is_a_config_error(self):
        """The gap str.isdigit() left open.

        '\u00b2'.isdigit() is True and int('\u00b2') raises, so the old gate let
        it through to an uncaught ValueError traceback rather than the
        ERROR: line every other bad value gets.
        """
        from common import ConfigError

        self.assertTrue("\u00b2".isdigit())
        with self.assertRaises(ConfigError):
            review_loop.parse_max_cycles({"MAX_CYCLES": "\u00b2"})

    def test_a_non_ascii_numeral_int_accepts_is_taken(self):
        """Documented so the behavior is chosen rather than stumbled into.

        int() accepts other decimal digit sets, so '\u0661\u0662' is 12. That is a
        non-negative integer by the rule the error message states, and
        rejecting it would need a stricter gate than int() for no gain.
        """
        self.assertEqual(review_loop.parse_max_cycles({"MAX_CYCLES": "\u0661\u0662"}), 12)


class CheckLitellmTest(unittest.TestCase):
    """The PID-1 zombie hazard. Python is PID 1 after entrypoint.sh execs into
    it, so a dead translator stays a zombie and os.kill(pid, 0) still succeeds
    against it. Without the non-blocking reap the liveness check would pass
    against a dead translator forever, and every pass would fail on connection
    refused instead of one loud error."""

    def _exited_child(self):
        """A child that has exited but has not been waited on: a zombie.

        The pipe is the synchronization. It has no reader but this process and
        no writer but the child, so EOF means the child is gone -- and reaching
        it costs no wait(), which would reap the very state under test.
        """
        proc = subprocess.Popen([sys.executable, "-c", ""], stdout=subprocess.PIPE)
        self.assertEqual(proc.stdout.read(), b"")
        proc.stdout.close()
        # check_litellm reaps this pid, so Popen never learns the child is gone
        # and __del__ warns that it is "still running". Record the status we
        # already know, or the suite's output stops being pristine.
        self.addCleanup(self._mark_reaped, proc)
        return proc

    @staticmethod
    def _mark_reaped(proc):
        if proc.returncode is None:
            proc.returncode = 0

    def _check_quietly(self, env):
        """The failure path logs a tail and dies; neither belongs in test output."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            review_loop.check_litellm(env)

    def test_a_dead_child_is_fatal_even_though_its_pid_still_answers(self):
        proc = self._exited_child()
        os.kill(proc.pid, 0)  # the premise: a zombie answers signal 0
        with self.assertRaises(SystemExit):
            self._check_quietly({"LITELLM_PID": str(proc.pid), "HOME": "/nonexistent"})

    def test_a_dead_normalizer_is_fatal_too(self):
        proc = self._exited_child()
        with self.assertRaises(SystemExit):
            self._check_quietly({"SHIM_PID": str(proc.pid), "HOME": "/nonexistent"})

    def test_a_live_child_passes(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            review_loop.check_litellm({"LITELLM_PID": str(proc.pid), "HOME": "/nonexistent"})
        finally:
            proc.kill()
            proc.wait()

    def test_an_unset_or_empty_pid_is_a_no_op(self):
        # Every provider but workersai leaves these unset.
        review_loop.check_litellm({})
        review_loop.check_litellm({"LITELLM_PID": "", "SHIM_PID": ""})

    def test_a_garbled_pid_is_fatal_like_the_shell(self):
        # entrypoint.sh's `kill -0 "$SHIM_PID"` rejects junk and dies. Skipping
        # the check instead would leave a dead translator undetected.
        with self.assertRaises(SystemExit):
            self._check_quietly({"SHIM_PID": "not-a-pid", "HOME": "/nonexistent"})


class RequiredEnvTest(unittest.TestCase):
    """entrypoint.sh exports WORK_REPO and REVIEW_MODEL before it execs us.

    If it stops doing so, the loop must say which var is missing. A bare
    KeyError traceback under `--restart unless-stopped` is a silent crash loop.
    """

    def test_missing_var_is_a_config_error_naming_it(self):
        with self.assertRaises(ConfigError) as caught:
            review_loop._required({}, "WORK_REPO")
        self.assertIn("WORK_REPO", str(caught.exception))

    def test_empty_is_treated_as_missing(self):
        with self.assertRaises(ConfigError):
            review_loop._required({"REVIEW_MODEL": ""}, "REVIEW_MODEL")

    def test_a_set_var_comes_back(self):
        self.assertEqual(
            review_loop._required({"REVIEW_MODEL": "glm-5.2:cloud"}, "REVIEW_MODEL"),
            "glm-5.2:cloud",
        )


class TunableDefaultsTest(unittest.TestCase):
    """The three intervals default to what entrypoint.sh defaulted to.

    A regression to a 30-second interval would be silent and would burn the
    quota the backoff exists to protect.
    """

    def test_review_interval(self):
        self.assertEqual(review_loop._positive_int({}, "REVIEW_INTERVAL_SECONDS", 300), 300)

    def test_limit_backoff(self):
        self.assertEqual(review_loop._positive_int({}, "LIMIT_BACKOFF_SECONDS", 1800), 1800)

    def test_max_passes_per_session_defaults_to_no_rotation(self):
        self.assertEqual(review_loop._positive_int({}, "MAX_PASSES_PER_SESSION", 0), 0)

    def test_an_explicit_value_wins(self):
        self.assertEqual(
            review_loop._positive_int({"REVIEW_INTERVAL_SECONDS": "60"}, "REVIEW_INTERVAL_SECONDS", 300),
            60,
        )

    def test_a_digit_int_refuses_is_a_config_error(self):
        # The same gap parse_max_cycles closed. main catches ConfigError and
        # turns it into an ERROR: line; a ValueError escaping it is a traceback
        # out of PID 1, which under --restart unless-stopped is a silent crash
        # loop.
        with self.assertRaises(ConfigError):
            review_loop._positive_int({"REVIEW_INTERVAL_SECONDS": "\u00b2"}, "REVIEW_INTERVAL_SECONDS", 300)

    def test_a_negative_value_is_refused(self):
        with self.assertRaises(ConfigError):
            review_loop._positive_int({"REVIEW_INTERVAL_SECONDS": "-1"}, "REVIEW_INTERVAL_SECONDS", 300)


class SpawnFailureTest(unittest.TestCase):
    """A pass that never gets off the ground is an ordinary failed pass.

    subprocess.Popen raises OSError for EAGAIN against --pids-limit, ENOMEM
    against --memory, and a claude that is not on PATH. The shell read
    PIPESTATUS[0] and saw a non-zero rc for all three. Letting it out of the
    cycle exits PID 1, and the session map lives only in memory, so the restart
    makes every pair re-post findings it already posted.
    """

    def _raising_supervisor(self, exc):
        s = review_loop.Supervisor(
            personas={"code": ["red_team"], "plan": []},
            persona_prompts={("code", "red_team"): "rt"},
            review_prompts={"code": "review #{{PR}}"},
            followup_prompts={"code": "recheck #{{PR}}"},
            model="m",
            mcp_args=[],
            cwd=".",
            max_passes_per_session=0,
        )

        def boom(**kwargs):
            raise exc

        self._original = review_loop.passes.run_pass
        review_loop.passes.run_pass = boom
        self.addCleanup(setattr, review_loop.passes, "run_pass", self._original)
        return s

    def test_an_oserror_from_the_spawn_is_an_ordinary_failure(self):
        s = self._raising_supervisor(OSError(11, "Resource temporarily unavailable"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            limited_flag = s.run_cycle([Pair(12, "code", "red_team")])
        self.assertFalse(limited_flag)
        self.assertIn("could not run claude", buf.getvalue())
        self.assertIn("starting a fresh session for it next cycle", buf.getvalue())

    def test_a_missing_claude_binary_does_not_take_the_loop_down(self):
        s = self._raising_supervisor(FileNotFoundError(2, "No such file or directory"))
        pairs = [Pair(n, "code", "red_team") for n in (12, 13)]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(pairs)
        # Both pairs were attempted rather than the first one killing the cycle,
        # and neither kept a session.
        self.assertEqual(s.sessions, {})
        self.assertIn("#13", buf.getvalue())

    def test_a_spawn_failure_inside_a_group_is_caught_per_pass(self):
        # The same guard has to sit inside each worker, not around the pool: one
        # thread's EAGAIN escaping would take the pool and PID 1 with it.
        s = self._raising_supervisor(OSError(11, "Resource temporarily unavailable"))
        s.personas["code"] = ["red_team", "sage"]
        s.persona_prompts[("code", "sage")] = "sg"
        s.max_concurrent = 0
        group = s.build_groups([(12, "code")])[0]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            results = s.run_group(group, list(group.pairs))
        self.assertEqual([r.rc for r in results.values()], [1, 1])
        self.assertFalse(any(r.limited for r in results.values()))
        self.assertIn("could not run claude", buf.getvalue())
        # _run_one caught it, so _dispatch's catch-all never saw it. That
        # distinction is what keeps a spawn failure classified as an ordinary
        # failed pass rather than as a supervisor bug.
        self.assertNotIn("pass raised", buf.getvalue())

    def test_repeated_spawn_failures_hit_the_three_strikes_rule(self):
        s = self._raising_supervisor(OSError(11, "Resource temporarily unavailable"))
        pairs = [Pair(n, "code", "red_team") for n in (12, 13, 14, 15)]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(pairs)
        self.assertIn("3 passes in a row failed", buf.getvalue())
        self.assertEqual(s.resume_at, Pair(15, "code", "red_team"))


class GroupTest(unittest.TestCase):
    def test_one_group_per_pr_holding_that_modes_personas(self):
        s = supervisor([])
        groups = s.build_groups([(12, "code"), (13, "plan")])
        self.assertEqual([g.pr for g in groups], [12, 13])
        self.assertEqual(
            groups[0].pairs,
            (Pair(12, "code", "red_team"), Pair(12, "code", "sage")),
        )
        self.assertEqual(groups[1].pairs, (Pair(13, "plan", "red_team"),))

    def test_no_candidates_yields_no_groups(self):
        self.assertEqual(supervisor([]).build_groups([]), [])


class ConcurrencyTest(unittest.TestCase):
    def test_cap_of_one_still_uses_the_pool(self):
        # The whole point: no separate sequential branch exists to drift from
        # the parallel one, so every inherited ordinal assertion still holds.
        s = supervisor([ok("A"), ok("B")], max_concurrent=1)
        group = s.build_groups([(12, "code")])[0]
        results = s.run_group(group, list(group.pairs))
        self.assertEqual(set(results), set(group.pairs))

    def test_cap_of_one_is_a_one_worker_pool(self):
        # Not just "the results came back in order": with one worker no two
        # passes are ever in flight together, which is what makes the inherited
        # ordinal assertions safe.
        s = supervisor([], max_concurrent=1)
        group = s.build_groups([(12, "code")])[0]
        self.assertEqual(s.workers_for(group, list(group.pairs)), 1)

    def test_cap_of_one_runs_personas_in_list_order(self):
        order = []

        class Recorder(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                order.append(pair.persona)
                return ok("S1")

        s = supervisor([], max_concurrent=1)
        s.__class__ = Recorder
        group = s.build_groups([(12, "code")])[0]
        s.run_group(group, list(group.pairs))
        self.assertEqual(order, ["red_team", "sage"])

    def test_unlimited_runs_the_group_concurrently(self):
        # A barrier inside the fake pass: it can only be crossed if both passes
        # are genuinely in flight at once. One worker breaks it on the timeout.
        barrier = threading.Barrier(2, timeout=2)

        class Concurrent(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                barrier.wait()
                return ok("S1")

        s = supervisor([], max_concurrent=0)
        s.__class__ = Concurrent
        group = s.build_groups([(12, "code")])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            results = s.run_group(group, list(group.pairs))
        # rc, not len(results): a broken barrier raises inside the worker and
        # _dispatch turns that into a failed pass, so a count of two is what a
        # serialized pool returns too.
        self.assertEqual([r.rc for r in results.values()], [0, 0])

    def test_a_cap_above_one_overlaps_up_to_the_cap(self):
        # Four personas, cap of two: the barrier of two is crossed twice, so
        # the cap is a floor on concurrency as well as a ceiling.
        barrier = threading.Barrier(2, timeout=2)

        class Concurrent(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                barrier.wait()
                return ok("S1")

        s = supervisor(
            [], max_concurrent=2,
            personas={"code": ["red_team", "sage", "sme", "adversarial"]},
            persona_prompts={("code", p): p for p in
                             ("red_team", "sage", "sme", "adversarial")},
        )
        s.__class__ = Concurrent
        group = s.build_groups([(12, "code")])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            results = s.run_group(group, list(group.pairs))
        self.assertEqual([r.rc for r in results.values()], [0, 0, 0, 0])

    def test_a_raising_pass_does_not_lose_its_siblings_results(self):
        class Exploding(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                if pair.persona == "red_team":
                    raise RuntimeError("boom")
                return ok("S1")

        s = supervisor([], max_concurrent=0)
        s.__class__ = Exploding
        group = s.build_groups([(12, "code")])[0]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            results = s.run_group(group, list(group.pairs))
        self.assertEqual(results[Pair(12, "code", "sage")].rc, 0)
        # An exception is a non-limit failure, so its pair's session is dropped
        # like any other. It must not take the group with it.
        self.assertNotEqual(results[Pair(12, "code", "red_team")].rc, 0)
        self.assertFalse(results[Pair(12, "code", "red_team")].limited)
        self.assertIn("pass raised RuntimeError", buf.getvalue())

    def test_to_run_narrows_which_personas_execute(self):
        s = supervisor([ok("A")], max_concurrent=0)
        group = s.build_groups([(12, "code")])[0]
        results = s.run_group(group, [Pair(12, "code", "sage")])
        self.assertEqual(list(results), [Pair(12, "code", "sage")])

    def test_an_empty_to_run_runs_nothing(self):
        # A pool with max_workers=0 is a ValueError, so the empty case has to be
        # answered before the pool is built.
        s = supervisor([], max_concurrent=0)
        group = s.build_groups([(12, "code")])[0]
        self.assertEqual(s.run_group(group, []), {})

    def test_a_resumed_pair_gets_the_followup_prompt(self):
        seen = {}

        class Recorder(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                seen[pair] = (prompt, session_id)
                return ok("S1")

        s = supervisor([], max_concurrent=0)
        s.__class__ = Recorder
        s.sessions[Pair(12, "code", "sage")] = "S9"
        group = s.build_groups([(12, "code")])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_group(group, list(group.pairs))
        self.assertEqual(seen[Pair(12, "code", "sage")], ("recheck #12", "S9"))
        self.assertEqual(seen[Pair(12, "code", "red_team")], ("review #12", None))


class ParseMaxConcurrentTest(unittest.TestCase):
    def test_unset_is_unlimited(self):
        self.assertEqual(review_loop.parse_max_concurrent({}), 0)

    def test_zero_is_unlimited(self):
        self.assertEqual(review_loop.parse_max_concurrent({"MAX_CONCURRENT_PASSES": "0"}), 0)

    def test_a_positive_value_is_taken(self):
        self.assertEqual(review_loop.parse_max_concurrent({"MAX_CONCURRENT_PASSES": "3"}), 3)

    def test_a_non_integer_is_refused(self):
        from common import ConfigError

        with self.assertRaises(ConfigError):
            review_loop.parse_max_concurrent({"MAX_CONCURRENT_PASSES": "lots"})

    def test_a_negative_value_is_refused(self):
        with self.assertRaises(ConfigError):
            review_loop.parse_max_concurrent({"MAX_CONCURRENT_PASSES": "-1"})


REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SHIPPED_PERSONAS = os.path.join(REPO_ROOT, "personas")


def preflight_env(**overrides):
    """Only what entrypoint.sh already has in hand before auth and the clone."""
    env = {
        "GITHUB_REPOSITORY": "owner/repo",
        "PR_IDS": "12",
        "PERSONA_DIR": SHIPPED_PERSONAS,
    }
    env.update(overrides)
    return env


class PreflightTest(unittest.TestCase):
    """entrypoint.sh runs this through --check before auth, the working clone
    and the translator's blocking startup, so it must read nothing the shell
    exports after that point."""

    def test_it_resolves_the_selector_and_both_modes(self):
        selector, resolved = review_loop.preflight(preflight_env())
        self.assertEqual(selector, "ids")
        self.assertEqual(sorted(resolved), ["code", "plan"])
        self.assertEqual([p.id for p in resolved["code"]], ["red_team", "adversarial", "sme", "sage"])

    def test_a_bad_persona_name_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            review_loop.preflight(preflight_env(PERSONAS="saeg"))

    def test_a_broken_plan_set_is_caught_even_with_no_plan_pr(self):
        with self.assertRaises(ConfigError):
            review_loop.preflight(preflight_env(PLAN_PERSONAS="saeg"))

    def test_no_selector_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            review_loop.preflight(preflight_env(PR_IDS=""))


class CheckModeTest(unittest.TestCase):
    def _run(self, args, env):
        original = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                rc = review_loop.main(args)
            except SystemExit as exc:
                rc = exc.code
        return rc, buf.getvalue(), err.getvalue()

    def test_check_exits_zero_without_the_vars_exported_after_it(self):
        # WORK_REPO, REVIEW_MODEL and MCP_CONFIG_FILE are all exported below the
        # point --check runs at, so needing any of them would make the
        # pre-flight refuse a configuration that is in fact fine.
        rc, out, err = self._run(["--check"], preflight_env())
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_check_refuses_a_bad_persona(self):
        rc, out, err = self._run(["--check"], preflight_env(PERSONAS="saeg"))
        self.assertEqual(rc, 1)
        self.assertIn("unknown persona 'saeg'", err)

    def test_check_refuses_a_missing_selector(self):
        rc, out, err = self._run(["--check"], preflight_env(PR_IDS=""))
        self.assertEqual(rc, 1)
        self.assertIn("no PR selector set", err)

    def test_an_unknown_flag_is_refused(self):
        rc, out, err = self._run(["--dry-run"], preflight_env())
        self.assertEqual(rc, 1)
        self.assertIn("--dry-run", err)


class GitFetchFailureTest(unittest.TestCase):
    """The shell wrote `git fetch ... || log WARN`, which covers a git that
    cannot be spawned as well as one that exits non-zero."""

    def _one_cycle(self, run):
        env = preflight_env(
            WORK_REPO=".", REVIEW_MODEL="m", MAX_CYCLES="1",
            REVIEW_INTERVAL_SECONDS="0",
        )
        original_env = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original_env)))

        original_run = review_loop.subprocess.run
        review_loop.subprocess.run = run
        self.addCleanup(setattr, review_loop.subprocess, "run", original_run)

        original_enum = review_loop.gh.enumerate_candidate_prs
        review_loop.gh.enumerate_candidate_prs = lambda *a, **k: []
        self.addCleanup(setattr, review_loop.gh, "enumerate_candidate_prs", original_enum)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = review_loop.main([])
        return rc, buf.getvalue()

    def test_a_git_that_cannot_be_spawned_warns_and_carries_on(self):
        def run(*a, **k):
            raise OSError(11, "Resource temporarily unavailable")

        rc, out = self._one_cycle(run)
        self.assertEqual(rc, 0)
        self.assertIn("git fetch could not run", out)

    def test_a_git_that_exits_non_zero_warns_and_carries_on(self):
        def run(*a, **k):
            return subprocess.CompletedProcess(a[0], 1, "", "fatal: bad remote")

        rc, out = self._one_cycle(run)
        self.assertEqual(rc, 0)
        self.assertIn("git fetch failed", out)


class McpArgsTest(unittest.TestCase):
    """--strict-mcp-config is a security boundary, not a preference: the
    reviewed repo is untrusted, and without it a repo shipping its own
    .mcp.json gets MCP servers of its choosing loaded into a
    --dangerously-skip-permissions session. test-personas.sh asserts it at the
    argv, and so does this, at the layer the decision now lives in."""

    def _argv(self, env):
        original = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))

        captured = {}

        class Recorder(review_loop.Supervisor):
            def _run_one(inner, pair, prompt, session_id):
                captured["mcp_args"] = list(inner.mcp_args)
                return ok()

        original_sup = review_loop.Supervisor
        review_loop.Supervisor = Recorder
        self.addCleanup(setattr, review_loop, "Supervisor", original_sup)

        original_enum = review_loop.gh.enumerate_candidate_prs
        review_loop.gh.enumerate_candidate_prs = lambda *a, **k: [(12, "code")]
        self.addCleanup(setattr, review_loop.gh, "enumerate_candidate_prs", original_enum)

        original_run = review_loop.subprocess.run
        review_loop.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", "")
        self.addCleanup(setattr, review_loop.subprocess, "run", original_run)

        with contextlib.redirect_stdout(io.StringIO()):
            review_loop.main([])
        return captured["mcp_args"]

    def test_strict_is_always_passed(self):
        argv = self._argv(preflight_env(
            WORK_REPO=".", REVIEW_MODEL="m", MAX_CYCLES="1", PERSONAS="red_team",
        ))
        self.assertEqual(argv, ["--strict-mcp-config"])

    def test_a_generated_config_is_spliced_in_behind_it(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{}")
            path = fh.name
        self.addCleanup(os.unlink, path)
        argv = self._argv(preflight_env(
            WORK_REPO=".", REVIEW_MODEL="m", MAX_CYCLES="1", PERSONAS="red_team",
            MCP_CONFIG_FILE=path,
        ))
        self.assertEqual(argv, ["--strict-mcp-config", "--mcp-config", path])

    def test_a_config_file_that_is_not_there_is_not_passed(self):
        # entrypoint.sh deletes the file when write_mcp_config fails, so a
        # partial write degrades to no MCP servers rather than to truncated JSON.
        argv = self._argv(preflight_env(
            WORK_REPO=".", REVIEW_MODEL="m", MAX_CYCLES="1", PERSONAS="red_team",
            MCP_CONFIG_FILE="/nonexistent/mcp.json",
        ))
        self.assertEqual(argv, ["--strict-mcp-config"])


if __name__ == "__main__":
    unittest.main()
