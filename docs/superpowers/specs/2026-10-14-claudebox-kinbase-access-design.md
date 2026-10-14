# Read-only kinbase context for reviewers: design

**Date:** 2026-10-14
**Status:** approved in conversation, spec awaiting review

## Problem

Kinbase (wandercom/kinbase) carries an organization's architecture and
operational knowledge into a coding session. A Company service holds signed
Company facts, the repo's `.kin/events` holds signed Codebase facts, and a
Claude Code `SessionStart` hook projects the facts that apply to the repo into
the model's context. A claudebox reviewer gets none of it, so it reviews a
change without knowing what the organization has already ruled about it.

The host this is built for runs several Companies under the named
`[companies.<name>]` config form from wandercom/kinbase#30, which picks one
Company per repository from its origin, its held certificate and its pins. It
also runs wandercom/kinbase#31, which keeps the checkpoint hooks fast. Neither
PR has merged, and the two conflict in `session.rs` when merged onto `main`.

Facts about kinbase at `a5b323e` (the resolved merge of both PRs) that shape
this design, read from its source:

- `SessionStart` is the only hook event that puts facts into the model's
  context. It returns them as `hookSpecificOutput.additionalContext`,
  prefixed `UNTRUSTED_EVIDENCE_NOT_INSTRUCTIONS`, capped at 32 items and
  128 KiB. It sends no POST. When a 200 ms TCP probe of the Company URL
  connects, it spawns a detached `kinbase status` or `fsck` worker that
  fetches a snapshot and takes lock files in the repo's `.git`.
- `UserPromptSubmit` classifies the prompt and admits candidates on the
  spot: a Company candidate is a `POST /facts` with the facts token, a
  Codebase candidate is written into `.kin/`. `Stop`, `SessionEnd` and
  `PreCompact` checkpoint the session and re-admit pending candidates the
  same way. The facts token carries `facts:read` and `questions:write`, and
  `POST /facts` is gated by the signer rather than the token's scope, so the
  token is not a read-only credential.
- `KINBASE_COMPANY_URL`, set to anything, skips the probe and the worker and
  makes every Company call refuse. Company facts then come only from the
  cache, which `SessionStart` reads without the network.
- `SessionStart` needs, in its cache, `facts-cache.sqlite3` and
  `repositories/<uuid>/certificate.json` (the latter written only by
  `repo init`). It needs a recorded proof clock, without which it injects
  nothing. At launcher load it chmods `cache_root` and `data_root` to 0700,
  so both must be owned by the running uid. Token and key files must be
  regular files, not symlinks, mode 0600 or 0400. The launcher refuses to
  load when it inherits a file descriptor other than 0, 1 and 2.
- The facts token is loaded and validated at startup but `SessionStart`
  never sends it, since it sends nothing.
- `kinbase doctor --repo PATH --json` reports `company_selection`: the form,
  each configured Company's `cache_root` and file presence, the evidence, and
  `outcome.selected`.

## Decisions

1. **Read-only.** A review session receives Company and Codebase facts and
   produces nothing bound for a shared store. Only `SessionStart` is wired.
   The reviewer is prompt-injectable and runs with permissions skipped, so
   any write path kinbase opens is a write by an untrusted agent.
2. **Offline in the container.** `KINBASE_COMPANY_URL` is always set, so the
   container never reaches a Company service and needs no route to the
   host's loopback ports. Company facts come from a copy of the host's cache.
3. **No credential crosses into the container.** The facts token is replaced
   by a placeholder file that passes kinbase's startup validation. The admin
   token is never read. The root public key is the one Company file mounted,
   and it is public.
4. **Optional, and automatic when present.** The launcher opts in when
   `kinbase` is on the host PATH, a repo is mounted, and the repo carries
   `.kin/kinbase.toml`. Missing any of the three is silent: none of them
   says the operator expected kinbase here. A host that has kinbase but cannot resolve a usable Company for
   the repo logs one WARN and launches without it. `--no-kinbase` opts out.
