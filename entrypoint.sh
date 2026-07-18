#!/usr/bin/env bash
#
# PR-reviewer loop. Configures auth, prepares a writable working copy of the
# (read-only) seed repo, points Claude Code at the configured model provider
# (Ollama Cloud, Anthropic, or any Anthropic-compatible endpoint), then
# repeatedly runs a non-interactive review pass until the container is stopped.
set -euo pipefail

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- Required configuration ------------------------------------------------
# Provider-specific credentials are validated in "Backend selection" below.
: "${GITHUB_TOKEN:?set GITHUB_TOKEN (privilege-minimized: read repo/PRs, write PR comments)}"
: "${GITHUB_REPOSITORY:?set GITHUB_REPOSITORY in owner/repo form}"

# --- Hardening checks ------------------------------------------------------
# The loop runs unattended in YOLO mode (--dangerously-skip-permissions), so it
# must not be able to cause damage. Verify the container was launched with the
# security boundaries the README requires and refuse to run if a security-
# critical one is missing. Resource bounds (pids/memory) only cap runaway use,
# not damage, and detecting them reliably varies across cgroup v1/v2, so a
# missing one is just a warning. Set ALLOW_UNHARDENED=1 to downgrade the hard
# failures to warnings (e.g. a non-Docker runtime, or a deliberate test).
ALLOW_UNHARDENED="${ALLOW_UNHARDENED:-0}"
hardening_failed=0

# Record an unmet security requirement. We collect all of them and decide
# whether to abort afterwards, so one run reports every problem at once.
require() {
  if [ "$ALLOW_UNHARDENED" = "1" ]; then
    log "WARN (unhardened, ignored via ALLOW_UNHARDENED): $*"
  else
    log "HARDENING ERROR: $*"
    hardening_failed=1
  fi
}

# Warn if no pids/memory limit is in effect. Best-effort: tries cgroup v2 then
# v1, and stays quiet if it can't tell (better than a false alarm).
check_resource_limit() {
  local name=$1 v2=$2 v1=$3 unlimited=$4 v
  if [ -r "$v2" ]; then v="$(cat "$v2")"
  elif [ -r "$v1" ]; then v="$(cat "$v1")"
  else log "WARN: cannot determine $name limit; consider setting it."; return 0; fi
  case "$v" in
    "$unlimited") log "WARN: no $name limit set; a runaway pass could exhaust host resources." ;;
    # cgroup v1 reports "unlimited" memory as a huge sentinel rather than 'max'.
    ''|*[!0-9]*) ;;
    *) if [ "$v" -ge 9223372036854000000 ]; then
         log "WARN: no $name limit set; a runaway pass could exhaust host resources."
       fi ;;
  esac
  # Always succeed: a "limit is fine" result must not leak a non-zero exit
  # status, or `set -e` would abort the script on a correctly-limited container.
  return 0
}

# Unprivileged user. Claude Code also refuses --dangerously-skip-permissions as
# root, but check explicitly for a clear message (e.g. if run with --user root).
[ "$(id -u)" != "0" ] || require "running as root; run as an unprivileged user (don't override the image's 'reviewer' user with --user root)."

# no-new-privileges and dropped capabilities both read from /proc/self/status.
if [ -r /proc/self/status ]; then
  nnp="$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)"
  capbnd="$(awk '/^CapBnd:/ {print $2}' /proc/self/status)"
  # Set by --security-opt no-new-privileges; blocks regaining privilege via setuid.
  [ "$nnp" = "1" ] || require "no-new-privileges is not set; run with --security-opt no-new-privileges."
  # --cap-drop ALL empties the bounding set; a default container keeps a non-zero set.
  { [ -z "$capbnd" ] || [ "$capbnd" = "0000000000000000" ]; } || require "Linux capabilities are not all dropped (CapBnd=$capbnd); run with --cap-drop ALL."
else
  log "WARN: cannot read /proc/self/status; skipping no-new-privileges and capability checks."
fi

check_resource_limit pids   /sys/fs/cgroup/pids.max   /sys/fs/cgroup/pids/pids.max                max
check_resource_limit memory /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory/memory.limit_in_bytes max

if [ "$hardening_failed" = "1" ]; then
  die "container is not hardened (see errors above). Re-run with the flags in README 'Run', or set ALLOW_UNHARDENED=1 to override."
fi

