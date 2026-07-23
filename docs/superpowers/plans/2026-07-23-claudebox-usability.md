# claudebox Usability Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator keep per-repo credentials in each repo's worktree (`.env.claudebox`) and run/manage multiple claudeboxes at once by auto-naming containers `claudebox--<org>--<repo>`, inferring env file and repo from the cwd (announced loudly), and adding a `--tail` flag.

**Architecture:** All changes live in the host-side launcher `claudebox.sh` plus docs. A small resolution pipeline (`resolve_env_file` → `resolve_repo` → `derive_name`) runs after arg-parsing for every command except `build`, populating `ENV_FILE`/`REPO`/`NAME`. Inferences (not explicit flags) print a loud stderr banner via `announce()`. `entrypoint.sh`, `Dockerfile`, and container runtime are untouched.

**Tech Stack:** Bash. `claudebox.sh` runs on the **host** where macOS ships **bash 3.2** — keep it 3.2-safe. Verification uses the launcher's own `--dry-run` (prints the exact `docker` command to stderr without executing) plus `bash -n`; there is no other test runner.

## Global Constraints

- `claudebox.sh` MUST stay bash-3.2-safe: expand possibly-empty arrays as `${arr[@]+"${arr[@]}"}`, never `"${arr[@]}"` under `set -u`. No associative arrays, no `${var,,}`.
- Do NOT modify `entrypoint.sh`, `Dockerfile`, or any hardening flag / safety boundary.
- Container name format is exactly `claudebox--<org>--<repo>`: strip a trailing `.git`, replace each `/` with `--`, prefix `claudebox--`. Case preserved; `_ . -` pass through.
- Name-derivation source order: env-file `GITHUB_REPOSITORY` first, then `git remote get-url origin`, then `die` (unless `--name` given).
- Env-file auto-select order (cwd): `.env.claudebox` then `.env`; `.env.claudebox` is preferred to avoid touching a project's own `.env`. An explicit `--env-file` disables inference and is silent.
- Announcements go to **stderr** and only for values that were *inferred*, never for values given explicitly by flag.
- Name derivation runs for `run`/`logs`/`shell`/`stop`/`status`; `test` resolves env+repo only (stays unnamed); `build` resolves nothing.
- Every step-with-code shows the full code. Verify each task with `bash -n claudebox.sh` before committing.

**Test scratch dir** (used verbatim in verification steps below):
```
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/2555f647-560a-4487-ad3b-1f1abfb0914c/scratchpad
CB=/Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh
```

---

### Task 1: Resolution pipeline (env file, repo, name) + loud announcements

**Files:**
- Modify: `claudebox.sh` — defaults block (`claudebox.sh:16-27`), arg parser (`claudebox.sh:92-114`), and add the pipeline + wiring after the `COMMAND` guard (`claudebox.sh:116`); update the `run` success hint (`claudebox.sh:180`).

**Interfaces:**
- Produces (globals other code relies on): `ENV_FILE` (resolved path), `REPO` (path), `NAME` (derived or `--name`), plus internal helpers `announce`, `resolve_env_file`, `resolve_repo`, `derive_name`, `resolve_context`.
- Consumes: existing `log`/`die`, and `build_run_flags` (reads `ENV_FILE`, `REPO`), the `case` arms (read `NAME`).

- [ ] **Step 1: Change the defaults block** — replace the current defaults (`claudebox.sh:16-27`) so `NAME`/`ENV_FILE` start empty (sentinel for "derive/auto") and add explicit-flag trackers + `TAIL`.

Replace:
```bash
# --- Defaults (override via flags) -----------------------------------------
IMAGE="claudebox"
NAME="claudebox"
ENV_FILE=".env"
REPO="$PWD"
MOUNT_REPO=1
MOUNT_CLAUDE=0
EXPORT_SESSIONS=0
RESTART=1
MEMORY="4g"
PIDS="512"
DRY_RUN=0
```
with:
```bash
# --- Defaults (override via flags) -----------------------------------------
IMAGE="claudebox"
NAME=""              # empty => derive claudebox--<org>--<repo>
NAME_EXPLICIT=0
ENV_FILE=""          # empty => auto-select .env.claudebox / .env from cwd
ENV_FILE_EXPLICIT=0
REPO="$PWD"
REPO_EXPLICIT=0
MOUNT_REPO=1
MOUNT_CLAUDE=0
EXPORT_SESSIONS=0
RESTART=1
MEMORY="4g"
PIDS="512"
TAIL=0
DRY_RUN=0
```

