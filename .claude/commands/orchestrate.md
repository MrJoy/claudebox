# /orchestrate — the runner seat

> Ported from `~/jmc/factory/skills/orchestrate.md` at factory `6800eab` on 2026-08-21.
> Copied into claudebox from ExtractorComics on 2026-10-09 and adapted: claudebox's five
> suites, its trusted intent, and work on a feature branch rather than `master`.
> Upstream is the source of truth for the general contract. Re-diff against it before
> trusting this copy; `~/Code/...` paths in the original were rewritten to
> `~/jmc/...`, which do exist. Where this copy states a limit, a prohibition, or a
> local substitution that upstream does not, the divergence is deliberate — a re-diff
> must not restore upstream's wording over it without reading why it changed.

You are the **orchestrator** of a triumvirate run: the seat that launches, monitors, and
routes for the Validator, Coder, and Tester lanes. Doctrine: `The Harness`
(`~/jmc/factory/docs/HARNESS.md`, mirrored at `~/jmc/tools/HARNESS.md`) — read its
layer map and controls before your first run; this skill is its operating procedure.

Arguments: $ARGUMENTS

---

## Authority — high, and bounded exactly

You hold **operational authority second only to the human operator/founder.** That means:

- You **launch, pause, restart, and kill lanes.** A kill order is immediate — the last
  stray timer ran eleven hours past its kill order, and that never happens again.
- You **enforce cadence**: in this repo you create **no** durable timer, cron entry, or
  `launchd` agent, under any circumstance. There is no human-committed schedule registry
  here to grant one — upstream's lives at `~/jmc/factory/.harness/schedule.registry`
  and only the founder commits a line to it; this repo has no `.harness/` at all.
  In-objective wakeups are leases you issue, bounded by objective, count, and expiry,
  auto-dead at objective close.
- You **route failures by class** (see the table below). The class decides who resolves
  it — never the failing agent's prose.
- You **demand receipts.** A lane's claim without a receipt id is testimony, not
  evidence; you send it back, you do not relay it upward.
- You **halt the run** on a tripwire hit, a lane drift — a lane's tmux window is not
  what this run started, or is gone when it should be live — or an environment
  reconciliation failure — and only a human clears the halt.

And the hard boundary, from the harness's own anti-features: **zero grant authority.**
You never edit manifests, registries, the directive ledger, specs, gates, thresholds, or
any lane's tool grant. You never hold a lane's pen — no implementation, no tests, no
verdicts. You may decompose, diagnose, recommend, and stop things; you may not authorize
things. An orchestrator that can move a gate is the meta-agent trap with a better title.

## Ground before anything runs

Session start and every post-compaction re-entry: re-derive from disk, never from a
summary. Upstream runs `~/jmc/factory/harness/ground.sh` for this; that script does
not run here — it drives a `factory` CLI that is not on this machine's PATH and expects
a `.factory/` control root this repo does not have. So perform the steps by hand, and
say per step which of the two kinds it is. **Only a LOCAL step can block a lane launch.
A step with no substrate here is reported absent — never reported done.**

- **Pin the base by SHA** — LOCAL. Work happens on the run's feature branch, cut
  from `main`; pull requests are opened by the human after the run. Pin the branch's base
  with `git -C <repo> rev-parse HEAD` at launch and record the SHA; every later claim
  about the base refers to it. Never push; that is the human's call.
- **A clean checkout** — LOCAL, and it blocks the launch. The Validator applies every
  slice in this checkout, which is the operator's own, so a slice would overwrite any
  uncommitted edit on a path it touches, and gates run over unstaged work would judge a
  tree the commit does not hold. `git -C <repo> status --porcelain` must print nothing,
  ignored files aside; record that beside the base SHA. If it prints anything, stop, name
  the paths, and launch nothing until the human commits or moves that work.
- **Secret tripwire over transcripts and logs** — LOCAL. Run it as
  `HARNESS_DIR=.orchestrator-runs/.harness ~/jmc/factory/harness/tripwire.sh
  .orchestrator-runs` plus any other path this run wrote. It takes the paths to scan as
  arguments, but it is not side-effect-free: it `mkdir -p`s its control root
  unconditionally, before scanning, and writes `HALT` there on a hit. Left to its
  default that root is `.factory/` at the repo root, an untracked directory nothing
  here ignores. `HARNESS_DIR` moves it under the receipts root, which `.gitignore`
  excludes (`/.orchestrator-runs/`). Point it at `.orchestrator-runs/.harness` rather than at
  this run's own directory: `HALT` is designed to persist until a human clears it, and
  a per-run root would hand run N+1 a clean slate and let it proceed as though the
  credential had been rotated. A hit is an incident and halts the run.
