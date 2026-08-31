# Python Review Loop, Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move claudebox's review loop out of `entrypoint.sh` into Python with byte-identical behavior, leaving the shell to do startup and `exec` into it.

**Architecture:** `entrypoint.sh` keeps everything whose output is an environment or a filesystem state (hardening, auth, provider wiring, clone prep, LiteLLM start) and ends with `exec python3 /opt/claudebox/reviewer/review_loop.py`. Flat modules under `reviewer/` take everything whose output is a decision: persona resolution, prompt assembly, PR enumeration, the session map, dispatch, limit classification, resume bookkeeping. Env crosses the boundary and nothing else.

**Tech Stack:** Python 3 stdlib only (no pip installs, matching `workersai-shim.py`). `unittest` for unit tests. Existing bash acceptance suites retargeted.

**Spec:** `docs/superpowers/specs/2026-08-30-claudebox-python-loop-design.md`

## Global Constraints

- **Stdlib only.** No new pip dependency. `python3` is already unconditional in the image (`Dockerfile:66`).
- **Behavior-preserving.** The only intentional behavior changes in Phase A are the three listed under "Deliberate departures" below. Everything else must produce identical observable output.
- **`MAX_CONCURRENT_PASSES` does not exist in Phase A.** Passes run one at a time. Phase B adds the knob.
- **Log format:** `[HH:MM:SS] message` on stdout, UTC, matching `entrypoint.sh:9` (`date -u +%H:%M:%S`). Lines emitted by a pass gain a pair prefix: `[14:22:07] [#12 code/sage] ...`.
- **`die()` writes `ERROR: <msg>` to stderr and exits 1**, matching `entrypoint.sh:10`.
- **Prompt strings are copied byte-for-byte** from `entrypoint.sh:510-532`. Task 2 pins this with captured fixtures.
- **`sys.stdout` must be line-buffered** so `docker logs -f` stays live.

### Deliberate departures from a faithful port

1. The `all`/`assignee`/`search` enumeration arms log a WARN when `gh pr list` fails, instead of being indistinguishable from "no open PRs" (Task 4).
2. `MAX_CYCLES` replaces the `sleep`-exits-non-zero loop-termination trick (Task 6).
3. Pass log lines gain the `[#PR mode/persona]` prefix, and stream lines gain timestamps they did not have (Task 5).

### Deviations from the spec's file list, decided during planning

- The spec named five files. This plan adds a sixth, `common.py`, holding `Pair`, `log`, `die`, and `ConfigError`. Everything else imports it, and it is too small to fold into any one of the others.
- The spec proposed an HTTP liveness probe for `check_litellm`. This plan uses `os.kill(pid, 0)` against `LITELLM_PID`/`SHIM_PID` exported by the shell, which is what the shell does today and is therefore more faithful. It carries a hazard the shell did not have: Python becomes PID 1 after `exec`, so a dead child stays a zombie and `os.kill(pid, 0)` still succeeds on it. Task 6 reaps non-blockingly before checking.

---

## File Structure

| File | Responsibility |
|---|---|
| `reviewer/common.py` | `Pair`, `log`, `die`, `ConfigError` |
| `reviewer/prompts.py` | the four stanzas, the four defaults, override/suffix composition, `{{PR}}` rendering |
| `reviewer/personas.py` | `PERSONA_DIR` resolution for both modes |
| `reviewer/gh.py` | selector resolution, PR id parsing, label-to-mode routing, candidate enumeration |
| `reviewer/passes.py` | invoking `claude`, consuming `stream-json`, classifying limits |
| `reviewer/review_loop.py` | entry point, cycle loop, session map, resume bookkeeping, failure counting |
| `tests/*.py` | stdlib `unittest` suites, one per module |
| `test-python.sh` | runner, matching the project's one-script-per-suite convention |
| `tools/capture-prompt-fixtures.sh` | one-shot capture of the four prompts from the pre-port entrypoint |

Modules are flat and imported by plain name (`import prompts`), not as a package, because `review_loop.py` runs as a script and its own directory is on `sys.path` automatically.

---

### Task 1: Scaffold, `common.py`, and the test runner

**Files:**
- Create: `reviewer/common.py`
- Create: `tests/_path.py`
- Create: `tests/test_common.py`
- Create: `test-python.sh`

**Interfaces:**
- Consumes: nothing
- Produces: `Pair(pr: int, mode: str, persona: str)` frozen dataclass with `__str__` returning `#12 code/sage`; `log(msg: str, pair: Pair | None = None) -> None`; `die(msg: str) -> NoReturn`; `class ConfigError(Exception)`

- [ ] **Step 1: Write the failing test**

Create `tests/_path.py`:

```python
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reviewer"))
```

Create `tests/test_common.py`:

```python
import io
import unittest
from unittest import mock

import _path  # noqa: F401

import common


class PairTest(unittest.TestCase):
    def test_str_is_the_log_prefix_body(self):
        self.assertEqual(str(common.Pair(12, "code", "sage")), "#12 code/sage")

    def test_is_hashable_and_usable_as_a_dict_key(self):
        a = common.Pair(12, "code", "sage")
        b = common.Pair(12, "code", "sage")
        d = {a: "x"}
        self.assertEqual(d[b], "x")

    def test_differs_by_mode(self):
        self.assertNotEqual(
            common.Pair(12, "code", "sage"), common.Pair(12, "plan", "sage")
        )


class LogTest(unittest.TestCase):
    def test_bare_line_has_a_utc_timestamp_and_no_prefix(self):
        buf = io.StringIO()
        with mock.patch.object(common, "_stamp", return_value="14:22:07"):
            common.log("Fetching latest refs...", stream=buf)
        self.assertEqual(buf.getvalue(), "[14:22:07] Fetching latest refs...\n")

    def test_pair_line_carries_the_pair(self):
        buf = io.StringIO()
        with mock.patch.object(common, "_stamp", return_value="14:22:07"):
            common.log("review complete", pair=common.Pair(12, "code", "sage"), stream=buf)
        self.assertEqual(
            buf.getvalue(), "[14:22:07] [#12 code/sage] review complete\n"
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -t tests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'common'`

- [ ] **Step 3: Write minimal implementation**

Create `reviewer/common.py`:

```python
"""Shared vocabulary for the review loop.

`Pair` replaces the "$pr:$mode:$persona" string key the bash loop used, along
with the ${key%%:*} / ${_rest#*:} unpacking that went with it. As a frozen
dataclass it is hashable, so it is the session map's key directly, and a
persona basename containing a colon can no longer corrupt a key.
"""

import sys
import threading
import time
from dataclasses import dataclass
from typing import NoReturn, Optional, TextIO


class ConfigError(Exception):
    """Startup misconfiguration. Caught at the top level and turned into die()."""


@dataclass(frozen=True, order=True)
class Pair:
    pr: int
    mode: str
    persona: str

    def __str__(self) -> str:
        return f"#{self.pr} {self.mode}/{self.persona}"


# Serializes writes so that concurrent passes (phase B) cannot interleave
# mid-line. Held here rather than in the caller so every log path gets it.
_lock = threading.Lock()


def _stamp() -> str:
    # UTC, matching entrypoint.sh's `date -u +%H:%M:%S`.
    return time.strftime("%H:%M:%S", time.gmtime())


def log(msg: str, pair: Optional[Pair] = None, stream: Optional[TextIO] = None) -> None:
    out = stream if stream is not None else sys.stdout
    prefix = f" [{pair}]" if pair is not None else ""
    with _lock:
        out.write(f"[{_stamp()}]{prefix} {msg}\n")
        out.flush()


def die(msg: str) -> NoReturn:
    sys.stderr.write(f"ERROR: {msg}\n")
    sys.stderr.flush()
    raise SystemExit(1)
```

Create `test-python.sh`:

```bash
#!/usr/bin/env bash
# Unit tests for the Python review loop. No Docker, no network, no credentials.
# The bash acceptance suites (test-providers.sh, test-personas.sh) prove the
# wiring; these prove the decisions the loop makes.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m unittest discover -s tests -t tests "$@"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `chmod +x test-python.sh && ./test-python.sh -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add reviewer/common.py tests/_path.py tests/test_common.py test-python.sh
git commit -m "feat(reviewer): add Pair, log, die and the Python test runner"
```

---

### Task 2: Capture prompt fixtures from the pre-port entrypoint

This task runs **before** `prompts.py` exists and produces the evidence that Task 3 is byte-correct. It must happen while `entrypoint.sh` still builds the prompts, so do not reorder it.

**Files:**
- Create: `tools/capture-prompt-fixtures.sh`
- Create: `tests/fixtures/prompt-code-review.txt`
- Create: `tests/fixtures/prompt-code-followup.txt`
- Create: `tests/fixtures/prompt-plan-review.txt`
- Create: `tests/fixtures/prompt-plan-followup.txt`
- Create: `tests/fixtures/prompt-code-review-linear.txt`

**Interfaces:**
- Consumes: `Pair`, `log` from Task 1 (not used here, but the tests directory exists)
- Produces: five fixture files holding the exact rendered prompt text for PR 1, consumed by Task 3's tests

- [ ] **Step 1: Write the capture script**

Create `tools/capture-prompt-fixtures.sh`:

```bash
#!/usr/bin/env bash
# One-shot: capture the four default prompts (plus one Linear variant) as the
# PRE-PORT entrypoint.sh renders them, so the Python port can be asserted
# byte-for-byte against them rather than against a re-reading of the source.
#
# Run once, commit the fixtures, and leave this script in place as their
# provenance. It borrows test-personas.sh's stubbing technique wholesale.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$SCRIPT_DIR/tests/fixtures"
mkdir -p "$OUT_DIR"

BASH_BIN=""
for c in /opt/homebrew/bin/bash /usr/local/bin/bash "$(command -v bash)"; do
  [ -x "$c" ] || continue
  case "$("$c" -c 'echo ${BASH_VERSINFO[0]}')" in 4|5|6|7|8|9) BASH_BIN="$c"; break ;; esac
done
[ -n "$BASH_BIN" ] || { echo "need bash 4+"; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"

cat >"$BIN/gh" <<'STUB'
#!/bin/sh
case "$1 $2" in
  "pr view") echo '{"number":1,"labels":[]}' ;;
  "pr list") echo '[]' ;;
  *) : ;;
