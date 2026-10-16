import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

import _path  # noqa: F401

import codex_pass
from common import Pair

try:
    import tomllib
except ImportError:  # Python < 3.11 on a developer's host; the image has 3.11.
    tomllib = None

FIXED_TAIL = [
    "-c", "allow_provider_model_fallback=false",
    "-c", "check_for_update_on_startup=false",
]


class ArgvTest(unittest.TestCase):
    def test_fresh_pass(self):
        self.assertEqual(
            codex_pass.build_argv(None, "gpt-6.1-sol", "P", "review it"),
            ["codex", "exec", "--json", "--ignore-user-config",
             "--dangerously-bypass-approvals-and-sandbox",
             "-m", "gpt-6.1-sol",
             "-c", "developer_instructions=" + codex_pass.toml_string("P")]
            + FIXED_TAIL + ["--", "review it"],
        )

    def test_resumed_pass(self):
        argv = codex_pass.build_argv("T1", "gpt-6.1-sol", "P", "review it")
        self.assertEqual(argv[:4], ["codex", "exec", "resume", "--json"])
        self.assertEqual(argv[-3:], ["--", "T1", "review it"])

    def test_resumed_pass_still_carries_the_persona_and_the_lockdown(self):
        # --append-system-prompt does not survive a Claude --resume, and nothing
        # proved developer_instructions survives a Codex one. The repo-config
        # lockdown has to be on every invocation for the same reason.
        argv = codex_pass.build_argv("T1", "gpt-6.1-sol", "P", "review it")
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("developer_instructions=" + codex_pass.toml_string("P"), argv)

    def test_never_bypasses_hook_trust(self):
        for sid in (None, "T1"):
            self.assertNotIn("--dangerously-bypass-hook-trust",
                             codex_pass.build_argv(sid, "m", "P", "x"))

    def test_a_dash_leading_prompt_follows_the_separator(self):
        fresh = codex_pass.build_argv(None, "m", "P", "--- x")
        self.assertEqual(fresh[fresh.index("--") + 1], "--- x")
        resumed = codex_pass.build_argv("T1", "m", "P", "--- x")
        self.assertEqual(resumed[resumed.index("--") + 1:], ["T1", "--- x"])


class TomlStringTest(unittest.TestCase):
    def test_escapes(self):
        self.assertEqual(codex_pass.toml_string('a"b\\c\nd\te'), '"a\\"b\\\\c\\nd\\te"')

    @unittest.skipUnless(tomllib, "tomllib needs Python 3.11")
    def test_non_ascii_survives(self):
        text = "é—✓ \"quoted\"\n\tand a backslash \\"
        self.assertEqual(tomllib.loads("x = " + codex_pass.toml_string(text))["x"], text)

    def test_control_characters_are_escaped(self):
        self.assertEqual(codex_pass.toml_string("\x01"), '"\\u0001"')
        self.assertEqual(codex_pass.toml_string("\x7f"), '"\\u007F"')


class FormatEventTest(unittest.TestCase):
    def test_thread_started(self):
        self.assertEqual(
            codex_pass.format_event({"type": "thread.started", "thread_id": "T1"}),
            ["  ▸ session T1 started"])

    def test_agent_message_splits_lines(self):
        event = {"type": "item.completed",
                 "item": {"type": "agent_message", "text": "one\ntwo"}}
        self.assertEqual(codex_pass.format_event(event), ["one", "two"])

    def test_empty_agent_message_is_dropped(self):
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": ""}}
        self.assertEqual(codex_pass.format_event(event), [])

    def test_command_execution(self):
        event = {"type": "item.completed", "item": {
            "type": "command_execution", "command": "git log -1",
            "aggregated_output": "abc  init\n\n", "exit_code": 0}}
        self.assertEqual(codex_pass.format_event(event),
                         ["  → git log -1", "  ← abc init"])

    def test_command_execution_truncates(self):
        event = {"type": "item.completed", "item": {
            "type": "command_execution", "command": "c" * 300,
            "aggregated_output": "o" * 300}}
        out = codex_pass.format_event(event)
        self.assertEqual(out[0], "  → " + "c" * 200)
        self.assertEqual(out[1], "  ← " + "o" * 200)

    def test_item_started_is_silent(self):
        event = {"type": "item.started", "item": {"type": "command_execution", "command": "x"}}
        self.assertEqual(codex_pass.format_event(event), [])

    def test_turn_completed(self):
        event = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 0}}
        self.assertEqual(codex_pass.format_event(event),
                         ["  ✓ turn complete (in 10, out 0 tokens)"])

    def test_unknown_event_is_silent(self):
        self.assertEqual(codex_pass.format_event({"type": "turn.started"}), [])


class ErrorTextTest(unittest.TestCase):
    def test_turn_failed(self):
        self.assertEqual(
            codex_pass.error_text({"type": "turn.failed", "error": {"message": "boom"}}),
            "boom")

    def test_error_item(self):
        event = {"type": "item.completed", "item": {"type": "error", "message": "bad"}}
        self.assertEqual(codex_pass.error_text(event), "bad")

    def test_error_event(self):
        self.assertEqual(codex_pass.error_text({"type": "error", "message": "nope"}), "nope")

    def test_a_reconnecting_retry_is_not_a_failure(self):
        event = {"type": "error", "message": "Reconnecting... 2/5 (unexpected status 429)"}
        self.assertEqual(codex_pass.error_text(event), "")

    def test_review_prose_is_never_error_text(self):
        event = {"type": "item.completed",
                 "item": {"type": "agent_message", "text": "you've hit your usage limit"}}
        self.assertEqual(codex_pass.error_text(event), "")


