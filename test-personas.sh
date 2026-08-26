#!/usr/bin/env bash
#
# Persona tests for entrypoint.sh. Same technique as test-providers.sh (stubs on
# PATH, `env -i`, ALLOW_UNHARDENED=1) with two deliberate differences:
#
#   * capture is INDEXED per `claude` invocation, because one cycle now runs one
#     invocation per (PR, persona) instead of exactly one;
#   * the `sleep` stub succeeds once before failing, so the loop runs TWO cycles.
#     That second cycle is the point: a one-cycle harness produces no resumed
#     invocation, which is why test-providers.sh cannot assert FOLLOWUP_PROMPT's
#     stanzas, and the most important property of the persona design (the persona
#     system prompt being re-passed on a resumed pass) lives exactly there.
#
#   ./test-personas.sh            # run everything
#   ./test-personas.sh resume     # only cases whose label matches 'resume'
#
# Needs jq (the entrypoint pipes claude's stream-json through it), mktemp, tee,
# and bash 4+ for the entrypoint itself. stdbuf is stubbed below rather than
# required, since macOS does not ship it.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRYPOINT="$SCRIPT_DIR/entrypoint.sh"
FILTER="${1:-}"

command -v jq >/dev/null || { printf 'ERROR: jq is required.\n' >&2; exit 1; }

BASH_BIN=""
for candidate in "${BASH:-}" "$(command -v bash || true)" /opt/homebrew/bin/bash /usr/local/bin/bash /bin/bash; do
  [ -n "$candidate" ] && [ -x "$candidate" ] || continue
  if [ "$("$candidate" -c 'echo ${BASH_VERSINFO[0]}')" -ge 4 ] 2>/dev/null; then
    BASH_BIN="$candidate"; break
  fi
done
[ -n "$BASH_BIN" ] || { printf 'ERROR: no bash 4+ found (macOS /bin/bash is 3.2 — `brew install bash`).\n' >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"

# `gh` now gets asked for a PR's labels, because mode routing decides code-vs-plan
# from them. Everything else it is asked still just has to succeed.
#   STUB_PLAN_PRS      -- comma-separated PR numbers that carry the plan label
#   STUB_LABEL_FAIL    -- comma-separated PR numbers whose label lookup fails
#   STUB_LABEL_GARBAGE -- comma-separated PR numbers whose lookup exits 0 but
#                         prints nothing (a successful, empty response)
#   STUB_LABEL_NULL    -- comma-separated PR numbers whose lookup exits 0 and
#                         prints the literal JSON `null` (a well-formed but
#                         useless response)
#   STUB_PLAN_AFTER    -- the PR is unlabeled until cycle N has finished, then
#                         labeled (see below)
cat >"$BIN/gh" <<'STUB'
#!/bin/sh
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  n="$3"
  case ",${STUB_LABEL_FAIL:-}," in
    *",$n,"*) echo "gh: could not resolve to a PullRequest" >&2; exit 1 ;;
  esac
  case ",${STUB_LABEL_GARBAGE:-}," in
    *",$n,"*) exit 0 ;;
  esac
  case ",${STUB_LABEL_NULL:-}," in
    *",$n,"*) echo "null"; exit 0 ;;
  esac
  # STUB_PLAN_AFTER=N: the PR is unlabeled until cycle N has finished, then
  # labeled. Cycles are counted by the sleep stub, which writes $HOME/sleeps.
  if [ -n "${STUB_PLAN_AFTER:-}" ]; then
    c=$(cat "$HOME/sleeps" 2>/dev/null || echo 0)
    if [ "$c" -ge "$STUB_PLAN_AFTER" ]; then
      printf '{"number":%s,"labels":[{"name":"%s"}]}\n' "$n" "${STUB_PLAN_LABEL:-plan}"
      exit 0
    fi
  fi
  case ",${STUB_PLAN_PRS:-}," in
    *",$n,"*) printf '{"number":%s,"labels":[{"name":"%s"}]}\n' "$n" "${STUB_PLAN_LABEL:-plan}" ;;
    *)        printf '{"number":%s,"labels":[]}\n' "$n" ;;
  esac
  exit 0
fi
exit 0
STUB
printf '#!/bin/sh\nexit 0\n' >"$BIN/git"

# The entrypoint pipes claude through `stdbuf -oL tee`, and stdbuf is GNU
# coreutils, which a bare macOS does not ship. test-providers.sh survives its
# absence because it only reads PIPESTATUS[0], but this suite needs the stream to
# actually reach tee: that is where the session id comes from, and the session id
# is what the resume assertions are about. Drop the flags, exec the rest.
cat >"$BIN/stdbuf" <<'STUB'
#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -*) shift ;; *) break ;; esac; done
exec "$@"
STUB

