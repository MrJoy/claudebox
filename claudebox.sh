#!/usr/bin/env bash
#
# claudebox — launcher for the unattended PR-reviewer container.
#
# Wraps `docker build` / `docker run` (with the hardening flags the entrypoint
# REQUIRES) plus the usual lifecycle commands behind one self-describing CLI, so
# the long, easy-to-get-wrong `docker run` invocation lives in exactly one place.
# Run `./claudebox.sh --help` for the full reference.
set -euo pipefail

log()  { printf '%s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

# PR selectors (mutually exclusive; passed through to the container as -e VARs).
PR_ALL=0
PR_ASSIGNEE=""
PR_IDS=""
PR_SEARCH=""
PR_SEL_COUNT=0
PR_SEL_NAMES=""

usage() {
  cat <<'EOF'
claudebox — launcher for the unattended PR-reviewer container.

USAGE
  ./claudebox.sh [options] <command> [-- extra docker/claude args]

COMMANDS
  build     Build the image from this directory.
  run       Start the reviewer detached and hardened (the normal way to run it).
  test      Run once in the FOREGROUND (--rm -it) for a quick, ephemeral check.
  logs      Follow the running container's logs (docker logs -f).
  shell     Open a bash shell inside the running container.
  stop      Stop and remove the container.
  status    Show the container's state (and live resource stats if running).

OPTIONS
  --repo PATH       Host repo to mount read-only at /repo (default: current dir).
                    The reviewer local-clones this as a seed; omit with --no-repo
                    to have it network-clone GITHUB_REPOSITORY instead.
  --no-repo         Don't mount a repo; the reviewer clones over the network.
  --env-file PATH   Env file passed to the container. Default: auto-select from
                    the cwd, preferring .env.claudebox over .env (so a repo can
                    carry its own claudebox creds without touching its .env).
  --mount-claude    Also bind-mount your ~/.claude (READ-WRITE) into the
                    container, so PROVIDER=anthropic can reuse your existing
                    `claude` login instead of an API key. Linux host only — a
                    macOS host keeps credentials in the Keychain, not a file.
  --export-sessions Export the container's Claude Code review transcripts to
                    your host ~/.claude and file them under the SAME project
                    folder your host uses for this repo (so in- and out-of-
                    container sessions line up). Requires a mounted repo.
                    Bind-mounts only this one repo's ~/.claude/projects folder
                    read-write; see README for the safety trade-off.
                    Reliable on macOS/Windows Docker Desktop; on a native
                    Linux host a UID mismatch may block the write — see
                    README.
  --name NAME       Container name. Default: derived as claudebox--<org>--<repo>
                    from GITHUB_REPOSITORY (env file) or the repo's git origin
                    remote, so each repo gets its own container and several can
                    run at once.
  --image NAME      Image tag to build/run (default: claudebox).
  --memory SIZE     Memory limit (default: 4g).
  --pids N          PID limit (default: 512).
  --no-restart      Don't pass --restart unless-stopped to `run`.
  --tail            After `run` starts the container, follow its logs (like the
                    `logs` command). Ctrl-C stops following; the container runs on.
  --all             Review all open PRs.
  --assignee LOGIN  Review open PRs assigned to this GitHub user.
  --prs LIST        Review exactly these PR numbers (comma/space list, e.g.
                    12,15,20).
  --search QUERY    Review PRs matching this gh search query (e.g.
                    "is:open label:needs-review"). You control state via the
                    query. Provide exactly ONE of --all/--assignee/--prs/--search
                    (here or via PR_* in the env file).
  --dry-run         Print the docker command instead of executing it.
  -h, --help        Show this help.

  Anything after `--` is appended verbatim to the underlying docker command
  (for `run`/`test`, that lands as arguments to the container entrypoint).

HARDENING (always applied to run/test; the entrypoint refuses to start without it)
  --cap-drop ALL  --security-opt no-new-privileges  --pids-limit N  --memory SIZE
  and the image's own non-root `reviewer` user.

EXAMPLES
  ./claudebox.sh build
  ./claudebox.sh run --repo ~/src/myrepo
  ./claudebox.sh run --repo ~/src/myrepo --mount-claude      # anthropic OAuth reuse
  cd ~/src/myrepo && claudebox run --tail                    # infer env+repo+name, then follow logs
  claudebox run --all --tail                                 # review every open PR
  claudebox run --assignee alice                             # PRs assigned to alice
  claudebox run --prs 12,15,20                               # just these PRs
  ./claudebox.sh test --repo ~/src/myrepo                    # one-off foreground run
  ./claudebox.sh logs
  ./claudebox.sh stop
EOF
}

# --- Parse arguments -------------------------------------------------------
COMMAND=""
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    build|run|test|logs|shell|stop|status)
      [ -z "$COMMAND" ] || die "more than one command given ('$COMMAND' and '$1')."
      COMMAND="$1" ;;
    --repo)        REPO="${2:?--repo requires a PATH}"; MOUNT_REPO=1; REPO_EXPLICIT=1; shift ;;
    --no-repo)     MOUNT_REPO=0 ;;
    --env-file)    ENV_FILE="${2:?--env-file requires a PATH}"; ENV_FILE_EXPLICIT=1; shift ;;
    --mount-claude) MOUNT_CLAUDE=1 ;;
    --export-sessions) EXPORT_SESSIONS=1 ;;
    --name)        NAME="${2:?--name requires a value}"; NAME_EXPLICIT=1; shift ;;
    --image)       IMAGE="${2:?--image requires a value}"; shift ;;
    --memory)      MEMORY="${2:?--memory requires a value}"; shift ;;
    --pids)        PIDS="${2:?--pids requires a value}"; shift ;;
    --no-restart)  RESTART=0 ;;
    --tail)        TAIL=1 ;;
    --all)         PR_ALL=1;      PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --all" ;;
    --assignee)    PR_ASSIGNEE="${2:?--assignee requires a LOGIN}"; PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --assignee"; shift ;;
    --prs)         PR_IDS="${2:?--prs requires a comma/space list of PR numbers}"; PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --prs"; shift ;;
    --search)      PR_SEARCH="${2:?--search requires a gh search query}"; PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --search"; shift ;;
    --dry-run)     DRY_RUN=1 ;;
    -h|--help)     usage; exit 0 ;;
    --)            shift; EXTRA=("$@"); break ;;
    -*)            die "unknown option: $1 (see --help)." ;;
    *)             die "unknown argument: $1 (see --help)." ;;
  esac
  shift
