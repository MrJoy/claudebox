#!/usr/bin/env bash
#
# Persona tests for entrypoint.sh. Same technique as test-providers.sh (stubs on
# PATH, `env -i`, ALLOW_UNHARDENED=1) with two deliberate differences:
#
#   * capture is INDEXED per `claude` invocation, because one cycle now runs one
#     invocation per (PR, persona) instead of exactly one;
#   * the baseline sets MAX_CYCLES=2, so the loop runs TWO cycles. That second
#     cycle is the point: a one-cycle harness produces no resumed invocation,
#     which is why test-providers.sh cannot assert FOLLOWUP_PROMPT's stanzas,
#     and the most important property of the persona design (the persona system
#     prompt being re-passed on a resumed pass) lives exactly there. Cases that
#     want a single cycle say MAX_CYCLES=1 for themselves.
#
#   The baseline also pins MAX_CONCURRENT_PASSES=1. A PR's personas run
#   concurrently by default, so which invocation is the second one is a race,
#   and every ARGV:N and STUB_FAIL_ON here is an ordinal. A cap of one is a
#   one-worker pool on the same code path as any other cap, so pinning it buys
#   determinism without testing a path that does not exist in production.
#
#   ./test-personas.sh            # run everything
#   ./test-personas.sh resume     # only cases whose label matches 'resume'
#
# Needs jq (the entrypoint generates its MCP and translator config through it),
# mktemp, python3 (the review supervisor is Python), and bash 4+ for the
# entrypoint itself.
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

# Resolved here, while a normal PATH is still in scope: the entrypoint runs
# under `env -i`, so the python3 stub cannot find a real interpreter itself.
REAL_PYTHON3="$(command -v python3 || true)"
[ -n "$REAL_PYTHON3" ] || { printf 'ERROR: python3 is required (the review supervisor is Python).\n' >&2; exit 1; }

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
#   STUB_PLAN_AFTER    -- the PR is unlabeled until N claude invocations have
#                         been made, then labeled. That is the same as "after
#                         cycle N" only for the single-PR, single-persona-per-
#                         mode case that uses it (see below)
#   STUB_FREEZE_HEAD   -- the PR reports the same head and the same update time
#                         in every cycle, i.e. nothing about it ever moves. The
#                         change gate is what that is for: a frozen PR is
#                         reviewed once and then skipped
cat >"$BIN/gh" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$HOME/gh-argv"
# Stage two of change detection asks the REST endpoint for inline diff comments.
# First, so it can never fall through into the `pr view` logic below.
if [ "$1" = "api" ]; then printf '[]\n'; exit 0; fi
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  n="$3"
  # Two different `pr view` calls arrive here now. Stage two asks for comments
  # and reviews; stage one asks for labels and the head. Dispatch on the --json
  # field list, which is the last argument of both.
  for a in "$@"; do last="$a"; done
  case "$last" in
    *comments*) printf '{"comments":[],"reviews":[]}\n'; exit 0 ;;
  esac
  # The change gate reviews a PR only when it moved. Cycle one is a first
  # sighting and always runs; cycle two needs a moved head, or the resumed
  # invocation this whole suite exists to assert would never happen. "Which
  # cycle is this" is read off the claude stub's counter -- $HOME/calls holds a
  # single integer, the number of invocations so far -- which is nonzero from
  # the first pass onward and so distinguishes cycle one from every cycle
  # after it. Reading it unlocked is safe because gh only ever runs from the
  # supervisor's own thread, between groups, when no claude stub is alive.
  # STUB_FREEZE_HEAD=1 pins the answer instead, which is how a case asserts
  # that the gate really does gate. Both stamps are far older than any settle
  # window, so no case here waits for a change to settle.
  oid=aaaaaaa; when=2020-01-01T00:00:00Z
  if [ -z "${STUB_FREEZE_HEAD:-}" ]; then
    c=$(cat "$HOME/calls" 2>/dev/null || echo 0)
    if [ "$c" -gt 0 ]; then oid=bbbbbbb; when=2020-01-02T00:00:00Z; fi
  fi
  meta="\"headRefOid\":\"$oid\",\"updatedAt\":\"$when\""
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
  # labeled. Cycles are counted by the claude stub through the same $HOME/calls
  # integer -- so this reads "after N passes", which is the same thing for the
  # single-PR, single-persona-per-mode case that uses it.
  if [ -n "${STUB_PLAN_AFTER:-}" ]; then
    c=$(cat "$HOME/calls" 2>/dev/null || echo 0)
    if [ "$c" -ge "$STUB_PLAN_AFTER" ]; then
      printf '{"number":%s,"labels":[{"name":"%s"}],%s}\n' \
        "$n" "${STUB_PLAN_LABEL:-plan}" "$meta"
      exit 0
    fi
  fi
  case ",${STUB_PLAN_PRS:-}," in
    *",$n,"*) printf '{"number":%s,"labels":[{"name":"%s"}],%s}\n' \
                "$n" "${STUB_PLAN_LABEL:-plan}" "$meta" ;;
    *)        printf '{"number":%s,"labels":[],%s}\n' "$n" "$meta" ;;
  esac
  exit 0
