# Plan review and code review as two modes

Date: 2026-08-25
Status: approved, not yet implemented
Supersedes nothing; extends `2026-08-21-claudebox-personas-design.md`.

## The correction

The personas imported from [advocate](https://github.com/jmcentire/advocate) are
**plan-review** personas. They were written to interrogate a proposal before the
work happens. Phase 1 shipped them as the mechanism for reviewing pull requests
in general, which is a misreading of what they are for.

The persona bodies survived the import fairly target-neutral, so nothing is
literally broken today. What is wrong is fit. Sage asking whether the
fundamental approach is sound, and SME asking whether the problem is correctly
understood, are cheap to act on before code exists and close to useless on a
finished PR, where the only honest response to either is "start over." Meanwhile
`personas/_shared.md` hard-codes `gh pr comment` and "this same pull request",
and `DEFAULT_PROMPT` is `gh pr diff`-shaped end to end, so the plumbing is
PR-only even where the persona text is not.

claudebox should do both jobs. This spec makes review **mode** a first-class
axis alongside PR and persona.

## Shape of the change

A plan arrives as a pull request whose diff is the plan document. That decision
is what keeps this change small: the review loop, the four PR selectors, the
`PR_SESSION` map, the usage-limit handling, and the `gh pr comment` output
channel are all untouched. What changes is which prompt and which persona set a
given PR gets.

Routing is by **label**, not by a path heuristic and not by a classifier pass. A
label is explicit, per-PR, and controlled by the author, and it puts no
nondeterministic decision inside the harness's control flow.

## 1. Mode routing

New operator var `PLAN_LABEL` (default `plan`). A PR carrying that label is
reviewed in plan mode; every other PR is reviewed in code mode. Code mode is the
default in the strong sense: an operator who never labels anything sees exactly
today's behavior.

`enumerate_candidate_prs` stops emitting bare numbers and emits
`number<TAB>mode`, so mode is decided at the one seam that already decides what
gets reviewed at all.

- `all`, `assignee`, `search`: the existing `gh pr list --json number` becomes
  `--json number,labels` and the `--jq` does the matching. No extra API calls.
- `ids`: a fixed list with no list call behind it, so it costs one
  `gh pr view N --json labels` per PR per cycle.

**A failed label lookup skips that PR for the cycle with a WARN.** It does not
fall back to code mode. A wrong-mode review posts real comments on a real PR and
cannot be taken back; a skip is one log line and a retry on the next cycle. For
the three list-based selectors a failure fails enumeration as a whole, which the
existing `|| true` already degrades to an empty candidate list. Only `ids` can
fail per-PR.

## 2. Persona layout

`PERSONA_DIR` becomes a parent directory holding `code/` and `plan/`. Each
subdirectory has its own `_shared.md` and its own six persona bodies.

A `PERSONA_DIR` with persona files sitting directly in it is a hard startup
error whose message names the subdirectory it expected. This breaks the flat
`PERSONA_DIR` mount contract that phase 1's docs advertise. Nothing has shipped
off this branch, so there is no compatibility debt to carry, and the check exists
for the same reason the missing-`_shared.md` check does: the mount-your-own
workflow is exactly the path that reaches it, and failing silently there means a
crash loop under `--restart unless-stopped`.

`resolve_personas` runs **once per mode at startup**, for both modes, whether or
not any PR is labeled. That preserves phase 1's property that a broken persona
definition kills the container at boot rather than surfacing the first time
somebody adds a label to a PR.

Keys become mode-qualified:

- `PERSONA_PROMPT["$mode:$id"]`, `PERSONA_LABEL["$mode:$id"]`
- `PERSONAS_LIST` splits into a per-mode list
- the pair key grows a segment: `"$pr:$mode:$persona"`

The consequence worth stating plainly: `PERSONAS_LIST` stops being one global
list, because each PR contributes its own mode's personas to the flattened pair
list, and that list is now heterogeneous. `RESUME_AT` already falls back to the
head of the list when its key no longer exists, so a PR whose label changes
between cycles orphans its old sessions and starts fresh under the new mode.
That is the behavior we want. The orphaned entries leak in `PR_SESSION`, which
is the same in-memory-only caveat phase 1 already documents and defers.

## 3. Persona sets

Both trees ship all six personas. The per-mode default differs:

| mode | default set |
|------|-------------|
| code | `red_team,adversarial,sme,sage` (unchanged) |
| plan | all six |

This cut needs no new justification. It is the reasoning already written into
`entrypoint.sh` at the `DEFAULT_PERSONAS` definition: `user` and `good_friend`
were authored against designs and whole projects, and on a narrow diff they
reach for material that is not in it. The phase 1 default was therefore already
the code subset. Plan mode is where the remaining two finally have something to
bite on.

Shipping all six in both trees, rather than four in `code/` and six in `plan/`,
keeps `PERSONAS=all` meaningful in both modes and lets an operator opt
`good_friend` into code review if they want it.

### The importer tension

advocate has one body per persona, so `tools/import-advocate-personas.py` cannot
invent two. It writes the same imported text into both trees. Any hand-tuning of
a body in one tree is something a later import run would clobber.

We are deliberately not building machinery for this. The importer's docstring
already says to run it once and commit the output, so a re-run produces a diff
that gets reviewed before it is committed, and a hand edit appears in that diff
as a reverted line to keep. A divergence-detecting importer buys very little
over reading a diff that was going to be read anyway.

## 4. Prompts

`DEFAULT_PROMPT` and `DEFAULT_FOLLOWUP` split per mode.

**Code mode** keeps today's text verbatim, `_test_stanza` and `_gh_stanza` and
`_linear_stanza` all included.

**Plan mode** drops `_test_stanza` entirely, since there is no implementation to
mutate. It keeps `_gh_stanza` (the privilege-minimized token constrains
`gh pr view` identically in both modes) and `_linear_stanza`, which arguably
earns its keep more here than on a diff: the ticket is where the problem the plan
claims to solve is actually stated.

Plan mode gains `_plan_stanza`, which has to do two jobs. It says what to review,
and it says what not to flag, because a code-shaped reviewer handed a design
document will reliably report missing error handling in code nobody has written:

> This pull request proposes an approach rather than implementing one. Review
> the proposal itself: whether the problem is stated correctly, whether this is
> the simplest thing that solves it, what it fails to account for, what it
> forecloses, and what would have to be true for it to work. Where you object,
> say what you would do instead. There is no implementation to inspect, so do
> not ask for tests, error handling, or input validation in code that does not
> exist yet; a gap in the plan's own reasoning is a finding, a gap in code it
> has not written is not.

Like the other stanzas, `_plan_stanza` is appended to the **defaults only**, so
an operator-supplied plan prompt reaches Claude verbatim.

`personas/plan/_shared.md` keeps the output contract (one `gh pr comment` per
finding, signed `-claudebox ({{PERSONA}})`), keeps the do-not-manufacture-findings
rule that is what lets a persona correctly say nothing, and keeps the
you-are-not-the-only-reviewer text. The blindness between personas matters
identically in both modes.

### Resumed passes carry the iteration

A plan PR does not sit still once claudebox comments on it. Feedback on a plan
produces a revised plan, pushed to the same branch, which is precisely the shape
the existing loop is built for: `--resume` means a persona reads revision two in
the context of what it already said about revision one, and `FOLLOWUP_PROMPT`
tells it to raise only what it has not already raised. Session resumption is
load-bearing for plan mode rather than incidental to it.

### New operator vars

Bare names keep meaning code mode. Plan mode gets `PLAN_`-prefixed counterparts:

- `PLAN_LABEL` (default `plan`)
- `PLAN_PERSONAS`
- `PLAN_REVIEW_PROMPT`, `PLAN_FOLLOWUP_PROMPT`
- `PLAN_REVIEW_PROMPT_SUFFIX`, `PLAN_FOLLOWUP_PROMPT_SUFFIX`

All six go on the `strip_surrounding_quotes` list, since every one of them is
operator-supplied and `docker run --env-file` does no quote processing.

## 5. Tests

`test-personas.sh` takes the new coverage. It already captures one dump per
`claude` invocation and runs two cycles, which is what these cases need:

1. a labeled PR resolves the plan persona set and a plan prompt
2. an unlabeled PR **in the same cycle** resolves the code set, proving the pair
   list is genuinely heterogeneous rather than uniformly one mode
3. a resumed plan pass still carries its plan persona, which is the
   `--append-system-prompt`-does-not-survive-`--resume` property the suite exists
   to pin, now asserted per mode
4. a PR that gains the label between cycle one and cycle two starts a fresh
   session rather than resuming a code-mode one
5. `_test_stanza` is absent from the plan prompt and present in the code one
6. a flat `PERSONA_DIR` dies at startup
7. a `plan/` missing its `_shared.md` dies at startup

`test-providers.sh` needs a smaller but mandatory change: its `gh` stub must
start returning labels, and its baseline PR must be unlabeled so that each case
still produces exactly one `claude` invocation under its pinned
`PERSONAS=red_team`. Its `_test_stanza` assertion stays where it is and becomes a
code-mode assertion by construction.

## 6. Docs

- `CLAUDE.md`: the Personas section states outright that personas are how
  claudebox reviews PRs, which is the sentence this change contradicts. Rewrite
  the section rather than amending it. The Configuration section takes the six
  new vars.
- `README.md`: the two-mode framing, the label workflow, and the six new vars.
- `.env.example`: the six new vars, unquoted per the existing rule.
- `HISTORY.md`: the correction and why it was made.
- The advocate provenance note gains the correction: these are plan-review
  personas on loan, and code mode runs the subset of them that survives contact
  with a diff.

## Out of scope

- Persisting `PR_SESSION`, `PR_PASSES`, or `RESUME_AT` across a container
  restart. Still deferred to phase 2, unchanged by this spec.
- The `aggregate` reconciliation pass, still reserved and still phase 2.
- Reviewing plans that are not in a PR: loose files in the repo, Linear ticket
  bodies, GitHub issue bodies. Considered and declined, because each needs a new
  enumeration source and a new output channel, and the ticket variant needs write
  access the safety model deliberately withholds.
- Any automatic detection of plan-ness. Path heuristics and classifier passes
  were both considered and declined in favor of the label.
