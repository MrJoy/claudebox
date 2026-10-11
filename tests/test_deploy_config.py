import _path  # noqa: F401

# Reachability ledger: org-store negatives use an existing allowed profile and
# model, with kindex_store explicitly org. Cases distinguish missing store,
# missing repo identity, wrong listed repo, and a matching repo; positive controls
# assert the resolved model and deferred private-repo check. Model negatives use
# existing profiles and pair with a positive allowed model. No auth/database
# predicate is present in Phase 1: these exercise config sharing policy only.
# Mock ledger: none; loaders, merges and deployment checks run through real APIs.

import json
import unittest


import deploy_config
from common import ConfigError


def profile(**over):
    p = {"provider": "ollama", "models": ["glm-5.2:cloud", "kimi-k2:cloud"],
         "default_model": "glm-5.2:cloud", "credential": "OLLAMA_API_KEY",
         "budget": {"unit": "tokens", "daily": 40000000}}
    p.update(over)
    return p


def load(doc):
    return deploy_config.load_deploy(json.dumps(doc).encode("utf-8"))


def problems(doc):
    """Every problem line for a document that must not load."""
    try:
        load(doc)
    except deploy_config.DeployError as exc:
        return "\n".join(exc.lines)
    raise AssertionError("expected DeployError")


class LoadTest(unittest.TestCase):
    def test_the_spec_example_loads(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 575.
        # Mutation: Discard configured clone size overrides.
        d = load({
            "profiles": {"ollama": profile(concurrency=8, budget={"unit": "tokens",
                         "daily": 40000000, "repo_share": 0.25}, limit_backoff_seconds=1800,
                         per_pair={"requests": 200, "tokens": 4000000})},
            "max_concurrent_jobs": 4,
            "timing": {"poll_seconds": 300, "settle_seconds": 30, "max_passes_per_session": 0},
            "caps": {"github_reads_per_pair": 300, "comments_per_pass": 25, "comment_bytes": 60000,
                     "clone_bytes": {"default": 2000000000,
                                     "overrides": {"big-monorepo": 8000000000}}},
            "retention": {"closed_pr_days": 14},
            "kindex": {"org_store": {"share_with": ["api", "web", "infra"]}},
        })
        self.assertEqual(d.profiles["ollama"].models, ("glm-5.2:cloud", "kimi-k2:cloud"))
        self.assertEqual(d.clone_bytes_overrides, {"big-monorepo": 8000000000})
        self.assertEqual(d.org_store_share_with, ("api", "web", "infra"))

    def test_defaults_fill_what_is_omitted(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 592.
        # Mutation: Change the default poll_seconds from 60 to 300.
        d = load({"profiles": {"ollama": profile()}})
        p = d.profiles["ollama"]
        self.assertEqual((p.concurrency, p.repo_share, p.limit_backoff_seconds), (8, 0.25, 1800))
        self.assertEqual((p.per_pair_requests, p.per_pair_tokens), (200, 4000000))
        self.assertEqual((d.max_concurrent_jobs, d.poll_seconds, d.settle_seconds), (4, 60, 30))
        self.assertEqual((d.github_reads_per_pair, d.comments_per_pass, d.comment_bytes),
                         (300, 25, 60000))
        self.assertEqual(d.clone_bytes_default, 2000000000)
        self.assertEqual(d.closed_pr_days, 14)
        self.assertIsNone(d.org_store_share_with)

    def test_no_profiles_is_an_error(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 604.
        # Mutation: Accept an empty profile catalog.
        self.assertIn("profiles", problems({}))
        self.assertIn("profiles", problems({"profiles": {}}))

    def test_an_unknown_top_level_key_is_an_error(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 608.
        # Mutation: Silently ignore unknown deployment root keys.
        self.assertIn("timeing: unknown key (did you mean 'timing'?)",
                      problems({"profiles": {"o": profile()}, "timeing": {}}))

    def test_malformed_shapes_are_problems_not_tracebacks(self):
        # PR #17 review: each guard below survived its own deletion.
        # Mutation: Drop any one of the section, name, body or override guards.
        for doc, line in (
                ({"profiles": {"o": profile()}, "timing": 5}, "timing: must be an object"),
                ({"profiles": {"o": profile()}, "caps": 5}, "caps: must be an object"),
                ({"profiles": {"o/../x": profile()}},
                 "profiles.o/../x: profile names use letters, digits"),
                ({"profiles": {"o\n": profile()}}, "profile names use letters, digits"),
                ({"profiles": {"o": 5}}, "profiles.o: must be an object"),
                *(({"profiles": {"o": profile()},
                    "caps": {"clone_bytes": {"overrides": {"api": bad}}}},
                   "caps.clone_bytes.overrides.api: must be a positive integer")
                  for bad in ("big", 0, True, None))):
            with self.subTest(line=line):
                self.assertIn(line, problems(doc))

    def test_an_unknown_key_inside_a_profile_is_an_error(self):
        # PR #17 review: profiles are the one tree reached through "*", so only
        # this case pins the descent into a member's body.
        # Mutation: Skip the descent for members reached through "*".
        self.assertIn("profiles.o.typo: unknown key", problems({"profiles": {"o": profile(typo=1)}}))

    def test_voyage_credential_is_unknown_until_phase_3b(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 612.
        # Mutation: Accept kindex.voyage_credential prematurely.
        self.assertIn("kindex.voyage_credential: unknown key",
                      problems({"profiles": {"o": profile()},
                                "kindex": {"voyage_credential": "VOYAGE_API_KEY"}}))

    def test_admins_are_not_deploy_file_config(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 617.
        # Mutation: Accept admins in deployment config.
        self.assertIn("admins: unknown key",
                      problems({"profiles": {"o": profile()}, "admins": [1]}))

    def test_an_unknown_provider_is_an_error(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 621.
        # Mutation: Allow an unknown provider type.
        self.assertIn("profiles.o.provider", problems({"profiles": {"o": profile(provider="azure")}}))

    def test_default_model_must_be_in_models(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 624.
        # Mutation: Accept a default_model outside models.
        self.assertIn("profiles.o.default_model",
                      problems({"profiles": {"o": profile(default_model="other")}}))

    def test_models_must_be_a_non_empty_list_of_unique_names(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 628.
        # Mutation: Accept a duplicate model list.
        for bad in ([], "glm", ["a", "a"], [1]):
            with self.subTest(bad=bad):
                self.assertIn("profiles.o.models",
                              problems({"profiles": {"o": profile(models=bad, default_model="a")}}))

    def test_a_credential_is_a_reference_not_a_value(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 634.
        # Mutation: Allow a literal hyphenated credential value.
        self.assertIn("profiles.o.credential",
                      problems({"profiles": {"o": profile(credential="sk-ant-abc123")}}))

    def test_a_credential_with_a_trailing_newline_is_refused(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 638.
        # Mutation: Accept a trailing newline in credential references.
        self.assertIn("profiles.o.credential",
                      problems({"profiles": {"o": profile(credential="OLLAMA_API_KEY\n")}}))

    def test_budget_unit_is_required(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 642.
        # Mutation: Default budget.unit when omitted.
        self.assertIn("profiles.o.budget.unit",
                      problems({"profiles": {"o": profile(budget={"daily": 5})}}))

    def test_passes_with_per_pair_tokens_is_an_error(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 646.
        # Mutation: Ignore per_pair.tokens under a passes budget.
        self.assertIn("profiles.o.per_pair.tokens",
                      problems({"profiles": {"o": profile(
                          budget={"unit": "passes", "daily": 500},
                          per_pair={"tokens": 1000})}}))

    def test_a_passes_budget_has_no_per_pair_tokens(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 652.
        # Mutation: Apply token caps to passes budgets.
        d = load({"profiles": {"o": profile(budget={"unit": "passes", "daily": 500})}})
        self.assertIsNone(d.profiles["o"].per_pair_tokens)

    def test_custom_with_tokens_needs_usage_reported(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 656.
        # Mutation: Permit custom token budgets without usage reported.
        self.assertIn("profiles.c.budget.unit",
                      problems({"profiles": {"c": profile(provider="custom")}}))
        d = load({"profiles": {"c": profile(provider="custom", usage="reported")}})
        self.assertTrue(d.profiles["c"].usage_reported)

    def test_usage_is_only_for_custom(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 662.
        # Mutation: Permit usage on a non-custom provider.
        self.assertIn("profiles.o.usage",
                      problems({"profiles": {"o": profile(usage="reported")}}))

    def test_repo_share_must_be_in_zero_one(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 666.
        # Mutation: Accept zero repo_share.
        for bad in (0, 1.5, -0.1, True):
            with self.subTest(bad=bad):
                self.assertIn("profiles.o.budget.repo_share", problems({"profiles": {"o": profile(
                    budget={"unit": "passes", "daily": 5, "repo_share": bad})}}))

    def test_a_bool_is_not_a_count(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 672.
        # Mutation: Treat True as max_concurrent_jobs.
        self.assertIn("max_concurrent_jobs",
                      problems({"profiles": {"o": profile()}, "max_concurrent_jobs": True}))

    def test_share_with_all_private_is_accepted(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 676.
        # Mutation: Reject all-private sharing.
        d = load({"profiles": {"o": profile()},
                  "kindex": {"org_store": {"share_with": "all-private"}}})
        self.assertEqual(d.org_store_share_with, "all-private")

    def test_a_share_with_entry_that_is_not_a_repo_name_is_an_error(self):
        # PR #17 review: only the per-entry check stands between a full name
        # (or a trailing newline) and a store that can never match.
        # Mutation: Accept any string entry in a share_with list.
        for entry in ("myorg/web", "api\n"):
            with self.subTest(entry=entry):
                self.assertIn("kindex.org_store.share_with",
                              problems({"profiles": {"o": profile()},
                                        "kindex": {"org_store": {"share_with": [entry]}}}))

    def test_a_profile_model_with_whitespace_is_an_error(self):
        # PR #17 review: the repo file's `model` rule, on the deployment's side.
        # Mutation: Accept any non-empty model string in a profile.
        for models in (["bad model"], ["glm-5.2:cloud", "kimi\n"]):
            with self.subTest(models=models):
                self.assertIn("profiles.o.models: must be a non-empty list of distinct model "
                              "names with no spaces",
                              problems({"profiles": {"o": profile(models=models,
                                                                  default_model=models[0])}}))

    def test_share_with_any_other_string_is_an_error(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 681.
        # Mutation: Accept arbitrary share_with strings.
        self.assertIn("kindex.org_store.share_with",
                      problems({"profiles": {"o": profile()},
                                "kindex": {"org_store": {"share_with": "all"}}}))

    def test_every_problem_is_reported_at_once(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 686.
        # Mutation: Stop accumulating deployment problems after the first.
        with self.assertRaises(deploy_config.DeployError) as ctx:
            load({"profiles": {"o": profile(provider="azure", credential="x y")},
                  "max_concurrent_jobs": -1})
        self.assertEqual(len(ctx.exception.lines), 3)
        self.assertTrue(all(line.startswith("deploy file: ") for line in ctx.exception.lines))

    def test_over_256_kb_is_refused(self):
        # Oracle: tester-brief.md, Task 4, Interfaces and signed starting oracle line 693.
        # Mutation: Omit the deployment byte cap.
        with self.assertRaises(ConfigError) as ctx:
            deploy_config.load_deploy(b'{"x": "' + b"a" * 270000 + b'"}')
        self.assertIn("262144 bytes", str(ctx.exception))


import repo_config


def deploy(**top):
    doc = {"profiles": {"ollama": profile(), "anthro": profile(
        provider="anthropic", models=["claude-opus-4-8"], default_model="claude-opus-4-8",
        credential="CLAUDE_CODE_OAUTH_TOKEN")}}
    doc.update(top)
    return load(doc)


def cfg(**kw):
    return repo_config.RepoConfig(**kw)


class AgainstDeployTest(unittest.TestCase):
    def test_a_valid_config_resolves_the_default_model(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 749.
        # Mutation: Leave the effective model unset when the profile default applies.
        r = deploy_config.against_deploy(cfg(profile="ollama"), deploy(), None)
        self.assertEqual(r.problems, [])
        self.assertEqual(r.model, "glm-5.2:cloud")

    def test_an_allowed_model_is_kept(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 754.
        # Mutation: Replace an allowed explicit model with the default.
        r = deploy_config.against_deploy(cfg(profile="ollama", model="kimi-k2:cloud"), deploy(), None)
        self.assertEqual((r.problems, r.model), ([], "kimi-k2:cloud"))

    def test_no_profile_is_an_error_naming_the_profiles(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 758.
        # Mutation: Allow a repo with no profile against a deployment.
        r = deploy_config.against_deploy(cfg(), deploy(), None)
        self.assertEqual(r.problems, ["profile: this deployment requires one; its profiles: anthro, ollama"])
        self.assertIsNone(r.model)

    def test_an_unknown_profile_is_an_error(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 763.
        # Mutation: Silently select a default for an unknown profile.
        r = deploy_config.against_deploy(cfg(profile="gpt"), deploy(), None)
        self.assertEqual(r.problems, ["profile: 'gpt' is not a profile of this deployment; its profiles: anthro, ollama"])

    def test_a_model_the_profile_does_not_allow_is_an_error(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 767.
        # Mutation: Skip the profile model allowlist.
        r = deploy_config.against_deploy(cfg(profile="ollama", model="claude-opus-4-8"), deploy(), None)
        self.assertEqual(r.problems, ["model: 'claude-opus-4-8' is not allowed by profile 'ollama'; allowed: glm-5.2:cloud, kimi-k2:cloud"])

    def test_org_store_with_no_org_store_is_an_error(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 771.
        # Mutation: Allow org store access when no org store is configured.
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), deploy(), "o/api")
        self.assertIn("kindex.store: org, but this deployment has no org store", r.problems)

    def test_org_store_not_shared_with_the_repo_is_an_error(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 775.
        # Mutation: Skip the org store sharing predicate.
        d = deploy(kindex={"org_store": {"share_with": ["api"]}})
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, "acme/web")
        self.assertIn("kindex.store: org, but the org store is not shared with 'web'", r.problems)

    def test_org_store_shared_with_the_repo_passes_and_flags_visibility(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 780.
        # Mutation: Omit the public-repo warning after accepting listed sharing.
        d = deploy(kindex={"org_store": {"share_with": ["api"]}})
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, "acme/api")
        self.assertEqual(r.problems, [])
        self.assertTrue(any("private" in u for u in r.unchecked))

    def test_org_store_list_without_a_repo_name_is_an_error(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 786.
        # Mutation: Treat an absent repo name as permission to use a listed org store.
        d = deploy(kindex={"org_store": {"share_with": ["api"]}})
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, None)
        self.assertIn("kindex.store: org needs --repo-name to check the org store's share_with list", r.problems)

    def test_all_private_needs_no_repo_name(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 791.
        # Mutation: Require a repo name even for all-private sharing.
        d = deploy(kindex={"org_store": {"share_with": "all-private"}})
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, None)
        self.assertEqual(r.problems, [])

    def test_a_team_is_never_silently_passed(self):
        # Oracle: tester-brief.md, Task 5, Interfaces and signed starting oracle line 796.
        # Mutation: Silently imply that a configured team was checked.
        r = deploy_config.against_deploy(cfg(profile="ollama", team="rev"), deploy(), None)
        self.assertIn("team 'rev' exists (checked by the control plane against GitHub)", r.unchecked)


class DeployBoundaryTest(unittest.TestCase):
    def test_deploy_byte_cap_accepts_exactly_and_refuses_one_more(self):
        # Oracle: Global Constraints 256 KB; Task 4 DEPLOY_FILE_CAP = 262144.
        # Mutation: reject >= cap or accept cap+1 in load_deploy.
        raw = json.dumps({"profiles": {"o": profile()}}).encode("utf-8")
        raw += b" " * (262144 - len(raw))
        self.assertEqual(deploy_config.load_deploy(raw).profiles["o"].provider, "ollama")
        with self.assertRaises(ConfigError) as ctx:
            deploy_config.load_deploy(raw + b" ")
        self.assertIn("262144 bytes", str(ctx.exception))

    def test_each_required_profile_field_is_required(self):
        # Oracle: Decided here 7 lists required provider/models/default_model/credential/budget fields.
        # Mutation: silently default any one required field.
        for key in ("provider", "models", "default_model", "credential", "budget.unit", "budget.daily"):
            with self.subTest(key=key):
                doc = profile()
                if "." in key:
                    parent, child = key.split(".")
                    del doc[parent][child]
                else:
                    del doc[key]
                self.assertIn("profiles.o." + key, problems({"profiles": {"o": doc}}))

    def test_share_with_matches_exact_repo_name_not_substring(self):
        # Oracle: Task 5 Interfaces last path component compared to share_with.
        # Mutation: substring-match 'api' in 'api-admin' or ignore owner/name normalization.
        d = deploy(kindex={"org_store": {"share_with": ["api"]}})
        for name in ("api", "acme/api"):
            with self.subTest(name=name):
                result = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, name)
                self.assertEqual(result.problems, [])
                self.assertIn("the repo is private (checked by the control plane; the org store is never shared with a public repo)", result.unchecked)
        result = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, "acme/api-admin")
        self.assertEqual(result.problems, ["kindex.store: org, but the org store is not shared with 'api-admin'"])
        self.assertEqual(result.model, "glm-5.2:cloud")  # valid profile reached sharing check

    def test_model_allowlist_is_scoped_to_selected_profile(self):
        # Oracle: Configuration strictness; Task 5 model error tied to selected profile.
        # Mutation: accept any model in any deployment profile.
        d = deploy()
        result = deploy_config.against_deploy(cfg(profile="ollama", model="claude-opus-4-8"), d, None)
        self.assertEqual(result.problems, ["model: 'claude-opus-4-8' is not allowed by profile 'ollama'; allowed: glm-5.2:cloud, kimi-k2:cloud"])
        result = deploy_config.against_deploy(cfg(profile="anthro", model="claude-opus-4-8"), d, None)
        self.assertEqual((result.problems, result.model), ([], "claude-opus-4-8"))


class DeployLeafCoverageTest(unittest.TestCase):
    def test_default_max_passes_per_session_is_literal_zero(self):
        # Oracle: tester-brief.md Decided here 7 timing.max_passes_per_session = 0.
        # Mutation: change the loader's omitted-field default to any nonzero value.
        self.assertEqual(load({"profiles": {"o": profile()}}).max_passes_per_session, 0)

    def test_every_allowed_leaf_changes_its_declared_loaded_field(self):
        # Oracle: Validator DISPATCH 3 parity requirement; tester-brief.md Task 4
        # KEYS and Deploy/Profile Interfaces. Values are fixture inputs, not
        # production observations. Each row maps a signed key to its signed field.
        # Mutation: add a KEYS leaf with no row; OR stop reading an existing leaf
        # and return its default; OR assign its value to the wrong output field.
        # No mocks: each of two inputs per leaf goes through the real JSON loader.
        def leaves(tree, prefix=()):
            for key, subtree in tree.items():
                path = prefix + (key,)
                if isinstance(subtree, dict):
                    yield from leaves(subtree, path)
                else:
                    yield ".".join(path)

        # path, loaded field, raw before, raw after, expected before, expected after
        rows = [
            ("profiles.*.provider", "provider", "ollama", "anthropic", "ollama", "anthropic"),
            ("profiles.*.models", "models",
             ["glm-5.2:cloud", "kimi-k2:cloud"], ["glm-5.2:cloud", "other-model"],
             ("glm-5.2:cloud", "kimi-k2:cloud"), ("glm-5.2:cloud", "other-model")),
            ("profiles.*.default_model", "default_model",
             "glm-5.2:cloud", "kimi-k2:cloud", "glm-5.2:cloud", "kimi-k2:cloud"),
            ("profiles.*.credential", "credential", "KEY_A", "KEY_B", "KEY_A", "KEY_B"),
            ("profiles.*.concurrency", "concurrency", 2, 3, 2, 3),
            # None here means omitted: usage has only one accepted explicit value.
            ("profiles.*.usage", "usage_reported", None, "reported", False, True),
            ("profiles.*.limit_backoff_seconds", "limit_backoff_seconds", 17, 19, 17, 19),
            ("profiles.*.budget.unit", "budget_unit", "tokens", "passes", "tokens", "passes"),
            ("profiles.*.budget.daily", "budget_daily", 101, 103, 101, 103),
            ("profiles.*.budget.repo_share", "repo_share", 0.2, 0.3, 0.2, 0.3),
            ("profiles.*.per_pair.requests", "per_pair_requests", 11, 13, 11, 13),
            ("profiles.*.per_pair.tokens", "per_pair_tokens", 1001, 1003, 1001, 1003),
            ("max_concurrent_jobs", "max_concurrent_jobs", 2, 3, 2, 3),
            ("timing.poll_seconds", "poll_seconds", 17, 19, 17, 19),
            ("timing.settle_seconds", "settle_seconds", 7, 9, 7, 9),
            ("timing.max_passes_per_session", "max_passes_per_session", 2, 3, 2, 3),
            ("caps.github_reads_per_pair", "github_reads_per_pair", 23, 29, 23, 29),
            ("caps.comments_per_pass", "comments_per_pass", 3, 5, 3, 5),
            ("caps.comment_bytes", "comment_bytes", 1000, 2000, 1000, 2000),
            ("caps.clone_bytes.default", "clone_bytes_default", 1000000, 2000000, 1000000, 2000000),
            ("caps.clone_bytes.overrides.*", "clone_bytes_overrides",
             3000000, 4000000, {"api": 3000000}, {"api": 4000000}),
            ("retention.closed_pr_days", "closed_pr_days", 3, 5, 3, 5),
            ("kindex.org_store.share_with", "org_store_share_with",
             ["api"], ["web"], ("api",), ("web",)),
        ]
        paths = [row[0] for row in rows]
        self.assertEqual(len(paths), len(set(paths)), "duplicate coverage row")
        self.assertEqual(set(leaves(deploy_config.KEYS)), set(paths),
                         "every allowed leaf requires a behavior row; update from ratified authority")
        for path, field, before, after, expected_before, expected_after in rows:
            with self.subTest(path=path):
                observed = []
                for value in (before, after):
                    doc = {"profiles": {"o": profile()}}
                    if path == "profiles.*.usage":
                        doc["profiles"]["o"] = profile(
                            provider="custom", budget={"unit": "passes", "daily": 101})
                    concrete = path.replace("profiles.*.", "profiles.o.")
                    concrete = concrete.replace("overrides.*", "overrides.api")
                    keys = concrete.split(".")
                    node = doc
                    for key in keys[:-1]:
                        node = node.setdefault(key, {})
                    if value is not None:
                        node[keys[-1]] = value
                    result = load(doc)
                    target = result.profiles["o"] if path.startswith("profiles.*.") else result
                    observed.append(getattr(target, field))
                self.assertEqual(observed, [expected_before, expected_after])
                self.assertNotEqual(observed[0], observed[1], "declared leaf has no observable effect")
