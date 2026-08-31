# Change-Driven Re-Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-review a pull request only when its head commit moved or somebody other than claudebox commented, instead of on every cycle.

**Architecture:** A new pure module `reviewer/signals.py` holds the fingerprint type, the signature test, the settle arithmetic, and a small cache that decides when a second GitHub lookup is worth making. `reviewer/gh.py` gains the lookup itself and widens the field list on the call it already makes. `Supervisor` gains one dict, `reviewed`, and one clause in `pairs_to_run`. Nothing about concurrency, the cut-and-owed bookkeeping, or the session map changes shape.

**Tech Stack:** Python 3 standard library only (the image installs no pip packages for the supervisor), `gh` CLI, bash for the acceptance suites.

**Spec:** `docs/superpowers/specs/2026-08-31-change-driven-review-design.md`

## Global Constraints

- Standard library only in `reviewer/`. Adding a pip dependency breaks the image, which installs none for the supervisor.
- Modules in `reviewer/` are flat and imported by plain name (`import gh`, not `from reviewer import gh`). `review_loop.py` runs as a script with its own directory on `sys.path`.
- Every startup misconfiguration raises `ConfigError`. `main` turns it into `ERROR:` on stderr and exit 1. A traceback out of PID 1 reads as a silent crash loop under `--restart unless-stopped`.
- No pass, no cycle, and no enumeration may let an exception escape to PID 1. `OSError` from a spawn is an ordinary failure.
- The signature marker string is `-claudebox`, matched case-insensitively as a substring of a comment body.
- `SETTLE_SECONDS` default is `30`. `REVIEW_INTERVAL_SECONDS` default becomes `60`, changed in both places it is written.
- `gh pr checks` is unusable and a bare `gh pr view` is forbidden; every `gh pr view` carries an explicit `--json` field list, and no new call may request `statusCheckRollup`.
- Run the Python suite with `./test-python.sh`. It takes `unittest discover` positionals, so name a module as `-p test_signals.py`, never as a bare argument.
- Commit after every task.

---

### Task 1: The fingerprint, the signature test, and the settle arithmetic

**Files:**
- Create: `reviewer/signals.py`
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `common.Pair` (not yet, but the module sits beside it).
- Produces:
  - `MARKER = "-claudebox"`
  - `@dataclass(frozen=True) class Signal: head_oid: str; mode: str; newest_human: str`
  - `is_own(body: Optional[str]) -> bool`
  - `newest_unsigned(entries: Iterable[Tuple[str, Optional[str]]]) -> str` where each entry is `(timestamp, body)`
  - `age_seconds(updated_at: str, now: float) -> Optional[float]`
  - `is_settling(updated_at: str, settle_seconds: int, now: float) -> bool`
  - `enabled(env: Mapping[str, str]) -> bool`
  - `change_reason(old: Optional[Signal], new: Signal) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_signals.py`:

```python
import unittest

import _path  # noqa: F401

import signals


class MarkerTest(unittest.TestCase):
    def test_signed_body_is_ours(self):
        self.assertTrue(signals.is_own("Looks fine to me.\n\n-claudebox (sage)"))

    def test_case_is_ignored(self):
        self.assertTrue(signals.is_own("-CLAUDEBOX (red_team)"))

    def test_unsigned_body_is_not_ours(self):
        self.assertFalse(signals.is_own("I disagree, see the ticket."))

    def test_empty_and_missing_bodies_are_not_ours(self):
        # A review submitted with no prose is still somebody acting on the PR.
        self.assertFalse(signals.is_own(""))
        self.assertFalse(signals.is_own(None))


class NewestUnsignedTest(unittest.TestCase):
    def test_picks_the_latest_unsigned_timestamp(self):
        entries = [
            ("2026-08-31T10:00:00Z", "a human"),
            ("2026-08-31T12:00:00Z", "-claudebox (sme)"),
            ("2026-08-31T11:00:00Z", "another human"),
        ]
        self.assertEqual(signals.newest_unsigned(entries), "2026-08-31T11:00:00Z")

    def test_no_unsigned_entries_gives_empty_string(self):
        entries = [("2026-08-31T12:00:00Z", "-claudebox (sage)")]
        self.assertEqual(signals.newest_unsigned(entries), "")

    def test_no_entries_at_all_gives_empty_string(self):
        self.assertEqual(signals.newest_unsigned([]), "")

    def test_entries_without_a_timestamp_are_ignored(self):
        # A malformed gh payload must not crash the cycle.
        self.assertEqual(signals.newest_unsigned([(None, "a human")]), "")


class AgeTest(unittest.TestCase):
    STAMP = "2026-08-31T00:00:00Z"

    def test_age_of_a_known_timestamp(self):
        base = signals._epoch(self.STAMP)
        self.assertEqual(signals.age_seconds(self.STAMP, base + 45.0), 45.0)

    def test_unparseable_timestamp_has_no_age(self):
        self.assertIsNone(signals.age_seconds("not a time", 0.0))

    def test_young_change_is_settling(self):
        base = signals._epoch(self.STAMP)
        self.assertTrue(signals.is_settling(self.STAMP, 30, base + 5.0))

    def test_old_change_is_not_settling(self):
        base = signals._epoch(self.STAMP)
        self.assertFalse(signals.is_settling(self.STAMP, 30, base + 31.0))

    def test_a_clock_behind_github_does_not_settle_forever(self):
        # Negative age means our clock is behind. Clamped to zero would still
        # settle; we run instead, so skew costs the batching and not the review.
        base = signals._epoch(self.STAMP)
        self.assertFalse(signals.is_settling(self.STAMP, 30, base - 600.0))

    def test_unparseable_timestamp_does_not_settle(self):
        self.assertFalse(signals.is_settling("not a time", 30, 0.0))

    def test_zero_disables_settling(self):
        self.assertFalse(
            signals.is_settling(self.STAMP, 0, signals._epoch(self.STAMP)))


class EnabledTest(unittest.TestCase):
    def test_default_is_on(self):
        self.assertTrue(signals.enabled({}))

    def test_off_spellings(self):
        for v in ("0", "false", "FALSE", "no", "off"):
            self.assertFalse(signals.enabled({"REVIEW_ON_CHANGE": v}), v)

    def test_anything_else_is_on(self):
        for v in ("1", "true", "yes", "on", "please"):
            self.assertTrue(signals.enabled({"REVIEW_ON_CHANGE": v}), v)


class ChangeReasonTest(unittest.TestCase):
    NEW = signals.Signal(head_oid="3f2a1b0deadbeef", mode="code", newest_human="")

    def test_no_prior_signal_is_a_first_review(self):
        self.assertEqual(signals.change_reason(None, self.NEW), "first review")

    def test_identical_signal_has_no_reason(self):
        self.assertEqual(signals.change_reason(self.NEW, self.NEW), "")

    def test_moved_head(self):
        old = signals.Signal(head_oid="aaaaaaa", mode="code", newest_human="")
        self.assertEqual(signals.change_reason(old, self.NEW), "new head 3f2a1b0")

    def test_new_comment(self):
        old = signals.Signal(head_oid="3f2a1b0deadbeef", mode="code", newest_human="")
        new = signals.Signal(
            head_oid="3f2a1b0deadbeef", mode="code", newest_human="2026-08-31T12:00:00Z"
        )
        self.assertEqual(signals.change_reason(old, new), "new comment activity")

    def test_mode_change(self):
        old = signals.Signal(head_oid="3f2a1b0deadbeef", mode="code", newest_human="")
        new = signals.Signal(head_oid="3f2a1b0deadbeef", mode="plan", newest_human="")
        self.assertEqual(signals.change_reason(old, new), "mode changed to plan")

    def test_several_reasons_are_all_named(self):
        old = signals.Signal(head_oid="aaaaaaa", mode="code", newest_human="")
        new = signals.Signal(
            head_oid="3f2a1b0deadbeef", mode="plan", newest_human="2026-08-31T12:00:00Z"
        )
        self.assertEqual(
            signals.change_reason(old, new),
            "new head 3f2a1b0, new comment activity, mode changed to plan",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p test_signals.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'signals'`