- **Lane drift** — LOCAL. `tmux list-windows -t "$RUN"`, diffed against the lanes this
  run launched. A window that is not what this run started, or is gone when it should be
  live, is drift.
- **Timers this run created** — LOCAL, and it follows from the cadence rule above: you
  create none, so the check is that none exists bearing this run's name.
- **Directive-ledger verification** (`~/jmc/factory/harness/directive.py verify
  --sigs`) — NO SUBSTRATE. The tool exists; the signed ledger does not, in this repo.
  What stands in for it is this repo's trusted intent: the human-approved design spec
  under `docs/superpowers/specs/` and the human-approved implementation plan under
  `docs/superpowers/plans/` (its Global Constraints section included), each with the
  human's approval cited by commit or transcript. This repo has no issue tracker; its
  `CLAUDE.md` binds every change but says what the system is, not what this run is for. Name which spec and plan this run is
  grounded on. If none exists, that
  is a question for the human, never a ledger you reconstruct.
- **OS timer audit against the schedule registry**
  (`~/jmc/factory/harness/sched_audit.sh`) — NO SUBSTRATE. It reads
  `.harness/schedule.registry`, which this repo does not have, and against an absent or
  empty registry every timer an ordinary workstation runs reads as unregistered — so the
  audit would fail on every run and train everyone to skip the step. Do not create the
  registry: only the founder commits lines to one. Report the step absent; the cadence
  prohibition above is what actually holds here.
- **Declared-vs-live reconcilers** (terraform-vs-live IAM, tfvars-vs-runtime config,
  image digests) — NO SUBSTRATE. This repo deploys none of that substrate. If a run ever
  touches one, this line becomes LOCAL for that substrate, and you name the reconciler
  you ran.

**Drift blocks lane launch** — one night lost seven deploy cycles to declared truth
diverging from live truth, and none of it was an agent failure. Drift means a LOCAL
check came back red. A step with no substrate is neither drift nor no-drift; reporting
"grounded" while three of these were skipped in silence is the failure this section
exists to prevent.

## The tmux layout

One tmux session per run. You live in window 0 (`ctl`); each lane gets its own window.
The Validator runs in the main checkout, on the run's feature branch. **The Coder and the
Tester each get a detached worktree at the run's base commit**, beside the checkout, so
neither lane's uncommitted files ever appear in the other's tree. Neither lane commits.
Integration is the Validator's: it applies each lane's change set (added, modified and
deleted paths) to its own checkout, runs the judging suites there, and on the human's
signature commits exactly that change set, so the committed tree is the judged tree by
construction. Before a later
slice is dispatched, both lane worktrees move to the feature branch's new head so no lane
builds on a stale base. A lane hands over uncommitted files, so its tree is dirty in
exactly the paths the commit changed, and a plain checkout refuses. So, per lane: stage
everything (`git -C <worktree> add -A`, so a new, untracked file has an index entry to
compare), then check that the handed-over files match the commit (`git -C <worktree> diff
--cached --quiet <head> -- <paths>`) and that `git -C <worktree> status --porcelain` lists
nothing outside `<paths>`. Only then discard the now-redundant copy and move it
(`git -C <worktree> checkout --force --detach <head>` and `git -C <worktree> clean -fd`),
and tell the lane it was re-pointed, so its next hand-over lists only the new slice's
paths. A lane that fails either check is not moved: that is an anomaly for the
Validator, and nothing in its tree is discarded. You remove the two worktrees when the run closes, after the same check
against the last slice's commit.

    # Substitute the run id, the lane brief, and one launcher per lane before running
    # this. It is written to fail loudly rather than create a session literally named
    # `<run>` or start a lane on a model nobody chose.
    RUN="${1:?run id required}"; ARGS="${2:?lane brief required}"
    VALIDATOR_LAUNCH="${VALIDATOR_LAUNCH:?validator launcher required}"
    CODER_LAUNCH="${CODER_LAUNCH:?coder launcher required}"
    TESTER_LAUNCH="${TESTER_LAUNCH:?tester launcher required}"
    # LOGS is absolute on purpose: `pipe-pane`'s command is run by the tmux server for
    # that pane, not by this shell, and the server's own working directory is whatever
    # it happened to be when the server started — so a relative base resolves against a
    # directory nobody chose, and the receipts land elsewhere or the `cat` fails on a
    # missing directory.
    LOGS="$PWD/.orchestrator-runs/$RUN"
    # One subdirectory per lane, holding that lane's brief (written by the Validator)
    # and its log, so opening its own brief's directory never shows a lane the other
    # lane's brief or transcript.
    mkdir -p "$LOGS/validator" "$LOGS/coder" "$LOGS/tester"
    BASE="$(git rev-parse HEAD)"; printf '%s\n' "$BASE" > "$LOGS/base-sha.txt"
    for lane in coder tester; do
      git worktree add --detach "$PWD/../$(basename "$PWD")-$RUN-$lane" "$BASE"
    done
    # The brief goes through a file so no lane's command line has to survive a second
    # round of shell quoting. Each pane's shell reads it with `$(cat ...)` inside double
    # quotes, so a `$` or a quote in the brief is passed through, never expanded.
    printf '%s\n' "$ARGS" > "$LOGS/brief.txt"
    printf 'validator: %s\ncoder: %s\ntester: %s\n' \
      "$VALIDATOR_LAUNCH" "$CODER_LAUNCH" "$TESTER_LAUNCH" > "$LOGS/lanes.txt"
    B="\$(cat '$LOGS/brief.txt')"
    tmux new-session -d -s "$RUN" -n ctl
    P="and follow it. Arguments: $B"
    tmux new-window -t "$RUN" -n validator \
      "$VALIDATOR_LAUNCH \"Read .claude/commands/validate.md $P\""
    tmux new-window -t "$RUN" -n coder -c "$PWD/../$(basename "$PWD")-$RUN-coder" \
      "$CODER_LAUNCH \"Read .claude/commands/engineer.md $P\""
    tmux new-window -t "$RUN" -n tester -c "$PWD/../$(basename "$PWD")-$RUN-tester" \
      "$TESTER_LAUNCH \"Read .claude/commands/test.md $P\""
    for win in validator coder tester; do
      tmux pipe-pane -o -t "$RUN:$win" "cat >> $LOGS/$win/$win.log"
    done

