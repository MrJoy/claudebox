# Hosted Phase 1: Repo Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a factory run: the Validator derives a Coder brief (no test code) and a Tester brief (no implementation code) from it, and neither lane opens this file.

**Goal:** A strict loader for the repo and org config files and the deployment file, a JSON Schema for editors, `claudebox.sh config validate`, and a local-mode startup notice, so Phase 2a's control plane has a config surface to read.

**Architecture:** Four new standard-library modules under `reviewer/`: `config_load.py` (strict JSON reading and the problem accumulator), `repo_config.py` (repo/org file schema, key-by-key merge, fallback), `deploy_config.py` (deployment file schema and the repo-against-deployment check), and `config_cli.py` (the validator's command line). `claudebox.sh config validate` runs `config_cli.py` inside the image in a throwaway, network-less, hardened container. `tools/gen-config-schema.py` writes `schema/claudebox.schema.json` from the loader's own constants and the shipped persona tree, and a test fails if the committed file drifts. Local mode reads none of it; `review_loop.main` logs once when a repo carries the file.

**Tech Stack:** Python 3.11 standard library (`json`, `dataclasses`, `difflib`, `argparse`, `unittest`), bash 3.2 for `claudebox.sh`, Docker.

**Spec:** `docs/superpowers/specs/2026-10-09-hosted-github-app-design.md`, sections "Configuration" ("Repo and org files", "Deployment config") and "Build order" (Phase 1). This plan also binds two founder rulings made 2026-10-09 in the session that wrote it (listed under Global Constraints).

**Prerequisite:** branch from `main` after PR #14 (hosted phase 0) has merged. Task 4 imports `providers.PROVIDERS` from it.

## Global Constraints

- Standard library only. Nothing under `reviewer/` or `tools/` may add a pip dependency; the image installs no Python packages for the reviewer.
- **Founder ruling, 2026-10-09: JSON only for now.** The design's `.github/claudebox.yml` and `claudebox.deploy.yml` are `.github/claudebox.json` and `claudebox.deploy.json` until a YAML decision is made. Same keys, same nesting, same meaning.
- **Founder ruling, 2026-10-09: `config validate` runs inside the image**, in a `docker run --rm` the launcher assembles, never on the host's Python.
- Size caps, verbatim from the spec: "capped at 256 KB" for the deployment file, "capped at 64 KB" for repo files. Measured in bytes of the file as read.
- "The loader rejects unknown keys, so a typo is an error and never a silent fall back to the org default. A missing `version` means 1; an unknown version is an error."
- "Neither may hold a secret; the schema has no field that could." The deployment file "names secrets only by reference."
- "`kindex.vectors` joins the schema in phase 3b ... Until then the key is unknown, and therefore an error." The same holds for `kindex.voyage_credential` in the deployment file.
- "A repo file overrides org defaults key by key."
- "When the repo file is invalid, the repo falls back to the org defaults; if those are invalid too, the repo is not reviewed. There is no 'last valid config' rung."
- Persona selectors use "same selector syntax as PLAN_PERSONAS today": `personas.resolve` is the one validator of them.
- The JSON Schema ships at `schema/claudebox.schema.json` and "enumerates persona names and the shape of every field."
- `budget.unit` is required, `tokens` | `passes`; with `passes`, `per_pair.tokens` is an error; `custom` is non-reporting unless the profile sets `usage: reported`.
- Local mode is unchanged apart from one startup line: "When `.github/claudebox.yml` exists in the repo, local mode logs once at startup that it does not read it and names `.env.claudebox` as the local equivalent." (Here: `.github/claudebox.json`.)
- `claudebox.sh` runs on macOS bash 3.2: expand possibly-empty arrays as `${arr[@]+"${arr[@]}"}`.
- Every existing suite (`test-python.sh`, `test-providers.sh`, `test-personas.sh`, `test-shim.sh`, `test-launcher.sh`) still passes.

## Decided here

The spec leaves these open, and each one changes code. They are this plan's answers, for the human to ratify in Phase 1 of the factory run:

1. **Merge depth.** "Key by key" is applied at leaf keys: a repo file that sets only `plan.label` keeps the org's `plan.personas`.
2. **Org invalid, repo valid.** The repo file is applied over the built-in defaults, and the org file's problems are reported. (The spec covers only the repo-invalid direction.)
3. **Built-in defaults** when neither file sets a key: `trigger: auto`, no team, no profile, no model, `plan.label: plan`, both persona selectors at `personas.DEFAULTS`, `max_concurrent_personas: 0`, `rounds.cap: 4` (today's `PLAN_MAX_ROUNDS` default; the spec's example shows 6), `kindex.store: none`.
   `kindex.store` accepts `none` and `org` only. `repo` joins in phase 3a, which ships per-repo stores; until then nothing could serve it, so it is unknown and an error, the same rule the spec gives `kindex.vectors`. `org` stays because the deployment gates it: `--deploy` refuses it when the deployment declares no org store.
4. **A profile is required only against a deployment.** Without `--deploy`, a file naming no profile is valid; with it, the effective config must name one of the deployment's profiles.
5. **What a run cannot check is printed as "not checked here", never passed silently.** Without `--deploy` that is everything only a deployment can judge (`profile`, `model`, `team`, `kindex.store`), and the success line says the deployment was not checked. With `--deploy` it is what only the control plane can judge (whether the team exists, whether the repo is public). `kindex.store: org` against a `share_with` list needs `--repo-name NAME`.
6. **`tokens` budgets, provisionally:** allowed for every provider type except `custom` without `usage: reported`. Phase 2a narrows this set from live responses.
7. **Deployment defaults the spec does not state:** `concurrency: 8`, `max_concurrent_jobs: 4`, `limit_backoff_seconds: 1800` (today's `LIMIT_BACKOFF_SECONDS`), `timing.max_passes_per_session: 0`, `retention.closed_pr_days: 14`. Required: `provider`, `models`, `default_model`, `credential`, `budget.unit`, `budget.daily`, and at least one profile.
   Defaults the spec does state, copied rather than decided: `budget.repo_share: 0.25` ("default 0.25, so one busy repo parks only itself"; a one-repo deployment scales `daily` rather than raising the share), `timing.settle_seconds: 30`, `per_pair.requests: 200`, `per_pair.tokens: 4000000` (tokens budgets only), and the caps table. `timing.poll_seconds` defaults to `60`, the spec's value "when webhooks are off", since webhooks arrive in phase 4.
8. **`$schema`** is an allowed, ignored top-level string in the repo file, so an editor can be pointed at the schema.
9. **The schema does not re-implement the persona selector grammar.** `plan.personas` and `code.personas` are plain strings whose description lists the shipped persona names; `personas.resolve` is the one validator, as the Global Constraints say. A pattern would disagree with it in both directions (`ALL` and `sage,` work and would squiggle; `sage,sage` is refused and would pass), and an editor that cries wolf costs more than the squiggle on a typo buys.

## Review Focus

- **A JSON boolean where a number belongs** (`"max_concurrent_personas": true`). Python's `bool` is an `int`; a reasonable person expects an error, not a 1. Pinned in Task 2 and Task 4.
- **A duplicated key** in one object (`{"trigger": "auto", "trigger": "requested"}`). `json.loads` keeps the last silently; a reasonable person expects the duplicate named as an error. Pinned in Task 1.
- **A valid JSON value that is not an object** (`[]`, `"auto"`, `null`, an empty file). Expected: one error saying the file must hold an object, never a traceback. Pinned in Task 1.
- **A file over its cap, or not UTF-8.** Expected: a single clean error naming the cap or the encoding. Pinned in Task 1.
- **Deep nesting inside the cap** (`[` a thousand times is 1 KB). `json.loads` raises `RecursionError`, which is not a `ValueError`; in phase 2a that file is on a repo's default branch and the reader is the control plane. Expected: the same named `ConfigError`. Pinned in Task 1.
- **A name with a trailing newline** (`"profile": "ollama\n"`, `"credential": "OLLAMA_API_KEY\n"`). Python's `$` matches before a final newline, so `re.match` with `$` accepts it. Expected: refused. Pinned in Tasks 2 and 4 (`fullmatch`).
- **A partial nested override** (repo sets `plan.label`, org sets `plan.personas`). Expected: both apply. Pinned in Task 3.

---

## File Structure

- Create `reviewer/config_load.py`: `read_json`, `Problems`, type predicates. Shared by both loaders.
- Create `reviewer/repo_config.py`: repo/org file keys, `check`, `merge`, `resolve`, `RepoConfig`, `Resolution`, `local_mode_notice`.
- Create `reviewer/deploy_config.py`: deployment file keys, `load_deploy`, `Deploy`, `Profile`, `against_deploy`.
- Create `reviewer/config_cli.py`: `main(argv)` for `validate FILE [--org FILE] [--deploy FILE] [--repo-name NAME]`.
- Create `tools/gen-config-schema.py`: writes `schema/claudebox.schema.json`.
- Create `schema/claudebox.schema.json`: generated, committed.
- Modify `reviewer/review_loop.py`: one call to `local_mode_notice` in `main`.
- Modify `claudebox.sh`: the `config` command and its three flags.
- Modify `test-launcher.sh`: `config validate` cases.
- Create `tests/test_config_load.py`, `tests/test_repo_config.py`, `tests/test_deploy_config.py`, `tests/test_config_cli.py`, `tests/test_config_schema.py`.
- Modify `README.md`, `CLAUDE.md`, `HISTORY.md`.

---

### Task 1: Strict JSON reading

**Files:**
- Create: `reviewer/config_load.py`
- Test: `tests/test_config_load.py`

**Interfaces:**
- Consumes: `common.ConfigError`.
- Produces:
  - `read_json(data: bytes, cap: int, label: str) -> dict` raises `ConfigError` with a message starting `f"{label}: "`.
  - `class Problems` with `add(path: str, message: str) -> None`, `extend(other: "Problems") -> None`, `__bool__`, `lines() -> List[str]` (each `"path: message"`, or just `message` when path is empty), and `unknown(path: str, key: str, allowed: Iterable[str]) -> None`, which adds `f"{join(path, key)}: unknown key"` plus `f" (did you mean '{m}'?)"` when `difflib.get_close_matches(key, allowed, n=1)` finds one.
  - `join(path: str, key: str) -> str`: `key` when `path` is empty, else `f"{path}.{key}"`.
  - `is_int(v) -> bool` (an `int` and not a `bool`), `is_str(v) -> bool`, `is_obj(v) -> bool` (a `dict`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_load.py
import unittest

import _path  # noqa: F401

from common import ConfigError
from config_load import Problems, is_int, join, read_json


class ReadJsonTest(unittest.TestCase):
    def test_an_object_is_returned(self):
        self.assertEqual(read_json(b'{"a": 1}', 100, "repo file"), {"a": 1})

    def test_a_duplicate_key_is_an_error_naming_it(self):
        with self.assertRaises(ConfigError) as ctx:
            read_json(b'{"trigger": "auto", "trigger": "requested"}', 100, "repo file")
        self.assertIn("duplicate key 'trigger'", str(ctx.exception))

    def test_a_nested_duplicate_key_is_an_error_too(self):
        with self.assertRaises(ConfigError):
            read_json(b'{"plan": {"label": "a", "label": "b"}}', 100, "repo file")

    def test_a_non_object_is_an_error(self):
        for raw in (b"[]", b'"auto"', b"null", b"3"):
            with self.subTest(raw=raw), self.assertRaises(ConfigError) as ctx:
                read_json(raw, 100, "repo file")
            self.assertIn("must hold a JSON object", str(ctx.exception))

    def test_an_empty_file_is_an_error_not_a_traceback(self):
        with self.assertRaises(ConfigError) as ctx:
            read_json(b"", 100, "repo file")
        self.assertTrue(str(ctx.exception).startswith("repo file: "))

    def test_invalid_json_names_line_and_column(self):
        with self.assertRaises(ConfigError) as ctx:
            read_json(b'{\n  "a": ,\n}', 100, "repo file")
        self.assertIn("line 2", str(ctx.exception))

    def test_nan_and_infinity_are_refused(self):
        for raw in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                read_json(raw, 100, "repo file")

    def test_over_the_cap_is_an_error_naming_the_cap(self):
        with self.assertRaises(ConfigError) as ctx:
            read_json(b'{"a": "' + b"x" * 200 + b'"}', 64, "repo file")
        self.assertIn("64 bytes", str(ctx.exception))

    def test_exactly_the_cap_is_accepted(self):
        raw = b'{"a": "' + b"x" * 10 + b'"}'
        self.assertEqual(read_json(raw, len(raw), "repo file"), {"a": "x" * 10})

    def test_deep_nesting_is_an_error_not_a_traceback(self):
        for raw in (b"[" * 2000, b'{"a": ' + b"[" * 1000 + b"]" * 1000 + b"}"):
            with self.subTest(size=len(raw)), self.assertRaises(ConfigError) as ctx:
                read_json(raw, 65536, "repo file")
            self.assertIn("nested too deeply", str(ctx.exception))

    def test_non_utf8_is_an_error(self):
        with self.assertRaises(ConfigError) as ctx:
            read_json(b'{"a": "\xff"}', 100, "repo file")
        self.assertIn("UTF-8", str(ctx.exception))


class ProblemsTest(unittest.TestCase):
    def test_lines_join_path_and_message(self):
        p = Problems()
        p.add("plan.label", "must be a non-empty string")
        p.add("", "the file is empty")
        self.assertEqual(p.lines(), ["plan.label: must be a non-empty string", "the file is empty"])

    def test_empty_is_falsy(self):
        self.assertFalse(Problems())

    def test_unknown_suggests_a_close_key(self):
        p = Problems()
        p.unknown("", "trigers", ["trigger", "team"])
        self.assertEqual(p.lines(), ["trigers: unknown key (did you mean 'trigger'?)"])

    def test_unknown_without_a_close_key_says_only_unknown(self):
        p = Problems()
        p.unknown("plan", "zzz", ["label", "personas"])
        self.assertEqual(p.lines(), ["plan.zzz: unknown key"])

    def test_join(self):
        self.assertEqual(join("", "a"), "a")
        self.assertEqual(join("a", "b"), "a.b")


class IsIntTest(unittest.TestCase):
    def test_a_bool_is_not_an_int(self):
        self.assertFalse(is_int(True))
        self.assertTrue(is_int(0))
        self.assertFalse(is_int(1.0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p 'test_config_load.py'`
Expected: FAIL with `ModuleNotFoundError: No module named 'config_load'`

- [ ] **Step 3: Write minimal implementation**

```python
# reviewer/config_load.py
"""Strict JSON reading for claudebox's config files, and the problem list.

The repo, org and deployment files are JSON for now (founder ruling,
2026-10-09; the design names YAML). JSON's own parser is lax in exactly the
ways a strict loader cannot afford: it keeps the last of two duplicate keys,
accepts NaN and Infinity, reads `true` back as something Python treats as
the integer 1, and raises RecursionError, which is not a ValueError, on deep
nesting well inside any byte cap. This module closes each of those, and everything it refuses
comes back as one ConfigError that names the file.
"""

import difflib
import json
from typing import Iterable, List, Tuple

from common import ConfigError


def join(path: str, key: str) -> str:
    return key if not path else f"{path}.{key}"


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_str(value) -> bool:
    return isinstance(value, str)


def is_obj(value) -> bool:
    return isinstance(value, dict)


class Problems:
    """Every problem in a file, so one run reports all of them at once."""

    def __init__(self) -> None:
        self._items: List[Tuple[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self._items.append((path, message))

    def extend(self, other: "Problems") -> None:
        self._items.extend(other._items)

    def unknown(self, path: str, key: str, allowed: Iterable[str]) -> None:
        close = difflib.get_close_matches(key, list(allowed), n=1)
        hint = f" (did you mean '{close[0]}'?)" if close else ""
        self.add(join(path, key), f"unknown key{hint}")

    def lines(self) -> List[str]:
        return [f"{p}: {m}" if p else m for p, m in self._items]

    def __bool__(self) -> bool:
        return bool(self._items)


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key '{key}'")
        out[key] = value
    return out


def _no_constants(name):
    raise ValueError(f"{name} is not a number this file accepts")


def read_json(data: bytes, cap: int, label: str) -> dict:
    if len(data) > cap:
        raise ConfigError(f"{label}: is {len(data)} bytes; the cap is {cap} bytes.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{label}: is not UTF-8 ({exc.reason} at byte {exc.start}).")
    try:
        value = json.loads(
            text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants
        )
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{label}: is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}."
        )
    except ValueError as exc:
        raise ConfigError(f"{label}: {exc}.")
    except RecursionError:
        # json's decoder recurses once per nesting level, and RecursionError
        # is not a ValueError. A byte cap does not bound depth: 1 KB of "["
        # is enough, and in phase 2a this file comes from a repo's default
        # branch and the reader is the control plane.
        raise ConfigError(f"{label}: is nested too deeply to read.")
    if not is_obj(value):
        raise ConfigError(f"{label}: must hold a JSON object at the top level.")
    return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -p 'test_config_load.py'`
Expected: PASS (all cases)

- [ ] **Step 5: Commit**

```bash
git add reviewer/config_load.py tests/test_config_load.py
git commit -m "config: strict JSON reading and a problem list"
```

---

### Task 2: The repo and org file schema

**Files:**
- Create: `reviewer/repo_config.py`
- Test: `tests/test_repo_config.py`

**Interfaces:**
- Consumes: Task 1 (`Problems`, `join`, `is_int`, `is_str`, `is_obj`); `personas.resolve(mode, persona_dir, env)`, `personas.SELECTOR_VAR`, `personas.DEFAULTS`; `common.ConfigError`.
- Produces:
  - `REPO_FILE = ".github/claudebox.json"`, `REPO_FILE_CAP = 65536`.
  - `TRIGGERS = ("auto", "requested")`, `KINDEX_STORES = ("none", "org")`, `DEFAULT_ROUNDS_CAP = 4`.
  - `KEYS`: the allowed key tree, `{"$schema": None, "version": None, "trigger": None, "team": None, "profile": None, "model": None, "plan": {"label": None, "personas": None}, "code": {"personas": None}, "max_concurrent_personas": None, "rounds": {"cap": None}, "kindex": {"store": None}}`. Task 7's schema test compares against it.
  - `check(raw: dict, persona_dir: str) -> Tuple[Dict[str, object], Problems]`: the file's leaf values keyed by dotted path (`"plan.label"`), omitting `$schema` and `version`, plus every problem found. A path with a problem is left out of the dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repo_config.py
import os
import unittest

import _path  # noqa: F401

import repo_config

SHIPPED_PERSONAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "personas")


def check(raw):
    return repo_config.check(raw, SHIPPED_PERSONAS)


class CheckTest(unittest.TestCase):
    def test_an_empty_object_is_valid_and_sets_nothing(self):
        values, problems = check({})
        self.assertEqual(values, {})
        self.assertFalse(problems)

    def test_the_spec_example_is_valid(self):
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
        values, problems = check({"$schema": "https://example.test/s.json"})
        self.assertFalse(problems)
        self.assertEqual(values, {})

    def test_an_unknown_top_level_key_is_an_error_with_a_suggestion(self):
        _, problems = check({"trigers": "auto"})
        self.assertEqual(problems.lines(), ["trigers: unknown key (did you mean 'trigger'?)"])

    def test_an_unknown_nested_key_is_an_error(self):
        _, problems = check({"plan": {"labels": "plan"}})
        self.assertIn("plan.labels: unknown key (did you mean 'label'?)", problems.lines())

    def test_kindex_vectors_is_unknown_until_phase_3b(self):
        _, problems = check({"kindex": {"vectors": True}})
        self.assertIn("kindex.vectors: unknown key", problems.lines()[0])

    def test_version_2_is_an_error(self):
        _, problems = check({"version": 2})
        self.assertIn("version: unknown version 2; this claudebox reads version 1", problems.lines())

    def test_version_true_is_not_version_1(self):
        _, problems = check({"version": True})
        self.assertTrue(problems)

    def test_trigger_outside_the_enum_is_an_error(self):
        _, problems = check({"trigger": "always"})
        self.assertIn("trigger: must be one of auto, requested; got 'always'", problems.lines())

    def test_a_bool_is_not_a_count(self):
        _, problems = check({"max_concurrent_personas": True})
        self.assertIn("max_concurrent_personas: must be a non-negative integer", problems.lines()[0])

    def test_a_negative_rounds_cap_is_an_error(self):
        _, problems = check({"rounds": {"cap": -1}})
        self.assertTrue(problems)

    def test_an_unknown_persona_is_an_error_naming_it(self):
        _, problems = check({"code": {"personas": "saeg"}})
        self.assertEqual(len(problems.lines()), 1)
        self.assertIn("code.personas: unknown persona 'saeg'", problems.lines()[0])

    def test_personas_must_be_a_string_not_a_list(self):
        _, problems = check({"plan": {"personas": ["sage"]}})
        self.assertIn("plan.personas: must be a persona selector string", problems.lines()[0])

    def test_an_empty_plan_label_is_an_error(self):
        _, problems = check({"plan": {"label": " "}})
        self.assertTrue(problems)

    def test_a_section_that_is_not_an_object_is_an_error(self):
        _, problems = check({"plan": "plan"})
        self.assertIn("plan: must be an object", problems.lines()[0])

    def test_kindex_store_outside_the_enum_is_an_error(self):
        _, problems = check({"kindex": {"store": "global"}})
        self.assertTrue(problems)

    def test_team_with_a_slash_is_an_error(self):
        _, problems = check({"team": "org/claudebox"})
        self.assertTrue(problems)

    def test_a_name_with_a_trailing_newline_is_an_error(self):
        for key in ("profile", "team"):
            with self.subTest(key=key):
                _, problems = check({key: "ollama\n"})
                self.assertTrue(problems)

    def test_kindex_store_repo_is_unknown_until_phase_3a(self):
        _, problems = check({"kindex": {"store": "repo"}})
        self.assertIn("kindex.store: must be one of none, org; got 'repo'", problems.lines())

    def test_every_problem_is_reported_at_once(self):
        _, problems = check({"trigger": "x", "team": "", "model": 3})
        self.assertEqual(len(problems.lines()), 3)

    def test_no_key_can_hold_a_secret(self):
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p 'test_repo_config.py'`
Expected: FAIL with `ModuleNotFoundError: No module named 'repo_config'`

- [ ] **Step 3: Write minimal implementation**

```python
# reviewer/repo_config.py
"""The repo and org config files: `.github/claudebox.json`.

Read by the hosted control plane (phase 2a on) from each repo's default
branch, and from the org's `.github` repository for org defaults. Local mode
reads neither; local_mode_notice is all it does with the file.

JSON rather than the design's YAML, by founder ruling (2026-10-09). The keys,
nesting and meaning are the design's.
"""

import re
from typing import Dict, Tuple

import personas as personas_mod
from common import ConfigError
from config_load import Problems, is_int, is_obj, is_str, join

REPO_FILE = ".github/claudebox.json"
REPO_FILE_CAP = 64 * 1024

TRIGGERS = ("auto", "requested")
# `repo` joins in phase 3a, with per-repo stores (plan decision 3).
KINDEX_STORES = ("none", "org")
DEFAULT_ROUNDS_CAP = 4

# The allowed keys, as a tree. A leaf is None. `$schema` is allowed so an
# editor can be pointed at schema/claudebox.schema.json, and is ignored.
KEYS = {
    "$schema": None,
    "version": None,
    "trigger": None,
    "team": None,
    "profile": None,
    "model": None,
    "plan": {"label": None, "personas": None},
    "code": {"personas": None},
    "max_concurrent_personas": None,
    "rounds": {"cap": None},
    "kindex": {"store": None},
}

_NAME_OK = re.compile(r"[A-Za-z0-9._-]+")


def _enum(problems, path, value, allowed):
    if value in allowed:
        return True
    problems.add(path, f"must be one of {', '.join(allowed)}; got {value!r}")
    return False


def _name(problems, path, value, what):
    # fullmatch, not match: Python's `$` also matches before a final newline,
    # so "ollama\n" would pass and then miss every lookup.
    if is_str(value) and _NAME_OK.fullmatch(value):
        return True
    problems.add(path, f"must be a {what} (letters, digits, '.', '_' and '-')")
    return False


def _count(problems, path, value):
    if is_int(value) and value >= 0:
        return True
    problems.add(path, "must be a non-negative integer")
    return False


def _personas(problems, path, value, mode, persona_dir):
    if not is_str(value):
        problems.add(path, "must be a persona selector string, e.g. \"red_team,sage\" or \"all\"")
        return False
    try:
        personas_mod.resolve(mode, persona_dir, {personas_mod.SELECTOR_VAR[mode]: value})
    except ConfigError as exc:
        problems.add(path, str(exc))
        return False
    return True


def _leaf(problems, path, value, persona_dir) -> bool:
    if path == "trigger":
        return _enum(problems, path, value, TRIGGERS)
    if path == "team":
        return _name(problems, path, value, "team slug")
    if path == "profile":
        return _name(problems, path, value, "profile name")
    if path == "model":
        if is_str(value) and value.strip() and not any(c.isspace() for c in value):
            return True
        problems.add(path, "must be a model name with no spaces")
        return False
    if path == "plan.label":
        if is_str(value) and value.strip() and value == value.strip():
            return True
        problems.add(path, "must be a non-empty label with no surrounding spaces")
        return False
    if path == "plan.personas":
        return _personas(problems, path, value, "plan", persona_dir)
    if path == "code.personas":
        return _personas(problems, path, value, "code", persona_dir)
    if path in ("max_concurrent_personas", "rounds.cap"):
        return _count(problems, path, value)
    if path == "kindex.store":
        return _enum(problems, path, value, KINDEX_STORES)
    raise AssertionError(f"no rule for {path}")  # KEYS and this function disagree


def _walk(raw: dict, tree: dict, path: str, values, problems, persona_dir) -> None:
    for key, value in raw.items():
        here = join(path, key)
        if key not in tree:
            problems.unknown(path, key, tree)
            continue
        sub = tree[key]
        if sub is not None:
            if not is_obj(value):
                problems.add(here, "must be an object")
                continue
            _walk(value, sub, here, values, problems, persona_dir)
            continue
        if here == "$schema":
            if not is_str(value):
                problems.add(here, "must be a string")
            continue
        if here == "version":
            if not (is_int(value) and value == 1):
                problems.add(here, f"unknown version {value!r}; this claudebox reads version 1")
            continue
        if _leaf(problems, here, value, persona_dir):
            values[here] = value


def check(raw: dict, persona_dir: str) -> Tuple[Dict[str, object], Problems]:
    values: Dict[str, object] = {}
    problems = Problems()
    _walk(raw, KEYS, "", values, problems, persona_dir)
    return values, problems
```

Note for the implementer: `version: 2` must produce exactly `version: unknown version 2; this claudebox reads version 1`, so the message uses `{value!r}` (which renders `2` as `2`).

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -p 'test_repo_config.py'`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/repo_config.py tests/test_repo_config.py
git commit -m "config: the repo and org file schema"
```

---

### Task 3: Merge, defaults and fallback

**Files:**
- Modify: `reviewer/repo_config.py`
- Test: `tests/test_repo_config.py`

**Interfaces:**
- Consumes: Task 1 `read_json`, `Problems`; Task 2 `check`, `REPO_FILE_CAP`, `DEFAULT_ROUNDS_CAP`; `personas.DEFAULTS`.
- Produces:
  - `@dataclass(frozen=True) class RepoConfig` with fields, in order: `trigger: str = "auto"`, `team: Optional[str] = None`, `profile: Optional[str] = None`, `model: Optional[str] = None`, `plan_label: str = "plan"`, `plan_personas: str = personas.DEFAULTS["plan"]`, `code_personas: str = personas.DEFAULTS["code"]`, `max_concurrent_personas: int = 0`, `rounds_cap: int = DEFAULT_ROUNDS_CAP`, `kindex_store: str = "none"`.
  - `merge(org: Dict[str, object], repo: Dict[str, object]) -> RepoConfig`: built-in defaults, then org leaves, then repo leaves. Dotted path `a.b` maps to field `a_b`.
  - `@dataclass(frozen=True) class Resolution` with `config: Optional[RepoConfig]`, `source: str` (one of `"repo+org"`, `"repo"`, `"org"`, `"defaults"`, `"none"`), `repo_problems: List[str]`, `org_problems: List[str]`.
  - `resolve(org_data: Optional[bytes], repo_data: Optional[bytes], persona_dir: str) -> Resolution`. `None` means the file does not exist.

Source values, by case:

| repo file | org file | source | config |
|---|---|---|---|
| valid | valid | `repo+org` | repo over org over built-ins |
| valid | absent or invalid | `repo` | repo over built-ins |
| absent or invalid | valid | `org` | org over built-ins |
| absent or invalid | absent | `defaults` | built-ins |
| absent or invalid | invalid | `none` | `None` (not reviewed) |

Problems from an invalid file are returned in every row.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_repo_config.py
import personas


def b(text):
    return text.encode("utf-8")


class MergeTest(unittest.TestCase):
    def test_nothing_set_is_the_built_in_defaults(self):
        cfg = repo_config.merge({}, {})
        self.assertEqual(cfg.trigger, "auto")
        self.assertEqual(cfg.plan_label, "plan")
        self.assertEqual(cfg.code_personas, personas.DEFAULTS["code"])
        self.assertEqual(cfg.rounds_cap, repo_config.DEFAULT_ROUNDS_CAP)
        self.assertEqual(cfg.kindex_store, "none")
        self.assertIsNone(cfg.profile)

    def test_the_repo_overrides_the_org_key_by_key(self):
        cfg = repo_config.merge({"trigger": "requested", "team": "rev"}, {"trigger": "auto"})
        self.assertEqual(cfg.trigger, "auto")
        self.assertEqual(cfg.team, "rev")

    def test_a_partial_nested_override_keeps_the_org_sibling(self):
        cfg = repo_config.merge(
            {"plan.label": "proposal", "plan.personas": "sage"}, {"plan.label": "rfc"})
        self.assertEqual(cfg.plan_label, "rfc")
        self.assertEqual(cfg.plan_personas, "sage")

    def test_every_leaf_key_has_a_field(self):
        def leaves(tree, path=""):
            for k, v in tree.items():
                here = f"{path}.{k}" if path else k
                if isinstance(v, dict):
                    yield from leaves(v, here)
                elif k not in ("$schema", "version"):
                    yield here
        names = {f.name for f in repo_config.fields(repo_config.RepoConfig)}
        for path in leaves(repo_config.KEYS):
            self.assertIn(path.replace(".", "_"), names)

    def test_a_checked_key_with_no_field_raises_rather_than_dropping(self):
        with self.assertRaises(AssertionError):
            repo_config.merge({}, {"kindex.vectors": True})

    def test_a_zero_from_the_repo_overrides_a_nonzero_org_value(self):
        cfg = repo_config.merge({"rounds.cap": 6}, {"rounds.cap": 0})
        self.assertEqual(cfg.rounds_cap, 0)


class ResolveTest(unittest.TestCase):
    def resolve(self, org, repo):
        return repo_config.resolve(
            None if org is None else b(org), None if repo is None else b(repo), SHIPPED_PERSONAS)

    def test_both_absent_is_the_defaults(self):
        r = self.resolve(None, None)
        self.assertEqual(r.source, "defaults")
        self.assertEqual(r.config, repo_config.merge({}, {}))

    def test_both_valid_merge(self):
        r = self.resolve('{"team": "rev"}', '{"trigger": "requested"}')
        self.assertEqual(r.source, "repo+org")
        self.assertEqual((r.config.team, r.config.trigger), ("rev", "requested"))

    def test_an_invalid_repo_falls_back_to_the_org_whole(self):
        r = self.resolve('{"team": "rev"}', '{"trigger": "requested", "trigers": 1}')
        self.assertEqual(r.source, "org")
        self.assertEqual(r.config.trigger, "auto")
        self.assertEqual(r.config.team, "rev")
        self.assertTrue(r.repo_problems)

    def test_an_invalid_repo_and_an_invalid_org_is_not_reviewed(self):
        r = self.resolve('{"trigger": 1}', '{"trigger": 2}')
        self.assertEqual(r.source, "none")
        self.assertIsNone(r.config)
        self.assertTrue(r.repo_problems and r.org_problems)

    def test_an_invalid_org_under_a_valid_repo_applies_the_repo_over_defaults(self):
        r = self.resolve('{"trigger": 1}', '{"team": "rev"}')
        self.assertEqual(r.source, "repo")
        self.assertEqual(r.config.team, "rev")
        self.assertTrue(r.org_problems)

    def test_an_invalid_repo_with_no_org_file_is_the_defaults_with_problems(self):
        r = self.resolve(None, '{"trigger": 2}')
        self.assertEqual(r.source, "defaults")
        self.assertEqual(r.config, repo_config.merge({}, {}))
        self.assertTrue(r.repo_problems)

    def test_unparseable_json_is_a_problem_not_an_exception(self):
        r = self.resolve(None, "{")
        self.assertTrue(r.repo_problems)
        self.assertTrue(r.repo_problems[0].startswith("repo file: "))

    def test_a_repo_file_over_64_kb_is_refused(self):
        r = self.resolve(None, '{"team": "' + "a" * 70000 + '"}')
        self.assertIn("65536 bytes", r.repo_problems[0])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p 'test_repo_config.py'`
Expected: FAIL with `AttributeError: module 'repo_config' has no attribute 'merge'`

- [ ] **Step 3: Write minimal implementation**

Append to `reviewer/repo_config.py` (and extend its imports with `from dataclasses import dataclass, fields`, `from typing import List, Optional`, `from config_load import read_json`):

```python
@dataclass(frozen=True)
class RepoConfig:
    trigger: str = "auto"
    team: Optional[str] = None
    profile: Optional[str] = None
    model: Optional[str] = None
    plan_label: str = "plan"
    plan_personas: str = personas_mod.DEFAULTS["plan"]
    code_personas: str = personas_mod.DEFAULTS["code"]
    max_concurrent_personas: int = 0
    rounds_cap: int = DEFAULT_ROUNDS_CAP
    kindex_store: str = "none"


def merge(org: Dict[str, object], repo: Dict[str, object]) -> RepoConfig:
    """Built-in defaults, then the org's leaves, then the repo's: key by key.

    A checked path with no RepoConfig field raises rather than being dropped:
    KEYS, _leaf and RepoConfig are three spellings of one key set, and a key
    added to the first two alone would otherwise validate, print OK, and be
    ignored.
    """
    known = {f.name for f in fields(RepoConfig)}
    values = {}
    for layer in (org, repo):
        for path, value in layer.items():
            name = path.replace(".", "_")
            if name not in known:
                raise AssertionError(f"{path} is a checked key with no RepoConfig field")
            values[name] = value
    return RepoConfig(**values)


@dataclass(frozen=True)
class Resolution:
    config: Optional[RepoConfig]
    source: str
    repo_problems: List[str]
    org_problems: List[str]


def _load(data: Optional[bytes], label: str, persona_dir: str):
    """(values, problems) for a file; (None, []) when it does not exist."""
    if data is None:
        return None, []
    try:
        raw = read_json(data, REPO_FILE_CAP, label)
    except ConfigError as exc:
        return None, [str(exc)]
    values, problems = check(raw, persona_dir)
    if problems:
        return None, [f"{label}: {line}" for line in problems.lines()]
    return values, []


def resolve(org_data: Optional[bytes], repo_data: Optional[bytes], persona_dir: str) -> Resolution:
    """The config a repo is reviewed under.

    An invalid repo file is dropped whole and the org defaults apply; there
    is no "last valid config", which would need a stored copy of repo content
    with its own lifetime. An invalid org file under a valid repo file is
    dropped and the repo file applies over the built-in defaults. Both
    invalid: the repo is not reviewed. Problems are returned either way.
    """
    org, org_problems = _load(org_data, "org file", persona_dir)
    repo, repo_problems = _load(repo_data, "repo file", persona_dir)

    if repo is not None:
        source = "repo+org" if org is not None else "repo"
        return Resolution(merge(org or {}, repo), source, repo_problems, org_problems)
    if org is not None:
        return Resolution(merge(org, {}), "org", repo_problems, org_problems)
    if org_problems:
        return Resolution(None, "none", repo_problems, org_problems)
    return Resolution(merge({}, {}), "defaults", repo_problems, org_problems)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -p 'test_repo_config.py'`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/repo_config.py tests/test_repo_config.py
git commit -m "config: merge org and repo key by key, with the fallback rungs"
```

---

### Task 4: The deployment file

**Files:**
- Create: `reviewer/deploy_config.py`
- Test: `tests/test_deploy_config.py`

**Interfaces:**
- Consumes: Task 1 (`read_json`, `Problems`, `join`, `is_int`, `is_str`, `is_obj`); `providers.PROVIDERS` (Phase 0).
- Produces:
  - Name checks use `re.fullmatch`, never `re.match` with `$` (Python's `$` matches before a final newline).
  - `DEPLOY_FILE = "claudebox.deploy.json"`, `DEPLOY_FILE_CAP = 262144`.
  - `TOKEN_REPORTING = frozenset({"ollama", "anthropic", "cloudflare", "workersai"})`.
  - `KEYS`: the allowed key tree for the file (below). `"*"` stands for any profile name or override repo name.
  - `@dataclass(frozen=True) class Profile`: `name: str`, `provider: str`, `models: Tuple[str, ...]`, `default_model: str`, `credential: str`, `concurrency: int`, `budget_unit: str`, `budget_daily: int`, `repo_share: float`, `limit_backoff_seconds: int`, `per_pair_requests: int`, `per_pair_tokens: Optional[int]`, `usage_reported: bool`.
  - `@dataclass(frozen=True) class Deploy`: `profiles: Dict[str, Profile]`, `max_concurrent_jobs: int`, `poll_seconds: int`, `settle_seconds: int`, `max_passes_per_session: int`, `github_reads_per_pair: int`, `comments_per_pass: int`, `comment_bytes: int`, `clone_bytes_default: int`, `clone_bytes_overrides: Dict[str, int]`, `closed_pr_days: int`, `org_store_share_with: Optional[Union[Tuple[str, ...], str]]` (`None` when no org store, the string `"all-private"`, or a tuple of repo names).
  - `class DeployError(ConfigError)` with `lines: List[str]`, every problem in the file, each prefixed `"deploy file: "`. Its message is the lines joined with `"; "`, for display only: a message may itself contain `"; "`, so callers that count or print problems use `lines`.
  - `load_deploy(data: bytes) -> Deploy` raises `DeployError` for schema problems, and plain `ConfigError` (from `read_json`) for a file that is not readable JSON.

The key tree:

```python
KEYS = {
    "profiles": {"*": {
        "provider": None, "models": None, "default_model": None, "credential": None,
        "concurrency": None, "usage": None, "limit_backoff_seconds": None,
        "budget": {"unit": None, "daily": None, "repo_share": None},
        "per_pair": {"requests": None, "tokens": None},
    }},
    "max_concurrent_jobs": None,
    "timing": {"poll_seconds": None, "settle_seconds": None, "max_passes_per_session": None},
    "caps": {"github_reads_per_pair": None, "comments_per_pass": None, "comment_bytes": None,
             "clone_bytes": {"default": None, "overrides": {"*": None}}},
    "retention": {"closed_pr_days": None},
    "kindex": {"org_store": {"share_with": None}},
}
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_deploy_config.py
import json
import unittest

import _path  # noqa: F401

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
        self.assertIn("profiles", problems({}))
        self.assertIn("profiles", problems({"profiles": {}}))

    def test_an_unknown_top_level_key_is_an_error(self):
        self.assertIn("timeing: unknown key (did you mean 'timing'?)",
                      problems({"profiles": {"o": profile()}, "timeing": {}}))

    def test_voyage_credential_is_unknown_until_phase_3b(self):
        self.assertIn("kindex.voyage_credential: unknown key",
                      problems({"profiles": {"o": profile()},
                                "kindex": {"voyage_credential": "VOYAGE_API_KEY"}}))

    def test_admins_are_not_deploy_file_config(self):
        self.assertIn("admins: unknown key",
                      problems({"profiles": {"o": profile()}, "admins": [1]}))

    def test_an_unknown_provider_is_an_error(self):
        self.assertIn("profiles.o.provider", problems({"profiles": {"o": profile(provider="azure")}}))

    def test_default_model_must_be_in_models(self):
        self.assertIn("profiles.o.default_model",
                      problems({"profiles": {"o": profile(default_model="other")}}))

    def test_models_must_be_a_non_empty_list_of_unique_names(self):
        for bad in ([], "glm", ["a", "a"], [1]):
            with self.subTest(bad=bad):
                self.assertIn("profiles.o.models",
                              problems({"profiles": {"o": profile(models=bad, default_model="a")}}))

    def test_a_credential_is_a_reference_not_a_value(self):
        self.assertIn("profiles.o.credential",
                      problems({"profiles": {"o": profile(credential="sk-ant-abc123")}}))

    def test_a_credential_with_a_trailing_newline_is_refused(self):
        self.assertIn("profiles.o.credential",
                      problems({"profiles": {"o": profile(credential="OLLAMA_API_KEY\n")}}))

    def test_budget_unit_is_required(self):
        self.assertIn("profiles.o.budget.unit",
                      problems({"profiles": {"o": profile(budget={"daily": 5})}}))

    def test_passes_with_per_pair_tokens_is_an_error(self):
        self.assertIn("profiles.o.per_pair.tokens",
                      problems({"profiles": {"o": profile(
                          budget={"unit": "passes", "daily": 500},
                          per_pair={"tokens": 1000})}}))

    def test_a_passes_budget_has_no_per_pair_tokens(self):
        d = load({"profiles": {"o": profile(budget={"unit": "passes", "daily": 500})}})
        self.assertIsNone(d.profiles["o"].per_pair_tokens)

    def test_custom_with_tokens_needs_usage_reported(self):
        self.assertIn("profiles.c.budget.unit",
                      problems({"profiles": {"c": profile(provider="custom")}}))
        d = load({"profiles": {"c": profile(provider="custom", usage="reported")}})
        self.assertTrue(d.profiles["c"].usage_reported)

    def test_usage_is_only_for_custom(self):
        self.assertIn("profiles.o.usage",
                      problems({"profiles": {"o": profile(usage="reported")}}))

    def test_repo_share_must_be_in_zero_one(self):
        for bad in (0, 1.5, -0.1, True):
            with self.subTest(bad=bad):
                self.assertIn("profiles.o.budget.repo_share", problems({"profiles": {"o": profile(
                    budget={"unit": "passes", "daily": 5, "repo_share": bad})}}))

    def test_a_bool_is_not_a_count(self):
        self.assertIn("max_concurrent_jobs",
                      problems({"profiles": {"o": profile()}, "max_concurrent_jobs": True}))

    def test_share_with_all_private_is_accepted(self):
        d = load({"profiles": {"o": profile()},
                  "kindex": {"org_store": {"share_with": "all-private"}}})
        self.assertEqual(d.org_store_share_with, "all-private")

    def test_share_with_any_other_string_is_an_error(self):
        self.assertIn("kindex.org_store.share_with",
                      problems({"profiles": {"o": profile()},
                                "kindex": {"org_store": {"share_with": "all"}}}))

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(deploy_config.DeployError) as ctx:
            load({"profiles": {"o": profile(provider="azure", credential="x y")},
                  "max_concurrent_jobs": -1})
        self.assertEqual(len(ctx.exception.lines), 3)
        self.assertTrue(all(line.startswith("deploy file: ") for line in ctx.exception.lines))

    def test_over_256_kb_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            deploy_config.load_deploy(b'{"x": "' + b"a" * 270000 + b'"}')
        self.assertIn("262144 bytes", str(ctx.exception))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p 'test_deploy_config.py'`
Expected: FAIL with `ModuleNotFoundError: No module named 'deploy_config'`

- [ ] **Step 3: Write minimal implementation**

```python
# reviewer/deploy_config.py
"""The deployment file: `claudebox.deploy.json`.

The operator's file. It lives with the deployment, never in a reviewed repo,
and names secrets only by reference: `credential` is the name of an entry in
the platform's secret store, never a value. JSON rather than the design's
YAML, by founder ruling (2026-10-09).

`against_deploy` checks a repo's effective config against it.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from common import ConfigError
from config_load import Problems, is_int, is_obj, is_str, join, read_json
from providers import PROVIDERS

DEPLOY_FILE = "claudebox.deploy.json"
DEPLOY_FILE_CAP = 256 * 1024

# Provisional (plan decision 6): which provider types report usage on every
# response, so a `tokens` budget can be counted. Phase 2a narrows this from
# live responses. `custom` joins only when its profile says `usage: reported`.
TOKEN_REPORTING = frozenset({"ollama", "anthropic", "cloudflare", "workersai"})

KEYS = {
    "profiles": {"*": {
        "provider": None, "models": None, "default_model": None, "credential": None,
        "concurrency": None, "usage": None, "limit_backoff_seconds": None,
        "budget": {"unit": None, "daily": None, "repo_share": None},
        "per_pair": {"requests": None, "tokens": None},
    }},
    "max_concurrent_jobs": None,
    "timing": {"poll_seconds": None, "settle_seconds": None, "max_passes_per_session": None},
    "caps": {"github_reads_per_pair": None, "comments_per_pass": None, "comment_bytes": None,
             "clone_bytes": {"default": None, "overrides": {"*": None}}},
    "retention": {"closed_pr_days": None},
    "kindex": {"org_store": {"share_with": None}},
}

# Used with fullmatch: Python's `$` matches before a final newline.
_NAME_OK = re.compile(r"[A-Za-z0-9._-]+")
_ENV_NAME_OK = re.compile(r"[A-Z_][A-Z0-9_]*")


class DeployError(ConfigError):
    """Every schema problem in the deployment file, one per line."""

    def __init__(self, lines: List[str]):
        self.lines = lines
        super().__init__("; ".join(lines))


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    models: Tuple[str, ...]
    default_model: str
    credential: str
    concurrency: int
    budget_unit: str
    budget_daily: int
    repo_share: float
    limit_backoff_seconds: int
    per_pair_requests: int
    per_pair_tokens: Optional[int]
    usage_reported: bool


@dataclass(frozen=True)
class Deploy:
    profiles: Dict[str, Profile]
    max_concurrent_jobs: int
    poll_seconds: int
    settle_seconds: int
    max_passes_per_session: int
    github_reads_per_pair: int
    comments_per_pass: int
    comment_bytes: int
    clone_bytes_default: int
    clone_bytes_overrides: Dict[str, int]
    closed_pr_days: int
    org_store_share_with: Optional[Union[Tuple[str, ...], str]]


def _unknown_keys(problems: Problems, raw: dict, tree: dict, path: str) -> None:
    """Report keys not in the tree, recursing into known sub-objects."""
    for key, value in raw.items():
        sub_key = key if key in tree else ("*" if "*" in tree else None)
        if sub_key is None:
            problems.unknown(path, key, [k for k in tree if k != "*"])
            continue
        sub = tree[sub_key]
        if sub is not None and is_obj(value):
            _unknown_keys(problems, value, sub, join(path, key))


def _section(problems, raw, key, path) -> dict:
    value = raw.get(key, {})
    if not is_obj(value):
        problems.add(join(path, key), "must be an object")
        return {}
    return value


def _int(problems, raw, key, path, default, minimum=0) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if is_int(value) and value >= minimum:
        return value
    word = "positive" if minimum == 1 else "non-negative"
    problems.add(join(path, key), f"must be a {word} integer")
    return default


def _profile(problems: Problems, name: str, raw) -> Optional[Profile]:
    path = join("profiles", name)
    before = len(problems.lines())
    if not _NAME_OK.fullmatch(name):
        problems.add(path, "profile names use letters, digits, '.', '_' and '-'")
    if not is_obj(raw):
        problems.add(path, "must be an object")
        return None

    provider = raw.get("provider")
    if provider not in PROVIDERS:
        problems.add(join(path, "provider"), f"must be one of {', '.join(PROVIDERS)}; got {provider!r}")

    models = raw.get("models")
    if not (isinstance(models, list) and models and all(is_str(m) and m for m in models)
            and len(set(models)) == len(models)):
        problems.add(join(path, "models"), "must be a non-empty list of distinct model names")
        models = []
    default_model = raw.get("default_model")
    if default_model not in models:
        problems.add(join(path, "default_model"), "must be one of this profile's models")

    credential = raw.get("credential")
    if not (is_str(credential) and _ENV_NAME_OK.fullmatch(credential)):
        problems.add(join(path, "credential"),
                     "must name a secret in the platform secret store (UPPER_SNAKE_CASE), "
                     "never hold its value")

    usage = raw.get("usage")
    if usage is not None and not (provider == "custom" and usage == "reported"):
        problems.add(join(path, "usage"), "only 'reported', and only on a custom profile")
    usage_reported = usage == "reported" and provider == "custom"

    budget = _section(problems, raw, "budget", path)
    bpath = join(path, "budget")
    unit = budget.get("unit")
    if unit not in ("tokens", "passes"):
        problems.add(join(bpath, "unit"), "is required: tokens or passes")
    elif (unit == "tokens" and provider in PROVIDERS and provider not in TOKEN_REPORTING
          and not usage_reported):
        # An unknown provider is already a problem; judging its usage too
        # would report one mistake twice.
        problems.add(join(bpath, "unit"),
                     f"tokens needs a provider that reports usage, and {provider} does not "
                     "(a custom profile may vouch for its upstream with usage: reported)")
    if "daily" not in budget:
        problems.add(join(bpath, "daily"), "is required")
    daily = _int(problems, budget, "daily", bpath, 1, minimum=1)
    # The spec's default: "so one busy repo parks only itself".
    repo_share = budget.get("repo_share", 0.25)
    if isinstance(repo_share, bool) or not isinstance(repo_share, (int, float)) \
            or not 0 < repo_share <= 1:
        problems.add(join(bpath, "repo_share"), "must be a number above 0 and at most 1")
        repo_share = 0.25

    per_pair = _section(problems, raw, "per_pair", path)
    ppath = join(path, "per_pair")
    requests = _int(problems, per_pair, "requests", ppath, 200, minimum=1)
    tokens: Optional[int] = None
    if unit == "passes":
        if "tokens" in per_pair:
            problems.add(join(ppath, "tokens"), "does not apply to a passes budget")
    else:
        tokens = _int(problems, per_pair, "tokens", ppath, 4_000_000, minimum=1)

    concurrency = _int(problems, raw, "concurrency", path, 8, minimum=1)
    backoff = _int(problems, raw, "limit_backoff_seconds", path, 1800)

    if len(problems.lines()) > before:
        return None
    return Profile(name, provider, tuple(models), default_model, credential, concurrency,
                   unit, daily, float(repo_share), backoff, requests, tokens, usage_reported)


def load_deploy(data: bytes) -> Deploy:
    raw = read_json(data, DEPLOY_FILE_CAP, "deploy file")
    problems = Problems()
    _unknown_keys(problems, raw, KEYS, "")

    profiles_raw = raw.get("profiles")
    profiles: Dict[str, Profile] = {}
    if not is_obj(profiles_raw) or not profiles_raw:
        problems.add("profiles", "must define at least one profile")
    else:
        for name, body in profiles_raw.items():
            prof = _profile(problems, name, body)
            if prof is not None:
                profiles[name] = prof

    timing = _section(problems, raw, "timing", "")
    caps = _section(problems, raw, "caps", "")
    clone = _section(problems, caps, "clone_bytes", "caps")
    overrides_raw = _section(problems, clone, "overrides", "caps.clone_bytes")
    overrides: Dict[str, int] = {}
    for repo, value in overrides_raw.items():
        if is_int(value) and value >= 1:
            overrides[repo] = value
        else:
            problems.add(join("caps.clone_bytes.overrides", repo), "must be a positive integer")
    retention = _section(problems, raw, "retention", "")
    kindex = _section(problems, raw, "kindex", "")
    share_with: Optional[Union[Tuple[str, ...], str]] = None
    if "org_store" in kindex:
        org_store = _section(problems, kindex, "org_store", "kindex")
        value = org_store.get("share_with")
        if value == "all-private":
            share_with = value
        elif isinstance(value, list) and all(is_str(v) and _NAME_OK.fullmatch(v) for v in value):
            share_with = tuple(value)
        else:
            problems.add("kindex.org_store.share_with",
                         "must be a list of repo names or 'all-private'")

    deploy = Deploy(
        profiles=profiles,
        max_concurrent_jobs=_int(problems, raw, "max_concurrent_jobs", "", 4, minimum=1),
        # The spec's value "when webhooks are off"; webhooks arrive in phase 4.
        poll_seconds=_int(problems, timing, "poll_seconds", "timing", 60, minimum=1),
        settle_seconds=_int(problems, timing, "settle_seconds", "timing", 30),
        max_passes_per_session=_int(problems, timing, "max_passes_per_session", "timing", 0),
        github_reads_per_pair=_int(problems, caps, "github_reads_per_pair", "caps", 300, minimum=1),
        comments_per_pass=_int(problems, caps, "comments_per_pass", "caps", 25, minimum=1),
        comment_bytes=_int(problems, caps, "comment_bytes", "caps", 60000, minimum=1),
        clone_bytes_default=_int(problems, clone, "default", "caps.clone_bytes",
                                 2_000_000_000, minimum=1),
        clone_bytes_overrides=overrides,
        closed_pr_days=_int(problems, retention, "closed_pr_days", "retention", 14),
        org_store_share_with=share_with,
    )
    if problems:
        raise DeployError([f"deploy file: {line}" for line in problems.lines()])
    return deploy
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -p 'test_deploy_config.py'`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/deploy_config.py tests/test_deploy_config.py
git commit -m "config: the deployment file"
```

---

### Task 5: A repo config against a deployment

**Files:**
- Modify: `reviewer/deploy_config.py`
- Test: `tests/test_deploy_config.py`

**Interfaces:**
- Consumes: Task 3 `RepoConfig`; Task 4 `Deploy`, `Profile`.
- Produces:
  - `@dataclass(frozen=True) class DeployCheck`: `problems: List[str]`, `unchecked: List[str]`, `model: Optional[str]` (the model the repo would run: its own, or the profile's default; `None` when the profile is unresolved).
  - `against_deploy(cfg: RepoConfig, deploy: Deploy, repo_name: Optional[str]) -> DeployCheck`. `repo_name` is `owner/name` or bare `name`; only the part after the last `/` is compared with `share_with`. (Checking the clone-bytes overrides against installed repos belongs to `hosted config apply`, phase 2a, which knows the installed repos.)

Exact problem strings (tests match them):
- no profile: `profile: this deployment requires one; its profiles: <names sorted, comma-separated>`
- unknown profile: `profile: '<p>' is not a profile of this deployment; its profiles: <names>`
- model not allowed: `model: '<m>' is not allowed by profile '<p>'; allowed: <models in file order, comma-separated>`
- org store missing: `kindex.store: org, but this deployment has no org store`
- org store not shared: `kindex.store: org, but the org store is not shared with '<name>'`
- org store with a list and no repo name: `kindex.store: org needs --repo-name to check the org store's share_with list`

Exact `unchecked` strings:
- when `cfg.team` is set: `team '<t>' exists (checked by the control plane against GitHub)`
- when `kindex_store == "org"`: `the repo is private (checked by the control plane; the org store is never shared with a public repo)`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_deploy_config.py
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
        r = deploy_config.against_deploy(cfg(profile="ollama"), deploy(), None)
        self.assertEqual(r.problems, [])
        self.assertEqual(r.model, "glm-5.2:cloud")

    def test_an_allowed_model_is_kept(self):
        r = deploy_config.against_deploy(cfg(profile="ollama", model="kimi-k2:cloud"), deploy(), None)
        self.assertEqual((r.problems, r.model), ([], "kimi-k2:cloud"))

    def test_no_profile_is_an_error_naming_the_profiles(self):
        r = deploy_config.against_deploy(cfg(), deploy(), None)
        self.assertEqual(r.problems, ["profile: this deployment requires one; its profiles: anthro, ollama"])
        self.assertIsNone(r.model)

    def test_an_unknown_profile_is_an_error(self):
        r = deploy_config.against_deploy(cfg(profile="gpt"), deploy(), None)
        self.assertEqual(r.problems, ["profile: 'gpt' is not a profile of this deployment; its profiles: anthro, ollama"])

    def test_a_model_the_profile_does_not_allow_is_an_error(self):
        r = deploy_config.against_deploy(cfg(profile="ollama", model="claude-opus-4-8"), deploy(), None)
        self.assertEqual(r.problems, ["model: 'claude-opus-4-8' is not allowed by profile 'ollama'; allowed: glm-5.2:cloud, kimi-k2:cloud"])

    def test_org_store_with_no_org_store_is_an_error(self):
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), deploy(), "o/api")
        self.assertIn("kindex.store: org, but this deployment has no org store", r.problems)

    def test_org_store_not_shared_with_the_repo_is_an_error(self):
        d = deploy(kindex={"org_store": {"share_with": ["api"]}})
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, "acme/web")
        self.assertIn("kindex.store: org, but the org store is not shared with 'web'", r.problems)

    def test_org_store_shared_with_the_repo_passes_and_flags_visibility(self):
        d = deploy(kindex={"org_store": {"share_with": ["api"]}})
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, "acme/api")
        self.assertEqual(r.problems, [])
        self.assertTrue(any("private" in u for u in r.unchecked))

    def test_org_store_list_without_a_repo_name_is_an_error(self):
        d = deploy(kindex={"org_store": {"share_with": ["api"]}})
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, None)
        self.assertIn("kindex.store: org needs --repo-name to check the org store's share_with list", r.problems)

    def test_all_private_needs_no_repo_name(self):
        d = deploy(kindex={"org_store": {"share_with": "all-private"}})
        r = deploy_config.against_deploy(cfg(profile="ollama", kindex_store="org"), d, None)
        self.assertEqual(r.problems, [])

    def test_a_team_is_never_silently_passed(self):
        r = deploy_config.against_deploy(cfg(profile="ollama", team="rev"), deploy(), None)
        self.assertIn("team 'rev' exists (checked by the control plane against GitHub)", r.unchecked)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p 'test_deploy_config.py'`
Expected: FAIL with `AttributeError: module 'deploy_config' has no attribute 'against_deploy'`

- [ ] **Step 3: Write minimal implementation**

Append to `reviewer/deploy_config.py`:

```python
@dataclass(frozen=True)
class DeployCheck:
    problems: List[str]
    unchecked: List[str]
    model: Optional[str]


def against_deploy(cfg, deploy: Deploy, repo_name: Optional[str]) -> DeployCheck:
    """Whether this deployment can satisfy a repo's effective config.

    Some of what the spec asks cannot be known offline (whether a team
    exists, whether the repo is public); those come back in `unchecked`,
    for the caller to print, never as a pass.
    """
    problems: List[str] = []
    unchecked: List[str] = []
    names = ", ".join(sorted(deploy.profiles))
    model: Optional[str] = None

    if cfg.profile is None:
        problems.append(f"profile: this deployment requires one; its profiles: {names}")
    elif cfg.profile not in deploy.profiles:
        problems.append(f"profile: '{cfg.profile}' is not a profile of this deployment; "
                        f"its profiles: {names}")
    else:
        prof = deploy.profiles[cfg.profile]
        if cfg.model is None:
            model = prof.default_model
        elif cfg.model in prof.models:
            model = cfg.model
        else:
            problems.append(f"model: '{cfg.model}' is not allowed by profile '{prof.name}'; "
                            f"allowed: {', '.join(prof.models)}")

    if cfg.team is not None:
        unchecked.append(f"team '{cfg.team}' exists (checked by the control plane against GitHub)")

    if cfg.kindex_store == "org":
        share = deploy.org_store_share_with
        bare = repo_name.rsplit("/", 1)[-1] if repo_name else None
        if share is None:
            problems.append("kindex.store: org, but this deployment has no org store")
        elif share != "all-private":
            if bare is None:
                problems.append("kindex.store: org needs --repo-name to check the org "
                                "store's share_with list")
            elif bare not in share:
                problems.append(f"kindex.store: org, but the org store is not shared with '{bare}'")
        unchecked.append("the repo is private (checked by the control plane; the org store "
                         "is never shared with a public repo)")

    return DeployCheck(problems, unchecked, model)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -p 'test_deploy_config.py'`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/deploy_config.py tests/test_deploy_config.py
git commit -m "config: check a repo config against the deployment"
```

---

### Task 6: The validator's command line

**Files:**
- Create: `reviewer/config_cli.py`
- Test: `tests/test_config_cli.py`

**Interfaces:**
- Consumes: Task 3 `resolve`, `Resolution`; Task 4 `load_deploy`; Task 5 `against_deploy`; `common.ConfigError`.
- Produces: `main(argv: Sequence[str], stdout=sys.stdout, stderr=sys.stderr, persona_dir: Optional[str] = None) -> int`. Exit 0 valid, 1 invalid or unreadable, 2 usage error. `persona_dir` defaults to `$PERSONA_DIR` or `/opt/claudebox/personas`.

Command: `config_cli.py validate FILE [--org ORG_FILE] [--deploy DEPLOY_FILE] [--repo-name NAME]`.

Output contract (tests match it):
- Every problem goes to stderr as `ERROR: <problem>`, one line each. Repo and org problems already carry their `repo file: ` / `org file: ` prefix; deploy-file problems (`DeployError.lines`) carry `deploy file: `; `against_deploy` problems are prefixed `against deploy file: `.
- FILE is judged strictly: if the repo file has any problem, the exit code is 1 even though a hosted repo would fall back to the org defaults. stderr then also carries `ERROR: a hosted repo with this file falls back to <the org file|the built-in defaults>`, or, with an invalid `--org` too, `ERROR: a hosted repo with this file and this org file is not reviewed`.
- An invalid `--org` file is exit 1 the same way.
- On success with `--deploy`, stdout carries `OK: config is valid (source: <source>)`, then one line per `RepoConfig` field, `  <field>: <value>`, in field order (`None` printed as `-`), then `  effective model: <model>` and one line per unchecked item, `NOT CHECKED HERE: <item>`.
- On success without `--deploy`, the first line is `OK: config is valid (source: <source>; deployment not checked)`, the field lines follow, and the last line is exactly `NOT CHECKED HERE: profile, model, team and kindex.store are checked only against a deployment (pass --deploy)`. A repo author usually cannot get the deployment file, and this run is the only answer they get in phase 1, so it must not read as a judgement of fields it never looked up.
- A FILE, `--org` or `--deploy` path that cannot be read: `ERROR: cannot read <role> <path>: <reason>`, exit 1. Roles: `repo file`, `org file`, `deploy file`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_cli.py
import io
import json
import os
import shutil
import tempfile
import unittest

import _path  # noqa: F401

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
        rc, out, err = self.run_cli("validate", self.write("r.json", {"trigger": "requested"}))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("OK: config is valid (source: repo; deployment not checked)", out)
        self.assertIn("  trigger: requested", out)
        self.assertIn("  profile: -", out)
        self.assertIn("NOT CHECKED HERE: profile, model, team and kindex.store", out)

    def test_an_invalid_file_exits_one_with_each_problem_on_stderr(self):
        rc, out, err = self.run_cli("validate", self.write("r.json", {"trigger": "x", "team": ""}))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: repo file: trigger:", err)
        self.assertIn("ERROR: repo file: team:", err)
        self.assertIn("falls back to the built-in defaults", err)
        self.assertNotIn("OK:", out)

    def test_an_invalid_file_with_a_valid_org_names_the_org_fallback(self):
        rc, _, err = self.run_cli("validate", self.write("r.json", {"trigger": "x"}),
                                  "--org", self.write("o.json", {}))
        self.assertEqual(rc, 1)
        self.assertIn("falls back to the org file", err)

    def test_both_invalid_says_not_reviewed(self):
        rc, _, err = self.run_cli("validate", self.write("r.json", {"trigger": "x"}),
                                  "--org", self.write("o.json", {"trigger": "y"}))
        self.assertEqual(rc, 1)
        self.assertIn("is not reviewed", err)

    def test_an_invalid_org_under_a_valid_repo_is_still_exit_one(self):
        rc, _, err = self.run_cli("validate", self.write("r.json", {}),
                                  "--org", self.write("o.json", {"trigger": "y"}))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: org file: trigger:", err)

    def test_an_unreadable_file_is_exit_one_not_a_traceback(self):
        rc, _, err = self.run_cli("validate", os.path.join(self.dir, "missing.json"))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: cannot read repo file", err)

    def test_deploy_check_reports_the_effective_model(self):
        rc, out, err = self.run_cli("validate", self.write("r.json", {"profile": "ollama"}),
                                    "--deploy", self.write("d.json", DEPLOY))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("OK: config is valid (source: repo)\n", out)
        self.assertIn("  effective model: glm-5.2:cloud", out)
        self.assertNotIn("deployment not checked", out)

    def test_deploy_check_uses_the_org_profile(self):
        rc, _, err = self.run_cli("validate", self.write("r.json", {}),
                                  "--org", self.write("o.json", {"profile": "ollama"}),
                                  "--deploy", self.write("d.json", DEPLOY))
        self.assertEqual((rc, err), (0, ""))

    def test_deploy_problems_are_exit_one(self):
        rc, _, err = self.run_cli("validate", self.write("r.json", {"profile": "gpt"}),
                                  "--deploy", self.write("d.json", DEPLOY))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: against deploy file: profile: 'gpt'", err)

    def test_an_invalid_deploy_file_is_exit_one(self):
        rc, _, err = self.run_cli("validate", self.write("r.json", {}),
                                  "--deploy", self.write("d.json", {"profiles": {}}))
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: deploy file: profiles", err)

    def test_unchecked_items_are_printed(self):
        rc, out, _ = self.run_cli("validate", self.write("r.json", {"profile": "ollama", "team": "rev"}),
                                  "--deploy", self.write("d.json", DEPLOY))
        self.assertEqual(rc, 0)
        self.assertIn("NOT CHECKED HERE: team 'rev' exists", out)

    def test_no_subcommand_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli()
        self.assertEqual(ctx.exception.code, 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p 'test_config_cli.py'`
Expected: FAIL with `ModuleNotFoundError: No module named 'config_cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# reviewer/config_cli.py
#!/usr/bin/env python3
"""`claudebox.sh config validate`: check a repo config file, offline.

Run inside the image (founder ruling, 2026-10-09): the launcher mounts the
files read-only into a throwaway, network-less container and calls this.
Without --deploy it checks the schema alone, which anyone can run. With
--deploy it also checks the effective config against the deployment, and
prints what only the control plane can check rather than passing it.
"""

import argparse
import os
import sys
from dataclasses import fields
from typing import Optional, Sequence

import deploy_config
import repo_config
from common import ConfigError


def _read(path: str, role: str, stderr) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError as exc:
        stderr.write(f"ERROR: cannot read {role} {path}: {exc.strerror}\n")
        return None


def main(argv: Sequence[str], stdout=sys.stdout, stderr=sys.stderr,
         persona_dir: Optional[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="claudebox.sh config")
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate")
    v.add_argument("file")
    v.add_argument("--org")
    v.add_argument("--deploy")
    v.add_argument("--repo-name")
    args = parser.parse_args(list(argv))
    persona_dir = persona_dir or os.environ.get("PERSONA_DIR") or "/opt/claudebox/personas"

    repo_data = _read(args.file, "repo file", stderr)
    org_data = _read(args.org, "org file", stderr) if args.org else None
    if repo_data is None or (args.org and org_data is None):
        return 1

    res = repo_config.resolve(org_data, repo_data, persona_dir)
    failed = False
    for line in res.repo_problems + res.org_problems:
        stderr.write(f"ERROR: {line}\n")
        failed = True
    if res.repo_problems:
        if res.config is None:
            stderr.write("ERROR: a hosted repo with this file and this org file is not reviewed\n")
        elif res.source == "org":
            stderr.write("ERROR: a hosted repo with this file falls back to the org file\n")
        else:
            stderr.write("ERROR: a hosted repo with this file falls back to the built-in defaults\n")

    check = None
    if args.deploy:
        deploy_data = _read(args.deploy, "deploy file", stderr)
        if deploy_data is None:
            return 1
        try:
            deploy = deploy_config.load_deploy(deploy_data)
        except deploy_config.DeployError as exc:
            for line in exc.lines:
                stderr.write(f"ERROR: {line}\n")
            return 1
        except ConfigError as exc:
            stderr.write(f"ERROR: {exc}\n")
            return 1
        if res.config is not None and not failed:
            check = deploy_config.against_deploy(res.config, deploy, args.repo_name)
            for line in check.problems:
                stderr.write(f"ERROR: against deploy file: {line}\n")
                failed = True

    if failed:
        return 1

    scope = "" if check is not None else "; deployment not checked"
    stdout.write(f"OK: config is valid (source: {res.source}{scope})\n")
    for f in fields(repo_config.RepoConfig):
        value = getattr(res.config, f.name)
        stdout.write(f"  {f.name}: {'-' if value is None else value}\n")
    if check is not None:
        stdout.write(f"  effective model: {check.model}\n")
        for item in check.unchecked:
            stdout.write(f"NOT CHECKED HERE: {item}\n")
    else:
        stdout.write("NOT CHECKED HERE: profile, model, team and kindex.store are checked "
                     "only against a deployment (pass --deploy)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -p 'test_config_cli.py'`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/config_cli.py tests/test_config_cli.py
git commit -m "config: the validate command line"
```

---

### Task 7: The JSON Schema

**Files:**
- Create: `tools/gen-config-schema.py`
- Create: `schema/claudebox.schema.json` (generated)
- Test: `tests/test_config_schema.py`

**Interfaces:**
- Consumes: Task 2 `KEYS`, `TRIGGERS`, `KINDEX_STORES`; `personas._available(directory)` over `personas/code` and `personas/plan`.
- Produces: `tools/gen-config-schema.py` with `_personas(mode: str, names) -> dict`, `build(persona_dir: str) -> dict` and a `main()` that writes `json.dumps(build(...), indent=2) + "\n"` to `schema/claudebox.schema.json` (paths relative to the repo root, found from the script's own location). Run with `--check` it exits 1 when the committed file differs from what it would write.

The schema: Draft 2020-12 (`"$schema": "https://json-schema.org/draft/2020-12/schema"`), `"title": "claudebox repo config"`, `"type": "object"`, `"additionalProperties": false` at every object level, properties mirroring `KEYS`: `$schema` string; `version` `{"const": 1}`; `trigger` `{"enum": TRIGGERS}`; `team` and `profile` `{"type": "string", "pattern": "^[A-Za-z0-9._-]+$"}`; `model` `{"type": "string", "pattern": "^\\S+$"}`; `plan.label` `{"type": "string", "pattern": "^\\S(?:.*\\S)?$"}` (non-empty, no surrounding whitespace, as the loader requires); `plan.personas` and `code.personas` `{"type": "string"}` whose `description` lists that mode's shipped persona names (plan decision 9: no grammar pattern); `max_concurrent_personas` and `rounds.cap` `{"type": "integer", "minimum": 0}`; `kindex.store` `{"enum": KINDEX_STORES}`. Each property carries a one-line `description`.

The persona description for a mode, with `names` the sorted ids from that mode's tree: `f"{Mode}-mode personas: a comma or space separated list, or all. Shipped: {', '.join(names)}."` where `Mode` is `Code` or `Plan`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_schema.py
import importlib.util
import json
import os
import unittest

import _path  # noqa: F401

import personas
import repo_config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(ROOT, "schema", "claudebox.schema.json")

spec = importlib.util.spec_from_file_location("gen", os.path.join(ROOT, "tools", "gen-config-schema.py"))
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def keys_of(schema):
    out = {}
    for name, sub in schema["properties"].items():
        out[name] = keys_of(sub) if sub.get("type") == "object" else None
    return out


class SchemaTest(unittest.TestCase):
    def setUp(self):
        with open(SCHEMA, encoding="utf-8") as fh:
            self.schema = json.load(fh)

    def test_the_committed_file_is_what_the_generator_writes(self):
        self.assertEqual(self.schema, gen.build(os.path.join(ROOT, "personas")))

    def test_its_keys_are_the_loaders_keys(self):
        self.assertEqual(keys_of(self.schema), repo_config.KEYS)

    def test_every_object_refuses_unknown_keys(self):
        def walk(node):
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False)
                for sub in node["properties"].values():
                    walk(sub)
        walk(self.schema)

    def test_the_persona_description_names_every_shipped_persona(self):
        for mode in ("code", "plan"):
            prop = self.schema["properties"][mode]["properties"]["personas"]
            self.assertEqual(prop["type"], "string")
            # No grammar: personas.resolve is the one validator (decision 9).
            self.assertNotIn("pattern", prop)
            for name in personas._available(os.path.join(ROOT, "personas", mode)):
                self.assertIn(name, prop["description"])

    def test_the_label_pattern_agrees_with_the_loader(self):
        import re
        pattern = self.schema["properties"]["plan"]["properties"]["label"]["pattern"]
        for label in ("plan", "needs plan", " plan", "plan ", " ", ""):
            _, problems = repo_config.check({"plan": {"label": label}},
                                            os.path.join(ROOT, "personas"))
            self.assertEqual(bool(re.search(pattern, label)), not problems, label)

    def test_enums_come_from_the_loader(self):
        props = self.schema["properties"]
        self.assertEqual(props["trigger"]["enum"], list(repo_config.TRIGGERS))
        self.assertEqual(props["kindex"]["properties"]["store"]["enum"], list(repo_config.KINDEX_STORES))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p 'test_config_schema.py'`
Expected: FAIL with `FileNotFoundError` for `tools/gen-config-schema.py`

- [ ] **Step 3: Write minimal implementation**

```python
# tools/gen-config-schema.py
#!/usr/bin/env python3
"""Write schema/claudebox.schema.json from the loader and the persona tree.

The schema is for editors: completion and red squiggles while writing
.github/claudebox.json. The loader in reviewer/repo_config.py is what
decides validity; this is generated from its constants so the two cannot
drift, and tests/test_config_schema.py fails when the committed file is not
what this would write. Run it after adding a persona or a key.

    python3 tools/gen-config-schema.py           # rewrite the file
    python3 tools/gen-config-schema.py --check   # exit 1 if it is stale
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "reviewer"))

import personas  # noqa: E402
import repo_config  # noqa: E402

OUT = os.path.join(ROOT, "schema", "claudebox.schema.json")
NAME = {"type": "string", "pattern": "^[A-Za-z0-9._-]+$"}


def _personas(mode, names):
    # A description, not a pattern: personas.resolve is the one validator, and
    # a hand-written grammar would disagree with it in both directions.
    return {"type": "string",
            "description": f"{mode.capitalize()}-mode personas: a comma or space separated "
                           f"list, or all. Shipped: {', '.join(sorted(names))}."}


def _obj(properties):
    return {"type": "object", "additionalProperties": False, "properties": properties}


def build(persona_dir: str) -> dict:
    code = personas._available(os.path.join(persona_dir, "code"))
    plan = personas._available(os.path.join(persona_dir, "plan"))
    schema = _obj({
        "$schema": {"type": "string", "description": "This schema's URL, for editors. Ignored."},
        "version": {"const": 1, "description": "Config format version. Omitted means 1."},
        "trigger": {"enum": list(repo_config.TRIGGERS),
                    "description": "auto reviews every PR; requested only PRs that ask."},
        "team": dict(NAME, description="Team slug whose review request counts as an ask."),
        "profile": dict(NAME, description="A provider profile the deployment defines."),
        "model": {"type": "string", "pattern": "^\\S+$",
                  "description": "A model the profile allows; omit for its default."},
        "plan": _obj({
            "label": {"type": "string", "pattern": "^\\S(?:.*\\S)?$",
                      "description": "The label that makes a PR a plan review."},
            "personas": _personas("plan", plan),
        }),
        "code": _obj({
            "personas": _personas("code", code),
        }),
        "max_concurrent_personas": {"type": "integer", "minimum": 0,
                                    "description": "A PR's personas at once; 0 is all."},
        "rounds": _obj({
            "cap": {"type": "integer", "minimum": 0,
                    "description": "Automatic rounds before an explicit ask is needed."},
        }),
        "kindex": _obj({
            "store": {"enum": list(repo_config.KINDEX_STORES),
                      "description": "Which kindex store reviewers read."},
        }),
    })
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema",
              "title": "claudebox repo config", **schema}
    return schema


def main(argv) -> int:
    text = json.dumps(build(os.path.join(ROOT, "personas")), indent=2) + "\n"
    if "--check" in argv:
        try:
            with open(OUT, encoding="utf-8") as fh:
                return 0 if fh.read() == text else 1
        except OSError:
            return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

Note for the implementer: the top-level schema object carries a `"$schema"` key for the draft and also a `properties["$schema"]` entry for the repo file's own allowed key. Those are different things; keep both.

- [ ] **Step 4: Generate the file and run the test**

Run: `python3 tools/gen-config-schema.py && ./test-python.sh -p 'test_config_schema.py'`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/gen-config-schema.py schema/claudebox.schema.json tests/test_config_schema.py
git commit -m "config: generate the JSON Schema from the loader"
```

---

### Task 8: `claudebox.sh config validate`

**Files:**
- Modify: `claudebox.sh` (defaults block, `usage()`, argument parsing, the resolution `case`, the command `case`)
- Test: `test-launcher.sh`

**Interfaces:**
- Consumes: Task 6's command line at `/opt/claudebox/reviewer/config_cli.py` inside the image.
- Produces: `claudebox.sh config validate FILE [--org FILE] [--deploy FILE] [--repo-name NAME]`. With `--dry-run` it prints the docker command and runs nothing.

The docker command it assembles, exactly (paths absolute on the host; each optional `-v` and flag only when given):

```
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit "$PIDS" --memory "$MEMORY" \
  -v "<abs FILE>:/config/claudebox.json:ro" \
  [-v "<abs ORG>:/config/org.json:ro"] [-v "<abs DEPLOY>:/config/claudebox.deploy.json:ro"] \
  --entrypoint python3 "$IMAGE" /opt/claudebox/reviewer/config_cli.py validate /config/claudebox.json \
  [--org /config/org.json] [--deploy /config/claudebox.deploy.json] [--repo-name NAME]
```

Rules:
- `config` is a command like `run`. The word after it must be `validate`; anything else dies with `unknown config subcommand '<x>' (the only one is validate)`. No word dies with `config needs a subcommand: config validate FILE`.
- Exactly one FILE after `validate`; none dies with `config validate needs a FILE`, two die with `config validate takes one FILE`.
- A FILE, `--org` or `--deploy` that is not a regular file dies with `<flag-or-FILE> not found: <path>` before docker is named.
- `--org`, `--deploy` and `--repo-name` with any other command die with `--org/--deploy/--repo-name only apply to config validate`.
- `config` needs no env file, no repo and no container name, so the resolution `case` treats it like `build`.
- Absolute paths in bash 3.2: `abs() { (cd "$(dirname "$1")" && printf '%s/%s' "$(pwd)" "$(basename "$1")"); }`.

- [ ] **Step 1: Write the failing tests**

Append to `test-launcher.sh`, before its summary block, in the file's own style:

```bash
CFG="$WORK/claudebox.json"; printf '{}\n' >"$CFG"
DEP="$WORK/claudebox.deploy.json"; printf '{}\n' >"$DEP"

L="config validate: runs the validator in a hardened, network-less throwaway container"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG"
  expect "$L" 0 -- "docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges" \
    "$CFG:/config/claudebox.json:ro" "--entrypoint python3 claudebox /opt/claudebox/reviewer/config_cli.py validate /config/claudebox.json" \
    '!--deploy' '!--env-file'
fi

L="config validate: --deploy and --repo-name reach the validator"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG" --deploy "$DEP" --repo-name acme/api
  expect "$L" 0 -- "$DEP:/config/claudebox.deploy.json:ro" "--deploy /config/claudebox.deploy.json" "--repo-name acme/api"
fi

L="config validate: --org is mounted and passed"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG" --org "$DEP"
  expect "$L" 0 -- "$DEP:/config/org.json:ro" "--org /config/org.json"
fi

L="config validate: a relative FILE is made absolute"
if selected "$L"; then
  (cd "$WORK" && env -i PATH="$BASE_PATH" HOME="$WORK" /bin/bash "$LAUNCHER" --dry-run config validate claudebox.json >"$WORK/out" 2>&1); RC=$?
  expect "$L" 0 -- "$WORK/claudebox.json:/config/claudebox.json:ro"
fi

L="config validate: a missing FILE dies before docker"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$WORK/nope.json"
  expect "$L" 1 -- "FILE not found: $WORK/nope.json" '!docker run'
fi

L="config validate: a missing --deploy dies"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG" --deploy "$WORK/nope.json"
  expect "$L" 1 -- "--deploy not found"
fi

L="config: no subcommand dies"
if selected "$L"; then
  launch "$L" 0 -- -- config
  expect "$L" 1 -- "config needs a subcommand"
fi

L="config: an unknown subcommand dies"
if selected "$L"; then
  launch "$L" 0 -- -- config check "$CFG"
  expect "$L" 1 -- "unknown config subcommand 'check'"
fi

L="config validate: no FILE dies"
if selected "$L"; then
  launch "$L" 0 -- -- config validate
  expect "$L" 1 -- "config validate needs a FILE"
fi

L="config validate: two FILEs die"
if selected "$L"; then
  launch "$L" 0 -- -- config validate "$CFG" "$DEP"
  expect "$L" 1 -- "config validate takes one FILE"
fi

L="config flags with another command die"
if selected "$L"; then
  launch "$L" 0 -- -- "${RUN[@]}" --deploy "$DEP"
  expect "$L" 1 -- "only apply to config validate"
fi

L="config validate: a FILE named like a command is still a FILE"
if selected "$L"; then
  printf '{}\n' >"$WORK/run"
  (cd "$WORK" && env -i PATH="$BASE_PATH" HOME="$WORK" /bin/bash "$LAUNCHER" --dry-run config validate run >"$WORK/out" 2>&1); RC=$?
  expect "$L" 0 -- "$WORK/run:/config/claudebox.json:ro" '!more than one command'
fi

L="config validate: needs no env file and no repo"
if selected "$L"; then
  (cd "$WORK" && env -i PATH="$BASE_PATH" HOME="$WORK" /bin/bash "$LAUNCHER" --dry-run --no-repo config validate "$CFG" >"$WORK/out" 2>&1); RC=$?
  expect "$L" 0 -- "config_cli.py validate"
fi
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test-launcher.sh config`
Expected: FAIL on every `config` case (`unknown argument: config`)

- [ ] **Step 3: Implement**

In `claudebox.sh`:

Defaults block, after `DRY_RUN=0`:

```bash
# `config validate` (the hosted App's repo config; run inside the image).
CONFIG_POS=()
CONFIG_ORG=""
CONFIG_DEPLOY=""
CONFIG_REPO_NAME=""
```

`usage()`, in COMMANDS after `status`:

```
  config validate FILE [--org FILE] [--deploy FILE] [--repo-name NAME]
            Check a hosted-App repo config (.github/claudebox.json) offline,
            inside the image, in a network-less throwaway container. --org
            merges org defaults under it; --deploy also checks it against a
            deployment file (claudebox.deploy.json); --repo-name OWNER/NAME
            lets that check read the org kindex store's share_with list.
            Local mode (run/test) does not read this file.
```

Argument parsing: add `config` to the command arm, three flag arms, and route bare words after `config` into `CONFIG_POS`:

```bash
    build|run|test|logs|shell|stop|status|config)
      # After `config`, every bare word is its own positional: a file may
      # be called `run` or `test`.
      if [ "$COMMAND" = config ]; then CONFIG_POS+=("$1")
      else
        [ -z "$COMMAND" ] || die "more than one command given ('$COMMAND' and '$1')."
        COMMAND="$1"
      fi ;;
    --org)         CONFIG_ORG="${2:?--org requires a FILE}"; shift ;;
    --deploy)      CONFIG_DEPLOY="${2:?--deploy requires a FILE}"; shift ;;
    --repo-name)   CONFIG_REPO_NAME="${2:?--repo-name requires OWNER/NAME}"; shift ;;
```

and replace the final `*)` arm with:

```bash
    *)
      if [ "$COMMAND" = config ]; then CONFIG_POS+=("$1")
      else die "unknown argument: $1 (see --help)."
      fi ;;
```

After `[ -n "$COMMAND" ] || { usage; exit 2; }`:

```bash
if [ "$COMMAND" != config ] && { [ -n "$CONFIG_ORG" ] || [ -n "$CONFIG_DEPLOY" ] || [ -n "$CONFIG_REPO_NAME" ]; }; then
  die "--org/--deploy/--repo-name only apply to config validate."
fi
```

Resolution `case`: `build|config) : ;;` in place of `build) : ;;`.

Command `case`, a new arm:

```bash
  config)
    abs() { (cd "$(dirname "$1")" && printf '%s/%s' "$(pwd)" "$(basename "$1")"); }
    [ "${#CONFIG_POS[@]}" -gt 0 ] || die "config needs a subcommand: config validate FILE"
    [ "${CONFIG_POS[0]}" = validate ] || die "unknown config subcommand '${CONFIG_POS[0]}' (the only one is validate)."
    [ "${#CONFIG_POS[@]}" -ge 2 ] || die "config validate needs a FILE."
    [ "${#CONFIG_POS[@]}" -le 2 ] || die "config validate takes one FILE."
    cfg_file="${CONFIG_POS[1]}"
    [ -f "$cfg_file" ] || die "FILE not found: $cfg_file"
    [ -z "$CONFIG_ORG" ] || [ -f "$CONFIG_ORG" ] || die "--org not found: $CONFIG_ORG"
    [ -z "$CONFIG_DEPLOY" ] || [ -f "$CONFIG_DEPLOY" ] || die "--deploy not found: $CONFIG_DEPLOY"
    mounts=(-v "$(abs "$cfg_file"):/config/claudebox.json:ro")
    cli_args=(validate /config/claudebox.json)
    if [ -n "$CONFIG_ORG" ]; then
      mounts+=(-v "$(abs "$CONFIG_ORG"):/config/org.json:ro"); cli_args+=(--org /config/org.json)
    fi
    if [ -n "$CONFIG_DEPLOY" ]; then
      mounts+=(-v "$(abs "$CONFIG_DEPLOY"):/config/claudebox.deploy.json:ro")
      cli_args+=(--deploy /config/claudebox.deploy.json)
    fi
    [ -z "$CONFIG_REPO_NAME" ] || cli_args+=(--repo-name "$CONFIG_REPO_NAME")
    show_and_run docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
      --pids-limit "$PIDS" --memory "$MEMORY" "${mounts[@]}" \
      --entrypoint python3 "$IMAGE" /opt/claudebox/reviewer/config_cli.py "${cli_args[@]}"
    ;;
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test-launcher.sh && bash -n claudebox.sh`
Expected: every case passes, including the 32 that existed before.

- [ ] **Step 5: Commit**

```bash
git add claudebox.sh test-launcher.sh
git commit -m "launcher: config validate, inside the image"
```

---

### Task 9: The local-mode startup notice

**Files:**
- Modify: `reviewer/repo_config.py`, `reviewer/review_loop.py` (in `main`, after `work_repo = _required(env, "WORK_REPO")`)
- Test: `tests/test_repo_config.py`

**Interfaces:**
- Consumes: Task 2 `REPO_FILE`.
- Produces: `local_mode_notice(work_repo: str) -> Optional[str]`: `None` unless `os.path.isfile(os.path.join(work_repo, REPO_FILE))`, else exactly `".github/claudebox.json configures the hosted GitHub App; this local container does not read it. Configure local mode through its env file (.env.claudebox) instead."`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_repo_config.py
import shutil
import tempfile


class LocalModeNoticeTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def test_no_file_no_notice(self):
        self.assertIsNone(repo_config.local_mode_notice(self.repo))

    def test_the_file_produces_the_notice_naming_the_env_file(self):
        os.makedirs(os.path.join(self.repo, ".github"))
        with open(os.path.join(self.repo, ".github", "claudebox.json"), "w") as fh:
            fh.write("{}")
        notice = repo_config.local_mode_notice(self.repo)
        self.assertIn("does not read it", notice)
        self.assertIn(".env.claudebox", notice)

    def test_a_directory_by_that_name_is_not_the_file(self):
        os.makedirs(os.path.join(self.repo, ".github", "claudebox.json"))
        self.assertIsNone(repo_config.local_mode_notice(self.repo))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p 'test_repo_config.py'`
Expected: FAIL with `AttributeError: module 'repo_config' has no attribute 'local_mode_notice'`

- [ ] **Step 3: Implement**

Append to `reviewer/repo_config.py` (add `import os`):

```python
def local_mode_notice(work_repo: str) -> Optional[str]:
    """The one line local mode logs when a repo carries the hosted config."""
    if not os.path.isfile(os.path.join(work_repo, REPO_FILE)):
        return None
    return (f"{REPO_FILE} configures the hosted GitHub App; this local container "
            "does not read it. Configure local mode through its env file "
            "(.env.claudebox) instead.")
```

In `reviewer/review_loop.py`, add `import repo_config` beside the other module imports, and directly after `work_repo = _required(env, "WORK_REPO")` in `main`:

```python
        notice = repo_config.local_mode_notice(work_repo)
        if notice:
            log(notice)
```

- [ ] **Step 4: Run all suites**

Run: `./test-python.sh && ./test-providers.sh && ./test-personas.sh`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add reviewer/repo_config.py reviewer/review_loop.py tests/test_repo_config.py
git commit -m "local mode: say once that it does not read the hosted repo config"
```

---

### Task 10: Documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `HISTORY.md`

No tests; the Validator checks the text against the code.

- [ ] **Step 1: README.** Add a section "Hosted config (preview)" after the configuration section: what `.github/claudebox.json` is, that local mode does not read it, the schema path for editors (`"$schema": "https://raw.githubusercontent.com/MrJoy/claudebox/main/schema/claudebox.schema.json"`), and `claudebox.sh config validate` with and without `--deploy`, including that it needs a built image. Add `config validate` to any command list the README carries.
- [ ] **Step 2: CLAUDE.md.** In "Two pieces working together", raise the module count by four and describe `config_load.py`, `repo_config.py`, `deploy_config.py`, `config_cli.py` in one sentence each. In "Commands", add `config validate`. Add a "Gotchas" bullet: the JSON Schema is generated; run `python3 tools/gen-config-schema.py` after adding a persona or a repo-file key, and `tests/test_config_schema.py` fails until you do. Add one sentence recording the JSON-for-now ruling and where the YAML decision would land (`config_load.read_json` is the one reader).
- [ ] **Step 3: HISTORY.md.** One Unreleased entry in the file's voice: what shipped, the JSON ruling, that local mode is unchanged apart from the notice.
- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md HISTORY.md
git commit -m "docs: hosted repo config, phase 1"
```
