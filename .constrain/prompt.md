# Briefing: multi-persona adversarial review in claudebox

## System Context

claudebox is a Docker image that runs an unattended PR reviewer. It bundles the
Claude Code CLI and the GitHub CLI, points Claude Code at a configurable model
provider, and loops over a repo's open PRs in headless YOLO mode, posting one
comment per finding. Its premise is that a different model reviewing than the one
that wrote the code avoids group-think.

Today each candidate PR gets one Claude session, resumed on later cycles so the
reviewer does not re-raise findings it has already posted. That single session is
a generalist: its prompt names the areas to attend to and carries two procedural
stanzas, one about the privilege-minimized token's actual capabilities and one
that turns "review the tests" into a mutation-testing procedure.

The work being briefed replaces the generalist with a set of named adversarial
personas borrowed from advocate, a six-persona review engine by the same author.
Each persona has an angle of attack and a success criterion: Red Team wants the
thing to survive assault, Adversarial wants the argument to hold under direct
challenge, Sage wants a smart person to be able to explain it simply, Subject
Matter Expert wants a peer to sign off, User wants a stranger to navigate it
without a guide, Good Friend wants you to hear the harsh truth now rather than at
3am. advocate itself cannot be used, because it calls provider APIs directly and
therefore cannot run against an Anthropic fixed-price plan. claudebox already
solves that problem: it drives the Claude Code CLI, which the plan covers.

Users are the operator running the container and the authors of the PRs being
reviewed. The operator holds every credential and pays for every token.

## Consequence Map

Ranked by severity.

1. **A persona silently stops being adversarial.** Verified: `--append-system-prompt`
   does not survive `--resume`. If the identity is passed only on the first pass,
   cycle 1 is adversarial and every later cycle is the generic reviewer wearing
   the persona's name in the logs. The system looks like it is working and the
   product is gone.
2. **The reviewed repo picks its own reviewer.** The per-project persona-to-model
   map lives in `/repo`, which is untrusted. If the map is ever read from a PR
   head, a PR author chooses the model that reviews their PR.
3. **Independence is lost to tidiness.** advocate runs its personas blind to each
   other and reconciles centrally. Any instruction telling a persona to defer to
   another persona's comment anchors it, which is group-think with extra steps and
   is the failure this project exists to avoid. Reconciliation therefore has to
   happen somewhere that is not a persona.
4. **Findings drown.** Four personas commenting per cycle on the same PR, each
   reading a comment list the others are growing.
5. **Usage limits become the binding constraint.** On a fixed-price plan the
   scarce resource is allowance, not dollars. Multiplying sessions per cycle by
   the persona count makes hitting the cap routine, and the existing failure path
   makes it worse: dropping the session means the next cycle re-reads the whole PR
   and re-posts findings already posted, spending more of the resource that ran
   out. Cadence surprises the operator too, since one cycle is now candidate PRs
   times enabled personas sequential sessions.
6. **A restart burst multiplies.** The session map is in memory today, so a
   restart re-reviews every PR once. With personas that is one burst per persona.

## Failure Archaeology

- A claudebox instance reviewed a PR whose tests passed identically with the
  production change reverted, and said nothing about it. The fix was not to ask
  for better test review but to name a procedure a model can execute against a
  diff: revert the lines the test depends on, decide whether it still passes.
  The lesson generalizes to this work. A named, checkable procedure beats an
  adjective, and a review that says nothing looks exactly like a clean review.
- A bare `gh pr view` implicitly requests `statusCheckRollup`, which no
  fine-grained PAT can be granted. Sessions that hit it read the failure as a
  broken token and start guessing at the diff. The fix was to state the working
  invocation in the prompt, and to repeat it on resumed passes because a
  long-running session's earliest turns are the first thing a context summary
  drops. The same repetition requirement now applies to persona identity, for a
  mechanical reason rather than a summarization one.
- Provider wiring bugs surface as per-request 401s rather than startup errors,
  which is why test-providers.sh exists and asserts the exact environment handed
  to `claude`. Any new untrusted-to-argv path deserves the same treatment: fail
  at startup, where someone is watching.

## Dependency Landscape

Upstream: the Claude Code CLI (its `--append-system-prompt`, `--resume`,
`--model` and `--strict-mcp-config` behavior), the GitHub CLI and the minimized
token, the selected model provider, optionally Linear over MCP, and for one
provider a LiteLLM proxy and a normalizer inside the container.

Downstream: nothing consumes claudebox's output programmatically. Its output is
PR comments read by humans.

