# Parallel Personas, Phase B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Review a PR with all of its personas concurrently, with a barrier at the end of each PR before the next one starts.

**Architecture:** The flat pair walk becomes a walk over per-PR groups. Inside a group, a `ThreadPoolExecutor` runs one task per persona. `RESUME_AT`, an index, becomes `owed`, a set of `Pair`, so a cycle cut by a usage limit re-runs only the personas that were limited rather than the whole group. The shared working clone is defended twice: a prompt stanza forbidding writing git commands, and `chmod a-w` on the `.git` directory inode.

**Tech Stack:** Python 3 stdlib (`concurrent.futures`, `os.chmod`). No new dependency.

**Spec:** `docs/superpowers/specs/2026-08-30-claudebox-python-loop-design.md`, Section 4.

**Prerequisite:** Phase A complete and its exit gate passed. This plan is written against the interfaces Phase A's plan produces; if Phase A's implementation diverged from them, reconcile before starting.

## Global Constraints

- **`MAX_CONCURRENT_PASSES=1` must take the identical code path** as any other value, with a one-worker pool. There must be no separate sequential branch to drift. This is what lets every inherited `test-personas.sh` case keep its ordinal assertions.
- **Default is unlimited**, meaning the group's persona count. The shipped default gets revisited after live observation on a Max20 plan; do not lower it in this plan.
- **Nothing is killed mid-review.** A limit reported by one persona does not terminate its in-flight siblings.
- **The barrier is per PR.** Groups are strictly serialized; only personas within one group overlap.
- **Log line atomicity comes from `common.log`'s lock**, which Phase A already holds. Do not add a second write path that bypasses it.

---

### Task 1: `MAX_CONCURRENT_PASSES` and group dispatch

**Files:**
- Modify: `reviewer/review_loop.py`
- Modify: `tests/test_review_loop.py`