# --- Defaults --------------------------------------------------------------
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
REVIEW_INTERVAL_SECONDS="${REVIEW_INTERVAL_SECONDS:-300}"
# REVIEW_MODEL's default depends on the provider; it is resolved in the
# "Backend selection" block below.
# Rotate to a fresh session after this many successful passes, to cap the
# growth of a long-lived resumed session's context. 0 = never rotate.
MAX_PASSES_PER_SESSION="${MAX_PASSES_PER_SESSION:-0}"
case "$MAX_PASSES_PER_SESSION" in ''|*[!0-9]*) die "MAX_PASSES_PER_SESSION must be a non-negative integer";; esac
DEFAULT_PROMPT="Please review open PRs to find unreviewed PRs, PRs in need of re-review, or PRs where your assistance has been requested (look for comments addressing 'claudebox'). Perform a thorough review / re-review of all such PRs. Pay particular attention to test quality/robustness, security, correctness, and architectural coherence/consistency. Also consider whether the approach the PR is taking is prudent and robust in light of the issue being addressed. Post findings as comments on the PR, one comment per finding. Be sure you're looking at the most recent commit on the branch. Sign your comments with '-claudebox'."
REVIEW_PROMPT="${REVIEW_PROMPT:-$DEFAULT_PROMPT}"
# Prompt used on resumed passes (the session already holds context from prior
# passes, so this nudges it to re-check rather than re-introduce the task).
DEFAULT_FOLLOWUP="I've fetched the latest refs. Re-check the repository for new or updated PRs since your last pass, any PRs still needing review, or PRs where your assistance has been requested (look for comments addressing 'claudebox'). Apply the same review standard. Only post findings that haven't already raised. Be sure you're looking at the most recent commit on each branch. Sign your comments with '-claudebox'."
FOLLOWUP_PROMPT="${FOLLOWUP_PROMPT:-$DEFAULT_FOLLOWUP}"

# --- GitHub auth (gh + git) ------------------------------------------------
# gh reads GH_TOKEN from the environment; setup-git makes git reuse it for
# github.com over https, so PR branch fetches are authenticated.
export GH_TOKEN="$GITHUB_TOKEN"
gh auth setup-git
git config --global user.name  "PR Reviewer (bot)"
git config --global user.email "pr-reviewer@localhost"
git config --global --add safe.directory "$WORK_REPO"

# --- Backend selection (Claude Code -> model provider) ---------------------
# Claude Code speaks the Anthropic API. PROVIDER picks where those requests go:
#   ollama    - Ollama Cloud's native Anthropic-compatible API (default).
#   anthropic - Anthropic's own API.
#   custom    - any other Anthropic-compatible endpoint you point us at.
# Whichever backend is chosen, we pin EVERY model tier to the single
# $REVIEW_MODEL. A non-Anthropic backend has no Opus/Sonnet/Haiku models, so if
# a subagent or alias requests an un-overridden tier, Claude Code errors out on
# an unknown model; mapping them all to $REVIEW_MODEL keeps any request valid.
# (On Anthropic this also guarantees one model does every bit of the work.)
PROVIDER="${PROVIDER:-ollama}"

echo
echo
echo "Using provider: ${PROVIDER}"
echo
echo