fi
exit 0
STUB
# Recording rather than silent, so a case can assert that a refusal happened
# before the entrypoint did any work at all. The pre-flight --check sits above
# `gh auth setup-git`, the git config calls and the working clone, so an absent
# $HOME/git-argv means nothing git-shaped ran.
cat >"$BIN/git" <<'STUB'
#!/bin/sh
printf '%s\n' "$*" >>"$HOME/git-argv"
exit 0
STUB

# How many cycles run is now the supervisor's own business (MAX_CYCLES in the
# baseline below), so `sleep` has no job left here beyond costing nothing: the
# entrypoint polls with it while waiting for a translator, and the loop's own
# interval is time.sleep() inside Python, out of reach of a PATH stub.
printf '#!/bin/sh\nexit 0\n' >"$BIN/sleep"

# entrypoint.sh ends in `exec python3 /opt/claudebox/reviewer/review_loop.py`,
# an image path that does not exist in a checkout. Remap it to this one; hand
# anything else to the real interpreter, which `env -i` would otherwise hide.
cat >"$BIN/python3" <<'STUB'
#!/bin/sh
case "$1" in
  */reviewer/review_loop.py) shift; exec "$REAL_PYTHON3" "$REVIEWER_MAIN" "$@" ;;
  *)                         exec "$REAL_PYTHON3" "$@" ;;