**Interfaces:**
- Consumes: `Supervisor`, `Pair`, `PassResult` from Phase A
- Produces:
  - `Group` frozen dataclass with `pr: int`, `mode: str`, `pairs: tuple[Pair, ...]`
  - `Supervisor.build_groups(candidates: list[tuple[int, str]]) -> list[Group]`, added **alongside** `build_pairs`, which Task 2 removes. Keeping both for one task is what lets `main()` and every Phase A test stay green at this task's commit; deleting `build_pairs` here would leave `main()` calling a method that no longer exists.
  - `Supervisor.__init__` gains `max_concurrent: int` (0 meaning unlimited)
  - `Supervisor.run_group(group: Group, to_run: list[Pair]) -> dict[Pair, PassResult]`
  - `parse_max_concurrent(env) -> int`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review_loop.py`:

```python
import threading


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
        # are genuinely in flight at once. A sequential implementation deadlocks
        # here and the test times out, which is the failure we want.
        barrier = threading.Barrier(2, timeout=5)

        class Concurrent(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                barrier.wait()
                return ok("S1")

        s = supervisor([], max_concurrent=0)
        s.__class__ = Concurrent
        group = s.build_groups([(12, "code")])[0]
        results = s.run_group(group, list(group.pairs))
        self.assertEqual(len(results), 2)

    def test_a_raising_pass_does_not_lose_its_siblings_results(self):
        class Exploding(FakeSupervisor):
            def _run_one(self, pair, prompt, session_id):
                if pair.persona == "red_team":
                    raise RuntimeError("boom")
                return ok("S1")

        s = supervisor([], max_concurrent=0)
        s.__class__ = Exploding
        group = s.build_groups([(12, "code")])[0]
        results = s.run_group(group, list(group.pairs))
        self.assertEqual(results[Pair(12, "code", "sage")].rc, 0)
        # An exception is a non-limit failure, so its pair's session is dropped
        # like any other. It must not take the group with it.
        self.assertNotEqual(results[Pair(12, "code", "red_team")].rc, 0)
        self.assertFalse(results[Pair(12, "code", "red_team")].limited)

    def test_to_run_narrows_which_personas_execute(self):
        s = supervisor([ok("A")], max_concurrent=0)
        group = s.build_groups([(12, "code")])[0]
        results = s.run_group(group, [Pair(12, "code", "sage")])
        self.assertEqual(list(results), [Pair(12, "code", "sage")])


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
```

Also update the `supervisor()` helper's `defaults` dict to include `max_concurrent=1`, and update `FakeSupervisor` to accept it.

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -v`
Expected: FAIL with `AttributeError: 'Supervisor' object has no attribute 'build_groups'`

- [ ] **Step 3: Write the implementation**

In `reviewer/review_loop.py`, add the imports and the `Group` type:

```python
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Group:
    """One PR's personas, run together behind a barrier.

    The group is the unit of both concurrency and cut-short accounting: a limit
    reported by one persona cannot recall its in-flight siblings, so the group
    finishes and only then does the cycle stop.
    """

    pr: int
    mode: str
    pairs: Tuple[Pair, ...]


def parse_max_concurrent(env) -> int:
    """How many passes may run at once inside a group. 0 means unlimited.

    Unlimited is the default: the group's persona count. An operator who hits
    rate-limit or memory pressure dials it down without a rebuild.
    """
    raw = env.get("MAX_CONCURRENT_PASSES", "").strip()
    if not raw:
        return 0
    if not raw.isdigit():
        raise ConfigError("MAX_CONCURRENT_PASSES must be a non-negative integer")
    return int(raw)
```

Add `max_concurrent: int = 0` to `Supervisor.__init__` and store it. Leave `build_pairs` and the existing `run_cycle` alone; Task 2 replaces both. Add beside them:

```python
    def build_groups(self, candidates) -> List[Group]:
        return [
            Group(
                pr=pr,
                mode=mode,
                pairs=tuple(Pair(pr, mode, p) for p in self.personas[mode]),
            )
            for pr, mode in candidates
        ]

    def workers_for(self, group: Group, to_run: Sequence[Pair]) -> int:
        """Effective concurrency for this group: the cap, or the group's size."""
        if self.max_concurrent > 0:
            return max(1, min(self.max_concurrent, len(to_run)))
        return max(1, len(to_run))

    def run_group(self, group: Group, to_run: Sequence[Pair]) -> Dict[Pair, "passes.PassResult"]:
        """Run to_run concurrently and wait for all of them.

        Nothing is killed. A limit reported by one persona leaves its siblings
        running, because a killed pass may have posted some findings and not
        others, and its session-id recovery is unreliable.
        """
        results: Dict[Pair, passes.PassResult] = {}
        if not to_run:
            return results

        with ThreadPoolExecutor(max_workers=self.workers_for(group, to_run)) as pool:
            futures = {pool.submit(self._dispatch, pair): pair for pair in to_run}
            for future, pair in futures.items():
                results[pair] = future.result()
        return results

    def _dispatch(self, pair: Pair) -> "passes.PassResult":
        session_id = self.sessions.get(pair)
        if session_id:
            log(f"Reviewing PR #{pair.pr} [{pair.mode}/{pair.persona}] "
                f"(resuming session {session_id})...")
            template = self.followup_prompts[pair.mode]
        else:
            log(f"Reviewing PR #{pair.pr} [{pair.mode}/{pair.persona}] (new session)...")
            template = self.review_prompts[pair.mode]

        try:
            return self._run_one(pair, prompts_mod.render(template, pair.pr), session_id)
        except Exception as exc:  # noqa: BLE001
            # A raise here is this supervisor's bug, not the provider's, so it
            # must not be classified as a limit and must not take the group with
            # it. Reported as an ordinary non-limit failure.
            log(f"WARN: pass raised {exc!r}", pair=pair)
            return passes.PassResult(rc=1, session_id=session_id, limited=False, limit_line="")
```

Note that `_dispatch` runs on a worker thread, so every log line it emits goes through `common.log`'s lock. Ordering across personas is not deterministic; ordering within a persona is.

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -v`
Expected: PASS. The `test_unlimited_runs_the_group_concurrently` barrier will time out after 5 seconds and fail loudly if the pool is not actually concurrent.

- [ ] **Step 5: Commit**

```bash
git add reviewer/review_loop.py tests/test_review_loop.py
git commit -m "feat(reviewer): run a PR's personas concurrently behind a per-PR barrier"
```

---

### Task 2: The `owed` resume set

**Files:**
- Modify: `reviewer/review_loop.py`
- Modify: `tests/test_review_loop.py`

**Interfaces:**
- Consumes: `Group`, `run_group` from Task 1
- Produces:
  - `Supervisor.owed: set[Pair]` replacing `Supervisor.resume_at`
  - `Supervisor.order_groups(groups: list[Group]) -> list[Group]`
  - `Supervisor.pairs_to_run(group: Group) -> list[Pair]`
  - `Supervisor.run_cycle(groups: list[Group]) -> bool` (signature changes from pairs to groups)
  - `Supervisor.start_index` and `Supervisor.build_pairs` are deleted here, along with the Phase A tests that exercised them (`BuildPairsTest`, and the `resume_at` assertions replaced by `OwedTest` below)

- [ ] **Step 1: Write the failing test**

Replace the whole `CutAndResumeTest` class in `tests/test_review_loop.py` with:

```python
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

    def test_owed_groups_run_first(self):
        s = supervisor([], max_concurrent=0)
        # Cut in group 13, so 13 is owed and 12 is not.
        self.cycle(s, {self.SG13: limited("S9")})
        s.attempted.clear()
        self.cycle(s, {})
        self.assertEqual(s.attempted[0].pr, 13)

    def test_a_group_that_completed_before_the_cut_still_runs_after_the_owed_ones(self):
        # This is what preserves the wrap-around, and it is what keeps a
        # persistent limit from starving the tail of the list forever.
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG13: limited("S9")})
        s.attempted.clear()
        self.cycle(s, {})
        self.assertEqual(set(p.pr for p in s.attempted), {12, 13})

    def test_two_clean_cycles_after_a_cut_return_to_full_service(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})
        self.cycle(s, {})
        s.attempted.clear()
        self.cycle(s, {})
        self.assertEqual(len(s.attempted), 4)

    def test_a_closed_pr_drops_out_of_owed(self):
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG13: limited("S9")})

        class Scripted(FakeSupervisor):
            def _run_one(inner, pair, prompt, session_id):
                inner.attempted.append(pair)
                return ok("S1")

        s.__class__ = Scripted
        s.attempted.clear()
        s.run_cycle(s.build_groups([(12, "code")]))
        self.assertEqual(s.owed, set())
        self.assertEqual(set(p.pr for p in s.attempted), {12})

    def test_an_empty_candidate_list_keeps_owed(self):
        # Enumeration failures degrade to an empty candidate list, so "no
        # candidate PRs" can mean gh had a bad minute. That must not silently
        # clear the debt.
        s = supervisor([], max_concurrent=0)
        self.cycle(s, {self.SG12: limited("S9")})
        before = set(s.owed)
        s.run_cycle([])
        self.assertEqual(s.owed, before)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -v`
Expected: FAIL with `AttributeError: 'Supervisor' object has no attribute 'owed'`

- [ ] **Step 3: Write the implementation**

In `Supervisor.__init__`, replace `self.resume_at = None` with:

```python
        # Pairs the last cycle owed but did not run: the personas a limit cut,
        # plus everything in the groups it never reached. Normally empty. In
        # memory alongside the session map; surviving a restart is deferred.
        self.owed: set = set()
