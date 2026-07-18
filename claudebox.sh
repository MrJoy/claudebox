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
  --env-file PATH   Env file passed to the container (default: ./.env).
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
  --name NAME       Container name (default: claudebox).
  --image NAME      Image tag to build/run (default: claudebox).
  --memory SIZE     Memory limit (default: 4g).
  --pids N          PID limit (default: 512).
  --no-restart      Don't pass --restart unless-stopped to `run`.
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
    --repo)        REPO="${2:?--repo requires a PATH}"; MOUNT_REPO=1; shift ;;
    --no-repo)     MOUNT_REPO=0 ;;
    --env-file)    ENV_FILE="${2:?--env-file requires a PATH}"; shift ;;
    --mount-claude) MOUNT_CLAUDE=1 ;;
    --export-sessions) EXPORT_SESSIONS=1 ;;
    --name)        NAME="${2:?--name requires a value}"; shift ;;
    --image)       IMAGE="${2:?--image requires a value}"; shift ;;
    --memory)      MEMORY="${2:?--memory requires a value}"; shift ;;
    --pids)        PIDS="${2:?--pids requires a value}"; shift ;;
    --no-restart)  RESTART=0 ;;
    --dry-run)     DRY_RUN=1 ;;
    -h|--help)     usage; exit 0 ;;
    --)            shift; EXTRA=("$@"); break ;;
    -*)            die "unknown option: $1 (see --help)." ;;
    *)             die "unknown argument: $1 (see --help)." ;;
  esac
  shift
done

[ -n "$COMMAND" ] || { usage; exit 2; }

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

  # Hardening: the entrypoint's startup checks refuse to run without these.
  RUN_FLAGS+=(--cap-drop ALL --security-opt no-new-privileges --pids-limit "$PIDS" --memory "$MEMORY")
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
    [ "$DRY_RUN" = 1 ] || log "Started '$NAME'. Follow it with: ./claudebox.sh logs --name $NAME"
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
