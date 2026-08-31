import contextlib
import io
import os
import shutil
import stat
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


def grouped(*pairs):
    """One group per PR over exactly these pairs.

    run_cycle takes groups now, and a case that wants to pin the pair list
    rather than the supervisor's persona set builds them here.
    """
    out = []
    for pair in pairs:
        if out and out[-1].pr == pair.pr and out[-1].mode == pair.mode:
            out[-1] = review_loop.Group(pair.pr, pair.mode, out[-1].pairs + (pair,))
        else:
            out.append(review_loop.Group(pair.pr, pair.mode, (pair,)))
    return out


class SessionTest(unittest.TestCase):
    def test_first_pass_starts_a_new_session_and_records_it(self):
        s = supervisor([ok("S9")])
        s.run_cycle(grouped(Pair(12, "code", "red_team")))
        self.assertEqual(s.sessions[Pair(12, "code", "red_team")], "S9")
        self.assertEqual(s.passes_done[Pair(12, "code", "red_team")], 1)

    def test_second_pass_resumes(self):
        s = supervisor([ok("S9"), ok("S9")])
        pair = Pair(12, "code", "red_team")
        s.run_cycle(grouped(pair))
        s.run_cycle(grouped(pair))
        self.assertEqual(s.passes_done[pair], 2)

    def test_the_same_pr_in_two_modes_holds_two_sessions(self):
        s = supervisor([ok("Sc"), ok("Sp")])
        s.run_cycle(grouped(Pair(12, "code", "red_team"), Pair(12, "plan", "red_team")))
        self.assertEqual(s.sessions[Pair(12, "code", "red_team")], "Sc")
        self.assertEqual(s.sessions[Pair(12, "plan", "red_team")], "Sp")

    def test_a_non_limit_failure_drops_the_session(self):
        pair = Pair(12, "code", "red_team")
        s = supervisor([ok("S9"), failed()])
        s.run_cycle(grouped(pair))
        s.run_cycle(grouped(pair))
        self.assertNotIn(pair, s.sessions)
        self.assertEqual(s.passes_done.get(pair, 0), 0)

    def test_a_limit_keeps_the_session(self):
        pair = Pair(12, "code", "red_team")
        s = supervisor([limited("S9")])
        s.run_cycle(grouped(pair))
        self.assertEqual(s.sessions[pair], "S9")

    def test_max_passes_rotates_the_session(self):
        pair = Pair(12, "code", "red_team")
        s = supervisor([ok("S9"), ok("S9")], max_passes_per_session=2)
        s.run_cycle(grouped(pair))
        self.assertIn(pair, s.sessions)
        s.run_cycle(grouped(pair))
        self.assertNotIn(pair, s.sessions)
        self.assertEqual(s.passes_done[pair], 0)

    def test_zero_max_passes_never_rotates(self):
        pair = Pair(12, "code", "red_team")
        s = supervisor([ok("S9")] * 5, max_passes_per_session=0)
        for _ in range(5):
            s.run_cycle(grouped(pair))
        self.assertIn(pair, s.sessions)