```

Delete `start_index`. Add:

```python
    def pairs_to_run(self, group: Group) -> List[Pair]:
        """Which of this group's personas run this cycle.

        A group that owes something runs ONLY what it owes: those personas were
        cut, and the rest of the group ran successfully in the interrupted
        cycle. A group that owes nothing runs in full. So an under-served group
        is under-served for exactly one cycle.
        """
        owed_here = [p for p in group.pairs if p in self.owed]
        return owed_here if owed_here else list(group.pairs)

    def order_groups(self, groups: List[Group]) -> List[Group]:
        """Owed groups first, in enumeration order; the rest after, in order."""
        owed_groups = [g for g in groups if any(p in self.owed for p in g.pairs)]
        rest = [g for g in groups if not any(p in self.owed for p in g.pairs)]
        if owed_groups:
            log("Resuming with " + " ".join(str(p) for g in owed_groups
                                            for p in self.pairs_to_run(g)) + ".")
        return owed_groups + rest
```

Replace `run_cycle` entirely:

```python
    def run_cycle(self, groups: List[Group]) -> bool:
        """Walk the groups. Returns True when a usage limit cut the cycle."""
        if not groups:
            return False

        # Prune debts whose PR closed or whose label changed, so a dead pair
        # cannot sit in `owed` forever reordering the list around a group that
        # no longer exists.
        live = {p for g in groups for p in g.pairs}
        self.owed &= live

        ordered = self.order_groups(groups)
        consecutive_failures = 0
        cut_index: Optional[int] = None
        cut_owes: set = set()
        was_limited = False

        for index, group in enumerate(ordered):
            to_run = self.pairs_to_run(group)
            results = self.run_group(group, to_run)

            any_success = False
            group_failures = 0
            limited_here = set()

            for pair in to_run:
                result = results[pair]
                if result.rc == 0:
                    any_success = True
                    self._record_success(pair, result)
                elif result.limited:
                    limited_here.add(pair)
                    self._record_limit(pair, result)
                else:
                    group_failures += 1
                    self._record_failure(pair)

            # Evaluated at the barrier rather than per pass. A success anywhere
            # in the group resets it: the provider is alive.
            if any_success:
                consecutive_failures = 0
            else:
                consecutive_failures += group_failures

            if limited_here:
                was_limited = True
                cut_index, cut_owes = index, limited_here
                break

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log(f"WARN: {consecutive_failures} passes in a row failed for reasons "
                    "other than a limit; the provider looks unhealthy. Abandoning this cycle.")
                cut_index, cut_owes = index, set()
                break

        if cut_index is None:
            self.owed = set()
            return was_limited

        # Owe the cut group's limited personas, plus whatever each unreached
        # group would have run.
        new_owed = set(cut_owes)
        skipped: List[Pair] = []
        for group in ordered[cut_index + 1 :]:
            for pair in self.pairs_to_run(group):
                new_owed.add(pair)
                skipped.append(pair)
        self.owed = new_owed

        if skipped:
            log("Not reviewed this cycle: " + " ".join(str(p) for p in skipped) + ".")
        if new_owed:
            log("Owed next cycle: " + " ".join(str(p) for p in sorted(new_owed)) + ".")
        return was_limited

    def _record_success(self, pair: Pair, result) -> None:
        if result.session_id:
            self.sessions[pair] = result.session_id
        self.passes_done[pair] = self.passes_done.get(pair, 0) + 1
        log(f"review complete (session {self.sessions.get(pair)}, "
            f"pass {self.passes_done[pair]}).", pair=pair)
        if (
            self.max_passes_per_session > 0
            and self.passes_done[pair] >= self.max_passes_per_session
        ):
            log(f"reached MAX_PASSES_PER_SESSION={self.max_passes_per_session}; "
                "rotating its session next cycle.", pair=pair)
            self.sessions.pop(pair, None)
            self.passes_done[pair] = 0

    def _record_limit(self, pair: Pair, result) -> None:
        if result.session_id:
            self.sessions[pair] = result.session_id
            log("WARN: hit a usage or rate limit; keeping its session and "
                "ending this cycle after the group finishes.", pair=pair)
        else:
            log("WARN: hit a usage or rate limit before it had a session; "
                "ending this cycle after the group finishes.", pair=pair)
        if result.limit_line:
            log(f"  limit reported by claude: {result.limit_line}", pair=pair)

    def _record_failure(self, pair: Pair) -> None:
        log("WARN: review failed; starting a fresh session for it next cycle.", pair=pair)
        self.sessions.pop(pair, None)
        self.passes_done[pair] = 0