esac
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
#
# STUB_FAIL_PERSONA fails by persona label instead of by ordinal, which is what
# the parallel cases need: under concurrency nothing chooses which pass is the
# third one. STUB_HOLD holds each invocation open for that many seconds, so the
# in-flight high-water mark the stub records is a fact about the supervisor
# rather than a race with process spawn.
cat >"$BIN/claude" <<'STUB'
#!/usr/bin/env bash
# The invocation number is allocated under a mkdir lock. mkdir is atomic on
# every filesystem we care about, and a busy-wait is fine at this scale. Without
# it, concurrent passes collide and dump files are silently overwritten, which
# would make a parallel case pass while proving nothing.
#
# The same lock keeps the in-flight high-water mark, which is the only thing in
# this harness that can tell a group that really overlapped from one that ran
# fast in sequence: CALLS:8 is satisfied either way. STUB_HOLD (seconds, passed
# to /bin/sleep because the `sleep` on PATH is a no-op stub) makes the overlap
# window wide enough to observe without depending on process-spawn timing.
# Bounded, because an unbounded spin is worse here than a wrong answer: the
# watchdog kills the entrypoint pid, not a stub that outlived it, so an orphan
# holding the lock would spin a core until somebody noticed. Thirty seconds is
# far longer than any real holder, which does four file operations, so reaching
# the cap means the holder is gone; steal the lock and carry on.
hold_lock() {
  local waited=0
  while ! mkdir "$HOME/calls.lock" 2>/dev/null; do
    /bin/sleep 0.01
    waited=$((waited + 1))
    if [ "$waited" -ge 3000 ]; then
      echo "stub: stale calls.lock after 30s, stealing it" >&2
      rmdir "$HOME/calls.lock" 2>/dev/null || true
      waited=0
    fi
  done
}
# Armed before the increment, not after: a kill in between would otherwise leak
# the in-flight count upward and hand a later case a peak it never reached.
# counted guards the other direction, so an exit before the increment does not
# decrement a count this process never added to.
counted=0
release() {
  [ "$counted" = 1 ] || return 0
  hold_lock
  echo $(( $(cat "$HOME/inflight" 2>/dev/null || echo 1) - 1 )) >"$HOME/inflight"
  rmdir "$HOME/calls.lock"
}
# Every exit path, the failing ones included, or the mark drifts upward on a
# case whose passes merely followed one another.
trap release EXIT
hold_lock
n=$(( $(cat "$HOME/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$HOME/calls"
inflight=$(( $(cat "$HOME/inflight" 2>/dev/null || echo 0) + 1 ))
echo "$inflight" >"$HOME/inflight"
counted=1
[ "$inflight" -gt "$(cat "$HOME/maxinflight" 2>/dev/null || echo 0)" ] \
  && echo "$inflight" >"$HOME/maxinflight"
rmdir "$HOME/calls.lock"
{
  echo "ARGV $*"
  for v in ANTHROPIC_MODEL ANTHROPIC_DEFAULT_FABLE_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL \
           ANTHROPIC_DEFAULT_SONNET_MODEL ANTHROPIC_DEFAULT_HAIKU_MODEL \
           ANTHROPIC_SMALL_FAST_MODEL; do
    if [ -n "${!v+set}" ]; then echo "ENV $v=${!v}"; else echo "ENV $v=<unset>"; fi
  done
} >"$HOME/dump.$n"
[ -n "${STUB_HOLD:-}" ] && /bin/sleep "$STUB_HOLD"
should_fail=
case ",${STUB_FAIL_ON:-},"  in
  *",$n,"*) should_fail=1 ;;
esac
# STUB_FAIL_PERSONA: fail every invocation whose --append-system-prompt carries
# this label. Ordinal STUB_FAIL_ON still works and is what the cap-of-1 cases
# use; this is for the parallel block, where "invocation 3" names nothing.
if [ -n "${STUB_FAIL_PERSONA:-}" ] && printf '%s' "$*" | grep -qF -- "$STUB_FAIL_PERSONA"; then
  should_fail=1
fi
if [ -n "$should_fail" ]; then
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
  exit 1
fi
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

# A run that never ends is a suite that never ends. MAX_CYCLES is the only thing
# that stops the supervisor now, and a *missing* one means "loop forever": an
# unparsable value is a hard ConfigError, but a baseline that lost the variable
# entirely would hang here instead of failing. Kill anything still alive after
# two minutes and note it in the captured output, so the case fails on its own
# assertions with the reason visible rather than stalling the run.
watchdog_wait() {
  local pid="$1" i=0
  while kill -0 "$pid" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -gt 1200 ]; then
      kill -9 "$pid" 2>/dev/null
      printf 'WATCHDOG: run exceeded 120s and was killed\n' >>"$OUT"
      break
    fi
    sleep 0.1
  done
  wait "$pid" 2>/dev/null
}

# Run the entrypoint once. $1 = label, rest = VAR=VALUE.
run_entrypoint() {
  local label="$1"; shift
  HOME_DIR="$WORK/home"; OUT="$WORK/out"
  rm -rf "$HOME_DIR"; mkdir -p "$HOME_DIR/work/repo/.git" "$HOME_DIR/seed"
  # Both waits are real time.sleep() calls inside the supervisor now, so both
  # are pinned to a second: the backoff is 1800s by default, and one limit case
  # running two cycles would otherwise stall the suite for half an hour.
  env -i PATH="$BIN:$PATH" HOME="$HOME_DIR" \
    ALLOW_UNHARDENED=1 \
    GITHUB_TOKEN=x GITHUB_REPOSITORY=owner/repo PR_IDS=1 \
    REPO_PATH="$HOME_DIR/seed" REVIEW_INTERVAL_SECONDS=1 LIMIT_BACKOFF_SECONDS=1 \
    MAX_CYCLES=2 MAX_CONCURRENT_PASSES=1 \
    REAL_PYTHON3="$REAL_PYTHON3" REVIEWER_MAIN="$SCRIPT_DIR/reviewer/review_loop.py" \
    PERSONA_DIR="$SCRIPT_DIR/personas" \
    PROVIDER=ollama OLLAMA_API_KEY=k \
    "$@" "$BASH_BIN" "$ENTRYPOINT" >"$OUT" 2>&1 &
  watchdog_wait $!
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

# The most claude invocations that were ever alive at once. 1 means the passes
# strictly followed one another; anything higher is direct evidence of overlap,
# which no count of invocations can give.
max_inflight() { cat "$HOME_DIR/maxinflight" 2>/dev/null || echo 0; }

# One dump's argv, isolated the way the ARGV assertions isolate it: everything
# before the first ENV line, because a persona's system prompt is
# multi-paragraph and contains blank lines.
dump_argv() { awk '/^ENV /{exit} {print}' "$1"; }

# Under concurrency the dump INDEX means nothing, so assertions address a dump
# by what is in it. The selector is one or more substrings joined by `&&`, and a
# dump matches when its argv carries all of them -- which is how a case names
# "the resumed pass belonging to Red Team" without knowing its number.
dump_matches() {
  local d="$1" rest="$2" part
  while :; do
    case "$rest" in
      *"&&"*) part="${rest%%&&*}"; rest="${rest#*&&}" ;;
      *)      part="$rest"; rest="" ;;
    esac
    grep -qF -- "$part" <(dump_argv "$d") || return 1
    [ -n "$rest" ] || return 0
  done
}

# How many dumps match the selector.
count_matching() {
  local n=0 d
  for d in "$HOME_DIR"/dump.*; do
    [ -e "$d" ] || continue
    dump_matches "$d" "$1" && n=$((n + 1))
  done
  printf '%s' "$n"
}

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

