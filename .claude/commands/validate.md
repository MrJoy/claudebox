# /validate — Validator lane

> Thin shim. The contract lives in the in-repo skill `factory-validate`
> (`.claude/skills/factory-validate/SKILL.md`), which is the single source of truth. This
> file exists only because `/orchestrate` launches the Validator lane pointed at it, on
> whichever harness and model the run chose, and it carries the project-specific
> overrides that skill does not.

Arguments: $ARGUMENTS

Invoke the `factory-validate` skill now, passing `$ARGUMENTS` through
unchanged, and follow it exactly, with the overrides named below.

If your harness has no Skill tool (Codex is one), read
`.claude/skills/factory-validate/SKILL.md` in full and follow it as if it had been invoked. Where
`$ARGUMENTS` was not substituted, your arguments are the text after "Arguments:" in
your launch prompt.

Project-specific gates this repo requires before you render any verdict. The judging
run of them is yours, and only your own record counts as one: if no line in your record
shows you ran them, they were not run. The Coder runs none of them. The Tester may run a
filtered case of the tests it wrote, to check its own artifact is well-formed (`/test`
says so); that run is in the Tester's log, it judges nothing, and it is never evidence
for the verdict.

- `./test-python.sh`: the `unittest` suite under `tests/`.
- `./test-providers.sh`, `./test-personas.sh`, `./test-shim.sh`, `./test-launcher.sh`:
  the bash suites. `test-providers.sh` and `test-personas.sh` need a bash 4+ on `PATH`
  and find one themselves.
- `python3 -m py_compile reviewer/*.py` and `bash -n entrypoint.sh claudebox.sh`.
- Whatever further gate the approved plan names for the slice in hand (Phase 1's schema
  check, `python3 tools/gen-config-schema.py --check`, is one).

Read the doctrine, `~/jmc/tools/HARNESS.md`, before your first dispatch. This repo has
no harness-workflow guide of its own.

## Trusted intent in this repo

This repo has no issue tracker and no signed-artifact ledger. What stands in for the
signed spec is the human-approved design spec under `docs/superpowers/specs/` and the
human-approved implementation plan under `docs/superpowers/plans/`, including the plan's
Global Constraints, Decided-here list and Review Focus. Cite the human's approval of each
by commit or transcript. `CLAUDE.md` is binding architecture and constraint documentation
for every change (its safety boundaries above all), but it states what the system is, not
what this change is for. When the plan was approved in a session, Phases 1 to 3 collapse
to one confirmation with the human against those documents; they are not skipped.

## Promotion here is a commit on the run's feature branch

Work happens on the feature branch the run names (Phase 1 of the hosted App:
`feat/hosted-phase1`). "Promote" means you commit the accepted slice on that branch after
the human signs the verdict. Nothing is pushed and no pull request is opened by you;
both are the human's call, and the pull request is where claudebox reviews its own
change. Commit messages follow the repo's style, as `git log` shows it: `<area>: <a short
lowercase summary>`, then a blank line, a short body, and the attribution trailers the
session gives you.

## What this project overrides in `factory-validate` — deliberately; do not restore it

- **Phase 4's dispatch of the Coder and Tester.** Here `/orchestrate` owns lane launch:
  the `coder` and `tester` windows were opened as separate agent processes, each on the
  harness and model the run chose (recorded in `.orchestrator-runs/<run>/lanes.txt`),
  before you were launched. Record those models in your verdict. You address them with `tmux send-keys` into their window.
  You do not invoke `factory-engineer` or `factory-test`, and you do not spawn them as
  `Agent()` subagents. Dispatching them yourself produces a second Coder and a second
  Tester: you would judge work the orchestrator holds no receipts for, while the lanes
  it launched sit holding no spec. Everything else in Phase 4 stands — both lanes get
  the same spec, neither receives the other's output, and the Coder's upward paths
  (question, failure report, specification defect) stay open.
- **The verdict's isolation line.** This copy of the skill carries no spoke channels and
  no channel probe: coordination is `tmux send-keys` into a named window, and there is
  nothing to encrypt. Report the boundary stated in `/orchestrate`'s tmux section, in
  these words: *no relay or notification path between the lanes, and separate worktrees;
  lanes can still address and read each other through the shared `tmux` server, the run
  directory, the process table and the kindex graph, and nothing prevents it.*
- **Phase 4's assembly.** The Coder and the Tester work in their own detached worktrees,
  which `/orchestrate` created. Before you run the gates, create an assembly worktree at
  the run's base commit (`.orchestrator-runs/<run>/base-sha.txt`), copy each lane's changed
  and new files into it, and run every gate there. Record a digest of every assembled file.
  On the human's signature, commit exactly those files on the feature branch, and check
  their digests match before you do. Leave the worktrees for `/orchestrate` to remove.

## Who may send, and how a lane answers

Two parties send into a lane window: the orchestrator, which relays, and you, which is
how Phase 4 works at all. The Coder and the Tester are told to send to nobody. Nothing
technical stops a lane from addressing another through the shared `tmux` server; a lane
that does is an incident to record, not a channel.

So a lane's upward path is its own window. You ask by `send-keys` into that lane's
window; the answer comes back in that lane's pane and in its
`.orchestrator-runs/<run>/<lane>/<lane>.log`, and you read it with `capture-pane` or by reading
the log. **That axis is the intended channel, not a leak.** The leak the boundary text
forbids is the Coder obtaining the tests, or anything else the Tester wrote, by any
route — including a quotation of them in your own lane's log. Reading the window of a lane you are judging is your job.

Keep all three of the Coder's upward paths open on that axis, and say in your verdict
which of them fired: a question, a failure report, and a specification defect. A spec
defect in particular is yours to resolve or escalate; it is never the Coder's to route
around by guessing.

## Split a superpowers plan into two briefs before dispatch

This repo's implementation plans come from `superpowers:writing-plans`, which writes
each task's test code and its implementation code side by side in one document. Handing
that document to either lane breaks the separation this whole arrangement exists for:
the Coder would read the tests it is judged by, and the Tester would read the
implementation its oracle must not come from.

So before Phase 4, derive two briefs from the plan, write each into this run's
directory, the Coder's as `.orchestrator-runs/<run>/coder/brief.md` and the Tester's as
`.orchestrator-runs/<run>/tester/brief.md`, and hand each lane only the path to its own:

- **Coder brief**: the plan with every test file's contents and every
  "Write the failing test" and "Run ... test" step removed. Keep the header, Global
  Constraints, File Structure, each task's Files and Interfaces blocks, and the
  implementation code. Replace each task's test-first steps with a note that the
  Tester owns them.
- **Tester brief**: the design spec, the plan's header, Global Constraints, Review
  Focus, File Structure, each task's Interfaces block, the fixture steps, and the
  plan's test code as the starting oracle. Remove every block of implementation code.

The plan's test code was written against the spec before any implementation existed,
so it is a legitimate starting oracle for the Tester, which may extend or correct it
with citations. Record in the verdict that the briefs were derived and how. Each lane's
own instructions forbid opening the plan itself; tell both lanes that in the dispatch.
