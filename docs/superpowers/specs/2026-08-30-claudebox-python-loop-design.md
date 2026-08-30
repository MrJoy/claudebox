# Port the review loop to Python, then run personas in parallel

Date: 2026-08-30
Status: approved, not implemented
Phases: A (behavior-preserving port), B (intra-PR persona fan-out)

## Problem

Two problems, and they turned out to be the same problem.

The immediate ask: review a PR with all of its personas concurrently instead of
sequentially, with a barrier at the end of each PR before the next one starts.
Today a cycle is a flat list of `(pr, mode, persona)` triples walked one at a
time, and with six personas on a plan PR the wall-clock cost of a cycle is six
sessions deep.

The underlying problem: `entrypoint.sh` is two programs sharing a file. The
first produces an environment for a child process, which is what shell is for.
The second is a stateful supervisor with per-task structured results, which is
what shell is worst at. Every defect recorded against this harness lives in the
second program:

- The `MODE_PERSONAS` expansions at `entrypoint.sh:365` and `:1298` word-split
  and glob-expand, because `MODE_PERSONAS` is a space-joined string where
  `PERSONAS_LIST` used to be an array. A persona basename containing a glob
  metacharacter yields an unbound `PERSONA_PROMPT` lookup that kills the loop
  mid-cycle.
- A defect class seen three times during the two-modes work: the harness turns
  malformed or empty input into confident wrong behavior instead of a loud skip.
  A successful-but-empty `gh pr view --json labels` dropped a PR with no log
  line. An unguarded `.number` in the jq label filter produced a candidate PR
  literally named `null`. Both are shell's inability to tell absence from
  emptiness.
- Three `case "$mode"` dispatches have no `*)` arm, so adding a mode fails at
  boot with no context.

Adding concurrency with per-slot results is the thing shell is least good at,
and it is exactly what phase B needs. Doing it in bash means writing the hard
part twice. So the loop moves first, and the fan-out lands on top of it.

## Decisions taken

Recorded here because each one closes a question that was genuinely open.

1. **The clone stays shared.** Personas are told, in the prompt, that the
   worktree is shared and that writing git commands are forbidden. Not N clones.
2. **A limit does not kill in-flight passes.** The group runs to completion, and
   only the personas that were limited are owed on the next cycle. The successful
   ones are not re-run.
3. **The log stays live and every pass line is prefixed** with `[#PR mode/persona]`.
   No buffering to a per-group dump.
4. **`MAX_CONCURRENT_PASSES` ships as a knob, defaulting to unlimited.** The
   shipped default gets tuned from observed behavior on a Max20 plan, not from a
   guess. Rate limits are the worry, memory is secondary.
5. **The port comes first, sequential and behavior-preserving,** with both bash
   suites green against it before a single thread is introduced.
6. **The shared-worktree constraint is enforced in the environment as well as in
   the prompt,** and it is appended to operator prompt overrides too. This is the
   one place the verbatim-operator-prompt guarantee is broken, deliberately,
   because a persona that checks out a branch under concurrency corrupts other
   personas' reviews and that is not an operator's to opt out of.

## Section 1: where the shell/Python cut falls

`entrypoint.sh` keeps everything whose output is an environment or a filesystem
state, and ends by exec'ing the loop:

- hardening checks, `gh`/`git` auth
- `build_custom_headers`, the provider `case` block, the model-tier block
- `strip_surrounding_quotes` and `check_url` validation
- the working clone, `write_mcp_config`, `write_litellm_config`, `start_litellm`
- `exec python3 /opt/claudebox/review_loop.py`

`review_loop.py` takes everything whose output is a decision:

- persona resolution from `PERSONA_DIR`, both modes, at startup
- the eight prompt defaults, their stanzas, `render_prompt`, the `MODE_*` tables
- `enumerate_candidate_prs` and `pr_modes`
- the session map, dispatch, `is_usage_limit`, resume bookkeeping, `check_litellm`

Env crosses the boundary and nothing else. No JSON handoff file, no serialized
bash arrays. `CLAUDE_MCP_ARGS` is reconstructed in Python from `MCP_CONFIG_FILE`,
which is where it came from.

### Consequences

**`format_stream` is deleted.** Python parses the `stream-json` lines it is
already reading, so the `tee`-to-tempfile plus `jq` plus `PIPESTATUS[0]` dance
for session-id recovery collapses into reading `.session_id` off a parsed event.
More importantly for phase B, Python holds the write lock on stdout, so prefixed
concurrent lines are atomic by construction rather than by hoping every line
stays under `PIPE_BUF`.