# refuses_before_work LABEL EXPECTED-SUBSTRING -- VAR=VALUE...
# refuses, plus: no git and no gh ran before the refusal. Both recorders matter:
# `gh auth setup-git` is the first thing past the pre-flight, so watching git
# alone would still pass if the pre-flight slid below it. A selector or
# persona typo has to cost a startup error and nothing else -- the old shell
# validated both immediately after its defaults block, and moving them past the
# exec bought a network clone of the whole repo and up to 120s of translator
# startup on every restart under --restart unless-stopped.
refuses_before_work() {
  local label="$1" want="$2"; shift 2
  [ "${1:-}" = "--" ] && shift
  selected "$label" || return 0
  run_entrypoint "$label" "$@"
  if [ "$(calls)" != 0 ]; then
    bad "$label" "expected a startup failure, but a review pass ran"
  elif ! grep -qF "$want" "$OUT"; then
    bad "$label" "expected the error to mention: $want"
  elif [ -e "$HOME_DIR/git-argv" ]; then
    bad "$label" "refused only after running git: $(tr '\n' ';' <"$HOME_DIR/git-argv")"
  elif [ -e "$HOME_DIR/gh-argv" ]; then
    bad "$label" "refused only after running gh: $(tr '\n' ';' <"$HOME_DIR/gh-argv")"
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
#   MAXINFLIGHT:N        -- exactly N claude processes were alive at once at the
#                           peak: the one assertion here that can tell overlap
#                           from a fast sequence
#   MATCHCOUNT:sel:N     -- exactly N dumps' argv match sel, where sel is one or
#                           more substrings joined by `&&`. Content-keyed, for
#                           the parallel cases where "dump 3" names nothing
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
      MAXINFLIGHT:*)
        [ "$(max_inflight)" = "${expect#MAXINFLIGHT:}" ] \
          || missing="$missing [expected ${expect#MAXINFLIGHT:} passes in flight at the peak, got $(max_inflight)]" ;;
      # The count is the tail, so a selector may itself contain a colon.
      MATCHCOUNT:*)
        rest="${expect#MATCHCOUNT:}"; n="${rest##*:}"; rest="${rest%:*}"
        [ "$(count_matching "$rest")" = "$n" ] \
          || missing="$missing [expected $n dumps matching $rest, got $(count_matching "$rest")]" ;;
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

# --- pre-flight ordering -----------------------------------------------------
refuses_before_work "preflight: a persona typo refuses before any git runs" \
  "unknown persona 'saeg'" \
  -- PERSONAS=saeg

refuses_before_work "preflight: a missing PR selector refuses before any git runs" \
  "no PR selector set" \
  -- PR_IDS=

# The control for the two cases above: on a run that is not refused, both
# recorders do record. Without it the ordering assertion would pass just as
# happily against stubs that record nothing.
L="preflight: the git and gh recorders record on a run that is not refused"
if selected "$L"; then
  run_entrypoint "$L" PERSONAS=red_team MAX_CYCLES=1
  if [ ! -s "$HOME_DIR/git-argv" ]; then
    bad "$L" "no git invocations were recorded, so the ordering cases prove nothing"
  elif [ ! -s "$HOME_DIR/gh-argv" ]; then
    bad "$L" "no gh invocations were recorded, so the ordering cases prove nothing"
  else ok "$L"; fi
fi

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
     LOG:"[#1 code/red_team] reached MAX_PASSES_PER_SESSION=1"

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

# A limit cannot recall the siblings already in flight, and killing them would
# leave a pass that posted some of its findings and not the rest. So the group
# finishes and the cycle stops at the barrier.
cycle "limits: the limited persona's siblings still finish their pass" \
  PERSONAS=red_team,sage,sme STUB_FAIL_ON=2 STUB_FAIL_MODE=limit MAX_CYCLES=1 \
  -- CALLS:3 \
     LOG:"ending this cycle after the group finishes"

# What the cut does abandon is every group after it: walking the remaining PRs
# into the same wall spends more of the resource that just ran out.
cycle "limits: the PRs after the cut are not reviewed" \
  PR_IDS=1,2 PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=limit MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"Not reviewed this cycle: #2 code/red_team" \
     NOLOG:"Reviewing PR #2"

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
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=nearmiss MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"starting a fresh session for it next cycle" \
     NOLOG:"Backing off"

# The matched line itself reaches the log. is_usage_limit scans the whole stderr
# while the WARN line tails only its last few, so without this a limit reported
# early in a long stderr is classified right and invisible.
cycle "limits: the line that read as a limit is logged" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=captured MAX_CYCLES=1 \
  -- LOG:"limit reported by claude: Claude AI usage limit reached|1755772800"

# On a genuinely first pass there is no session to keep, and the log used to say
# there was.
cycle "limits: a limit before any session does not claim to keep one" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=limit STUB_NO_SESSION=1 MAX_CYCLES=1 \
  -- LOG:"before it had a session" \
     NOLOG:"keeping its session"