class OwedTest(unittest.TestCase):
    """Groups are 12 and 13, two personas each.

    A cycle cut by a limit owes: the limited personas of the cut group, plus
    everything in the groups that never started. The successful personas of the
    cut group are NOT owed -- they ran, and recently.
    """

    CANDIDATES = [(12, "code"), (13, "code")]
    RT12 = Pair(12, "code", "red_team")
    SG12 = Pair(12, "code", "sage")
    RT13 = Pair(13, "code", "red_team")
    SG13 = Pair(13, "code", "sage")

    def cycle(self, s, results_by_pair):
        class Scripted(FakeSupervisor):
            def _run_one(inner, pair, prompt, session_id):
                inner.attempted.append(pair)
                return results_by_pair.get(pair, ok("S1"))

        s.__class__ = Scripted
        with contextlib.redirect_stdout(io.StringIO()):
            return s.run_cycle(s.build_groups(self.CANDIDATES))

    def test_a_clean_cycle_owes_nothing(self):
        s = supervisor([], max_concurrent=0)
        self.assertFalse(self.cycle(s, {}))
        self.assertEqual(s.owed, set())

    def test_a_limit_owes_the_limited_persona_and_the_untouched_group(self):
        s = supervisor([], max_concurrent=0)
        limited_result = self.cycle(s, {self.SG12: limited("S9")})
        self.assertTrue(limited_result)
        self.assertEqual(s.owed, {self.SG12, self.RT13, self.SG13})

    def test_the_successful_sibling_of_a_limited_persona_is_not_owed(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})
        self.assertNotIn(self.RT12, s.owed)

    def test_the_whole_group_finishes_before_the_cycle_stops(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.RT12: limited("S9")})
        self.assertIn(self.SG12, s.attempted)

    def test_a_limited_pair_keeps_its_session(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})
        self.assertEqual(s.sessions[self.SG12], "S9")

    def test_the_next_cycle_runs_only_what_is_owed_in_the_cut_group(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})
        s.attempted.clear()
        self.cycle(s, {})
        self.assertEqual(
            set(p for p in s.attempted if p.pr == 12), {self.SG12}
        )

    def test_the_untouched_group_runs_in_full_on_the_next_cycle(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})
        s.attempted.clear()
        self.cycle(s, {})
        self.assertEqual(
            set(p for p in s.attempted if p.pr == 13), {self.RT13, self.SG13}
        )

    def test_the_next_cycle_starts_after_the_group_the_cut_stopped_in(self):
        # Phase A's wrap-around, at group granularity. Starting at the debt
        # instead is what StarvationTest below rules out.
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG13: limited("S9")})
        s.attempted.clear()
        self.cycle(s, {})
        self.assertEqual(s.attempted[0].pr, 12)

    def test_the_cut_group_goes_last_and_still_gets_its_debt_served(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})
        s.attempted.clear()
        self.cycle(s, {})
        self.assertEqual(s.attempted[0].pr, 13)
        self.assertEqual(s.attempted[-1], self.SG12)

    def test_a_group_that_completed_before_the_cut_still_runs_on_the_next_cycle(self):
        # This is what preserves the wrap-around, and it is what keeps a
        # persistent limit from starving the tail of the list forever.
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG13: limited("S9")})
        s.attempted.clear()
        self.cycle(s, {})
        self.assertEqual(set(p.pr for p in s.attempted), {12, 13})

    def test_a_cut_group_that_disappears_leaves_the_order_alone(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG13: limited("S9")})

        class Scripted(FakeSupervisor):
            def _run_one(inner, pair, prompt, session_id):
                inner.attempted.append(pair)
                return ok("S1")

        s.__class__ = Scripted
        s.attempted.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(s.build_groups([(12, "code")]))
        self.assertEqual(set(p.pr for p in s.attempted), {12})

    def test_two_clean_cycles_after_a_cut_return_to_full_service(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})
        self.cycle(s, {})
        s.attempted.clear()
        self.cycle(s, {})
        self.assertEqual(len(s.attempted), 4)

    def test_the_skipped_pairs_are_named_in_the_log(self):
        # An operator has to be able to read a stall, not infer one from
        # comments that never arrive.
        s = supervisor([], max_concurrent=0)

        class Scripted(FakeSupervisor):
            def _run_one(inner, pair, prompt, session_id):
                inner.attempted.append(pair)
                return limited("S9") if pair == OwedTest.SG12 else ok("S1")

        s.__class__ = Scripted
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(s.build_groups(self.CANDIDATES))
        line = [l for l in buf.getvalue().splitlines() if "Not reviewed" in l]
        self.assertEqual(len(line), 1)
        for pair in (self.RT13, self.SG13):
            self.assertIn(str(pair), line[0])
        self.assertNotIn(str(self.RT12), line[0])

    def test_the_resume_log_names_what_the_next_cycle_owes(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})

        class Scripted(FakeSupervisor):
            def _run_one(inner, pair, prompt, session_id):
                inner.attempted.append(pair)
                return ok("S1")

        s.__class__ = Scripted
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(s.build_groups(self.CANDIDATES))
        line = [l for l in buf.getvalue().splitlines() if "Resuming with" in l]
        self.assertEqual(len(line), 1)
        self.assertIn(str(self.SG12), line[0])
        self.assertNotIn(str(self.RT12), line[0])

    def test_a_closed_pr_drops_out_of_owed(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG13: limited("S9")})

        class Scripted(FakeSupervisor):
            def _run_one(inner, pair, prompt, session_id):
                inner.attempted.append(pair)
                return ok("S1")

        s.__class__ = Scripted
        s.attempted.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(s.build_groups([(12, "code")]))
        self.assertEqual(s.owed, set())
        self.assertEqual(set(p.pr for p in s.attempted), {12})

    def test_an_unreached_group_owes_its_whole_persona_set(self):
        # Group 12 carries a one-persona debt from cycle 1 and is then not
        # reached at all by cycle 2's cut. None of it ran, so all of it is owed:
        # the narrowing was justified by a cut two cycles back, and red_team's
        # last pass is that old.
        candidates = [(12, "code"), (13, "code"), (14, "code")]

        def scripted(limited_pairs):
            class Scripted(FakeSupervisor):
                def _run_one(inner, pair, prompt, session_id):
                    inner.attempted.append(pair)
                    return limited("S9") if pair in limited_pairs else ok("S1")
            return Scripted

        s = supervisor([], max_concurrent=0)
        with contextlib.redirect_stdout(io.StringIO()):
            s.__class__ = scripted({self.SG12})
            s.run_cycle(s.build_groups(candidates))
            s.__class__ = scripted({Pair(13, "code", "sage")})
            s.run_cycle(s.build_groups(candidates))
            s.__class__ = scripted(set())
            s.attempted.clear()
            s.run_cycle(s.build_groups(candidates))
        self.assertEqual(
            [p for p in s.attempted if p.pr == 12], [self.RT12, self.SG12]
        )

    def test_an_empty_candidate_list_keeps_owed(self):
        # Enumeration failures degrade to an empty candidate list, so "no
        # candidate PRs" can mean gh had a bad minute. That must not silently
        # clear the debt.
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})
        before = set(s.owed)
        s.run_cycle([])
        self.assertEqual(s.owed, before)


