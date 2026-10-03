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
PR_AUTHOR=""
PR_IDS=""
PR_SEARCH=""
PR_NEW=0
PR_SEL_COUNT=0
PR_SEL_NAMES=""

# Persona selection (parsed and validated by the entrypoint, not here — this is
# bash 3.2 on macOS, and the entrypoint is authoritative anyway).
PERSONAS=""

# How many of a PR's personas review it at once (validated by the supervisor,
# not here, for the same reason PERSONAS isn't).
MAX_CONCURRENT_PASSES=""

# kindex: on by default whenever `kin` is on PATH (--no-kindex opts out). The
# store is resolved by kindex itself, from inside the repo, and mounted
# read-only; the container works from a copy. See CLAUDE.md.
KINDEX=1
KINDEX_PROFILE=""
KINDEX_DIR=""
KINDEX_MOUNT=""

usage() {
  cat <<'EOF'
claudebox — launcher for the unattended PR-reviewer container.

USAGE
  ./claudebox.sh [options] <command> [-- extra docker/claude args]

COMMANDS
  build     Build the image from this directory. Pins the image's kindex to
            the host's own `kin --version`, when `kin` is on PATH, so the
            image opens the schema the host writes. Override with
            `-- --build-arg KINDEX_VERSION=X.Y.Z` (a later --build-arg for the
            same key wins over the launcher's own) -- useful for a source or
            git install of kin whose version string isn't on PyPI.
  run       Start the reviewer detached and hardened (the normal way to run it).
  test      Run once in the FOREGROUND (--rm -it) for a quick, ephemeral check.
  logs      Follow the running container's logs (docker logs -f).
  shell     Open a bash shell inside the running container.
  stop      Stop and remove the container.
  status    Show the container's state (and live resource stats if running).

OPTIONS
  --repo PATH       Host repo to seed the reviewer from (default: current dir).
                    Only its .git is mounted, read-only at /repo/.git: the
                    reviewer local-clones that and never sees your working tree,
                    so ignored files (Library/, worktrees, build output) are not
                    exposed to it. Must be a primary repo, not a git worktree.
                    Omit with --no-repo to network-clone GITHUB_REPOSITORY.
  --no-repo         Don't mount a repo; the reviewer clones over the network.
                    Also skips automatic kindex resolution, since there is no
                    repo to resolve a store against -- pass --kindex-profile
                    or --kindex-dir to give the reviewer one anyway.
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
  --author LOGIN    Review open PRs opened by this GitHub user (an app's login
                    carries an app/ prefix, e.g. app/dependabot).
  --prs LIST        Review exactly these PR numbers (comma/space list, e.g.
                    12,15,20).
  --search QUERY    Review PRs matching this gh search query (e.g.
                    "is:open label:needs-review"). You control state via the
                    query.
  --new             Review open PRs created after this container started. The
                    cutoff is captured once at startup and resets on restart.
                    Combine with --assignee or --author to review only that
                    user's new PRs. Otherwise provide exactly ONE of
                    --all/--assignee/--author/--prs/--search/--new (here or via
                    PR_* in the env file).
  --persona LIST    Review with these adversarial personas only (comma list, or
                    'all'). Default: red_team,adversarial,sme,sage. Also
                    available: user, good_friend, helland. One session per PR per persona,
                    so a cycle is (PRs x personas) reviews, a PR's personas
                    running together -- see --max-concurrent-passes.
  --max-concurrent-passes N
                    How many of a PR's personas review it at the same time.
                    Default (unset or 0): all of them. Lower it if you hit
                    provider rate limits or the container's memory ceiling; 1
                    reviews one persona at a time.
  --no-kindex       Don't give reviewers your kindex graph. By default, when
                    `kin` is on PATH, the launcher asks kindex which store
                    serves --repo (`kin config get data_dir`, run from inside
                    the repo), mounts that store read-only, and reviewers read
                    a private copy of it through the kindex MCP tools, read
                    tools only. They can see ALL of that store, and a reviewer
                    can post what it reads onto a PR.
  --kindex-profile NAME
                    Use this kindex profile instead of the one kindex resolves.
                    Works under --no-repo: resolution runs from the current
                    directory instead of a repo.
  --kindex-dir DIR  Mount this kindex data dir (it must hold kindex.db) instead
                    of asking kindex. Works under --no-repo.
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
  claudebox run --author alice                               # PRs alice opened
  claudebox run --prs 12,15,20                               # just these PRs
  claudebox run --new                                        # only PRs opened after startup
  claudebox run --assignee alice --new                       # alice's PRs opened after startup
  claudebox run --author alice --new                         # PRs alice opened after startup
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
    --author)      PR_AUTHOR="${2:?--author requires a LOGIN}"; PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --author"; shift ;;
    --prs)         PR_IDS="${2:?--prs requires a comma/space list of PR numbers}"; PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --prs"; shift ;;
    --search)      PR_SEARCH="${2:?--search requires a gh search query}"; PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --search"; shift ;;
    --new)         PR_NEW=1 ;;
    --persona)     PERSONAS="${2:?--persona requires a comma-separated list of persona names}"; shift ;;
    --max-concurrent-passes)
                   MAX_CONCURRENT_PASSES="${2:?--max-concurrent-passes requires a non-negative integer}"; shift ;;
    --no-kindex)   KINDEX=0 ;;
    --kindex-profile) KINDEX_PROFILE="${2:?--kindex-profile requires a profile NAME}"; shift ;;
    --kindex-dir)  KINDEX_DIR="${2:?--kindex-dir requires a PATH}"; shift ;;
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
# --new stands alone or narrows --assignee or --author to PRs created after
# startup.
[ "$PR_SEL_COUNT" -le 1 ] || die "multiple PR selector flags given ($(echo "$PR_SEL_NAMES" | xargs)); provide exactly one of --all, --assignee, --author, --prs, --search, --new (--new may also be combined with --assignee or --author)."
if [ "$PR_NEW" = 1 ] && [ "$PR_SEL_COUNT" = 1 ] && [ -z "$PR_ASSIGNEE" ] && [ -z "$PR_AUTHOR" ]; then
  die "--new cannot be combined with$PR_SEL_NAMES; it works alone or with --assignee or --author."