# Two cycles by default: succeed on the first sleep, fail on the next so the
# entrypoint's own `set -e` ends the run. STUB_MAX_CYCLES overrides per case.
cat >"$BIN/sleep" <<'STUB'
#!/bin/sh
n=$(( $(cat "$HOME/sleeps" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$HOME/sleeps"
[ "$n" -ge "${STUB_MAX_CYCLES:-2}" ] && exit 1
exit 0
STUB

# The probe. One dump file per invocation ($HOME/dump.N, N counting from 1),
# holding the argv and the model-tier env. Reports a successful pass by emitting
# stream-json with a per-invocation session id, so the supervisor records it and
# the next cycle resumes it. STUB_FAIL_ON is a comma-separated list of invocation
# numbers that fail (a single number is the common case), which is what lets a
# case describe a RUN of failures, and a run with a success in the middle of it.
# STUB_FAIL_MODE picks the wording written to stderr; see the case below. A
# failing invocation still emits its init event first, because that is what
# really happens: the session exists and then a request fails, which is exactly
# the case where throwing the session id away is the wrong move.
cat >"$BIN/claude" <<'STUB'
#!/usr/bin/env bash
n=$(( $(cat "$HOME/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$HOME/calls"
{
  echo "ARGV $*"
  for v in ANTHROPIC_MODEL ANTHROPIC_DEFAULT_FABLE_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL \
           ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL \
           ANTHROPIC_SMALL_FAST_MODEL; do
    if [ -n "${!v+set}" ]; then echo "ENV $v=${!v}"; else echo "ENV $v=<unset>"; fi
  done
} >"$HOME/dump.$n"
case ",${STUB_FAIL_ON:-},"  in
  *",$n,"*)
    # STUB_NO_SESSION suppresses the init event, which is the one case where the
    # supervisor really has no session id to keep: the request died before the
    # session existed.
    [ -n "${STUB_NO_SESSION:-}" ] || printf '{"type":"system","subtype":"init","session_id":"S%s"}\n' "$n"
    case "${STUB_FAIL_MODE:-limit}" in
      # Hand-written, and deliberately so: it is the shape the classifier was
      # first written against, not evidence about any upstream.
      limit)    echo "API Error: 429 rate limit exceeded" >&2 ;;
      # CAPTURED, NOT COMPOSED. This is Claude Code's own message when the
      # account allowance is exhausted, epoch and all -- the same string the
      # design review quoted when it asked for a real fixture here. Do not
      # paraphrase it, do not tidy the pipe: an upstream rewording is precisely
      # what this fixture exists to turn red.
      captured) echo "Claude AI usage limit reached|1755772800" >&2 ;;
      # The near-miss wording that motivated the `limit reached` alternative in
      # USAGE_LIMIT_RE: no `rate limit`, no `usage limit`, no 429.
      window)   echo "5-hour limit reached, resets at 3pm" >&2 ;;
      # Limit-SHAPED but outside the pattern on purpose: a spend cap, not a rate
      # cap. This is the classifier's negative direction, and it must stay a
      # miss if the pattern grows further.
      nearmiss) echo "API Error: 403 Your credit balance is too low to continue" >&2 ;;
      *)        echo "API Error: 400 invalid request" >&2 ;;
    esac
    exit 1 ;;
