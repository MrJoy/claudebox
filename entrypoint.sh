#!/usr/bin/env bash
#
# PR-reviewer loop. Configures auth, prepares a writable working copy of the
# (read-only) seed repo, points Claude Code at Ollama Cloud, then repeatedly
# runs a non-interactive review pass until the container is stopped.
set -euo pipefail

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- Required configuration ------------------------------------------------
: "${OLLAMA_API_KEY:?set OLLAMA_API_KEY (from https://ollama.com/settings/keys)}"
: "${GITHUB_TOKEN:?set GITHUB_TOKEN (privilege-minimized: read repo/PRs, write PR comments)}"
: "${GITHUB_REPOSITORY:?set GITHUB_REPOSITORY in owner/repo form}"

# --- Defaults --------------------------------------------------------------
REPO_PATH="${REPO_PATH:-/repo}"
WORK_DIR="${WORK_DIR:-$HOME/work}"
WORK_REPO="$WORK_DIR/repo"
REVIEW_INTERVAL_SECONDS="${REVIEW_INTERVAL_SECONDS:-300}"
REVIEW_MODEL="${REVIEW_MODEL:-glm-5.2:cloud}"
# Rotate to a fresh session after this many successful passes, to cap the
# growth of a long-lived resumed session's context. 0 = never rotate.
MAX_PASSES_PER_SESSION="${MAX_PASSES_PER_SESSION:-0}"
case "$MAX_PASSES_PER_SESSION" in ''|*[!0-9]*) die "MAX_PASSES_PER_SESSION must be a non-negative integer";; esac
DEFAULT_PROMPT="Please review open PRs to find unreviewed PRs, or PRs in need of re-review. Perform a thorough review / re-review of all such PRs. Pay particular attention to test quality/robustness, security, correctness, and architectural coherence/consistency. Post findings as comments on the PR, one comment per finding. Be sure you're looking at the most recent commit on the branch."
REVIEW_PROMPT="${REVIEW_PROMPT:-$DEFAULT_PROMPT}"
# Prompt used on resumed passes (the session already holds context from prior
# passes, so this nudges it to re-check rather than re-introduce the task).
DEFAULT_FOLLOWUP="I've fetched the latest refs. Re-check the repository for new or updated PRs since your last pass, and any PRs still needing review, applying the same review standard. Only post findings you haven't already raised. Be sure you're looking at the most recent commit on each branch."
FOLLOWUP_PROMPT="${FOLLOWUP_PROMPT:-$DEFAULT_FOLLOWUP}"

# --- GitHub auth (gh + git) ------------------------------------------------
# gh reads GH_TOKEN from the environment; setup-git makes git reuse it for
# github.com over https, so PR branch fetches are authenticated.
export GH_TOKEN="$GITHUB_TOKEN"
gh auth setup-git
git config --global user.name  "PR Reviewer (bot)"
git config --global user.email "pr-reviewer@localhost"
git config --global --add safe.directory "$WORK_REPO"

# --- Claude Code -> Ollama Cloud -------------------------------------------
# Ollama serves a native Anthropic-compatible API at https://ollama.com, so no
# translation proxy is needed. The key MUST be ANTHROPIC_AUTH_TOKEN (not
# ANTHROPIC_API_KEY). We point EVERY model tier at the one Ollama model: the
# backend has no Anthropic models, so if a subagent or alias asks for Opus/
# Sonnet/Haiku and that tier isn't overridden, Claude Code errors out on an
# unknown model. Mapping them all to $REVIEW_MODEL keeps any such request valid.
export ANTHROPIC_BASE_URL="https://ollama.com"
export ANTHROPIC_AUTH_TOKEN="$OLLAMA_API_KEY"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_MODEL="$REVIEW_MODEL"
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

log "Reviewer ready. repo=$GITHUB_REPOSITORY model=$REVIEW_MODEL interval=${REVIEW_INTERVAL_SECONDS}s"

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

# Run one review pass. Starts a new session unless SESSION_ID is set, in which
# case it resumes that one. Captures/refreshes SESSION_ID from the JSON output.
# Returns non-zero on failure (caller clears SESSION_ID to start fresh).
run_pass() {
  local prompt="$1" out rc errfile sid result
  errfile="$(mktemp)"
  if [ -n "$SESSION_ID" ]; then
    out="$(claude -p --resume "$SESSION_ID" --output-format json \
            --dangerously-skip-permissions --model "$REVIEW_MODEL" "$prompt" 2>"$errfile")"; rc=$?
  else
    out="$(claude -p --output-format json \
            --dangerously-skip-permissions --model "$REVIEW_MODEL" "$prompt" 2>"$errfile")"; rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    log "WARN: claude exited $rc:"; tail -n 5 "$errfile" >&2
    rm -f "$errfile"; return "$rc"
  fi
  rm -f "$errfile"
  sid="$(printf '%s' "$out" | jq -r '.session_id // empty' 2>/dev/null || true)"
  [ -n "$sid" ] && SESSION_ID="$sid"
  result="$(printf '%s' "$out" | jq -r '.result // empty' 2>/dev/null || true)"
  if [ -n "$result" ]; then log "Pass result:"; printf '%s\n' "$result"; fi
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