class StarvationTest(unittest.TestCase):
    """One pair reports a limit on every attempt, forever.

    It is reachable without account-level exhaustion: the limit classifier
    matches 429, 529, quota and overloaded, so one PR with a diff big enough to
    trip a per-request ceiling does it. Serving the debt first would re-cut the
    cycle at the head of the list every time and no other PR would ever be
    reviewed again -- silently, one `Resuming with` line per cycle and no error.
    Phase A served every pair under exactly this condition, so the rotation is
    not an improvement, it is the property being kept.
    """

    CANDIDATES = [(12, "code"), (13, "code")]
    RT12 = Pair(12, "code", "red_team")
    SG12 = Pair(12, "code", "sage")

    def cycles(self, s, limited_pairs, count=6):
        class Scripted(FakeSupervisor):
            def _run_one(inner, pair, prompt, session_id):
                inner.attempted.append(pair)
                return limited("S9") if pair in limited_pairs else ok("S1")

        s.__class__ = Scripted
        seen = []
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(count):
                s.attempted.clear()
                s.run_cycle(s.build_groups(self.CANDIDATES))
                seen.append(list(s.attempted))
        return seen

    def test_the_other_pr_is_reviewed_on_every_cycle(self):
        s = supervisor([], max_concurrent=0)
        seen = self.cycles(s, {self.SG12})
        for index, attempted in enumerate(seen[1:], start=2):
            self.assertEqual(
                set(p.pr for p in attempted if p.pr == 13), {13},
                f"PR 13 was not reviewed on cycle {index}: {attempted}",
            )

    def test_the_pr_13_group_runs_in_full_once_its_debt_clears(self):
        s = supervisor([], max_concurrent=0)
        seen = self.cycles(s, {self.SG12})
        self.assertEqual(
            [p for p in seen[2] if p.pr == 13],
            [Pair(13, "code", "red_team"), Pair(13, "code", "sage")],
        )

    def test_the_limited_pairs_sibling_is_not_starved_either(self):
        # A narrowed group leaves its siblings unrun, and a cut owes what did
        # not run, so red_team comes back on the visit after.
        s = supervisor([], max_concurrent=0)
        seen = self.cycles(s, {self.SG12})
        served = [i for i, attempted in enumerate(seen) if self.RT12 in attempted]
        self.assertGreaterEqual(len(served), 3, f"red_team ran on cycles {served}")

    def test_the_limited_pair_is_retried_every_cycle(self):
        s = supervisor([], max_concurrent=0)
        seen = self.cycles(s, {self.SG12})
        for index, attempted in enumerate(seen, start=1):
            self.assertIn(self.SG12, attempted, f"cycle {index}: {attempted}")

    def test_an_exhausted_provider_still_makes_no_progress(self):
        # The other direction. When every pair is limited there is nothing to
        # rotate towards, and the loop must not manufacture progress: each cycle
        # runs one group, reports the limit, and backs off.
        every = {Pair(pr, "code", persona)
                 for pr in (12, 13) for persona in ("red_team", "sage")}
        s = supervisor([], max_concurrent=0)
        seen = self.cycles(s, every)
        self.assertTrue(all(len(attempted) == 2 for attempted in seen), seen)
        self.assertEqual(s.owed, every)

    def test_it_recovers_on_the_first_cycle_that_succeeds(self):
        every = {Pair(pr, "code", persona)
                 for pr in (12, 13) for persona in ("red_team", "sage")}
        s = supervisor([], max_concurrent=0)
        self.cycles(s, every)
        recovered = self.cycles(s, set(), count=1)
        self.assertEqual(len(recovered[0]), 4)
        self.assertEqual(s.owed, set())
        self.assertIsNone(s.cut_group)


