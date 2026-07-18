# claudebox session export & path alignment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `--export-sessions` flag that exports the container's Claude Code review transcripts onto the host and files them under the same `~/.claude/projects/<encoded>` folder the host uses for that repo.

**Architecture:** The launcher (`claudebox.sh`) computes the host repo's Claude-project folder name (`sed 's/[^a-zA-Z0-9]/-/g'` over the absolute path), pre-creates it on the host, bind-mounts *only* that one folder read-write into the container, and passes the host repo path as `HOST_REPO_PATH`. The entrypoint clones and runs the review at that host path so Claude Code's cwd-derived project folder matches, and transcripts stream straight into the mounted host folder. A two-line Dockerfile change pre-creates the writable top-level roots the unprivileged user needs to recreate that path inside the container.

**Tech Stack:** Bash (host launcher must stay macOS bash-3.2-safe; entrypoint runs modern bash in-image), Docker, Claude Code CLI.

## Global Constraints

- `claudebox.sh` runs on the **host** where macOS ships **bash 3.2**: expand possibly-empty arrays as `${arr[@]+"${arr[@]}"}` (never bare `"${arr[@]}"` under `set -u`); no bash-4 features (no associative arrays, `${x,,}`, etc.).
- `entrypoint.sh` runs inside the image (modern bash) under `set -euo pipefail`; guard maybe-unset vars as `${VAR:-}`.
- The three safety boundaries are load-bearing and must be preserved: unprivileged `reviewer` user, read-only `/repo` seed, privilege-minimized token. Do **not** add `--read-only` root fs; do **not** run as root or introduce a privilege drop (blocked by `--cap-drop ALL`).
- The encoding rule is exactly `printf '%s' "$abs_path" | sed 's/[^a-zA-Z0-9]/-/g'` — one-for-one char map, no run-collapsing.
- No test framework exists. Verification is `bash -n <script>` for syntax, `./claudebox.sh --dry-run` for the assembled `docker` command, and targeted `docker build` / `docker run --entrypoint` checks.
- Spec: `docs/superpowers/specs/2026-07-17-claudebox-session-export-design.md`.

---

## File structure

- **Modify `Dockerfile`** — pre-create `/Users` + `/home` (owned by `reviewer`) so the host repo path is recreatable in-container; pre-create `/home/reviewer/.claude/projects` (owned by `reviewer`) so the narrow bind mount doesn't leave root-owned parents that block Claude Code's other `~/.claude` writes.
- **Modify `entrypoint.sh`** — when `HOST_REPO_PATH` is set, use it as the working-clone location (with graceful fallback); otherwise unchanged.
- **Modify `claudebox.sh`** — add `--export-sessions`: parse it, validate it needs a mounted repo, compute the encoded folder, pre-create it on the host, add the env + narrow mount (skipping the mount when `--mount-claude` already covers it), and document it in `usage()`.
- **Modify `README.md` + `.env.example`** — document the flag and the launcher-managed `HOST_REPO_PATH`, with the safety note.

Task order builds foundation-first (Dockerfile), then the consumer (entrypoint), then the producer (launcher, fully `--dry-run`-testable), then docs, then a manual end-to-end check.

---

### Task 1: Dockerfile — writable roots + pre-created `~/.claude/projects`

**Files:**
- Modify: `Dockerfile:41` (after the `useradd` line) and `Dockerfile:58-60` (the reviewer-side `.claude.json` RUN)

**Interfaces:**
- Produces: an image where (a) `/Users` and `/home` are `reviewer`-owned so `mkdir -p <host-path>` succeeds as `reviewer`, and (b) `/home/reviewer/.claude/projects` exists `reviewer`-owned so a narrow bind mount at `.../projects/<encoded>` leaves writable parents.

- [ ] **Step 1: Add the writable-roots RUN after `useradd`**

In `Dockerfile`, immediately after line 41 (`RUN useradd --create-home --shell /bin/bash reviewer`), insert:

```dockerfile

# Pre-create the top-level roots that host repo paths live under, owned by
# `reviewer`, so `--export-sessions` can clone the working copy at the *host*
# path (session-folder alignment). The unprivileged user can't create a new
# top-level dir under `/`, and can't drop from root under --cap-drop ALL, so
# only the first path component must pre-exist and be writable — `mkdir -p`
# creates the rest. /Users covers macOS hosts, /home covers Linux hosts.
RUN mkdir -p /Users /home && chown reviewer:reviewer /Users /home
```

- [ ] **Step 2: Pre-create `~/.claude/projects` in the reviewer-side RUN**

In `Dockerfile`, replace the onboarding RUN (currently lines 58-60):

```dockerfile
# Pre-accept onboarding so headless runs never block on a first-run prompt.
RUN printf '{"hasCompletedOnboarding": true, "bypassPermissionsModeAccepted": true}\n' \
      > /home/reviewer/.claude.json
```

with:

```dockerfile
# Pre-accept onboarding so headless runs never block on a first-run prompt, and
# pre-create ~/.claude/projects owned by `reviewer`. The latter matters for
# --export-sessions: it bind-mounts a single host folder at
# ~/.claude/projects/<encoded>, and if that parent didn't already exist Docker
# would create it root-owned, blocking Claude Code's other writes under ~/.claude.
RUN mkdir -p /home/reviewer/.claude/projects \
 && printf '{"hasCompletedOnboarding": true, "bypassPermissionsModeAccepted": true}\n' \
      > /home/reviewer/.claude.json
```

- [ ] **Step 3: Build the image**

Run: `docker build -t claudebox /Users/jonathonfrisby/mrjoy/claudebox`
Expected: build succeeds.

- [ ] **Step 4: Verify ownership and that a host-style path is creatable as `reviewer`**

Run:
```bash
docker run --rm --entrypoint sh claudebox -c \
  'id -un; ls -ld /Users /home /home/reviewer/.claude/projects; mkdir -p /Users/jonathonfrisby/mrjoy/repo && echo MKDIR_OK'
```
Expected: first line `reviewer`; the three `ls` lines all show owner `reviewer`; last line `MKDIR_OK`.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile
git commit -m "Pre-create writable host-path roots and ~/.claude/projects for session export"
```

---

### Task 2: entrypoint.sh — clone at the host path when `HOST_REPO_PATH` is set

**Files:**
- Modify: `entrypoint.sh:82-85` (the `--- Defaults ---` block defining `WORK_DIR`/`WORK_REPO`)

**Interfaces:**
- Consumes: `HOST_REPO_PATH` (env, optional) — the host's absolute repo path, set by the launcher's `--export-sessions`.
- Produces: `WORK_REPO` / `WORK_DIR` pointing at `HOST_REPO_PATH` when it is set and creatable; otherwise the existing `$HOME/work/repo` default. All downstream code (`mkdir -p "$WORK_DIR"`, the local clone, `safe.directory`, `cd "$WORK_REPO"`, the loop) is unchanged and consumes these two variables as before.

- [ ] **Step 1: Replace the WORK_DIR/WORK_REPO defaults with HOST_REPO_PATH-aware selection**

In `entrypoint.sh`, replace these lines (currently 83-85):

```bash
REPO_PATH="${REPO_PATH:-/repo}"
WORK_DIR="${WORK_DIR:-$HOME/work}"
WORK_REPO="$WORK_DIR/repo"
```

with:

```bash
REPO_PATH="${REPO_PATH:-/repo}"
# Working-clone location. Normally a private dir under $HOME. With
# --export-sessions the launcher sets HOST_REPO_PATH to the *host's* repo path
# and bind-mounts ~/.claude/projects/<encoded> from the host; cloning and
# running the review at that same path makes Claude Code encode the session
# project folder identically to the host, so transcripts file under the shared
# folder. mkdir here both creates the path and proves it's writable — if it
# isn't (an exotic host root not pre-created in the image; see Dockerfile), we
# warn and fall back to the default so the loop still runs (sessions just won't
# line up).
if [ -n "${HOST_REPO_PATH:-}" ] && mkdir -p "$HOST_REPO_PATH" 2>/dev/null; then
  WORK_REPO="$HOST_REPO_PATH"
  WORK_DIR="$(dirname "$HOST_REPO_PATH")"
