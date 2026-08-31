# Change-driven re-review

Date: 2026-08-31
Status: approved, ready for an implementation plan

## Problem

The review loop re-reviews every candidate PR on every cycle. A PR that
nobody has touched since the last pass gets a full persona fan-out anyway,
and each of those passes spends provider tokens to conclude that nothing
has changed. With the persona multiplier the waste scales as
(PRs x personas) per cycle rather than per event.

The loop should re-review a PR only when something happened to it: a new
head commit, or a comment somebody other than claudebox wrote. Retries
after an error are exempt, because a failed pass has no result to
preserve.

## What counts as a change

Four triggers, decided per PR:

1. The head commit moved (`headRefOid`).
2. A new conversation comment whose body does not carry the claudebox
   signature.
3. A new inline diff comment or submitted review, same signature test. A
   review submitted with an empty body counts, because an approve or a
   request-changes with no prose is still somebody acting on the PR.
4. The PR's review mode changed, meaning `PLAN_LABEL` was added or
   removed.

Edits to the title, body, or base branch do not count. They move
`updatedAt` and so cost one extra lookup, and they stop there.

## Identifying claudebox's own comments

A comment is claudebox's when its body contains the marker `-claudebox`,
matched case-insensitively as a substring. Author login is not consulted.
That choice makes the marker load-bearing rather than decorative, so both
`personas/code/_shared.md` and `personas/plan/_shared.md` gain a sentence
saying that an unsigned comment reads as a human's and costs the PR
another review round.

Two holes follow from the choice and are accepted:

* A human who quotes a claudebox signature while replying is invisible to
  the trigger, and that PR waits for its next real change.
* A persona that forgets to sign buys one extra round of passes. This
  self-limits, because the followup prompt already tells a resumed
  persona to post only findings it has not already raised, so the round
  that follows an unsigned comment usually posts nothing and the loop
  settles.

The alternative of matching the token's own login was rejected: claudebox
is often run under the operator's own PAT, where every comment the
operator writes would be classified as claudebox's and the comment
trigger would never fire for the person most likely to use it.

## Two-stage polling

Stage one is the call `enumerate_candidate_prs` already makes. Its
`--json` field list widens from `number,labels` to
`number,labels,headRefOid,updatedAt`, and it returns a `PRSnapshot`
record in place of the `(number, mode)` tuple. No selector gains a
request, including `ids`, which already does a per-PR `gh pr view`.

Stage two runs only for a PR whose `updatedAt` differs from the value
recorded at its last stage-two lookup. It is a new function in `gh.py`
and makes two calls:

* `gh pr view N --json comments,reviews` for conversation comments and
  review submissions.
* `gh api repos/{repo}/pulls/N/comments` for inline diff comments, which
  `gh pr view --json reviews` does not carry.

Both stay inside what the privilege-minimized token can do. Neither
touches `statusCheckRollup`, the field `GH_STANZA` exists to route
around.

The cost of a cycle in which nothing happened is therefore exactly the
cost of a cycle today: one request.

## State

Two dicts on `Supervisor`, in memory beside `sessions`:

* `polled: Dict[int, str]` maps PR number to the `updatedAt` its last
  stage-two lookup ran against. This is an API-cost cache with no bearing
  on correctness.
* `reviewed: Dict[Pair, Signal]` maps a pair to the fingerprint it last
  successfully reviewed at.

`Signal` is a frozen dataclass holding `head_oid`, `mode`, and
`newest_human`, the timestamp of the newest unsigned comment, or the
empty string when there are none. `updatedAt` is deliberately not part of
the fingerprint. It moves for edits the design decided to ignore, and
including it would make a typo fix in a PR body cost a persona fan-out.

Neither dict is persisted. That is not a deferral, it is required:
`sessions` lives only in memory, so a persisted fingerprint would survive
a restart into a process whose sessions did not, leaving a fresh session
that has never read the PR believing it had already reviewed it. The
existing consequence stands, that a container restart re-reviews each
pair once.

## The gate

`Supervisor.pairs_to_run` keeps its current shape and grows one clause:

```python
owed_here = [p for p in group.pairs if p in self.owed]
if owed_here:
    return owed_here                      # unchanged narrowing
return [p for p in group.pairs
        if self.sessions.get(p) is None or self.reviewed.get(p) != signal]
```

The owed path is untouched, which is what exempts retries after an error.
A pair with no session always runs, so first sight, a session dropped by
`_record_failure`, and `MAX_PASSES_PER_SESSION` rotation all keep working
without knowing this feature exists. A mode flip needs no special case
either: it produces a different `Pair`, which has no session.

A group where nothing needs review yields an empty `to_run`. `run_group`
already returns an empty result for that, and the cut accounting already
reads it as neither a success nor a failure, so `run_cycle` needs no
change on that path.

`reviewed[pair]` is written in `_record_success`, from the fingerprint
snapshotted when the cycle enumerated. A comment that lands while the
pass is running therefore triggers one more round even if the reviewer
happened to read it. That is the safe direction and it is bounded.

## Settle delay

`SETTLE_SECONDS`, default 30, `0` to disable. A PR whose `updatedAt` is
younger than the setting is dropped from this cycle's candidate list with
one log line, and nothing is recorded as polled for it, so the next poll
reconsiders it from scratch.

A negative age, which means the container clock is behind GitHub's, is
clamped to zero and the PR runs. Skew costs the batching and never the
review.

## Cadence and configuration

`REVIEW_INTERVAL_SECONDS` becomes a poll interval. Its default drops from
300 to 60, in both places it is written: `entrypoint.sh` and
`review_loop.main`. A cycle that finds nothing changed is cheap enough
that polling more often is the right trade, and the interval is now what
bounds latency from a push to the first review.

New variables, both read by the supervisor and both added to
`strip_surrounding_quotes` in `entrypoint.sh`:

* `SETTLE_SECONDS`, default 30, non-negative integer, validated during
  `--check` so a typo fails at boot.
* `REVIEW_ON_CHANGE`, default on. Setting it to `0` restores
  fixed-interval behavior, which is the escape hatch for an operator who
  suspects the gate is wrong.

## Failure behavior

A stage-one failure degrades to an empty candidate list, which is the
existing path and is unchanged.

A stage-two failure fails open: the PR is reviewed, with a WARN naming
it. The `ids` selector's mode lookup skips on failure because a
wrong-mode review posts real comments that cannot be taken back. Here the
mode already came from stage one, so the worst case is a redundant pass.

To keep a persistent `gh` outage from re-reviewing everything on every
poll, a failed stage two still records `polled[pr]`. The PR then fails
open once per `updatedAt` change rather than once per cycle.

## Logging

The `Candidate PRs` line keeps its shape. Three companions join it:

* what was skipped as unchanged,
* what is settling and how long it has left,
* for each PR that runs, the reason: `new head 3f2a1b0`, `2 new
  comments`, `mode changed to plan`, `no session`.

An operator asking why a review happened should be able to read the
answer instead of inferring it from which comments appeared.

## Testing

New cases under `tests/`, run by `test-python.sh`:

* fingerprint equality and inequality, one case per trigger kind
* marker matching, including case, and an unsigned review with an empty
  body
* the settle boundary, including a negative age
* an owed pair running while its fingerprint is unchanged
* a pair with no session running while its fingerprint is unchanged
* stage two not called when `updatedAt` is unchanged
* stage-two failure failing open exactly once per `updatedAt` change

`test-personas.sh` needs a change to its `gh` stub. The suite runs two
cycles specifically to produce a resumed invocation, and under the gate
cycle two runs nothing unless the PR changed. The stub must advance
`headRefOid` and `updatedAt` between cycles.

`test-providers.sh` runs one cycle against a first-sighting PR, so its
behavior is unchanged. Its `gh` stub still needs the new fields in its
`--json` output.

## Documentation

`CLAUDE.md` gains a section describing the trigger set, the two stages,
the fingerprint, and the marker's new load-bearing status, and its
description of a cycle is updated. `.env.example` documents
`SETTLE_SECONDS` and `REVIEW_ON_CHANGE` and the changed meaning of
`REVIEW_INTERVAL_SECONDS`. `README.md` gains a short note on why a PR
that nobody touched is not re-reviewed.