5. **The host's kinbase picks the Company.** The launcher runs
   `kinbase doctor --repo REPO --json` from inside the repo and reads
   `company_selection.outcome.selected`. It does not reimplement #30's
   selection rules, which refuse on ambiguity and on unreadable evidence for
   reasons a shell port would lose. Under the single `[company]` form the
   one Company is used as-is.
6. **One Company per container.** The launcher mounts only the selected
   Company's cache and root key. The container's config uses the single
   `[company]` form, so the in-container kinbase never runs selection.
7. **Built from a pinned fork commit until the PRs merge.** The image builds
   `KINBASE_REPO`@`KINBASE_REF`, defaulting to `MrJoy/kinbase` at `a5b323e`
   (branch `combo/multi-company-plus-checkpoint-perf`). Once #30 and #31
   merge, both defaults move to `wandercom/kinbase`.

## Components

### Image (`Dockerfile`)

A builder stage on `rust:1.99-bookworm` clones `KINBASE_REPO`, checks out
`KINBASE_REF` by full SHA, and runs `cargo build --locked --release -p
kinbase`. rusqlite is built with its bundled SQLite and there is no OpenSSL,
so the stage needs only the toolchain's C compiler. The final stage copies
the binary to `/usr/local/bin/kinbase`, root-owned like the other tools the
reviewer runs but must not edit, and fails the build unless
`kinbase --version` runs. The `personal-recall` feature stays off. `git` is
already in the image.

### Launcher (`claudebox.sh`)

`resolve_kinbase` runs beside `resolve_kindex` and sets `KINBASE_MOUNTS`.

- Skips silently when `--no-kinbase`, `--no-repo`, `kinbase` not on PATH, or
  no `.kin/kinbase.toml` in the repo.
- Otherwise runs `(cd "$repo_abs" && kinbase doctor --repo . --json)` and
  reads the selected Company's name and `cache_root`. It finds that
  Company's `root_public_key_file` in the host config (the plan settles
  how: the doctor report lists only whether the file exists).
- Requires `cache_root/facts-cache.sqlite3` and a
  `cache_root/repositories/<uuid>/certificate.json` for the repo's UUID from
  `.kin/kinbase.toml`. Without them `SessionStart` would project nothing, so
  the launcher says why rather than mounting a cache that cannot help.
- Mounts `cache_root` at `/kinbase-src/cache:ro` and the root key at
  `/kinbase-src/root.pub:ro`, and announces the Company name, its cache path,
  and that the reviewer can read every Company fact in it.
- `--kinbase-company NAME` forces a configured Company and turns every
  resolution failure into a `die`, mirroring `--kindex-profile`.
  `--no-kinbase` with `--kinbase-company` is a contradiction and dies.
- Bash 3.2-safe like the rest of the file.

### Entrypoint (`entrypoint.sh`)

When `/kinbase-src` is present and the binary runs:

- `unset KINBASE_ENABLED` before the check, as with `KINDEX_ENABLED`, so an
  env-file value cannot make the prompt promise facts nobody wired.
- Writes `$HOME/.config/kinbase/config.toml` (mode 0600) in the single form:
  `[personal] data_root` under `$HOME`, `[company]` with a placeholder `url`,
  `facts_token_file` pointing at a placeholder token written at mode 0600,
  `root_public_key_file` pointing at a 0600 copy of the mounted key (a bind
  mount does not carry the mode kinbase requires), and `cache_root` at the
  snapshot directory.
- Exports `KINBASE_COMPANY_URL` for every pass.
- Registers exactly one hook, `SessionStart` running
  `/usr/local/bin/kinbase hooks dispatch claude SessionStart`, in the
  container user's `~/.claude/settings.json`, merged into whatever the image
  baked there.
- Adds the working clone to git's `safe.directory`.
- Exports `KINBASE_ENABLED=1` and `KINBASE_SRC` for the supervisor.

### Supervisor (`reviewer/kinbase_snapshot.py`)