```

Update `main()`: `supervisor.run_cycle(supervisor.build_groups(candidates))`, and pass `max_concurrent=parse_max_concurrent(env)` into the constructor.

Note the log-line move: `_record_success` and friends now pass `pair=pair` instead of spelling the PR and persona into the message, because Phase A's prefix already carries it. Update any Phase A test asserting the old wording.

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add reviewer/review_loop.py tests/test_review_loop.py
git commit -m "feat(reviewer): resume only the personas a limit actually cut"
```

---

### Task 3: The shared-worktree stanza

**Files:**
- Modify: `reviewer/prompts.py`
- Modify: `reviewer/review_loop.py`
- Modify: `tests/test_prompts.py`

**Interfaces:**
- Consumes: `prompts.build` from Phase A
- Produces:
  - `prompts.WORKTREE_STANZA: str`
  - `prompts.build(env, shared_worktree_modes: frozenset[str] = frozenset()) -> Prompts`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_prompts.py`:

```python
class WorktreeStanzaTest(unittest.TestCase):
    def test_absent_when_no_mode_is_concurrent(self):
        p = prompts.build({})
        for table in (p.review, p.followup):
            for mode in ("code", "plan"):
                self.assertNotIn(prompts.WORKTREE_STANZA, table[mode])

    def test_present_on_both_prompts_of_a_concurrent_mode(self):
        p = prompts.build({}, shared_worktree_modes=frozenset({"code"}))
        self.assertIn(prompts.WORKTREE_STANZA, p.review["code"])
        self.assertIn(prompts.WORKTREE_STANZA, p.followup["code"])

    def test_absent_from_a_mode_that_is_not_concurrent(self):
        p = prompts.build({}, shared_worktree_modes=frozenset({"code"}))
        self.assertNotIn(prompts.WORKTREE_STANZA, p.review["plan"])

    def test_appended_to_an_operator_override_too(self):
        # THE ONE PLACE THE VERBATIM GUARANTEE IS BROKEN, deliberately. A prompt
        # that lets a persona check out a branch under concurrency corrupts the
        # other personas' reviews, so it is not an operator-level opt-out.
        p = prompts.build(
            {"REVIEW_PROMPT": "just look at #{{PR}}"},
            shared_worktree_modes=frozenset({"code"}),
        )
        self.assertTrue(p.review["code"].startswith("just look at #{{PR}}"))
        self.assertIn(prompts.WORKTREE_STANZA, p.review["code"])

    def test_lands_after_the_suffix(self):
        p = prompts.build(
            {"REVIEW_PROMPT_SUFFIX": "Be terse."},
            shared_worktree_modes=frozenset({"code"}),
        )
        self.assertTrue(p.review["code"].endswith(prompts.WORKTREE_STANZA))
        self.assertIn(" Be terse. ", p.review["code"])

    def test_forbids_the_commands_that_take_index_lock(self):
        for verb in ("checkout", "fetch", "branch", "stash"):
            self.assertIn(verb, prompts.WORKTREE_STANZA)

    def test_names_the_read_path(self):
        self.assertIn("gh pr diff", prompts.WORKTREE_STANZA)
        self.assertIn("gh pr view", prompts.WORKTREE_STANZA)
