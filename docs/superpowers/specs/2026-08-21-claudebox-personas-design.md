# Multi-persona adversarial review in claudebox

Date: 2026-08-21
Status: draft, vetted against simulacrum's specialist prompt, pending approval
Constrain artifacts: `.constrain/` (prompt.md, constraints.yaml, component_map.yaml, trust_policy.yaml, schema_hints.yaml)

## Problem

claudebox reviews each open PR with one generalist Claude session. advocate
reviews a target with six named personas, each with an angle of attack and a
success criterion, and treats disagreement between them as signal. advocate
cannot be used directly because it calls provider APIs and therefore cannot run
against an Anthropic fixed-price plan. claudebox drives the Claude Code CLI,
which the plan covers, so the personas move into claudebox rather than the
billing model moving to advocate.

## What is being built

1. Persona definitions borrowed from advocate, shipped as files in the image.
2. A `--persona` launcher flag (and `PERSONAS` env var) selecting which personas
   run. Optional; with nothing selected, four run.
3. One Claude session per PR **per persona**, resumed across cycles as today.
4. A per-project persona-to-model map read from the reviewed repo, overridable
   by the operator. Stretch goal, phased last.
5. An aggregation pass per PR: the only pass that reads other personas' findings,
   maintaining one digest comment that names where they agree and where they
   disagree.
6. Usage-limit-aware failure handling, which is a prerequisite for running several
   times as many sessions per cycle rather than a nicety.
7. A harness-authored coverage comment, making persona coverage and the swept
   commit visible to a PR reader with no access to container logs.
8. A persisted session map, so a restart resumes instead of re-reviewing.

Explicitly not being built: per-persona *providers*.

## The one verified fact the design rests on

`--append-system-prompt` does not survive `--resume`. Measured, not assumed: a
session started with an injected identity and resumed without the flag answered
without it; resumed with the flag re-passed, it answered with it. Every pass
therefore re-passes the persona's system prompt. This is the same repetition
requirement that `_gh_stanza` and `_test_stanza` already satisfy, for a
mechanical reason rather than a context-summarization one, and it is the single
most important thing for the test suite to pin down: if it regresses, cycle 1 is
adversarial and every later cycle is the old generalist under a persona's name.

## Design

### Persona definitions (`personas/*.md`, new)

One file per persona, `COPY`d to `/opt/claudebox/personas` and read from
`PERSONA_DIR` (overridable, so an operator can mount their own set). Frontmatter
carries `label` and `success`; the body is the system prompt.

```
---
label: Red Team
success: The thing survives assault.
---
You are a Red Team security reviewer. Your job is to find the attack surface.
...
```

Six files, ids matching advocate's: `red_team`, `adversarial`, `sage`, `user`,
`sme`, `good_friend`. The prompt bodies come from advocate's `SYSTEM_PROMPTS`
with two edits. advocate's `_COMMON_OUTPUT_FORMAT` tail is removed, because it
demands a JSON findings array and claudebox's output channel is `gh pr comment`.
Its final rule is kept and moved into a shared preamble, because it is the
load-bearing sentence for a reviewer that should sometimes say nothing:

> If the input is solid and you have no findings, say so and post nothing. Do not
> manufacture findings to appear thorough. Silence from you is a strong signal.

`personas/_shared.md` holds the claudebox output contract appended to every
persona body: post one comment per finding through `gh`, and sign
`-claudebox (<label>)`.

It also holds the independence rule, which is the inverse of what a
noise-reduction instinct would write. Comments signed by other personas are not
yours; do not defer to them, do not treat their existence as coverage, and do not
suppress a finding because another persona reached a similar one from a different
angle. advocate runs its six personas in parallel and blind to each other and
reconciles centrally, and that blindness is the mechanism that makes six
perspectives worth more than one. Telling persona four to check what personas one
through three said anchors it to them, which is group-think with extra steps and
the exact failure this project exists to avoid. Reconciliation happens in the
aggregation pass below.