# --- cycles cut short --------------------------------------------------------
# A cycle that always restarted at the first pair would, under a limit that only
# allows a few passes per backoff window, review the leading pairs forever and
# the trailing ones never.
cycle "resume: the next cycle re-runs only the persona the limit cut" \
  PERSONAS=red_team,sage,sme STUB_FAIL_ON=2 STUB_FAIL_MODE=limit \
  -- CALLS:4 \
     LOG:"Resuming with #1 code/sage." \
     NOLOG:"Not reviewed this cycle" \
     ARGV:3:"You are a Subject Matter Expert" \
     NOARGV:3:"--resume" \
     ARGV:4:"You are a Sage" \
     ARGV:4:"--resume S2"

# --- mode routing ------------------------------------------------------------
# Mode is decided once per PR, inside enumerate_candidate_prs, from its labels.
cycle "mode: an unlabeled PR is reviewed in code mode" \
  PERSONAS=red_team MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"Candidate PRs (ids): 1:code" \
     LOG:"Reviewing PR #1 [code/red_team]"

cycle "mode: a PR carrying the plan label is reviewed in plan mode" \
  PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"Candidate PRs (ids): 1:plan" \
     LOG:"Reviewing PR #1 [plan/red_team]"

cycle "mode: PLAN_LABEL names the label that means plan" \
  PERSONAS=red_team PLAN_PERSONAS=red_team PLAN_LABEL=proposal STUB_PLAN_PRS=1 STUB_PLAN_LABEL=proposal MAX_CYCLES=1 \
  -- CALLS:1 LOG:"1:plan"

cycle "mode: a label that is not PLAN_LABEL leaves the PR in code mode" \
  PERSONAS=red_team PLAN_LABEL=proposal STUB_PLAN_PRS=1 STUB_PLAN_LABEL=plan MAX_CYCLES=1 \
  -- CALLS:1 LOG:"1:code"

cycle "mode: both modes can appear in one cycle" \
  PR_IDS=1,2 PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_PRS=2 MAX_CYCLES=1 \
  -- CALLS:2 \
     LOG:"Candidate PRs (ids): 1:code 2:plan"

# A failed label lookup must NOT fall back to code mode. Guessing posts real
# comments in the wrong register on a real PR, and there is no undoing that; a
# skip is one log line and a retry next cycle.
cycle "mode: a failed label lookup skips the PR rather than guessing" \
  PR_IDS=1,2 PERSONAS=red_team STUB_LABEL_FAIL=1 MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"could not read labels for PR #1" \
     LOG:"Candidate PRs (ids): 2:code" \
     NOLOG:"Reviewing PR #1"

# A `gh` that exits 0 but prints nothing is not a failed lookup by the ordinary
# check (it succeeded), so it must be caught separately or the PR vanishes from
# the cycle with no WARN at all -- indistinguishable from having been reviewed
# clean.
cycle "mode: a successful but empty label lookup skips the PR, not silently" \
  PR_IDS=1,2 PERSONAS=red_team STUB_LABEL_GARBAGE=1 MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"could not read labels for PR #1" \
     LOG:"Candidate PRs (ids): 2:code" \
     NOLOG:"Reviewing PR #1"

# A `gh` that exits 0 with a well-formed but useless body (bare `null`) must not
# turn into a candidate PR literally numbered "null".
cycle "mode: a null label response skips the PR rather than reviewing PR #null" \
  PR_IDS=1,2 PERSONAS=red_team STUB_LABEL_NULL=1 MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"could not read labels for PR #1" \
     LOG:"Candidate PRs (ids): 2:code" \
     NOLOG:"Reviewing PR #1" \
     NOLOG:"PR #null"

# --- per-mode persona sets ---------------------------------------------------
cycle "modes: plan mode runs all six personas by default" \
  STUB_PLAN_PRS=1 MAX_CYCLES=1 \
  -- CALLS:6 LOG:"plan personas: adversarial good_friend red_team sage sme user"

cycle "modes: code mode still runs the four code-facing personas by default" \
  MAX_CYCLES=1 \
  -- CALLS:4 LOG:"code personas: red_team adversarial sme sage"

cycle "modes: PLAN_PERSONAS selects the plan set, PERSONAS the code set" \
  PR_IDS=1,2 PERSONAS=red_team PLAN_PERSONAS=user,sage STUB_PLAN_PRS=2 MAX_CYCLES=1 \
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
# The count is taken at each barrier rather than per pass, so this is three
# whole groups that reviewed nothing.
# It then waits the backoff rather than the poll interval: the provider looks
# dead, every session it touched was dropped, and the retry is a fresh full
# review that the change gate cannot narrow. The wait is what test-python.sh
# pins; here the case runs one cycle and exits before the sleep.
cycle "failures: a run of non-limit failures abandons the cycle" \
  PR_IDS=1,2,3,4 PERSONAS=red_team STUB_FAIL_ON=1,2,3 STUB_FAIL_MODE=other MAX_CYCLES=1 \
  -- CALLS:3 \
     LOG:"3 passes in a row failed for reasons other than a limit" \
     LOG:"Not reviewed this cycle: #4 code/red_team" \
     NOLOG:"Reviewing PR #4"

