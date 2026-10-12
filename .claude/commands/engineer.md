# /engineer — Coder lane

> Thin shim. The contract lives in the in-repo skill `factory-engineer`
> (`.claude/skills/factory-engineer/SKILL.md`), which is the single source of truth. This
> file exists only because `/orchestrate` launches the Coder lane pointed at it, on
> whichever harness and model the run chose. This is the factory **Coder lane**; if you
> arrived here by searching on the word "engineer", `factory-engineer` is the contract
> you want.

Arguments: $ARGUMENTS

Invoke the `factory-engineer` skill now, passing `$ARGUMENTS` through
unchanged, and follow it exactly.

If your harness has no Skill tool (Codex is one), read
`.claude/skills/factory-engineer/SKILL.md` in full and follow it as if it had been invoked. Where
`$ARGUMENTS` was not substituted, your arguments are the text after "Arguments:" in
your launch prompt.

One project-specific note, and it is a prohibition rather than a gate: this repo's
judging suite is the five suites `./test-python.sh` (unittest, under `tests/`),
`./test-providers.sh`, `./test-personas.sh`, `./test-shim.sh` and `./test-launcher.sh`.
**The Validator runs those and judges; you do not.** `python3 -m py_compile` and
`bash -n` on the files you touched, and scratch scripts in your own scratch directory,
are your tools; run them freely. Make that directory with `mktemp -d`, outside your
worktree: the Validator stages your whole tree (`git add -A`) and holds a lane back when
anything in it is outside your change set, so a scratch file left in the worktree either
stalls the slice or, listed in the change set, lands on the branch. Running any of the five suites, or opening any file
under `tests/` or any `test-*.sh`, is not. The judging run, and the word "done", belong
to the Validator.

**Work from your brief, never from the plan.** This repo's implementation plans under
`docs/superpowers/plans/` hold each task's implementation code and its test code side by
side, so opening one shows you the tests you are judged by. The Validator derives a
brief for your lane and hands you its path; if you open a plan anyway, say so and stop.
The brief's file appearing, or changing, is not your signal to start: only a DISPATCH
message in your own window is. Do not poll or watch the run directory.

**The kindex graph is shared with the Tester.** Search it as your skill says, but do not
`add` to it until the run closes: a note about how your implementation behaves is
a statement of what to assert about it, and the Tester can read it. List your captures in
your hand-over instead.