Blindness here is best-effort rather than enforced. The default prompt fetches the
PR's `comments`, which is how a persona sees human replies to its own findings,
so it can also see the other personas'. Dropping `comments` from the field list
would make blindness structural at the cost of human feedback, and human feedback
is worth more. The instruction is therefore the mechanism, and its imperfection is
a known limitation rather than an oversight.

The output contract lives in the **system** prompt rather than the task prompt so
that an operator-supplied `REVIEW_PROMPT` still reaches Claude verbatim. The
default prompts lose their own "Sign your comments with '-claudebox'" sentence,
which would otherwise contradict the persona-specific signature.

### Selection (`claudebox.sh`, `entrypoint.sh`)

`--persona red_team,sage` on the launcher, or `PERSONAS=red_team,sage` in the env
file. `all` expands to the six. Default, when unset:
`red_team,adversarial,sme,sage`. `user` and `good_friend` are omitted from the
default because advocate wrote them against designs and whole projects, and on a
narrow diff they reach for material that is not in it.

The launcher parses nothing: it forwards the string. macOS ships bash 3.2, which
has no associative arrays, and a persona-to-model map is exactly the shape that
invites one.

`entrypoint.sh` validates each name against the files in `PERSONA_DIR` and dies
naming the valid set. Order within a cycle is the order given, and the default's
order is the order listed above. Ordering is documented and stable so that a cycle
cut short by a backoff is interpretable, and so the aggregation pass reliably runs
after the personas it aggregates.

### The loop (`entrypoint.sh`)

`PR_SESSION` and `PR_PASSES` become keyed by `"$pr:$persona"`.

```
for pr in candidate PRs:
  for persona in enabled personas:      # stable order
      run one pass; record session id; update coverage comment
  run the aggregation pass for this PR
```

`MAX_PASSES_PER_SESSION` counts passes per pair. A failed pass drops that pair's
session id only, except on a usage limit; see below.

Passes stay sequential, and the reason is worth stating because the obvious one is
wrong. Blind personas are not ordered by anything, and they only read the working
clone, whose one writer runs once per cycle before any pass, so concurrency is
available and would keep cycle time flat. It is declined because concurrency
multiplies instantaneous usage-limit pressure and makes the backoff below race
against itself. Order still matters for two other reasons: a partially completed
cycle after a backoff is interpretable, and the aggregation pass has to run after
the personas it aggregates.

`REVIEW_INTERVAL_SECONDS` keeps its current meaning, the gap after a cycle, and
the docs gain the sentence that a cycle is now (candidate PRs x enabled personas)
sequential sessions, plus one aggregation pass per PR. Four PRs and four personas
is twenty sessions before the sleep, and an operator who reads the interval as a
period will otherwise conclude the loop is hung.

### The aggregation pass

advocate's value is not only six prompts. It is six independent reads plus a
central step that surfaces where they disagree, because a Sage saying "simplify"
against an SME saying "this complexity is necessary" is itself the finding. A port
that keeps the prompts and drops the reconciliation has imported the cheaper half.

So after a PR's personas have swept it, one more pass runs with the reserved id
`aggregate`. It is not a persona and cannot be named in `--persona`. Its prompt
reads only the PR's `-claudebox` comments, not the diff, and it maintains a single
marker-identified digest comment, edited in place, with two sections: findings
several personas reached independently, and findings that are in tension. It has
its own resumed session per PR, like a persona.

It costs one extra session per PR per cycle. `AGGREGATE=0` turns it off, and it is
off automatically when only one persona is enabled, where it would have nothing to
reconcile.

### Usage limits

This is a prerequisite, not a refinement. On a fixed-price plan the binding
resource is usage allowance, and four personas across four PRs is twenty
sessions per cycle, so reaching the cap goes from theoretical to routine. The
existing failure path is exactly wrong under a limit: a non-zero exit drops the
pair's session, so the next cycle re-reads the whole PR from scratch and re-posts
findings it already posted, consuming more of the resource that just ran out.