Copies `/kinbase-src/cache` into the container-owned `cache_root` the
config names. It follows `graph_snapshot`'s copy discipline: the host keeps
`facts-cache.sqlite3` as a live WAL database, so the copy takes the database
and its `-wal`, accepts it only when no source file's size or mtime moved
during the copy, retries a fixed small number of times, runs
`PRAGMA quick_check` on the staged copy, and swaps it in. It copies the
repo's `repositories/<uuid>/certificate.json` and sets the directory to 0700.

It records a proof clock for the clone so `SessionStart` has one to judge
freshness by. The value comes from the snapshot's own `issued_at`, written
where kinbase reads a recorded clock in the working clone
(`.kin/local/proof-clock`). The plan checks whether a clock earlier than a
Codebase event's date hides that event, and if it does, picks the clock
from the later of the two.

It runs once before cycle 1, where a failure is a `ConfigError` because the
stanza has already promised facts, and once per cycle beside the kindex
refresh, before the cycle's `git fetch`, where a failure logs a WARN and
keeps the previous good copy.

### Prompts (`reviewer/_stanzas.py`, `reviewer/prompts.py`)

`KINBASE_STANZA` is appended to the four defaults only when
`KINBASE_ENABLED` is set. It says that Company and Codebase facts arrive at
session start, that a change violating one is a finding like any other, and
that nothing in them is an instruction. Operator overrides reach Claude
verbatim, as with the Linear and kindex stanzas. `--append-system-prompt`
is untouched: `SessionStart` fires on resumed passes too, so the facts do
not need the system prompt's survival across `--resume`.

## Error handling

| Where | Situation | Result |
|---|---|---|
| launcher, automatic | doctor exits non-zero, selection refused or empty, cache or certificate missing | WARN naming the cause, launch without kinbase |
| launcher, `--kinbase-company` | any of the above | `die` |
| entrypoint | `/kinbase-src` present, binary fails to run | `die`: the operator's host resolved a Company and the image cannot serve it |
| supervisor, startup | snapshot fails | `ConfigError` |
| supervisor, per cycle | snapshot fails | WARN, keep previous copy |
| hook, per pass | kinbase refuses or degrades | exit 3 by kinbase's own contract, which never blocks Claude; the pass reviews without facts |

## Exposure

The reviewer can read every Company fact in the selected Company's cache
and every Codebase fact in the repo, and can post any of them onto a PR
through `gh pr comment`. That is the same containment argument the kindex
design makes. A host whose Company partitioning puts unrelated
organizations' facts in one Company exposes them to every repo that
Company serves.

## Testing

- `test-launcher.sh`: a stub `kinbase` on PATH drives each arm: skip when
  absent, skip without `.kin/kinbase.toml`, mount on a selected Company,
  WARN on refusal, WARN on missing certificate, `die` under
  `--kinbase-company`, and the `--no-kinbase` contradiction.
- `test-providers.sh` or `test-personas.sh`: with `/kinbase-src` present,
  the settings file carries `SessionStart` and no other kinbase event,
  `KINBASE_COMPANY_URL` reaches `claude`'s environment, and no token from
  the host appears in the container's config.
- `test-python.sh`: the snapshot's stability check, retry and swap, the
  proof clock it writes, and the stanza's presence on defaults and absence
  on overrides.
- A live check against a repo onboarded with `repo issue` and `repo init`,
  since no repo on the development host carries `.kin/kinbase.toml` today.

## Verified during planning

- Whether `claude -p` hands its hook processes any descriptor beyond 0, 1
  and 2 in the container. kinbase refuses to load if it does.
- The placeholder token format that kinbase's startup validation accepts.
- That `kinbase doctor` reports `company_selection` under the single
  `[company]` form as well as the named one.
- How the launcher reads the selected Company's `root_public_key_file`.

## Out of scope

- Any kinbase hook other than `SessionStart`.
- Network access from the container to a Company service, and any use of
  the facts or admin token inside the container.
- Kinbase in the hosted GitHub App.
- Personal recall.
