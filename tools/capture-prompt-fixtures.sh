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
# GNU coreutils stdbuf is absent on macOS. The stream must reach tee, where
# the session id comes from; drop the flags and exec the rest.
cat >"$BIN/stdbuf" <<'STUB'
#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -*) shift ;; *) break ;; esac; done
exec "$@"
STUB
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

# Verification checks to ensure fixtures differ and are correct
echo ""
echo "=== Verification ==="
PASS=0
FAIL=0

# Check 1: code-review and code-followup differ
if cmp -s "$OUT_DIR/prompt-code-review.txt" "$OUT_DIR/prompt-code-followup.txt"; then
  echo "FAIL: code-review and code-followup are identical"
  FAIL=$((FAIL+1))
else
  echo "PASS: code-review and code-followup differ"
  PASS=$((PASS+1))
fi

# Check 2: plan-review and plan-followup differ
if cmp -s "$OUT_DIR/prompt-plan-review.txt" "$OUT_DIR/prompt-plan-followup.txt"; then
  echo "FAIL: plan-review and plan-followup are identical"
  FAIL=$((FAIL+1))
else
  echo "PASS: plan-review and plan-followup differ"
  PASS=$((PASS+1))
fi

# Check 3: code-followup contains followup-specific language
if grep -q "only post findings you haven't already raised" "$OUT_DIR/prompt-code-followup.txt"; then
  echo "PASS: code-followup contains followup instruction"
  PASS=$((PASS+1))
else
  echo "FAIL: code-followup missing followup instruction"
  FAIL=$((FAIL+1))
fi

# Check 4: plan-followup contains followup-specific language
if grep -q "A point you raised that the revision addresses is settled" "$OUT_DIR/prompt-plan-followup.txt"; then
  echo "PASS: plan-followup contains followup instruction"
  PASS=$((PASS+1))
else
  echo "FAIL: plan-followup missing followup instruction"
  FAIL=$((FAIL+1))
fi

# Check 5: all fixtures contain #1
for file in "$OUT_DIR/prompt-"*.txt; do
  if ! grep -q '#1' "$file"; then
    echo "FAIL: $file missing #1 token"
    FAIL=$((FAIL+1))
  else
    echo "PASS: $(basename "$file") contains #1"
    PASS=$((PASS+1))
  fi
done

# Check 6: code fixtures contain test stanza
if grep -q "mentally revert" "$OUT_DIR/prompt-code-review.txt"; then
  echo "PASS: code-review contains test stanza"
  PASS=$((PASS+1))
else
  echo "FAIL: code-review missing test stanza"
  FAIL=$((FAIL+1))
fi

# Check 7: plan fixtures do not contain test stanza
if grep -q "mentally revert" "$OUT_DIR/prompt-plan-review.txt"; then
  echo "FAIL: plan-review contains test stanza (should not)"
  FAIL=$((FAIL+1))
else
  echo "PASS: plan-review does not contain test stanza"
  PASS=$((PASS+1))
fi

# Check 8: linear fixture is superset of code-review
if grep -q "references a Linear ticket" "$OUT_DIR/prompt-code-review-linear.txt"; then
  echo "PASS: code-review-linear contains Linear stanza"
  PASS=$((PASS+1))
else
  echo "FAIL: code-review-linear missing Linear stanza"
  FAIL=$((FAIL+1))
fi

echo ""
echo "Verification: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] || exit 1