**LiteLLM's lifecycle splits.** Shell starts it, because generating the master
key and writing the config is provider-wiring work. Python owns the per-cycle
liveness probe over HTTP. `exec` makes Python PID 1 in the container, so
teardown needs no trap.

**Loop termination becomes explicit.** `MAX_CYCLES`, default unset meaning
forever, replaces the `sleep`-exits-non-zero trick that both suites rely on. It
gives the suites a deterministic stop instead of one riding on `set -e`, and it
makes `claudebox.sh test` a genuine one-shot rather than a foreground run the
operator interrupts.

A cycle counts toward `MAX_CYCLES` whether it completed or was cut short by a
limit or by consecutive failures, so a case asking for two cycles gets two
regardless of what happened inside them. The loop exits immediately on reaching
the count, without the trailing sleep, so a suite does not pay
`REVIEW_INTERVAL_SECONDS` for nothing. Exit status is 0.

**Persona resolution still kills the container at boot.** `resolve_personas`
runs for both modes as Python's first act, immediately after `exec`, preserving
the property that a broken persona definition fails at startup rather than the
first time somebody labels a PR.

## Section 2: the Python loop, phase A

Behavior-preserving. Nothing about what gets reviewed, in what order, or what a
failure costs changes in this phase.

### Modules

Stdlib only, mirroring `workersai-shim.py`'s no-dependency rule.

| File | Owns |
|---|---|
| `review_loop.py` | entry point, cycle loop, resume bookkeeping, failure counting |
| `personas.py` | `PERSONA_DIR` resolution for both modes at startup |
| `prompts.py` | the eight defaults, the four stanzas, rendering |
| `gh.py` | candidate enumeration, label-to-mode routing |
| `passes.py` | invoking `claude`, consuming the stream, classifying limits |

### The pair key stops being a string

`"$pr:$mode:$persona"` with its `${key%%:*}` and `${_rest#*:}` unpacking becomes
a frozen `Pair(pr, mode, persona)` used directly as a dict key. The parsing goes
away, and with it the class of bug where a persona basename containing a colon
silently corrupts a key.

### Defects that die on their own

- The word-splitting and glob-expanding `MODE_PERSONAS` expansions have no
  Python equivalent.
- The `case "$mode"` dispatches with no `*)` arm raise with context.
- `gh` output goes through `json.loads`, so "exited 0 with empty stdout" and "an
  object with a null `number`" become explicit checks rather than jq filters that
  quietly yield nothing.

### One deliberate departure from a faithful port

The `all`, `assignee`, and `search` arms of `enumerate_candidate_prs` currently
cannot distinguish "`gh pr list` failed" from "no open PRs". Both log
`No candidate PRs for selector X`. The `ids` arm got a skip-and-warn path in
commit `1fbfc1e`; the list arms did not.

Phase A distinguishes them and logs a WARN on the failure case. It changes no
review behavior. It is called out here because it is not strictly
behavior-preserving.

### Two porting hazards to encode in the code

**`USAGE_LIMIT_RE` needs `re.MULTILINE`.** The pattern transfers character for
character:

```
rate.?limit|usage limit|limit reached|reached your limit|too many requests|quota|overloaded|(^|[^0-9])(429|529)([^0-9]|$)
```

`grep` is line-oriented, while Python's `^` and `$` anchor the whole string
without `re.MULTILINE`. Without the flag, a bare `429` on its own line stops
matching, and a missed limit degrades to the ordinary drop-the-session path,
which is silent. Unit-tested explicitly.

**stderr keeps going to a temp file.** Reading two pipes from one child is where
deadlocks live. stdout is the only pipe, read line by line; stderr goes to a
temp file that `is_usage_limit` and `usage_limit_line` read after the child
exits. This preserves the current structure rather than improving on it.

### Session-id recovery

The stream is consumed as it arrives, so the last `session_id` seen is in hand
before the exit code is checked. That is the property `run_pass` currently
arranges deliberately with `tee` plus `tail -n 1`, and it matters because a pass
that started a session and then hit a limit still has a resumable session.

### Log format

`[HH:MM:SS] message` on stdout, `sys.stdout` reconfigured to line buffering so
`docker logs -f` stays live.

Every line emitted by a pass carries its pair: `[14:22:07] [#12 code/sage] ...`.
This lands in phase A, not phase B, so the format an operator learns is the
format that survives the fan-out. Stream lines get timestamps too, which they do
not have today, because once passes overlap the difference between a slow pass
and a hung one is only visible in the gaps between lines. Supervisor lines with
no pair behind them (fetching refs, the candidate list, sleeping) keep the bare
`[14:22:07] ...` shape.