- [ ] **Step 2: Set explicit-flag trackers in the arg parser** — update the three option arms (`claudebox.sh:97,99,102`) to record that the value was given explicitly.

Replace these three lines:
```bash
    --repo)        REPO="${2:?--repo requires a PATH}"; MOUNT_REPO=1; shift ;;
    --env-file)    ENV_FILE="${2:?--env-file requires a PATH}"; shift ;;
    --name)        NAME="${2:?--name requires a value}"; shift ;;
```
with:
```bash
    --repo)        REPO="${2:?--repo requires a PATH}"; MOUNT_REPO=1; REPO_EXPLICIT=1; shift ;;
    --env-file)    ENV_FILE="${2:?--env-file requires a PATH}"; ENV_FILE_EXPLICIT=1; shift ;;
    --name)        NAME="${2:?--name requires a value}"; NAME_EXPLICIT=1; shift ;;
```

- [ ] **Step 3: Add the pipeline functions + wiring** — insert this block immediately after the `[ -n "$COMMAND" ] || { usage; exit 2; }` line (`claudebox.sh:116`), before the `show_and_run` definition.

```bash
# --- Inference pipeline (announced loudly) ---------------------------------
# Print a visually distinct banner for each value we INFER (never for values
# the operator gave explicitly). Goes to stderr like log().
announce() {
  log ""
  log ">>> claudebox: $*"
}

# Auto-select the env file from the cwd unless --env-file was given.
# Prefer .env.claudebox so we never co-opt a project's own .env.
resolve_env_file() {
  [ "$ENV_FILE_EXPLICIT" = 1 ] && return 0
  if [ -f ".env.claudebox" ]; then
    ENV_FILE=".env.claudebox"
    announce "env file: .env.claudebox (preferred over .env)"
  elif [ -f ".env" ]; then
    ENV_FILE=".env"
    announce "env file: .env (no .env.claudebox in cwd)"
  else
    ENV_FILE=".env"   # nominal default; build_run_flags reports if it's needed & missing
  fi
}

# REPO already defaults to $PWD; just announce when it was inferred (and a repo
# is actually being mounted).
resolve_repo() {
  [ "$REPO_EXPLICIT" = 1 ] && return 0
  [ "$MOUNT_REPO" = 1 ] || return 0
  announce "repo: $REPO (inferred from current directory)"
}

# Derive NAME=claudebox--<org>--<repo> from GITHUB_REPOSITORY in the env file,
# else the repo's git 'origin' remote, else die. Skipped when --name was given.
derive_name() {
  [ "$NAME_EXPLICIT" = 1 ] && return 0
  local slug="" src="" url=""
  if [ -f "$ENV_FILE" ]; then
    slug="$(sed -n -E 's/^[[:space:]]*GITHUB_REPOSITORY[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" \
            | tail -n1 | sed -E 's/^["'\'']//; s/["'\'']?[[:space:]]*$//')"
    [ -n "$slug" ] && src="env file $ENV_FILE"
  fi
  if [ -z "$slug" ]; then
    url="$(git -C "$REPO" remote get-url origin 2>/dev/null || true)"
    if [ -n "$url" ]; then
      slug="$(printf '%s' "$url" | sed -E 's#^git@[^:]+:##; s#^[a-zA-Z]+://[^/]+/##; s#\.git$##')"
      printf '%s' "$slug" | grep -qE '^[^/]+/[^/]+$' || slug=""
      [ -n "$slug" ] && src="git remote of $REPO"
    fi
  fi
  [ -n "$slug" ] || die "can't determine org/repo for the container name (no GITHUB_REPOSITORY in '$ENV_FILE' and no usable git 'origin' remote in '$REPO'). Pass --name, or set GITHUB_REPOSITORY."
  NAME="claudebox--$(printf '%s' "$slug" | sed 's#/#--#g')"
  announce "container name: $NAME (from $src)"
}

# Run the resolution appropriate to the command. build needs nothing; test is
# ephemeral/unnamed so it skips name derivation.
case "$COMMAND" in
  build) : ;;
  test)  resolve_env_file; resolve_repo ;;
  *)     resolve_env_file; resolve_repo; derive_name ;;
esac
```