# ... and the counter is consecutive, not cumulative: two failures, a group that
# reviewed something, another failure is an ordinary bad day, not a dead
# endpoint.
cycle "failures: a success resets the run, so scattered failures do not abandon" \
  PR_IDS=1,2,3,4 PERSONAS=red_team STUB_FAIL_ON=1,2,4 STUB_FAIL_MODE=other MAX_CYCLES=1 \
  -- CALLS:4 \
     NOLOG:"Abandoning this cycle" \
     NOLOG:"Not reviewed this cycle"

# Every failure in a group counts, not one per group: a PR whose whole persona
# set failed is three strikes on its own, and the next PR is not walked into the
# same wall.
cycle "failures: a whole group failing is counted pass by pass" \
  PR_IDS=1,2 PERSONAS=red_team,sage,sme STUB_FAIL_ON=1,2,3 STUB_FAIL_MODE=other MAX_CYCLES=1 \
  -- CALLS:3 \
     LOG:"3 passes in a row failed for reasons other than a limit" \
     NOLOG:"Reviewing PR #2"

# A group that reviewed something proves the provider is alive whatever its
# siblings did, so the failures inside it do not feed the count.
cycle "failures: one success in a group forgives its failing siblings" \
  PR_IDS=1,2 PERSONAS=red_team,sage,sme STUB_FAIL_ON=1,2,4,5 STUB_FAIL_MODE=other MAX_CYCLES=1 \
  -- CALLS:6 \
     NOLOG:"Abandoning this cycle"

# --- per-mode prompts --------------------------------------------------------
# The test stanza asks the reviewer to mentally revert production lines a test
# depends on. There are none in a plan, and a reviewer handed a design document
# will otherwise report missing tests in code nobody has written.
cycle "prompts: plan mode drops the test stanza and code mode keeps it" \
  PR_IDS=1,2 PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_PRS=2 MAX_CYCLES=1 \
  -- CALLS:2 \
     ARGV:1:"Treat the tests in this PR as code under review" \
     NOARGV:2:"Treat the tests in this PR as code under review"

cycle "prompts: the plan default says what a plan review is and is not" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 MAX_CYCLES=1 \
  -- ARGV:1:"proposes an approach rather than implementing one" \
     ARGV:1:"do not ask for tests, error handling, or input validation in code that does not exist yet"

# The gh constraints are what the privilege-minimized token can actually do, and
# they are identical in both modes.
cycle "prompts: the plan default keeps the gh stanza" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 MAX_CYCLES=1 \
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
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 PLAN_REVIEW_PROMPT='just read the plan in 1' MAX_CYCLES=1 \
  -- ARGV:1:"just read the plan in 1" \
     NOARGV:1:'do not use `gh pr checks`' \
     NOARGV:1:"proposes an approach rather than implementing one"

cycle "prompts: PLAN_REVIEW_PROMPT_SUFFIX appends to the plan default" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 PLAN_REVIEW_PROMPT_SUFFIX='And mention the ticket.' MAX_CYCLES=1 \
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
  REVIEW_PROMPT='code only 1' MAX_CYCLES=1 \
  -- CALLS:2 \
     ARGV:1:"code only 1" \
     NOARGV:2:"code only 1" \
     ARGV:2:"proposes an approach rather than implementing one"

# The opposite direction: a plan override must not leak into code mode either.
# The asymmetry is the kind of thing that hides a one-directional bug.
cycle "prompts: a plan override does not reach code mode" \
  PR_IDS=1,2 PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_PRS=2 \
  PLAN_REVIEW_PROMPT='plan only 2' MAX_CYCLES=1 \
  -- CALLS:2 \
     NOARGV:1:"plan only 2" \
     ARGV:2:"plan only 2"

# --- MCP flags --------------------------------------------------------------
# The reviewed repo is untrusted, so --strict-mcp-config is a security boundary,
# not a preference: without it a repo shipping its own .mcp.json gets MCP servers
# of its choosing loaded into a --dangerously-skip-permissions session. The
# supervisor builds these flags itself now, from MCP_CONFIG_FILE, so this asserts
# the end of that wire rather than the shell array that used to carry it -- on
# the RESUMED invocation too, since that is the one a per-session flag could be
# lost on.
cycle "mcp: every invocation is strict, resumed ones included" \
  PERSONAS=red_team \
  -- CALLS:2 \
     ARGV:1:"--strict-mcp-config" \
     ARGV:2:"--strict-mcp-config" \
     NOARGV:1:"--mcp-config"

# ... and with a Linear key the generated config is spliced in behind it, which
# is what proves MCP_CONFIG_FILE still crosses the exec into the supervisor.
cycle "mcp: a Linear key adds the generated config to every invocation" \
  PERSONAS=red_team LINEAR_API_KEY=lin_api_x \
  -- CALLS:2 \
     LOG:"Linear MCP enabled" \
     ARGV:1:"--strict-mcp-config --mcp-config" \
     ARGV:2:"--strict-mcp-config --mcp-config"

