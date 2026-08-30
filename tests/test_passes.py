import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

import _path  # noqa: F401

import passes
from common import Pair


class UsageLimitTest(unittest.TestCase):
    def test_hand_written_429(self):
        self.assertTrue(passes.is_usage_limit("API Error: 429 rate limit exceeded"))

    def test_captured_claude_code_wording(self):
        # CAPTURED, NOT COMPOSED. Claude Code's own message when the account
        # allowance is exhausted, epoch and all. Do not paraphrase it: an
        # upstream rewording is precisely what this fixture exists to turn red.
        self.assertTrue(passes.is_usage_limit("Claude AI usage limit reached|1755772800"))

    def test_near_miss_wording_without_429_or_rate_limit(self):
        self.assertTrue(passes.is_usage_limit("5-hour limit reached, resets at 3pm"))

    def test_reached_your_limit(self):
        self.assertTrue(passes.is_usage_limit("you have reached your limit"))

    def test_529_overloaded(self):
        self.assertTrue(passes.is_usage_limit("API Error: 529"))

    def test_quota(self):
        self.assertTrue(passes.is_usage_limit("quota exhausted"))

    def test_429_on_its_own_line_in_a_long_stderr(self):
        # THE REGRESSION THIS MODULE'S PORTING HAZARD IS ABOUT. grep is
        # line-oriented; Python's ^ and $ anchor the whole string without
        # re.MULTILINE. Without the flag this stops matching and a real limit
        # degrades silently to the ordinary drop-the-session path.
        stderr = "connecting\nnegotiating\n429\nretrying\n"
        self.assertTrue(passes.is_usage_limit(stderr))

    def test_a_spend_cap_is_not_a_rate_cap(self):
        # Limit-SHAPED but outside the pattern on purpose. This is the
        # classifier's negative direction and must stay a miss.
        self.assertFalse(
            passes.is_usage_limit(
                "API Error: 403 Your credit balance is too low to continue"
            )
        )

    def test_ordinary_failure_is_not_a_limit(self):
        self.assertFalse(passes.is_usage_limit("API Error: 400 invalid request"))

    def test_a_bare_4290_is_not_a_429(self):
        self.assertFalse(passes.is_usage_limit("code 4290 something"))

    def test_empty_stderr_is_not_a_limit(self):
        self.assertFalse(passes.is_usage_limit(""))


class UsageLimitLineTest(unittest.TestCase):
    def test_returns_only_the_matching_line(self):
        stderr = "connecting\nAPI Error: 429 rate limit exceeded\nretrying\n"
        self.assertEqual(
            passes.usage_limit_line(stderr), "API Error: 429 rate limit exceeded"
        )

    def test_returns_the_first_match_only(self):
        stderr = "429 first\n429 second\n"
        self.assertEqual(passes.usage_limit_line(stderr), "429 first")

    def test_truncates_to_400_characters(self):
        # claude's stderr is not a stream that can be assumed credential-free,
        # so the log gets the smallest slice that explains the stall.
        stderr = "429 " + ("x" * 1000)
        self.assertEqual(len(passes.usage_limit_line(stderr)), 400)

    def test_no_match_is_empty(self):
        self.assertEqual(passes.usage_limit_line("all fine"), "")


class FormatEventTest(unittest.TestCase):
    def test_init_event(self):
        got = passes.format_event(
            {"type": "system", "subtype": "init", "session_id": "abc"}
        )
        self.assertEqual(got, ["  ▸ session abc started"])

    def test_assistant_text(self):
        got = passes.format_event(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}}
        )
        self.assertEqual(got, ["hello"])

    def test_multiline_assistant_text_becomes_multiple_lines(self):
        # Each gets its own pair prefix at emit time, so a wrapped paragraph
        # stays attributable when passes overlap.
        got = passes.format_event(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "a\nb"}]}}
        )
        self.assertEqual(got, ["a", "b"])

    def test_empty_assistant_text_is_dropped(self):
        got = passes.format_event(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": ""}]}}
        )
        self.assertEqual(got, [])

    def test_tool_use(self):
        got = passes.format_event(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}}]
                },
            }
        )
        self.assertEqual(got, ['  → Bash: {"cmd": "ls"}'])

    def test_tool_use_input_is_truncated_to_200(self):
        big = {"cmd": "x" * 500}
        got = passes.format_event(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash", "input": big}]},
            }
        )
        self.assertEqual(len(got[0]), len("  → Bash: ") + 200)

    def test_tool_result_whitespace_is_squashed(self):
        got = passes.format_event(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": [{"text": "a\n\n  b\tc"}]}
                    ]
                },
            }
        )
        self.assertEqual(got, ["  ← a b c"])

    def test_result_event(self):
        got = passes.format_event(
            {"type": "result", "subtype": "success", "result": "done"}
        )
        self.assertEqual(got, ["  ✓ result (success): done"])

    def test_unknown_event_is_silent(self):
        self.assertEqual(passes.format_event({"type": "ping"}), [])

    def test_a_zero_result_is_not_swallowed(self):
        # jq's `//` falls back on null or false only. Python's `or` would also
        # swallow 0, logging an empty string where the shell logged "0".
        got = passes.format_event(
            {"type": "result", "subtype": "success", "result": 0}
        )
        self.assertEqual(got, ["  \u2713 result (success): 0"])

    def test_a_dict_tool_result_is_json_encoded(self):
        # jq's tostring emits JSON. Python's str() would emit single quotes.
        got = passes.format_event(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": {"a": 1}}]},
            }
        )
        self.assertEqual(got, ['  \u2190 {"a": 1}'])


