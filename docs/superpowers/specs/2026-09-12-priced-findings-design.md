# Priced findings

Date: 2026-09-12
Status: implemented on feat/change-driven-review

## Problem

A persona that posts a wrong finding pays nothing for it. The author pays:
they read it, work out why it does not apply, and write the reply. The only
brake today is one sentence in the shared contract, "do not manufacture
findings to appear thorough", and a sentence is not a price.

The false positives that cost the most share a shape. They ask for code
that defends against something nobody has done: a caller that does not
exist, a change another engineer might make elsewhere later, a
configuration axis nobody has asked for. They are nitpicks dressed as
rigour. And they come back round after round, because the followup prompt
on round twenty is the followup prompt on round two, so a PR that has
survived three rounds is reviewed with the same latitude as one nobody
has looked at.

Three things change. A finding has a price the persona pays before it
posts. The price rises with the number of rounds the PR has survived. And
Sage, whose job is already to find unnecessary complexity, gets a second
job: rebut a sibling that asks for it.

All of it is code mode only. Plan mode already tells its personas that a
gap in code the plan has not written is not a finding, and the failure
modes here are code-shaped.

## The price, in `personas/code/_shared.md`

The shared contract is the right home because it rides in the system
prompt on every pass, resumed or not, and reaches an operator who has
overridden the task prompt. The task-prompt defaults and fixtures do not
change.

The contract gains a section, "What a finding costs", with four rules:

1. **Demonstrate it.** A finding names the concrete input, mutation, or
   attack that shows the defect in this diff, and says what to do about
   it. A finding the persona cannot demonstrate against the change in
   front of it is not posted. Code that only goes wrong if someone later
   changes something elsewhere fails this rule by construction: the
   demonstration would have to include the change nobody has made.
2. **Refute it.** Before posting, argue the author's side: why the code is
   fine as written, what the persona has missed, what the surrounding code
   already guarantees. Post only if that argument fails, and say in the
   comment which refutation was tried and why it did not hold. A comment
   that cannot name a refutation attempt is not posted.
3. **Tag it.** The comment's first line is one of `blocking`,
   `should-fix`, or `nit`, followed by the finding's one-line summary.
   `blocking` means the change is wrong as merged: a defect a user, an
   attacker, or the next deploy would hit. `should-fix` means the change
   works and a named case will break it in a way the demonstration shows.
   `nit` is everything else, and a nit is never posted. The tag is
   self-assigned, so on its own it is a soft price; the round ladder
   below is what makes it bite.
4. **Named non-findings.** These are not findings at any severity, and a
   persona that catches itself drafting one stops:
   - a defence against a caller, input, or code path that does not exist
     in the repository as it stands;
   - a guard against a change another engineer might make in another
     part of the code later;
   - an abstraction, indirection, configuration option, or extension
     point for a case nobody has;
   - style, naming, ordering, formatting, and comment wording;
   - a request to handle an error the surrounding code already cannot
     produce.

The contract keeps "if the change is solid and you have no findings, say
so and post nothing". A persona that has costed out every candidate and
found nothing above `nit` is in exactly that position.

Sage's own body says "if something exists for a hypothetical future, it's
a finding". That is not in tension with rule 4: Sage flags code that
over-guards, and rule 4 stops a reviewer demanding that the author add
guards. Both point at the same failure from opposite sides.

## The ladder

A **round** is one completed pass of one `(pr, mode, persona)` pair. Round
N of a pair means N-1 of its passes have completed, whatever sessions
those passes ran in. A pair's round survives session rotation and
survives a failed pass, because both of those are about the session and
the round is about the PR's history with this persona.

| Round | Floor | Scope |
|---|---|---|
| 1 | `should-fix` | the whole PR |
| 2 | `should-fix` | commits since the persona's last review |
| 3 and later | `blocking` | commits since the persona's last review; the rest is presumed sound |

The floor is the lowest tag a persona may post that round. The scope is
what the persona is asked to read closely: on round 2 and later, the
diff since it last looked, not the whole PR again. A round-3 persona that
finds a `should-fix` in the new commits does not post it.