- [ ] **Step 3: Write the implementation**

Create `reviewer/signals.py`:

```python
"""What changed on a pull request since we last reviewed it.

A cycle used to re-review every candidate whether or not anything had happened
to it, which costs (PRs x personas) provider sessions per cycle to conclude
that nothing had. This module holds the decision: a per-PR fingerprint, the
test that tells claudebox's own comments from everybody else's, and the settle
arithmetic that batches a burst of pushes into one review.

Everything here is pure. The GitHub calls live in gh.py, and the state that
outlives a cycle lives on Supervisor.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Tuple

# Comments claudebox posts carry this, because personas/*/_shared.md tells every
# persona to sign its findings with it. Author login is deliberately NOT
# consulted: claudebox is commonly run under the operator's own PAT, where
# matching on login would classify the operator's own comments as ours and the
# comment trigger would never fire for the person most likely to want it.
MARKER = "-claudebox"

_OFF = ("0", "false", "no", "off")


@dataclass(frozen=True)
class Signal:
    """One PR's fingerprint. Two of these differ when a re-review is owed.

    `updatedAt` is deliberately absent. It moves for edits this design decided
    to ignore (a typo fix in the PR body, a title change), and including it
    would turn each of those into a full persona fan-out. Its job is to gate
    the second GitHub lookup, which is Tracker's business, not this record's.
    """

    head_oid: str
    mode: str
    # Timestamp of the newest comment that does not carry MARKER, or "".
    # ISO-8601 Z strings compare correctly as strings, so nothing here parses.
    newest_human: str


def is_own(body: Optional[str]) -> bool:
    """True when this comment body is one claudebox posted."""
    return MARKER in (body or "").lower()


def newest_unsigned(entries: Iterable[Tuple[Optional[str], Optional[str]]]) -> str:
    """The latest timestamp among entries whose body lacks the marker.

    Entries are (timestamp, body). One missing a timestamp is skipped rather
    than raising: a gh payload we cannot read must not take the cycle with it.
    """
    best = ""
    for stamp, body in entries:
        if not isinstance(stamp, str) or not stamp:
            continue
        if is_own(body):
            continue
        if stamp > best:
            best = stamp
    return best


def _epoch(stamp: str) -> float:
    """RFC 3339 UTC as GitHub writes it, e.g. 2026-08-31T12:00:00Z."""
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def age_seconds(updated_at: str, now: float) -> Optional[float]:
    """Seconds since the timestamp, or None when it cannot be read."""
    try:
        return now - _epoch(updated_at)
    except (AttributeError, ValueError):
        return None


def is_settling(updated_at: str, settle_seconds: int, now: float) -> bool:
    """True when this change is too fresh to review yet.

    A negative age means the container clock is behind GitHub's. It runs rather
    than settling, so skew costs the batching and never the review. An
    unreadable timestamp runs for the same reason.
    """
    if settle_seconds <= 0:
        return False
    age = age_seconds(updated_at, now)
    if age is None:
        return False
    return 0 <= age < settle_seconds


def enabled(env: Mapping[str, str]) -> bool:
    """Whether the change gate is on. Default on; REVIEW_ON_CHANGE=0 turns it off."""
    return (env.get("REVIEW_ON_CHANGE", "") or "").strip().lower() not in _OFF


def change_reason(old: Optional[Signal], new: Signal) -> str:
    """Why this PR is being reviewed, for the log. Empty when nothing changed."""
    if old is None:
        return "first review"
    reasons = []
    if old.head_oid != new.head_oid:
        reasons.append(f"new head {new.head_oid[:7]}")
    if old.newest_human != new.newest_human:
        reasons.append("new comment activity")
    if old.mode != new.mode:
        reasons.append(f"mode changed to {new.mode}")
    return ", ".join(reasons)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh -p test_signals.py`
Expected: PASS, and `./test-python.sh` still passes in full.

- [ ] **Step 5: Commit**

```bash
git add reviewer/signals.py tests/test_signals.py
git commit -m "feat(signals): a per-PR fingerprint, the signature test and settle arithmetic"
```

---

### Task 2: Widen stage one to carry the head and the update time

**Files:**
- Modify: `reviewer/gh.py` (`Candidate` type, `pr_modes`, `enumerate_candidate_prs`)
- Modify: `tests/test_gh.py` (existing assertions compare against `(number, mode)` tuples)
- Modify: `reviewer/review_loop.py:main` (the candidate log line and the `build_groups` call)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `@dataclass(frozen=True) class PRSnapshot: number: int; mode: str; head_oid: str; updated_at: str`
  - `pr_modes(payload, plan_label) -> List[PRSnapshot]` (same name, new element type)
  - `enumerate_candidate_prs(selector, env, run=subprocess.run) -> List[PRSnapshot]`

`build_groups` keeps taking `Sequence[Tuple[int, str]]`. `main` maps snapshots down to that, which keeps roughly thirty existing `run_cycle`/`build_groups` tests untouched.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gh.py`:

```python
class SnapshotTest(unittest.TestCase):
    ENV = {"GITHUB_REPOSITORY": "o/r", "PLAN_LABEL": "plan"}

    def test_list_payload_carries_head_and_update_time(self):
        payload = json.dumps([
            {"number": 12, "labels": [], "headRefOid": "abc123",
             "updatedAt": "2026-08-31T12:00:00Z"},
        ])
        run = runner(Result(0, payload))
        got = gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run)
        self.assertEqual(
            got,
            [gh.PRSnapshot(number=12, mode="code", head_oid="abc123",
                           updated_at="2026-08-31T12:00:00Z")],
        )

    def test_stage_one_asks_for_the_new_fields(self):
        run = runner(Result(0, "[]"))
        gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run)
        self.assertIn("number,labels,headRefOid,updatedAt", run.calls[0])

    def test_ids_selector_asks_for_the_new_fields(self):
        run = runner(Result(0, json.dumps(
            {"number": 12, "labels": [], "headRefOid": "abc123",
             "updatedAt": "2026-08-31T12:00:00Z"})))
        gh.enumerate_candidate_prs("ids", dict(self.ENV, PR_IDS="12"), run=run)
        self.assertIn("number,labels,headRefOid,updatedAt", run.calls[0])

    def test_missing_fields_become_empty_strings(self):
        # A gh that answers without them must not crash the cycle; an empty
        # head_oid simply never matches a recorded one, so the PR is reviewed.
        payload = json.dumps([{"number": 12, "labels": []}])
        run = runner(Result(0, payload))
        got = gh.enumerate_candidate_prs("all", dict(self.ENV, PR_ALL="1"), run=run)
        self.assertEqual(got[0].head_oid, "")
        self.assertEqual(got[0].updated_at, "")