esac
exit 0
STUB
printf '#!/bin/sh\nexit 0\n' >"$BIN/git"
# Drop the flags, exec the rest. GNU coreutils stdbuf is absent on macOS, and a
# stub that execs `-oL` dies with "invalid option", failing every pass — which
# drops the session and makes cycle 2 a second NEW session, not a resume. Same
# stub as test-personas.sh:89-93.
cat >"$BIN/stdbuf" <<'STUB'
#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -*) shift ;; *) break ;; esac; done
exec "$@"
STUB
# Succeeds once so a second cycle runs, which is the only way to see a
# resumed pass's prompt. Fails after, ending the loop under set -e.
cat >"$BIN/sleep" <<'STUB'
#!/bin/sh
n=$(( $(cat "$HOME/slept" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$HOME/slept"
[ "$n" -le 1 ] || exit 1
exit 0
STUB
# Records the LAST argument of each invocation, which is the prompt (the `--`
# before it in run_pass guarantees that).
cat >"$BIN/claude" <<'STUB'
#!/usr/bin/env bash
n=$(( $(cat "$HOME/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$HOME/calls"
printf '%s' "${!#}" >"$HOME/prompt.$n"
printf '{"type":"system","subtype":"init","session_id":"S1"}\n'
printf '{"type":"result","subtype":"success","session_id":"S1","result":"ok"}\n'
exit 0
STUB
chmod +x "$BIN"/*

# $1 = label for the pair of files, rest = extra env
capture() {
  local plan_label="$1" new_out="$2" resumed_out="$3"; shift 3
  local HOME_DIR="$WORK/home"
  rm -rf "$HOME_DIR"; mkdir -p "$HOME_DIR/work/repo/.git" "$HOME_DIR/seed"
  env -i PATH="$BIN:$PATH" HOME="$HOME_DIR" \
    ALLOW_UNHARDENED=1 \
    GITHUB_TOKEN=x GITHUB_REPOSITORY=owner/repo PR_IDS=1 \
    REPO_PATH="$HOME_DIR/seed" REVIEW_INTERVAL_SECONDS=1 \
    PERSONA_DIR="$SCRIPT_DIR/personas" \
    PERSONAS=red_team PLAN_PERSONAS=red_team \
    PLAN_LABEL="$plan_label" \
    PROVIDER=ollama OLLAMA_API_KEY=k \
    "$@" "$BASH_BIN" "$SCRIPT_DIR/entrypoint.sh" >/dev/null 2>&1 || true
  cp "$HOME_DIR/prompt.1" "$OUT_DIR/$new_out"
  cp "$HOME_DIR/prompt.2" "$OUT_DIR/$resumed_out"
  echo "wrote $new_out and $resumed_out"
}

# The gh stub above returns an empty label list, so no PR matches PLAN_LABEL and
# both cycles run in code mode.
capture plan prompt-code-review.txt prompt-code-followup.txt

# Force plan mode by relabeling: a gh stub that returns the plan label.
cat >"$BIN/gh" <<'STUB'
#!/bin/sh
case "$1 $2" in
  "pr view") echo '{"number":1,"labels":[{"name":"plan"}]}' ;;
  "pr list") echo '[]' ;;
  *) : ;;
esac
exit 0
STUB
chmod +x "$BIN/gh"
capture plan prompt-plan-review.txt prompt-plan-followup.txt

# Back to code mode, with Linear configured, to capture the linear stanza tail.
# Only the NEW-session prompt is wanted here, so this does not reuse `capture`
# (which insists on writing both files and would need a throwaway path for the
# second).
cat >"$BIN/gh" <<'STUB'
#!/bin/sh
case "$1 $2" in
  "pr view") echo '{"number":1,"labels":[]}' ;;
  "pr list") echo '[]' ;;
  *) : ;;
esac
exit 0
STUB
chmod +x "$BIN/gh"
HOME_DIR="$WORK/home"
rm -rf "$HOME_DIR"; mkdir -p "$HOME_DIR/work/repo/.git" "$HOME_DIR/seed"
env -i PATH="$BIN:$PATH" HOME="$HOME_DIR" \
  ALLOW_UNHARDENED=1 \
  GITHUB_TOKEN=x GITHUB_REPOSITORY=owner/repo PR_IDS=1 \
  REPO_PATH="$HOME_DIR/seed" REVIEW_INTERVAL_SECONDS=1 \
  PERSONA_DIR="$SCRIPT_DIR/personas" \
  PERSONAS=red_team PLAN_PERSONAS=red_team PLAN_LABEL=plan \
  PROVIDER=ollama OLLAMA_API_KEY=k \
  LINEAR_API_KEY=lin_test \
  "$BASH_BIN" "$SCRIPT_DIR/entrypoint.sh" >/dev/null 2>&1 || true
cp "$HOME_DIR/prompt.1" "$OUT_DIR/prompt-code-review-linear.txt"
echo "wrote prompt-code-review-linear.txt"
```

- [ ] **Step 2: Run it and confirm the fixtures are non-trivial**

Run:

```bash
chmod +x tools/capture-prompt-fixtures.sh && ./tools/capture-prompt-fixtures.sh
wc -c tests/fixtures/prompt-*.txt
grep -c 'statusCheckRollup' tests/fixtures/prompt-code-review.txt
grep -c 'mentally revert' tests/fixtures/prompt-code-review.txt
grep -c 'mentally revert' tests/fixtures/prompt-plan-review.txt
grep -c 'Linear' tests/fixtures/prompt-code-review-linear.txt
```

Expected: each file is over 1000 bytes; `statusCheckRollup` appears once in the code review prompt (the gh stanza landed); `mentally revert` appears once in the code prompt and **zero** times in the plan prompt (the test stanza is code-only); `Linear` appears in the Linear variant.

If `prompt-plan-review.txt` contains `mentally revert`, the capture put the PR in the wrong mode. Stop and fix the `gh` stub before continuing, because every assertion in Task 3 rests on these files.

- [ ] **Step 3: Commit**

```bash
git add tools/capture-prompt-fixtures.sh tests/fixtures/
git commit -m "test: capture the four default prompts from the pre-port entrypoint"
```

---

### Task 3: `prompts.py`

**Files:**
- Create: `reviewer/prompts.py`
- Create: `tests/test_prompts.py`
- Reference: `entrypoint.sh:510-556` (the stanzas, defaults, override and suffix composition)

**Interfaces:**
- Consumes: `ConfigError` from `common`
- Produces:
  - `GH_STANZA: str`, `TEST_STANZA: str`, `PLAN_STANZA: str` (module constants)
  - `linear_stanza(env: Mapping[str, str]) -> str`
  - `build(env: Mapping[str, str]) -> Prompts` where `Prompts` is a frozen dataclass with `review: dict[str, str]` and `followup: dict[str, str]`, both keyed by mode (`"code"` / `"plan"`)
  - `render(template: str, pr: int) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_prompts.py`:

```python
import os
import unittest

import _path  # noqa: F401

import prompts

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


BASE = {}


class DefaultsMatchTheShellTest(unittest.TestCase):
    """The fixtures were captured from the pre-port entrypoint (Task 2).

    These are the port's real acceptance criteria: a stanza that lost a space,
    a period, or a backtick during transcription changes what every review is
    asked to do, and nothing else in the suite would notice.
    """

    def test_code_review_prompt(self):
        built = prompts.render(prompts.build(BASE).review["code"], 1)
        self.assertEqual(built, fixture("prompt-code-review.txt"))

    def test_code_followup_prompt(self):
        built = prompts.render(prompts.build(BASE).followup["code"], 1)
        self.assertEqual(built, fixture("prompt-code-followup.txt"))

    def test_plan_review_prompt(self):
        built = prompts.render(prompts.build(BASE).review["plan"], 1)
        self.assertEqual(built, fixture("prompt-plan-review.txt"))

    def test_plan_followup_prompt(self):
        built = prompts.render(prompts.build(BASE).followup["plan"], 1)
        self.assertEqual(built, fixture("prompt-plan-followup.txt"))

    def test_linear_stanza_lands_on_the_default(self):
        built = prompts.render(
            prompts.build({"LINEAR_API_KEY": "lin_test"}).review["code"], 1
        )
        self.assertEqual(built, fixture("prompt-code-review-linear.txt"))


class StanzaScopeTest(unittest.TestCase):
    def test_plan_prompts_carry_no_test_stanza(self):
        p = prompts.build(BASE)
        self.assertNotIn(prompts.TEST_STANZA, p.review["plan"])
        self.assertNotIn(prompts.TEST_STANZA, p.followup["plan"])

    def test_plan_prompts_carry_the_gh_stanza(self):
        p = prompts.build(BASE)
        self.assertIn(prompts.GH_STANZA, p.review["plan"])
        self.assertIn(prompts.GH_STANZA, p.followup["plan"])

    def test_code_prompts_carry_no_plan_stanza(self):
        p = prompts.build(BASE)
        self.assertNotIn(prompts.PLAN_STANZA, p.review["code"])
        self.assertNotIn(prompts.PLAN_STANZA, p.followup["code"])

    def test_gh_stanza_is_repeated_in_every_followup(self):
        # It is repeated rather than left to the session's history because a
        # long-resumed session's earliest turns are the first thing a context
        # summary drops.
        p = prompts.build(BASE)
        self.assertIn(prompts.GH_STANZA, p.followup["code"])
        self.assertIn(prompts.GH_STANZA, p.followup["plan"])

    def test_linear_stanza_is_absent_without_a_key(self):
        self.assertEqual(prompts.linear_stanza({}), "")
        self.assertEqual(prompts.linear_stanza({"LINEAR_API_KEY": ""}), "")


class OverrideTest(unittest.TestCase):
    def test_override_reaches_claude_verbatim(self):
        p = prompts.build({"REVIEW_PROMPT": "just look at #{{PR}}"})
        self.assertEqual(p.review["code"], "just look at #{{PR}}")
        self.assertNotIn(prompts.GH_STANZA, p.review["code"])

    def test_linear_stanza_does_not_touch_an_override(self):
        p = prompts.build(
            {"REVIEW_PROMPT": "just look at #{{PR}}", "LINEAR_API_KEY": "lin_test"}
        )
        self.assertEqual(p.review["code"], "just look at #{{PR}}")

    def test_code_override_does_not_change_plan_mode(self):
        p = prompts.build({"REVIEW_PROMPT": "just look at #{{PR}}"})
        self.assertIn(prompts.PLAN_STANZA, p.review["plan"])

    def test_plan_override_does_not_change_code_mode(self):
        p = prompts.build({"PLAN_REVIEW_PROMPT": "read the plan in #{{PR}}"})
        self.assertEqual(p.review["plan"], "read the plan in #{{PR}}")
        self.assertIn(prompts.TEST_STANZA, p.review["code"])

    def test_suffix_appends_to_a_default(self):
        p = prompts.build({"REVIEW_PROMPT_SUFFIX": "Be terse."})
        self.assertTrue(p.review["code"].endswith(" Be terse."))
        self.assertIn(prompts.TEST_STANZA, p.review["code"])

    def test_suffix_appends_to_an_override(self):
        p = prompts.build(
            {"REVIEW_PROMPT": "just look at #{{PR}}", "REVIEW_PROMPT_SUFFIX": "Be terse."}
        )
        self.assertEqual(p.review["code"], "just look at #{{PR}} Be terse.")

    def test_all_four_suffixes_are_wired(self):
        p = prompts.build(
            {
                "REVIEW_PROMPT_SUFFIX": "a",
                "FOLLOWUP_PROMPT_SUFFIX": "b",
                "PLAN_REVIEW_PROMPT_SUFFIX": "c",
                "PLAN_FOLLOWUP_PROMPT_SUFFIX": "d",
            }
        )
        self.assertTrue(p.review["code"].endswith(" a"))
        self.assertTrue(p.followup["code"].endswith(" b"))
        self.assertTrue(p.review["plan"].endswith(" c"))
        self.assertTrue(p.followup["plan"].endswith(" d"))

    def test_empty_override_is_treated_as_unset(self):
        # ${REVIEW_PROMPT:-$DEFAULT_PROMPT} in the shell: an empty value falls
        # back to the default rather than sending Claude an empty prompt.
        p = prompts.build({"REVIEW_PROMPT": ""})
        self.assertIn(prompts.TEST_STANZA, p.review["code"])


class RenderTest(unittest.TestCase):
    def test_replaces_every_occurrence(self):
        self.assertEqual(prompts.render("#{{PR}} and #{{PR}}", 12), "#12 and #12")

    def test_leaves_other_text_alone(self):
        self.assertEqual(prompts.render("no token here", 12), "no token here")

    def test_pr_number_is_stringified(self):
        self.assertEqual(prompts.render("{{PR}}", 7), "7")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'prompts'`

- [ ] **Step 3: Generate the stanza constants from the shell, do not retype them**

The four stanzas are between 400 and 1900 characters each. Transcribing them by hand is the single most likely way to break this port, so extract them mechanically.

Create `tools/extract-stanzas.py` and run it once:

```python
#!/usr/bin/env python3
"""Emit reviewer/_stanzas.py from entrypoint.sh's stanza assignments.

Run once, commit the output, delete nothing: this script is the provenance of
those four strings, and the fixture tests in tests/test_prompts.py are what
prove the extraction was faithful.
"""
import re
import sys

SRC = "entrypoint.sh"
WANT = {
    "_test_stanza": "TEST_STANZA",
    "_gh_stanza": "GH_STANZA",
    "_plan_stanza": "PLAN_STANZA",
}

out = {}
with open(SRC, encoding="utf-8") as fh:
    for line in fh:
        m = re.match(r'^(_\w+)="(.*)"$', line.rstrip("\n"))
        if m and m.group(1) in WANT:
            # Undo the shell's escaping inside a double-quoted string. Only
            # these three sequences appear; anything else would be a new escape
            # nobody vetted, so fail loudly rather than guess.
            body = m.group(2)
            if re.search(r'\\[^`$\\]', body):
                sys.exit(f"unexpected escape in {m.group(1)}; extend this script")
            body = body.replace("\\`", "`").replace("\\$", "$").replace("\\\\", "\\")
            out[WANT[m.group(1)]] = body

# The Linear stanza is the printf payload inside linear_stanza(), leading space
# included, so it is matched separately.
with open(SRC, encoding="utf-8") as fh:
    for line in fh:
        m = re.match(r"^\s*printf '%s' \"( If the PR title.*)\"$", line.rstrip("\n"))
        if m:
            body = m.group(1).replace("\\`", "`").replace("\\$", "$").replace("\\\\", "\\")
            out["LINEAR_STANZA"] = body

missing = {"TEST_STANZA", "GH_STANZA", "PLAN_STANZA", "LINEAR_STANZA"} - set(out)
if missing:
    sys.exit(f"did not find: {sorted(missing)}")

with open("reviewer/_stanzas.py", "w", encoding="utf-8") as fh:
    fh.write('"""Generated by tools/extract-stanzas.py from entrypoint.sh.\n\n')
    fh.write("Do not hand-edit. See prompts.py for what each stanza is for, and\n")
    fh.write('tests/test_prompts.py for the fixtures that pin them.\n"""\n\n')
    for name in ("GH_STANZA", "TEST_STANZA", "PLAN_STANZA", "LINEAR_STANZA"):
        fh.write(f"{name} = {out[name]!r}\n\n")
print("wrote reviewer/_stanzas.py")
```

Run: `python3 tools/extract-stanzas.py && python3 -c "import sys; sys.path.insert(0,'reviewer'); import _stanzas; print(len(_stanzas.GH_STANZA), len(_stanzas.TEST_STANZA), len(_stanzas.PLAN_STANZA), len(_stanzas.LINEAR_STANZA))"`

Expected: four lengths printed, each over 380, and `_stanzas.LINEAR_STANZA` starting with a space.

- [ ] **Step 3b: Write `prompts.py` on top of the generated constants**

The four default templates are short enough to write out here, and the fixture tests catch any drift in them.

```python
"""Prompt assembly.

Four defaults, one per (mode, new-or-resumed) combination, plus the stanzas
appended to them. Two rules govern the whole module and neither is cosmetic:

  * A stanza is appended to the DEFAULTS ONLY. An operator who supplies
    REVIEW_PROMPT gets exactly that prompt, unedited.
  * A SUFFIX is appended to whichever prompt is in effect, default or override.

The bare names (REVIEW_PROMPT, FOLLOWUP_PROMPT) mean code mode, so tuning the
code prompt cannot silently change what a plan PR gets asked.
"""

from dataclasses import dataclass
from typing import Dict, Mapping

# Extracted from entrypoint.sh by tools/extract-stanzas.py. See the long
# comments there for why each exists; the short version:
#   GH     - what the privilege-minimized token can actually do.
#   TEST   - "review the tests" as a runnable procedure rather than a quality.
#   PLAN   - what to review in a proposal, and what NOT to flag in one.
#   LINEAR - read the ticket the PR claims to implement. Leading space included.
from _stanzas import GH_STANZA, LINEAR_STANZA as _LINEAR_STANZA, PLAN_STANZA, TEST_STANZA

_DEFAULT_REVIEW_CODE = (
    "Perform a thorough review of pull request #{{PR}} in this repository. Inspect it "
    "with `gh pr diff {{PR}}` and `gh pr view {{PR}} --json number,title,body,author,"
    "url,state,isDraft,headRefName,headRefOid,baseRefName,labels,files,commits,"
    "comments,reviews`, and be sure you're looking at the most recent commit on its "
    "branch. " + GH_STANZA + " Pay particular attention to test quality/robustness, "
    "security, correctness, and architectural coherence/consistency, and whether the "
    "approach the PR takes is prudent and robust in light of the issue it addresses. "
    + TEST_STANZA + " Post findings as comments on the PR, one comment per finding."
)

_DEFAULT_FOLLOWUP_CODE = (
    "I've fetched the latest refs. Re-check pull request #{{PR}} for new commits or "
    "changes since your last review of it. Apply the same review standard, and only "
    "post findings you haven't already raised on this PR. Be sure you're looking at "
    "the most recent commit on its branch. " + GH_STANZA + " " + TEST_STANZA
)

_DEFAULT_REVIEW_PLAN = (
    "Review the plan or design proposed in pull request #{{PR}} in this repository. "
    "Read it with `gh pr diff {{PR}}` and `gh pr view {{PR}} --json number,title,body,"
    "author,url,state,isDraft,headRefName,headRefOid,baseRefName,labels,files,commits,"
    "comments,reviews`, and be sure you're looking at the most recent commit on its "
    "branch. " + GH_STANZA + " " + PLAN_STANZA + " Post findings as comments on the "
    "PR, one comment per finding."
)

_DEFAULT_FOLLOWUP_PLAN = (
    "I've fetched the latest refs. Re-read the plan in pull request #{{PR}} for "
    "revisions since your last review of it. Apply the same review standard, and only "
    "post findings you haven't already raised on this PR. A point you raised that the "
    "revision addresses is settled; say nothing further about it. Be sure you're "
    "looking at the most recent commit on its branch. " + GH_STANZA + " " + PLAN_STANZA
)


@dataclass(frozen=True)
class Prompts:
    review: Dict[str, str]
    followup: Dict[str, str]


def linear_stanza(env: Mapping[str, str]) -> str:
    """The Linear instruction, or empty when Linear is not configured.

    Leading space included: it is appended to a prompt.
    """
    if not env.get("LINEAR_API_KEY"):
        return ""
    return _LINEAR_STANZA


def build(env: Mapping[str, str]) -> Prompts:
    ls = linear_stanza(env)

    # Stanza on the default only. An override is verbatim.
    review = {
        "code": env.get("REVIEW_PROMPT") or (_DEFAULT_REVIEW_CODE + ls),
        "plan": env.get("PLAN_REVIEW_PROMPT") or (_DEFAULT_REVIEW_PLAN + ls),
    }
    followup = {
        "code": env.get("FOLLOWUP_PROMPT") or (_DEFAULT_FOLLOWUP_CODE + ls),
        "plan": env.get("PLAN_FOLLOWUP_PROMPT") or (_DEFAULT_FOLLOWUP_PLAN + ls),
    }

    # Suffix on whichever is in effect. A single space joins, since the defaults
    # end in '.'.
    for key, mode in (("REVIEW_PROMPT_SUFFIX", "code"), ("PLAN_REVIEW_PROMPT_SUFFIX", "plan")):
        if env.get(key):
            review[mode] = f"{review[mode]} {env[key]}"
    for key, mode in (("FOLLOWUP_PROMPT_SUFFIX", "code"), ("PLAN_FOLLOWUP_PROMPT_SUFFIX", "plan")):
        if env.get(key):
            followup[mode] = f"{followup[mode]} {env[key]}"

    return Prompts(review=review, followup=followup)


def render(template: str, pr: int) -> str:
    """Substitute the {{PR}} token. Overrides included, per entrypoint.sh:483."""
    return template.replace("{{PR}}", str(pr))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -v`
Expected: PASS, all 20 tests. If a fixture comparison fails, `diff <(python3 -c "import sys; sys.path.insert(0,'reviewer'); import prompts; print(prompts.render(prompts.build({}).review['code'], 1), end='')") tests/fixtures/prompt-code-review.txt` shows the exact byte that drifted.

- [ ] **Step 5: Commit**

```bash
git add reviewer/prompts.py reviewer/_stanzas.py tools/extract-stanzas.py tests/test_prompts.py
git commit -m "feat(reviewer): port prompt assembly, pinned to captured fixtures"
```

---

### Task 4: `personas.py`

**Files:**
- Create: `reviewer/personas.py`
- Create: `tests/test_personas.py`
- Reference: `entrypoint.sh:388-460` (`persona_meta`, `persona_body`, `persona_prompt`, `resolve_personas`)

**Interfaces:**
- Consumes: `ConfigError` from `common`
- Produces:
  - `Persona` frozen dataclass with `id: str`, `label: str`, `prompt: str`
  - `REVIEW_MODES: tuple[str, ...]` = `("code", "plan")`
  - `DEFAULTS: dict[str, str]` mapping mode to its default comma-separated selector
  - `RESERVED: frozenset[str]` = `frozenset({"aggregate"})`
  - `resolve(mode: str, persona_dir: str, env: Mapping[str, str]) -> list[Persona]`, raising `ConfigError` on anything unresolvable

- [ ] **Step 1: Write the failing test**

Create `tests/test_personas.py`:

```python
import os
import shutil
import tempfile
import unittest

import _path  # noqa: F401

import personas
from common import ConfigError

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SHIPPED = os.path.join(REPO, "personas")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TreeBuilder:
    """Builds a PERSONA_DIR on disk. Used instead of mocking the filesystem
    because every failure this module exists to catch is a filesystem shape."""

    def __init__(self):
        self.root = tempfile.mkdtemp()

    def tree(self, mode, shared="Say nothing if you have nothing. You are {{PERSONA}}."):
        if shared is not None:
            write(os.path.join(self.root, mode, "_shared.md"), shared)
        return self

    def persona(self, mode, pid, label="Red Team", body="Attack the change."):
        fm = f"---\nlabel: {label}\nsuccess: Finds real holes.\n---\n"
        write(os.path.join(self.root, mode, f"{pid}.md"), fm + body)
        return self

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class ShippedPersonasTest(unittest.TestCase):
    def test_code_default_resolves_to_four(self):
        got = personas.resolve("code", SHIPPED, {})
        self.assertEqual([p.id for p in got], ["red_team", "adversarial", "sme", "sage"])

    def test_plan_default_resolves_to_six(self):
        got = personas.resolve("plan", SHIPPED, {})
        self.assertEqual(len(got), 6)

    def test_prompt_is_body_then_shared_contract(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "red_team"})
        self.assertEqual(len(got), 1)
        self.assertGreater(len(got[0].prompt), 100)
        self.assertNotIn("{{PERSONA}}", got[0].prompt)
        self.assertIn(got[0].label, got[0].prompt)

    def test_frontmatter_is_not_in_the_prompt(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "red_team"})
        self.assertNotIn("success:", got[0].prompt)

    def test_all_selects_every_persona_in_the_tree(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "all"})
        self.assertEqual(len(got), 6)

    def test_all_is_case_insensitive(self):
        self.assertEqual(
            len(personas.resolve("code", SHIPPED, {"PERSONAS": "ALL"})), 6
        )

    def test_selector_order_is_preserved(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "sage,red_team"})
        self.assertEqual([p.id for p in got], ["sage", "red_team"])

    def test_whitespace_separated_selector(self):
        got = personas.resolve("code", SHIPPED, {"PERSONAS": "sage red_team"})
        self.assertEqual([p.id for p in got], ["sage", "red_team"])

    def test_plan_selector_var_is_separate(self):
        got = personas.resolve("plan", SHIPPED, {"PERSONAS": "sage"})
        self.assertEqual(len(got), 6)


class RefusalTest(unittest.TestCase):
    def setUp(self):
        self.b = TreeBuilder()
        self.addCleanup(self.b.cleanup)

    def test_missing_persona_dir(self):
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", "/nonexistent/personas", {})
        self.assertIn("is not a directory", str(cm.exception))

    def test_flat_layout_names_the_subdirectories(self):
        write(os.path.join(self.b.root, "_shared.md"), "contract")
        write(os.path.join(self.b.root, "red_team.md"), "---\nlabel: X\n---\nbody")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {})
        self.assertIn("code/", str(cm.exception))
        self.assertIn("plan/", str(cm.exception))

    def test_missing_shared_contract(self):
        self.b.tree("code", shared=None).persona("code", "red_team")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {})
        self.assertIn("_shared.md", str(cm.exception))

    def test_empty_body_is_refused(self):
        # Judged on the persona's OWN body, before the shared contract is
        # appended: a body-plus-contract string is never empty, so judging the
        # composed prompt lets a frontmatter-only file review a PR as an
        # identity-free reviewer signing a label it has no angle behind.
        self.b.tree("code").persona("code", "hollow", body="")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {"PERSONAS": "hollow"})
        self.assertIn("empty prompt body", str(cm.exception))

    def test_whitespace_only_body_is_refused(self):
        self.b.tree("code").persona("code", "hollow", body="  \n\n\t\n")
        with self.assertRaises(ConfigError):
            personas.resolve("code", self.b.root, {"PERSONAS": "hollow"})

    def test_missing_label_is_refused(self):
        self.b.tree("code")
        write(
            os.path.join(self.b.root, "code", "nolabel.md"),
            "---\nsuccess: nothing\n---\nbody here",
        )
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {"PERSONAS": "nolabel"})
        self.assertIn("no label", str(cm.exception))

    def test_label_with_a_slash_is_refused(self):
        self.b.tree("code").persona("code", "bad", label="Red/Team")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {"PERSONAS": "bad"})
        self.assertIn("unexpected characters", str(cm.exception))

    def test_unknown_persona_is_refused(self):
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": "nosuch"})
        self.assertIn("unknown persona", str(cm.exception))

    def test_reserved_persona_is_refused(self):
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": "aggregate"})
        self.assertIn("reserved", str(cm.exception))

    def test_duplicate_persona_is_refused(self):
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": "sage,sage"})
        self.assertIn("twice", str(cm.exception))

    def test_empty_selector_is_refused(self):
        # PERSONAS set but naming nothing. The shell used ${PERSONAS-$def}, so an
        # explicitly-empty value does NOT fall back to the default; it is an error.
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": ""})
        self.assertIn("names no persona", str(cm.exception))

    def test_underscore_files_are_not_personas(self):
        with self.assertRaises(ConfigError):
            personas.resolve("code", SHIPPED, {"PERSONAS": "_shared"})

    def test_glob_is_a_name_not_a_pattern(self):
        # The bash version word-split AND glob-expanded here, so a PERSONAS of
        # '*' resolved against the current directory. Python cannot do that, and
        # this test pins the property the bash suite already asserted.
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", SHIPPED, {"PERSONAS": "*"})
        self.assertIn("unknown persona", str(cm.exception))

    def test_empty_tree_is_refused(self):
        self.b.tree("code")
        with self.assertRaises(ConfigError) as cm:
            personas.resolve("code", self.b.root, {})
        self.assertIn("no persona definitions found", str(cm.exception))

    def test_unknown_mode_is_refused(self):
        with self.assertRaises(ConfigError):
            personas.resolve("aesthetic", SHIPPED, {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'personas'`

- [ ] **Step 3: Write the implementation**

Create `reviewer/personas.py`:

```python
"""Persona resolution.

PERSONA_DIR is a parent holding one tree per review mode. A persona is a file in
PERSONA_DIR/<mode>: frontmatter (label, success) plus a body that becomes the
pass's system prompt. Files starting with an underscore are not personas;
_shared.md is the output contract appended to every persona body in that tree.

Everything here raises ConfigError rather than returning a sentinel. A typo that
silently narrowed the review to one persona, or to none, would look exactly like
a working run in the log.
"""

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple

from common import ConfigError

REVIEW_MODES: Tuple[str, ...] = ("code", "plan")

# The code default is the subset: advocate's `user` and `good_friend` were
# written against designs and whole projects, so on a narrow diff they reach for
# material that isn't in it. Plan mode is where they finally have something to
# bite on, which is why the plan default is everything.
DEFAULTS: Dict[str, str] = {
    "code": "red_team,adversarial,sme,sage",
    "plan": "adversarial,good_friend,red_team,sage,sme,user",
}

# The selector env var per mode. The bare name means code mode.
SELECTOR_VAR: Dict[str, str] = {"code": "PERSONAS", "plan": "PLAN_PERSONAS"}

# Claimed now, used in phase 2: the pass that reconciles what the personas said
# is the only one allowed to read their findings, which is why it is not itself
# a persona and cannot be selected as one.
RESERVED = frozenset({"aggregate"})

_LABEL_OK = re.compile(r"^[A-Za-z0-9 ._-]+$")


@dataclass(frozen=True)
class Persona:
    id: str
    label: str
    prompt: str


def _split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Return (frontmatter, body). A file with no leading '---' is all body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: Dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1 :])
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip()
    # Unterminated frontmatter: no body at all.
    return meta, ""


def _available(directory: str) -> List[str]:
    out = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md") or name.startswith("_"):
            continue
        out.append(name[: -len(".md")])
    return out


def _selected(mode: str, env: Mapping[str, str], available: List[str]) -> List[str]:
    var = SELECTOR_VAR[mode]
    raw = env[var] if var in env else DEFAULTS[mode]
    if raw.strip().lower() == "all":
        raw = ",".join(available)

    chosen: List[str] = []
    for token in raw.replace(",", " ").split():
        if token in RESERVED:
            raise ConfigError(f"persona '{token}' is reserved and cannot be selected.")
        if token not in available:
            raise ConfigError(
                f"unknown persona '{token}' for {mode} review; available: "
                + " ".join(available)
            )
        if token in chosen:
            raise ConfigError(f"persona '{token}' is listed twice for {mode} review.")
        chosen.append(token)

    if not chosen:
        raise ConfigError(
            f"{var} is set but names no persona; unset it for the default set "
            f"({DEFAULTS[mode]}), or name one of: " + " ".join(available)
        )
    return chosen


def resolve(mode: str, persona_dir: str, env: Mapping[str, str]) -> List[Persona]:
    if mode not in REVIEW_MODES:
        raise ConfigError(
            f"unknown review mode '{mode}'; expected one of: " + ", ".join(REVIEW_MODES)
        )
    if not os.path.isdir(persona_dir):
        raise ConfigError(
            f"no persona definitions: PERSONA_DIR={persona_dir} is not a directory."
        )

    directory = os.path.join(persona_dir, mode)
    if not os.path.isdir(directory):
        # The flat layout phase 1 shipped is reachable by exactly the
        # mount-your-own-personas workflow the docs advertise, so it has to say
        # what changed rather than dying on a missing file three checks later.
        if any(n.endswith(".md") for n in os.listdir(persona_dir)):
            raise ConfigError(
                "PERSONA_DIR now holds one tree per review mode: "
                f"{persona_dir} needs code/ and plan/ subdirectories, but its "
                "persona files sit directly in it."
            )
        raise ConfigError(
            f"no persona definitions for {mode} review: {directory} is not a directory."
        )

    shared_path = os.path.join(directory, "_shared.md")
    if not os.path.isfile(shared_path):
        raise ConfigError(
            f"no output contract: {shared_path} is missing; every persona body "
            "is appended to it."
        )
    with open(shared_path, encoding="utf-8") as fh:
        shared = fh.read()

    available = _available(directory)
    if not available:
        raise ConfigError(f"no persona definitions found in {directory}.")

    out: List[Persona] = []
    for pid in _selected(mode, env, available):
        with open(os.path.join(directory, f"{pid}.md"), encoding="utf-8") as fh:
            meta, body = _split_frontmatter(fh.read())

        label = meta.get("label", "")
        if not label:
            raise ConfigError(f"persona '{mode}/{pid}' has no label: in its frontmatter.")
        if not _LABEL_OK.match(label):
            raise ConfigError(
                f"persona '{mode}/{pid}' has a label with unexpected characters: "
                f"'{label}' (letters, digits, spaces, dot, underscore and hyphen only)."
            )
        # Judged on the body ALONE, before the contract is appended. See the
        # test of the same name for why.
        if not body.strip():
            raise ConfigError(f"persona '{mode}/{pid}' has an empty prompt body.")

        prompt = (body + "\n" + shared).replace("{{PERSONA}}", label)
        out.append(Persona(id=pid, label=label, prompt=prompt))

    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -v`
Expected: PASS, all tests including the 20 from earlier tasks

- [ ] **Step 5: Commit**

```bash
git add reviewer/personas.py tests/test_personas.py
git commit -m "feat(reviewer): port persona resolution"
```

---

### Task 5: `gh.py`

**Files:**
- Create: `reviewer/gh.py`
- Create: `tests/test_gh.py`
- Reference: `entrypoint.sh:127-227` (`pr_truthy`, `parse_pr_ids`, `resolve_pr_selection`, `pr_modes`, `enumerate_candidate_prs`)

**Interfaces:**
- Consumes: `ConfigError`, `log` from `common`
- Produces:
  - `pr_truthy(value: str | None) -> bool`
  - `parse_pr_ids(raw: str) -> list[int]`
  - `resolve_pr_selection(env: Mapping[str, str]) -> str` returning `"all" | "assignee" | "ids" | "search"`
  - `pr_modes(payload: object, plan_label: str) -> list[tuple[int, str]]`
  - `enumerate_candidate_prs(selector: str, env: Mapping[str, str], run=subprocess.run) -> list[tuple[int, str]]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gh.py`:

```python
import json
import subprocess
import unittest

import _path  # noqa: F401

import gh
from common import ConfigError


class Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


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


class PrModesTest(unittest.TestCase):
    def test_list_payload_routes_by_label(self):
        payload = [
            {"number": 12, "labels": [{"name": "plan"}]},
            {"number": 13, "labels": [{"name": "bug"}]},
        ]
        self.assertEqual(gh.pr_modes(payload, "plan"), [(12, "plan"), (13, "code")])

    def test_single_object_payload(self):
        self.assertEqual(
            gh.pr_modes({"number": 12, "labels": []}, "plan"), [(12, "code")]
        )

    def test_missing_labels_key_is_code_mode(self):
        # `.labels[]?` in the jq version read a missing key as no labels, so a PR
        # object arriving without one is code mode: the same answer an unlabeled
        # PR gets.
        self.assertEqual(gh.pr_modes({"number": 12}, "plan"), [(12, "code")])

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
        self.assertEqual(gh.pr_modes(payload, "design"), [(12, "plan")])

    def test_label_without_a_name_key_does_not_crash(self):
        payload = [{"number": 12, "labels": [{"color": "f00"}]}]
        self.assertEqual(gh.pr_modes(payload, "plan"), [(12, "code")])


class EnumerateTest(unittest.TestCase):
    ENV = {"GITHUB_REPOSITORY": "owner/repo", "PLAN_LABEL": "plan"}

    def test_all_selector_calls_gh_pr_list_once(self):
        run = runner(Result(0, json.dumps([{"number": 12, "labels": []}])))
        got = gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run)
        self.assertEqual(got, [(12, "code")])
        self.assertEqual(len(run.calls), 1)
        self.assertIn("--json", run.calls[0])
        self.assertIn("number,labels", run.calls[0])

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
        self.assertEqual(got, [(12, "plan"), (13, "code")])
        self.assertEqual(len(run.calls), 2)

    def test_ids_failed_lookup_skips_that_pr_only(self):
        # It is never guessed into code mode: a wrong-mode review posts real
        # comments on a real PR and cannot be taken back, where a skip is one
        # log line and a retry next cycle.
        run = runner(Result(1, ""), Result(0, json.dumps({"number": 13, "labels": []})))
        got = gh.enumerate_candidate_prs("ids", dict(self.ENV, PR_IDS="12,13"), run=run)
        self.assertEqual(got, [(13, "code")])

    def test_ids_empty_but_successful_lookup_skips(self):
        # A gh that exits 0 with empty stdout must not drop the PR silently.
        run = runner(Result(0, "   "))
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gh'`

- [ ] **Step 3: Write the implementation**

Create `reviewer/gh.py`:

```python
"""PR selection and review-mode routing.

Which PRs to review is chosen by exactly one selector env var. Review mode is
decided here too, at the one seam that already decides what gets reviewed at
all, so nothing downstream asks GitHub a second time.

Everything that reads gh output treats "exited 0 with nothing usable" as the
default hazard rather than an edge case. That is not defensiveness for its own
sake: a successful-but-empty label query once dropped a PR with no log line, and
an unguarded number field once produced a candidate PR literally named `null`.
"""

import json
import subprocess
from typing import Any, Callable, List, Mapping, Tuple

from common import ConfigError, log

Candidate = Tuple[int, str]


def pr_truthy(value) -> bool:
    """True when the value is a truthy flag: 1 / true / yes, any case.

    Deliberately does not strip: the shell version piped through `tr` without
    trimming, so " true" was falsy there and stays falsy here.
    """
    return (value or "").lower() in ("1", "true", "yes")


def parse_pr_ids(raw: str) -> List[int]:
    """Split a comma/whitespace-separated list into ints, refusing anything else."""
    out: List[int] = []
    for token in (raw or "").replace(",", " ").split():
        if not token.isdigit():
            raise ConfigError(
                f"PR_IDS contains a non-numeric value: '{token}' (expected e.g. 12,15,20)"
            )
        out.append(int(token))
    return out


def resolve_pr_selection(env: Mapping[str, str]) -> str:
    """Return the single active selector, or raise."""
    active: List[str] = []
    if pr_truthy(env.get("PR_ALL")):
        active.append("all")
    if env.get("PR_ASSIGNEE"):
        active.append("assignee")
    if env.get("PR_IDS"):
        active.append("ids")
    if env.get("PR_SEARCH"):
        active.append("search")

    if not active:
        raise ConfigError(
            "no PR selector set; provide exactly one of PR_ALL, PR_ASSIGNEE, "
            "PR_IDS, PR_SEARCH (launcher: --all / --assignee / --prs / --search)."
        )
    if len(active) > 1:
        raise ConfigError(
            "multiple PR selectors set; provide exactly one of PR_ALL, "
            "PR_ASSIGNEE, PR_IDS, PR_SEARCH."
        )
    selector = active[0]
    # Validate the ID list up front so a bad value fails fast, not every cycle.
    if selector == "ids":
        parse_pr_ids(env.get("PR_IDS", ""))
    return selector


def pr_modes(payload: Any, plan_label: str) -> List[Candidate]:
    """Turn gh --json number,labels output into (number, mode) pairs.

    Accepts an array (gh pr list) or a single object (gh pr view). A PR carrying
    plan_label is plan mode; everything else is code mode, so an operator who
    never labels anything sees exactly the pre-modes behavior.
    """
    entries = payload if isinstance(payload, list) else [payload]
    out: List[Candidate] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if not isinstance(number, int):
            continue
        labels = entry.get("labels") or []
        is_plan = any(
            isinstance(lbl, dict) and lbl.get("name") == plan_label for lbl in labels
        )
        out.append((number, "plan" if is_plan else "code"))
    return out


def _read_json(result) -> Any:
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def enumerate_candidate_prs(
    selector: str,
    env: Mapping[str, str],
    run: Callable[..., Any] = subprocess.run,
) -> List[Candidate]:
    """One (number, mode) pair per candidate PR."""
    repo = env["GITHUB_REPOSITORY"]
    plan_label = env.get("PLAN_LABEL") or "plan"

    def gh_run(argv):
        return run(
            argv, capture_output=True, text=True, check=False
        )

    if selector == "ids":
        out: List[Candidate] = []
        for number in parse_pr_ids(env.get("PR_IDS", "")):
            argv = [
                "gh", "pr", "view", str(number), "-R", repo, "--json", "number,labels",
            ]
            got = pr_modes(_read_json(gh_run(argv)) or [], plan_label)
            if got:
                out.extend(got)
            else:
                log(f"WARN: could not read labels for PR #{number}; skipping it this cycle.")
        return out

    base = ["gh", "pr", "list", "-R", repo]
    if selector == "all":
        argv = base + ["--state", "open", "--limit", "100", "--json", "number,labels"]
    elif selector == "assignee":
        argv = base + [
            "--state", "open", "--assignee", env["PR_ASSIGNEE"],
            "--limit", "100", "--json", "number,labels",
        ]
    elif selector == "search":
        argv = base + [
            "--search", env["PR_SEARCH"], "--limit", "100", "--json", "number,labels",
        ]
    else:
        raise ConfigError(f"unknown PR selector '{selector}'.")

    payload = _read_json(gh_run(argv))
    if payload is None:
        # Deliberate departure from the shell, which logged the same
        # "No candidate PRs" line whether gh failed or there simply were none.
        log(f"WARN: `gh pr list` failed or returned nothing usable for selector '{selector}'.")
        return []
    return pr_modes(payload, plan_label)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/gh.py tests/test_gh.py
git commit -m "feat(reviewer): port PR selection and mode routing"
```

---

### Task 6: `passes.py`

**Files:**
- Create: `reviewer/passes.py`
- Create: `tests/test_passes.py`
- Reference: `entrypoint.sh:1199-1259` (`USAGE_LIMIT_RE`, `is_usage_limit`, `usage_limit_line`, `run_pass`) and `entrypoint.sh:format_stream`

**Interfaces:**
- Consumes: `Pair`, `log` from `common`
- Produces:
  - `USAGE_LIMIT_RE: re.Pattern`
  - `is_usage_limit(text: str) -> bool`
  - `usage_limit_line(text: str) -> str` (first matching line, truncated to 400 chars, `""` if none)
  - `format_event(event: dict) -> list[str]` (the human-readable lines for one `stream-json` event)
  - `PassResult` frozen dataclass with `rc: int`, `session_id: str | None`, `limited: bool`, `limit_line: str`
  - `run_pass(pair, prompt, session_id, persona_prompt, model, mcp_args, cwd, popen=subprocess.Popen) -> PassResult`

- [ ] **Step 1: Write the failing test**

Create `tests/test_passes.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'passes'`

- [ ] **Step 3: Write the implementation**

Create `reviewer/passes.py`:

```python
"""Running one review pass.

One `claude` invocation, its stream-json consumed as it arrives, its stderr
captured to a temp file. stdout is the only pipe on purpose: reading two pipes
from one child is where deadlocks live, and the shell version already sent
stderr to a file.
"""

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from common import Pair, log

# Matches provider error text, an upstream surface that can change without
# notice, so the failure mode of a miss matters: a missed match falls through to
# the ordinary path (drop the session, carry on), which is the pre-existing
# behavior. A false positive keeps a session that will fail again next cycle and
# be dropped then. Neither wedges the loop.
#
# `limit reached` and `reached your limit` are here because `limit` on its own is
# only reachable via `rate.?limit` and `usage limit`, so the near-miss wordings
# (`5-hour limit reached`, `you have reached your limit`) matched nothing.
#
# re.MULTILINE IS LOAD-BEARING. The shell used grep, which is line-oriented.
# Python's ^ and $ anchor the whole string without it, so the 429/529 arm would
# silently stop matching a status code on its own line.
USAGE_LIMIT_RE = re.compile(
    r"rate.?limit|usage limit|limit reached|reached your limit|too many requests"
    r"|quota|overloaded|(^|[^0-9])(429|529)([^0-9]|$)",
    re.IGNORECASE | re.MULTILINE,
)

_WHITESPACE = re.compile(r"[\n\t ]+")


def is_usage_limit(text: str) -> bool:
    return bool(USAGE_LIMIT_RE.search(text or ""))


def usage_limit_line(text: str) -> str:
    """The first line that read as a limit, truncated.

    The classifier scans the whole stderr while the log tails only its last few
    lines, so a limit reported early in a long stderr is classified right and
    invisible to whoever reads the log. Only the matched line, and only 400
    characters of it: claude's stderr is not a stream that can be assumed
    credential-free.
    """
    for line in (text or "").splitlines():
        if USAGE_LIMIT_RE.search(line):
            return line[:400]
    return ""


def format_event(event: Dict[str, Any]) -> List[str]:
    """Human-readable lines for one stream-json event, or none."""
    etype = event.get("type")

    if etype == "system" and event.get("subtype") == "init":
        return [f"  ▸ session {event.get('session_id')} started"]

    if etype == "assistant":
        out: List[str] = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text") or ""
                if text:
                    out.extend(text.split("\n"))
            elif block.get("type") == "tool_use":
                payload = json.dumps(block.get("input"))[:200]
                out.append(f"  → {block.get('name')}: {payload}")
        return out

    if etype == "user":
        out = []
        for block in (event.get("message") or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, list):
                text = " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
            else:
                text = "" if content is None else str(content)
            out.append("  ← " + _WHITESPACE.sub(" ", text).strip()[:200])
        return out

    if etype == "result":
        return [
            f"  ✓ result ({event.get('subtype') or ''}): "
            + str(event.get("result") or "")[:800]
        ]

    return []


def build_argv(
    session_id: Optional[str],
    model: str,
    persona_prompt: str,
    mcp_args: List[str],
    prompt: str,
) -> List[str]:
    """The claude invocation.

    --append-system-prompt is passed on EVERY invocation, resumed included: the
    flag does not survive --resume (measured 2026-08-21). It also keeps the
    persona out of the task prompt, which is what makes the verbatim-operator-
    prompt guarantee possible.

    The `--` before the prompt is load-bearing: --mcp-config is variadic, so
    without it the CLI parses the prompt as another config path.
    """
    argv = ["claude", "-p"]
    if session_id:
        argv += ["--resume", session_id]
    argv += [
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--model", model,
        "--append-system-prompt", persona_prompt,
    ]
    argv += list(mcp_args)
    argv += ["--", prompt]
    return argv


@dataclass(frozen=True)
class PassResult:
    rc: int
    session_id: Optional[str]
    limited: bool
    limit_line: str


def run_pass(
    pair: Pair,
    prompt: str,
    session_id: Optional[str],
    persona_prompt: str,
    model: str,
    mcp_args: List[str],
    cwd: str,
    popen: Callable[..., Any] = subprocess.Popen,
) -> PassResult:
    argv = build_argv(session_id, model, persona_prompt, mcp_args, prompt)
    recovered = session_id

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errfile:
        proc = popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=errfile,
            cwd=cwd,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            # Take the last session_id seen. Recovered here, as the stream
            # arrives, so it is in hand before the exit code is judged.
            if event.get("session_id"):
                recovered = event["session_id"]
            for out in format_event(event):
                log(out, pair=pair)
        proc.stdout.close()
        rc = proc.wait()

        errfile.seek(0)
        stderr = errfile.read()

    if rc != 0:
        limited = is_usage_limit(stderr)
        line = usage_limit_line(stderr) if limited else ""
        log(f"WARN: claude exited {rc}:", pair=pair)
        for tail in stderr.splitlines()[-5:]:
            log(f"  {tail}", pair=pair)
        return PassResult(rc=rc, session_id=recovered, limited=limited, limit_line=line)

    return PassResult(rc=0, session_id=recovered, limited=False, limit_line="")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/passes.py tests/test_passes.py
git commit -m "feat(reviewer): port pass execution and usage-limit classification"
```

---

### Task 7: `review_loop.py`

**Files:**
- Create: `reviewer/review_loop.py`
- Create: `tests/test_review_loop.py`
- Reference: `entrypoint.sh:1263-1399` (the loop) and `entrypoint.sh:823-834` (`check_litellm`)

**Interfaces:**
- Consumes: everything from Tasks 1, 3, 4, 5, 6
- Produces:
  - `Supervisor` class holding `sessions: dict[Pair, str]`, `passes_done: dict[Pair, int]`, `resume_at: Pair | None`
  - `Supervisor.build_pairs(candidates: list[tuple[int, str]]) -> list[Pair]`
  - `Supervisor.start_index(pairs: list[Pair]) -> int`
  - `Supervisor.run_cycle(pairs: list[Pair]) -> bool` returning True when a usage limit cut it
  - `check_litellm(env) -> None`
  - `main() -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_review_loop.py`:

```python
import unittest

import _path  # noqa: F401

import review_loop
from common import Pair
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'review_loop'`

- [ ] **Step 3: Write the implementation**

Create `reviewer/review_loop.py`:

```python
#!/usr/bin/env python3
"""The review supervisor.

One Claude session per (PR, mode, persona) triple. Each cycle: fetch refs,
enumerate candidate PRs with their review mode, review each pair in its own
session (new, or --resume of that triple's session so a persona will not
re-raise findings it already raised), then sleep.

Claude Code's /loop cannot be used here because it needs a live interactive
session, which headless -p is not. This loop plus --resume gives the same
continuous, context-retaining behavior while staying headless and crash-safe.

Reached by `exec` from entrypoint.sh, which owns everything upstream of this:
hardening checks, gh/git auth, the provider environment, the working clone, and
the LiteLLM translator when one is running.
"""

import os
import subprocess
import sys
import time
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import gh
import passes
import personas as personas_mod
import prompts as prompts_mod
from common import ConfigError, Pair, die, log

# Not operator-configurable: this is a guard against a dead provider, not a
# tuning knob. Connection refused, a dead LiteLLM translator, a gateway 502:
# none classify as a limit, and each failure drops its pair's session, so
# walking a whole list into a dead endpoint costs a duplicate-comment burst per
# pair.
MAX_CONSECUTIVE_FAILURES = 3


def parse_max_cycles(env: Mapping[str, str]) -> Optional[int]:
    """How many cycles to run before exiting. Unset or 0 means forever.

    Exists so the acceptance suites can stop the loop deterministically instead
    of relying on a stubbed `sleep` exiting non-zero, and so `claudebox.sh test`
    can be a genuine one-shot.
    """
    raw = env.get("MAX_CYCLES", "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ConfigError("MAX_CYCLES must be a non-negative integer")
    return int(raw) or None


def prompt_token_warnings(review: Mapping[str, str], followup: Mapping[str, str]) -> List[str]:
    """One warning per prompt that will not name the PR it is reviewing.

    Reachable only through an operator override, and worth a line because the
    resulting reviews look like a model ignoring instructions rather than like
    a configuration mistake.
    """
    out = []
    for label, table in (("review", review), ("followup", followup)):
        for mode in sorted(table):
            if "{{PR}}" not in table[mode]:
                out.append(
                    f"WARN: the {mode} {label} prompt has no {{{{PR}}}} token; "
                    "reviews won't name the specific PR."
                )
    return out


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise ConfigError(f"{name} must be a non-negative integer")
    return int(raw)


def check_litellm(env: Mapping[str, str]) -> None:
    """Are the workersai translators still up?

    Called each cycle so a dead one is a loud, fatal error rather than every
    review pass failing on connection refused. No-op for every other provider.

    We are PID 1 after the exec, so a dead child becomes a zombie that os.kill
    would still find. Reap first, non-blockingly, or the check never fires.
    """
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break

    for var, label, logfile, lines in (
        ("SHIM_PID", "Workers AI normalizer", "shim.log", 20),
        ("LITELLM_PID", "LiteLLM translator", "litellm.log", 40),
    ):
        raw = env.get(var, "").strip()
        if not raw.isdigit():
            continue
        try:
            os.kill(int(raw), 0)
        except OSError:
            path = os.path.join(env.get("HOME", ""), logfile)
            log(f"--- last {lines} lines of the {label} log ---")
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh.read().splitlines()[-lines:]:
                        log(line)
            except OSError:
                pass
            die(f"the {label} died (log above). Restarting the container will bring it back.")


class Supervisor:
    """Owns the per-(PR, mode, persona) state and the shape of a cycle.

    All of this is in memory, so a container restart re-reviews each PR once per
    persona and may re-comment once. Persisting it is deferred.
    """

    def __init__(
        self,
        personas: Dict[str, List[str]],
        persona_prompts: Dict[Tuple[str, str], str],
        review_prompts: Dict[str, str],
        followup_prompts: Dict[str, str],
        model: str,
        mcp_args: List[str],
        cwd: str,
        max_passes_per_session: int,
    ):
        self.personas = personas
        self.persona_prompts = persona_prompts
        self.review_prompts = review_prompts
        self.followup_prompts = followup_prompts
        self.model = model
        self.mcp_args = mcp_args
        self.cwd = cwd
        self.max_passes_per_session = max_passes_per_session

        self.sessions: Dict[Pair, str] = {}
        self.passes_done: Dict[Pair, int] = {}
        # The pair the next cycle starts at. None means "start at the first".
        # Without it, a limit that allows only a few passes per backoff window
        # would review the leading pairs forever and the trailing ones never.
        self.resume_at: Optional[Pair] = None

    def build_pairs(self, candidates: Sequence[Tuple[int, str]]) -> List[Pair]:
        out: List[Pair] = []
        for pr, mode in candidates:
            for persona in self.personas[mode]:
                out.append(Pair(pr, mode, persona))
        return out

    def start_index(self, pairs: Sequence[Pair]) -> int:
        """Where this cycle begins.

        A resume point that no longer exists (its PR closed, the persona set
        changed) falls back to the head of the list rather than skipping a cycle.
        """
        if self.resume_at is None:
            return 0
        try:
            index = pairs.index(self.resume_at)
        except ValueError:
            return 0
        if index:
            log(f"Starting this cycle at {pairs[index]}, where the last one was cut.")
        return index

    def _run_one(self, pair: Pair, prompt: str, session_id: Optional[str]):
        return passes.run_pass(
            pair=pair,
            prompt=prompt,
            session_id=session_id,
            persona_prompt=self.persona_prompts[(pair.mode, pair.persona)],
            model=self.model,
            mcp_args=self.mcp_args,
            cwd=self.cwd,
        )

    def run_cycle(self, pairs: Sequence[Pair]) -> bool:
        """Walk the pair list. Returns True when a usage limit cut it short."""
        count = len(pairs)
        if not count:
            # An empty list keeps the resume point rather than clearing it:
            # enumeration failures degrade to an empty candidate list, and that
            # must not silently send the next cycle back to the head.
            return False

        start = self.start_index(pairs)
        consecutive_failures = 0
        cut_at: Optional[int] = None
        cut_offset = -1
        was_limited = False

        for offset in range(count):
            index = (start + offset) % count
            pair = pairs[index]
            session_id = self.sessions.get(pair)

            if session_id:
                log(f"Reviewing PR #{pair.pr} [{pair.mode}/{pair.persona}] "
                    f"(resuming session {session_id})...")
                template = self.followup_prompts[pair.mode]
            else:
                log(f"Reviewing PR #{pair.pr} [{pair.mode}/{pair.persona}] (new session)...")
                template = self.review_prompts[pair.mode]

            result = self._run_one(pair, prompts_mod.render(template, pair.pr), session_id)

            if result.rc == 0:
                consecutive_failures = 0
                if result.session_id:
                    self.sessions[pair] = result.session_id
                self.passes_done[pair] = self.passes_done.get(pair, 0) + 1
                log(f"PR #{pair.pr} [{pair.mode}/{pair.persona}] review complete "
                    f"(session {self.sessions.get(pair)}, pass {self.passes_done[pair]}).")
                if (
                    self.max_passes_per_session > 0
                    and self.passes_done[pair] >= self.max_passes_per_session
                ):
                    log(f"PR #{pair.pr} [{pair.mode}/{pair.persona}] reached "
                        f"MAX_PASSES_PER_SESSION={self.max_passes_per_session}; "
                        "rotating its session next cycle.")
                    self.sessions.pop(pair, None)
                    self.passes_done[pair] = 0
                continue

            if result.limited:
                # Keep the session. Abandon the rest of the cycle rather than
                # walking the remaining pairs into the same wall.
                if result.session_id:
                    self.sessions[pair] = result.session_id
                    log(f"WARN: PR #{pair.pr} [{pair.mode}/{pair.persona}] hit a usage or "
                        "rate limit; keeping its session and ending this cycle early.")
                else:
                    log(f"WARN: PR #{pair.pr} [{pair.mode}/{pair.persona}] hit a usage or "
                        "rate limit before it had a session; ending this cycle early.")
                if result.limit_line:
                    log(f"  limit reported by claude: {result.limit_line}")
                was_limited = True
                cut_at, cut_offset = index, offset
                break

            log(f"WARN: PR #{pair.pr} [{pair.mode}/{pair.persona}] review failed; "
                "starting a fresh session for it next cycle.")
            self.sessions.pop(pair, None)
            self.passes_done[pair] = 0
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Not a limit, so no backoff: the next cycle comes at the
                # ordinary interval, and starts where this one stopped.
                log(f"WARN: {consecutive_failures} passes in a row failed for reasons "
                    "other than a limit; the provider looks unhealthy. Abandoning this cycle.")
                cut_at, cut_offset = index, offset
                break

        if cut_at is not None:
            self.resume_at = pairs[(cut_at + 1) % count]
            skipped = [pairs[(start + o) % count] for o in range(cut_offset + 1, count)]
            if skipped:
                log("Not reviewed this cycle: " + " ".join(str(p) for p in skipped)
                    + f". The next cycle starts at {self.resume_at}.")
            else:
                log(f"The next cycle starts at {self.resume_at}.")
        else:
            self.resume_at = None

        return was_limited


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    env = os.environ

    try:
        selector = gh.resolve_pr_selection(env)
        persona_dir = env.get("PERSONA_DIR") or "/opt/claudebox/personas"
        # Resolved for EVERY mode at startup, even one no PR currently uses, so
        # a broken definition fails at boot rather than the first time somebody
        # adds a label to a PR.
        resolved = {}
        for mode in personas_mod.REVIEW_MODES:
            resolved[mode] = personas_mod.resolve(mode, persona_dir, env)
            log(f"{mode} personas: " + " ".join(p.id for p in resolved[mode]))

        built = prompts_mod.build(env)
        for warning in prompt_token_warnings(built.review, built.followup):
            log(warning)
        max_cycles = parse_max_cycles(env)
        interval = _positive_int(env, "REVIEW_INTERVAL_SECONDS", 300)
        backoff = _positive_int(env, "LIMIT_BACKOFF_SECONDS", 1800)
        max_passes = _positive_int(env, "MAX_PASSES_PER_SESSION", 0)
    except ConfigError as exc:
        die(str(exc))

    mcp_args = ["--strict-mcp-config"]
    mcp_config = env.get("MCP_CONFIG_FILE", "")
    if mcp_config and os.path.isfile(mcp_config):
        mcp_args += ["--mcp-config", mcp_config]

    supervisor = Supervisor(
        personas={m: [p.id for p in ps] for m, ps in resolved.items()},
        persona_prompts={(m, p.id): p.prompt for m, ps in resolved.items() for p in ps},
        review_prompts=built.review,
        followup_prompts=built.followup,
        model=env["REVIEW_MODEL"],
        mcp_args=mcp_args,
        cwd=env["WORK_REPO"],
        max_passes_per_session=max_passes,
    )

    cycles = 0
    while True:
        check_litellm(env)

        log("Fetching latest refs...")
        fetched = subprocess.run(
            ["git", "fetch", "--all", "--prune", "--quiet"],
            cwd=supervisor.cwd, capture_output=True, text=True, check=False,
        )
        if fetched.returncode != 0:
            log("WARN: git fetch failed; continuing")

        try:
            candidates = gh.enumerate_candidate_prs(selector, env)
        except ConfigError as exc:
            die(str(exc))

        if not candidates:
            log(f"No candidate PRs for selector '{selector}'.")
        else:
            log(f"Candidate PRs ({selector}): "
                + " ".join(f"{pr}:{mode}" for pr, mode in candidates))

        limited = supervisor.run_cycle(supervisor.build_pairs(candidates))

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            log(f"Reached MAX_CYCLES={max_cycles}; exiting.")
            return 0

        if limited:
            log(f"Backing off {backoff}s after a usage limit...")
            time.sleep(backoff)
        else:
            log(f"Sleeping {interval}s...")
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/review_loop.py tests/test_review_loop.py
git commit -m "feat(reviewer): port the review loop supervisor"
```

---

### Task 8: Wire `entrypoint.sh` and the Dockerfile

**Files:**
- Modify: `entrypoint.sh` (delete the ported blocks, export the handoff vars, exec)
- Modify: `Dockerfile` (copy `reviewer/`)

**Interfaces:**
- Consumes: `reviewer/review_loop.py` from Task 7
- Produces: an `entrypoint.sh` that ends in `exec python3 /opt/claudebox/reviewer/review_loop.py`, with `WORK_REPO`, `REVIEW_MODEL`, `MCP_CONFIG_FILE`, `LITELLM_PID` and `SHIM_PID` exported into its environment

- [ ] **Step 1: Delete the ported blocks from `entrypoint.sh`**

Line numbers below were verified against the file at commit `df8f489`. Work **bottom-up** so earlier deletions do not shift later ranges.

| Lines | What |
|---|---|
| 1126-end | `# --- Review loop` header, the session maps, `is_usage_limit`, `usage_limit_line`, `run_pass`, `format_stream`, `cd "$WORK_REPO"`, and the whole `while true` loop |
| 823-834 | `check_litellm` |
| 483-573 | the prompt block: its long comment, the four stanzas, the four defaults, the Linear composition, the `MODE_*` tables, the four suffix blocks, and the startup calls to `resolve_pr_selection` / `resolve_personas` / the `{{PR}}`-token warning loop |
| 395-400 | `linear_stanza` and its two-line comment |
| 235-384 | the whole persona registry section, from `# --- Persona registry` through the end of `resolve_personas` |
| 174-233 | `# --- Review mode` header, `PLAN_LABEL`, `pr_modes`, `enumerate_candidate_prs`, `render_prompt` |
| 123-172 | `# --- PR selection` header, `pr_truthy`, `parse_pr_ids`, `resolve_pr_selection` |

**Do not delete**, and check each by name after the deletions:

- `write_mcp_config` (408-443) — the earlier draft of this table had a range that swallowed it. It generates `$HOME/mcp.json` and `test-providers.sh` asserts the result.
- 445-482: the `WORK_DIR`/`WORK_REPO` resolution and the `REVIEW_INTERVAL_SECONDS` / `LIMIT_BACKOFF_SECONDS` / `MAX_PASSES_PER_SESSION` defaults and validators.
- `strip_surrounding_quotes`, `check_url`, `write_litellm_config`, `start_litellm`, everything in the provider `case`, the MCP block at 1077-1089, and the clone prep at 1090-1125.

`PERSONA_DIR`'s default assignment goes with the persona registry; Python reads the env var and defaults it.

Verify with:

```bash
for f in write_mcp_config write_litellm_config start_litellm strip_surrounding_quotes check_url; do
  printf '%s: %s\n' "$f" "$(grep -c "^$f()" entrypoint.sh)"
done
grep -c 'WORK_REPO=' entrypoint.sh
```

Expected: each function count is 1, and `WORK_REPO=` still appears.

- [ ] **Step 2: Add the handoff and the exec**

Replace the deleted loop at the end of `entrypoint.sh` with:

```bash
# --- Hand off to the supervisor --------------------------------------------
# Everything above produced an environment or a filesystem state, which is what
# shell is for. Everything below the exec is a stateful supervisor with
# structured per-task results, which is what Python is for. Env is the ONLY
# thing that crosses this line: no JSON handoff file, no serialized arrays.
#
# exec, not a child process: the supervisor becomes PID 1 so `docker stop`
# signals it directly, and any LiteLLM/shim children started above are inherited
# rather than orphaned. The supervisor reaps them (see check_litellm) because
# PID 1 gets no automatic reaping.
export WORK_REPO
export REVIEW_MODEL
export MCP_CONFIG_FILE
export LITELLM_PID="${LITELLM_PID:-}"
export SHIM_PID="${SHIM_PID:-}"
# Exported even though Python defaults them identically, so the value this
# script validated and logged is the value the supervisor uses. Two defaults in
# two languages is how the two drift.
export REVIEW_INTERVAL_SECONDS
export LIMIT_BACKOFF_SECONDS
export MAX_PASSES_PER_SESSION

log "Reviewer ready. repo=$GITHUB_REPOSITORY provider=$PROVIDER_LABEL model=$REVIEW_MODEL interval=${REVIEW_INTERVAL_SECONDS}s"
exec python3 /opt/claudebox/reviewer/review_loop.py
```

Move that `log "Reviewer ready..."` line here from its current position at `entrypoint.sh:1124` so it stays the last thing the shell says.

- [ ] **Step 3: Copy the package in the Dockerfile**

Find the line that copies `personas/` into the image and add beside it:

```dockerfile
COPY reviewer/ /opt/claudebox/reviewer/
```

- [ ] **Step 4: Syntax-check and confirm the handoff exists**

Run:

```bash
bash -n entrypoint.sh
grep -c 'exec python3 /opt/claudebox/reviewer/review_loop.py' entrypoint.sh
grep -c 'while true' entrypoint.sh
grep -c 'format_stream' entrypoint.sh
python3 -m py_compile reviewer/*.py
```

Expected: `bash -n` silent; the exec line appears exactly once; `while true` and `format_stream` appear **zero** times; `py_compile` silent.

- [ ] **Step 5: Commit**

```bash
git add entrypoint.sh Dockerfile
git commit -m "refactor: exec the Python supervisor from entrypoint.sh"
```

---

### Task 9: Retarget the bash acceptance suites

**Files:**
- Modify: `test-providers.sh` (the `python3` stub, and `MAX_CYCLES` in the baseline)
- Modify: `test-personas.sh` (the `sleep` stub, `MAX_CYCLES` per case)

**Interfaces:**
- Consumes: the exec'd loop from Task 8
- Produces: both suites green against the Python supervisor

- [ ] **Step 1: Fix the `python3` stub in `test-providers.sh`**

`test-providers.sh:89` currently replaces `python3` wholesale, which now swallows the review loop and hangs the suite forever. Replace it with a stub that only intercepts the shim:

```bash
# python3 is stubbed ONLY for the workersai shim, whose argv this suite asserts.
# The review loop is also python3 now, so a blanket stub would swallow it and
# hang forever waiting on a `tail -f` that never ends. Dispatch on the script.
cat >"$BIN/python3" <<'STUB'
#!/bin/sh
case "$1" in
  *workersai-shim.py)
    printf '%s upstream=%s port=%s' "$*" "$SHIM_UPSTREAM_URL" "$SHIM_PORT" >"$HOME/shim-argv"
    exec tail -f /dev/null ;;
  *)
    exec "$REAL_PYTHON3" "$@" ;;
esac
STUB
```

Then, in the `env -i` invocation at `test-providers.sh:155`, add `REAL_PYTHON3="$(command -v python3)"` so the passthrough arm can find the real interpreter under `env -i`.

- [ ] **Step 2: Add `MAX_CYCLES` to both suites' baselines**

In `test-providers.sh:155-160`, add `MAX_CYCLES=1` to the baseline env.

In `test-personas.sh:155-162`, add `MAX_CYCLES=2` to the baseline env, and delete the `sleep` stub at `test-personas.sh:97-104` along with the comment block explaining the succeeds-once-then-fails trick. Replace the stub with a plain no-op so the interval costs nothing:

```bash
printf '#!/bin/sh\nexit 0\n' >"$BIN/sleep"
```

Also set `REVIEW_INTERVAL_SECONDS=1` (already present) so the real `time.sleep` between the two cycles is one second rather than 300.

Update the header comment in `test-personas.sh:1-12` to say the suite runs two cycles because `MAX_CYCLES=2`, not because of a stubbed `sleep`.

- [ ] **Step 3: Run both suites**

Run:

```bash
./test-providers.sh
./test-personas.sh
./test-python.sh
```

Expected: all three green. If a `test-providers.sh` case now reports zero invocations, the `python3` dispatch is wrong; check that `REAL_PYTHON3` reached the entrypoint's environment.

- [ ] **Step 4: Confirm the resumed-persona property still holds**

This is the assertion the whole persona design rests on, so verify it explicitly rather than trusting a green summary.

Run: `./test-personas.sh resume`
Expected: PASS. The case asserts that invocation 2 (`--resume`) still carries `--append-system-prompt`, because the flag does not survive `--resume`.

- [ ] **Step 5: Commit**

```bash
git add test-providers.sh test-personas.sh
git commit -m "test: retarget the acceptance suites at the Python supervisor"
```

---

### Task 10: Documentation

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `CLAUDE.md`
- Modify: `HISTORY.md`

**Interfaces:**
- Consumes: everything above
- Produces: docs describing the two-language split and `MAX_CYCLES`

- [ ] **Step 1: Update `CLAUDE.md`**

Rewrite the "Two pieces working together" section so `entrypoint.sh` is described as startup-plus-exec and `reviewer/` as the supervisor, listing the six modules and what each owns. Update "There is no application code or build system" in the "What this is" section: there is now application code, in `reviewer/`.

Update the Commands section: add `./test-python.sh` beside the other three suites, and note that `python3 -m py_compile reviewer/*.py` is the Python equivalent of `bash -n`.

In "Gotchas when editing", replace the note about `test-providers.sh`'s stubbed `sleep` ending the loop with a note that both suites now stop via `MAX_CYCLES`, and add: the `python3` stub in `test-providers.sh` dispatches on the script path, because a blanket stub swallows the supervisor.

Add a gotcha for the `re.MULTILINE` hazard in `USAGE_LIMIT_RE`.

- [ ] **Step 2: Update `.env.example`**

Add, unquoted per the existing convention:

```
# How many review cycles to run before exiting. Unset or 0 = run forever, which
# is what an unattended container wants. Set it to 1 for a one-shot run.
# MAX_CYCLES=
```

- [ ] **Step 3: Update `README.md`**

In the architecture section, describe the shell/Python split in a paragraph. Add `MAX_CYCLES` to the optional-configuration table.

- [ ] **Step 4: Add a `HISTORY.md` entry**

Record the port: what moved, why (the defect classes named in the spec's Problem section), and that behavior is unchanged apart from the three deliberate departures.

- [ ] **Step 5: Verify and commit**

Run:

```bash
grep -c 'MAX_CYCLES' README.md .env.example CLAUDE.md
grep -c 'reviewer/' CLAUDE.md
./test-python.sh && ./test-providers.sh && ./test-personas.sh
```

Expected: `MAX_CYCLES` present in all three docs; `reviewer/` present in `CLAUDE.md`; all three suites green.

```bash
git add README.md .env.example CLAUDE.md HISTORY.md
git commit -m "docs: describe the shell/Python split and MAX_CYCLES"
```

---

## Phase A exit gate

Before starting Phase B, all of the following must hold:

- [ ] `./test-python.sh` green
- [ ] `./test-providers.sh` green, full matrix, no filter
- [ ] `./test-personas.sh` green, including the resumed-persona case
- [ ] `bash -n entrypoint.sh && bash -n claudebox.sh` silent
- [ ] `python3 -m py_compile reviewer/*.py` silent
- [ ] `grep -c 'MAX_CONCURRENT_PASSES' reviewer/ entrypoint.sh` returns zero matches
- [ ] A live run against a real repo posts at least one comment, verified by hand with `./claudebox.sh test --repo <path>`

Phase B is planned in `docs/superpowers/plans/2026-08-30-claudebox-python-loop-phase-b.md`.