class ConsecutiveFailureTest(unittest.TestCase):
    PAIRS = [Pair(n, "code", "red_team") for n in (12, 13, 14, 15, 16)]

    def test_three_non_limit_failures_abandon_the_cycle(self):
        s = supervisor([failed(), failed(), failed()])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            was_limited = s.run_cycle(grouped(*self.PAIRS))
        self.assertFalse(was_limited)
        self.assertEqual(len(s.attempted), 3)
        # The pairs that failed are not owed: they ran. The ones the cycle never
        # reached are.
        self.assertEqual(s.owed, {self.PAIRS[3], self.PAIRS[4]})

    def test_a_success_resets_the_counter(self):
        s = supervisor([failed(), failed(), ok(), failed(), failed()])
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(grouped(*self.PAIRS))
        self.assertEqual(len(s.attempted), 5)

    def test_limits_do_not_feed_the_non_limit_counter(self):
        s = supervisor([failed(), failed(), limited()])
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(grouped(*self.PAIRS))
        self.assertEqual(len(s.attempted), 3)
        self.assertEqual(s.owed, {self.PAIRS[2], self.PAIRS[3], self.PAIRS[4]})

    def test_the_count_is_taken_at_the_barrier_so_a_group_finishes(self):
        # Four personas on one PR, every one of them failing. Counting per pass
        # would abandon after the third and leave the fourth unrun; the group is
        # the unit, so all four run and the cycle stops at the barrier.
        personas = ["red_team", "sage", "sme", "adversarial"]
        s = supervisor(
            [failed()] * 4,
            personas={"code": personas},
            persona_prompts={("code", p): p for p in personas},
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(s.build_groups([(12, "code")]))
        self.assertEqual(len(s.attempted), 4)
        self.assertIn("4 passes in a row failed", buf.getvalue())

    def test_a_success_anywhere_in_the_group_resets_the_count(self):
        # A group that reviewed something proves the provider is alive, whatever
        # its siblings did, so two groups of a failure-plus-a-success never
        # reach the three-strikes rule.
        s = supervisor([failed(), ok(), failed(), ok(), failed(), ok()])
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(s.build_groups([(12, "code"), (13, "code"), (14, "code")]))
        self.assertEqual(len(s.attempted), 6)


    def test_the_three_strikes_exit_owes_a_narrowed_groups_unrun_siblings(self):
        # The three-strikes branch computes its debt the same way the limit
        # branch does, and only a narrowed group tells the two spellings apart:
        # with the whole group running, `did_not_run` is empty and an exit that
        # owed nothing would look identical. PR 13 enters owing one persona of
        # two, so its sibling never runs, and the counter reaches three inside
        # it. That sibling has to come out owed or a provider outage silently
        # drops it until something else happens to re-owe the group.
        s = supervisor([failed(), failed(), failed()])
        s.owed = {Pair(13, "code", "sage")}
        groups = s.build_groups([(12, "code"), (13, "code")])
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(groups)
        self.assertEqual(
            [p.persona for p in s.attempted if p.pr == 13], ["sage"])
        self.assertIn(Pair(13, "code", "red_team"), s.owed)


class PromptChoiceTest(unittest.TestCase):
    def test_new_session_gets_the_review_prompt_rendered(self):
        seen = []

        class Recorder(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                seen.append((prompt, session_id))
                return ok("S1")

        s = supervisor([])
        s.__class__ = Recorder
        s.run_cycle(grouped(Pair(12, "code", "red_team")))
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
        s.run_cycle(grouped(pair))
        s.run_cycle(grouped(pair))
        self.assertEqual(seen[1], ("recheck #12", "S1"))

    def test_plan_mode_uses_the_plan_prompts(self):
        seen = []

        class Recorder(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                seen.append(prompt)
                return ok("S1")

        s = supervisor([])
        s.__class__ = Recorder
        s.run_cycle(grouped(Pair(13, "plan", "red_team")))
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
            limited_flag = s.run_cycle(grouped(Pair(12, "code", "red_team")))
        self.assertFalse(limited_flag)
        self.assertIn("could not run claude", buf.getvalue())
        self.assertIn("starting a fresh session for it next cycle", buf.getvalue())

    def test_a_missing_claude_binary_does_not_take_the_loop_down(self):
        s = self._raising_supervisor(FileNotFoundError(2, "No such file or directory"))
        pairs = [Pair(n, "code", "red_team") for n in (12, 13)]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(grouped(*pairs))
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
            s.run_cycle(grouped(*pairs))
        self.assertIn("3 passes in a row failed", buf.getvalue())
        self.assertEqual(s.owed, {Pair(15, "code", "red_team")})


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


class SubmitFailureTest(unittest.TestCase):
    """A container out of threads cannot accept the next pass.

    ThreadPoolExecutor.submit raises RuntimeError, which is not an OSError and
    is raised on the submitting thread, so neither _run_one's guard nor
    _dispatch's catch-all is anywhere near it. Letting it out would discard the
    results the pool already holds -- passes that have posted their comments and
    hold resumable sessions.
    """

    def _cap_submissions(self, allowed):
        """Patch the pool so only `allowed` submissions per group succeed.

        Returns the real class, so a case that needs a second, healthy cycle can
        put it back.
        """
        real = review_loop.ThreadPoolExecutor

        class Exhausted(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._accepted = 0

            def submit(self, *args, **kwargs):
                if self._accepted >= allowed:
                    raise RuntimeError("can't start new thread")
                self._accepted += 1
                return super().submit(*args, **kwargs)

        review_loop.ThreadPoolExecutor = Exhausted
        self.addCleanup(setattr, review_loop, "ThreadPoolExecutor", real)
        return real

    def test_the_pairs_the_pool_refused_have_no_result(self):
        self._cap_submissions(1)
        s = supervisor([ok("S1")], max_concurrent=1)
        group = s.build_groups([(12, "code")])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            results = s.run_group(group, list(group.pairs))
        self.assertEqual(list(results), [Pair(12, "code", "red_team")])

    def test_the_results_already_collected_survive(self):
        self._cap_submissions(1)
        s = supervisor([ok("S9")], max_concurrent=1)
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(s.build_groups([(12, "code")]))
        self.assertEqual(s.sessions[Pair(12, "code", "red_team")], "S9")
        self.assertEqual(s.passes_done[Pair(12, "code", "red_team")], 1)

    def test_a_refused_pair_is_owed_rather_than_failed(self):
        self._cap_submissions(1)
        s = supervisor([ok("S9")], max_concurrent=1)
        s.sessions[Pair(12, "code", "sage")] = "S8"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(s.build_groups([(12, "code")]))
        self.assertIn(Pair(12, "code", "sage"), s.owed)
        # It did not run, so it is not a failed pass: its session stands.
        self.assertEqual(s.sessions[Pair(12, "code", "sage")], "S8")
        self.assertNotIn("review failed", buf.getvalue())
        self.assertIn("could not start a worker", buf.getvalue())
        # An operator grepping for the cut finds it: the limit path says
        # "ending this cycle", three strikes says "Abandoning this cycle", and
        # this one used to say neither.
        self.assertIn("abandoning this cycle", buf.getvalue())

    def test_the_cycle_stops_and_owes_the_groups_it_never_reached(self):
        self._cap_submissions(1)
        s = supervisor([ok("S9")], max_concurrent=1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            was_limited = s.run_cycle(s.build_groups([(12, "code"), (13, "code")]))
        # Not a limit, so the next cycle comes at the ordinary interval.
        self.assertFalse(was_limited)
        self.assertEqual(set(p.pr for p in s.attempted), {12})
        self.assertEqual(
            s.owed,
            {Pair(12, "code", "sage"),
             Pair(13, "code", "red_team"), Pair(13, "code", "sage")},
        )

    def test_one_refusal_stops_the_group_being_submitted(self):
        # The next submit would hit the same wall, so it is not attempted: one
        # WARN names the pair the pool refused, and the rest of the group is
        # owed without a line each.
        self._cap_submissions(1)
        personas = ["red_team", "sage", "sme"]
        s = supervisor(
            [ok("S9")], max_concurrent=1,
            personas={"code": personas},
            persona_prompts={("code", p): p for p in personas},
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(s.build_groups([(12, "code")]))
        refusals = [l for l in buf.getvalue().splitlines() if "could not start a worker" in l]
        self.assertEqual(len(refusals), 1)
        self.assertIn(str(Pair(12, "code", "sage")), refusals[0])
        self.assertEqual(
            s.owed, {Pair(12, "code", "sage"), Pair(12, "code", "sme")}
        )

    def test_the_next_cycle_runs_what_the_pool_refused(self):
        real = self._cap_submissions(1)
        s = supervisor([ok("S9")], max_concurrent=1)
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(s.build_groups([(12, "code")]))
        review_loop.ThreadPoolExecutor = real
        s.attempted.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(s.build_groups([(12, "code")]))
        self.assertEqual(s.attempted, [Pair(12, "code", "sage")])


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


def scratch_repo(case):
    """A throwaway WORK_REPO, cleaned up with the case.

    main() chmods its WORK_REPO now, so a test that passes "." makes the
    checkout the suite is running from read-only and leaves it that way. The
    atexit guard in _path.py is the net under this; use the helper instead of
    landing in it.
    """
    repo = tempfile.mkdtemp()
    os.mkdir(os.path.join(repo, ".git"))
    case.addCleanup(shutil.rmtree, repo, ignore_errors=True)
    return repo


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
            WORK_REPO=scratch_repo(self), REVIEW_MODEL="m", MAX_CYCLES="1",
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
            WORK_REPO=scratch_repo(self), REVIEW_MODEL="m", MAX_CYCLES="1", PERSONAS="red_team",
        ))
        self.assertEqual(argv, ["--strict-mcp-config"])

    def test_a_generated_config_is_spliced_in_behind_it(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{}")
            path = fh.name
        self.addCleanup(os.unlink, path)
        argv = self._argv(preflight_env(
            WORK_REPO=scratch_repo(self), REVIEW_MODEL="m", MAX_CYCLES="1", PERSONAS="red_team",
            MCP_CONFIG_FILE=path,
        ))
        self.assertEqual(argv, ["--strict-mcp-config", "--mcp-config", path])

    def test_a_config_file_that_is_not_there_is_not_passed(self):
        # entrypoint.sh deletes the file when write_mcp_config fails, so a
        # partial write degrades to no MCP servers rather than to truncated JSON.
        argv = self._argv(preflight_env(
            WORK_REPO=scratch_repo(self), REVIEW_MODEL="m", MAX_CYCLES="1", PERSONAS="red_team",
            MCP_CONFIG_FILE="/nonexistent/mcp.json",
        ))
        self.assertEqual(argv, ["--strict-mcp-config"])


class SharedWorktreeModesTest(unittest.TestCase):
    """Which modes are told their working copy is shared.

    Per mode, because effective concurrency is min(cap, that mode's persona
    count). Telling a mode that runs one persona at a time it is sharing a tree
    would be a false statement in the prompt.
    """

    def test_a_mode_with_one_persona_is_never_concurrent(self):
        got = review_loop.shared_worktree_modes(
            personas={"code": ["red_team"], "plan": ["a", "b"]}, max_concurrent=0
        )
        self.assertEqual(got, frozenset({"plan"}))

    def test_a_cap_of_one_makes_no_mode_concurrent(self):
        got = review_loop.shared_worktree_modes(
            personas={"code": ["a", "b"], "plan": ["a", "b"]}, max_concurrent=1
        )
        self.assertEqual(got, frozenset())

    def test_unlimited_makes_every_multi_persona_mode_concurrent(self):
        got = review_loop.shared_worktree_modes(
            personas={"code": ["a", "b"], "plan": ["a", "b", "c"]}, max_concurrent=0
        )
        self.assertEqual(got, frozenset({"code", "plan"}))

    def test_a_cap_above_one_is_concurrent(self):
        got = review_loop.shared_worktree_modes(
            personas={"code": ["a", "b", "c"]}, max_concurrent=2
        )
        self.assertEqual(got, frozenset({"code"}))

    def test_an_empty_mode_is_not_concurrent(self):
        got = review_loop.shared_worktree_modes(personas={"code": []}, max_concurrent=0)
        self.assertEqual(got, frozenset())

    def test_it_agrees_with_the_worker_count_the_group_gets(self):
        # The stanza claims something the pool has to make true. If these two
        # disagree, a persona is told it is alone while a sibling runs, or told
        # to avoid a conflict that cannot happen.
        for cap in (0, 1, 2, 5):
            for count in (1, 2, 4):
                sup = review_loop.Supervisor(
                    personas={"code": ["p%d" % i for i in range(count)]},
                    persona_prompts={}, review_prompts={}, followup_prompts={},
                    model="m", mcp_args=[], cwd=".", max_passes_per_session=0,
                    max_concurrent=cap,
                )
                group = sup.build_groups([(1, "code")])[0]
                workers = sup.workers_for(group, group.pairs)
                said = "code" in review_loop.shared_worktree_modes(
                    personas=sup.personas, max_concurrent=cap
                )
                self.assertEqual(said, workers > 1, "cap=%d count=%d" % (cap, count))


class WorktreeStanzaWiringTest(unittest.TestCase):
    """The stanza has to reach an actual pass, not only prompts.build."""

    def _prompt(self, env):
        original = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))

        captured = {}

        class Recorder(review_loop.Supervisor):
            def _run_one(inner, pair, prompt, session_id):
                captured["prompt"] = prompt
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
        return captured["prompt"]

    def test_a_multi_persona_run_carries_it(self):
        prompt = self._prompt(preflight_env(
            WORK_REPO=scratch_repo(self), REVIEW_MODEL="m", MAX_CYCLES="1",
        ))
        self.assertIn(review_loop.prompts_mod.WORKTREE_STANZA, prompt)

    def test_a_single_persona_run_does_not(self):
        prompt = self._prompt(preflight_env(
            WORK_REPO=scratch_repo(self), REVIEW_MODEL="m", MAX_CYCLES="1", PERSONAS="red_team",
        ))
        self.assertNotIn(review_loop.prompts_mod.WORKTREE_STANZA, prompt)

    def test_a_cap_of_one_does_not(self):
        prompt = self._prompt(preflight_env(
            WORK_REPO=scratch_repo(self), REVIEW_MODEL="m", MAX_CYCLES="1",
            MAX_CONCURRENT_PASSES="1",
        ))
        self.assertNotIn(review_loop.prompts_mod.WORKTREE_STANZA, prompt)


@unittest.skipUnless(shutil.which("git"), "git not available")
class GitDirLockTest(unittest.TestCase):
    """What the read-only .git actually stops, exercised through real git.

    Asserting the mode bits would prove only that os.chmod works. Every test
    here runs a git command and reads its exit code, because the mode bits are
    the mechanism and the exit code is the claim.
    """

    def setUp(self):
        if os.geteuid() == 0:
            # root ignores the write bit, so every "this fails" assertion below
            # would pass a locked directory and go green having tested nothing.
            # The container runs as `reviewer` for exactly this reason.
            self.skipTest("root writes through a read-only directory")
        self.repo = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        self._init(self.repo)

    def _init(self, path):
        subprocess.run(["git", "init", "-q", path], check=True)
        subprocess.run(["git", "-C", path, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", path, "config", "user.name", "t"], check=True)
        open(os.path.join(path, "f.txt"), "w").close()
        subprocess.run(["git", "-C", path, "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", path, "commit", "-qm", "init"], check=True)
        subprocess.run(["git", "-C", path, "commit", "-qm", "two", "--allow-empty"], check=True)

    def _cleanup(self):
        review_loop.unlock_git_dir(self.repo)
        shutil.rmtree(self.repo, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", self.repo, *args], capture_output=True, text=True, check=False
        )

    def test_the_lock_is_the_reason_a_write_fails(self):
        # Ties the exit codes below to the mechanism: unlocked, the same command
        # succeeds, so a green from the locked case is not some other failure.
        self.assertEqual(self.git("checkout", "-b", "before").returncode, 0)
        review_loop.lock_git_dir(self.repo)
        self.assertNotEqual(self.git("checkout", "-b", "after").returncode, 0)

    def test_reading_git_still_works_when_locked(self):
        # for-each-ref is in the list because the lock now covers .git/refs:
        # enumerating refs must survive locking the directories they live in.
        review_loop.lock_git_dir(self.repo)
        for args in (
            ("log", "-1", "--oneline"),
            ("show", "--stat", "HEAD"),
            ("diff", "HEAD"),
            ("status", "--porcelain"),
            ("cat-file", "-p", "HEAD"),
            ("for-each-ref",),
            ("branch", "--list"),
        ):
            self.assertEqual(self.git(*args).returncode, 0, args)

    def test_the_index_writers_fail_when_locked(self):
        # All four take .git/index.lock, which needs write permission on the
        # .git directory inode. This is the whole mechanism.
        review_loop.lock_git_dir(self.repo)
        open(os.path.join(self.repo, "g.txt"), "w").close()
        for args in (
            ("add", "g.txt"),
            ("commit", "--allow-empty", "-m", "x"),
            ("stash",),
            ("reset", "--hard", "HEAD"),
        ):
            self.assertNotEqual(self.git(*args).returncode, 0, args)

    def test_the_ref_writers_fail_when_locked(self):
        # The reason the lock covers .git/refs. Under a .git-only lock these all
        # exit 0, and two of them change what a sibling reads: update-ref on a
        # remote-tracking ref empties `git log origin/main..HEAD`, and a note
        # renders inside `git log` and `git show` as commit metadata.
        review_loop.lock_git_dir(self.repo)
        for args in (
            ("update-ref", "refs/remotes/origin/main", "HEAD"),
            ("notes", "add", "-f", "-m", "injected", "HEAD"),
            ("branch", "nb"),
            ("tag", "t1"),
            ("checkout", "-b", "feature"),
            ("worktree", "add", os.path.join(self.repo, "..", "wt")),
        ):
            self.assertNotEqual(self.git(*args).returncode, 0, args)
        # A failed checkout -b or worktree add used to leave refs/heads/<name>
        # behind. With refs locked they cannot, which is worth asserting rather
        # than assuming.
        self.assertEqual(self.git("rev-parse", "--verify", "-q", "feature").returncode, 1)
        self.assertEqual(self.git("rev-parse", "--verify", "-q", "wt").returncode, 1)

    def test_a_sibling_still_reads_the_pr_it_started_with(self):
        # The consequence the ref lock exists for, stated as the sibling sees
        # it: one commit ahead of origin/main before the attack, and still one
        # commit ahead after it.
        self.git("update-ref", "refs/remotes/origin/main", "HEAD~1")
        before = self.git("log", "--oneline", "refs/remotes/origin/main..HEAD").stdout
        self.assertEqual(len(before.splitlines()), 1)

        review_loop.lock_git_dir(self.repo)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("notes", "add", "-f", "-m", "injected", "HEAD")

        after = self.git("log", "--oneline", "refs/remotes/origin/main..HEAD")
        self.assertEqual(after.returncode, 0)
        self.assertEqual(after.stdout, before)
        self.assertNotIn("injected", self.git("log", "-1").stdout)

    def _upstream(self):
        upstream = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, upstream, ignore_errors=True)
        self._init(upstream)
        subprocess.run(["git", "-C", self.repo, "remote", "add", "up", upstream], check=True)
        return upstream

    def test_the_supervisors_own_fetch_needs_the_unlock(self):
        # git fetch opens .git/FETCH_HEAD, so the loop's once-a-cycle fetch is
        # itself blocked. unlocked_git_dir exists for this and nothing else.
        self._upstream()
        review_loop.lock_git_dir(self.repo)
        self.assertNotEqual(self.git("fetch", "up").returncode, 0)
        with review_loop.unlocked_git_dir(self.repo, enabled=True):
            self.assertEqual(self.git("fetch", "up").returncode, 0)
        open(os.path.join(self.repo, "g.txt"), "w").close()
        self.assertNotEqual(self.git("add", "g.txt").returncode, 0)

    def test_a_fetch_cannot_move_a_ref_once_the_supervisor_has_fetched_once(self):
        # FETCH_HEAD survives the supervisor's own fetch as an ordinary writable
        # file, so the .git half of the lock stops blocking fetch after cycle
        # one -- and --no-write-fetch-head walks past it even on cycle one. The
        # refs half is what holds: a fetch that would move a remote-tracking ref
        # fails on that ref's lock, so the part of a fetch a sibling can observe
        # is closed even though the command is not.
        upstream = self._upstream()
        review_loop.lock_git_dir(self.repo)
        with review_loop.unlocked_git_dir(self.repo, enabled=True):
            self.assertEqual(self.git("fetch", "up").returncode, 0)
        before = self.git("rev-parse", "refs/remotes/up/HEAD@{0}").stdout

        subprocess.run(["git", "-C", upstream, "commit", "-qm", "three", "--allow-empty"],
                       check=True)
        for args in (("fetch", "up"), ("fetch", "--no-write-fetch-head", "up")):
            self.assertNotEqual(self.git(*args).returncode, 0, args)
        self.assertEqual(self.git("rev-parse", "refs/remotes/up/HEAD@{0}").stdout, before)

    def test_a_stale_lock_blocks_the_entrypoints_clone_prep(self):
        # entrypoint.sh runs `git remote set-url` on a clone that survived a
        # restart, and set-url writes .git/config through .git/config.lock. This
        # is the failure the chmod u+w in the clone-prep block recovers from;
        # without it the shell dies under set -e and the container crash-loops.
        review_loop.lock_git_dir(self.repo)
        self.assertNotEqual(
            self.git("remote", "set-url", "origin", "https://example.invalid/x.git").returncode, 0
        )
        self.assertNotEqual(
            self.git("remote", "add", "origin", "https://example.invalid/x.git").returncode, 0
        )
        review_loop.unlock_git_dir(self.repo)
        self.assertEqual(
            self.git("remote", "add", "origin", "https://example.invalid/x.git").returncode, 0
        )

    def test_unlocking_restores_writability(self):
        review_loop.lock_git_dir(self.repo)
        review_loop.unlock_git_dir(self.repo)
        open(os.path.join(self.repo, "g.txt"), "w").close()
        self.assertEqual(self.git("add", "g.txt").returncode, 0)

    def test_the_context_manager_relocks_after_a_clean_exit(self):
        review_loop.lock_git_dir(self.repo)
        with review_loop.unlocked_git_dir(self.repo, enabled=True):
            pass
        open(os.path.join(self.repo, "g.txt"), "w").close()
        self.assertNotEqual(self.git("add", "g.txt").returncode, 0)

    def test_the_context_manager_relocks_after_a_raise(self):
        # git fetch is already allowed to fail. A raise on the way out must not
        # leave the clone writable for the whole cycle with nothing in the log.
        review_loop.lock_git_dir(self.repo)
        with self.assertRaises(RuntimeError):
            with review_loop.unlocked_git_dir(self.repo, enabled=True):
                raise RuntimeError("fetch blew up")
        open(os.path.join(self.repo, "g.txt"), "w").close()
        self.assertNotEqual(self.git("add", "g.txt").returncode, 0)

    def test_disabled_leaves_an_unlocked_clone_alone(self):
        with review_loop.unlocked_git_dir(self.repo, enabled=False):
            pass
        open(os.path.join(self.repo, "g.txt"), "w").close()
        self.assertEqual(self.git("add", "g.txt").returncode, 0)

    def test_disabled_does_not_unlock_a_locked_clone(self):
        # A sequential run never locks, so this cannot arise there. It is
        # asserted anyway because the flag reads like "unlock unless disabled",
        # and a disabled pass that unlocked would be an enforcement hole.
        review_loop.lock_git_dir(self.repo)
        with review_loop.unlocked_git_dir(self.repo, enabled=False):
            open(os.path.join(self.repo, "g.txt"), "w").close()
            self.assertNotEqual(self.git("add", "g.txt").returncode, 0)

    def test_the_object_store_is_never_walked(self):
        # The property commit fdd0ac1's file-count guard cares about: a chmod
        # over .git/objects is O(object count). Asserted on the deepest fanout
        # directory as well as the root, since a walk would reach both.
        objects = os.path.join(self.repo, ".git", "objects")
        subdirs = [
            os.path.join(objects, name) for name in os.listdir(objects)
            if os.path.isdir(os.path.join(objects, name))
        ]
        self.assertTrue(subdirs, "the fixture repo should have loose objects")
        watched = [objects] + subdirs
        before = [os.stat(path).st_mode for path in watched]
        review_loop.lock_git_dir(self.repo)
        self.assertEqual([os.stat(path).st_mode for path in watched], before)

    def test_locking_is_recursive_over_refs_and_nowhere_else(self):
        # Recursive over refs, because a ref write locks beside the ref rather
        # than in .git. Nowhere else, because everything else under .git is
        # either O(object count) or holds nothing a review can reach.
        refs = os.path.join(self.repo, ".git", "refs")
        os.makedirs(os.path.join(refs, "remotes", "origin"), exist_ok=True)
        logs = os.path.join(self.repo, ".git", "logs")
        os.makedirs(logs, exist_ok=True)
        review_loop.lock_git_dir(self.repo)
        for root, dirs, _files in os.walk(refs):
            for path in [root] + [os.path.join(root, d) for d in dirs]:
                self.assertFalse(os.access(path, os.W_OK), path)
        self.assertTrue(os.access(logs, os.W_OK))

    def test_unlocking_restores_the_mode_the_lock_found(self):
        # Not a bare u+w: the lock drops group and other too, so restoring only
        # the owner bit walks a 775 .git down to 755 and leaves it there.
        git_dir = os.path.join(self.repo, ".git")
        os.chmod(git_dir, 0o775)
        review_loop.lock_git_dir(self.repo)
        review_loop.unlock_git_dir(self.repo)
        self.assertEqual(stat.S_IMODE(os.stat(git_dir).st_mode), 0o775)

    def test_a_missing_git_dir_refuses_rather_than_warning(self):
        # Nothing but the concurrent path calls this, and by the time it runs
        # every persona has already been told the working copy is protected.
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        with self.assertRaises(ConfigError) as caught:
            review_loop.lock_git_dir(empty)
        self.assertIn("no .git at", str(caught.exception))


@unittest.skipUnless(shutil.which("git"), "git not available")
class GitDirLockWiringTest(unittest.TestCase):
    """Whether main() locks its own clone, which no test of lock_git_dir sees.

    Driven end to end and asserted on a real git command, so computing the
    concurrency correctly and then never calling the lock reads as a failure.
    """

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("root writes through a read-only directory")
        self.repo = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        subprocess.run(["git", "init", "-q", self.repo], check=True)

    def _cleanup(self):
        review_loop.unlock_git_dir(self.repo)
        shutil.rmtree(self.repo, ignore_errors=True)

    def _main(self, **env):
        original = os.environ.copy()
        os.environ.clear()
        os.environ.update(preflight_env(
            WORK_REPO=self.repo, REVIEW_MODEL="m", MAX_CYCLES="1", **env
        ))
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))

        original_enum = review_loop.gh.enumerate_candidate_prs
        review_loop.gh.enumerate_candidate_prs = lambda *a, **k: []
        self.addCleanup(setattr, review_loop.gh, "enumerate_candidate_prs", original_enum)

        with contextlib.redirect_stdout(io.StringIO()) as out:
            review_loop.main([])
        return out.getvalue()

    def _with_upstream(self):
        upstream = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, upstream, ignore_errors=True)
        for args in (
            ["git", "init", "-q", upstream],
            ["git", "-C", upstream, "config", "user.email", "t@t"],
            ["git", "-C", upstream, "config", "user.name", "t"],
            ["git", "-C", upstream, "commit", "-qm", "i", "--allow-empty"],
            ["git", "-C", self.repo, "remote", "add", "origin", upstream],
        ):
            subprocess.run(args, check=True)
        return upstream

    def _can_write(self):
        return subprocess.run(
            ["git", "-C", self.repo, "checkout", "-b", "probe"],
            capture_output=True, text=True, check=False,
        ).returncode == 0

    def test_a_concurrent_run_leaves_the_clone_locked(self):
        self._main()
        self.assertFalse(self._can_write())

    def test_the_cycles_own_fetch_still_runs(self):
        # A real remote, so the fetch has something to succeed at. Locking the
        # clone and not lifting it around the fetch leaves the working copy
        # frozen at the revision it was cloned at, and the only symptom is one
        # WARN a cycle.
        self._with_upstream()
        log = self._main()
        self.assertNotIn("git fetch failed", log)
        self.assertFalse(self._can_write())

    def test_a_restart_into_a_locked_clone_can_still_fetch(self):
        # The whole restart sequence, because the bug lived between the runs.
        # entrypoint.sh restores $WORK_REPO/.git and nothing under it, so the
        # second run met a writable .git over a read-only .git/refs, recorded
        # THOSE as the originals, and restored them faithfully on every unlock.
        # The fetch window opened with refs unwritable for the life of the
        # container, and the working clone silently stopped advancing while
        # every review kept reading a frozen tree.
        self._with_upstream()

        review_loop.lock_git_dir(self.repo)
        # The process dies: the recorded modes go with it, the chmods do not.
        review_loop._ORIGINAL_MODES.clear()
        # entrypoint.sh, on its way to the exec.
        git_dir = os.path.join(self.repo, ".git")
        os.chmod(git_dir, os.stat(git_dir).st_mode | stat.S_IWUSR)

        log = self._main()
        self.assertNotIn("git fetch failed", log)
        # The repair must not have cost the enforcement.
        self.assertFalse(self._can_write())

    def test_a_restart_with_concurrency_off_can_still_fetch(self):
        # The same inherited lock, on a run that will not lock anything itself.
        # It has the same frozen clone and the same silent WARN, so the repair
        # cannot be conditional on this run's concurrency.
        self._with_upstream()
        review_loop.lock_git_dir(self.repo)
        review_loop._ORIGINAL_MODES.clear()

        log = self._main(PERSONAS="red_team", PLAN_PERSONAS="red_team")
        self.assertNotIn("git fetch failed", log)
        self.assertTrue(self._can_write())

    def test_a_restart_leaves_the_owner_able_to_write_every_locked_dir(self):
        # The mode half of the sequence, and the honest limit of the repair: a
        # 775 .git comes back 755 and stays there. Nothing carries 775 across a
        # process boundary, and the fallback adds the owner bit only, which
        # never loosens what it found.
        git_dir = os.path.join(self.repo, ".git")
        os.chmod(git_dir, 0o775)
        review_loop.lock_git_dir(self.repo)
        review_loop._ORIGINAL_MODES.clear()
        os.chmod(git_dir, os.stat(git_dir).st_mode | stat.S_IWUSR)

        self._main(PERSONAS="red_team", PLAN_PERSONAS="red_team")
        for path in review_loop._locked_dirs(self.repo):
            self.assertTrue(os.access(path, os.W_OK), path)
        self.assertEqual(stat.S_IMODE(os.stat(git_dir).st_mode), 0o755)

    def test_a_single_persona_run_leaves_it_writable(self):
        # Nothing runs beside it, so there is nothing to defend against. BOTH
        # modes have to be narrowed: one clone serves both, so a six-persona
        # plan mode locks it even on a cycle whose candidates are all code.
        self._main(PERSONAS="red_team", PLAN_PERSONAS="red_team")
        self.assertTrue(self._can_write())

    def test_a_clone_that_cannot_be_locked_kills_the_run(self):
        # The same ERROR-on-stderr, exit 1 every other startup failure gives,
        # rather than six personas against a tree with no enforcement.
        shutil.rmtree(os.path.join(self.repo, ".git"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                self._main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("no .git at", err.getvalue())

    def test_a_concurrent_plan_mode_alone_locks_it(self):
        # The lock is a property of the clone, not of a mode, and a plan PR can
        # arrive on any cycle. Narrowing only code mode must not disarm it.
        self._main(PERSONAS="red_team")
        self.assertFalse(self._can_write())


if __name__ == "__main__":
    unittest.main()