class RunPassTest(unittest.TestCase):
    """Drives run_pass against a real subprocess: a tiny python script standing
    in for `claude`. Real pipes, because the streaming and exit-code handling is
    what this function is for and a mock would prove nothing about either."""

    def _fake_claude(self, body):
        fd, path = tempfile.mkstemp(suffix=".py")
        os.write(fd, textwrap.dedent(body).encode())
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def _run(self, script, **kwargs):
        def popen(argv, **pk):
            return subprocess.Popen([sys.executable, script], **pk)

        return passes.run_pass(
            pair=Pair(12, "code", "sage"),
            prompt="review it",
            session_id=kwargs.pop("session_id", None),
            persona_prompt="you are red team",
            model="glm-5.2:cloud",
            mcp_args=["--strict-mcp-config"],
            cwd=".",
            popen=popen,
            **kwargs,
        )

    def test_successful_pass_recovers_the_session_id(self):
        script = self._fake_claude(
            """
            import json, sys
            print(json.dumps({"type": "system", "subtype": "init", "session_id": "S1"}))
            print(json.dumps({"type": "result", "subtype": "success",
                              "session_id": "S1", "result": "ok"}))
            """
        )
        got = self._run(script)
        self.assertEqual(got.rc, 0)
        self.assertEqual(got.session_id, "S1")
        self.assertFalse(got.limited)

    def test_last_session_id_wins(self):
        script = self._fake_claude(
            """
            import json
            print(json.dumps({"type": "system", "subtype": "init", "session_id": "S1"}))
            print(json.dumps({"type": "result", "subtype": "success", "session_id": "S2"}))
            """
        )
        self.assertEqual(self._run(script).session_id, "S2")

    def test_session_id_is_recovered_before_the_exit_code_is_judged(self):
        # A pass that started a session and THEN hit a limit still has a
        # resumable session. Recovering the id after checking rc would throw it
        # away, and the usage-limit path depends on knowing it.
        script = self._fake_claude(
            """
            import json, sys
            print(json.dumps({"type": "system", "subtype": "init", "session_id": "S1"}))
            sys.stderr.write("API Error: 429 rate limit exceeded\\n")
            sys.exit(1)
            """
        )
        got = self._run(script)
        self.assertEqual(got.rc, 1)
        self.assertEqual(got.session_id, "S1")
        self.assertTrue(got.limited)
        self.assertIn("429", got.limit_line)

    def test_failure_before_a_session_exists(self):
        script = self._fake_claude(
            """
            import sys
            sys.stderr.write("API Error: 429 rate limit exceeded\\n")
            sys.exit(1)
            """
        )
        got = self._run(script)
        self.assertIsNone(got.session_id)
        self.assertTrue(got.limited)

    def test_non_limit_failure_is_not_classified_as_one(self):
        script = self._fake_claude(
            """
            import sys
            sys.stderr.write("API Error: 400 invalid request\\n")
            sys.exit(1)
            """
        )
        got = self._run(script)
        self.assertEqual(got.rc, 1)
        self.assertFalse(got.limited)
        self.assertEqual(got.limit_line, "")

    def test_a_resumed_pass_keeps_the_session_id_when_none_is_reported(self):
        script = self._fake_claude("import sys; sys.exit(0)")
        got = self._run(script, session_id="S-prior")
        self.assertEqual(got.session_id, "S-prior")

    def test_unparseable_stream_lines_are_ignored(self):
        script = self._fake_claude(
            """
            import json
            print("not json at all")
            print(json.dumps({"type": "result", "subtype": "success", "session_id": "S1"}))
            """
        )
        self.assertEqual(self._run(script).session_id, "S1")


class ArgvTest(unittest.TestCase):
    def test_new_session_argv(self):
        argv = passes.build_argv(
            session_id=None,
            model="glm-5.2:cloud",
            persona_prompt="you are red team",
            mcp_args=["--strict-mcp-config"],
            prompt="review it",
        )
        self.assertNotIn("--resume", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--append-system-prompt", argv)
        self.assertEqual(argv[-1], "review it")
        self.assertEqual(argv[-2], "--")

    def test_resumed_session_argv_still_carries_the_persona(self):
        # --append-system-prompt does NOT survive --resume (measured 2026-08-21),
        # so it is re-passed on every invocation. This is the single most
        # important property of the persona design.
        argv = passes.build_argv(
            session_id="S1",
            model="glm-5.2:cloud",
            persona_prompt="you are red team",
            mcp_args=["--strict-mcp-config"],
            prompt="re-check it",
        )
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "S1")
        self.assertIn("--append-system-prompt", argv)
        self.assertEqual(argv[argv.index("--append-system-prompt") + 1], "you are red team")

    def test_double_dash_precedes_the_prompt(self):
        # --mcp-config is variadic, so without the -- the CLI parses the prompt
        # as another config path.
        argv = passes.build_argv(
            session_id=None,
            model="m",
            persona_prompt="p",
            mcp_args=["--strict-mcp-config", "--mcp-config", "/home/r/mcp.json"],
            prompt="the prompt",
        )
        self.assertEqual(argv[-2:], ["--", "the prompt"])


if __name__ == "__main__":
    unittest.main()