`run_pass` therefore classifies its failure. A usage or rate-limit failure keeps
the pair's session id, abandons the remainder of the cycle, and backs off before
the next one. Any other failure behaves as today.

Detection reads Claude Code's exit status and stderr, which is an upstream surface
that can change without warning. A missed classification must degrade to today's
behavior, never to a crash, and the test suite pins the classifier against captured
error text so an upstream change shows up as a red test rather than as a slow
month of duplicate comments.

### Per-pass model wiring (`run_pass`)

`run_pass` takes the persona's model. The model-tier fan-out that currently
happens once at startup is reproduced per invocation, as command-scoped env
rather than by mutating the process environment:

```sh
env ANTHROPIC_MODEL="$m" \
    ANTHROPIC_DEFAULT_FABLE_MODEL="$m" ANTHROPIC_DEFAULT_OPUS_MODEL="$m" \
    ANTHROPIC_DEFAULT_SONNET_MODEL="$m" ANTHROPIC_DEFAULT_HAIKU_MODEL="$m" \
    ANTHROPIC_SMALL_FAST_MODEL="$m" \
  claude -p ... --model "$m" --append-system-prompt "$persona_prompt" ...
```

All tiers must move together for the same reason they do today: non-Anthropic
providers have no Opus/Sonnet/Haiku, so a tier left pointing at the global
`REVIEW_MODEL` means part of the review silently runs on a different model than
the map specifies. There is no fallback anywhere in this path.

### The persona-to-model map (stretch, phase 3)

Two sources.

- **Operator**: `PERSONA_MODELS=red_team=glm-5.2:cloud,sage=@cf/zai-org/glm-5.2`,
  plus a repeatable `--persona-model red_team=...` on the launcher.
- **Reviewed repo**: `.claudebox/personas.yml` in the working clone.

Operator wins per persona. The repo supplies values for personas the operator did
not name. A persona named by neither uses the global `REVIEW_MODEL`.

**The repo file is untrusted input.** It is read from the default-branch working
clone and never from a PR head, and no code path checks out a PR head. Today that
property holds by accident, because the reviewer reads diffs through
`gh pr diff`; the design makes it an explicit commented rule, because the moment
it breaks, a PR author chooses the model that reviews their own PR.

The accepted grammar is a strict flat-mapping subset, documented as a subset
rather than advertised as YAML: comment lines, blank lines, and `persona: model`
with at least one space after the colon, optional surrounding quotes stripped.
Anything else, including indentation or nesting, is refused with the line number.
A tiny auditable grammar in shell is preferable to importing a YAML parser to
read a file the reviewed repo controls; the alternative considered was PyYAML from
the LiteLLM venv, rejected because it would couple persona config to a provider's
dependency.

Every resolved model id is validated at startup against
`^[A-Za-z0-9._:@/-]{1,200}$` and an unknown persona key in either map is an
error. Refusal happens at startup, where an operator is watching, rather than
once per request, which reads as a provider outage. A well-formed but nonexistent
model still fails per-request; a live startup probe was considered and rejected
as needing a per-provider probe for all five backends.

### Coverage comment (`status-comment-writer`, new)

The harness, not a persona, owns it. Located by an HTML marker
(`<!-- claudebox:status v1 -->`), created once per PR, edited in place each cycle
through `gh api -X PATCH .../issues/comments/{id}`, which needs only the PR
comment write permission the minimized token already has. It records per persona:
the head SHA last swept, the pass count, and the number of comments that pass
added, obtained by counting the PR's comments before and after each pass.

It exists to make coverage legible to a PR reader who has no access to container
logs: which personas have looked at this PR, and at which commit. That is its
whole claim. An earlier draft justified it as resolving the ambiguity between a
persona with nothing to say and a persona that failed, which it cannot do; it
records that a pass ran and how many comments it added, and `docker logs` already
says that much to the operator.