```

Then update every existing assertion in `tests/test_gh.py` that compares a
result against `(12, "code")`-shaped tuples so it compares against
`gh.PRSnapshot(...)` instead. There are assertions at roughly lines 160, 183,
192, and 233; run the suite to find them all rather than trusting those numbers.

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p test_gh.py`
Expected: FAIL with `AttributeError: module 'gh' has no attribute 'PRSnapshot'`

- [ ] **Step 3: Write the implementation**

In `reviewer/gh.py`, replace the `Candidate` alias with the record:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class PRSnapshot:
    """One candidate PR as stage one sees it.

    head_oid and updated_at ride along in the call that was already being made
    for labels, so change detection costs no request on a cycle where nothing
    happened. A field gh did not return becomes "", which never matches a
    recorded fingerprint and so reviews the PR: the safe direction.
    """

    number: int
    mode: str
    head_oid: str
    updated_at: str
```

In `pr_modes`, build the record instead of the tuple:

```python
        out.append(PRSnapshot(
            number=number,
            mode="plan" if is_plan else "code",
            head_oid=entry.get("headRefOid") or "",
            updated_at=entry.get("updatedAt") or "",
        ))
```

Change the three `--json number,labels` field lists in
`enumerate_candidate_prs` (the `ids` arm's `gh pr view`, and the `all`,
`assignee` and `search` arms' `gh pr list`) to
`number,labels,headRefOid,updatedAt`. Update the return annotations from
`List[Candidate]` to `List[PRSnapshot]`.

In `reviewer/review_loop.py:main`, the candidate log line and the
`build_groups` call now read from snapshots:

```python
        if not snapshots:
            log(f"No candidate PRs for selector '{selector}'.")
        else:
            log(f"Candidate PRs ({selector}): "
                + " ".join(f"{s.number}:{s.mode}" for s in snapshots))
```

and

```python
        groups = supervisor.build_groups([(s.number, s.mode) for s in snapshots])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh`
Expected: PASS in full.

- [ ] **Step 5: Commit**

```bash
git add reviewer/gh.py reviewer/review_loop.py tests/test_gh.py
git commit -m "feat(gh): carry headRefOid and updatedAt out of the enumeration call"
```

---

### Task 3: Stage two, the comment lookup

**Files:**
- Modify: `reviewer/gh.py`
- Test: `tests/test_gh.py`

**Interfaces:**
- Consumes: `signals.Signal`, `signals.newest_unsigned` from Task 1; `gh.PRSnapshot` from Task 2.
- Produces: `pr_signal(snapshot: PRSnapshot, env, run=subprocess.run) -> Optional[Signal]` — the fingerprint, or `None` when the lookup failed. `None` is the fail-open value: the caller reviews the PR.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_gh.py`:

```python
class PRSignalTest(unittest.TestCase):
    ENV = {"GITHUB_REPOSITORY": "o/r"}
    SNAP = None  # set in setUp, because gh.PRSnapshot arrives in Task 2

    def setUp(self):
        self.SNAP = gh.PRSnapshot(
            number=12, mode="code", head_oid="abc123",
            updated_at="2026-08-31T12:00:00Z")

    def test_unsigned_conversation_comment_sets_newest_human(self):
        view = json.dumps({
            "comments": [
                {"createdAt": "2026-08-31T09:00:00Z", "body": "-claudebox (sage)"},
                {"createdAt": "2026-08-31T10:00:00Z", "body": "I disagree."},
            ],
            "reviews": [],
        })
        run = runner(Result(0, view), Result(0, "[]"))
        got = gh.pr_signal(self.SNAP, self.ENV, run=run)
        self.assertEqual(got.newest_human, "2026-08-31T10:00:00Z")
        self.assertEqual(got.head_oid, "abc123")
        self.assertEqual(got.mode, "code")

    def test_only_signed_comments_leave_newest_human_empty(self):
        view = json.dumps({
            "comments": [{"createdAt": "2026-08-31T10:00:00Z",
                          "body": "Nit here.\n\n-claudebox (red_team)"}],
            "reviews": [],
        })
        run = runner(Result(0, view), Result(0, "[]"))
        self.assertEqual(gh.pr_signal(self.SNAP, self.ENV, run=run).newest_human, "")

    def test_a_review_with_an_empty_body_counts_as_a_human_event(self):
        view = json.dumps({
            "comments": [],
            "reviews": [{"submittedAt": "2026-08-31T11:00:00Z", "body": ""}],
        })
        run = runner(Result(0, view), Result(0, "[]"))
        self.assertEqual(
            gh.pr_signal(self.SNAP, self.ENV, run=run).newest_human,
            "2026-08-31T11:00:00Z",
        )

    def test_inline_review_comments_are_read_over_the_rest_api(self):
        view = json.dumps({"comments": [], "reviews": []})
        inline = json.dumps([
            {"created_at": "2026-08-31T13:00:00Z", "body": "this line is wrong"},
        ])
        run = runner(Result(0, view), Result(0, inline))
        got = gh.pr_signal(self.SNAP, self.ENV, run=run)
        self.assertEqual(got.newest_human, "2026-08-31T13:00:00Z")
        self.assertEqual(run.calls[1], [
            "gh", "api", "repos/o/r/pulls/12/comments", "--paginate",
        ])

    def test_stage_two_never_asks_for_status_checks(self):
        run = runner(Result(0, json.dumps({"comments": [], "reviews": []})),
                     Result(0, "[]"))
        gh.pr_signal(self.SNAP, self.ENV, run=run)
        self.assertNotIn("statusCheckRollup", " ".join(run.calls[0]))

    def test_a_failed_view_returns_none(self):
        run = runner(Result(1, "", "gh: HTTP 502"), Result(0, "[]"))
        self.assertIsNone(gh.pr_signal(self.SNAP, self.ENV, run=run))

    def test_a_failed_inline_lookup_returns_none(self):
        view = json.dumps({"comments": [], "reviews": []})
        run = runner(Result(0, view), Result(1, "", "gh: HTTP 502"))
        self.assertIsNone(gh.pr_signal(self.SNAP, self.ENV, run=run))

    def test_a_gh_that_cannot_be_spawned_returns_none(self):
        def run(argv, **kwargs):
            raise OSError("no such file")
        self.assertIsNone(gh.pr_signal(self.SNAP, self.ENV, run=run))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p test_gh.py`
Expected: FAIL with `AttributeError: module 'gh' has no attribute 'pr_signal'`

- [ ] **Step 3: Write the implementation**

In `reviewer/gh.py`, add `import signals` at the top and:

```python
def pr_signal(snapshot: PRSnapshot, env, run=subprocess.run):
    """This PR's fingerprint, or None when GitHub could not be read.

    Two calls, made only for a PR whose updatedAt moved. `gh pr view` carries
    conversation comments and review submissions; inline diff comments are not
    in the reviews field, so they come from the REST endpoint. Neither asks for
    statusCheckRollup, which the privilege-minimized token cannot fetch.

    None is the fail-open value and the caller reviews the PR on it. That is
    the opposite of the ids selector's mode lookup, which skips on failure --
    there a wrong mode posts comments that cannot be taken back, and here the
    mode is already known from stage one, so the worst case is one redundant
    pass.
    """
    repo = env["GITHUB_REPOSITORY"]

    def gh_run(argv):
        try:
            return run(argv, capture_output=True, text=True, check=False)
        except OSError as exc:
            return subprocess.CompletedProcess(argv, 1, "", f"could not run gh: {exc}")

    view = _read_json(gh_run([
        "gh", "pr", "view", str(snapshot.number), "-R", repo,
        "--json", "comments,reviews",
    ]))
    if not isinstance(view, dict):
        return None

    inline = _read_json(gh_run([
        "gh", "api", f"repos/{repo}/pulls/{snapshot.number}/comments", "--paginate",
    ]))
    if not isinstance(inline, list):
        return None

    entries = []
    for c in view.get("comments") or []:
        if isinstance(c, dict):
            entries.append((c.get("createdAt"), c.get("body")))
    for r in view.get("reviews") or []:
        if isinstance(r, dict):
            entries.append((r.get("submittedAt"), r.get("body")))
    for c in inline:
        if isinstance(c, dict):
            entries.append((c.get("created_at"), c.get("body")))

    return signals.Signal(
        head_oid=snapshot.head_oid,
        mode=snapshot.mode,
        newest_human=signals.newest_unsigned(entries),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh`
Expected: PASS in full.

- [ ] **Step 5: Commit**

```bash
git add reviewer/gh.py tests/test_gh.py
git commit -m "feat(gh): read a PR's comment activity into a fingerprint"
```

---

### Task 4: The lookup cache, so an unchanged cycle stays one request

**Files:**
- Modify: `reviewer/signals.py`
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `Signal` and `PRSnapshot`.
- Produces: `class Tracker` with `polled: Dict[int, str]` and
  `signal_for(snapshot, fetch) -> Optional[Signal]`, where `fetch` is a callable
  taking a snapshot and returning `Optional[Signal]`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_signals.py`:

```python
class Snap:
    """Stands in for gh.PRSnapshot so this module's tests do not import gh."""

    def __init__(self, number=12, mode="code", head_oid="abc", updated_at="t1"):
        self.number = number
        self.mode = mode
        self.head_oid = head_oid
        self.updated_at = updated_at


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fetch(self, result):
        def f(snapshot):
            self.calls.append(snapshot.number)
            return result
        return f

    def test_first_sight_fetches(self):
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "")
        self.assertEqual(t.signal_for(Snap(), self.fetch(sig)), sig)
        self.assertEqual(self.calls, [12])

    def test_unchanged_update_time_does_not_fetch_again(self):
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "")
        snap = Snap(updated_at="t1")
        t.signal_for(snap, self.fetch(sig))
        again = t.signal_for(snap, self.fetch(sig))
        self.assertEqual(self.calls, [12])
        self.assertEqual(again, sig)

    def test_moved_update_time_fetches_again(self):
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "")
        t.signal_for(Snap(updated_at="t1"), self.fetch(sig))
        t.signal_for(Snap(updated_at="t2"), self.fetch(sig))
        self.assertEqual(self.calls, [12, 12])

    def test_a_failed_fetch_returns_none_and_still_records_the_poll(self):
        # Fail open once per updatedAt change, not once per cycle: a persistent
        # gh outage must not re-review every PR on every poll.
        t = signals.Tracker()
        snap = Snap(updated_at="t1")
        self.assertIsNone(t.signal_for(snap, self.fetch(None)))
        self.assertIsNone(t.signal_for(snap, self.fetch(None)))
        self.assertEqual(self.calls, [12])

    def test_a_failed_fetch_retries_once_the_update_time_moves(self):
        t = signals.Tracker()
        t.signal_for(Snap(updated_at="t1"), self.fetch(None))
        sig = signals.Signal("abc", "code", "")
        self.assertEqual(t.signal_for(Snap(updated_at="t2"), self.fetch(sig)), sig)
        self.assertEqual(self.calls, [12, 12])

    def test_a_snapshot_with_no_update_time_always_fetches(self):
        # "" would otherwise cache as a legitimate value and pin a PR forever.
        t = signals.Tracker()
        sig = signals.Signal("abc", "code", "")
        t.signal_for(Snap(updated_at=""), self.fetch(sig))
        t.signal_for(Snap(updated_at=""), self.fetch(sig))
        self.assertEqual(self.calls, [12, 12])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p test_signals.py`
Expected: FAIL with `AttributeError: module 'signals' has no attribute 'Tracker'`

- [ ] **Step 3: Write the implementation**

Append to `reviewer/signals.py`:

```python
class Tracker:
    """Decides when a second GitHub lookup is worth making.

    `updatedAt` moves on a push, a comment, a review and a label change, so a
    PR whose value has not moved cannot have changed in any way this design
    cares about. That is what keeps a cycle in which nothing happened at the
    cost it has today: one request.

    In memory, like the session map. It must NOT be persisted: `sessions` is in
    memory too, so a fingerprint that survived a restart would leave a fresh
    session that has never read the PR believing it had already reviewed it.
    """

    def __init__(self):
        # PR number -> the updatedAt its last lookup ran against.
        self.polled: Dict[int, str] = {}
        # PR number -> the fingerprint that lookup produced, or None when it
        # failed. Cached either way, so a persistent gh outage fails open once
        # per updatedAt change instead of once per cycle.
        self._cache: Dict[int, Optional[Signal]] = {}

    def signal_for(self, snapshot, fetch) -> Optional[Signal]:
        number = snapshot.number
        stamp = snapshot.updated_at
        # An empty stamp is not a value to cache against: gh did not tell us
        # when the PR last moved, so every poll has to look for itself.
        if stamp and self.polled.get(number) == stamp:
            cached = self._cache.get(number)
            if cached is None:
                return None
            # The mode can change without updatedAt reaching us as a new value
            # in the same cycle; keep the cached comment timestamp and take the
            # rest from the snapshot we were just handed.
            return Signal(
                head_oid=snapshot.head_oid,
                mode=snapshot.mode,
                newest_human=cached.newest_human,
            )
        result = fetch(snapshot)
        if stamp:
            self.polled[number] = stamp
        self._cache[number] = result
        return result
```

Add `Dict` to the `typing` import at the top of the module.

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh`
Expected: PASS in full.

- [ ] **Step 5: Commit**

```bash
git add reviewer/signals.py tests/test_signals.py
git commit -m "feat(signals): cache the stage-two lookup on updatedAt"
```

---

### Task 5: The gate in Supervisor

**Files:**
- Modify: `reviewer/review_loop.py` (`Supervisor.__init__`, `pairs_to_run`, `run_cycle`, `_record_success`)
- Test: `tests/test_review_loop.py`

**Interfaces:**
- Consumes: `signals.Signal`, `signals.change_reason`.
- Produces:
  - `Supervisor.reviewed: Dict[Pair, Signal]`
  - `Supervisor.pairs_to_run(group, signal: Optional[Signal] = None) -> List[Pair]`
  - `Supervisor.run_cycle(groups, signals: Optional[Dict[int, Signal]] = None) -> bool`
  - `Supervisor._record_success(pair, result, signal: Optional[Signal] = None)`