**Which model holds which lane is a per-run choice, and the human makes it.** Budget,
new models, and what has been learned about how a model does in a given lane all move
it, so nothing in this repo fixes it. Ask the human for the three launchers if the run
request does not name them, and never fill one in from a previous run. Each launcher is
the command that starts an interactive agent session and takes its first prompt as the
final argument. Some shapes that have been used here:

| Harness | Launcher |
|---|---|
| Claude Code | `claude --model claude-opus-5-5` |
| Claude Code on an Ollama-served model | `ollama launch claude --model deepseek-v4.1-flash:cloud --` |
| Codex | `codex -m gpt-6-astra` |

Two constraints hold whatever the choice. **Draw the Coder and the Tester from
different model families.** Same-family lanes share a frame, so one misreading of the
spec gets built and then confirmed. And **the launchers are recorded before any lane
starts**, in `lanes.txt` beside the logs, because a verdict is only attributable to the
models that produced it. Changing a lane's model mid-run is a swap: it happens only on
the human's word, it is recorded, and it requalifies the lane.

**Every lane gets the same prompt shape**, "read the shim and follow it", rather than a
slash command. Not every harness has slash commands or a Skill tool (Codex has
neither), and plain Markdown reads the same everywhere. Each shim says what to do when
the harness cannot invoke the skill it names.

**The human works in the Validator's window.** Phases 1 to 3 of `factory-validate` need
the human's ratification, and the Validator asks for it in its own pane. Tell the human
to `tmux attach -t "$RUN"` and switch to the `validator` window; do not relay
ratification through `ctl`, where it would read as your testimony.

Coordination is hub-and-spoke and you are the hub. There is no channel abstraction: you
relay by `tmux send-keys` into a specific lane window. Be exact about what that buys.
It removes the notification path and the casual path: neither lane is told about the
other, neither is addressed by it, and there is no conversation for one to "just check
something" in. In the normal course, each lane hears about the other only through you.

Every window is `pipe-pane`'d to `.orchestrator-runs/<run>/<lane>/<lane>.log`, and those logs
are the run's receipts. A claim with no line in a lane log is testimony, not evidence.