### Session persistence (`session-store`, new)

`$WORK_DIR/sessions.tsv`, mode 600, `pr<TAB>persona<TAB>session_id<TAB>passes`,
rewritten atomically after each pass and loaded at startup. Holds no credential
and no diff content. A persisted id that no longer resumes takes the existing
failure path: drop it, start fresh for that pair next cycle.

Without it, the existing one-duplicate-burst-per-restart becomes one burst per
persona.

## Testing

`test-providers.sh` exists to prove credential and model wiring, and it does that
with one `claude` invocation per cycle. Rather than rewrite every case in it for
reasons unrelated to what it tests, its baseline environment pins a single persona
and `AGGREGATE=0`, so its existing assertions keep holding unchanged. That is a
one-line change plus a comment explaining why the line is there.

A new `test-personas.sh` uses the same technique (stubs on `PATH`, `env -i`,
`ALLOW_UNHARDENED=1`) with two differences: capture is indexed per invocation
(`claude.N.env`), and its `sleep` stub succeeds on the first call and fails on the
second, so the harness runs two cycles. That second cycle is the whole point: a
one-cycle harness produces no resumed invocation, which is why the existing suite
cannot assert `FOLLOWUP_PROMPT`'s stanzas today and why the most important
assertion in this design would otherwise be unwritable.

Cases:

- default set is exactly the four, in order
- `all` expands to six; an unknown name refuses, naming the valid set
- one invocation per (PR, persona) in one cycle, in the documented order, plus one
  aggregation invocation last; `AGGREGATE=0` removes it; a single enabled persona
  removes it
- the persona system prompt is present on the second cycle's resumed invocation as
  well as the first cycle's fresh one, which is the regression test for the one
  verified fact above
- a stubbed usage-limit failure keeps the pair's session id and ends the cycle; any
  other failure drops it, as today
- per-persona model appears on `--model` and on every model-tier env var
- operator map beats repo map for a persona both name
- a malformed model id refuses at startup; so does an unknown persona key in
  either map; so does a repo file line outside the accepted grammar
- no persona definition file contains a JSON output contract

Before anything runs unattended: `bash -n` on both scripts, both suites green,
and one live `claudebox.sh test` against a real PR, because the suites prove the
wiring is what we intended and nothing more.

## Phasing

1. **Core.** Persona files, selector, per-pair sessions, per-pass system prompt,
   usage-limit classification and backoff, `test-personas.sh`, docs. The
   rate-limit path ships with the multiplier that makes it necessary, not after.
2. **Reconciliation and operability.** Aggregation pass, the marker-based
   create-or-edit comment helper it shares with the coverage comment, coverage
   comment, persisted session map.
3. **Stretch.** The persona-to-model map, both sources, validation, tests.

Each phase is shippable alone. Phase 1 is the product; phase 2 is what makes six
opinions readable; phase 3 is the budget lever.

## Consequences accepted

- Usage and cycle duration scale with the enabled persona count, plus one
  aggregation pass per PR. Four personas by default, so roughly five times
  today's per-cycle work.
- Every existing deployment gets that multiplier on upgrade without asking for it.
  An operator who wants today's cost sets `--persona` to a single persona.
- PR comment volume rises, and blindness means overlapping findings between
  adversarial and sme in particular will appear as separate comments. The
  aggregation digest names the overlap rather than preventing it.
- A repo whose persona map is malformed stops its own reviews at container start.
  Raised as a denial-of-review surface reachable by anyone with merge rights, and
  reaffirmed: the people who can write that file own the repo. The mitigation is
  the error message, which must name the file, the line, and the fact that
  deleting the file restores service.
- Blindness is instructed rather than structural, because the alternative costs
  the personas their view of human replies.