Every new parameter defaults to `None` and `None` means "run it", which is why
the roughly thirty existing `run_cycle` and `pairs_to_run` tests need no edits.

- [ ] **Step 1: Write the failing test**

Add `import gh` and `import signals` to the imports at the top of
`tests/test_review_loop.py`, then add this class. It uses the `supervisor()`,
`ok()` and `FakeSupervisor` helpers already defined in that module; code mode
there has personas `red_team` and `sage`.

```python
class ChangeGateTest(unittest.TestCase):
    SIG_A = signals.Signal(head_oid="aaaaaaa", mode="code", newest_human="")
    SIG_B = signals.Signal(head_oid="bbbbbbb", mode="code", newest_human="")
    RT = Pair(12, "code", "red_team")
    SG = Pair(12, "code", "sage")
    GROUP = review_loop.Group(12, "code", (RT, SG))

    def reviewed_and_sessioned(self, s):
        """A supervisor that has already reviewed both pairs at SIG_A."""
        for pair in (self.RT, self.SG):
            s.sessions[pair] = "S1"
            s.reviewed[pair] = self.SIG_A
        return s

    def test_no_signal_runs_every_pair(self):
        s = self.reviewed_and_sessioned(supervisor([]))
        self.assertEqual(s.pairs_to_run(self.GROUP, None), [self.RT, self.SG])

    def test_a_pair_with_no_session_runs_whatever_the_signal_says(self):
        s = supervisor([])
        s.reviewed[self.RT] = self.SIG_A
        s.reviewed[self.SG] = self.SIG_A
        self.assertEqual(s.pairs_to_run(self.GROUP, self.SIG_A), [self.RT, self.SG])

    def test_an_unchanged_signal_runs_nothing(self):
        s = self.reviewed_and_sessioned(supervisor([]))
        self.assertEqual(s.pairs_to_run(self.GROUP, self.SIG_A), [])

    def test_a_changed_signal_runs_every_pair(self):
        s = self.reviewed_and_sessioned(supervisor([]))
        self.assertEqual(s.pairs_to_run(self.GROUP, self.SIG_B), [self.RT, self.SG])

    def test_an_owed_pair_runs_even_when_nothing_changed(self):
        # The "barring retries due to errors" carve-out: an owed pair has no
        # result to preserve, so the gate must not hold it back.
        s = self.reviewed_and_sessioned(supervisor([]))
        s.owed = {self.RT}
        self.assertEqual(s.pairs_to_run(self.GROUP, self.SIG_A), [self.RT])

    def test_a_failed_pair_runs_next_time_because_its_session_was_dropped(self):
        s = self.reviewed_and_sessioned(supervisor([]))
        with contextlib.redirect_stdout(io.StringIO()):
            s._record_failure(self.RT)
        self.assertEqual(s.pairs_to_run(self.GROUP, self.SIG_A), [self.RT])

    def test_a_successful_pass_records_the_fingerprint(self):
        s = supervisor([])
        with contextlib.redirect_stdout(io.StringIO()):
            s._record_success(self.RT, ok("S9"), self.SIG_A)
        self.assertEqual(s.reviewed[self.RT], self.SIG_A)

    def test_a_pass_with_no_fingerprint_records_nothing(self):
        # Fail-open and gate-off both arrive here with signal=None. Recording
        # anything would claim knowledge we do not have.
        s = supervisor([])
        with contextlib.redirect_stdout(io.StringIO()):
            s._record_success(self.RT, ok("S9"), None)
        self.assertNotIn(self.RT, s.reviewed)

    def test_a_cycle_where_nothing_changed_runs_no_passes(self):
        s = self.reviewed_and_sessioned(supervisor([]))
        with contextlib.redirect_stdout(io.StringIO()):
            limited = s.run_cycle([self.GROUP], {12: self.SIG_A})
        self.assertEqual(s.attempted, [])
        self.assertFalse(limited)

    def test_a_cycle_where_the_head_moved_runs_both_pairs(self):
        s = self.reviewed_and_sessioned(supervisor([]))
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle([self.GROUP], {12: self.SIG_B})
        self.assertEqual(sorted(s.attempted), [self.RT, self.SG])

    def test_a_gateless_cycle_runs_both_pairs(self):
        # REVIEW_ON_CHANGE=0 reaches run_cycle as an empty signal map.
        s = self.reviewed_and_sessioned(supervisor([]))
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_cycle([self.GROUP], {})
        self.assertEqual(sorted(s.attempted), [self.RT, self.SG])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p test_review_loop.py`
Expected: FAIL with `TypeError: pairs_to_run() takes 2 positional arguments but 3 were given`

- [ ] **Step 3: Write the implementation**

In `Supervisor.__init__`, beside `self.owed`:

```python
        # The fingerprint each pair last successfully reviewed at. A pair whose
        # PR still fingerprints the same has nothing new to read, so it does not
        # run. In memory alongside the session map, and deliberately not
        # persisted: sessions are in memory too, so a fingerprint that outlived
        # a restart would leave a fresh session that has never read the PR
        # believing it had already reviewed it.
        self.reviewed: Dict[Pair, "signals.Signal"] = {}
```

`pairs_to_run` gains the signal and one clause:

```python
    def pairs_to_run(self, group: Group, signal=None) -> List[Pair]:
        """Which of this group's personas run this cycle.

        A group that owes something runs ONLY what it owes ... (existing
        docstring kept verbatim) ...

        A group that owes nothing is then filtered by the change gate. `signal`
        is None when the gate is off, and when stage two failed and we chose to
        fail open; both mean "run it". A pair with no session always runs, which
        is what makes first sight, a session dropped by _record_failure, and
        MAX_PASSES_PER_SESSION rotation work without knowing about the gate. A
        mode flip needs no case of its own either: it makes a different Pair,
        and that Pair has no session.
        """
        owed_here = [p for p in group.pairs if p in self.owed]
        if owed_here:
            return owed_here
        if signal is None:
            return list(group.pairs)
        return [
            p for p in group.pairs
            if self.sessions.get(p) is None or self.reviewed.get(p) != signal
        ]
```

`run_cycle` takes the map, passes each group its own signal, and logs the
reason:

```python
    def run_cycle(self, groups: List[Group], signals_by_pr=None) -> bool:
        ...
        signals_by_pr = signals_by_pr or {}
        ...
        for index, group in enumerate(ordered):
            signal = signals_by_pr.get(group.pr)
            to_run = self.pairs_to_run(group, signal)
            if not to_run:
                continue
            if signal is not None:
                prior = next(
                    (self.reviewed[p] for p in group.pairs if p in self.reviewed), None
                )
                reason = signals_mod.change_reason(prior, signal) or "no session"
                log(f"PR #{group.pr} [{group.mode}]: {reason}.")
            results = self.run_group(group, to_run)
            ...
```

Note the `continue`: a group with nothing to run is skipped before
`run_group`, which keeps the cut accounting from reasoning about an empty
result set at all. It does not affect `order_groups`, whose rotation key is
built from the group list rather than from what ran.

Then `_record_success(pair, result)` gains the signal:

```python
    def _record_success(self, pair: Pair, result, signal=None) -> None:
        if signal is not None:
            self.reviewed[pair] = signal
        ...existing body unchanged...
```