case "$PROVIDER" in
  ollama)
    # Ollama serves a native Anthropic-compatible API; auth MUST go through
    # ANTHROPIC_AUTH_TOKEN (a Bearer token), not ANTHROPIC_API_KEY.
    : "${OLLAMA_API_KEY:?set OLLAMA_API_KEY (from https://ollama.com/settings/keys), or choose a different PROVIDER}"
    export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://ollama.com}"
    export ANTHROPIC_AUTH_TOKEN="$OLLAMA_API_KEY"
    export ANTHROPIC_API_KEY=""
    REVIEW_MODEL="${REVIEW_MODEL:-glm-5.2:cloud}"
    ;;
  anthropic)
    # Anthropic's own API, using its default endpoint (base URL left unset).
    # Credential resolution, in precedence order — we take the first available so
    # you don't have to paste an API key if `claude` already has a login:
    #   1. ANTHROPIC_API_KEY        - explicit console key (x-api-key).
    #   2. CLAUDE_CODE_OAUTH_TOKEN  - long-lived subscription token; mint one on
    #                                 the host with `claude setup-token`. This is
    #                                 the portable, cross-platform headless path.
    #   3. a mounted credentials file - the SAME creds `claude` uses outside the
    #                                 container. Mount the host's ~/.claude into
    #                                 the reviewer's home; mount it READ-WRITE so
    #                                 Claude Code can refresh the token (a :ro
    #                                 mount fails on refresh), and note a macOS
    #                                 host keeps these in the Keychain, not a file.
    # An EMPTY ANTHROPIC_API_KEY outranks the OAuth token in Claude Code's
    # precedence, so for paths 2/3 we `unset` (not blank) the header vars to make
    # sure nothing shadows the credential we actually want used.
    creds_file="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json"
    if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
      export ANTHROPIC_AUTH_TOKEN=""   # explicit key wins; drop any stray Bearer
      log "PROVIDER=anthropic: authenticating with ANTHROPIC_API_KEY."
    elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
      unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
      log "PROVIDER=anthropic: authenticating with CLAUDE_CODE_OAUTH_TOKEN."
    elif [ -r "$creds_file" ]; then
      unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
      log "PROVIDER=anthropic: authenticating with mounted credentials at $creds_file."
    else
      die "PROVIDER=anthropic needs a credential. Provide one of: ANTHROPIC_API_KEY (https://console.anthropic.com/); CLAUDE_CODE_OAUTH_TOKEN (run 'claude setup-token' on your host); or mount your host ~/.claude (read-write) so $creds_file exists."
    fi
    REVIEW_MODEL="${REVIEW_MODEL:-claude-opus-4-8}"
    ;;
  custom)
    # Any other Anthropic-compatible endpoint. The caller supplies the URL and a
    # model the endpoint serves; there is no sensible default for either.
    : "${ANTHROPIC_BASE_URL:?set ANTHROPIC_BASE_URL to your endpoint for PROVIDER=custom}"
    : "${REVIEW_MODEL:?set REVIEW_MODEL to a model your endpoint serves for PROVIDER=custom}"
    export ANTHROPIC_BASE_URL
    # Accept whichever auth style the endpoint expects: ANTHROPIC_AUTH_TOKEN
    # sends "Authorization: Bearer" (most gateways/compatible services),
    # ANTHROPIC_API_KEY sends Anthropic's native "x-api-key". Require one.
    if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
      export ANTHROPIC_API_KEY=""
    elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
      export ANTHROPIC_AUTH_TOKEN=""
    else
      die "PROVIDER=custom needs an auth credential: set ANTHROPIC_AUTH_TOKEN (Bearer) or ANTHROPIC_API_KEY (x-api-key)."
    fi
    ;;
  *)
    die "unknown PROVIDER='$PROVIDER'; use one of: ollama, anthropic, custom."
    ;;
esac

# Pin every model tier to the one review model (see the note above).
export ANTHROPIC_MODEL="$REVIEW_MODEL"
export ANTHROPIC_DEFAULT_FABLE_MODEL="$REVIEW_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$REVIEW_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$REVIEW_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$REVIEW_MODEL"
# Deprecated alias for the small/fast model; set too for older code paths.
export ANTHROPIC_SMALL_FAST_MODEL="$REVIEW_MODEL"

# --- Prepare a writable working copy ---------------------------------------
# REPO_PATH is the user's primary repo, mounted read-only. We make a cheap LOCAL
# clone of it: git copies the local object store rather than pulling over the
# network. --no-hardlinks is required because a bind mount is a different device
# than the container fs, so hardlinking (git clone --local's default) fails with
# "Invalid cross-device link". The clone is our own writable repo; the mount is
# never touched. If no usable repo is mounted, fall back to a network clone.
git config --global --add safe.directory "$REPO_PATH"
mkdir -p "$WORK_DIR"
if [ ! -d "$WORK_REPO/.git" ]; then
  if git -C "$REPO_PATH" rev-parse --git-dir >/dev/null 2>&1; then
    log "Local-cloning seed repo from $REPO_PATH (no network) -> $WORK_REPO"
    git clone --local --no-hardlinks "$REPO_PATH" "$WORK_REPO"
  else
    log "No usable git repo at $REPO_PATH; cloning $GITHUB_REPOSITORY over the network"
    gh repo clone "$GITHUB_REPOSITORY" "$WORK_REPO"
  fi
fi

# Make sure 'origin' points at GitHub (the seed copy may have a local/other
# remote) so both git fetch and gh resolve to the right repository.
git -C "$WORK_REPO" remote set-url origin "https://github.com/${GITHUB_REPOSITORY}.git" \
  || git -C "$WORK_REPO" remote add origin "https://github.com/${GITHUB_REPOSITORY}.git"

log "Reviewer ready. repo=$GITHUB_REPOSITORY provider=$PROVIDER model=$REVIEW_MODEL interval=${REVIEW_INTERVAL_SECONDS}s"