elif [ -n "${HOST_REPO_PATH:-}" ]; then
  log "WARN: HOST_REPO_PATH=$HOST_REPO_PATH is not creatable here; sessions won't line up. Using the default work dir."
  WORK_DIR="${WORK_DIR:-$HOME/work}"
  WORK_REPO="$WORK_DIR/repo"
else
  WORK_DIR="${WORK_DIR:-$HOME/work}"
  WORK_REPO="$WORK_DIR/repo"
fi
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n /Users/jonathonfrisby/mrjoy/claudebox/entrypoint.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Verify the aligned-path branch selects `WORK_REPO` correctly (in-image)**

This runs the real file logic up to the selection by shadowing `claude` with a stub and pointing the loop at a throwaway. Instead of the full loop, verify just the variable selection by extracting the block against the built image's bash:

```bash
docker run --rm --entrypoint bash claudebox -c '
  HOST_REPO_PATH=/Users/jonathonfrisby/mrjoy/repo
  log() { printf "%s\n" "$*"; }
  if [ -n "${HOST_REPO_PATH:-}" ] && mkdir -p "$HOST_REPO_PATH" 2>/dev/null; then
    WORK_REPO="$HOST_REPO_PATH"; WORK_DIR="$(dirname "$HOST_REPO_PATH")"
  else WORK_DIR="$HOME/work"; WORK_REPO="$WORK_DIR/repo"; fi
  echo "WORK_REPO=$WORK_REPO"; echo "WORK_DIR=$WORK_DIR"'
```
Expected:
```
WORK_REPO=/Users/jonathonfrisby/mrjoy/repo
WORK_DIR=/Users/jonathonfrisby/mrjoy
```

- [ ] **Step 4: Verify the fallback branch when the root is not writable**