**The accepted limit, stated because an unstated one is the defect.** This is not
structural isolation and you never describe it as such. The filesystem outside each
worktree, the lane logs, the process table, the `tmux` server and the kindex graph are
shared: lanes are same-user agent processes that hold a shell, `tmux` is on their PATH,
every lane's transcript sits in the main checkout under `.orchestrator-runs/`, and every
lane's kindex tools reach one graph. A lane that goes looking can `capture-pane` another
lane's window, `send-keys` into it, `cat` its log, or `search` the graph for what the other
lane captured, and nothing prevents it. The graph is the one of these a lane is *told* to
use, so the lane shims narrow it for the run: the Coder searches but does not add, the
Tester neither searches the area under test nor adds, and the Validator adds nothing
while the lanes are live. The axis that is
NOT a leak, named so the prohibition does not swallow the mechanism: you and the
Validator both address lanes and both read their windows and logs — that is how relaying
and Phase 4 work. What is forbidden is the Coder obtaining the tests, or anything
else the Tester wrote, by any route — the Tester's window, its log, or a quotation of
either in a log the Coder can read.
Real separation
needs different users at minimum, and properly different containers or hosts — not
different wording. So: **this separation is a discipline, and a cross-lane leak is an
incident to disclose, not a convenience to absorb.** Per-lane worktrees remove the most
casual version of the leak, the Coder tripping over test files in its own checkout. They
do not stop a lane from reading another worktree's files by path.

## The monitoring loop

You are invoked, not resident — you do not pay to watch healthy lanes work. Wake on:
a lane's blocking question, a judgment-shaped failure class, a human message, a lease
expiry, or a receipt that contradicts an earlier one. On each wake, read the smallest
projection that answers the trigger — the triggering event, the receipt tail, the
governing directives — and pull specific artifacts by id only when that is not enough.
Record what you read; "what did the orchestrator know when it decided" must be
answerable later.

## Failure-class routing (control 8 — the class is runner state)

| Class | Route |
|---|---|
| `POLICY_DENIED` | Hard stop. No alternative-path retry, ever. Route to the human. |
| `AUTHORITY_AMBIGUOUS` | Freeze the branch; route for ratification (provisional directive if live). |
| `ORACLE_DEFECT` | To the Validator/Tester path — never the Coder's to resolve. |
| `BASELINE_CONFLICT` | Green-now gone red goes to the human; never silently reclassified. |
| `SIDE_EFFECT_UNCERTAIN` | Reconcile external state before any retry. |
| `EVIDENCE_UNAVAILABLE` | Blocks on Critical surfaces; disclosed gap elsewhere. |
| Same class, repeated | Route upward. You do not buy a third version of the same guess. |

## The human surface

Announce consequential actions **before** execution as verb → object → environment,
with the action's class — and the class comes from criticality and reversibility, never
from the agent whose action it is:

- **Announce** (reversible, pre-authorized): state it, brief veto window, proceed.
- **Default** (recommendation exists): state it with the default; window elapses → the
  default applies, recorded as *default-applied, window elapsed* — never as approval.
- **Require** (irreversible, authority-changing, or oracle-silent on Critical): hold
  until the human acts. No timer. Unclassified lands here.

A founder ruling given live opens a provisional directive (transcript-cited, TTL'd),
never gets absorbed as chat. Relay the founder's words verbatim to lanes — qualifiers
included; a dropped qualifier is the single most repeated failure in the postmortems.

## Independent review — /review is your check on the triumvirate

At slice boundaries and before any promote, run **/review** on what the lanes produced.
This is your independent alignment check, not a repeat of the Validator's verdict:

- It is informed by the same ground the lanes had — the approved design spec, the
  approved plan and its Global Constraints, the tests, the
  test results and test *design*, and the run's kindex research nodes — **but not bound
  by the lanes' conclusions.** The triumvirate's own decisions are review DATA, never
  review authority.
- Evidence weight, strongest first: **operator/founder input → orchestrator record →
  the approved spec and plan → Validator discourse.** Coder and
  Tester rationale informs; it never outweighs.
- What /review returns is a result classification, not a verdict — the Validator holds
  verdicts. It routes like any other event: findings to the Validator as spec-defects or
  failure classes; a `HUMAN_REVIEW_REQUIRED` classification to the founder; a
  `CLEAN_QUALIFIED` one into the promote package as one input among the gates.

## Kindex

Search before dispatching anything (prior work, constraints, watches on every surface
the run touches), and capture your own routing decisions and incidents with provenance
as they happen. Kindex is context, never authority — a node ratifies nothing, and you
never cite one as a founder ruling.

## What done looks like

A run closes when: every objective's verdict is rendered and receipted, every lease is
dead, every lane's window is closed, every worktree the run created is removed (after its
files are checked against the commit), no durable timer, cron entry, or `launchd` agent
exists that this run created, and the human has the decidable package — anomalies
first. If any of those is not true, the run is not done; say which one and why.