done

[ -n "$COMMAND" ] || { usage; exit 2; }

# Selector flags are mutually exclusive (the entrypoint is authoritative and
# also errors when none/multiple are set via the env file; this is the friendly
# early check for CLI flags). Zero flags is fine here — the env file may set one.
[ "$PR_SEL_COUNT" -le 1 ] || die "multiple PR selector flags given ($(echo "$PR_SEL_NAMES" | xargs)); provide exactly one of --all, --assignee, --prs, --search."

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
    # Only accept a well-formed org/repo (same guard as the git-remote path
    # below); this rejects e.g. a value with a trailing inline comment and lets
    # derivation fall through to the git remote rather than build a bad name.
    printf '%s' "$slug" | grep -qE '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$' || slug=""
    [ -n "$slug" ] && src="env file $ENV_FILE"
  fi
  if [ -z "$slug" ]; then
    url="$(git -C "$REPO" remote get-url origin 2>/dev/null || true)"
    if [ -n "$url" ]; then
      slug="$(printf '%s' "$url" | sed -E 's#^git@[^:]+:##; s#^[a-zA-Z]+://[^/]+/##; s#\.git$##')"
      printf '%s' "$slug" | grep -qE '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$' || slug=""
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

# Print a command (quoted so it's copy-pasteable) and then run it — unless
# --dry-run, in which case only print. This is what makes the launcher
# self-documenting: you always see the exact docker invocation.
show_and_run() {
  { printf '+ '; printf '%q ' "$@"; printf '\n'; } >&2
  [ "$DRY_RUN" = 1 ] && return 0
  "$@"
}