# --- parallel dispatch -------------------------------------------------------
# Everything above runs at MAX_CONCURRENT_PASSES=1 so its ordinal assertions
# mean something. These cases unset the cap, which is production's default, and
# address dumps by content instead. PR 12 rather than PR 1 throughout, so that
# "#12" in a prompt is a selector that cannot also match "#1".

# The floor: a group's personas each get their own pass, on both cycles.
cycle "parallel: every code persona of a group gets its own pass, both cycles" \
  PR_IDS=12 MAX_CONCURRENT_PASSES= \
  -- CALLS:8 \
     MATCHCOUNT:"You are a Red Team security reviewer":2 \
     MATCHCOUNT:"You are an Adversarial reviewer":2 \
     MATCHCOUNT:"You are a Subject Matter Expert":2 \
     MATCHCOUNT:"You are a Sage":2

# ... and they really do overlap. This is the assertion CALLS:8 cannot make: a
# supervisor that ran the four one after another satisfies every count above.
# STUB_HOLD keeps each pass alive long enough that the peak is a property of the
# dispatch rather than of how fast a process starts.
cycle "parallel: the personas of a group are in flight at the same time" \
  PR_IDS=12 MAX_CONCURRENT_PASSES= STUB_HOLD=0.3 MAX_CYCLES=1 \
  -- CALLS:4 MAXINFLIGHT:4

# The control for the case above, and the guarantee the whole inherited suite
# rests on: at a cap of one the group is strictly sequential, so the ordinal
# assertions up there are not a race that happens to be winning.
cycle "parallel: a cap of one runs the group one pass at a time" \
  PR_IDS=12 STUB_HOLD=0.3 MAX_CYCLES=1 \
  -- CALLS:4 MAXINFLIGHT:1

# ... and a cap between the two is honoured rather than rounded to either end.
cycle "parallel: MAX_CONCURRENT_PASSES caps the peak without dropping a pass" \
  PR_IDS=12 MAX_CONCURRENT_PASSES=2 STUB_HOLD=0.3 MAX_CYCLES=1 \
  -- CALLS:4 MAXINFLIGHT:2

# The property the persona design rests on, now under concurrency:
# --append-system-prompt does not survive --resume, so cycle two has to re-pass
# it. Four resumed passes, and each carries its OWN persona -- the part that a
# session map keyed on something coarser than the pair would break, and that
# MATCHCOUNT:--resume:4 on its own would not notice.
cycle "parallel: a resumed pass still carries its own persona" \
  PR_IDS=12 MAX_CONCURRENT_PASSES= \
  -- CALLS:8 \
     MATCHCOUNT:"--resume":4 \
     MATCHCOUNT:"--resume&&--append-system-prompt&&You are a Red Team security reviewer":1 \
     MATCHCOUNT:"--resume&&--append-system-prompt&&You are an Adversarial reviewer":1 \
     MATCHCOUNT:"--resume&&--append-system-prompt&&You are a Subject Matter Expert":1 \
     MATCHCOUNT:"--resume&&--append-system-prompt&&You are a Sage":1

# --- the shared working copy -------------------------------------------------
# The stanza is the half of the shared-clone defense the model can act on, and
# every pass needs it: a persona that checks out a branch corrupts what its
# siblings are reading, and a resumed pass is as able to do that as a new one.
cycle "worktree: the shared-copy stanza reaches every pass, resumed ones included" \
  PR_IDS=12 MAX_CONCURRENT_PASSES= \
  -- CALLS:8 \
     MATCHCOUNT:"One more constraint":8 \
     LOG:"Shared-worktree constraint active for: code plan"

# Nothing runs beside a pass at a cap of one, so the constraint is not true and
# is not asserted. Both modes have to be narrowed for real -- one clone serves
# both, and a plan PR can arrive on any cycle -- which the cap does by making
# each mode's effective concurrency one.
cycle "worktree: no shared-copy stanza when nothing runs beside the pass" \
  PR_IDS=12 \
  -- CALLS:8 \
     MATCHCOUNT:"One more constraint":0 \
     NOLOG:"Shared-worktree constraint active"

# The one place the verbatim-operator-prompt guarantee gives way. An operator
# who replaces REVIEW_PROMPT is not the person who pays for a persona writing to
# the shared tree, so the stanza is appended to the override too -- while the
# stanzas that are defaults-only stay away, which is what the two zero counts
# say.
cycle "worktree: the stanza is appended even to an operator prompt override" \
  PR_IDS=12 MAX_CONCURRENT_PASSES= REVIEW_PROMPT='look at #{{PR}}' MAX_CYCLES=1 \
  -- CALLS:4 \
     MATCHCOUNT:"look at #12":4 \
     MATCHCOUNT:"You are a Red Team security reviewer&&look at #12&&One more constraint":1 \
     MATCHCOUNT:"Perform a thorough review of pull request":0 \
     MATCHCOUNT:"Treat the tests in this PR as code under review":0