and its call site inside `run_cycle` becomes
`self._record_success(pair, result, signal)`.

Add `import signals as signals_mod` at the top of `review_loop.py`, matching
the `personas as personas_mod` / `prompts as prompts_mod` convention already
there. The parameter is named `signals_by_pr` so it cannot shadow the module.

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh`
Expected: PASS in full, including the existing `run_cycle` tests, which pass
no signal map and so behave exactly as before.

- [ ] **Step 5: Commit**

```bash
git add reviewer/review_loop.py tests/test_review_loop.py
git commit -m "feat(supervisor): skip a pair whose PR has not changed since its last pass"
```

---

### Task 6: Wire the cycle, the settle filter and the new configuration

**Files:**
- Modify: `reviewer/review_loop.py` (`main`, and its `preflight`/config block)
- Test: `tests/test_review_loop.py`

**Interfaces:**
- Consumes: everything from Tasks 1 through 5.
- Produces: no new public names. `main` now reads `SETTLE_SECONDS` and
  `REVIEW_ON_CHANGE`, holds a `signals.Tracker`, and defaults
  `REVIEW_INTERVAL_SECONDS` to 60.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_review_loop.py`:

```python
class SettleFilterTest(unittest.TestCase):
    STAMP = "2026-08-31T00:00:00Z"

    def snaps(self):
        return [gh.PRSnapshot(12, "code", "abc", self.STAMP)]

    def test_a_young_change_is_held_back(self):
        base = signals._epoch(self.STAMP)
        ready, settling = review_loop.partition_settling(self.snaps(), 30, base + 5.0)
        self.assertEqual(ready, [])
        self.assertEqual([x.number for x in settling], [12])

    def test_an_old_change_is_ready(self):
        base = signals._epoch(self.STAMP)
        ready, settling = review_loop.partition_settling(self.snaps(), 30, base + 60.0)
        self.assertEqual([x.number for x in ready], [12])
        self.assertEqual(settling, [])

    def test_settle_zero_holds_nothing_back(self):
        base = signals._epoch(self.STAMP)
        ready, _ = review_loop.partition_settling(self.snaps(), 0, base)
        self.assertEqual([x.number for x in ready], [12])

    def test_an_unreadable_timestamp_is_ready(self):
        # A gh that did not tell us when the PR moved must not pin it forever.
        snaps = [gh.PRSnapshot(12, "code", "abc", "")]
        ready, settling = review_loop.partition_settling(snaps, 30, 1000.0)
        self.assertEqual([x.number for x in ready], [12])
        self.assertEqual(settling, [])


class IntervalDefaultTest(unittest.TestCase):
    def test_the_poll_interval_defaults_to_sixty(self):
        self.assertEqual(
            review_loop._positive_int({}, "REVIEW_INTERVAL_SECONDS", 60), 60)

    def test_settle_seconds_rejects_a_typo(self):
        with self.assertRaises(ConfigError):
            review_loop._positive_int({"SETTLE_SECONDS": "thirty"},
                                      "SETTLE_SECONDS", 30)
```

Update the existing assertion at roughly `tests/test_review_loop.py:703` that
pins the `REVIEW_INTERVAL_SECONDS` default at 300; it becomes 60. The two tests
below it, at roughly 713 and 723, pass the default explicitly and only need the
literal changed to match.

- [ ] **Step 2: Run test to verify it fails**

Run: `./test-python.sh -p test_review_loop.py`
Expected: FAIL with `AttributeError: module 'review_loop' has no attribute 'partition_settling'`

- [ ] **Step 3: Write the implementation**

Add to `reviewer/review_loop.py`, beside the other module-level helpers:

```python
def partition_settling(snapshots, settle_seconds: int, now: float):
    """Split candidates into (ready, settling).

    A PR whose newest change is younger than the setting is left for the next
    poll, so a burst of pushes costs one review instead of one per push.
    Nothing about it is recorded, which is what makes the next poll reconsider
    it from scratch.
    """
    ready, settling = [], []
    for snap in snapshots:
        if signals_mod.is_settling(snap.updated_at, settle_seconds, now):
            settling.append(snap)
        else:
            ready.append(snap)
    return ready, settling
```

In `main`'s config block:

```python
        interval = _positive_int(env, "REVIEW_INTERVAL_SECONDS", 60)
        settle = _positive_int(env, "SETTLE_SECONDS", 30)
        gate_on = signals_mod.enabled(env)
```

Both calls sit inside the existing `try` that turns `ConfigError` into `die`,
so a typo in either fails at boot. That block runs after `--check` has already
returned, though, and `entrypoint.sh` runs `--check` before it buys a network
clone and up to 120 seconds of LiteLLM startup. So `preflight` validates the
value too, as its last line:

```python
    # Validated here as well as in main, because --check returns before main's
    # config block and a typo should cost a startup error rather than a clone
    # and a translator. Reads only the environment, which is what --check is
    # allowed to touch.
    _positive_int(env, "SETTLE_SECONDS", 30)
```

The return value is discarded; `main` reads it again for real.

Create the tracker beside the supervisor:

```python
    tracker = signals_mod.Tracker()
```

The cycle body, replacing the enumeration-to-`run_cycle` stretch:

```python
        try:
            snapshots = gh.enumerate_candidate_prs(selector, env)
        except ConfigError as exc:
            die(str(exc))

        if not snapshots:
            log(f"No candidate PRs for selector '{selector}'.")
        else:
            log(f"Candidate PRs ({selector}): "
                + " ".join(f"{s.number}:{s.mode}" for s in snapshots))

        signals_by_pr = {}
        if gate_on:
            snapshots, settling = partition_settling(snapshots, settle, time.time())
            if settling:
                log(f"Settling ({settle}s), left for the next poll: "
                    + " ".join(f"#{s.number}" for s in settling) + ".")
            for snap in snapshots:
                sig = tracker.signal_for(
                    snap, lambda s: gh.pr_signal(s, env))
                if sig is None:
                    log(f"WARN: could not read PR #{snap.number}'s comment "
                        "activity; reviewing it rather than skipping it.")
                else:
                    signals_by_pr[snap.number] = sig

        groups = supervisor.build_groups([(s.number, s.mode) for s in snapshots])
        skipped = [
            g for g in groups
            if not supervisor.pairs_to_run(g, signals_by_pr.get(g.pr))
        ]
        if skipped:
            log("Unchanged since their last review: "
                + " ".join(f"#{g.pr}" for g in skipped) + ".")

        limited = supervisor.run_cycle(groups, signals_by_pr)
```

The `skipped` computation calls `pairs_to_run` a second time, which is pure
and cheap, and it buys a log line an operator can read instead of inferring a
stall from missing comments.

Finally, the sleep line becomes `log(f"Polling again in {interval}s...")`, so
the log stops describing a cadence the loop no longer has.

- [ ] **Step 4: Run test to verify it passes**

Run: `./test-python.sh`
Expected: PASS in full.

- [ ] **Step 5: Commit**

```bash
git add reviewer/review_loop.py tests/test_review_loop.py
git commit -m "feat(loop): poll for change, settle a burst, review only what moved"
```

---

### Task 7: Entrypoint and operator configuration