class SwitchedModelTest(unittest.TestCase):
    def test_nested_previous_and_current_model(self):
        event = {"type": "x", "data": {"previous_model": "gpt-6.1-sol",
                                       "current_model": "gpt-6-luna"}}
        self.assertEqual(codex_pass.switched_model(event), "gpt-6-luna")

    def test_same_model_is_not_a_switch(self):
        event = {"previous_model": "a", "current_model": "a"}
        self.assertEqual(codex_pass.switched_model(event), "")

    def test_switch_announced_in_a_message(self):
        event = {"type": "error",
                 "message": "Automatically switched to gpt-6-luna due to usage limits."}
        self.assertEqual(codex_pass.switched_model(event), "gpt-6-luna")

    def test_ordinary_event(self):
        self.assertEqual(codex_pass.switched_model({"type": "turn.started"}), "")


class RunPassTest(unittest.TestCase):
    """Drives run_pass against a real subprocess, as tests/test_passes.py does."""

    def _fake_codex(self, body):
        fd, path = tempfile.mkstemp(suffix=".py")
        os.write(fd, textwrap.dedent(body).encode())
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def _run(self, script, session_id=None):
        seen = {}

        def popen(argv, **pk):
            seen.update(pk)
            return subprocess.Popen([sys.executable, script], **pk)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = codex_pass.run_pass(
                pair=Pair(12, "code", "sage"),
                prompt="review it",
                session_id=session_id,
                persona_prompt="you are red team",
                model="gpt-6.1-sol",
                cwd=".",
                popen=popen,
            )
        return result, out.getvalue(), seen

    def _events(self, *events, rc=0, stderr=""):
        lines = "\n".join(f"print(json.dumps({e!r}))" for e in events)
        return self._fake_codex(
            f"import json, sys\n{lines}\nsys.stderr.write({stderr!r})\nsys.exit({rc})\n")

    def test_success_recovers_the_thread_id(self):
        got, _, _ = self._run(self._events(
            {"type": "thread.started", "thread_id": "T1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
        self.assertEqual((got.rc, got.session_id, got.limited, got.switched),
                         (0, "T1", False, False))

    def test_resume_keeps_the_given_id_when_no_thread_event(self):
        got, _, _ = self._run(self._events({"type": "turn.completed", "usage": {}}),
                              session_id="T0")
        self.assertEqual(got.session_id, "T0")

    def test_usage_limit_in_turn_failed(self):
        msg = ("You've hit your usage limit. Upgrade to Plus to continue using Codex "
               "(https://chatgpt.com/explore/plus), or try again at 3:00 PM.")
        got, _, _ = self._run(self._events(
            {"type": "thread.started", "thread_id": "T1"},
            {"type": "turn.failed", "error": {"message": msg}}, rc=1))
        self.assertNotEqual(got.rc, 0)
        self.assertTrue(got.limited)
        self.assertIn("usage limit", got.limit_line)
        self.assertEqual(got.session_id, "T1")

    def test_turn_failed_with_exit_zero_is_a_failure(self):
        got, _, _ = self._run(self._events(
            {"type": "turn.failed", "error": {"message": "something broke"}}, rc=0))
        self.assertEqual(got.rc, 1)
        self.assertFalse(got.limited)

    def test_reconnecting_429_then_success_is_not_a_limit(self):
        got, _, _ = self._run(self._events(
            {"type": "error",
             "message": "Reconnecting... 2/5 (unexpected status 429 Too Many Requests)"},
            {"type": "turn.completed", "usage": {}}))
        self.assertEqual(got.rc, 0)
        self.assertFalse(got.limited)

    def test_review_prose_about_limits_is_not_a_limit(self):
        got, _, _ = self._run(self._events(
            {"type": "item.completed", "item": {
                "type": "agent_message",
                "text": "This retry loop ignores a 429 and the usage limit header."}},
            {"type": "turn.completed", "usage": {}}))
        self.assertEqual(got.rc, 0)
        self.assertFalse(got.limited)

    def test_limit_on_stderr_only(self):
        got, _, _ = self._run(self._events(rc=1, stderr="You've hit your usage limit.\n"))
        self.assertNotEqual(got.rc, 0)
        self.assertTrue(got.limited)

    def test_a_model_switch_is_reported(self):
        got, log, _ = self._run(self._events(
            {"type": "x", "previous_model": "gpt-6.1-sol", "current_model": "gpt-6-luna"},
            {"type": "turn.completed", "usage": {}}))
        self.assertEqual(got.rc, 0)
        self.assertTrue(got.switched)
        self.assertIn("WARN", log)
        self.assertIn("gpt-6.1-sol", log)
        self.assertIn("gpt-6-luna", log)

    def test_stdin_is_closed(self):
        script = self._fake_codex(
            """
            import json, sys
            if sys.stdin.read() == "":
                print(json.dumps({"type": "turn.completed", "usage": {}}))
            else:
                sys.exit(3)
            """
        )
        got, _, seen = self._run(script)
        self.assertIs(seen.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(got.rc, 0)


if __name__ == "__main__":
    unittest.main()
