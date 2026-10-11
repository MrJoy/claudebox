import _path  # noqa: F401

# Reachability ledger: sharing rejection goes through the real CLI and resolves
# an org-supplied valid profile first. Its positive control changes only repo-name
# and observes the effective model plus the deferred private-repo check.
# Mock ledger: no mocks; isolated temporary JSON files and real loaders/checks.

import io
import json
import os
import shutil
import tempfile
import unittest


import config_cli

SHIPPED_PERSONAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "personas")

DEPLOY = {"profiles": {"ollama": {
    "provider": "ollama", "models": ["glm-5.2:cloud"], "default_model": "glm-5.2:cloud",
    "credential": "OLLAMA_API_KEY", "budget": {"unit": "passes", "daily": 100}}}}


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, content):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content if isinstance(content, str) else json.dumps(content))
        return path

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        rc = config_cli.main(list(args), stdout=out, stderr=err, persona_dir=SHIPPED_PERSONAS)
        return rc, out.getvalue(), err.getvalue()

    def test_a_valid_file_exits_zero_and_prints_the_effective_config(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 865.
        # Mutation: Omit the deployment-not-checked disclosure on schema-only success.
        rc, out, err = self.run_cli("validate", self.write("r.json", {"trigger": "requested"}))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("OK: config is valid (source: repo; deployment not checked)", out)
        self.assertIn("  trigger: requested", out)
        self.assertIn("  profile: -", out)
        self.assertIn("NOT CHECKED HERE: profile, model, team and kindex.store", out)

    def test_an_invalid_file_exits_one_with_each_problem_on_stderr(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 873.
        # Mutation: Return success for invalid repo input because defaults exist.
        rc, out, err = self.run_cli("validate", self.write("r.json", {"trigger": "x", "team": ""}))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: repo file: trigger:", err)
        self.assertIn("ERROR: repo file: team:", err)
        self.assertIn("falls back to the built-in defaults", err)
        self.assertNotIn("OK:", out)

    def test_an_invalid_file_with_a_valid_org_names_the_org_fallback(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 881.
        # Mutation: Misreport an org fallback as a built-in fallback.
        rc, _, err = self.run_cli("validate", self.write("r.json", {"trigger": "x"}),
                                  "--org", self.write("o.json", {}))
        self.assertEqual(rc, 1)
        self.assertIn("falls back to the org file", err)

    def test_both_invalid_says_not_reviewed(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 887.
        # Mutation: Claim review proceeds when both files are invalid.
        rc, _, err = self.run_cli("validate", self.write("r.json", {"trigger": "x"}),
                                  "--org", self.write("o.json", {"trigger": "y"}))
        self.assertEqual(rc, 1)
        self.assertIn("is not reviewed", err)

    def test_both_invalid_with_a_deploy_file_says_not_reviewed_and_checks_nothing(self):
        # PR #17 review: the guard that keeps against_deploy away from a None
        # config had no test, and without it this command raised.
        rc, out, err = self.run_cli("validate", self.write("r.json", {"trigger": "x"}),
                                    "--org", self.write("o.json", {"trigger": "y"}),
                                    "--deploy", self.write("d.json", DEPLOY))
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("is not reviewed", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("against deploy file", err)

    def test_a_hostile_key_cannot_put_a_control_sequence_on_the_terminal(self):
        # PR #17 review: unknown-key names were echoed raw, and SGR 8 hid the
        # real verdict behind a forged one.
        rc, out, err = self.run_cli("validate", self.write("r.json", {
            "\x1b[2K\x1b[8m": 1, "\x1b[28mOK: config is valid (source: repo+org)": 1}))
        self.assertEqual(rc, 1)
        self.assertNotIn("\x1b", out + err)
        self.assertIn("\\x1b[8m: unknown key", err)

    def test_a_bidi_key_is_escaped_through_both_layers(self):
        # PR #17 review: the \u arm of printable had no input reaching it, and
        # say()'s own escaping could be deleted with nothing failing. An
        # unknown key goes through Problems.lines(); a duplicate key's message
        # comes from read_json and reaches the terminal through say() alone.
        rc, out, err = self.run_cli("validate", self.write("r.json", {"\u202e": 1}))
        self.assertEqual(rc, 1)
        self.assertNotIn("\u202e", out + err)
        self.assertIn("\\u202e: unknown key", err)
        rc, out, err = self.run_cli("validate", self.write("d.json", '{"\u202e": 1, "\u202e": 2}'))
        self.assertEqual(rc, 1)
        self.assertNotIn("\u202e", out + err)
        self.assertIn("duplicate key '\\u202e'", err)

    def test_an_invalid_org_under_a_valid_repo_is_still_exit_one(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 893.
        # Mutation: Ignore errors in an org file underneath a valid repo.
        rc, _, err = self.run_cli("validate", self.write("r.json", {}),
                                  "--org", self.write("o.json", {"trigger": "y"}))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: org file: trigger:", err)

    def test_an_unreadable_file_is_exit_one_not_a_traceback(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 899.
        # Mutation: Let missing repo input escape as a filesystem exception.
        rc, _, err = self.run_cli("validate", os.path.join(self.dir, "missing.json"))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: cannot read repo file", err)

    def test_deploy_check_reports_the_effective_model(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 904.
        # Mutation: Omit the effective-model line on deployment validation.
        rc, out, err = self.run_cli("validate", self.write("r.json", {"profile": "ollama"}),
                                    "--deploy", self.write("d.json", DEPLOY))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("OK: config is valid (source: repo)\n", out)
        self.assertIn("  effective model: glm-5.2:cloud", out)
        self.assertNotIn("deployment not checked", out)

    def test_deploy_check_uses_the_org_profile(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 912.
        # Mutation: Validate the sparse repo alone instead of merged org profile.
        rc, _, err = self.run_cli("validate", self.write("r.json", {}),
                                  "--org", self.write("o.json", {"profile": "ollama"}),
                                  "--deploy", self.write("d.json", DEPLOY))
        self.assertEqual((rc, err), (0, ""))

    def test_deploy_problems_are_exit_one(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 918.
        # Mutation: Ignore against-deployment problems when deciding the exit code.
        rc, _, err = self.run_cli("validate", self.write("r.json", {"profile": "gpt"}),
                                  "--deploy", self.write("d.json", DEPLOY))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: against deploy file: profile: 'gpt'", err)

    def test_an_invalid_deploy_file_is_exit_one(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 924.
        # Mutation: Accept an invalid deployment file.
        rc, _, err = self.run_cli("validate", self.write("r.json", {}),
                                  "--deploy", self.write("d.json", {"profiles": {}}))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: deploy file: profiles", err)

    def test_unchecked_items_are_printed(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 930.
        # Mutation: Drop unchecked team disclosures from CLI output.
        rc, out, _ = self.run_cli("validate", self.write("r.json", {"profile": "ollama", "team": "rev"}),
                                  "--deploy", self.write("d.json", DEPLOY))
        self.assertEqual(rc, 0)
        self.assertIn("NOT CHECKED HERE: team 'rev' exists", out)

    def test_no_subcommand_is_a_usage_error(self):
        # Oracle: tester-brief.md, Task 6, Interfaces and signed starting oracle line 936.
        # Mutation: Accept an empty command line instead of raising usage error.
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli()
        self.assertEqual(ctx.exception.code, 2)


class CliAcceptanceTest(unittest.TestCase):
    setUp = CliTest.setUp
    write = CliTest.write
    run_cli = CliTest.run_cli
    def test_cli_rejects_store_access_after_resolving_org_profile(self):
        # Oracle: Task 5 sharing errors; Task 6 prefixes and effective config; decision 5.
        # Mutation: CLI omits against_deploy or ignores repo-name when checking sharing.
        d = dict(DEPLOY, kindex={"org_store": {"share_with": ["api"]}})
        repo = self.write("repo.json", {"kindex": {"store": "org"}})
        org = self.write("org.json", {"profile": "ollama"})
        dep = self.write("deploy.json", d)
        rc, out, err = self.run_cli("validate", repo, "--org", org, "--deploy", dep,
                                    "--repo-name", "acme/web")
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: against deploy file: kindex.store: org, but the org store is not shared with 'web'", err)
        self.assertNotIn("OK:", out)
        rc, out, err = self.run_cli("validate", repo, "--org", org, "--deploy", dep,
                                    "--repo-name", "acme/api")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("  effective model: glm-5.2:cloud", out)
        self.assertIn("NOT CHECKED HERE: the repo is private", out)

    def test_each_optional_file_read_error_names_its_role(self):
        # Oracle: Task 6 unreadable FILE/--org/--deploy -> exit 1 with role.
        # Mutation: silently ignore missing optional file or report wrong role.
        repo = self.write("repo.json", {})
        for flag, role in (("--org", "org file"), ("--deploy", "deploy file")):
            with self.subTest(flag=flag):
                rc, out, err = self.run_cli("validate", repo, flag, os.path.join(self.dir, "absent"))
                self.assertEqual(rc, 1)
                self.assertIn("ERROR: cannot read " + role, err)
                self.assertNotIn("OK:", out)
