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

FIXED_TAIL = ["-c", "check_for_update_on_startup=false"]


class ArgvTest(unittest.TestCase):
    def test_fresh_pass(self):
        self.assertEqual(
            codex_pass.build_argv(None, "gpt-6.1-sol", "P", "review it"),
            ["codex", "exec", "--json", "--strict-config", "--ignore-user-config",
             "--dangerously-bypass-approvals-and-sandbox",
             "-m", "gpt-6.1-sol",
             "-c", "developer_instructions=" + codex_pass.toml_string("P")]
            + FIXED_TAIL + ["--", "review it"],
        )

    def test_mcp_args_come_before_the_separator_on_both_shapes(self):
        mcp = ["-c", "mcp_servers.k.required=true"]
        for sid in (None, "T1"):
            argv = codex_pass.build_argv(sid, "m", "P", "x", mcp_args=mcp)
            i = argv.index("mcp_servers.k.required=true")
            self.assertLess(i, argv.index("--"))
            self.assertEqual(argv[i - 1], "-c")

    def test_no_mcp_args_is_todays_argv(self):
        self.assertEqual(codex_pass.build_argv(None, "m", "P", "x"),
                         codex_pass.build_argv(None, "m", "P", "x", mcp_args=()))

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

    def test_no_setting_codex_does_not_have(self):
        # allow_provider_model_fallback is a model-catalog field, not config:
        # without --strict-config codex ignored it silently, and with it every
        # pass would fail. --strict-config is what turns the next such mistake
        # into a loud failure rather than a guard that is not there.
        for sid in (None, "T1"):
            argv = codex_pass.build_argv(sid, "m", "P", "x")
            self.assertIn("--strict-config", argv)
            self.assertFalse(any("allow_provider_model_fallback" in a for a in argv))

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


class TomlArrayTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(codex_pass.toml_array([]), "[]")

    def test_round_trips(self):
        if tomllib is None:
            self.skipTest("tomllib needs Python 3.11")
        items = ['a"b', "c\\d", "e f"]
        self.assertEqual(tomllib.loads("x = " + codex_pass.toml_array(items))["x"], items)


class McpFailureLineTest(unittest.TestCase):
    HELPER = ('2026-10-17T06:23:16.357451Z ERROR rmcp::transport::worker: worker quit with fatal: '
              'Transport channel closed, when Client(HttpRequest(HttpRequest("MCP HTTP headers '
              'helper exited with status exit status: 1")))')
    AUTH = ('2026-10-17T06:23:15.376237Z ERROR rmcp::transport::worker: worker quit with fatal: '
            'Transport channel closed, when AuthRequired(AuthRequiredError { www_authenticate_header: '
            '"Bearer realm=\\"OAuth\\"" })')

    def test_captured_helper_failure(self):
        self.assertEqual(codex_pass.mcp_failure_line("Reading additional input from stdin...\n" + self.HELPER),
                         self.HELPER)

    def test_captured_auth_failure(self):
        self.assertEqual(codex_pass.mcp_failure_line(self.AUTH), self.AUTH)

    def test_truncated(self):
        self.assertEqual(len(codex_pass.mcp_failure_line(self.HELPER + "x" * 500)), 400)

    def test_ordinary_stderr(self):
        self.assertEqual(codex_pass.mcp_failure_line("Reading additional input from stdin...\n"), "")


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

    def test_the_phrase_in_command_output_is_not_a_switch(self):
        # A pass reading a diff that contains this very phrase (this file
        # does) must not end the cycle on a switch that never happened.
        event = {"type": "item.completed", "item": {
            "type": "command_execution", "command": "gh pr diff 7",
            "aggregated_output": "+ Automatically switched to gpt-6-luna due to usage limits."}}
        self.assertEqual(codex_pass.switched_model(event), "")

    def test_the_phrase_in_review_text_is_not_a_switch(self):
        event = {"type": "item.completed", "item": {
            "type": "agent_message",
            "text": "The regex matches 'Automatically switched to (.+?) due to'."}}
        self.assertEqual(codex_pass.switched_model(event), "")

    def test_an_error_item_announcing_a_switch_is_one(self):
        event = {"type": "item.completed", "item": {
            "type": "error", "message": "Automatically switched to gpt-6-luna due to usage limits."}}
        self.assertEqual(codex_pass.switched_model(event), "gpt-6-luna")


class RunPassTest(unittest.TestCase):
    """Drives run_pass against a real subprocess, as tests/test_passes.py does."""

    def _fake_codex(self, body):
        fd, path = tempfile.mkstemp(suffix=".py")
        os.write(fd, textwrap.dedent(body).encode())
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def _run(self, script, session_id=None, mcp_args=()):
        seen = {}

        def popen(argv, **pk):
            seen.update(pk)
            seen["argv"] = argv
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
                mcp_args=mcp_args,
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

    def test_an_auth_failure_names_codex_login(self):
        # codex login status passes any auth.json, so a revoked login first
        # shows itself here. The hint is what turns a run of failed passes into
        # something an operator can act on.
        msg = ("Your access token could not be refreshed because your refresh token "
               "was already used. Please log out and sign in again.")
        got, log, _ = self._run(self._events(
            {"type": "turn.failed", "error": {"message": msg}}, rc=1))
        self.assertNotEqual(got.rc, 0)
        self.assertFalse(got.limited)
        self.assertIn("claudebox.sh codex-login", log)

    def test_an_ordinary_failure_does_not_name_codex_login(self):
        got, log, _ = self._run(self._events(
            {"type": "turn.failed", "error": {"message": "unexpected status 400"}}, rc=1))
        self.assertNotIn("codex-login", log)

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

    def test_a_skipped_mcp_server_is_logged_on_success(self):
        got, log, _ = self._run(self._events(
            {"type": "turn.completed", "usage": {}}, stderr=McpFailureLineTest.HELPER + "\n"))
        self.assertEqual(got.rc, 0)
        self.assertIn("an MCP server did not start", log)
        self.assertIn("MCP HTTP headers helper exited", log)

    def test_mcp_args_reach_popen(self):
        mcp = ["-c", "mcp_servers.k.required=true"]
        _, _, seen = self._run(self._events({"type": "turn.completed", "usage": {}}),
                               mcp_args=mcp)
        self.assertIn("mcp_servers.k.required=true", seen["argv"])