### State

In memory, same restart caveat as today. Persistence stays deferred.

## Section 3: testing

Three layers. The point of the split is that the bash acceptance suites stop
being the only thing standing between a change and a regression.

### Unit tests, stdlib `unittest`

New `tests/`, run by a `test-python.sh` wrapper so the project keeps its
one-script-per-suite convention. No new image dependency.

These cover what today can only be tested by booting the whole entrypoint:

- `is_usage_limit` against real provider error text, including the
  429-on-its-own-line case that `re.MULTILINE` exists for
- `pr_modes` label routing, including a missing `labels` key and a null `number`
- persona resolution, including a frontmatter-only body and the missing
  `_shared.md` refusal
- prompt rendering and stanza composition

### `test-personas.sh`, retargeted, mechanism preserved

It keeps stubbing `gh`, `git`, and `claude` onto `PATH` and running the
entrypoint under `env -i` with `ALLOW_UNHARDENED=1`. Two changes:

1. The `python3` stub at `test-providers.sh:89` currently records argv and then
   `exec tail -f /dev/null`. It exists for the workersai shim. If the entrypoint
   execs `python3 review_loop.py`, that stub swallows the loop and the suite
   hangs forever. It becomes a stub that passes through to the real interpreter
   for `review_loop.py` and keeps recording argv for the shim.
2. Loop termination moves to `MAX_CYCLES`, so each case says `MAX_CYCLES=2`
   outright instead of encoding "two cycles" as "a stub that succeeds once then
   fails".

### `test-providers.sh`, largely untouched

It asserts the environment handed to `claude`, and env survives `exec`, so the
provider matrix keeps passing. Its `PERSONAS=red_team` pin still yields one
invocation per cycle. Its `_test_stanza` assertion still works because the
prompt still arrives in argv, built by `prompts.py` instead of by shell.

### The gate between phases

`test-personas.sh` and the provider matrix both green against the Python loop,
plus the new unit tests, with `MAX_CONCURRENT_PASSES` not yet existing, before
phase B starts.

## Section 4: phase B, the fan-out

### Dispatch

One group per candidate PR, with a barrier at the end of each. Inside a group,
`concurrent.futures.ThreadPoolExecutor(max_workers=cap)` submits one task per
persona enabled for that PR's mode. Threads rather than processes, because every
task's real work is a `claude` subprocess and the GIL never enters it.

`MAX_CONCURRENT_PASSES` defaults to unlimited, meaning the group's persona
count. A value of 1 takes the identical path with a one-worker pool. There is no
separate sequential branch to drift.

### The resume set

`RESUME_AT`, an index into a flat pair list, becomes `owed`, a set of `Pair`.
Normally empty.

When a cycle is cut, `owed` becomes the limited personas of the cut group plus
every pair in the groups that never started. The successful personas of the cut
group are **not** owed.

The next cycle orders groups so any group intersecting `owed` runs first, in
enumeration order. A group intersecting `owed` runs **only** its owed personas.
A group that does not intersect `owed` runs all of its personas.

So the personas that succeeded in the cut group sit out exactly one cycle and
are back to normal the cycle after, while a group that was never reached is
fully owed and runs in full. Groups that completed before the cut still run,
after the owed ones. That last part is what preserves today's wrap-around and
keeps a persistent limit from starving the tail of the list forever, which is
the failure `RESUME_AT` was introduced to prevent.

`owed` is intersected with the current candidate list each cycle, so a PR that
closed or was relabeled drops out rather than resurrecting a dead pair. It lives
in memory alongside the session map.

### Limit handling at the barrier

All futures complete. Nothing is killed mid-review, because a killed pass may
have posted some findings and not others, and its session-id recovery is
unreliable.

If any pass in the group reported a limit: keep those sessions, do not start
further groups, populate `owed` as above, and back off `LIMIT_BACKOFF_SECONDS`.
Successful passes in the cut group record their session and pass count normally.
Non-limit failures in the cut group drop their sessions as they do today.

### Failure counting

`MAX_CONSECUTIVE_FAILURES` is evaluated at the barrier rather than per pass. Add
the group's non-limit failure count to the counter, reset it to zero if any pass
in the group succeeded, abandon the cycle at 3. Against a dead provider a
six-persona group trips it on the first barrier, sooner than today, which is the
right direction for a guard whose purpose is to stop walking into a wall.

### The shared-worktree stanza