fi

if [ "$KINDEX" = 0 ] && { [ -n "$KINDEX_PROFILE" ] || [ -n "$KINDEX_DIR" ]; }; then
  die "--no-kindex contradicts --kindex-profile/--kindex-dir; give one or the other."
fi
if [ -n "$KINDEX_PROFILE" ] && [ -n "$KINDEX_DIR" ]; then
  die "give --kindex-profile or --kindex-dir, not both."
fi

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

# Automatic kindex resolution warns and carries on: a kin that misbehaves on
# the host should not block a review launch. A store the operator named with
# --kindex-profile/--kindex-dir dies instead, because they asked for it.
kindex_fail() {
  if [ -n "$KINDEX_PROFILE" ] || [ -n "$KINDEX_DIR" ]; then die "$1"; fi
  log "WARN: $1; running without kindex."
}

# Sets KINDEX_MOUNT to the host data dir to mount, or leaves it empty. Not
# called inside $(...): it may die, and a die in a subshell exits only that.
resolve_kindex() {
  KINDEX_MOUNT=""
  [ "$KINDEX" = 1 ] || return 0
  if [ -n "$KINDEX_DIR" ]; then
    [ -f "$KINDEX_DIR/kindex.db" ] || { kindex_fail "--kindex-dir '$KINDEX_DIR' holds no kindex.db"; return 0; }
    KINDEX_MOUNT="$(cd "$KINDEX_DIR" && pwd)"
    announce "kindex: store $KINDEX_MOUNT (from --kindex-dir), mounted read-only; reviewers can read all of it"
    return 0
  fi
  if [ "$MOUNT_REPO" = 0 ] && [ -z "$KINDEX_PROFILE" ]; then
    log "NOTE: kindex is not resolved without a mounted repo (--no-repo); pass --kindex-profile or --kindex-dir to give one anyway."
    return 0
  fi
  if ! command -v kin >/dev/null 2>&1; then
    [ -z "$KINDEX_PROFILE" ] || die "--kindex-profile given, but kin is not on PATH."
    return 0
  fi
  # From inside the repo: kindex matches profile roots against the cwd, and
  # reads the repo's tracked .kin/config from there. With --no-repo there is
  # no repo to stand in, so a named --kindex-profile resolves from $PWD
  # instead -- the operator named the profile, and cwd only matters for
  # kindex's own lookup.
  local from="$REPO" dir="" which="" profile="" source=""
  [ "$MOUNT_REPO" = 1 ] || from="$PWD"
  [ -d "$from" ] || from="$PWD"
  local -a pargs
  pargs=()
  [ -z "$KINDEX_PROFILE" ] || pargs=(--profile "$KINDEX_PROFILE")
  if ! dir="$(cd "$from" && kin config get data_dir ${pargs[@]+"${pargs[@]}"} 2>/dev/null)" || [ -z "$dir" ]; then
    kindex_fail "kin could not resolve a kindex store for '$from' (run 'kin config get data_dir' there to see why)"
    return 0
  fi
  # kin prints data_dir verbatim, unexpanded: a leading '~' or a relative path
  # both come through as-is. Expand '~' ourselves, then canonicalize a
  # relative path against $from (the same directory kin resolved it from),
  # so both the kindex.db check and the eventual `-v` flag see an absolute
  # path -- a bare relative path reaching `docker run -v` is read as a NAMED
  # VOLUME, not a bind mount, and docker creates it empty and silent.
  case "$dir" in
    "~") dir="$HOME" ;;
    "~/"*) dir="$HOME/${dir#"~/"}" ;;
  esac
  local abs=""
  abs="$(CDPATH= cd "$from" 2>/dev/null && CDPATH= cd "$dir" 2>/dev/null && pwd)" || abs=""
  if [ -z "$abs" ]; then
    kindex_fail "kindex resolved '$dir' for '$from', which does not exist"
    return 0
  fi
  [ -f "$abs/kindex.db" ] || { kindex_fail "kindex resolved '$abs' for '$from', which holds no kindex.db"; return 0; }
  which="$(cd "$from" && kin profile which --json ${pargs[@]+"${pargs[@]}"} 2>/dev/null || true)"
  profile="$(printf '%s' "$which" | sed -n 's/.*"profile": *"\([^"]*\)".*/\1/p')"
  source="$(printf '%s' "$which" | sed -n 's/.*"source": *"\([^"]*\)".*/\1/p')"
  KINDEX_MOUNT="$abs"
  announce "kindex: profile ${profile:-(none)} via ${source:-legacy}, store $abs, mounted read-only; reviewers can read all of it (--no-kindex to opt out)"
}

# Assemble the shared `docker run` flags (mounts + hardening) for run/test.
build_run_flags() {
  RUN_FLAGS=(--env-file "$ENV_FILE")

  [ -f "$ENV_FILE" ] || die "env file '$ENV_FILE' not found (copy .env.example to .env, or pass --env-file)."

  if [ "$MOUNT_REPO" = 1 ]; then
    [ -d "$REPO" ] || die "repo path '$REPO' is not a directory (use --repo PATH, or --no-repo to network-clone)."
    local repo_abs; repo_abs="$(cd "$REPO" && pwd)"
    # Mount the OBJECT STORE, not the working tree. The container needs the repo
    # only to make its local clone at startup, and that clone reads nothing but
    # .git -- while a whole-repo mount leaves every ignored file (a Unity
    # Library/, nested worktrees, build output) exposed for the container's
    # entire life, where a reviewer that decides to go wandering can walk it.
    # On a VirtIO-backed mount that walk can pin file descriptors hard enough to
    # take the host down. Mounting .git alone means there is nothing to walk.
    # The container path convention is unchanged: the object store lands where
    # it would sit under a whole-repo mount, so the entrypoint reads one path.
    if [ ! -d "$repo_abs/.git" ]; then
      if [ -e "$repo_abs/.git" ]; then
        die "repo path '$REPO' is a git worktree (.git is a file, not a directory). Mount the PRIMARY repo instead: a worktree keeps its objects in the parent and is structurally unusable on its own."
      fi
      die "repo path '$REPO' has no .git directory, so there is no object store to seed from (use --repo PATH on a primary git repo, or --no-repo to network-clone)."
    fi
    RUN_FLAGS+=(-v "$repo_abs/.git:/repo/.git:ro")
  fi

  resolve_kindex
  [ -z "$KINDEX_MOUNT" ] || RUN_FLAGS+=(-v "$KINDEX_MOUNT:/kindex-src:ro")

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
  [ -n "$PR_AUTHOR" ]   && RUN_FLAGS+=(-e "PR_AUTHOR=$PR_AUTHOR")
  [ -n "$PR_IDS" ]      && RUN_FLAGS+=(-e "PR_IDS=$PR_IDS")
  [ -n "$PR_SEARCH" ]   && RUN_FLAGS+=(-e "PR_SEARCH=$PR_SEARCH")
  [ "$PR_NEW" = 1 ]     && RUN_FLAGS+=(-e "PR_NEW=1")
  [ -n "$PERSONAS" ]    && RUN_FLAGS+=(-e "PERSONAS=$PERSONAS")
  [ -n "$MAX_CONCURRENT_PASSES" ] && RUN_FLAGS+=(-e "MAX_CONCURRENT_PASSES=$MAX_CONCURRENT_PASSES")
  true  # keep the function's exit status 0: the last `[ ... ] && ...` above
        # would otherwise make build_run_flags itself fail under `set -e`
        # whenever PR_SEARCH is unset (the test-then-&& idiom is only safe
        # when it's NOT the final statement executed).
}

case "$COMMAND" in
  build)
    # Match the image's kindex to the host's, so the image opens the schema the
    # host writes. No kin, or a version string we don't recognise, leaves the
    # Dockerfile's pinned default.
    build_args=()
    if command -v kin >/dev/null 2>&1; then
      kv="$(kin --version 2>/dev/null | awk '{print $2}')" || kv=""
      if printf '%s' "$kv" | grep -qE '^[0-9]+(\.[0-9]+)+$'; then
        build_args=(--build-arg "KINDEX_VERSION=$kv")
        announce "kindex version: $kv (matching the host's kin)"
      fi
    fi
    show_and_run docker build -t "$IMAGE" ${build_args[@]+"${build_args[@]}"} ${EXTRA[@]+"${EXTRA[@]}"} "$SCRIPT_DIR"
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
