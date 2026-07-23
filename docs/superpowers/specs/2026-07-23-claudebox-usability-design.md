# claudebox usability improvements — design

**Date:** 2026-07-23
**Scope:** `claudebox.sh` launcher (host-side), plus docs (`README.md`, `.env.example`).
**Goal:** Make it ergonomic to keep **per-repo credentials in each repo's worktree** (a
`.env.claudebox` file) and to **launch and manage multiple claudeboxes simultaneously**,
one per repo, without hand-editing a shared `.env` before each launch.

All changes are confined to the host-side launcher and documentation. `entrypoint.sh`,
`Dockerfile`, and the container's runtime behavior are unchanged. The launcher must stay
macOS **bash 3.2**-safe (see `CLAUDE.md`).

## Motivation

Today the launcher reads a single shared `./.env` and names every container `claudebox`,
so only one can run at a time and the operator edits `.env` between launches. The target
workflow is: `cd` into a repo's worktree that carries its own `.env.claudebox`, run
`./claudebox.sh run --tail`, and get a container named after that repo — repeatable across
many repos at once.

## Features

### 1. Env-file auto-selection

When `--env-file` is **not** passed, select from the **current working directory** in this
order:

1. `.env.claudebox`
2. `.env`

`.env.claudebox` is preferred specifically so claudebox never has to co-opt or pollute a
project's own `.env` setup — a repo can carry claudebox credentials in a dedicated file
that sits alongside, and takes precedence over, whatever `.env` the project already uses.

If neither file exists, the nominal default remains `.env` so that `run`/`test` still fail
with the existing "env file not found" message from `build_run_flags`. An explicit
`--env-file PATH` disables inference entirely and is used verbatim (no announcement).

### 2. Repo inference

The default remains `REPO="$PWD"`. The change is that when `--repo` was not given, this
inference is **announced** (see §4). `--no-repo` still suppresses the mount; name
derivation (§3) can still read the cwd's git remote in that case.

### 3. Container-name namespacing — `claudebox--<org>--<repo>`

When `--name` is **not** given, derive the container name from the org/repo, trying these
sources in order:

- **a.** `GITHUB_REPOSITORY=org/repo` parsed from the resolved env file (if it exists and
  sets the variable). This is the primary source: it matches what the container will
  actually review.
- **b.** else `git -C "$REPO" remote get-url origin`, parsing both SSH
  (`git@github.com:org/repo(.git)`) and HTTPS (`https://github.com/org/repo(.git)`) forms.
- **c.** else **die** with a clear message pointing the operator at `--name` or at setting
  `GITHUB_REPOSITORY` in the env file.

Transform `org/repo` into the container name: strip a trailing `.git`, replace `/` with
`--`, and prefix `claudebox--`. Example: `mrjoy/hordes-of-orcs-next` →
`claudebox--mrjoy--hordes-of-orcs-next`. Case is preserved; the other characters GitHub
allows in names (`_ . -`) are already Docker-legal and pass through unchanged.

An explicit `--name` bypasses derivation entirely (and its sources are not consulted, so
`--name` works even when neither the env file nor a git remote yields an org/repo).

### 4. Loud announcements of inferences

A dedicated `announce()` helper prints a visually distinct banner to **stderr** for each
inference that was actually made:

- env file auto-selected (which file, and that it was chosen over the alternative)
- repo inferred from cwd (the path)
- container name derived (the resulting name **and** which source produced it: env file vs
  git remote)

When the corresponding value was supplied explicitly by a flag, nothing is announced for
it.

### 5. `--tail` flag (applies to `run`)

`--tail` makes `run`, after the detached `docker run` succeeds, immediately exec
`docker logs -f "$NAME"` — i.e. the equivalent of following up with the `logs` command.
Ctrl-C stops following the logs but leaves the (detached) container running, which is the
natural behavior of `docker logs -f`. `--tail` is only meaningful for `run`; it is a no-op
for other commands. Under `--dry-run`, both the `docker run` and the `docker logs -f`
commands are printed and neither is executed.

### 6. Subcommand targeting via the same inference

The env-file → repo → name derivation pipeline runs for `run`, `test`, `logs`, `shell`,
`stop`, and `status` — every command **except** `build` (which needs neither an env file
nor a container name). As a result, from a repo's worktree, `./claudebox.sh logs`,
`stop`, `status`, and `shell` all target that repo's container with no extra flags.
`--repo`, `--name`, and `--env-file` override the inference for any of them.

## Order of operations (per invocation)

After argument parsing, and for every command except `build`:

1. Resolve the env file (auto-select unless `--env-file` given; announce if inferred).
2. Resolve the repo (default `$PWD`; announce if inferred).
3. Derive the container name (unless `--name` given; announce with its source; die if no
   source yields org/repo).
4. Run the command as today, using the resolved `ENV_FILE`, `REPO`, and `NAME`.

Name derivation must gracefully tolerate a **missing** env file (subcommands like `logs`
don't require the env file to exist): a missing/incomplete env file simply falls through
to the git-remote source.

## Known limitation (intentional)

Env-file inference reads from **cwd**, not from `--repo`'s directory. Running
`--repo ~/elsewhere` from an unrelated cwd will not pick up that repo's `.env.claudebox`;
pass `--env-file` explicitly in that case. This matches the "from the cwd" intent and
keeps the common case (invoked from inside the worktree) simple.

## Documentation updates

- `usage()` / `--help`: document `.env.claudebox` precedence, `--tail`, and the derived
  per-repo container name (with a note that `--name` overrides it).
- `README.md`: describe the per-repo `.env.claudebox` workflow and running multiple
  claudeboxes at once; document `--tail` and the container naming scheme.
- `.env.example`: mention the `.env.claudebox` convention.

## Out of scope

- No changes to `entrypoint.sh`, `Dockerfile`, or in-container behavior.
- No change to hardening flags or the safety boundaries.
- No lowercasing/normalization of org/repo beyond `/`→`--` and `.git` stripping.