# Assemble the shared `docker run` flags (mounts + hardening) for run/test.
build_run_flags() {
  RUN_FLAGS=(--env-file "$ENV_FILE")

  [ -f "$ENV_FILE" ] || die "env file '$ENV_FILE' not found (copy .env.example to .env, or pass --env-file)."

  if [ "$MOUNT_REPO" = 1 ]; then
    [ -d "$REPO" ] || die "repo path '$REPO' is not a directory (use --repo PATH, or --no-repo to network-clone)."
    local repo_abs; repo_abs="$(cd "$REPO" && pwd)"
    RUN_FLAGS+=(-v "$repo_abs:/repo:ro")
  fi

  if [ "$MOUNT_CLAUDE" = 1 ]; then
    [ -d "$HOME/.claude" ] || die "--mount-claude: '$HOME/.claude' does not exist (log in with 'claude' first, or use a token)."
    [ -f "$HOME/.claude/.credentials.json" ] || log "WARN: $HOME/.claude/.credentials.json not found; if you're on macOS your login lives in the Keychain (not a file) — use CLAUDE_CODE_OAUTH_TOKEN instead."
    RUN_FLAGS+=(-v "$HOME/.claude:/home/reviewer/.claude")
  fi

  if [ "$EXPORT_SESSIONS" = 1 ]; then
    # Aligning the in-container path to the host repo path is what makes a
    # narrow per-repo mount land, so a mounted repo is required.
    [ "$MOUNT_REPO" = 1 ] || die "--export-sessions needs a mounted repo (it aligns the in-container path to the host repo path); drop --no-repo."
    local encoded_es
    # Claude Code names the session project folder after the cwd with every
    # non-alphanumeric char mapped 1:1 to '-'. Reproduce that so we mount the
    # exact folder Claude will write to.
    encoded_es="$(printf '%s' "$repo_abs" | sed 's/[^a-zA-Z0-9]/-/g')"
    RUN_FLAGS+=(-e "HOST_REPO_PATH=$repo_abs")
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

  # Hardening: the entrypoint's startup checks refuse to run without these.
  RUN_FLAGS+=(--cap-drop ALL --security-opt no-new-privileges --pids-limit "$PIDS" --memory "$MEMORY")

  # Pass any given PR selector through to the container. (An env-file value of
  # the same var is overridden by this -e; two selectors reaching the container
  # is what the entrypoint rejects.)
  [ "$PR_ALL" = 1 ]     && RUN_FLAGS+=(-e "PR_ALL=1")
  [ -n "$PR_ASSIGNEE" ] && RUN_FLAGS+=(-e "PR_ASSIGNEE=$PR_ASSIGNEE")
  [ -n "$PR_IDS" ]      && RUN_FLAGS+=(-e "PR_IDS=$PR_IDS")
  [ -n "$PR_SEARCH" ]   && RUN_FLAGS+=(-e "PR_SEARCH=$PR_SEARCH")
  true  # keep the function's exit status 0: the last `[ ... ] && ...` above
        # would otherwise make build_run_flags itself fail under `set -e`
        # whenever PR_SEARCH is unset (the test-then-&& idiom is only safe
        # when it's NOT the final statement executed).
}

case "$COMMAND" in
  build)
    show_and_run docker build -t "$IMAGE" ${EXTRA[@]+"${EXTRA[@]}"} "$SCRIPT_DIR"
    ;;
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
  test)
    build_run_flags
    show_and_run docker run --rm -it "${RUN_FLAGS[@]}" "$IMAGE" ${EXTRA[@]+"${EXTRA[@]}"}
    ;;
  logs)
    show_and_run docker logs -f ${EXTRA[@]+"${EXTRA[@]}"} "$NAME"
    ;;
  shell)
    show_and_run docker exec -it "$NAME" bash ${EXTRA[@]+"${EXTRA[@]}"}
    ;;
  stop)
    show_and_run docker rm -f "$NAME"
    ;;
  status)
    show_and_run docker ps -a --filter "name=^/${NAME}$" \
      --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
    if [ "$DRY_RUN" != 1 ] && docker ps --filter "name=^/${NAME}$" --format '{{.Names}}' | grep -q "^${NAME}$"; then
      docker stats --no-stream "$NAME"
    fi
    ;;
esac