- [ ] **Step 4: Update the `run` success hint** — the derived name means the operator can follow logs from the same cwd with no flags. Replace the hint line (`claudebox.sh:180`):
```bash
    [ "$DRY_RUN" = 1 ] || log "Started '$NAME'. Follow it with: ./claudebox.sh logs --name $NAME"
```
with:
```bash
    [ "$DRY_RUN" = 1 ] || log "Started '$NAME'. Follow it with: ./claudebox.sh logs (from here), or re-run with --tail."
```

- [ ] **Step 5: Syntax check**

Run: `bash -n /Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh`
Expected: no output, exit 0.

- [ ] **Step 6: Verify env-file source + name derivation (env wins)**

Run:
```bash
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/2555f647-560a-4487-ad3b-1f1abfb0914c/scratchpad
CB=/Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh
mkdir -p "$SB/t-env" && cd "$SB/t-env"
printf 'GITHUB_TOKEN=x\nGITHUB_REPOSITORY=mrjoy/hordes-of-orcs-next\n' > .env.claudebox
printf 'GITHUB_REPOSITORY=other/wrong\n' > .env
"$CB" --dry-run status 2>&1 | grep -E 'claudebox: (env file|container name)'
```
Expected output contains:
```
>>> claudebox: env file: .env.claudebox (preferred over .env)
>>> claudebox: container name: claudebox--mrjoy--hordes-of-orcs-next (from env file .env.claudebox)
```
(And the `+ docker ps ...` line uses `name=^/claudebox--mrjoy--hordes-of-orcs-next$`.)

- [ ] **Step 7: Verify git-remote fallback (no env file) for both URL forms**

Run:
```bash
mkdir -p "$SB/t-git" && cd "$SB/t-git" && rm -f .env .env.claudebox
git init -q . 2>/dev/null; git remote remove origin 2>/dev/null || true
git remote add origin git@github.com:acme/widgets.git
"$CB" --dry-run stop 2>&1 | grep 'container name'
git remote set-url origin https://github.com/acme/widgets.git
"$CB" --dry-run stop 2>&1 | grep 'container name'
```
Expected both lines:
```
>>> claudebox: container name: claudebox--acme--widgets (from git remote of ...t-git)
>>> claudebox: container name: claudebox--acme--widgets (from git remote of ...t-git)
```

- [ ] **Step 8: Verify die when no source, and that `--name` bypasses derivation**

Run:
```bash
mkdir -p "$SB/t-none" && cd "$SB/t-none" && rm -rf .env .env.claudebox .git
"$CB" --dry-run stop; echo "exit=$?"
"$CB" --dry-run --name my-box stop 2>&1 | grep -E 'my-box|claudebox:'
```
Expected: the first command prints an `ERROR: can't determine org/repo ...` and `exit=1`; the second prints the `+ docker rm -f my-box` line and shows **no** `container name` announcement (only possibly an env-file line, which won't appear since none exists).

- [ ] **Step 9: Verify repo announcement + explicit flags are silent**

Run:
```bash
cd "$SB/t-env"
"$CB" --dry-run run 2>&1 | grep 'repo:'                         # inferred -> announced
"$CB" --dry-run --repo "$SB/t-env" --name x run 2>&1 | grep -c 'claudebox: repo'   # explicit -> silent
```
Expected: first prints `>>> claudebox: repo: .../t-env (inferred from current directory)`; second prints `0`.

- [ ] **Step 10: Commit**

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
git add claudebox.sh
git commit -m "launcher: infer env file, repo, and per-repo container name

- Auto-select .env.claudebox over .env from cwd (never touch project .env)
- Derive container name claudebox--<org>--<repo> from GITHUB_REPOSITORY, else
  git origin remote, else die (unless --name)
- Announce every inference loudly on stderr
- Run the pipeline for all commands except build; test stays unnamed

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B4nPWbXrMg1D5bMFLZpYa"
```

---

### Task 2: `--tail` flag on `run`

**Files:**
- Modify: `claudebox.sh` — add `--tail` to the arg parser (near `claudebox.sh:107`) and branch the `run` arm (`claudebox.sh:175-181`).

**Interfaces:**
- Consumes: `TAIL` global (added in Task 1 defaults), `NAME`, `show_and_run`, `DRY_RUN`.
- Produces: nothing new; changes `run` behavior only.

- [ ] **Step 1: Add the `--tail` parser arm** — insert after the `--no-restart` arm (`claudebox.sh:106`):
```bash
    --tail)        TAIL=1 ;;