# --- Review loop -----------------------------------------------------------
# One continuous, *stateful* Claude session: the first pass starts a new session
# and we capture its id; every later pass --resumes that id so Claude remembers
# what it already reviewed (avoids duplicate comments). /loop can't be used here
# because it needs a live interactive session, which headless `-p` mode isn't.
# This shell acts as the supervisor: it controls cadence and relaunches Claude
# (a fresh session) if a pass fails. Headless + all permissions skipped is safe:
# unprivileged user, minimized token, read-only seed.
cd "$WORK_REPO"
SESSION_ID=""
PASSES_THIS_SESSION=0

# Pretty-print Claude's stream-json (one JSON event per line) into readable,
# live log lines: assistant text, tool calls, tool results, and the final
# result. fromjson? tolerates any non-JSON line instead of aborting the stream.
format_stream() {
  jq -j --unbuffered -R '
    (fromjson? // empty) as $e | $e
    | if .type == "system" and .subtype == "init" then
        "  ▸ session \(.session_id) started\n"
      elif .type == "assistant" then
        ( .message.content[]?
          | if .type == "text" then (.text | select(length > 0) | "\(.)\n")
            elif .type == "tool_use" then "  → \(.name): \(.input | tojson | .[0:200])\n"
            else empty end )
      elif .type == "user" then
        ( .message.content[]?
          | if .type == "tool_result" then
              ( (.content | if type == "array" then (map(.text? // "") | join(" ")) else tostring end))
              | gsub("[\\n\\t ]+"; " ") | "  ← \(.[0:200])\n"
            else empty end )
      elif .type == "result" then
        "  ✓ result (\(.subtype // "")): \((.result // "") | .[0:800])\n"
      else empty end
  '
}

# Run one review pass. Starts a new session unless SESSION_ID is set, in which
# case it resumes that one. Streams events live to the log (so `docker logs -f`
# shows the play-by-play) while teeing the raw stream to a temp file to recover
# the session id. Returns non-zero on failure (caller clears SESSION_ID).
run_pass() {
  local prompt="$1" rc errfile rawfile sid
  errfile="$(mktemp)"; rawfile="$(mktemp)"
  # set +e around the pipeline so a formatter hiccup can't abort the script and
  # so we can read Claude's own exit code via PIPESTATUS[0] (not tee's/jq's).
  set +e
  if [ -n "$SESSION_ID" ]; then
    claude -p --resume "$SESSION_ID" --output-format stream-json --verbose \
      --dangerously-skip-permissions --model "$REVIEW_MODEL" "$prompt" \
      2>"$errfile" | stdbuf -oL tee "$rawfile" | format_stream
  else
    claude -p --output-format stream-json --verbose \
      --dangerously-skip-permissions --model "$REVIEW_MODEL" "$prompt" \
      2>"$errfile" | stdbuf -oL tee "$rawfile" | format_stream
  fi
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" -ne 0 ]; then
    log "WARN: claude exited $rc:"; tail -n 5 "$errfile" >&2
    rm -f "$errfile" "$rawfile"; return "$rc"
  fi
  # session_id appears in the init and result events; take the last one seen.
  sid="$(jq -r -R '(fromjson? // empty) | select(.session_id) | .session_id' "$rawfile" 2>/dev/null | tail -n 1 || true)"
  [ -n "$sid" ] && SESSION_ID="$sid"
  rm -f "$errfile" "$rawfile"
  return 0
}

while true; do
  log "Fetching latest refs..."
  git fetch --all --prune --quiet || log "WARN: git fetch failed; continuing"

  if [ -z "$SESSION_ID" ]; then
    log "Starting review pass (new session)..."
    PROMPT="$REVIEW_PROMPT"
    PASSES_THIS_SESSION=0
  else
    log "Starting review pass (resuming session $SESSION_ID)..."
    PROMPT="$FOLLOWUP_PROMPT"
  fi

  if run_pass "$PROMPT"; then
    PASSES_THIS_SESSION=$((PASSES_THIS_SESSION + 1))
    log "Review pass complete (session $SESSION_ID, pass $PASSES_THIS_SESSION)."
    # Rotate to a fresh session once the cap is hit, to bound context growth.
    if [ "$MAX_PASSES_PER_SESSION" -gt 0 ] && [ "$PASSES_THIS_SESSION" -ge "$MAX_PASSES_PER_SESSION" ]; then
      log "Reached MAX_PASSES_PER_SESSION=$MAX_PASSES_PER_SESSION; rotating to a fresh session next cycle."
      SESSION_ID=""
    fi
  else
    log "WARN: review pass failed; starting a fresh session next cycle."
    SESSION_ID=""
  fi

  log "Sleeping ${REVIEW_INTERVAL_SECONDS}s..."
  sleep "$REVIEW_INTERVAL_SECONDS"
done