```

Add to `tests/test_review_loop.py`:

```python
class SharedWorktreeModesTest(unittest.TestCase):
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -v`
Expected: FAIL with `AttributeError: module 'prompts' has no attribute 'WORKTREE_STANZA'`

- [ ] **Step 3: Write the implementation**

In `reviewer/prompts.py`:

```python
# Emitted only when a mode's personas actually run together. Unlike every other
# stanza it is appended to an operator OVERRIDE as well as to the defaults: a
# prompt that lets a persona check out a branch under concurrency corrupts the
# other personas' reviews of the same PR, which is not an operator-level opt-out.
# Backed by a permission boundary in review_loop.lock_git_dir; the two are halves
# of one defense, because the chmod is a chokepoint rather than a wall.
WORKTREE_STANZA = (
    "One more constraint, and it is not optional: the git working copy in your "
    "current directory is SHARED with other reviewers reading this same pull "
    "request at this same moment. Read the change through `gh pr diff` and "
    "`gh pr view` rather than through the working copy, and run no git command "
    "that writes: no checkout, no fetch, no pull, no branch, no stash, no "
    "commit, no reset. Read-only git (log, show, diff, cat-file, ls-tree) is "
    "fine. A writing command will either fail on a permission error or corrupt "
    "another reviewer's read, so if one fails, do not retry it and do not work "
    "around it -- use gh instead."
)


def build(env, shared_worktree_modes=frozenset()) -> Prompts:
    ...  # existing body unchanged through the suffix loop
    # Last, so it is the final thing in the prompt, and after the suffix so an
    # operator's own last word cannot displace it.
    for mode in ("code", "plan"):
        if mode in shared_worktree_modes:
            review[mode] = f"{review[mode]} {WORKTREE_STANZA}"
            followup[mode] = f"{followup[mode]} {WORKTREE_STANZA}"

    return Prompts(review=review, followup=followup)
```

In `reviewer/review_loop.py`:

```python
def shared_worktree_modes(personas: Dict[str, List[str]], max_concurrent: int) -> frozenset:
    """Modes whose personas actually run together this run.

    Per mode rather than globally, because effective concurrency is
    min(cap, that mode's persona count): a mode dialed down to one persona has
    nothing running beside it and gets byte-identical prompts to Phase A.
    Persona sets do not change while the container runs, so a resumed session
    cannot gain or lose the stanza between passes.
    """
    out = set()
    for mode, ids in personas.items():
        effective = len(ids) if max_concurrent <= 0 else min(max_concurrent, len(ids))
        if effective > 1:
            out.add(mode)
    return frozenset(out)
