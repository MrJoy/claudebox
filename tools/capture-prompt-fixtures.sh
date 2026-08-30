#!/usr/bin/env bash
# One-shot: capture the four default prompts (plus one Linear variant) as the
# PRE-PORT entrypoint.sh renders them, so the Python port can be asserted
# byte-for-byte against them rather than against a re-reading of the source.
#
# Run once, commit the fixtures, and leave this script in place as their
# provenance. It borrows test-personas.sh's stubbing technique wholesale.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$SCRIPT_DIR/tests/fixtures"
mkdir -p "$OUT_DIR"

BASH_BIN=""
for c in /opt/homebrew/bin/bash /usr/local/bin/bash "$(command -v bash)"; do
  [ -x "$c" ] || continue
  case "$("$c" -c 'echo ${BASH_VERSINFO[0]}')" in 4|5|6|7|8|9) BASH_BIN="$c"; break ;; esac
done
[ -n "$BASH_BIN" ] || { echo "need bash 4+"; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"

cat >"$BIN/gh" <<'STUB'
#!/bin/sh
case "$1 $2" in
  "pr view") echo '{"number":1,"labels":[]}' ;;
  "pr list") echo '[]' ;;
  *) : ;;
esac
exit 0
STUB
printf '#!/bin/sh\nexit 0\n' >"$BIN/git"
printf '#!/bin/sh\nexec "$@"\n' >"$BIN/stdbuf"
# Succeeds once so a second cycle runs, which is the only way to see a
# resumed pass's prompt. Fails after, ending the loop under set -e.
cat >"$BIN/sleep" <<'STUB'
#!/bin/sh
n=$(( $(cat "$HOME/slept" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$HOME/slept"
[ "$n" -le 1 ] || exit 1
exit 0
STUB
# Records the LAST argument of each invocation, which is the prompt (the `--`
# before it in run_pass guarantees that).
cat >"$BIN/claude" <<'STUB'
#!/usr/bin/env bash
n=$(( $(cat "$HOME/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" >"$HOME/calls"
printf '%s' "${!#}" >"$HOME/prompt.$n"
printf '{"type":"system","subtype":"init","session_id":"S1"}\n'
printf '{"type":"result","subtype":"success","session_id":"S1","result":"ok"}\n'
exit 0
STUB
chmod +x "$BIN"/*

# $1 = label for the pair of files, rest = extra env
capture() {
  local plan_label="$1" new_out="$2" resumed_out="$3"; shift 3
  local HOME_DIR="$WORK/home"
  rm -rf "$HOME_DIR"; mkdir -p "$HOME_DIR/work/repo/.git" "$HOME_DIR/seed"
  env -i PATH="$BIN:$PATH" HOME="$HOME_DIR" \
    ALLOW_UNHARDENED=1 \
    GITHUB_TOKEN=x GITHUB_REPOSITORY=owner/repo PR_IDS=1 \
    REPO_PATH="$HOME_DIR/seed" REVIEW_INTERVAL_SECONDS=1 \
    PERSONA_DIR="$SCRIPT_DIR/personas" \
    PERSONAS=red_team PLAN_PERSONAS=red_team \
    PLAN_LABEL="$plan_label" \
    PROVIDER=ollama OLLAMA_API_KEY=k \
    "$@" "$BASH_BIN" "$SCRIPT_DIR/entrypoint.sh" >/dev/null 2>&1 || true
  cp "$HOME_DIR/prompt.1" "$OUT_DIR/$new_out"
  cp "$HOME_DIR/prompt.2" "$OUT_DIR/$resumed_out"
  echo "wrote $new_out and $resumed_out"
}

# The gh stub above returns an empty label list, so no PR matches PLAN_LABEL and
# both cycles run in code mode.
capture plan prompt-code-review.txt prompt-code-followup.txt

# Force plan mode by relabeling: a gh stub that returns the plan label.
cat >"$BIN/gh" <<'STUB'
#!/bin/sh
case "$1 $2" in
  "pr view") echo '{"number":1,"labels":[{"name":"plan"}]}' ;;
  "pr list") echo '[]' ;;
  *) : ;;
esac
exit 0
STUB
chmod +x "$BIN/gh"
capture plan prompt-plan-review.txt prompt-plan-followup.txt

# Back to code mode, with Linear configured, to capture the linear stanza tail.
# Only the NEW-session prompt is wanted here, so this does not reuse `capture`
# (which insists on writing both files and would need a throwaway path for the
# second).
cat >"$BIN/gh" <<'STUB'
#!/bin/sh
case "$1 $2" in
  "pr view") echo '{"number":1,"labels":[]}' ;;
  "pr list") echo '[]' ;;
  *) : ;;
esac
exit 0
STUB
chmod +x "$BIN/gh"
HOME_DIR="$WORK/home"
rm -rf "$HOME_DIR"; mkdir -p "$HOME_DIR/work/repo/.git" "$HOME_DIR/seed"
env -i PATH="$BIN:$PATH" HOME="$HOME_DIR" \
  ALLOW_UNHARDENED=1 \
  GITHUB_TOKEN=x GITHUB_REPOSITORY=owner/repo PR_IDS=1 \
  REPO_PATH="$HOME_DIR/seed" REVIEW_INTERVAL_SECONDS=1 \
  PERSONA_DIR="$SCRIPT_DIR/personas" \
  PERSONAS=red_team PLAN_PERSONAS=red_team PLAN_LABEL=plan \
  PROVIDER=ollama OLLAMA_API_KEY=k \
  LINEAR_API_KEY=lin_test \
  "$BASH_BIN" "$SCRIPT_DIR/entrypoint.sh" >/dev/null 2>&1 || true
cp "$HOME_DIR/prompt.1" "$OUT_DIR/prompt-code-review-linear.txt"
echo "wrote prompt-code-review-linear.txt"
