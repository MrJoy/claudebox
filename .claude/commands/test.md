# /test — Tester lane

> Thin shim. The contract lives in the in-repo skill `factory-test`
> (`.claude/skills/factory-test/SKILL.md`), which is the single source of truth. This
> file exists only because `/orchestrate` launches the Tester lane pointed at it, on
> whichever harness and model the run chose, and it carries the project-specific notes
> that skill does not.

Arguments: $ARGUMENTS

Invoke the `factory-test` skill now, passing `$ARGUMENTS` through
unchanged, and follow it exactly.

If your harness has no Skill tool (Codex is one), read
`.claude/skills/factory-test/SKILL.md` in full and follow it as if it had been invoked. Where
`$ARGUMENTS` was not substituted, your arguments are the text after "Arguments:" in
your launch prompt.

One project-specific note, and it is a prohibition rather than a gate: this repo's
judging suite is the five suites `./test-python.sh`, `./test-providers.sh`,
`./test-personas.sh`, `./test-shim.sh` and `./test-launcher.sh`. **The Validator runs
those and judges; you do not.** You may execute a test you wrote to confirm it is
well-formed (`./test-python.sh -p 'test_<name>.py'`, or one case of a bash suite by its
label filter); that is a check on your own artifact, not the observation that decides
whether the change is good. That run is the Validator's.

Tests follow this repo's conventions. Python cases are stdlib `unittest` under `tests/`,
in files named `test_*.py`, each starting with `import _path  # noqa: F401` so that
`reviewer/` is importable by plain module name. They stub at seams (subprocess, `gh`,
the filesystem in a `tempfile.mkdtemp()`), never shell out to Docker or the network, and
never point anything at this checkout's own `.git` (`tests/_path.py` fails the run if a
test leaves it read-only). Launcher cases go in `test-launcher.sh` in its own style:
`launch LABEL WITH_KIN -- VARS -- ARGS` then `expect LABEL RC -- substrings`, every case
under `--dry-run`, under `/bin/bash`, which is 3.2 on macOS. No pip packages.

**Work from your brief, never from the plan.** This repo's implementation plans under
`docs/superpowers/plans/` hold each task's implementation code and its test code side by
side, so opening one shows you the implementation your oracle must not come from. The
Validator derives a brief for your lane and hands you its path; if you open a plan
anyway, say so and stop.