**Files:**
- Modify: `entrypoint.sh:73-82` (the `strip_surrounding_quotes` list) and `entrypoint.sh:217` (the interval default)
- Modify: `.env.example`

**Interfaces:**
- Consumes: the variable names from Task 6.
- Produces: nothing the Python reads differently. The shell only repairs quotes and sets the interval default.

- [ ] **Step 1: Verify the shell still parses**

There is no unit test for the entrypoint's defaults block; the check is
`bash -n` plus the provider suite. Run `bash -n entrypoint.sh` first to have a
clean baseline.

- [ ] **Step 2: Make the edits**

In the `strip_surrounding_quotes` call, add the two new names to the line that
already carries `LIMIT_BACKOFF_SECONDS MAX_CYCLES`:

```bash
  PERSONAS PLAN_PERSONAS PERSONA_DIR PLAN_LABEL LIMIT_BACKOFF_SECONDS MAX_CYCLES \
  MAX_CONCURRENT_PASSES REPO_PATH SETTLE_SECONDS REVIEW_ON_CHANGE
```

Both belong on that list for the reason the comment under it gives: each is a
number or a flag drawn from a fixed vocabulary, where a leading or trailing
quote is always operator error. Neither is free text, so neither gets the
prompt vars' exemption.

Change the interval default and give it the comment the new meaning deserves:

```bash
# How long to wait between polls. This is a POLL interval, not a review
# interval: a cycle that finds nothing changed costs one `gh pr list`, so
# polling often is cheap and the setting is what bounds the delay between
# somebody pushing and the first review of that push. Validation lives in the
# supervisor, with SETTLE_SECONDS and the rest of the numeric settings.
REVIEW_INTERVAL_SECONDS="${REVIEW_INTERVAL_SECONDS:-60}"
```

- [ ] **Step 3: Document both new variables in `.env.example`**

Add near `REVIEW_INTERVAL_SECONDS` at line 151, and do not quote either value,
per the rule at the top of that file:

```bash
# How long to wait after a PR changes before reviewing it, so a burst of
# pushes or comments becomes one review instead of one per event. A PR whose
# newest change is younger than this is left for the next poll. 0 disables it.
# SETTLE_SECONDS=30

# Re-review a PR only when something happened to it: a new head commit, or a
# comment somebody other than claudebox wrote. On by default. Set it to 0 to
# review every candidate on every cycle, which is what the loop did before
# change detection existed. If you turn it off, raise REVIEW_INTERVAL_SECONDS
# back toward 300: the low default assumes most cycles review nothing.
# REVIEW_ON_CHANGE=1
```

Update the existing `REVIEW_INTERVAL_SECONDS=300` line at 151 to `60` and say
in its comment that it is now a poll interval. Check lines 211, 241, 249 and
308, which each describe `REVIEW_INTERVAL_SECONDS` as the gap between reviews,
and reword them to say polls.

- [ ] **Step 4: Verify**

Run: `bash -n entrypoint.sh && ./test-providers.sh`
Expected: both pass. The provider suite exercises one cycle against a
first-sighting PR, so the gate lets everything through and its assertions are
unaffected. If it fails, the cause is the `gh` stub not answering the new
`--json` field list; that is Task 9's job, so stop and do Task 9 first.

- [ ] **Step 5: Commit**

```bash
git add entrypoint.sh .env.example
git commit -m "feat(entrypoint): SETTLE_SECONDS, REVIEW_ON_CHANGE, and a 60s poll default"
```

---

### Task 8: Make the signature load-bearing in the persona contract

**Files:**
- Modify: `personas/code/_shared.md`
- Modify: `personas/plan/_shared.md`

**Interfaces:**
- Consumes: `signals.MARKER`, whose value is `-claudebox`.
- Produces: nothing in code. This is the half of the design that makes the
  marker reliable enough to key on.

`tools/import-advocate-personas.py` writes persona bodies and does not touch
`_shared.md`, so an edit here is not at risk from a re-import.

- [ ] **Step 1: Edit both files**

In `personas/code/_shared.md`, under "How to report what you find", the
existing text says to sign each comment `-claudebox ({{PERSONA}})`. Add
immediately after that sentence:

```markdown
Signing is not a courtesy. claudebox decides whether a pull request needs
another look by reading its comments, and a comment without that signature is
read as a human's, which costs the pull request another full round of reviews.
Sign every comment you post.
```

Make the identical addition to `personas/plan/_shared.md`, whose surrounding
text is the same.

- [ ] **Step 2: Verify nothing else moved**

Run: `./test-personas.sh`
Expected: PASS. The persona suite composes `_shared.md` into every persona
prompt but does not pin its text, so adding a paragraph is invisible to it. If
it fails, the cause is the `gh` stub, which Task 9 fixes.

Run: `./test-python.sh -p test_prompts.py`
Expected: PASS. The fixtures under `tests/fixtures/` pin the four assembled
task prompts, and `_shared.md` is a system prompt, so it is not in them.

- [ ] **Step 3: Commit**

```bash
git add personas/code/_shared.md personas/plan/_shared.md
git commit -m "docs(personas): say why the claudebox signature is load-bearing"
```

---

### Task 9: Teach the acceptance suites' gh stubs about the new fields

**Files:**
- Modify: `test-personas.sh` (the `gh` stub at line 66, and the case baseline)
- Modify: `test-providers.sh` (the `gh` stub at line 47)

**Interfaces:**
- Consumes: the field list from Task 2 and the calls from Task 3.
- Produces: nothing importable. This keeps the two bash suites honest.

- [ ] **Step 1: Extend the `test-providers.sh` stub**

That suite runs one cycle with `PR_IDS`, so every PR is a first sighting and
the gate lets it through. The stub needs the new fields on the stage-one
answer and needs to answer stage two at all. Replace its `pr view` arm:

```sh
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  n="$3"
  # Stage two asks for comments and reviews; stage one asks for labels and the
  # head. Dispatch on the --json argument, which is the last one.
  for a in "$@"; do last="$a"; done
  case "$last" in
    *comments*) printf '{"comments":[],"reviews":[]}\n'; exit 0 ;;
  esac
  printf '{"number":%s,"labels":[],"headRefOid":"abc123","updatedAt":"2020-01-01T00:00:00Z"}\n' "$n"
  exit 0
fi
if [ "$1" = "api" ]; then printf '[]\n'; exit 0; fi
```

The fixed 2020 timestamp is deliberate: it is far older than any settle
window, so the suite never waits.

- [ ] **Step 2: Run the provider suite**

Run: `./test-providers.sh`
Expected: PASS, with the same case count as before the change.

- [ ] **Step 3: Extend the `test-personas.sh` stub**

This one is consequential. That suite runs `MAX_CYCLES=2` specifically to
produce a resumed invocation, and the resumed invocation is where the
persona-survives-`--resume` assertion lives. Under the gate, cycle two reviews
nothing unless the PR changed, so the stub must make it change.

Replace the whole `pr view` arm of `test-personas.sh`'s `gh` stub with this,
keeping the comment block above the stub and extending it to mention the two
new variables:

```sh
cat >"$BIN/gh" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$HOME/gh-argv"
if [ "$1" = "api" ]; then printf '[]\n'; exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  n="$3"
  # Stage two asks for comments and reviews; stage one asks for labels and the
  # head. Dispatch on the --json value, which is the last argument.
  for a in "$@"; do last="$a"; done
  case "$last" in
    *comments*) printf '{"comments":[],"reviews":[]}\n'; exit 0 ;;
  esac
  # The change gate reviews a PR only when it moved. Cycle one is a first
  # sighting and always runs; cycle two needs a moved head, or the resumed
  # invocation this whole suite exists to assert would never happen. Passes are
  # counted by the claude stub, one line per invocation, which is the same
  # counter STUB_PLAN_AFTER uses. STUB_FREEZE_HEAD=1 pins it instead, which is
  # how a case asserts the gate actually gates.
  oid=aaaaaaa; when=2020-01-01T00:00:00Z
  if [ -z "${STUB_FREEZE_HEAD:-}" ]; then
    c=$(wc -l <"$HOME/calls" 2>/dev/null || echo 0)
    if [ "$c" -gt 0 ]; then oid=bbbbbbb; when=2020-01-02T00:00:00Z; fi
  fi
  meta="\"headRefOid\":\"$oid\",\"updatedAt\":\"$when\""
  case ",${STUB_LABEL_FAIL:-}," in
    *",$n,"*) echo "gh: could not resolve to a PullRequest" >&2; exit 1 ;;
  esac
  case ",${STUB_LABEL_GARBAGE:-}," in
    *",$n,"*) exit 0 ;;
  esac
  case ",${STUB_LABEL_NULL:-}," in
    *",$n,"*) echo "null"; exit 0 ;;
  esac
  if [ -n "${STUB_PLAN_AFTER:-}" ]; then
    c=$(cat "$HOME/calls" 2>/dev/null || echo 0)
    if [ "$c" -ge "$STUB_PLAN_AFTER" ]; then
      printf '{"number":%s,"labels":[{"name":"%s"}],%s}\n' \
        "$n" "${STUB_PLAN_LABEL:-plan}" "$meta"
      exit 0
    fi
  fi
  case ",${STUB_PLAN_PRS:-}," in
    *",$n,"*) printf '{"number":%s,"labels":[{"name":"%s"}],%s}\n' \
                "$n" "${STUB_PLAN_LABEL:-plan}" "$meta" ;;
    *)        printf '{"number":%s,"labels":[],%s}\n' "$n" "$meta" ;;
  esac
  exit 0
fi
exit 0
STUB
```

Three things about it are deliberate. The `api` arm is first, so stage two's
inline-comment call never falls through to the `pr view` logic. The
`STUB_LABEL_*` arms are untouched, because they model a broken stage-one
lookup, which is still a skip-with-a-WARN and has nothing to do with the gate.
The `2020` timestamps are far older than any settle window, so no case waits.

The `STUB_PLAN_AFTER` case deserves a second look while you are here: it flips
a PR into plan mode partway through, which changes its mode and therefore its
`Pair`, and a new `Pair` has no session, so the gate lets it through on
identity rather than on the head having moved. Confirm that case still passes
for that reason rather than by accident.

- [ ] **Step 4: Run the persona suite**

Run: `./test-personas.sh`
Expected: PASS with the same case count. The case that matters is the one
asserting a resumed invocation carries `--append-system-prompt`; confirm it is
still reached rather than skipped, since a gate bug would show up as that case
finding only one invocation where it expects two.

- [ ] **Step 5: Add a case proving the gate actually gates**

The suites' whole job is proving the wiring, and nothing so far proves that a
real end-to-end run skips an unchanged PR. Add a case to `test-personas.sh`
modeled on the existing two-cycle single-PR cases, setting `STUB_FREEZE_HEAD=1`
in its environment so the stub reports the same head and update time in both
cycles.

The assertion: with `MAX_CYCLES=2`, `PR_IDS=12` and `PERSONAS=red_team`, the
run produces exactly one dump file (`$HOME/dump.1`) rather than two. Cycle one
is a first sighting and reviews; cycle two finds an identical fingerprint and a
recorded session, so it runs nothing. Assert on the dump count the way the
neighbouring cases do, and assert the log carries the line
`Unchanged since their last review: #12.`

Add a second case identical to it except for `REVIEW_ON_CHANGE=0`, keeping
`STUB_FREEZE_HEAD=1` so the PR still never moves, and assert two dumps. The
contrast between the two cases is the whole assertion: same frozen PR, gate on
gives one review, gate off gives two. That is the case that fails if the escape
hatch ever stops working.

- [ ] **Step 6: Commit**

```bash
git add test-personas.sh test-providers.sh
git commit -m "test: teach the gh stubs the change-detection fields and calls"
```

---

### Task 10: Documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `HISTORY.md`

**Interfaces:**
- Consumes: the finished behavior.
- Produces: nothing importable.

- [ ] **Step 1: Update `CLAUDE.md`**

Three edits, in the places the existing text is now wrong:

1. The `reviewer/` module list currently says "Six flat modules". It is seven
   now; add `signals.py` with a one-line description: the per-PR fingerprint,
   the signature test, the settle arithmetic, and the lookup cache.
2. The "A cycle is" sentence describes `check_litellm` then `git fetch` then
   enumerate then walk then sleep. Insert the change gate between enumerate and
   walk, and say that the sleep is a poll interval now.
3. Add a subsection under "Two pieces working together", after the persona
   sections, covering: the four triggers, why the marker and not the author
   login, the two stages and why an unchanged cycle still costs one request,
   why `updatedAt` is outside the fingerprint, why the state is not persisted,
   how `owed` short-circuits the gate, and the fail-open-once-per-`updatedAt`
   rule.

Add to "Gotchas when editing":

```markdown
- The `-claudebox` signature in `personas/*/_shared.md` is load-bearing, not
  cosmetic: it is the only thing distinguishing claudebox's own comments from a
  human's, and an unsigned comment costs the PR another review round. Author
  login is deliberately not consulted, because claudebox is commonly run under
  the operator's own PAT.
- `signals.Tracker` and `Supervisor.reviewed` must stay in memory. Persisting a
  fingerprint without persisting the session map would leave a fresh session
  that has never read the PR believing it had already reviewed it.
- `test-personas.sh`'s `gh` stub advances `headRefOid` between its two cycles on
  purpose. Freeze it and the suite's central assertion, that a resumed pass
  still carries its persona, silently stops being reached.
```

Update the "Configuration" section with `SETTLE_SECONDS`, `REVIEW_ON_CHANGE`,
and the changed meaning and default of `REVIEW_INTERVAL_SECONDS`.

- [ ] **Step 2: Update `README.md`**

Add a short paragraph where the loop's cadence is described, saying that a PR
nobody has touched is not re-reviewed, that a new head commit or a comment from
anybody other than claudebox is what brings it back, and that
`SETTLE_SECONDS` batches a burst.

- [ ] **Step 3: Update `HISTORY.md`**

Follow the file's existing format and add the entry for this change.

- [ ] **Step 4: Verify the whole suite**

Run: `./test-python.sh && ./test-providers.sh && ./test-personas.sh && ./test-shim.sh`
Expected: all four pass.

Run: `python3 -m py_compile reviewer/*.py && bash -n entrypoint.sh && bash -n claudebox.sh`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md README.md HISTORY.md
git commit -m "docs: describe change-driven re-review and its knobs"
```
