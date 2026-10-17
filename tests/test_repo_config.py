import _path  # noqa: F401

# Handoff mock ledger (LocalStartupAcceptanceTest only): patch.dict supplies a
# complete minimal local environment; subprocess.run returns CompletedProcess
# with explicit rc/stdout/stderr to stand in for successful git fetch; GitHub
# enumeration returns an explicit empty list. Neither seam is the subject of
# these tests. No MagicMock default determines config acceptance or notice text.
# Reachability: two actual main-loop polls are counted; malformed hosted JSON
# must not prevent them. Notice helper, file existence and log output are real.
# Other tests use real temporary files and pure public config interfaces.
# New feature: no repair red-now/green-now classification claimed. The absent-file
# startup case is a compatibility guard; Validator owns all base/head results.

import os
from dataclasses import fields
import unittest


import repo_config

SHIPPED_PERSONAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "personas")


def check(raw):
    return repo_config.check(raw, SHIPPED_PERSONAS)


class CheckTest(unittest.TestCase):
    def test_an_empty_object_is_valid_and_sets_nothing(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 256.
        # Mutation: Insert defaults into the sparse check result.
        values, problems = check({})
        self.assertEqual(values, {})
        self.assertFalse(problems)

    def test_the_spec_example_is_valid(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 261.
        # Mutation: Drop the checked plan.label leaf.
        values, problems = check({
            "version": 1, "trigger": "auto", "team": "claudebox", "profile": "ollama",
            "model": "glm-5.2:cloud", "plan": {"label": "plan", "personas": "all"},
            "code": {"personas": "red_team,adversarial,sme,sage"},
            "max_concurrent_personas": 0, "rounds": {"cap": 6}, "kindex": {"store": "none"},
        })
        self.assertEqual(problems.lines(), [])
        self.assertEqual(values["plan.label"], "plan")
        self.assertEqual(values["rounds.cap"], 6)
        self.assertNotIn("version", values)

    def test_dollar_schema_is_allowed_and_dropped(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 273.
        # Mutation: Keep $schema in checked leaf values.
        values, problems = check({"$schema": "https://example.test/s.json"})
        self.assertFalse(problems)
        self.assertEqual(values, {})

    def test_an_unknown_top_level_key_is_an_error_with_a_suggestion(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 278.
        # Mutation: Silently discard unknown root keys.
        _, problems = check({"trigers": "auto"})
        self.assertEqual(problems.lines(), ["trigers: unknown key (did you mean 'trigger'?)"])

    def test_an_unknown_nested_key_is_an_error(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 282.
        # Mutation: Silently discard unknown nested keys.
        _, problems = check({"plan": {"labels": "plan"}})
        self.assertIn("plan.labels: unknown key (did you mean 'label'?)", problems.lines())

    def test_kindex_vectors_is_unknown_until_phase_3b(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 286.
        # Mutation: Allow kindex.vectors before phase 3b.
        _, problems = check({"kindex": {"vectors": True}})
        self.assertIn("kindex.vectors: unknown key", problems.lines()[0])

    def test_version_2_is_an_error(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 290.
        # Mutation: Accept arbitrary integer versions.
        _, problems = check({"version": 2})
        self.assertIn("version: unknown version 2; this claudebox reads version 1", problems.lines())

    def test_version_true_is_not_version_1(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 294.
        # Mutation: Accept True as version 1.
        _, problems = check({"version": True})
        self.assertTrue(problems)

    def test_trigger_outside_the_enum_is_an_error(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 298.
        # Mutation: Allow any trigger string.
        _, problems = check({"trigger": "always"})
        self.assertIn("trigger: must be one of auto, requested; got 'always'", problems.lines())

    def test_a_bool_is_not_a_count(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 302.
        # Mutation: Accept True as max_concurrent_personas.
        _, problems = check({"max_concurrent_personas": True})
        self.assertIn("max_concurrent_personas: must be a non-negative integer", problems.lines()[0])

    def test_a_negative_rounds_cap_is_an_error(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 306.
        # Mutation: Allow negative rounds.cap.
        _, problems = check({"rounds": {"cap": -1}})
        self.assertTrue(problems)

    def test_an_unknown_persona_is_an_error_naming_it(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 310.
        # Mutation: Skip persona selector validation.
        _, problems = check({"code": {"personas": "saeg"}})
        self.assertEqual(len(problems.lines()), 1)
        self.assertIn("code.personas: unknown persona 'saeg'", problems.lines()[0])

    def test_personas_must_be_a_string_not_a_list(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 315.
        # Mutation: Coerce persona lists to selector strings.
        _, problems = check({"plan": {"personas": ["sage"]}})
        self.assertIn("plan.personas: must be a persona selector string", problems.lines()[0])

    def test_an_empty_plan_label_is_an_error(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 319.
        # Mutation: Accept a whitespace-only plan label.
        _, problems = check({"plan": {"label": " "}})
        self.assertTrue(problems)

    def test_control_characters_are_refused_in_free_text_values(self):
        # PR #17 review: a model or label carrying ESC, BEL, CR or a C1 or
        # format control validated, then reached the operator's terminal.
        for doc in ({"model": "glm\x1b]0;x\x07"}, {"model": "glm\x08"},
                    {"model": "glm\u202e"}, {"plan": {"label": "plan\rOK: config is valid"}},
                    {"plan": {"label": "plan\x9b2J"}}, {"plan": {"personas": "sage\nred_team"}},
                    {"code": {"personas": "sage\x1b"}}, {"\x1b[2K": 1}):
            with self.subTest(doc=doc):
                _, problems = check(doc)
                self.assertTrue(problems)
                self.assertNotIn("\x1b", "\n".join(problems.lines()))
        _, problems = check({"model": "glm-5.2:cloud", "plan": {"label": "needs plan review"}})
        self.assertFalse(problems)

    def test_a_section_that_is_not_an_object_is_an_error(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 323.
        # Mutation: Ignore a non-object plan section.
        _, problems = check({"plan": "plan"})
        self.assertIn("plan: must be an object", problems.lines()[0])

    def test_kindex_store_outside_the_enum_is_an_error(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 327.
        # Mutation: Accept an arbitrary kindex store name.
        _, problems = check({"kindex": {"store": "global"}})
        self.assertTrue(problems)

    def test_team_with_a_slash_is_an_error(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 331.
        # Mutation: Allow slashes in team names.
        _, problems = check({"team": "org/claudebox"})
        self.assertTrue(problems)

    def test_a_name_with_a_trailing_newline_is_an_error(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 335.
        # Mutation: Use newline-tolerant name matching.
        for key in ("profile", "team"):
            with self.subTest(key=key):
                _, problems = check({key: "ollama\n"})
                self.assertTrue(problems)

    def test_kindex_store_repo_is_unknown_until_phase_3a(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 341.
        # Mutation: Enable repo stores prematurely.
        _, problems = check({"kindex": {"store": "repo"}})
        self.assertIn("kindex.store: must be one of none, org; got 'repo'", problems.lines())

    def test_every_problem_is_reported_at_once(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 345.
        # Mutation: Stop after the first schema problem.
        _, problems = check({"trigger": "x", "team": "", "model": 3})
        self.assertEqual(len(problems.lines()), 3)

    def test_no_key_can_hold_a_secret(self):
        # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 349.
        # Mutation: Add a credential field to the allowed repo key tree.
        flat = []
        def walk(tree, path):
            for k, v in tree.items():
                flat.append(f"{path}{k}")
                if isinstance(v, dict):
                    walk(v, f"{path}{k}.")
        walk(repo_config.KEYS, "")
        for name in flat:
            for word in ("token", "key", "secret", "password", "credential"):
                self.assertNotIn(word, name.lower())


import personas


def b(text):
    return text.encode("utf-8")


class MergeTest(unittest.TestCase):
    def test_nothing_set_is_the_built_in_defaults(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 406.
        # Mutation: Change the built-in trigger to requested.
        cfg = repo_config.merge({}, {})
        self.assertEqual(cfg.trigger, "auto")
        self.assertEqual(cfg.plan_label, "plan")
        self.assertEqual(cfg.code_personas, personas.DEFAULTS["code"])
        self.assertEqual(cfg.rounds_cap, repo_config.DEFAULT_ROUNDS_CAP)
        self.assertEqual(cfg.kindex_store, "none")
        self.assertIsNone(cfg.profile)

    def test_the_repo_overrides_the_org_key_by_key(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 415.
        # Mutation: Apply org leaves after repo leaves.
        cfg = repo_config.merge({"trigger": "requested", "team": "rev"}, {"trigger": "auto"})
        self.assertEqual(cfg.trigger, "auto")
        self.assertEqual(cfg.team, "rev")

    def test_a_partial_nested_override_keeps_the_org_sibling(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 420.
        # Mutation: Drop the org plan.personas sibling on partial override.
        cfg = repo_config.merge(
            {"plan.label": "proposal", "plan.personas": "sage"}, {"plan.label": "rfc"})
        self.assertEqual(cfg.plan_label, "rfc")
        self.assertEqual(cfg.plan_personas, "sage")

    def test_every_leaf_key_has_a_field(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 426.
        # Mutation: Add a key without its dataclass field.
        def leaves(tree, path=""):
            for k, v in tree.items():
                here = f"{path}.{k}" if path else k
                if isinstance(v, dict):
                    yield from leaves(v, here)
                elif k not in ("$schema", "version"):
                    yield here
        names = {f.name for f in fields(repo_config.RepoConfig)}
        for path in leaves(repo_config.KEYS):
            self.assertIn(path.replace(".", "_"), names)

    def test_a_checked_key_with_no_field_raises_rather_than_dropping(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 438.
        # Mutation: Silently discard an unmapped checked key in merge.
        with self.assertRaises(AssertionError):
            repo_config.merge({}, {"kindex.vectors": True})

    def test_a_zero_from_the_repo_overrides_a_nonzero_org_value(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 442.
        # Mutation: Use truthiness instead of presence when overlaying zero.
        cfg = repo_config.merge({"rounds.cap": 6}, {"rounds.cap": 0})
        self.assertEqual(cfg.rounds_cap, 0)


class ResolveTest(unittest.TestCase):
    def resolve(self, org, repo):
        return repo_config.resolve(
            None if org is None else b(org), None if repo is None else b(repo), SHIPPED_PERSONAS)

    def test_both_absent_is_the_defaults(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 452.
        # Mutation: Return no config when both files are absent.
        r = self.resolve(None, None)
        self.assertEqual(r.source, "defaults")
        self.assertEqual(r.config, repo_config.merge({}, {}))

    def test_both_valid_merge(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 457.
        # Mutation: Discard the org team while merging a valid repo.
        r = self.resolve('{"team": "rev"}', '{"trigger": "requested"}')
        self.assertEqual(r.source, "repo+org")
        self.assertEqual((r.config.team, r.config.trigger), ("rev", "requested"))

    def test_an_invalid_repo_falls_back_to_the_org_whole(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 462.
        # Mutation: Salvage valid leaves from an invalid repo rather than discard it whole.
        r = self.resolve('{"team": "rev"}', '{"trigger": "requested", "trigers": 1}')
        self.assertEqual(r.source, "org")
        self.assertEqual(r.config.trigger, "auto")
        self.assertEqual(r.config.team, "rev")
        self.assertTrue(r.repo_problems)

    def test_an_invalid_repo_and_an_invalid_org_is_not_reviewed(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 469.
        # Mutation: Use built-ins when both present files are invalid.
        r = self.resolve('{"trigger": 1}', '{"trigger": 2}')
        self.assertEqual(r.source, "none")
        self.assertIsNone(r.config)
        self.assertTrue(r.repo_problems and r.org_problems)

    def test_an_invalid_org_under_a_valid_repo_applies_the_repo_over_defaults(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 475.
        # Mutation: Block a valid repo when its org file is invalid.
        r = self.resolve('{"trigger": 1}', '{"team": "rev"}')
        self.assertEqual(r.source, "repo")
        self.assertEqual(r.config.team, "rev")
        self.assertTrue(r.org_problems)

    def test_an_invalid_repo_with_no_org_file_is_the_defaults_with_problems(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 481.
        # Mutation: Suppress problems on invalid repo with absent org.
        r = self.resolve(None, '{"trigger": 2}')
        self.assertEqual(r.source, "defaults")
        self.assertEqual(r.config, repo_config.merge({}, {}))
        self.assertTrue(r.repo_problems)

    def test_unparseable_json_is_a_problem_not_an_exception(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 487.
        # Mutation: Let malformed JSON escape resolve as an exception.
        r = self.resolve(None, "{")
        self.assertTrue(r.repo_problems)
        self.assertTrue(r.repo_problems[0].startswith("repo file: "))

    def test_a_repo_file_over_64_kb_is_refused(self):
        # Oracle: tester-brief.md, Task 3, Interfaces and signed starting oracle line 492.
        # Mutation: Omit the repo file size cap.
        r = self.resolve(None, '{"team": "' + "a" * 70000 + '"}')
        self.assertIn("65536 bytes", r.repo_problems[0])


import shutil
import tempfile


class LocalModeNoticeTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_no_file_no_notice(self):
        # Oracle: tester-brief.md, Task 9, Interfaces and signed starting oracle line 1203.
        # Mutation: Return a notice when no hosted file exists.
        self.assertIsNone(repo_config.local_mode_notice(self.repo))

    def test_the_file_produces_the_notice_naming_the_env_file(self):
        # Oracle: tester-brief.md, Task 9, Interfaces and signed starting oracle line 1206.
        # Mutation: Omit the local env-file name from the notice.
        os.makedirs(os.path.join(self.repo, ".github"))
        with open(os.path.join(self.repo, ".github", "claudebox.json"), "w") as fh:
            fh.write("{}")
        notice = repo_config.local_mode_notice(self.repo)
        self.assertIn("does not read it", notice)
        self.assertIn(".env.claudebox", notice)

    def test_a_directory_by_that_name_is_not_the_file(self):
        # Oracle: tester-brief.md, Task 9, Interfaces and signed starting oracle line 1214.
        # Mutation: Use exists instead of isfile for the notice.
        os.makedirs(os.path.join(self.repo, ".github", "claudebox.json"))
        self.assertIsNone(repo_config.local_mode_notice(self.repo))


class RepoBoundaryTest(unittest.TestCase):
    def test_invalid_leaf_is_omitted_without_hiding_valid_siblings(self):
        # Oracle: Task 2 Interfaces: a path with a problem is left out of values.
        # Mutation: retain invalid plan.label, or discard valid code.personas too.
        values, problems = check({"plan": {"label": " "}, "code": {"personas": "sage"}})
        self.assertTrue(problems)
        self.assertNotIn("plan.label", values)
        self.assertEqual(values["code.personas"], "sage")

    def test_full_resolution_keeps_nested_org_sibling(self):
        # Oracle: Decided here 1; Review Focus partial nested override.
        # Mutation: resolve replaces the plan object instead of merging leaves.
        r = repo_config.resolve(b'{"plan":{"label":"proposal","personas":"sage"}}',
                                b'{"plan":{"label":"rfc"}}', SHIPPED_PERSONAS)
        self.assertEqual((r.source, r.repo_problems, r.org_problems), ("repo+org", [], []))
        self.assertEqual((r.config.plan_label, r.config.plan_personas), ("rfc", "sage"))

    def test_repo_byte_cap_accepts_exactly_and_refuses_one_more(self):
        # Oracle: Global Constraints 64 KB, Task 2 REPO_FILE_CAP = 65536.
        # Mutation: reject >= cap, or allow cap+1 through resolve.
        raw = b"{}" + b" " * (65536 - 2)
        r = repo_config.resolve(None, raw, SHIPPED_PERSONAS)
        self.assertEqual((r.source, r.repo_problems), ("repo", []))
        r = repo_config.resolve(None, raw + b" ", SHIPPED_PERSONAS)
        self.assertEqual(r.source, "defaults")
        self.assertIn("65536 bytes", r.repo_problems[0])

    def test_absent_repo_invalid_org_is_not_reviewed(self):
        # Oracle: Task 3 source table, absent repo / invalid org -> none.
        # Mutation: apply defaults when the only present org file is invalid.
        r = repo_config.resolve(b'{"trigger":"wrong"}', None, SHIPPED_PERSONAS)
        self.assertEqual((r.source, r.config, r.repo_problems), ("none", None, []))
        self.assertTrue(r.org_problems)

    def test_resolution_has_no_last_valid_config(self):
        # Oracle: Global Constraints fallback; spec Configuration has no last-valid rung.
        # Mutation: cache the previous valid repo config for an invalid replacement.
        good = repo_config.resolve(None, b'{"team":"previous"}', SHIPPED_PERSONAS)
        bad = repo_config.resolve(None, b'{"team":"previous","typo":1}', SHIPPED_PERSONAS)
        self.assertEqual(good.config.team, "previous")
        self.assertEqual(bad.source, "defaults")
        self.assertIsNone(bad.config.team)
        self.assertTrue(bad.repo_problems)


class LocalStartupAcceptanceTest(unittest.TestCase):
    def run_startup(self, content):
        # Mocks are only process/GitHub seams from existing test_review_loop conventions.
        import contextlib
        import io
        import subprocess
        from unittest.mock import patch
        import review_loop
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        os.mkdir(os.path.join(repo, ".git"))
        if content is not None:
            os.mkdir(os.path.join(repo, ".github"))
            with open(os.path.join(repo, ".github", "claudebox.json"), "w") as fh:
                fh.write(content)
        env = {"GITHUB_REPOSITORY": "owner/repo", "PR_IDS": "12",
               "PERSONA_DIR": SHIPPED_PERSONAS, "WORK_REPO": repo,
               "REVIEW_MODEL": "m", "MAX_CYCLES": "2", "REVIEW_INTERVAL_SECONDS": "0"}
        out = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(review_loop.subprocess, "run", side_effect=lambda args, **kw:
                          subprocess.CompletedProcess(args, 0, "", "")), \
             patch.object(review_loop.gh, "enumerate_candidate_prs", return_value=[]) as poll, \
             contextlib.redirect_stdout(out):
            review_loop.main([])
        return out.getvalue(), poll.call_count

    def test_main_logs_notice_once_and_does_not_parse_hosted_file(self):
        # Oracle: Global Constraints local mode unchanged, once at startup; Task 9 exact notice.
        # Mutation: omit main's notice call, repeat per poll, or parse hosted config locally.
        output, polls = self.run_startup("invalid hosted JSON, deliberately")
        notice = (".github/claudebox.json configures the hosted GitHub App; this local container "
                  "does not read it. Configure local mode through its env file (.env.claudebox) instead.")
        self.assertEqual(polls, 2)  # reaches two real loop iterations, not an early startup error
        self.assertEqual(output.count(notice), 1)

    def test_main_without_hosted_file_has_no_notice(self):
        # Oracle: Task 9 None unless the file exists; Global Constraints local mode unchanged.
        # Mutation: log the hosted notice unconditionally.
        output, polls = self.run_startup(None)
        self.assertEqual(polls, 2)
        self.assertNotIn("this local container does not read it", output)


class RatifiedDefaultsTest(unittest.TestCase):
    def test_repo_defaults_are_the_ratified_values(self):
        # Oracle: tester-brief.md Decided here 3; Task 3 RepoConfig/merge/resolve.
        # Mutation: change rounds default and DEFAULT_ROUNDS_CAP together, or
        # change any other listed dataclass default or empty-resolution default.
        candidates = (repo_config.RepoConfig(), repo_config.merge({}, {}),
                      repo_config.resolve(None, None, SHIPPED_PERSONAS).config)
        for index, config in enumerate(candidates):
            with self.subTest(entrypoint=index):
                self.assertEqual(config.rounds_cap, 4)
                self.assertEqual(config.max_concurrent_personas, 0)
                self.assertEqual(config.plan_personas, personas.DEFAULTS["plan"])
                self.assertEqual(config.code_personas, personas.DEFAULTS["code"])
                self.assertIsNone(config.team)
                self.assertIsNone(config.profile)
                self.assertIsNone(config.model)