# --- limits and failures inside a group --------------------------------------
# A limit cannot recall the siblings already in flight, and killing them would
# leave a pass that posted some findings and not the rest, so the group finishes
# and the cycle stops at the barrier. What the cut owes is that persona alone:
# the next cycle re-runs Red Team and nobody else, and re-runs it RESUMED,
# because a limit is not a broken session.
cycle "parallel: a limit owes only the persona it cut, and its siblings finish" \
  PR_IDS=12 MAX_CONCURRENT_PASSES= STUB_FAIL_PERSONA="Red Team" STUB_FAIL_MODE=limit \
  -- CALLS:5 \
     MATCHCOUNT:"You are an Adversarial reviewer":1 \
     MATCHCOUNT:"You are a Subject Matter Expert":1 \
     MATCHCOUNT:"You are a Sage":1 \
     MATCHCOUNT:"You are a Red Team security reviewer":2 \
     MATCHCOUNT:"--resume":1 \
     MATCHCOUNT:"--resume&&You are a Red Team security reviewer":1 \
     LOG:"Owed next cycle: #12 code/red_team." \
     LOG:"Resuming with #12 code/red_team." \
     LOG:"Backing off" \
     NOLOG:"Not reviewed this cycle"

# The groups after the cut are abandoned whole -- walking them into the same
# wall spends more of the resource that just ran out -- and the next cycle
# starts PAST the cut rather than at the debt. Serving the debt first reads as
# the obvious thing and starves everything else: Red Team reports a limit on
# every attempt here, so a cycle that returned to #12 first would review #13
# never.
cycle "parallel: the cut abandons the groups after it and the next cycle starts past it" \
  PR_IDS=12,13 MAX_CONCURRENT_PASSES= STUB_FAIL_PERSONA="Red Team" STUB_FAIL_MODE=limit \
  -- CALLS:8 \
     MATCHCOUNT:"request #12":4 \
     MATCHCOUNT:"request #13":4 \
     MATCHCOUNT:"--resume":0 \
     LOG:"Not reviewed this cycle: #13 code/red_team #13 code/adversarial #13 code/sme #13 code/sage." \
     LOG:"Resuming with #13 code/red_team #13 code/adversarial #13 code/sme #13 code/sage #12 code/red_team." \
     LOG:"Not reviewed this cycle: #12 code/red_team #12 code/adversarial #12 code/sme #12 code/sage."

# A non-limit failure is not a cut: the group finishes, the cycle carries on to
# the next group, and the three siblings keep the sessions they opened. Red Team
# has none to keep -- it has never completed a pass -- which is why the compound
# count is zero rather than one.
cycle "parallel: a non-limit failure neither cuts the cycle nor costs its siblings their sessions" \
  PR_IDS=12 MAX_CONCURRENT_PASSES= STUB_FAIL_PERSONA="Red Team" STUB_FAIL_MODE=other \
  -- CALLS:8 \
     MATCHCOUNT:"--resume":3 \
     MATCHCOUNT:"--resume&&You are a Red Team security reviewer":0 \
     MATCHCOUNT:"You are a Red Team security reviewer":2 \
     LOG:"starting a fresh session for it next cycle" \
     NOLOG:"Not reviewed this cycle" \
     NOLOG:"Backing off"

# --- the change gate --------------------------------------------------------
# The Python suite proves the decision; these two prove the wiring end to end,
# and they are a matched pair. Same PR, frozen so that nothing about it ever
# moves: with the gate on it is reviewed once, with the gate off it is reviewed
# every cycle. The second is what turns red if the escape hatch stops working.
cycle "gate: a PR that has not moved is reviewed once, not again" \
  PR_IDS=12 PERSONAS=red_team STUB_FREEZE_HEAD=1 \
  -- CALLS:1 \
     NOARGV:1:"--resume" \
     LOG:"Unchanged since their last review: #12."

cycle "gate: REVIEW_ON_CHANGE=0 reviews the same frozen PR every cycle" \
  PR_IDS=12 PERSONAS=red_team STUB_FREEZE_HEAD=1 REVIEW_ON_CHANGE=0 \
  -- CALLS:2 \
     ARGV:2:"--resume S1" \
     NOLOG:"Unchanged since their last review"

printf '\n%d passed, %d failed' "$PASS" "$FAIL"
[ "$SKIP" -gt 0 ] && printf ', %d skipped' "$SKIP"
printf '\n'
[ "$FAIL" -eq 0 ] || { printf 'failed:%s\n' "$FAILED_LABELS"; exit 1; }