New `_worktree_stanza`: the working clone is shared with concurrent reviewers,
read the PR through `gh pr diff` and `gh pr view`, run no writing git command,
no `checkout`, no `fetch`, no `branch`, no `stash`.

It is emitted only when effective concurrency exceeds 1, so a cap of 1 produces
byte-identical prompts to phase A and every existing prompt assertion holds.

Effective concurrency is evaluated per group, as `min(cap, len(personas for that
PR's mode))`. A mode configured down to a single persona therefore never sees
the stanza, because nothing runs beside it. Persona sets do not change while the
container runs, so a resumed session cannot gain or lose the stanza between
passes.

Unlike every other stanza it is appended to operator overrides as well as to the
defaults. This breaks the verbatim-operator-prompt guarantee, which `CLAUDE.md`
states in two places and which must be updated to read "verbatim except the
shared-worktree constraint under concurrency". The justification: a prompt that
lets a persona check out a branch under concurrency corrupts the other personas'
reviews, so it is not an operator-level opt-out.

### Environment enforcement

The stanza is backed by a permission change, because a prompt is guidance and
this needs a wall.

The obvious implementation is wrong twice over. `.git` cannot be permanently
read-only, because the supervisor runs `git fetch --all --prune` on that clone
at the top of every cycle. And `chmod -R a-w` on `.git` is O(object count),
which is the operation that produced commit `fdd0ac1`.

The mechanism: `chmod a-w` on the `.git` **directory inode only**, not
recursive. Every mutating git operation creates a lock file directly in `.git`
first (`index.lock` for `checkout`, `add`, `commit`; `FETCH_HEAD` and ref locks
for `fetch`), and creating a file in a directory requires write permission on
that directory. Reading paths never do, so `git log`, `git show`, `git diff`,
and `git cat-file` keep working.

One inode, so the supervisor toggles it writable around its own fetch at the top
of the cycle and back before the first pass launches. Groups are serialized
after the fetch, so no pass ever sees it writable.

The restore runs in a `finally`, because `git fetch` is already allowed to fail
(`WARN: git fetch failed; continuing`) and a fetch that raised on the way out
would otherwise leave the clone writable for the whole cycle, silently removing
the enforcement while the log said nothing.

This is a chokepoint rather than a wall. Object writes into `.git/objects/**`
would succeed on their own, but nothing reaches them without taking a lock
first. Documented as such rather than overclaimed.

The working tree stays writable, so a persona that drops a scratch file does not
hit a confusing error. Scratch files cannot corrupt another persona's review,
because reviews come through `gh pr diff` rather than through the tree.

## Section 5: scope

### In scope

- `entrypoint.sh` shrinks to startup plus `exec`
- new `review_loop.py` and its four modules
- new `tests/` and `test-python.sh`
- `test-personas.sh` retargeted
- `test-providers.sh`'s `python3` stub reworked
- new env vars `MAX_CYCLES` and `MAX_CONCURRENT_PASSES`
- `claudebox.sh` gains a `--max-concurrent-passes` passthrough and nothing else
- docs across `README.md`, `.env.example`, `CLAUDE.md`, `HISTORY.md`

### Explicitly out of scope

Named so they do not creep in:

- `claudebox.sh`'s missing `--plan-persona` and `--plan-label`, and its stale
  `--help` text describing `--persona` as if there were one persona set
- persisting the session map across container restarts
- the four-copy `_gh_stanza` drift problem, though the port consolidates the
  stanzas into one module, which makes it a smaller problem than it was
- reconciliation across personas, which remains phase 2 in the existing docs
- `code/_shared.md` and `plan/_shared.md` being byte-identical and untested for
  drift

### Rollout

Phase A merges on its own, with both bash suites and the new unit tests green,
and `MAX_CONCURRENT_PASSES` not yet in existence.

Phase B merges after, defaulting to unlimited fan-out. The shipped default is
then set from observed behavior against a real repo on a Max20 plan rather than
from a guess.

## Open risks

- **The `.git` inode chmod is a chokepoint, not a guarantee.** If a persona
  finds a mutating git path that does not take a lock file, it succeeds. The
  stanza is the other half of the defense for exactly this reason.
- **Concurrency multiplies instantaneous rate-limit pressure**, which is the
  reason `CLAUDE.md` gives for having declined it. Decision 4 accepts that and
  handles it after the fact rather than preventing it. If the limited-persona
  resume proves to churn, the fallback is lowering the default cap, not
  reverting the design.
- **A rewrite can drop behavior nobody wrote a test for.** The phase gate exists
  to catch that, but the existing suites do not cover everything the loop does.
  The unit tests widen coverage; they do not close the gap.