```

In `main()`, compute it before `prompts_mod.build` and pass it in:

```python
    max_concurrent = parse_max_concurrent(env)
    persona_ids = {m: [p.id for p in ps] for m, ps in resolved.items()}
    worktree_modes = shared_worktree_modes(persona_ids, max_concurrent)
    if worktree_modes:
        log("Shared-worktree constraint active for: " + " ".join(sorted(worktree_modes)))
    built = prompts_mod.build(env, shared_worktree_modes=worktree_modes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -v`
Expected: PASS. The Phase A fixture tests must still pass unchanged, because `build({})` with no `shared_worktree_modes` produces exactly what it produced before.

- [ ] **Step 5: Commit**

```bash
git add reviewer/prompts.py reviewer/review_loop.py tests/test_prompts.py tests/test_review_loop.py
git commit -m "feat(reviewer): tell personas the working copy is shared"
```

---

### Task 4: Environment enforcement on the shared clone

**Files:**
- Modify: `reviewer/review_loop.py`
- Modify: `entrypoint.sh`
- Modify: `tests/test_review_loop.py`

**Interfaces:**
- Consumes: `shared_worktree_modes` from Task 3
- Produces:
  - `lock_git_dir(work_repo: str) -> None`
  - `unlock_git_dir(work_repo: str) -> None`
  - `unlocked_git_dir(work_repo: str, enabled: bool)` context manager

- [ ] **Step 1: Write the failing test**

Append to `tests/test_review_loop.py`:

```python
import os
import stat
import subprocess
import tempfile


@unittest.skipUnless(shutil.which("git"), "git not available")
class GitDirLockTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "t"], check=True)
        open(os.path.join(self.repo, "f.txt"), "w").close()
        subprocess.run(["git", "-C", self.repo, "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "init"], check=True)

    def _cleanup(self):
        review_loop.unlock_git_dir(self.repo)
        shutil.rmtree(self.repo, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", self.repo, *args], capture_output=True, text=True, check=False
        )

    def test_reading_git_still_works_when_locked(self):
        review_loop.lock_git_dir(self.repo)
        for args in (("log", "-1", "--oneline"), ("show", "--stat", "HEAD"), ("diff", "HEAD")):
            self.assertEqual(self.git(*args).returncode, 0, args)

    def test_a_writing_command_fails_when_locked(self):
        review_loop.lock_git_dir(self.repo)
        # Needs .git/index.lock, which needs write permission on the .git
        # directory inode. This is the whole mechanism.
        open(os.path.join(self.repo, "g.txt"), "w").close()
        self.assertNotEqual(self.git("add", "g.txt").returncode, 0)

    def test_branch_creation_fails_when_locked(self):
        review_loop.lock_git_dir(self.repo)
        self.assertNotEqual(self.git("checkout", "-b", "feature").returncode, 0)

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

    def test_disabled_is_a_no_op(self):
        with review_loop.unlocked_git_dir(self.repo, enabled=False):
            pass
        mode = os.stat(os.path.join(self.repo, ".git")).st_mode
        self.assertTrue(mode & stat.S_IWUSR)

    def test_locking_is_not_recursive(self):
        # chmod -R on .git is O(object count), which is the operation that
        # produced the file-count guard in commit fdd0ac1. Only the inode moves.
        objects = os.path.join(self.repo, ".git", "objects")
        before = os.stat(objects).st_mode
        review_loop.lock_git_dir(self.repo)
        self.assertEqual(os.stat(objects).st_mode, before)
```

Add `import shutil` at the top of the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -v`
Expected: FAIL with `AttributeError: module 'review_loop' has no attribute 'lock_git_dir'`

- [ ] **Step 3: Write the implementation**

In `reviewer/review_loop.py`:

```python
import stat
from contextlib import contextmanager


def _git_dir(work_repo: str) -> str:
    return os.path.join(work_repo, ".git")


def lock_git_dir(work_repo: str) -> None:
    """Drop write permission on the .git DIRECTORY INODE. Not recursive.

    Every mutating git operation creates a lock file directly in .git first --
    index.lock for checkout/add/commit, FETCH_HEAD and the ref locks for fetch --
    and creating a file in a directory needs write permission on that directory.
    Reading paths never do, so log, show, diff and cat-file keep working.

    One inode, so this is O(1) and can be toggled around the supervisor's own
    fetch. A recursive chmod would be O(object count), which is the operation
    that produced the file-count guard in commit fdd0ac1.

    This is a CHOKEPOINT, not a wall: object writes into .git/objects/** would
    succeed on their own, but nothing reaches them without taking a lock first.
    The prompt stanza in prompts.WORKTREE_STANZA is the other half of the
    defense, not a redundant restatement of this one.

    The working tree is deliberately left writable, so a persona that drops a
    scratch file does not hit a confusing error. Scratch files cannot corrupt
    another persona's review, because reviews come through `gh pr diff`.
    """
    path = _git_dir(work_repo)
    if not os.path.isdir(path):
        return
    mode = os.stat(path).st_mode
    os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def unlock_git_dir(work_repo: str) -> None:
    path = _git_dir(work_repo)
    if not os.path.isdir(path):
        return
    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)


@contextmanager
def unlocked_git_dir(work_repo: str, enabled: bool):
    """Writable for the duration, locked again on the way out, raise or not.

    Groups are strictly serialized after the fetch, so no pass ever observes the
    window this opens.
    """
    if not enabled:
        yield
        return
    unlock_git_dir(work_repo)
    try:
        yield
    finally:
        lock_git_dir(work_repo)
```

In `main()`, after computing `worktree_modes`:

```python
    enforce_lock = bool(worktree_modes)
    if enforce_lock:
        lock_git_dir(env["WORK_REPO"])
        log("Working clone's .git is read-only between cycles (shared-worktree enforcement).")
```

And wrap the fetch:

```python
        log("Fetching latest refs...")
        with unlocked_git_dir(supervisor.cwd, enforce_lock):
            fetched = subprocess.run(
                ["git", "fetch", "--all", "--prune", "--quiet"],
                cwd=supervisor.cwd, capture_output=True, text=True, check=False,
            )
        if fetched.returncode != 0:
            log("WARN: git fetch failed; continuing")
```

- [ ] **Step 4: Make `entrypoint.sh` survive a restart into a locked clone**

The clone persists across container restarts under `--restart unless-stopped`, so on the second boot the shell meets a `.git` it cannot write, and `git remote set-url` (which writes `.git/config` through `.git/config.lock`) fails. Add this immediately before the `git config --global --add safe.directory` lines in the clone-prep block:

```bash
# A previous run may have left the clone's .git read-only (the shared-worktree
# enforcement in the supervisor). Restore write access for our own setup here;
# the supervisor locks it again once it is running.
[ -d "$WORK_REPO/.git" ] && chmod u+w "$WORK_REPO/.git" 2>/dev/null || true
```

- [ ] **Step 5: Verify and commit**

Run:

```bash
./test-python.sh -v
bash -n entrypoint.sh
```

Expected: PASS, `bash -n` silent.

```bash
git add reviewer/review_loop.py entrypoint.sh tests/test_review_loop.py
git commit -m "feat: make the shared clone's .git read-only between cycles"
```

---

### Task 5: Parallel-mode acceptance tests

**Files:**
- Modify: `test-personas.sh`

**Interfaces:**
- Consumes: everything above
- Produces: `test-personas.sh` cases running at `MAX_CONCURRENT_PASSES=1` (all inherited cases, unchanged assertions) plus a new block at unlimited concurrency with content-keyed assertions

- [ ] **Step 1: Pin every inherited case to a cap of 1**

Add `MAX_CONCURRENT_PASSES=1` to the baseline env in `run_entrypoint` (`test-personas.sh:155-162`). Update the header comment: invocation ordering is deterministic only because the cap is 1, and any case that removes the pin must not use ordinal assertions.

Run `./test-personas.sh` and confirm every existing case still passes with its ordinal `ARGV:N:` / `ENV:N:` / `STUB_FAIL_ON` assertions untouched. If one fails, the cap-of-1 path has drifted from sequential and Task 1's constraint is violated; stop and fix that before continuing.

- [ ] **Step 2: Make the claude stub's counter atomic**

The current read-modify-write on `$HOME/calls` races the moment two `claude` processes start together. Replace the first two lines of the `claude` stub with:

```bash
#!/usr/bin/env bash
# The invocation number is allocated under a mkdir lock. mkdir is atomic on
# every filesystem we care about, and a busy-wait is fine at this scale. Without
# it, concurrent passes collide and dump files are silently overwritten, which
# would make a parallel case pass while proving nothing.
while ! mkdir "$HOME/calls.lock" 2>/dev/null; do :; done
n=$(( $(cat "$HOME/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$HOME/calls"
rmdir "$HOME/calls.lock"
```

Also change `STUB_FAIL_ON` to key on the persona rather than the invocation number, since invocation order stops being meaningful under concurrency. Add beside it:

```bash
# STUB_FAIL_PERSONA: fail every invocation whose --append-system-prompt carries
# this label. Ordinal STUB_FAIL_ON still works and is what the cap-of-1 cases
# use; this is for the parallel block, where "invocation 3" names nothing.
if [ -n "${STUB_FAIL_PERSONA:-}" ] && printf '%s' "$*" | grep -qF -- "$STUB_FAIL_PERSONA"; then
  should_fail=1
fi
```

Wire `should_fail` into the same branch the ordinal `STUB_FAIL_ON` match takes.

- [ ] **Step 3: Add content-keyed assertion helpers**

Add near `calls()`:

```bash
# Under concurrency the dump INDEX means nothing, so assertions address a dump
# by what is in it. dump_matching echoes the path of the single dump whose argv
# contains $1, or nothing when zero or more than one match.
dump_matching() {
  local hit="" d
  for d in "$HOME_DIR"/dump.*; do
    [ -e "$d" ] || continue
    if grep -qF -- "$1" <(awk '/^ENV /{exit} {print}' "$d"); then
      [ -z "$hit" ] || return 1
      hit="$d"
    fi
  done
  [ -n "$hit" ] && printf '%s' "$hit"
}

# How many dumps' argv contain $1.
count_matching() {
  local n=0 d
  for d in "$HOME_DIR"/dump.*; do
    [ -e "$d" ] || continue
    grep -qF -- "$1" <(awk '/^ENV /{exit} {print}' "$d") && n=$((n + 1))
  done
  printf '%s' "$n"
}
```

Add two assertion forms to the `wires`-style checker: `MATCHCOUNT:<sel>:<n>` (exactly n dumps carry `<sel>`) and `MATCHARGV:<sel>:<want>` (the unique dump carrying `<sel>` also carries `<want>`).

- [ ] **Step 4: Add the parallel block**

Add these cases, all with `MAX_CONCURRENT_PASSES` unset (unlimited) and `MAX_CYCLES=2`:

1. **All four code personas run on one PR.** `PR_IDS=12`, default `PERSONAS`. Assert `CALLS:8` (four personas, two cycles) and `MATCHCOUNT:Red Team:2`, `MATCHCOUNT:Adversar:2` (adjust each selector to a substring unique to that persona's label in `--append-system-prompt`).

2. **The worktree stanza reaches every pass.** Assert `MATCHCOUNT:no stash:8`, so the stanza is on both the new and the resumed prompt for every persona.

3. **The worktree stanza is absent at a cap of 1.** Same case with `MAX_CONCURRENT_PASSES=1`; assert `MATCHCOUNT:no stash:0`.

4. **The worktree stanza survives an operator prompt override.** `REVIEW_PROMPT='look at #{{PR}}'`; assert one dump carries both `look at #12` and `no stash`.

5. **A resumed parallel pass still carries its persona.** The property the whole design rests on. Assert that the dump carrying both `--resume` and `Red Team` exists: `MATCHARGV:--resume:Red Team` will not work when several dumps carry `--resume`, so assert `MATCHCOUNT:--resume:4` (four personas resumed on cycle two) and, for one persona, that a dump exists carrying `--resume`, `Red Team`, and `--append-system-prompt` together.

6. **A limit in one persona lets its siblings finish and owes only that persona.** `STUB_FAIL_PERSONA='Red Team'`, `STUB_FAIL_MODE=limit`, two PRs. Assert `CALLS` shows all four of PR 12's personas ran in cycle one, that PR 13 ran zero passes in cycle one, and that cycle two ran Red Team on PR 12 plus all of PR 13.

7. **A non-limit failure in one persona does not stop the group.** `STUB_FAIL_PERSONA='Red Team'`, `STUB_FAIL_MODE=other`. Assert the other three personas' dumps exist for that cycle.

- [ ] **Step 5: Run and commit**

Run:

```bash
./test-personas.sh
./test-providers.sh
./test-python.sh
```

Expected: all green.

```bash
git add test-personas.sh
git commit -m "test: cover parallel persona dispatch, limits and the worktree stanza"
```

---

### Task 6: Launcher passthrough and documentation

**Files:**
- Modify: `claudebox.sh`
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `CLAUDE.md`
- Modify: `HISTORY.md`

**Interfaces:**
- Consumes: everything above
- Produces: `--max-concurrent-passes N` on the launcher, and docs describing the fan-out

- [ ] **Step 1: Add the launcher flag**

In `claudebox.sh`, add `--max-concurrent-passes N` beside the existing `--persona` handling, passing it through as `-e MAX_CONCURRENT_PASSES=N`. Keep it bash 3.2-safe: expand possibly-empty arrays as `${arr[@]+"${arr[@]}"}`, never `"${arr[@]}"`.

Add it to `--help`. Do **not** touch `--plan-persona`, `--plan-label`, or the stale `--persona` help text: those are explicitly out of scope per the spec, and widening this task is how they stop being out of scope.

Run `bash -n claudebox.sh` and `./claudebox.sh run --repo . --max-concurrent-passes 3 --dry-run` and confirm the printed docker command carries `-e MAX_CONCURRENT_PASSES=3`.

- [ ] **Step 2: Update `.env.example`**

```
# How many of a PR's personas review it at the same time. Unset or 0 = all of
# them, which is the default. Lower it if you are hitting provider rate limits
# or the container's memory ceiling; 1 restores the fully sequential behavior.
# MAX_CONCURRENT_PASSES=
```

- [ ] **Step 3: Update `CLAUDE.md`**

Rewrite the paragraph that currently reads "Concurrency is available (blind personas are unordered, and the clone is only read) and declined, because it multiplies instantaneous usage-limit pressure." It is now the opposite of what the code does. Replace it with the group-and-barrier description, the `owed` semantics, and the reason a limit no longer stops the cycle immediately.

Update the two places stating the verbatim-operator-prompt guarantee to say "verbatim except the shared-worktree constraint under concurrency", and say why.

Add to "Gotchas when editing":

- `MAX_CONCURRENT_PASSES=1` must stay on the same code path as any other value; a separate sequential branch would drift and would silently invalidate every ordinal assertion in `test-personas.sh`.
- `lock_git_dir` moves one inode, never `-R`. A recursive chmod on `.git` is O(object count), which is what commit `fdd0ac1` exists to avoid.
- `entrypoint.sh` must `chmod u+w` the clone's `.git` before its own git setup, because a restart meets a clone the supervisor left locked.

- [ ] **Step 4: Update `README.md`**

Describe the fan-out in the architecture section and add `MAX_CONCURRENT_PASSES` to the optional-configuration table.

- [ ] **Step 5: Verify and commit**

Run:

```bash
grep -c 'MAX_CONCURRENT_PASSES' README.md .env.example CLAUDE.md claudebox.sh
grep -c 'and declined' CLAUDE.md
bash -n claudebox.sh
./test-python.sh && ./test-providers.sh && ./test-personas.sh
```

Expected: `MAX_CONCURRENT_PASSES` in all four files; `and declined` returns zero (the stale claim is gone); `bash -n` silent; all three suites green.

```bash
git add claudebox.sh README.md .env.example CLAUDE.md HISTORY.md
git commit -m "docs: describe parallel persona review and its knob"
```

---

## Phase B exit gate

- [ ] All three suites green
- [ ] `bash -n entrypoint.sh && bash -n claudebox.sh` silent
- [ ] `python3 -m py_compile reviewer/*.py` silent
- [ ] A live run against a real repo with two or more open PRs, watched via `./claudebox.sh logs`, showing prefixed interleaved lines from concurrent personas and a clean per-PR barrier
- [ ] The shipped default for `MAX_CONCURRENT_PASSES` revisited against observed rate-limit behavior on a Max20 plan, and lowered in `.env.example` and `CLAUDE.md` if the observation warrants it

## Deferred, still

Unchanged from the spec: `claudebox.sh`'s missing `--plan-persona` / `--plan-label` and its stale `--persona` help text; persisting the session map and `owed` across container restarts; the four-copy `_gh_stanza` drift problem; cross-persona reconciliation.