Borrowed from: advocate's `PERSONA_META` and `SYSTEM_PROMPTS`. The identity,
dimension list, success criterion and the do-not-manufacture-findings rule are
worth taking. The JSON output contract at the end of every one of advocate's
prompts is not, because claudebox's output channel is `gh pr comment`.

## Boundary Conditions

In scope: a persona selector on the launcher, one session per PR per persona,
persona definitions shipped as files in the image, a per-project persona-to-model
map read from the reviewed repo with operator override, a harness-authored
coverage comment, and a persisted session map.

Out of scope: per-persona *providers*. Provider selection resolves once at
startup into process-wide environment, and one provider runs a single in-container
proxy bound to one upstream. Giving each persona its own provider means
refactoring that whole block into a per-pass function, and it is not needed to
get adversarial breadth.

Also out of scope: cross-persona synthesis. advocate aggregates findings and
reports disagreements centrally. claudebox has no aggregation point, so
disagreement has to be expressed on the PR by one persona replying to another.

Unchanged: the three hardening boundaries, `--strict-mcp-config`, the no-fallback
rule on models, verbatim delivery of operator-supplied prompts, and bash 3.2
compatibility for the host launcher.

## Success Shape

- A resumed pass is provably still in persona, and the test suite is what proves it.
- Which persona said a thing is visible on the PR, and which personas have swept
  a PR at which commit is visible without reading container logs.
- The reviewed repo can express a per-persona model preference; the operator can
  always overrule it; a bad value fails at startup rather than per request.
- An operator who wants today's behavior can still get a narrow, cheap run.
- Nothing about the change enlarges what an unattended YOLO session can do.

## Done When

1. `./claudebox.sh --help` documents `--persona`, and an unknown persona name
   fails at startup with a message naming the valid set.
2. With no selector, exactly four personas run: red_team, adversarial, sme, sage.
   `user` and `good_friend` run only when named.
3. `test-providers.sh` asserts, for a single cycle: one invocation per (PR,
   persona), the persona system prompt present on the resumed invocation as well
   as the first, the per-persona model on both `--model` and every model-tier env
   var, and a refusal for each of an unknown persona name, a malformed model id,
   and a repo map key that is not a persona.
4. No persona definition file contains a JSON output contract, and the suite
   asserts that.
5. A repo map naming a model for a persona the operator also named resolves to
   the operator's value, and the suite asserts it.
6. Persona ordering within a cycle is stable across runs and is documented.
7. A restart with a persisted map resumes rather than re-reviewing, and a stale
   session id degrades to a fresh session for that pair rather than failing forever.
8. One status comment per PR, edited in place, naming each persona and the head
   SHA it last swept.
9. `bash -n` clean on both scripts; `./test-providers.sh` and `./test-shim.sh`
   pass; one live run under `claudebox.sh test` against a real PR before anything
   runs unattended.
10. CLAUDE.md, README.md and .env.example describe the persona surface, the
    cadence consequence, and the untrusted-map threat model.

## Trust and Authority Model

There is no trust-scoring subsystem here; see `trust_policy.yaml` for why most of
that vocabulary is left null. What matters: three credentials (GitHub token,
provider credential, optional Linear key), none of which may appear in a persona
prompt, a status comment, or the persisted session map; and one untrusted input,
the reviewed repo's persona map, which may name models and nothing else, is read
only from the default-branch working clone, is validated against a character
allow-list at startup, and loses to the operator wherever the operator has spoken.

Authority is single-owner throughout. The launcher owns the host flag vocabulary
and parses no configuration. The persona registry owns what a persona is. The
resolver is the only place the two maps meet. The supervisor owns cadence,
ordering and rotation. The session runner is the only spawner of `claude`. The
status writer is the only author of the coverage comment, and it is the harness
rather than a model precisely so that coverage reporting is verifiable.

## Component Topology

The launcher hands the container an environment. The supervisor asks the registry
for the enabled persona set and the resolver for a model per persona, then loops:
fetch refs, enumerate candidate PRs, and for each PR run each persona in a stable
order through the session runner, one headless `claude` invocation per pair,
recording the session id in the session store and updating the PR's coverage
comment through the status writer. Both the harness and each persona session
reach GitHub only through the `gh` CLI carrying the minimized token. The provider
wiring is unchanged except that its model-tier fan-out becomes something the
session runner can reproduce per invocation. The test suites sit outside the
runtime and assert that the environment and argv handed to `claude` are what the
design says they should be.