```bash
docker run --rm --entrypoint bash claudebox -c '
  HOST_REPO_PATH=/nope-not-a-root/repo
  log() { printf "%s\n" "$*"; }
  if [ -n "${HOST_REPO_PATH:-}" ] && mkdir -p "$HOST_REPO_PATH" 2>/dev/null; then
    WORK_REPO="$HOST_REPO_PATH"; WORK_DIR="$(dirname "$HOST_REPO_PATH")"
  elif [ -n "${HOST_REPO_PATH:-}" ]; then
    log "WARN: fallback"; WORK_DIR="$HOME/work"; WORK_REPO="$WORK_DIR/repo"
  else WORK_DIR="$HOME/work"; WORK_REPO="$WORK_DIR/repo"; fi
  echo "WORK_REPO=$WORK_REPO"'
```
Expected: a `WARN: fallback` line, then `WORK_REPO=/home/reviewer/work/repo`.
(`/nope-not-a-root` is under `/`, which `reviewer` can't write, so `mkdir -p` fails and the fallback fires.)

- [ ] **Step 5: Commit**

```bash
git add entrypoint.sh
git commit -m "entrypoint: clone at HOST_REPO_PATH for session-folder alignment"
```

---

### Task 3: claudebox.sh — the `--export-sessions` flag

**Files:**
- Modify: `claudebox.sh` — defaults block (~line 26), `usage()` (~lines 44-77), argument parser (~lines 82-103), and `build_run_flags()` (~lines 117-136)

**Interfaces:**
- Consumes: `$REPO`, `$MOUNT_REPO`, `$MOUNT_CLAUDE`, `$DRY_RUN`, and the `RUN_FLAGS` array built in `build_run_flags`.
- Produces: when `--export-sessions` is passed, appends `-e "HOST_REPO_PATH=$repo_abs"` and (unless `--mount-claude` is set) `-v "$HOME/.claude/projects/$encoded:/home/reviewer/.claude/projects/$encoded"` to `RUN_FLAGS`, and `mkdir -p`s the host source folder. `$encoded` = `printf '%s' "$repo_abs" | sed 's/[^a-zA-Z0-9]/-/g'`.

- [ ] **Step 1: Add the default**

In `claudebox.sh`, in the defaults block, after `MOUNT_CLAUDE=0` (line 22) add:

```bash
EXPORT_SESSIONS=0
```

- [ ] **Step 2: Document it in `usage()`**

In the `OPTIONS` section of `usage()`, after the `--mount-claude` block (ends at line 53, `# macOS host keeps credentials in the Keychain, not a file.`), insert:

```
  --export-sessions Export the container's Claude Code review transcripts to
                    your host ~/.claude and file them under the SAME project
                    folder your host uses for this repo (so in- and out-of-
                    container sessions line up). Requires a mounted repo.
                    Bind-mounts only this one repo's ~/.claude/projects folder
                    read-write; see README for the safety trade-off.
```

- [ ] **Step 3: Parse the flag**

In the argument `case`, after the `--mount-claude) MOUNT_CLAUDE=1 ;;` line (line 90) add:

```bash
    --export-sessions) EXPORT_SESSIONS=1 ;;
```

- [ ] **Step 4: Assemble the env + mount in `build_run_flags`**

In `build_run_flags()`, after the `--mount-claude` block (which ends at line 132 `  fi`) and before the hardening line (line 135), insert:

```bash

  if [ "$EXPORT_SESSIONS" = 1 ]; then
    # Aligning the in-container path to the host repo path is what makes a
    # narrow per-repo mount land, so a mounted repo is required.
    [ "$MOUNT_REPO" = 1 ] || die "--export-sessions needs a mounted repo (it aligns the in-container path to the host repo path); drop --no-repo."
    local repo_abs_es encoded_es
    repo_abs_es="$(cd "$REPO" && pwd)"
    # Claude Code names the session project folder after the cwd with every
    # non-alphanumeric char mapped 1:1 to '-'. Reproduce that so we mount the
    # exact folder Claude will write to.
    encoded_es="$(printf '%s' "$repo_abs_es" | sed 's/[^a-zA-Z0-9]/-/g')"
    RUN_FLAGS+=(-e "HOST_REPO_PATH=$repo_abs_es")
    if [ "$MOUNT_CLAUDE" = 1 ]; then
      # --mount-claude already mounts all of ~/.claude read-write, so the
      # narrow projects mount would be redundant; alignment via the env is enough.
      log "NOTE: --export-sessions with --mount-claude: ~/.claude is already mounted; skipping the narrow projects mount."
    else
      # Pre-create the host source dir (user-owned) so Docker binds it rather
      # than creating a root-owned one. Skip the side effect on --dry-run.
      [ "$DRY_RUN" = 1 ] || mkdir -p "$HOME/.claude/projects/$encoded_es"
      RUN_FLAGS+=(-v "$HOME/.claude/projects/$encoded_es:/home/reviewer/.claude/projects/$encoded_es")
    fi
  fi
```

- [ ] **Step 5: Syntax-check**

Run: `bash -n /Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh`
Expected: no output, exit 0.

- [ ] **Step 6: Verify the assembled command for the default (ollama) case**

Run (from the repo dir, so `--repo .` resolves to the claudebox repo itself):
```bash
cd /Users/jonathonfrisby/mrjoy/claudebox && ./claudebox.sh run --repo . --export-sessions --dry-run 2>&1
```
Expected: the printed `docker run` line contains both
`-e HOST_REPO_PATH=/Users/jonathonfrisby/mrjoy/claudebox` and
`-v /Users/jonathonfrisby/.claude/projects/-Users-jonathonfrisby-mrjoy-claudebox:/home/reviewer/.claude/projects/-Users-jonathonfrisby-mrjoy-claudebox`.

- [ ] **Step 7: Verify `--mount-claude` suppresses the narrow mount**

Run:
```bash
cd /Users/jonathonfrisby/mrjoy/claudebox && ./claudebox.sh run --repo . --export-sessions --mount-claude --dry-run 2>&1
```
Expected: the `NOTE:` line about skipping the narrow mount appears; the printed `docker run` line contains `-e HOST_REPO_PATH=...` and `-v /Users/jonathonfrisby/.claude:/home/reviewer/.claude` but **no** `.../projects/-Users-...:.../projects/-Users-...` narrow mount.

- [ ] **Step 8: Verify `--export-sessions --no-repo` is rejected**

Run:
```bash
cd /Users/jonathonfrisby/mrjoy/claudebox && ./claudebox.sh run --no-repo --export-sessions --dry-run 2>&1; echo "exit=$?"
```
Expected: an `ERROR: --export-sessions needs a mounted repo ...` line and `exit=1`.

- [ ] **Step 9: Confirm `--dry-run` created no host folder**

Run (using a throwaway repo path that has no existing host session folder):
```bash
mkdir -p /private/tmp/claude-501/es-test-repo
cd /private/tmp/claude-501/es-test-repo && git init -q 2>/dev/null; \
/Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh run --repo /private/tmp/claude-501/es-test-repo --export-sessions --dry-run >/dev/null 2>&1; \
ls -d "$HOME/.claude/projects/-private-tmp-claude-501-es-test-repo" 2>&1; echo "exit=$?"
```
Expected: `ls: ... No such file or directory` and `exit` non-zero — the dry run must not have created the folder.

- [ ] **Step 10: Commit**

```bash
git add claudebox.sh
git commit -m "launcher: add --export-sessions (export transcripts + align host path)"
```

---

### Task 4: Docs — README.md and .env.example

**Files:**
- Modify: `README.md` (launcher command list ~lines 70-78; a new subsection; the Configuration list ~lines 196-202)
- Modify: `.env.example` (Optional section, after the `MAX_PASSES_PER_SESSION` block ~line 51)

**Interfaces:**
- Consumes: the `--export-sessions` behavior from Task 3 and `HOST_REPO_PATH` from Task 2.
- Produces: user-facing documentation; no code.

- [ ] **Step 1: Add `--export-sessions` to the launcher example block in README**

In `README.md`, in the launcher code block (lines 70-78), after the `--mount-claude` run line (line 74) add:

```bash
./claudebox.sh run --repo /path/to/your/repo --export-sessions   # export transcripts to host ~/.claude
```

- [ ] **Step 2: Add a subsection explaining the flag**

In `README.md`, immediately before the `## Build` heading (line 82), insert:

```markdown
### Exporting review sessions to your host

By default the reviewer's Claude Code transcripts live on the container's
ephemeral filesystem and vanish when it's removed. `--export-sessions` writes
them to your host `~/.claude` instead, filed under the **same** project folder
your host `claude` uses for this repo — so you can compare in-container review
sessions against your own sessions for the repo (e.g. in a session-viewer tool)
and have them grouped together.

```bash
./claudebox.sh run --repo /path/to/your/repo --export-sessions
```

It works by two coupled steps: it bind-mounts your host
`~/.claude/projects/<repo-folder>` into the container read-write, and it runs
the review clone at your repo's *host path* inside the container so Claude Code
encodes the session folder to that same `<repo-folder>` name. It therefore
requires a mounted repo (it can't be combined with `--no-repo`). If you're
already using `--mount-claude`, all of `~/.claude` is mounted, so the narrow
mount is skipped and only the path alignment is added.

> **Safety trade-off:** this bind-mounts one host session folder **read-write**
> into a container running in YOLO mode — the container can read and rewrite
> *this repo's* transcripts. The mount is deliberately narrow (just this one
> repo's folder), so every other project's transcripts stay untouched. It's
> off by default; enable it only when you want the export.
```

- [ ] **Step 3: Add `--export-sessions` to the Configuration section**

In `README.md`, in the `Optional:` list (lines 196-202), after the `MAX_PASSES_PER_SESSION` bullet (line 202) add:

```markdown
- `--export-sessions` (launcher flag, not an env var) — export review transcripts to the host and align the session folder; see [Exporting review sessions to your host](#exporting-review-sessions-to-your-host)
```

- [ ] **Step 4: Note `HOST_REPO_PATH` in .env.example**

In `.env.example`, after the `MAX_PASSES_PER_SESSION=0` block (line 51) add:

```bash

# HOST_REPO_PATH is set automatically by the launcher's --export-sessions flag
# (it's the host path the working clone runs at, so exported session folders
# line up with your host ones). You normally don't set this yourself; it's here
# only so a hand-rolled `docker run` can opt into the same alignment.
# HOST_REPO_PATH=
```

- [ ] **Step 5: Sanity-check the docs render**

Run: `grep -n "export-sessions" /Users/jonathonfrisby/mrjoy/claudebox/README.md /Users/jonathonfrisby/mrjoy/claudebox/.env.example`
Expected: matches in both the launcher block, the new subsection, and the Configuration list (README) — confirming the anchor `#exporting-review-sessions-to-your-host` target exists.

- [ ] **Step 6: Commit**

```bash
git add README.md .env.example
git commit -m "docs: document --export-sessions and HOST_REPO_PATH"
```

---

### Task 5: End-to-end manual verification (real creds)

**Files:** none (verification only).

**Interfaces:**
- Consumes: a working `.env` in the repo (valid `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, and provider credential). This is the true acceptance test and needs real credentials, so it's run by hand rather than automated.

- [ ] **Step 1: Build and start a real export-enabled run against a repo**

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
./claudebox.sh build
./claudebox.sh run --repo /path/to/a/repo/with/open/PRs --export-sessions
./claudebox.sh logs   # watch until you see "▸ session <id> started" and a completed pass
```
Expected: the log shows a session id and `Review pass complete`.

- [ ] **Step 2: Confirm a transcript appeared on the host under the aligned folder**

With the repo path used above, compute the folder and list it on the host:
```bash
REPO_ABS=/path/to/a/repo/with/open/PRs
ENC="$(printf '%s' "$REPO_ABS" | sed 's/[^a-zA-Z0-9]/-/g')"
ls -la "$HOME/.claude/projects/$ENC"/*.jsonl
```
Expected: at least one `<session-id>.jsonl` file, freshly modified — the in-container session, now on the host, under the same folder the host uses for that repo.

- [ ] **Step 3: Tear down**

```bash
./claudebox.sh stop
```
Expected: container removed.

---

## Self-review notes

- **Spec coverage:** launcher flag + encoding + host-side mkdir + narrow mount + `--mount-claude` skip + `--no-repo` rejection → Task 3; entrypoint HOST_REPO_PATH + graceful fallback → Task 2; Dockerfile `/Users`+`/home` + `~/.claude/projects` pre-create → Task 1; README + .env.example + `--help` → Tasks 3 (help) & 4 (docs); safety note → Task 4. All spec sections map to a task.
- **Encoding consistency:** the exact string `printf '%s' … | sed 's/[^a-zA-Z0-9]/-/g'` is used identically in Task 3 (launcher) and Task 5 (verification); the entrypoint never re-encodes (Claude Code does), matching the spec.
- **bash 3.2:** `RUN_FLAGS+=(...)` and `local` are 3.2-safe; no arrays are expanded bare under `set -u` in the added code.