"Since the persona's last review" is a procedure the persona runs, not a
value the loop supplies. The persona's own earlier comments are signed
`-claudebox (<its label>)` and carry timestamps, and `gh pr view --json
commits` lists the branch's commits with dates, so the persona finds its
newest comment and reads the commits after it. A pass in a fresh session
at round 3, after a rotation or a failure, has no memory of its last
review and follows the same procedure. This is why the loop does not hand
the persona the last-reviewed head: `Supervisor.reviewed` holds one only
while the change gate is on, and the procedure works either way.

The loop's part is a **round line**, appended to the persona's system
prompt on every pass, after the shared contract. It states the round
number, the floor, and the scope in two or three sentences, and on round
3 and later it says outright that the PR has already survived review and
a persona finding nothing new is the expected outcome. The text lives in
`reviewer/prompts.py` as `round_stanza(mode, round) -> str`, returning
the empty string for plan mode so a plan pass's system prompt is
byte-identical to today's. The three rungs are three constants; there is
no environment variable to move them, because the operator who wants a
different ladder overrides `REVIEW_PROMPT` and `FOLLOWUP_PROMPT` and
writes their own.

The round line goes in the system prompt rather than the task prompt for
the same reason the persona does: it must be re-passed on `--resume`, and
it must reach an operator override without editing it. `build_argv`
already re-passes `--append-system-prompt` on every invocation, so the
only change is what that argument holds.

## Sage's second job

Sage's code-tree body gains a mandate: read the comments other personas
posted on this pull request since its most recent commit, and where one
of them asks for a defence against a hypothetical, a guard for a caller
that does not exist, or an abstraction for a case nobody has, post a
signed comment that names the sibling's finding, says the defence guards
against a change nobody has made, and tells the author not to act on it.
The mandate covers those classes and nothing else. A sibling finding Sage
merely disagrees with is not in scope, and Sage's own findings against
the code are unchanged.

The shared contract's blindness paragraph stays as written for every
other persona and gains one sentence: Sage reads sibling comments for the
purpose of that mandate, and for no other; it does not defer to them and
does not treat them as coverage.

### Phases

Sage cannot rebut what has not been posted yet. A PR's personas run
concurrently behind one barrier, so on round 1 Sage runs beside the
persona it is meant to police and sees nothing, and a sibling's signed
comment deliberately does not re-trigger the change gate, so on a PR
where nobody pushes or replies the rebuttal never lands.

So a persona has a **phase**, from a `phase:` key in its frontmatter,
default 1, and the only other accepted value is 2. Anything else is a
`ConfigError` at startup, naming the file. `Persona` carries it, and
`personas.resolve` reads it the way it reads `label`. Sage's code-tree
body sets `phase: 2`. Nothing else does, and the plan tree is untouched.

`run_group` partitions the pairs it is given by phase and runs phase 1
under a pool as it does today, waits, then runs phase 2 under a second
pool and waits again. It is still one group: one `Group` record, one
result dict, one cut, one debt, and the limit and failure evaluation at
the barrier happens once, after both phases, exactly where it happens
now. `workers_for` is computed per phase from the pairs in that phase.
With `MAX_CONCURRENT_PASSES=1` the phases are one worker each and the
only visible effect is that Sage runs last, which is what the ordinal
assertions in `test-personas.sh` will pin.

If any phase-1 result is a usage limit, phase 2 does not start. The
phase-2 pairs then have no result, which is the same state a pool refusal
leaves a pair in, and the existing accounting owes them to the next
cycle. Starting Sage against a provider that has just refused its
siblings would spend the exhausted resource to rebut findings that were
never posted.

A group narrowed by `owed` or by the change gate may leave Sage as the
only phase-2 pair, or leave it out entirely. Neither needs a case: a
phase with no pairs is skipped, and a phase-2 pass that finds no sibling
comments since the head commit has nothing to rebut and posts nothing.

