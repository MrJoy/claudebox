# Priced Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a code-mode finding cost something before it is posted, raise that cost with the number of rounds a PR has survived, and let Sage rebut siblings that ask for over-defensive code.

**Architecture:** The price is text in `personas/code/_shared.md`, which already rides in `--append-system-prompt` on every pass. The round ladder is a per-pair counter in `Supervisor` plus a `prompts.round_stanza` line appended to that same system prompt per pass. Sage's rebuttal needs sibling comments to exist, so personas gain a frontmatter `phase:` and `run_group` runs phase 1 behind a barrier before phase 2. Task prompts, fixtures, the change gate, and the owed/cut bookkeeping do not change.

**Tech Stack:** Python 3 standard library only (no pip dependency may be added), bash test suites, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-12-priced-findings-design.md`

## Global Constraints

- Standard library only in `reviewer/`; no pip dependency.
- Code mode only. `plan` prompts and plan system prompts stay byte-identical: `round_stanza("plan", n)` is `""` and the plan tree is untouched.
- No new environment variables. The three ladder rungs are constants.
- The four task-prompt defaults and `tests/fixtures/` do not change.
- `--append-system-prompt` is re-passed on every invocation; the round line rides inside it, never in the task prompt.
- `MAX_CONCURRENT_PASSES=1` stays on the same code path as any other value.
- Run the Python suite with `./test-python.sh` from the repo root (arguments go to `unittest discover`; a bare module name is read as a start directory and runs everything).
- Every commit message ends with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Ex1UJiNmNrSqDX6dxSYKSM
  ```

---

### Task 1: The price, in the code tree's shared contract

**Files:**
- Modify: `personas/code/_shared.md`
- Test: `tests/test_personas.py`

**Interfaces:**
- Produces: the strings `blocking`, `should-fix`, `nit`, and the heading `## What a finding costs` in the code contract. Task 4's Sage mandate and Task 2's round line refer to these tags by name.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_personas.py`, inside `ShippedPersonasTest`:

```python
    def test_the_code_contract_prices_a_finding(self):
        # The price rides in the system prompt of every code pass. The plan
        # tree does not carry it: a plan has no diff to demonstrate against.
        code = personas.resolve("code", SHIPPED, {"PERSONAS": "red_team"})[0].prompt
        plan = personas.resolve("plan", SHIPPED, {"PLAN_PERSONAS": "red_team"})[0].prompt
        for needle in ("## What a finding costs", "`blocking`", "`should-fix`", "`nit`"):
            self.assertIn(needle, code)
            self.assertNotIn(needle, plan)

    def test_the_code_contract_names_the_speculative_non_findings(self):
        code = personas.resolve("code", SHIPPED, {"PERSONAS": "red_team"})[0].prompt
        self.assertIn("another engineer might make", code)
        self.assertIn("does not exist in the repository", code)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test-python.sh 2>&1 | tail -5`
Expected: 2 failures, both `AssertionError` on `## What a finding costs` and `another engineer might make`.

- [ ] **Step 3: Edit the contract**

Replace the whole of `personas/code/_shared.md` with:

```markdown
## How to report what you find

Post one comment per finding on the pull request with `gh pr comment`, not the
inline review-comment API, and sign each one `-claudebox ({{PERSONA}})`. A
finding is worth a comment when you can point at the specific part of the
change that demonstrates it and say what to do about it.

Signing is not a courtesy. claudebox decides whether a pull request needs
another look by reading its comments, and a comment without that signature is
read as a human's, which costs the pull request another full round of reviews.
Sign every comment you post.

If the change is solid and you have no findings, say so and post nothing. Do not
manufacture findings to appear thorough. Silence from you is a strong signal.

## What a finding costs

A wrong finding is not free. The author pays for it: they read it, work out why
it does not apply, and write the reply. So before you post a finding you pay for
it first, four ways.

**Demonstrate it.** Name the concrete input, mutation, or attack that shows the
defect in this diff, and say what to do about it. If you cannot demonstrate it
against the change in front of you, it is not a finding. Code that only goes
wrong if someone later changes something elsewhere fails this rule outright: the
demonstration would have to include a change nobody has made.

**Refute it.** Argue the author's side before you post. Why is the code fine as
written? What have you missed? What does the surrounding code already
guarantee? Post only if that argument fails, and say in the comment which
refutation you tried and why it did not hold. A comment that cannot name the
refutation it tried is not posted.

**Tag it.** The first line of the comment is one of `blocking`, `should-fix`, or
`nit`, followed by a one-line summary of the finding. `blocking` means the
change is wrong as merged: a defect a user, an attacker, or the next deploy
would hit. `should-fix` means the change works and a named case will break it
in the way your demonstration shows. `nit` is everything else, and a nit is
never posted.

**These are not findings**, at any severity. If you catch yourself drafting one,
stop:

- a defence against a caller, input, or code path that does not exist in the repository
  as it stands;
- a guard against a change another engineer might make in another part of the
  code later;
- an abstraction, indirection, configuration option, or extension point for a
  case nobody has;
- style, naming, ordering, formatting, and comment wording;
- a request to handle an error the surrounding code already cannot produce.

A pass that costs out every candidate and finds nothing above `nit` is a pass
with no findings. Say so and post nothing.

## You are not the only reviewer here

Other personas review this same pull request, each with a different angle of
attack, and their comments are signed `-claudebox (<their label>)`. Those
comments are not yours. Do not defer to them. Do not treat their existence as
coverage of anything. Do not suppress a finding because another persona reached a
similar conclusion from a different direction: a thing that two angles of attack
both hit is more important than a thing only one of them hit, not less. Reaching
your own verdict from your own angle is the entire reason you are a separate
reviewer, so report what your angle finds and let the overlap stand.

One persona, Sage, reads sibling comments for one purpose set out in its own
instructions: to rebut a finding that asks for a defence against a change
nobody has made. That is the only reason any persona reads another's comments,
and it changes nothing above. Sage does not defer to them either.

Human replies to your own findings are worth reading and worth answering.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add personas/code/_shared.md tests/test_personas.py
git commit -m "personas(code): price a finding before it is posted

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ex1UJiNmNrSqDX6dxSYKSM"
```