```

- [ ] **Step 2: Branch the `run` arm to follow logs when `--tail`** — replace the current `run` arm body (`claudebox.sh:175-181`):
```bash
  run)
    build_run_flags
    restart_flags=()
    [ "$RESTART" = 1 ] && restart_flags=(--restart unless-stopped)
    show_and_run docker run -d --name "$NAME" ${restart_flags[@]+"${restart_flags[@]}"} "${RUN_FLAGS[@]}" "$IMAGE" ${EXTRA[@]+"${EXTRA[@]}"}
    [ "$DRY_RUN" = 1 ] || log "Started '$NAME'. Follow it with: ./claudebox.sh logs (from here), or re-run with --tail."
    ;;
```
with:
```bash
  run)
    build_run_flags
    restart_flags=()
    [ "$RESTART" = 1 ] && restart_flags=(--restart unless-stopped)
    show_and_run docker run -d --name "$NAME" ${restart_flags[@]+"${restart_flags[@]}"} "${RUN_FLAGS[@]}" "$IMAGE" ${EXTRA[@]+"${EXTRA[@]}"}
    if [ "$TAIL" = 1 ]; then
      # Follow the logs just like the `logs` command. Ctrl-C stops following but
      # leaves the detached container running.
      show_and_run docker logs -f "$NAME"
    else
      # Echo how the launcher was actually invoked ($0) rather than a hardcoded
      # ./claudebox.sh — the operator typically runs it from a repo worktree, not
      # from the claudebox dir, so a literal ./ path would be wrong.
      [ "$DRY_RUN" = 1 ] || log "Started '$NAME'. Follow it with: $0 logs (from this dir), or re-run with --tail."
    fi
    ;;
```

- [ ] **Step 3: Syntax check**

Run: `bash -n /Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh`
Expected: no output, exit 0.

- [ ] **Step 4: Verify `--tail` dry-run prints both commands**

Run:
```bash
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/2555f647-560a-4487-ad3b-1f1abfb0914c/scratchpad
cd "$SB/t-env"
/Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh --dry-run run --tail 2>&1 | grep -E '^\+ docker (run|logs)'
```
Expected two lines: a `+ docker run -d --name claudebox--mrjoy--hordes-of-orcs-next ...` line and a `+ docker logs -f claudebox--mrjoy--hordes-of-orcs-next` line.

- [ ] **Step 5: Verify plain `run` still prints the hint, not a logs command**

Run:
```bash
cd "$SB/t-env"
/Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh --dry-run run 2>&1 | grep -cE '^\+ docker logs'
```
Expected: `0`.

- [ ] **Step 6: Commit**

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
git add claudebox.sh
git commit -m "launcher: add --tail to run (start detached, then follow logs)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B4nPWbXrMg1D5bMFLZpYa"
```

---

### Task 3: Documentation (help, README, .env.example)

**Files:**
- Modify: `claudebox.sh` `usage()` heredoc (`claudebox.sh:29-87`).
- Modify: `README.md` — launcher section (~`README.md:66-83`) and Monitoring note (`README.md:189`).
- Modify: `.env.example` — header note (top of file).

**Interfaces:** none (docs only).

- [ ] **Step 1: Update `usage()`** — make three edits inside the heredoc.

  (a) Replace the `--env-file` option line:
```
  --env-file PATH   Env file passed to the container (default: ./.env).
```
  with:
```
  --env-file PATH   Env file passed to the container. Default: auto-select from
                    the cwd, preferring .env.claudebox over .env (so a repo can
                    carry its own claudebox creds without touching its .env).
```

  (b) Replace the `--name` option line:
```
  --name NAME       Container name (default: claudebox).
```
  with:
```
  --name NAME       Container name. Default: derived as claudebox--<org>--<repo>
                    from GITHUB_REPOSITORY (env file) or the repo's git origin
                    remote, so each repo gets its own container and several can
                    run at once.
```

  (c) Add a `--tail` option line immediately after the `--no-restart` line:
```
  --no-restart      Don't pass --restart unless-stopped to `run`.
```
  becomes:
```
  --no-restart      Don't pass --restart unless-stopped to `run`.
  --tail            After `run` starts the container, follow its logs (like the
                    `logs` command). Ctrl-C stops following; the container runs on.
```

  (d) Add an example after the existing `run --repo ... --mount-claude` example line:
```
  ./claudebox.sh run --repo ~/src/myrepo --mount-claude      # anthropic OAuth reuse
```
  becomes:
```
  ./claudebox.sh run --repo ~/src/myrepo --mount-claude      # anthropic OAuth reuse
  cd ~/src/myrepo && claudebox run --tail                    # infer env+repo+name, then follow logs
```

- [ ] **Step 2: Update `README.md` launcher section** — after the launcher command list (before line ~83), add a short subsection. Insert after the closing ``` of the code block at `README.md:78-82`:
```markdown

**Per-repo config & naming.** Run the launcher from inside a repo's worktree and it
infers everything from the cwd, announcing each inference loudly:

- **Env file:** it auto-selects `.env.claudebox` (preferred) or `.env` from the current
  directory, so a repo can carry its own claudebox credentials in `.env.claudebox` without
  disturbing the project's own `.env`. Override with `--env-file PATH`.
- **Repo:** defaults to the current directory (override with `--repo PATH`).
- **Container name:** derived as `claudebox--<org>--<repo>` from `GITHUB_REPOSITORY` (in the
  env file) or the repo's git `origin` remote — e.g. `claudebox--mrjoy--hordes-of-orcs-next`.
  This is what lets several claudeboxes run at once, one per repo. Override with `--name`.

The same inference runs for `logs`, `shell`, `stop`, and `status`, so from a repo's worktree
`claudebox logs` / `stop` target that repo's container with no flags. Add `--tail` to `run`
to start the container and immediately follow its logs.
```

- [ ] **Step 3: Update the Monitoring note in `README.md`** — replace the sentence at `README.md:189` that hard-codes `--name claudebox`:
```
The reviewer logs its whole heartbeat — and a live play-by-play of each pass — to stdout. The detached, **named** container from [Run](#run) (`--name claudebox`) is what makes its logs attachable. `./claudebox.sh logs` / `shell` / `status` cover the common views; the raw commands:
```
with:
```
The reviewer logs its whole heartbeat — and a live play-by-play of each pass — to stdout. The detached, **named** container from [Run](#run) (named `claudebox--<org>--<repo>`) is what makes its logs attachable. `./claudebox.sh logs` / `shell` / `status` re-derive that name from the cwd, so they cover the common views with no flags; the raw commands:
```

- [ ] **Step 4: Update `.env.example` header** — replace the top two comment lines:
```
# Copy to .env and fill in. Pass to the container with `docker run --env-file .env`.
# NOTE: keep your real .env out of git (already covered by .dockerignore/.gitignore).
```
with:
```
# Copy this into a repo as .env.claudebox (preferred — the launcher auto-selects it
# over .env, keeping claudebox creds separate from the project's own .env) or as .env,
# and fill in. Pass to the container with `docker run --env-file <file>`.
# NOTE: keep your real env file out of git (already covered by .dockerignore/.gitignore).
```

- [ ] **Step 5: Syntax check the launcher (heredoc edits can't break it, but confirm)**

Run: `bash -n /Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh && /Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh --help | grep -E '\--tail|--env-file|--name'`
Expected: exit 0 and the three option lines print with their updated text.

- [ ] **Step 6: Commit**

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
git add claudebox.sh README.md .env.example
git commit -m "docs: document .env.claudebox, per-repo naming, and --tail

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B4nPWbXrMg1D5bMFLZpYa"
```

---

## Notes for the implementer

- After Task 1, `cd`-ing between the scratch fixtures matters: env/name inference reads the **cwd**, so always `cd` to the fixture dir shown in each step before invoking `$CB`.
- The `sed` remote-URL parser handles `git@host:org/repo(.git)` and `scheme://host/org/repo(.git)`; the `grep -qE '^[^/]+/[^/]+$'` guard rejects anything that isn't exactly `org/repo`.
- `.gitignore`/`.dockerignore` already ignore `.env`; confirm `.env.claudebox` is also ignored — if not, that's a one-line addition to `.gitignore` (fold into Task 3 if needed). Check with `git check-ignore .env.claudebox` in a repo.