Selection order is unchanged. `PERSONAS=sage,red_team` still logs
`code personas: sage red_team`; phase decides run order, the selector
decides the log line and the pair list.

### The importer

`tools/import-advocate-personas.py` writes `label:` and `success:` and a
body into both trees. A re-run clobbers Sage's mandate and, worse, drops
`phase: 2`, which would silently demote Sage to phase 1 and disarm the
rebuttal with no error anywhere. The existing rule, that a re-run
produces a diff that gets reviewed, covers the body. For the frontmatter
key a unit test pins the shipped `personas/code/sage.md` to `phase: 2`
and every other shipped persona to phase 1, so the demotion fails the
suite rather than the review.

## Bookkeeping

`Supervisor.rounds: Dict[Pair, int]`, in memory beside `sessions` and
`passes_done`. Incremented in `_record_success` only. Not touched by
`_record_failure`, by `MAX_PASSES_PER_SESSION` rotation, or by a limit.
The round a pass runs at is `rounds.get(pair, 0) + 1`, computed in
`_dispatch` and handed to `_run_one`, which composes the system prompt
as `persona_prompt + "\n" + round_stanza(mode, round)`.

An owed pair re-runs at the same round it was cut at, because the cut
pass did not complete. A pair that failed re-runs at the same round for
the same reason. Both are right: no round was survived.

A restart is round 1 for every pair, matching the existing rule that a
restart re-reviews each pair once. A persisted round without a persisted
session would tell a session that has never read the PR that the PR has
survived three of its reviews, so `rounds` stays in memory for the same
reason `reviewed` does.

The log line `_record_success` already writes gains the round: `review
complete (session S, pass P, round R)`.

## What does not change

- The four task-prompt defaults and their fixtures. Nothing here touches
  `REVIEW_PROMPT`, `FOLLOWUP_PROMPT`, or the plan pair.
- The verbatim-operator-prompt guarantee, and its one existing exception
  for `WORKTREE_STANZA`.
- The change gate, `owed`, `cut_group`, `debt_for`, `_gate_holds`.
- The plan tree, and plan mode's prompts and system prompts, byte for
  byte.
- `.env.example`: there are no new environment variables.

## Testing

`tests/test_prompts.py`:
- `round_stanza("code", n)` for n in 1, 2, 3, 7: each names its floor and
  scope; 3 and 7 are identical; 1 does not say "since your last review".
- `round_stanza("plan", n)` is `""` for every n.

`tests/test_personas.py`:
- `phase:` absent resolves to 1; `phase: 2` resolves to 2; `phase: 3`,
  `phase: two`, and `phase:` empty each raise `ConfigError` naming the
  file.
- The shipped tree: `code/sage.md` is phase 2, every other file in both
  trees is phase 1.

`tests/test_review_loop.py`:
- `rounds` increments on success only; a failure, a rotation, and a limit
  leave it where it was; the next pass after each runs at the unchanged
  round.
- The composed `--append-system-prompt` on a resumed pass carries both
  the persona body and the round line, with the round number the
  bookkeeping predicts.
- `run_group` with a mixed-phase group: every phase-1 dispatch completes
  before any phase-2 dispatch starts, under caps of 0, 1, and 2.
- A phase-1 limit leaves phase 2 undispatched, and the phase-2 pairs end
  up in `owed`.
- A group whose `to_run` is phase 2 only, and one that is phase 1 only,
  each run once with no error.

`test-personas.sh`:
- `PERSONAS=sage,red_team`: the log line keeps selection order, and the
  invocation ordinals put Red Team first and Sage second in both cycles.
- The resumed invocation's `--append-system-prompt` carries "round 2"
  adjacent to the persona text, the same adjacency technique the persona
  assertion uses, so the round line is proven to ride with the persona
  rather than to have leaked into the task prompt.

## Documentation

`CLAUDE.md` gains a subsection under "Review modes and personas" covering
the price, the ladder, phases, and the importer hazard, and the "Gotchas"
list gains the `phase: 2` pin and the rule that the round line goes in
the system prompt. `HISTORY.md` gets its entry when the work lands.