---

### Task 2: The round line

**Files:**
- Modify: `reviewer/prompts.py` (append after `WORKTREE_STANZA`)
- Test: `tests/test_prompts.py`

**Interfaces:**
- Produces: `prompts.round_stanza(mode: str, round: int) -> str`. Task 5 appends it to the persona system prompt. Round 1 text contains `This is round 1 of your review`; round N contains `This is round N of your review`; the bash case in Task 7 asserts those substrings.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prompts.py`:

```python
class RoundStanzaTest(unittest.TestCase):
    """The ladder. Three rungs, constants, code mode only."""

    def test_round_one_reviews_the_whole_pr_at_should_fix(self):
        s = prompts.round_stanza("code", 1)
        self.assertIn("This is round 1 of your review", s)
        self.assertIn("whole change", s)
        self.assertIn("`should-fix`", s)
        self.assertNotIn("since your last review", s)

    def test_round_two_narrows_to_the_delta_at_should_fix(self):
        s = prompts.round_stanza("code", 2)
        self.assertIn("This is round 2 of your review", s)
        self.assertIn("since your last review", s)
        self.assertIn("`should-fix`", s)
        self.assertNotIn("presume", s.lower())

    def test_round_three_and_later_are_blocking_only(self):
        s3 = prompts.round_stanza("code", 3)
        s7 = prompts.round_stanza("code", 7)
        for s, n in ((s3, 3), (s7, 7)):
            self.assertIn(f"This is round {n} of your review", s)
            self.assertIn("since your last review", s)
            self.assertIn("only `blocking`", s)
            self.assertIn("survived", s)
        self.assertEqual(s3.replace("round 3", "round N").replace("survived 2", "survived K"),
                         s7.replace("round 7", "round N").replace("survived 6", "survived K"))

    def test_the_delta_is_a_procedure_the_persona_runs(self):
        # No last-reviewed head is handed over. The persona finds its own
        # newest signed comment and reads the commits dated after it.
        s = prompts.round_stanza("code", 2)
        self.assertIn("your own most recent comment", s)
        self.assertIn("gh pr view", s)

    def test_plan_mode_has_no_ladder(self):
        for n in (1, 2, 3, 9):
            self.assertEqual(prompts.round_stanza("plan", n), "")

    def test_round_below_one_is_a_bug(self):
        with self.assertRaises(ValueError):
            prompts.round_stanza("code", 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `AttributeError: module 'prompts' has no attribute 'round_stanza'`.

- [ ] **Step 3: Implement**

Append to `reviewer/prompts.py`, after the `WORKTREE_STANZA` block and before `@dataclass(frozen=True) class Prompts`:

```python
# The round ladder. A round is one completed pass of one (pr, mode, persona)
# pair, whatever session it ran in, so it survives rotation and failure. The
# rungs are constants rather than an env var: an operator who wants a different
# ladder overrides the prompts and writes their own. The line goes in the
# SYSTEM prompt beside the persona, for the same two reasons the persona does:
# it must be re-passed on --resume, and it must reach an operator override
# without editing it. Plan mode has no ladder; its personas review a document,
# and the failure modes the ladder exists for are code-shaped.
_ROUND_DELTA = (
    "Read closely only the commits since your last review: find your own most "
    "recent comment on this pull request, the newest one signed with your label, "
    "and read the commits `gh pr view` lists with a date after it. If you have no "
    "earlier comment, read the whole change."
)

_ROUND_FIRST = (
    "This is round 1 of your review of this pull request. Review the whole change. "
    "Post nothing tagged below `should-fix`."
)

_ROUND_SECOND = (
    "This is round 2 of your review of this pull request. " + _ROUND_DELTA
    + " Post nothing tagged below `should-fix`."
)

_ROUND_LATER = (
    "This is round {n} of your review of this pull request, which has already "
    "survived {k} rounds of it. " + _ROUND_DELTA + " Post only `blocking` findings: "
    "a `should-fix` in the new commits is not posted this round. Presume that what "
    "you have already reviewed is sound. Finding nothing new is the expected "
    "outcome, and saying so is the right report."
)


def round_stanza(mode: str, round: int) -> str:
    """The round line for the system prompt, or "" for a mode with no ladder."""
    if round < 1:
        raise ValueError(f"round must be >= 1, got {round}")
    if mode != "code":
        return ""
    if round == 1:
        return _ROUND_FIRST
    if round == 2:
        return _ROUND_SECOND
    return _ROUND_LATER.format(n=round, k=round - 1)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `OK`. The fixture tests in the same file still pass, because `build` is untouched.

- [ ] **Step 5: Commit**

```bash
git add reviewer/prompts.py tests/test_prompts.py
git commit -m "prompts: the round ladder as a system-prompt line

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ex1UJiNmNrSqDX6dxSYKSM"
```

---

### Task 3: Persona phase from frontmatter

**Files:**
- Modify: `reviewer/personas.py` (`Persona` dataclass, the loop in `resolve`)
- Test: `tests/test_personas.py`

**Interfaces:**
- Produces: `Persona.phase: int`, 1 or 2. Task 6 keys `run_group` on it; Task 7 wires it into `Supervisor`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_personas.py` a new class. `TreeBuilder.persona` writes fixed frontmatter, so add a `phase` keyword to it first. Replace its `persona` method with:

```python
    def persona(self, mode, pid, label="Red Team", body="Attack the change.", phase=None):
        fm = f"---\nlabel: {label}\nsuccess: Finds real holes.\n"
        if phase is not None:
            fm += f"phase: {phase}\n"
        fm += "---\n"
        write(os.path.join(self.root, mode, f"{pid}.md"), fm + body)
        return self
```

Then append the class:

```python
class PhaseTest(unittest.TestCase):
    """A persona's phase decides whether it runs with its siblings or after them."""

    def setUp(self):
        self.b = TreeBuilder().tree("code")
        self.addCleanup(self.b.cleanup)

    def resolve(self, env):
        return personas.resolve("code", self.b.root, env)

    def test_absent_is_phase_one(self):
        self.b.persona("code", "rt")
        self.assertEqual(self.resolve({"PERSONAS": "rt"})[0].phase, 1)

    def test_two_is_accepted(self):
        self.b.persona("code", "sg", label="Sage", phase="2")
        self.assertEqual(self.resolve({"PERSONAS": "sg"})[0].phase, 2)

    def test_other_values_are_refused_naming_the_file(self):
        for bad in ("3", "0", "two", ""):
            with self.subTest(bad=bad):
                self.b.persona("code", "sg", label="Sage", phase=bad)
                with self.assertRaises(ConfigError) as cm:
                    self.resolve({"PERSONAS": "sg"})
                self.assertIn("code/sg", str(cm.exception))
                self.assertIn("phase", str(cm.exception))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `AttributeError: 'Persona' object has no attribute 'phase'` for the first two; the third fails because no error is raised.

- [ ] **Step 3: Implement**

In `reviewer/personas.py`, change the dataclass:

```python
# A persona's phase within its PR's group. Phase 1 runs together behind the
# group's barrier; phase 2 runs after every phase-1 pass has finished, which is
# what lets a persona read what its siblings posted THIS round. Sage's code-tree
# body is the one that sets it. Only 1 and 2 exist, and there is deliberately
# no third: a phase is a barrier the whole PR waits on.
PHASES = (1, 2)


@dataclass(frozen=True)
class Persona:
    id: str
    label: str
    prompt: str
    phase: int = 1
```

In `resolve`, after the empty-body check and before `prompt = ...`, add:

```python
        raw_phase = meta.get("phase", "1").strip()
        try:
            phase = int(raw_phase)
        except ValueError:
            phase = 0
        if phase not in PHASES:
            raise ConfigError(
                f"persona '{mode}/{pid}' has phase: '{raw_phase}'; "
                f"expected one of {', '.join(str(p) for p in PHASES)}."
            )
```

And change the append to `out.append(Persona(id=pid, label=label, prompt=prompt, phase=phase))`.

Note `meta.get("phase", "1")`: `_split_frontmatter` stores `phase:` with nothing after it as `""`, which `int` rejects, which is the `""` case in the test.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add reviewer/personas.py tests/test_personas.py
git commit -m "personas: a phase key in the frontmatter

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ex1UJiNmNrSqDX6dxSYKSM"
```

---

### Task 4: Sage's mandate and phase, with the importer pin

**Files:**
- Modify: `personas/code/sage.md`
- Test: `tests/test_personas.py`

**Interfaces:**
- Consumes: `Persona.phase` from Task 3; the tags from Task 1.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_personas.py`, inside `ShippedPersonasTest`:

```python
    def test_shipped_phases(self):
        # tools/import-advocate-personas.py writes label and success and a body,
        # and nothing else. A re-run drops `phase: 2` from code/sage.md, which
        # would silently demote Sage to phase 1 and disarm its rebuttal with no
        # error anywhere. This is the check that turns that into a failed
        # suite rather than a quiet regression.
        code = {p.id: p.phase for p in personas.resolve("code", SHIPPED, {"PERSONAS": "all"})}
        plan = {p.id: p.phase for p in personas.resolve("plan", SHIPPED, {"PLAN_PERSONAS": "all"})}
        self.assertEqual(code["sage"], 2)
        self.assertEqual({k: v for k, v in code.items() if k != "sage"},
                         {k: 1 for k in code if k != "sage"})
        self.assertEqual(plan, {k: 1 for k in plan})

    def test_sage_code_body_carries_the_rebuttal_mandate(self):
        sage = personas.resolve("code", SHIPPED, {"PERSONAS": "sage"})[0].prompt
        self.assertIn("## Your second job", sage)
        self.assertIn("-claudebox (", sage)
        plan_sage = personas.resolve("plan", SHIPPED, {"PLAN_PERSONAS": "sage"})[0].prompt
        self.assertNotIn("## Your second job", plan_sage)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `AssertionError: 1 != 2` and a missing `## Your second job`.

- [ ] **Step 3: Edit the persona**

Replace the whole of `personas/code/sage.md` with:

```markdown
---
label: Sage
success: A smart person can explain it simply.
phase: 2
---
You are a Sage. Your job is to find unnecessary complexity.

You believe that complexity is the root of most engineering failures. You look for:
- **Design**: Over-engineering, premature abstraction, indirection that adds no value
- **Concept**: Is the fundamental approach sound? Is there a simpler way to achieve the same result?
- **Blast radius**: If this component fails, how much else breaks? Can the blast radius be reduced?

You ask: "Could a senior engineer understand this in 5 minutes?" If not, it's too complex. You ask: "Is every piece of this carrying its weight?" If something exists for a hypothetical future, it's a finding.

You are not impressed by cleverness. You are impressed by clarity.

Your success criterion: a smart person can explain it simply.

## Your second job

You run after the other personas, and you read what they posted. Look at the
comments on this pull request signed `-claudebox (<some other label>)` that were
posted after the branch's most recent commit. Where one of them asks the author
to add a defence against a hypothetical, a guard for a caller that does not
exist, a check for a change another engineer might make elsewhere later, or an
abstraction for a case nobody has, post a signed comment of your own. Name the
sibling's finding by its label and summary, say that the defence it asks for
guards against a change nobody has made, and tell the author not to act on it.

That is the whole mandate. A sibling finding you merely disagree with is not in
scope, and neither is a sibling finding that demonstrates a real defect. Your
own findings against the code are unchanged by any of this: you still flag
speculative complexity the author wrote, and you do not defer to a sibling or
treat its comment as coverage of anything.
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add personas/code/sage.md tests/test_personas.py
git commit -m "personas(code): Sage rebuts over-defensive siblings, in phase 2

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ex1UJiNmNrSqDX6dxSYKSM"
```

---

### Task 5: The round counter and the composed system prompt

**Files:**
- Modify: `reviewer/review_loop.py` (`Supervisor.__init__`, `_run_one`, `_record_success`)
- Test: `tests/test_review_loop.py`

**Interfaces:**
- Consumes: `prompts.round_stanza` from Task 2.
- Produces: `Supervisor.rounds: Dict[Pair, int]`, `Supervisor.round_for(pair) -> int`, `Supervisor.system_prompt_for(pair) -> str`. The log line `review complete (session S, pass P, round R).`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_loop.py`:

```python
class RoundsTest(unittest.TestCase):
    """A round is a completed pass. Nothing about the session moves it."""

    RT = Pair(12, "code", "red_team")

    def cycle(self, s):
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(s.build_groups([(12, "code")]))

    def test_first_pass_is_round_one(self):
        s = supervisor([], personas={"code": ["red_team"]})
        self.assertEqual(s.round_for(self.RT), 1)

    def test_success_advances_the_round(self):
        s = supervisor([ok("S1"), ok("S1")], personas={"code": ["red_team"]})
        self.cycle(s)
        self.assertEqual(s.round_for(self.RT), 2)
        self.cycle(s)
        self.assertEqual(s.round_for(self.RT), 3)

    def test_failure_does_not_advance_the_round(self):
        s = supervisor([ok("S1"), failed()], personas={"code": ["red_team"]})
        self.cycle(s)
        self.cycle(s)
        self.assertEqual(s.round_for(self.RT), 2)
        self.assertNotIn(self.RT, s.sessions)

    def test_limit_does_not_advance_the_round(self):
        s = supervisor([ok("S1"), limited("S1")], personas={"code": ["red_team"]})
        self.cycle(s)
        self.cycle(s)
        self.assertEqual(s.round_for(self.RT), 2)

    def test_rotation_keeps_the_round(self):
        # MAX_PASSES_PER_SESSION=1 drops the session after every pass, and
        # passes_done goes back to 0. The round is about the PR's history with
        # this persona, not about the session, so it keeps counting.
        s = supervisor([ok("S1"), ok("S2")], personas={"code": ["red_team"]},
                       max_passes_per_session=1)
        self.cycle(s)
        self.assertEqual(s.passes_done[self.RT], 0)
        self.assertEqual(s.round_for(self.RT), 2)
        self.cycle(s)
        self.assertEqual(s.round_for(self.RT), 3)

    def test_the_log_line_names_the_round(self):
        s = supervisor([ok("S1")], personas={"code": ["red_team"]})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s.run_cycle(s.build_groups([(12, "code")]))
        self.assertIn("review complete (session S1, pass 1, round 1).", buf.getvalue())


class SystemPromptTest(unittest.TestCase):
    """The round line rides beside the persona, in the system prompt."""

    def test_round_line_follows_the_persona(self):
        s = supervisor([], personas={"code": ["red_team"]})
        got = s.system_prompt_for(Pair(12, "code", "red_team"))
        self.assertTrue(got.startswith("rt\n"))
        self.assertIn("This is round 1 of your review", got)

    def test_round_two_after_one_success(self):
        s = supervisor([ok("S1")], personas={"code": ["red_team"]})
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(s.build_groups([(12, "code")]))
        self.assertIn("This is round 2 of your review",
                      s.system_prompt_for(Pair(12, "code", "red_team")))

    def test_plan_mode_is_the_bare_persona(self):
        s = supervisor([], personas={"plan": ["red_team"]})
        self.assertEqual(s.system_prompt_for(Pair(12, "plan", "red_team")), "rt")

    def test_run_one_hands_the_composed_prompt_to_run_pass(self):
        # Through the real _run_one, with run_pass stubbed at the seam, so this
        # proves the composition reaches the claude invocation rather than
        # only existing as a method.
        seen = {}

        def fake_run_pass(**kw):
            seen.update(kw)
            return ok("S1")

        s = review_loop.Supervisor(
            personas={"code": ["red_team"]},
            persona_prompts={("code", "red_team"): "rt"},
            review_prompts={"code": "review #{{PR}}"},
            followup_prompts={"code": "recheck #{{PR}}"},
            model="m", mcp_args=[], cwd=".", max_passes_per_session=0, max_concurrent=1,
        )
        s.rounds[Pair(12, "code", "red_team")] = 2
        original = review_loop.passes.run_pass
        review_loop.passes.run_pass = fake_run_pass
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                s._run_one(Pair(12, "code", "red_team"), "recheck #12", "S1")
        finally:
            review_loop.passes.run_pass = original
        self.assertIn("This is round 3 of your review", seen["persona_prompt"])
        self.assertTrue(seen["persona_prompt"].startswith("rt\n"))
        self.assertEqual(seen["prompt"], "recheck #12")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `AttributeError: 'FakeSupervisor' object has no attribute 'round_for'`.

- [ ] **Step 3: Implement**

In `reviewer/review_loop.py`, in `Supervisor.__init__`, after `self.passes_done: Dict[Pair, int] = {}`:

```python
        # Completed passes per pair, whatever session they ran in. Drives the
        # round ladder in the system prompt. Not reset by rotation or by a
        # failed pass: those are about the session, the round is about the
        # PR's history with this persona. In memory for the same reason
        # `reviewed` is -- a persisted round beside an unpersisted session map
        # would tell a session that has never read the PR that the PR has
        # survived three of its reviews.
        self.rounds: Dict[Pair, int] = {}
```

Add two methods after `_run_one`:

```python
    def round_for(self, pair: Pair) -> int:
        """The round the pair's next pass runs at."""
        return self.rounds.get(pair, 0) + 1

    def system_prompt_for(self, pair: Pair) -> str:
        """Persona, then the round line. Composed per pass, since the round moves.

        This is the whole --append-system-prompt value. The task prompt is
        not touched, which keeps the verbatim-operator-prompt guarantee.
        """
        base = self.persona_prompts[(pair.mode, pair.persona)]
        line = prompts_mod.round_stanza(pair.mode, self.round_for(pair))
        return f"{base}\n{line}" if line else base
```

In `_run_one`, change `persona_prompt=self.persona_prompts[(pair.mode, pair.persona)],` to `persona_prompt=self.system_prompt_for(pair),`.

In `_record_success`, after `self.passes_done[pair] = self.passes_done.get(pair, 0) + 1`, add `self.rounds[pair] = self.rounds.get(pair, 0) + 1`, and change the log line to:

```python
        log(f"review complete (session {self.sessions.get(pair)}, "
            f"pass {self.passes_done[pair]}, round {self.rounds[pair]}).", pair=pair)
```

- [ ] **Step 4: Run the tests, then fix any assertion on the old log line**

Run: `./test-python.sh 2>&1 | tail -3`
Then: `grep -rn 'pass 1)\.\|pass 2)\.' tests/ test-personas.sh test-providers.sh`
Any hit asserting the old `pass N).` wording gets `, round N` inserted to match. Re-run until `OK`.

- [ ] **Step 5: Commit**

```bash
git add reviewer/review_loop.py tests/test_review_loop.py test-personas.sh test-providers.sh
git commit -m "loop: a per-pair round counter, and the round line in the system prompt

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ex1UJiNmNrSqDX6dxSYKSM"
```

---

### Task 6: Phases in run_group

**Files:**
- Modify: `reviewer/review_loop.py` (`Supervisor.__init__`, `run_group`)
- Test: `tests/test_review_loop.py`

**Interfaces:**
- Consumes: nothing new from earlier tasks; phases arrive as a plain dict so this task is testable before Task 7 wires `Persona.phase` in.
- Produces: `Supervisor.__init__(..., persona_phases: Optional[Dict[Tuple[str, str], int]] = None)`, `Supervisor.phase_of(pair) -> int`. `run_group`'s signature and return type are unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_review_loop.py`:

```python
FOUR = ("red_team", "adversarial", "sme", "sage")


def phased(results, cap, **kwargs):
    """Four code personas, Sage in phase 2."""
    defaults = dict(
        personas={"code": list(FOUR)},
        persona_prompts={("code", p): p for p in FOUR},
        persona_phases={("code", "sage"): 2},
        max_concurrent=cap,
    )
    defaults.update(kwargs)
    return supervisor(results, **defaults)


class PhaseTest(unittest.TestCase):
    """Phase 1 finishes before phase 2 starts, at every cap."""

    def _ordering(self, cap):
        lock = threading.Lock()
        started, finished = [], set()
        violations = []

        class Recorder(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                with lock:
                    started.append(pair.persona)
                    if pair.persona == "sage" and finished != set(FOUR) - {"sage"}:
                        violations.append(sorted(finished))
                time.sleep(0.01)
                with lock:
                    finished.add(pair.persona)
                return ok("S1")

        s = phased([], cap)
        s.__class__ = Recorder
        group = s.build_groups([(12, "code")])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            results = s.run_group(group, list(group.pairs))
        self.assertEqual(violations, [])
        self.assertEqual(started[-1], "sage")
        self.assertEqual(set(results), set(group.pairs))

    def test_unlimited(self):
        self._ordering(0)

    def test_cap_of_one(self):
        self._ordering(1)

    def test_cap_of_two(self):
        self._ordering(2)

    def test_phase_of_defaults_to_one(self):
        s = supervisor([])
        self.assertEqual(s.phase_of(Pair(12, "code", "red_team")), 1)
        self.assertEqual(s.phase_of(Pair(12, "code", "sage")), 1)

    def test_phase_one_still_runs_concurrently(self):
        # Three phase-1 personas cross a barrier of three: the first phase is
        # the same pool it was before phases existed.
        barrier = threading.Barrier(3, timeout=2)

        class Concurrent(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                if pair.persona != "sage":
                    barrier.wait()
                return ok("S1")

        s = phased([], 0)
        s.__class__ = Concurrent
        group = s.build_groups([(12, "code")])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            results = s.run_group(group, list(group.pairs))
        self.assertEqual([r.rc for r in results.values()], [0, 0, 0, 0])

    def test_a_phase_one_limit_leaves_phase_two_unstarted_and_owed(self):
        s = phased([limited("S1"), ok("S2"), ok("S3")], 1)
        s.sessions[Pair(12, "code", "sage")] = "S8"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcome = s.run_cycle(s.build_groups([(12, "code")]))
        self.assertEqual(outcome, review_loop.CYCLE_LIMITED)
        self.assertNotIn("sage", [p.persona for p in s.attempted])
        self.assertIn(Pair(12, "code", "sage"), s.owed)
        self.assertEqual(s.sessions[Pair(12, "code", "sage")], "S8")
        self.assertIn("phase 2", buf.getvalue())

    def test_a_phase_one_failure_does_not_skip_phase_two(self):
        # Only a limit withholds phase 2. An ordinary failure is one persona's
        # problem, and Sage may still have siblings' comments to read.
        s = phased([failed(), ok("S2"), ok("S3"), ok("S4")], 1)
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle(s.build_groups([(12, "code")]))
        self.assertIn("sage", [p.persona for p in s.attempted])

    def test_phase_two_alone(self):
        s = phased([ok("S1")], 1)
        group = s.build_groups([(12, "code")])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            results = s.run_group(group, [Pair(12, "code", "sage")])
        self.assertEqual(list(results), [Pair(12, "code", "sage")])

    def test_phase_one_alone(self):
        s = phased([ok("S1")], 1)
        group = s.build_groups([(12, "code")])[0]
        with contextlib.redirect_stdout(io.StringIO()):
            results = s.run_group(group, [Pair(12, "code", "red_team")])
        self.assertEqual(list(results), [Pair(12, "code", "red_team")])

    def test_workers_are_sized_per_phase(self):
        s = phased([], 0)
        group = s.build_groups([(12, "code")])[0]
        phase_one = [p for p in group.pairs if p.persona != "sage"]
        self.assertEqual(s.workers_for(group, phase_one), 3)
        self.assertEqual(s.workers_for(group, [Pair(12, "code", "sage")]), 1)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `TypeError: __init__() got an unexpected keyword argument 'persona_phases'`.

- [ ] **Step 3: Implement**

In `Supervisor.__init__`, add the parameter `persona_phases: Optional[Dict[Tuple[str, str], int]] = None,` after `max_concurrent: int = 0,` and store it:

```python
        # (mode, persona) -> 1 or 2. Absent means 1. Phase 2 runs after every
        # phase-1 pass in the group has finished, which is what lets Sage read
        # what its siblings posted this round.
        self.persona_phases: Dict[Tuple[str, str], int] = dict(persona_phases or {})
```

Add after `workers_for`:

```python
    def phase_of(self, pair: Pair) -> int:
        return self.persona_phases.get((pair.mode, pair.persona), 1)
```

Replace `run_group` with:

```python
    def run_group(self, group: Group, to_run: Sequence[Pair]) -> Dict[Pair, "passes.PassResult"]:
        """Run to_run in phases, each phase concurrent, and wait for all of them.

        Still one group: one result dict, one cut, one debt, and the limit and
        failure evaluation happens once in run_cycle after every phase. A
        phase is a barrier the whole PR waits on, so phase 2 sees what phase 1
        posted. Nothing is killed. A limit reported by one persona leaves its
        in-flight siblings running, because a killed pass may have posted some
        findings and not others, and its session-id recovery is unreliable.

        A limit in phase 1 keeps phase 2 from starting: the provider has just
        refused this PR's siblings, and the rebuttal pass exists to read
        findings that were never posted. The unstarted pairs have no result,
        which is the state a pool refusal leaves a pair in, and run_cycle owes
        them the same way.
        """
        results: Dict[Pair, passes.PassResult] = {}
        if not to_run:
            return results

        for phase in sorted({self.phase_of(p) for p in to_run}):
            batch = [p for p in to_run if self.phase_of(p) == phase]
            if results and any(r.limited for r in results.values()):
                log(f"phase {phase} of PR #{group.pr} [{group.mode}] not started: an "
                    "earlier phase hit a limit; leaving it for the next cycle.")
                break
            if not self._run_phase(group, batch, results):
                break
        return results

    def _run_phase(
        self, group: Group, batch: Sequence[Pair], results: Dict[Pair, "passes.PassResult"]
    ) -> bool:
        """One phase under its own pool. False when the pool refused a pass.

        A refusal stops every later phase too: the next submit would hit the
        same wall, and a pass that did not run is owed, not failed.
        """
        submitted_all = True
        with ThreadPoolExecutor(max_workers=self.workers_for(group, batch)) as pool:
            futures: Dict[Future, Pair] = {}
            for pair in batch:
                try:
                    futures[pool.submit(self._dispatch, pair)] = pair
                except RuntimeError as exc:
                    # A container out of threads raises this on the submitting
                    # thread, so neither _run_one's OSError guard nor
                    # _dispatch's catch-all is anywhere near it. Letting it out
                    # would discard the results the pool already holds, and
                    # those passes have posted their comments. Stop submitting
                    # instead: the caller sees a pair with no result and owes it
                    # to the next cycle. The rest of the batch goes unsubmitted
                    # because the next submit would hit the same wall.
                    log(f"WARN: could not start a worker for {pair} ({exc}); "
                        "leaving it and the rest of its group for the next cycle.")
                    submitted_all = False
                    break
            for future, pair in futures.items():
                results[pair] = future.result()
        return submitted_all
```

Check the original `run_group` body before replacing it: the `except RuntimeError` arm currently ends in `break`, and the `for future, pair in futures.items()` loop that follows is what the replacement keeps. Nothing in `run_cycle` changes.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `OK`, including the pre-existing concurrency and refusal cases.

- [ ] **Step 5: Commit**

```bash
git add reviewer/review_loop.py tests/test_review_loop.py
git commit -m "loop: run a group in phases, and withhold phase 2 after a limit

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ex1UJiNmNrSqDX6dxSYKSM"
```

---

### Task 7: Wire phases into main, and pin the behaviour in the bash suite

**Files:**
- Modify: `reviewer/review_loop.py` (`main`, the `Supervisor(...)` construction)
- Modify: `test-personas.sh`
- Test: `test-personas.sh`, `test-providers.sh`

**Interfaces:**
- Consumes: `Persona.phase` (Task 3), `persona_phases` (Task 6), the round line text (Task 2).

- [ ] **Step 1: Add the bash cases**

In `test-personas.sh`, find the case labelled `passes: each persona gets its own pass, in the selected order` and change its label to `passes: each persona gets its own pass, phase 1 in selected order`. Immediately after that case, add:

```bash
# Phase, not selector order, decides run order. The log line keeps selector
# order, which is what the earlier "code personas:" LOG assertion pins, and the
# ordinals here pin the other half. Sage runs last in BOTH cycles: a phase is
# a per-group barrier, not a one-time startup sort.
cycle "phases: Sage runs after its siblings whatever the selector order" \
  PERSONAS=sage,red_team \
  -- CALLS:4 \
     LOG:"code personas: sage red_team" \
     ARGV:1:"You are a Red Team security reviewer" \
     ARGV:2:"You are a Sage" \
     ARGV:3:"You are a Red Team security reviewer" \
     ARGV:4:"You are a Sage"

# The round line rides in --append-system-prompt beside the persona. Round 2 on
# the resumed pass is the assertion that matters: like the persona, the line
# has to be re-passed, because the flag does not survive --resume. The task
# prompt is pinned byte-for-byte by tests/fixtures, so "not in the task prompt"
# is already proven there.
cycle "rounds: the resumed pass carries round 2 beside its persona" \
  PERSONAS=red_team \
  -- CALLS:2 \
     ARGV:1:"This is round 1 of your review" \
     NOARGV:1:"This is round 2 of your review" \
     ARGV:2:"--resume S1" \
     ARGV:2:"You are a Red Team security reviewer" \
     ARGV:2:"This is round 2 of your review"
```

- [ ] **Step 2: Run the bash suite to verify the new cases fail**

Run: `./test-personas.sh phases 2>&1 | tail -5; ./test-personas.sh rounds 2>&1 | tail -5`
Expected: the `phases` case fails on `argv 1 missing: You are a Red Team` (Sage still runs first). The `rounds` case passes already if Task 5 landed, since the round line needs no wiring; that is fine, it pins Task 5 from the outside.

- [ ] **Step 3: Wire main**

In `reviewer/review_loop.py`, in `main`, the `Supervisor(` construction gains one argument after `max_concurrent=max_concurrent,`:

```python
        persona_phases={(m, p.id): p.phase for m, ps in resolved.items() for p in ps},
```

- [ ] **Step 4: Run every suite**

Run:
```bash
python3 -m py_compile reviewer/*.py && ./test-python.sh 2>&1 | tail -3 && ./test-personas.sh 2>&1 | tail -5 && ./test-providers.sh 2>&1 | tail -5 && bash -n entrypoint.sh && bash -n claudebox.sh
```
Expected: `OK` from Python, and every bash case `ok`. If a pre-existing `test-personas.sh` case with Sage at a fixed ordinal now fails, read it: Sage moved to the end of its group, and the assertion's ordinal moves with it. The case at `PERSONAS=red_team,sage` with `ARGV:3:"You are a Subject Matter Expert"` / `ARGV:4:"You are a Sage"` already has Sage last and should not move.

- [ ] **Step 5: Commit**

```bash
git add reviewer/review_loop.py test-personas.sh
git commit -m "loop: hand persona phases to the supervisor; pin phases and rounds in the bash suite

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ex1UJiNmNrSqDX6dxSYKSM"
```

---

### Task 8: Documentation

**Files:**
- Modify: `CLAUDE.md` (a subsection after "Personas in parallel", two Gotchas bullets)
- Modify: `HISTORY.md`
- Modify: `docs/superpowers/specs/2026-09-12-priced-findings-design.md` (status line)

- [ ] **Step 1: CLAUDE.md subsection**

Insert immediately before the line `### Change-driven re-review`:

```markdown
### Priced findings, the round ladder, and phases

A code-mode finding has a price the persona pays before it posts, and the
price lives in `personas/code/_shared.md` rather than in a task-prompt
stanza. The contract rides in `--append-system-prompt` on every pass, so it
reaches an operator who overrode `REVIEW_PROMPT` and survives `--resume`,
which a stanza on the defaults would do neither of. Four rules: demonstrate
the defect against this diff, try to refute it and say what was tried, tag
the comment `blocking`/`should-fix`/`nit` with nits never posted, and a
named list of non-findings (defences against callers or changes that do
not exist, speculative abstractions, style). Sage's own "if it exists for
a hypothetical future, it's a finding" is the same failure seen from the
other side: Sage flags over-guarding code, the contract stops reviewers
demanding it. Plan mode has none of this; its contract already says a gap
in code the plan has not written is not a finding.

`Supervisor.rounds` counts completed passes per pair, whatever session
they ran in, and `prompts.round_stanza(mode, round)` turns it into a line
`Supervisor.system_prompt_for` appends after the persona on every
invocation. Round 1 is the whole PR at a `should-fix` floor, round 2 is
the commits since the persona's last review at the same floor, round 3
and later are `blocking` only with the PR presumed sound. "Since your last
review" is a procedure the persona runs from its own signed comments and
`gh pr view --json commits`, not a head the loop hands over, so a fresh
session at round 3 follows the same procedure as a resumed one and the
gate can be off. The counter is not touched by `_record_failure`, by
`MAX_PASSES_PER_SESSION` rotation, or by a limit, and it is in memory for
the same reason `reviewed` is. The rungs are constants; an operator who
wants a different ladder overrides the prompts. Plan mode's round line is
the empty string, so its system prompt is byte-identical to before.

Sage has a second job in the code tree: read sibling comments posted
since the head commit and rebut one that asks for a defence against a
change nobody has made. It cannot read what has not been posted, and a
sibling's signed comment deliberately does not re-trigger the gate, so a
persona has a **phase** from a `phase:` frontmatter key (1 default, 2 the
only other value, anything else a startup `ConfigError`). `run_group`
runs each phase under its own pool and waits between them; it is still
one group with one cut and one debt, and `run_cycle`'s evaluation at the
barrier is untouched. A usage limit in phase 1 withholds phase 2, whose
pairs then have no result and are owed exactly as a pool refusal's are.
`MAX_CONCURRENT_PASSES=1` is still one code path: one worker per phase,
Sage last. The selector's order still decides the log line and the pair
list; phase decides run order only.
```

- [ ] **Step 2: Gotchas bullets**

Append to the `## Gotchas when editing` list:

```markdown
- `personas/code/sage.md` carries `phase: 2`, and `tools/import-advocate-personas.py`
  writes only `label:` and `success:`. A re-run drops the key and demotes Sage
  to phase 1 with no error anywhere; `tests/test_personas.py`'s shipped-phases
  test is what fails instead. Keep the key when reviewing an importer diff.
- The round line goes in the system prompt beside the persona, never in the
  task prompt, and for the same reasons: `--append-system-prompt` is re-passed
  on `--resume`, and an operator override must not be edited.
```

- [ ] **Step 3: HISTORY.md and spec status**

Read the top of `HISTORY.md` for its entry format and add an entry for this work in that format, covering: the price in the code contract, the round ladder, Sage's mandate and phase 2, and the importer pin. Then change the spec's `Status:` line to `Status: implemented on feat/change-driven-review`.

- [ ] **Step 4: Verify**

Run: `./test-python.sh 2>&1 | tail -3`
Expected: `OK` (docs only, but the run confirms nothing was disturbed).

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md HISTORY.md docs/superpowers/specs/2026-09-12-priced-findings-design.md
git commit -m "docs: priced findings, the round ladder, and phases

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ex1UJiNmNrSqDX6dxSYKSM"
```