esac
printf '{"type":"system","subtype":"init","session_id":"S%s"}\n' "$n"
printf '{"type":"result","subtype":"success","session_id":"S%s","result":"ok"}\n' "$n"
exit 0
STUB
chmod +x "$BIN"/*

# Persona directories built to be broken, for the startup checks that only a
# mounted PERSONA_DIR can reach. The shipped personas/ cannot express any of
# these, and all three used to be silent: the first crash-looped the container
# with nothing but cat's own message, the second resolved and reviewed a PR with
# no identity behind the label it signed, the third is what the flat pre-modes
# layout now looks like from the inside.
NO_SHARED="$WORK/personas-no-shared"; mkdir -p "$NO_SHARED/code" "$NO_SHARED/plan"
cp "$SCRIPT_DIR/personas/code/red_team.md" "$NO_SHARED/code/red_team.md"
cp "$SCRIPT_DIR/personas/plan/_shared.md" "$NO_SHARED/plan/_shared.md"
cp "$SCRIPT_DIR/personas/plan/red_team.md" "$NO_SHARED/plan/red_team.md"

HOLLOW="$WORK/personas-hollow"; mkdir -p "$HOLLOW/code" "$HOLLOW/plan"
cp "$SCRIPT_DIR/personas/code/_shared.md" "$HOLLOW/code/_shared.md"
cp "$SCRIPT_DIR/personas/plan/_shared.md" "$HOLLOW/plan/_shared.md"
printf -- '---\nlabel: Hollow\nsuccess: Nothing at all.\n---\n' >"$HOLLOW/code/hollow.md"
cp "$HOLLOW/code/hollow.md" "$HOLLOW/plan/hollow.md"

# The layout phase 1 shipped: persona files directly in PERSONA_DIR, no
# subdirectories. Reachable by exactly the mount-your-own-personas workflow the
# docs advertise, so it has to say what changed rather than dying on a missing file.
FLAT="$WORK/personas-flat"; mkdir -p "$FLAT"
cp "$SCRIPT_DIR/personas/code/_shared.md" "$SCRIPT_DIR/personas/code/red_team.md" "$FLAT/"

PASS=0; FAIL=0; SKIP=0
FAILED_LABELS=""

# Run the entrypoint once. $1 = label, rest = VAR=VALUE.
run_entrypoint() {
  local label="$1"; shift
  HOME_DIR="$WORK/home"; OUT="$WORK/out"
  rm -rf "$HOME_DIR"; mkdir -p "$HOME_DIR/work/repo/.git" "$HOME_DIR/seed"
  env -i PATH="$BIN:$PATH" HOME="$HOME_DIR" \
    ALLOW_UNHARDENED=1 \
    GITHUB_TOKEN=x GITHUB_REPOSITORY=owner/repo PR_IDS=1 \
    REPO_PATH="$HOME_DIR/seed" REVIEW_INTERVAL_SECONDS=1 \
    PERSONA_DIR="$SCRIPT_DIR/personas" \
    PROVIDER=ollama OLLAMA_API_KEY=k \
    "$@" "$BASH_BIN" "$ENTRYPOINT" >"$OUT" 2>&1
}

ok()  { PASS=$((PASS + 1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); FAILED_LABELS="$FAILED_LABELS
  - $1"; printf 'FAIL %s\n       %s\n' "$1" "$2"; [ -s "$OUT" ] && sed 's/^/       | /' "$OUT"; }

selected() {
  [ -z "$FILTER" ] && return 0
  case "$1" in *"$FILTER"*) return 0 ;; *) SKIP=$((SKIP + 1)); return 1 ;; esac
}

# How many times the claude stub was called.
calls() { cat "$HOME_DIR/calls" 2>/dev/null || echo 0; }

# refuses LABEL EXPECTED-SUBSTRING -- VAR=VALUE...
refuses() {
  local label="$1" want="$2"; shift 2
  [ "${1:-}" = "--" ] && shift
  selected "$label" || return 0
  run_entrypoint "$label" "$@"
  if [ "$(calls)" != 0 ]; then
    bad "$label" "expected a startup failure, but a review pass ran"
  elif ! grep -qF "$want" "$OUT"; then
    bad "$label" "expected the error to mention: $want"
  else
    ok "$label"
  fi
}

# cycle LABEL 'VAR=VALUE...' -- EXPECTATION...
# Runs the entrypoint, then checks expectations:
#   CALLS:N              -- exactly N claude invocations happened
#   ARGV:N:substring     -- invocation N's argv contains substring
#   NOARGV:N:substring   -- invocation N's argv does NOT contain substring
#   ENV:N:VAR=value      -- invocation N saw exactly that value
#   LOG:substring        -- the run's log contains substring
#   NOLOG:substring      -- the run's log does not contain substring
cycle() {
  local label="$1"; shift
  local -a env_in=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do env_in+=("$1"); shift; done
  [ "${1:-}" = "--" ] && shift
  selected "$label" || return 0
  run_entrypoint "$label" ${env_in[@]+"${env_in[@]}"}
  local expect missing="" n rest dump
  for expect in "$@"; do
    case "$expect" in
      CALLS:*)
        [ "$(calls)" = "${expect#CALLS:}" ] || missing="$missing [expected ${expect#CALLS:} invocations, got $(calls)]" ;;
      ARGV:*|NOARGV:*|ENV:*)
        rest="${expect#*:}"; n="${rest%%:*}"; rest="${rest#*:}"
        dump="$HOME_DIR/dump.$n"
        if [ ! -e "$dump" ]; then missing="$missing [no invocation $n]"; continue; fi
        case "$expect" in
          # The ARGV line can itself contain literal newlines (a persona's
          # --append-system-prompt value is multi-paragraph), so isolating it
          # is "everything before the first ENV line", not "lines starting
          # with ARGV " — the latter would silently truncate at the value's
          # first blank line.
          ARGV:*)   grep -qF -- "$rest" <(awk '/^ENV /{exit} {print}' "$dump") || missing="$missing [argv $n missing: $rest]" ;;
          NOARGV:*) grep -qF -- "$rest" <(awk '/^ENV /{exit} {print}' "$dump") && missing="$missing [argv $n should not have: $rest]" ;;
          ENV:*)    grep -qxF "ENV $rest" "$dump" || missing="$missing [env $n: $rest (was: $(grep "^ENV ${rest%%=*}=" "$dump" | sed 's/^ENV //'))]" ;;
        esac ;;
      LOG:*)   grep -qF "${expect#LOG:}" "$OUT" || missing="$missing [log missing: ${expect#LOG:}]" ;;
      NOLOG:*) grep -qF "${expect#NOLOG:}" "$OUT" && missing="$missing [log should not have: ${expect#NOLOG:}]" ;;
      *) missing="$missing [unknown expectation: $expect]" ;;
    esac
  done
  if [ -n "$missing" ]; then bad "$label" "$missing"; else ok "$label"; fi
}

printf 'Running persona tests with %s (bash %s)\n\n' "$BASH_BIN" "$("$BASH_BIN" -c 'echo $BASH_VERSION')"

# --- definition files (static checks, no entrypoint run) --------------------
if selected "definitions: every persona file is well formed"; then
  problems=""
  for tree in code plan; do
    for f in "$SCRIPT_DIR/personas/$tree"/*.md; do
      b="$(basename "$f" .md)"
      case "$b" in _*) continue ;; esac
      head -1 "$f" | grep -qx -- "---" || problems="$problems [$tree/$b: no frontmatter]"
      grep -qE '^label: [A-Za-z0-9 ._-]+$' "$f" || problems="$problems [$tree/$b: no usable label]"
      grep -q '^success: ' "$f" || problems="$problems [$tree/$b: no success criterion]"
      grep -qF "JSON" "$f" && problems="$problems [$tree/$b: carries an output contract]"
    done
    [ -f "$SCRIPT_DIR/personas/$tree/_shared.md" ] || problems="$problems [$tree/_shared.md missing]"
    grep -qF '{{PERSONA}}' "$SCRIPT_DIR/personas/$tree/_shared.md" || problems="$problems [$tree/_shared.md has no {{PERSONA}} token]"
  done
  if [ -n "$problems" ]; then bad "definitions: every persona file is well formed" "$problems"
  else ok "definitions: every persona file is well formed"; fi
fi

# --- selection --------------------------------------------------------------
cycle "selection: default set is the four code-facing personas in order" \
  -- CALLS:8 LOG:"code personas: red_team adversarial sme sage"

cycle "selection: an explicit list is honoured, in the order given" \
  PERSONAS=sage,red_team \
  -- CALLS:4 LOG:"code personas: sage red_team"

cycle "selection: all expands to every shipped persona" \
  PERSONAS=all \
  -- CALLS:12 LOG:"code personas: adversarial good_friend red_team sage sme user"

refuses "selection: an unknown persona name refuses at startup" \
  "unknown persona 'red-team'" \
  -- PERSONAS=red-team

refuses "selection: the reserved aggregate id cannot be selected" \
  "reserved" \
  -- PERSONAS=aggregate

refuses "selection: an empty list refuses at startup" \
  "PERSONAS is set but names no persona" \
  -- PERSONAS=,

refuses "selection: a duplicate name refuses at startup" \
  "listed twice" \
  -- PERSONAS=sage,sage

refuses "selection: a missing persona directory refuses at startup" \
  "no persona definitions" \
  -- PERSONA_DIR=/nonexistent

refuses "selection: a persona directory with no output contract refuses at startup" \
  "no output contract" \
  -- PERSONA_DIR="$NO_SHARED" PERSONAS=red_team

refuses "selection: a persona that is only frontmatter refuses at startup" \
  "empty prompt body" \
  -- PERSONA_DIR="$HOLLOW" PERSONAS=hollow PLAN_PERSONAS=hollow

# The token split has to be unquoted to split on the separators, which also
# makes it glob. Without `set -f` the name reaching the error is whatever the
# startup directory happens to contain, and a directory holding a file named
# after a persona would select it silently.
refuses "selection: a glob is a name, not a pattern" \
  "unknown persona '*'" \
  -- PERSONAS='*'

# --- per-persona passes -----------------------------------------------------
cycle "passes: each persona gets its own pass, in the selected order" \
  PERSONAS=red_team,sage \
  -- CALLS:4 \
     ARGV:1:"You are a Red Team security reviewer" \
     ARGV:2:"You are a Sage" \
     ARGV:3:"You are a Red Team security reviewer" \
     ARGV:4:"You are a Sage"

# Both assertions are adjacency assertions, the same technique the --resume ones
# use. Asserting the two strings separately would pass just as happily with the
# persona concatenated onto the task prompt, which is the arrangement this case
# exists to rule out: the flag has to be followed by the persona, and the `--`
# that ends the flags has to be followed by the task prompt with nothing wedged
# in between.
cycle "passes: the persona travels in the system prompt, not the task prompt" \
  PERSONAS=red_team \
  -- ARGV:1:"--append-system-prompt You are a Red Team security reviewer" \
     ARGV:1:"-- Perform a thorough review of pull request #1"

# The persona-specific signature lives in the system prompt (see the case above),
# so a default prompt carrying its own signature sentence would contradict it and
# the winner would be model-dependent. Asserted on both a new and a resumed pass,
# because DEFAULT_PROMPT and DEFAULT_FOLLOWUP each had the sentence.
cycle "prompts: the defaults carry no signature instruction of their own" \
  PERSONAS=red_team \
  -- CALLS:2 \
     NOARGV:1:"Sign your comments with '-claudebox'." \
     NOARGV:2:"Sign your comments with '-claudebox'." \
     ARGV:1:"-claudebox (Red Team)" \
     ARGV:2:"-claudebox (Red Team)"

cycle "passes: the persona label is substituted into the shared contract" \
  PERSONAS=sme \
  -- ARGV:1:"-claudebox (Subject Matter Expert)" \
     NOARGV:1:"{{PERSONA}}"

cycle "resume: cycle one starts a session, cycle two resumes that persona's own" \
  PERSONAS=red_team,sage \
  -- CALLS:4 \
     NOARGV:1:"--resume" \
     NOARGV:2:"--resume" \
     ARGV:3:"--resume S1" \
     ARGV:4:"--resume S2"

cycle "resume: the persona system prompt is re-passed on a resumed pass" \
  PERSONAS=red_team \
  -- CALLS:2 \
     ARGV:2:"--resume S1" \
     ARGV:2:"--append-system-prompt" \
     ARGV:2:"You are a Red Team security reviewer"

cycle "resume: MAX_PASSES_PER_SESSION rotates per (PR, persona) pair" \
  PERSONAS=red_team,sage MAX_PASSES_PER_SESSION=1 \
  -- CALLS:4 \
     NOARGV:3:"--resume" \
     NOARGV:4:"--resume" \
     LOG:"PR #1 [code/red_team] reached MAX_PASSES_PER_SESSION=1"

cycle "model: every tier still points at the one review model" \
  PERSONAS=red_team \
  -- ENV:1:ANTHROPIC_MODEL=glm-5.2:cloud \
     ENV:1:ANTHROPIC_DEFAULT_OPUS_MODEL=glm-5.2:cloud \
     ENV:1:ANTHROPIC_SMALL_FAST_MODEL=glm-5.2:cloud

# --- usage limits -----------------------------------------------------------
# A limit is not a broken session. Dropping the session id would make the next
# attempt re-read the whole PR and re-post findings already posted, spending more
# of the resource that just ran out.
cycle "limits: a rate-limited pass keeps its session and resumes next cycle" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=limit \
  -- CALLS:2 \
     ARGV:2:"--resume S1" \
     LOG:"hit a usage or rate limit" \
     LOG:"Backing off"

cycle "limits: an ordinary failure still drops the session" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=other \
  -- CALLS:2 \
     NOARGV:2:"--resume" \
     LOG:"starting a fresh session for it next cycle" \
     NOLOG:"Backing off"

cycle "limits: the rest of the cycle is abandoned, not pushed through" \
  PERSONAS=red_team,sage,sme STUB_FAIL_ON=2 STUB_FAIL_MODE=limit STUB_MAX_CYCLES=1 \
  -- CALLS:2 \
     LOG:"ending this cycle early"

cycle "limits: a real captured limit message is classified as a limit" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=captured \
  -- CALLS:2 \
     ARGV:2:"--resume S1" \
     LOG:"hit a usage or rate limit" \
     LOG:"Backing off"

cycle "limits: the near-miss window wording is classified as a limit" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=window \
  -- CALLS:2 \
     ARGV:2:"--resume S1" \
     LOG:"Backing off"

# The classifier's negative direction: limit-shaped, deliberately outside the
# pattern. If the pattern ever grows wide enough to swallow a spend-cap error,
# this goes red rather than the loop quietly backing off for hours on a failure
# that no amount of waiting fixes.
cycle "limits: an unrecognised failure degrades to the ordinary path" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=nearmiss STUB_MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"starting a fresh session for it next cycle" \
     NOLOG:"Backing off"

# The matched line itself reaches the log. is_usage_limit scans the whole stderr
# while the WARN line tails only its last few, so without this a limit reported
# early in a long stderr is classified right and invisible.
cycle "limits: the line that read as a limit is logged" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=captured STUB_MAX_CYCLES=1 \
  -- LOG:"limit reported by claude: Claude AI usage limit reached|1755772800"

# On a genuinely first pass there is no session to keep, and the log used to say
# there was.
cycle "limits: a limit before any session does not claim to keep one" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=limit STUB_NO_SESSION=1 STUB_MAX_CYCLES=1 \
  -- LOG:"before it had a session" \
     NOLOG:"keeping its session"

# --- cycles cut short --------------------------------------------------------
# A cycle that always restarted at the first pair would, under a limit that only
# allows a few passes per backoff window, review the leading pairs forever and
# the trailing ones never.
cycle "resume: the next cycle starts at the pair after the one a limit cut" \
  PERSONAS=red_team,sage,sme STUB_FAIL_ON=2 STUB_FAIL_MODE=limit \
  -- CALLS:5 \
     LOG:"Not reviewed this cycle: 1:code:sme" \
     LOG:"Starting this cycle at 1:code:sme, where the last one was cut." \
     ARGV:3:"You are a Subject Matter Expert" \
     NOARGV:3:"--resume" \
     ARGV:4:"--resume S1" \
     ARGV:5:"--resume S2"

# --- mode routing ------------------------------------------------------------
# Mode is decided once per PR, inside enumerate_candidate_prs, from its labels.
cycle "mode: an unlabeled PR is reviewed in code mode" \
  PERSONAS=red_team STUB_MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"Candidate PRs (ids): 1:code" \
     LOG:"Reviewing PR #1 [code/red_team]"

cycle "mode: a PR carrying the plan label is reviewed in plan mode" \
  PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 STUB_MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"Candidate PRs (ids): 1:plan" \
     LOG:"Reviewing PR #1 [plan/red_team]"

cycle "mode: PLAN_LABEL names the label that means plan" \
  PERSONAS=red_team PLAN_PERSONAS=red_team PLAN_LABEL=proposal STUB_PLAN_PRS=1 STUB_PLAN_LABEL=proposal STUB_MAX_CYCLES=1 \
  -- CALLS:1 LOG:"1:plan"

cycle "mode: a label that is not PLAN_LABEL leaves the PR in code mode" \
  PERSONAS=red_team PLAN_LABEL=proposal STUB_PLAN_PRS=1 STUB_PLAN_LABEL=plan STUB_MAX_CYCLES=1 \
  -- CALLS:1 LOG:"1:code"

cycle "mode: both modes can appear in one cycle" \
  PR_IDS=1,2 PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_PRS=2 STUB_MAX_CYCLES=1 \
  -- CALLS:2 \
     LOG:"Candidate PRs (ids): 1:code 2:plan"

# A failed label lookup must NOT fall back to code mode. Guessing posts real
# comments in the wrong register on a real PR, and there is no undoing that; a
# skip is one log line and a retry next cycle.
cycle "mode: a failed label lookup skips the PR rather than guessing" \
  PR_IDS=1,2 PERSONAS=red_team STUB_LABEL_FAIL=1 STUB_MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"could not read labels for PR #1" \
     LOG:"Candidate PRs (ids): 2:code" \
     NOLOG:"Reviewing PR #1"

# A `gh` that exits 0 but prints nothing is not a failed lookup by the ordinary
# check (it succeeded), so it must be caught separately or the PR vanishes from
# the cycle with no WARN at all -- indistinguishable from having been reviewed
# clean.
cycle "mode: a successful but empty label lookup skips the PR, not silently" \
  PR_IDS=1,2 PERSONAS=red_team STUB_LABEL_GARBAGE=1 STUB_MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"could not read labels for PR #1" \
     LOG:"Candidate PRs (ids): 2:code" \
     NOLOG:"Reviewing PR #1"

# A `gh` that exits 0 with a well-formed but useless body (bare `null`) must not
# turn into a candidate PR literally numbered "null".
cycle "mode: a null label response skips the PR rather than reviewing PR #null" \
  PR_IDS=1,2 PERSONAS=red_team STUB_LABEL_NULL=1 STUB_MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"could not read labels for PR #1" \
     LOG:"Candidate PRs (ids): 2:code" \
     NOLOG:"Reviewing PR #1" \
     NOLOG:"PR #null"

# --- per-mode persona sets ---------------------------------------------------
cycle "modes: plan mode runs all six personas by default" \
  STUB_PLAN_PRS=1 STUB_MAX_CYCLES=1 \
  -- CALLS:6 LOG:"plan personas: adversarial good_friend red_team sage sme user"

cycle "modes: code mode still runs the four code-facing personas by default" \
  STUB_MAX_CYCLES=1 \
  -- CALLS:4 LOG:"code personas: red_team adversarial sme sage"

cycle "modes: PLAN_PERSONAS selects the plan set, PERSONAS the code set" \
  PR_IDS=1,2 PERSONAS=red_team PLAN_PERSONAS=user,sage STUB_PLAN_PRS=2 STUB_MAX_CYCLES=1 \
  -- CALLS:3 \
     LOG:"code personas: red_team" \
     LOG:"plan personas: user sage" \
     ARGV:1:"You are a Red Team security reviewer" \
     ARGV:2:"You are a User advocate" \
     ARGV:3:"You are a Sage"

# Both modes resolve at startup, so a broken plan persona kills the container at
# boot rather than the first time somebody labels a PR.
refuses "modes: a broken plan persona refuses at startup even with no plan PR" \
  "unknown persona 'saeg'" \
  -- PLAN_PERSONAS=saeg

refuses "modes: a flat PERSONA_DIR says what the layout changed to" \
  "code/ and plan/" \
  -- PERSONA_DIR="$FLAT" PERSONAS=red_team

refuses "modes: a mode tree with no output contract refuses at startup" \
  "no output contract" \
  -- PERSONA_DIR="$NO_SHARED" PERSONAS=red_team

# The property the whole persona design rests on, now asserted per mode: the flag
# does not survive --resume, so a resumed plan pass must re-carry its plan persona.
cycle "modes: a resumed plan pass still carries its plan persona" \
  PLAN_PERSONAS=user STUB_PLAN_PRS=1 \
  -- CALLS:2 \
     ARGV:2:"--resume S1" \
     ARGV:2:"--append-system-prompt You are a User advocate"

# A label added between cycles changes the pair key, so the old session is
# orphaned and the new mode starts fresh rather than resuming a code session
# under a plan persona.
cycle "modes: a PR that gains the label starts a fresh session, not a resumed one" \
  PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_AFTER=1 \
  -- CALLS:2 \
     LOG:"Reviewing PR #1 [code/red_team]" \
     LOG:"Reviewing PR #1 [plan/red_team]" \
     NOARGV:2:"--resume"

# Three in a row that are not limits means the provider is unhealthy, not that
# these particular pairs are cursed. Walking the rest of the list into it costs
# one duplicate-comment burst per pair, since each failure drops its session.
cycle "failures: a run of non-limit failures abandons the cycle at the ordinary interval" \
  PR_IDS=1,2 PERSONAS=red_team,sage,sme STUB_FAIL_ON=2,3,4 STUB_FAIL_MODE=other STUB_MAX_CYCLES=1 \
  -- CALLS:4 \
     LOG:"3 passes in a row failed for reasons other than a limit" \
     LOG:"Not reviewed this cycle: 2:code:sage 2:code:sme" \
     LOG:"The next cycle starts at 2:code:sage" \
     NOLOG:"Backing off"

# ... and the counter is consecutive, not cumulative: two failures, a success,
# another failure is an ordinary bad day, not a dead endpoint.
cycle "failures: a success resets the run, so scattered failures do not abandon" \
  PERSONAS=red_team,sage,sme,adversarial STUB_FAIL_ON=1,2,4 STUB_FAIL_MODE=other STUB_MAX_CYCLES=1 \
  -- CALLS:4 \
     NOLOG:"Abandoning this cycle" \
     NOLOG:"Not reviewed this cycle"

# --- per-mode prompts --------------------------------------------------------
# The test stanza asks the reviewer to mentally revert production lines a test
# depends on. There are none in a plan, and a reviewer handed a design document
# will otherwise report missing tests in code nobody has written.
cycle "prompts: plan mode drops the test stanza and code mode keeps it" \
  PR_IDS=1,2 PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_PRS=2 STUB_MAX_CYCLES=1 \
  -- CALLS:2 \
     ARGV:1:"Treat the tests in this PR as code under review" \
     NOARGV:2:"Treat the tests in this PR as code under review"

cycle "prompts: the plan default says what a plan review is and is not" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 STUB_MAX_CYCLES=1 \
  -- ARGV:1:"proposes an approach rather than implementing one" \
     ARGV:1:"do not ask for tests, error handling, or input validation in code that does not exist yet"

# The gh constraints are what the privilege-minimized token can actually do, and
# they are identical in both modes.
cycle "prompts: the plan default keeps the gh stanza" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 STUB_MAX_CYCLES=1 \
  -- ARGV:1:'do not use `gh pr checks`'

# The code followup's own coverage: it lived as a bare scalar before this branch
# moved both prompts into MODE_FOLLOWUP_PROMPT[code], the refactor that could
# silently drop a stanza, and the gh/test text is now hand-maintained in four
# copies rather than two.
cycle "prompts: the code followup repeats the gh and test stanzas" \
  PERSONAS=red_team \
  -- CALLS:2 \
     ARGV:2:"Re-check pull request #1" \
     ARGV:2:'do not use `gh pr checks`' \
     ARGV:2:"Treat the tests in this PR as code under review"

# Repeated on resumed passes for the same reason the gh stanza is: a long-resumed
# session's earliest turns are the first thing a context summary drops.
# The "Re-read the plan" assertion is what tells the followup apart from the
# review prompt: both carry the plan stanza, so a resumed pass handed the review
# prompt by mistake would satisfy the stanza assertion and re-introduce the whole
# task from scratch on every cycle.
cycle "prompts: a resumed plan pass repeats the plan stanza" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 \
  -- CALLS:2 \
     ARGV:2:"--resume S1" \
     ARGV:2:"Re-read the plan in pull request" \
     ARGV:2:"do not ask for tests, error handling, or input validation in code that does not exist yet"

cycle "prompts: PLAN_REVIEW_PROMPT reaches Claude verbatim, with no stanzas" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 PLAN_REVIEW_PROMPT='just read the plan in 1' STUB_MAX_CYCLES=1 \
  -- ARGV:1:"just read the plan in 1" \
     NOARGV:1:'do not use `gh pr checks`' \
     NOARGV:1:"proposes an approach rather than implementing one"

cycle "prompts: PLAN_REVIEW_PROMPT_SUFFIX appends to the plan default" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 PLAN_REVIEW_PROMPT_SUFFIX='And mention the ticket.' STUB_MAX_CYCLES=1 \
  -- ARGV:1:"proposes an approach rather than implementing one" \
     ARGV:1:"And mention the ticket."

cycle "prompts: FOLLOWUP_PROMPT reaches Claude verbatim, with no stanzas" \
  PERSONAS=red_team FOLLOWUP_PROMPT='just re-check 1' \
  -- CALLS:2 \
     ARGV:2:"just re-check 1" \
     NOARGV:1:"just re-check 1" \
     NOARGV:2:'do not use `gh pr checks`' \
     NOARGV:2:"Treat the tests in this PR as code under review"

cycle "prompts: PLAN_FOLLOWUP_PROMPT reaches Claude verbatim, with no stanzas" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 PLAN_FOLLOWUP_PROMPT='just re-read the plan in 1' \
  -- CALLS:2 \
     ARGV:2:"--resume S1" \
     ARGV:2:"just re-read the plan in 1" \
     NOARGV:1:"just re-read the plan in 1" \
     NOARGV:2:'do not use `gh pr checks`' \
     NOARGV:2:"proposes an approach rather than implementing one"

cycle "prompts: FOLLOWUP_PROMPT_SUFFIX appends to the code followup default" \
  PERSONAS=red_team FOLLOWUP_PROMPT_SUFFIX='And mention the ticket.' \
  -- CALLS:2 \
     ARGV:2:"Re-check pull request #1" \
     ARGV:2:"And mention the ticket." \
     NOARGV:1:"And mention the ticket."

cycle "prompts: PLAN_FOLLOWUP_PROMPT_SUFFIX appends to the plan followup default" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 PLAN_FOLLOWUP_PROMPT_SUFFIX='And mention the ticket.' \
  -- CALLS:2 \
     ARGV:2:"Re-read the plan in pull request" \
     ARGV:2:"And mention the ticket." \
     NOARGV:1:"And mention the ticket."

# The code-mode overrides must not leak into plan mode, or an operator who tuned
# their code prompt would silently get it on plan PRs too.
cycle "prompts: a code override does not reach plan mode" \
  PR_IDS=1,2 PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_PRS=2 \
  REVIEW_PROMPT='code only 1' STUB_MAX_CYCLES=1 \
  -- CALLS:2 \
     ARGV:1:"code only 1" \
     NOARGV:2:"code only 1" \
     ARGV:2:"proposes an approach rather than implementing one"

printf '\n%d passed, %d failed' "$PASS" "$FAIL"
[ "$SKIP" -gt 0 ] && printf ', %d skipped' "$SKIP"
printf '\n'
[ "$FAIL" -eq 0 ] || { printf 'failed:%s\n' "$FAILED_LABELS"; exit 1; }
