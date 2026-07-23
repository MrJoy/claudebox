# Per-PR Sessions + PR Targeting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give claudebox a separate Claude Code session per PR (the harness enumerates candidate PRs and iterates), with CLI/env-file options to target which PRs are reviewed.

**Architecture:** `entrypoint.sh` (in-container supervisor) replaces its single continuous session with a per-PR session map and enumerates PRs via `gh` from one of four mutually-exclusive selectors. Prompts are PR-scoped via a `{{PR}}` token. `claudebox.sh` (host launcher) gains selector flags that pass through as `-e VAR=…`. Docs updated.

**Tech Stack:** Bash. `entrypoint.sh` runs inside the image on **modern bash** (bash 4+, so `declare -A` associative arrays are available). `claudebox.sh` runs on the **host** where macOS ships **bash 3.2** — keep it 3.2-safe (no associative arrays; expand possibly-empty arrays as `${arr[@]+"${arr[@]}"}`). No unit-test runner exists; verification is `bash -n`, launcher `--dry-run` assertions, and `awk`-extract + `eval` unit tests of the entrypoint's pure functions with a `gh` stub.

## Global Constraints

- Exactly one PR selector must be active. **Zero → error, more than one → error.** The authoritative check is in `entrypoint.sh` (covers env-file-only users). The launcher additionally errors on two selector *flags*.
- The four selectors and their env vars: `PR_ALL` (truthy: `1`/`true`/`yes`, case-insensitive), `PR_ASSIGNEE` (non-empty login), `PR_IDS` (non-empty comma/space list of integers), `PR_SEARCH` (non-empty gh search string). Launcher flags: `--all`, `--assignee LOGIN`, `--prs LIST`, `--search STR`.
- Prompt template token is exactly `{{PR}}`, substituted with the PR number via `${template//\{\{PR\}\}/$num}`. Applies to default AND custom `REVIEW_PROMPT`/`FOLLOWUP_PROMPT`. A template lacking `{{PR}}` produces a one-time WARN, not an error.
- `gh` enumeration uses `-R "$GITHUB_REPOSITORY" --json number --jq '.[].number' --limit 100`; `--all`/`--assignee` force `--state open`; `--search` lets the query control state; `--prs` uses the numbers verbatim.
- PRs are reviewed **sequentially**. `MAX_PASSES_PER_SESSION` now applies **per PR**. The PR→session map is **in-memory only** (restart may re-review once).
- Do NOT change: provider/backend selection, hardening checks, the working-clone strategy, or `--export-sessions`. `GITHUB_TOKEN`/`GITHUB_REPOSITORY` stay required.
- Sign review comments with `-claudebox` (unchanged).
- Verify with `bash -n entrypoint.sh` and `bash -n claudebox.sh` in every task that touches them.

**Test scaffolding used by Task 1 steps** (paths verbatim):
```
CB=/Users/jonathonfrisby/mrjoy/claudebox
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/2555f647-560a-4487-ad3b-1f1abfb0914c/scratchpad
```

`load_fn` — extracts one multi-line function's source from entrypoint.sh so a test can `eval` and call the REAL code (functions under test are all written `name() {` … lone `}`):
```bash
load_fn() { awk "/^$1\\(\\) \\{/,/^\\}/" "$CB/entrypoint.sh"; }
```

---

### Task 1: entrypoint — per-PR selection, loop, and prompt templating

**Files:**
- Modify: `entrypoint.sh` — insert PR-selection functions after `check_resource_limit` (`entrypoint.sh:57`); replace the prompt defaults (`entrypoint.sh:111-116`) and add a selection-resolution + template-warning block after them; replace the session-state init (`entrypoint.sh:253-254`), the `run_pass` function (`entrypoint.sh:285-311`), and the review loop (`entrypoint.sh:313-341`).

**Interfaces:**
- Produces (functions later steps/tasks rely on): `pr_truthy val`; `parse_pr_ids "list"` (echoes one integer per line, dies on non-integer); `resolve_pr_selection` (sets global `PR_SELECTOR` to `all|assignee|ids|search`, dies unless exactly one selector); `enumerate_candidate_prs` (echoes candidate PR numbers, one per line, per `PR_SELECTOR`); `render_prompt "template" NUM` (echoes template with `{{PR}}`→NUM); refactored `run_pass "prompt" "sid"` (sets global `RUN_PASS_SESSION_ID`, returns non-zero on failure).
- Consumes: existing `log`, `die`, `format_stream`, `$REVIEW_MODEL`, `$GITHUB_REPOSITORY`, `$MAX_PASSES_PER_SESSION`, `$REVIEW_INTERVAL_SECONDS`, `$WORK_REPO`.

- [ ] **Step 1: Insert the PR-selection functions** after the closing `}` of `check_resource_limit` (`entrypoint.sh:57`), before the `# Unprivileged user.` comment (`:59`). Insert this block:

```bash

# --- PR selection ----------------------------------------------------------
# Which PRs to review is chosen by exactly one selector env var. These helpers
# validate that choice and enumerate the candidate PR numbers each cycle.

# True (exit 0) when $1 is a truthy flag value: 1 / true / yes (any case).
pr_truthy() {
  case "$(printf '%s' "${1:-}" | tr 'A-Z' 'a-z')" in
    1|true|yes) return 0 ;;
    *) return 1 ;;
  esac
}

# Split a comma/whitespace-separated list of PR numbers into one-per-line,
# validating each is a positive integer (die otherwise). Word-splitting on the
# unquoted expansion does the comma->space splitting.
parse_pr_ids() {
  local raw="$1" tok
  for tok in $(printf '%s' "$raw" | tr ',' ' '); do
    case "$tok" in
      ''|*[!0-9]*) die "PR_IDS contains a non-numeric value: '$tok' (expected e.g. 12,15,20)" ;;
      *) printf '%s\n' "$tok" ;;
    esac
  done
}

# Determine the active selector; die unless EXACTLY ONE is provided. Sets the
# global PR_SELECTOR to one of: all | assignee | ids | search.
resolve_pr_selection() {
  local n=0
  PR_SELECTOR=""
  if pr_truthy "${PR_ALL:-}";        then n=$((n + 1)); PR_SELECTOR="all"; fi
  if [ -n "${PR_ASSIGNEE:-}" ];      then n=$((n + 1)); PR_SELECTOR="assignee"; fi
  if [ -n "${PR_IDS:-}" ];           then n=$((n + 1)); PR_SELECTOR="ids"; fi
  if [ -n "${PR_SEARCH:-}" ];        then n=$((n + 1)); PR_SELECTOR="search"; fi
  if [ "$n" -eq 0 ]; then
    die "no PR selector set; provide exactly one of PR_ALL, PR_ASSIGNEE, PR_IDS, PR_SEARCH (launcher: --all / --assignee / --prs / --search)."
  fi
  if [ "$n" -gt 1 ]; then
    die "multiple PR selectors set; provide exactly one of PR_ALL, PR_ASSIGNEE, PR_IDS, PR_SEARCH."
  fi
  # Validate the ID list up front so a bad value fails fast, not every cycle.
  [ "$PR_SELECTOR" = "ids" ] && parse_pr_ids "$PR_IDS" >/dev/null
  return 0
}

# Echo candidate PR numbers (one per line) for the active selector.
enumerate_candidate_prs() {
  case "$PR_SELECTOR" in
    all)      gh pr list -R "$GITHUB_REPOSITORY" --state open --limit 100 --json number --jq '.[].number' ;;
    assignee) gh pr list -R "$GITHUB_REPOSITORY" --state open --assignee "$PR_ASSIGNEE" --limit 100 --json number --jq '.[].number' ;;
    search)   gh pr list -R "$GITHUB_REPOSITORY" --search "$PR_SEARCH" --limit 100 --json number --jq '.[].number' ;;
    ids)      parse_pr_ids "$PR_IDS" ;;
  esac
}

# Substitute the {{PR}} token in a prompt template with a PR number.
render_prompt() {
  printf '%s' "${1//\{\{PR\}\}/$2}"
}
```

- [ ] **Step 2: Replace the prompt defaults** (`entrypoint.sh:111-116`). Replace this block:

```bash
DEFAULT_PROMPT="Please review open PRs to find unreviewed PRs, PRs in need of re-review, or PRs where your assistance has been requested (look for comments addressing 'claudebox'). Perform a thorough review / re-review of all such PRs. Pay particular attention to test quality/robustness, security, correctness, and architectural coherence/consistency. Also consider whether the approach the PR is taking is prudent and robust in light of the issue being addressed. Post findings as comments on the PR, one comment per finding. Be sure you're looking at the most recent commit on the branch. Sign your comments with '-claudebox'."
REVIEW_PROMPT="${REVIEW_PROMPT:-$DEFAULT_PROMPT}"
# Prompt used on resumed passes (the session already holds context from prior
# passes, so this nudges it to re-check rather than re-introduce the task).
DEFAULT_FOLLOWUP="I've fetched the latest refs. Re-check the repository for new or updated PRs since your last pass, any PRs still needing review, or PRs where your assistance has been requested (look for comments addressing 'claudebox'). Apply the same review standard. Only post findings that haven't already raised. Be sure you're looking at the most recent commit on each branch. Sign your comments with '-claudebox'."
FOLLOWUP_PROMPT="${FOLLOWUP_PROMPT:-$DEFAULT_FOLLOWUP}"
```
with:
```bash
# Prompts are PR-scoped: the harness runs one session per PR and substitutes the
# {{PR}} token with that PR's number. REVIEW_PROMPT starts a PR's session;
# FOLLOWUP_PROMPT is used when resuming it on a later cycle. Custom overrides use
# the same {{PR}} token.
DEFAULT_PROMPT="Perform a thorough review of pull request #{{PR}} in this repository. Inspect it with \`gh pr view {{PR}}\` and \`gh pr diff {{PR}}\`, and be sure you're looking at the most recent commit on its branch. Pay particular attention to test quality/robustness, security, correctness, and architectural coherence/consistency, and whether the approach the PR takes is prudent and robust in light of the issue it addresses. Post findings as comments on the PR, one comment per finding. Sign your comments with '-claudebox'."
REVIEW_PROMPT="${REVIEW_PROMPT:-$DEFAULT_PROMPT}"
# Prompt used when RESUMING a PR's session (it already holds context from prior
# passes on that PR, so this nudges a re-check rather than re-introducing the task).
DEFAULT_FOLLOWUP="I've fetched the latest refs. Re-check pull request #{{PR}} for new commits or changes since your last review of it. Apply the same review standard, and only post findings you haven't already raised on this PR. Be sure you're looking at the most recent commit on its branch. Sign your comments with '-claudebox'."
FOLLOWUP_PROMPT="${FOLLOWUP_PROMPT:-$DEFAULT_FOLLOWUP}"

# Validate PR selection now (fail fast, before auth/clone), and warn if a prompt
# template won't name the PR.
resolve_pr_selection
case "$REVIEW_PROMPT"   in *'{{PR}}'*) : ;; *) log "WARN: REVIEW_PROMPT has no {{PR}} token; reviews won't name the specific PR." ;; esac
case "$FOLLOWUP_PROMPT" in *'{{PR}}'*) : ;; *) log "WARN: FOLLOWUP_PROMPT has no {{PR}} token; reviews won't name the specific PR." ;; esac
```

- [ ] **Step 3: Replace the session-state init** (`entrypoint.sh:253-254`). Replace:
```bash
SESSION_ID=""
PASSES_THIS_SESSION=0
```
with:
```bash
# Per-PR state: session id and successful-pass count, keyed by PR number. These
# are in-memory only, so a container restart re-reviews each PR once (may
# re-comment once) — the same trade-off the old single-session design had.
declare -A PR_SESSION=()
declare -A PR_PASSES=()
```

- [ ] **Step 4: Replace `run_pass`** (`entrypoint.sh:285-311`). Replace the whole function:
```bash
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
```
with:
```bash
# Run one review pass for $1=prompt, resuming $2=session id when non-empty.
# On success sets RUN_PASS_SESSION_ID to the recovered id (falling back to the
# passed id) and returns 0; returns claude's exit code on failure.
RUN_PASS_SESSION_ID=""
run_pass() {
  local prompt="$1" sid="$2" rc errfile rawfile got
  RUN_PASS_SESSION_ID="$sid"
  errfile="$(mktemp)"; rawfile="$(mktemp)"
  # set +e around the pipeline so a formatter hiccup can't abort the script and
  # so we can read Claude's own exit code via PIPESTATUS[0] (not tee's/jq's).
  set +e
  if [ -n "$sid" ]; then
    claude -p --resume "$sid" --output-format stream-json --verbose \
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
  got="$(jq -r -R '(fromjson? // empty) | select(.session_id) | .session_id' "$rawfile" 2>/dev/null | tail -n 1 || true)"
  [ -n "$got" ] && RUN_PASS_SESSION_ID="$got"
  rm -f "$errfile" "$rawfile"
  return 0
}
```

- [ ] **Step 5: Replace the review loop** (`entrypoint.sh:313-341`). Replace the whole `while true; do … done` block:
```bash
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
```
with:
```bash
while true; do
  log "Fetching latest refs..."
  git fetch --all --prune --quiet || log "WARN: git fetch failed; continuing"

  # Re-enumerate every cycle so newly-matching PRs get picked up (PR_IDS is a
  # fixed set). Read the numbers into an array.
  prs=()
  while IFS= read -r _n; do [ -n "$_n" ] && prs+=("$_n"); done < <(enumerate_candidate_prs || true)

  if [ "${#prs[@]}" -eq 0 ]; then
    log "No candidate PRs for selector '$PR_SELECTOR'."
  else
    log "Candidate PRs ($PR_SELECTOR): ${prs[*]}"
  fi

  # Review each PR in its own session, sequentially (they share the one clone).
  for pr in ${prs[@]+"${prs[@]}"}; do
    sid="${PR_SESSION[$pr]:-}"
    if [ -z "$sid" ]; then
      log "Reviewing PR #$pr (new session)..."
      prompt="$(render_prompt "$REVIEW_PROMPT" "$pr")"
    else
      log "Reviewing PR #$pr (resuming session $sid)..."
      prompt="$(render_prompt "$FOLLOWUP_PROMPT" "$pr")"
    fi

    if run_pass "$prompt" "$sid"; then
      PR_SESSION[$pr]="$RUN_PASS_SESSION_ID"
      PR_PASSES[$pr]=$(( ${PR_PASSES[$pr]:-0} + 1 ))
      log "PR #$pr review complete (session ${PR_SESSION[$pr]}, pass ${PR_PASSES[$pr]})."
      # Rotate this PR's session once its cap is hit, to bound context growth.
      if [ "$MAX_PASSES_PER_SESSION" -gt 0 ] && [ "${PR_PASSES[$pr]}" -ge "$MAX_PASSES_PER_SESSION" ]; then
        log "PR #$pr reached MAX_PASSES_PER_SESSION=$MAX_PASSES_PER_SESSION; rotating its session next cycle."
        unset 'PR_SESSION[$pr]'
        PR_PASSES[$pr]=0
      fi
    else
      log "WARN: PR #$pr review failed; starting a fresh session for it next cycle."
      unset 'PR_SESSION[$pr]'
    fi
  done

  log "Sleeping ${REVIEW_INTERVAL_SECONDS}s..."
  sleep "$REVIEW_INTERVAL_SECONDS"
done
```

- [ ] **Step 6: Syntax check**

Run: `bash -n /Users/jonathonfrisby/mrjoy/claudebox/entrypoint.sh`
Expected: no output, exit 0.

- [ ] **Step 7: Unit-test `pr_truthy` and `render_prompt`**

Run:
```bash
CB=/Users/jonathonfrisby/mrjoy/claudebox
load_fn() { awk "/^$1\\(\\) \\{/,/^\\}/" "$CB/entrypoint.sh"; }
bash -c '
  '"$(declare -f load_fn)"'
  CB='"$CB"'
  eval "$(load_fn pr_truthy)"; eval "$(load_fn render_prompt)"
  for v in 1 true YES yes; do pr_truthy "$v" && echo "truthy:$v=yes" || echo "truthy:$v=NO"; done
  for v in "" 0 false no bogus; do pr_truthy "$v" && echo "falsy:$v=YES" || echo "falsy:$v=no"; done
  render_prompt "review #{{PR}} then #{{PR}} again" 42
  echo
'
```
Expected: `truthy:*=yes` for all of 1/true/YES/yes; `falsy:*=no` for all of ""/0/false/no/bogus; and the render line `review #42 then #42 again`.

- [ ] **Step 8: Unit-test `parse_pr_ids`**

Run:
```bash
CB=/Users/jonathonfrisby/mrjoy/claudebox
load_fn() { awk "/^$1\\(\\) \\{/,/^\\}/" "$CB/entrypoint.sh"; }
bash -c '
  '"$(declare -f load_fn)"'
  CB='"$CB"'
  die() { printf "DIE: %s\n" "$*" >&2; exit 1; }
  eval "$(load_fn parse_pr_ids)"
  echo "--- good ---"; parse_pr_ids "12, 15  20"
  echo "--- bad ---"; ( parse_pr_ids "12,x,20" ); echo "exit=$?"
'
```
Expected: under `--- good ---`, three lines `12` / `15` / `20`; under `--- bad ---`, a `DIE: PR_IDS contains a non-numeric value: 'x'…` line and `exit=1`.

- [ ] **Step 9: Unit-test `resolve_pr_selection` (zero / one / multiple / bad-ids)**

Run:
```bash
CB=/Users/jonathonfrisby/mrjoy/claudebox
load_fn() { awk "/^$1\\(\\) \\{/,/^\\}/" "$CB/entrypoint.sh"; }
runsel() {
  bash -c '
    '"$(declare -f load_fn)"'
    CB='"$CB"'
    die() { printf "DIE: %s\n" "$*" >&2; exit 1; }
    eval "$(load_fn pr_truthy)"; eval "$(load_fn parse_pr_ids)"; eval "$(load_fn resolve_pr_selection)"
    resolve_pr_selection && echo "OK PR_SELECTOR=$PR_SELECTOR"
  '
}
echo "--- none ---";      ( runsel ); echo "exit=$?"
echo "--- ids ok ---";    PR_IDS="12,15" runsel
echo "--- all ---";       PR_ALL=true runsel
echo "--- multiple ---";  ( PR_ALL=1 PR_ASSIGNEE=alice runsel ); echo "exit=$?"
echo "--- bad ids ---";   ( PR_IDS="12,x" runsel ); echo "exit=$?"
```
Expected: `--- none ---` → `DIE: no PR selector set…`, exit=1. `--- ids ok ---` → `OK PR_SELECTOR=ids`. `--- all ---` → `OK PR_SELECTOR=all`. `--- multiple ---` → `DIE: multiple PR selectors set…`, exit=1. `--- bad ids ---` → `DIE: PR_IDS contains a non-numeric value…`, exit=1.

- [ ] **Step 10: Unit-test `enumerate_candidate_prs` with a `gh` stub**

Run:
```bash
CB=/Users/jonathonfrisby/mrjoy/claudebox
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/2555f647-560a-4487-ad3b-1f1abfb0914c/scratchpad
load_fn() { awk "/^$1\\(\\) \\{/,/^\\}/" "$CB/entrypoint.sh"; }
mkdir -p "$SB/bin"
cat > "$SB/bin/gh" <<'EOF'
#!/usr/bin/env bash
# stub: echo fixed PR numbers for `gh pr list ... --jq '.[].number'`
printf '%s\n' 3 7 9
EOF
chmod +x "$SB/bin/gh"
bash -c '
  '"$(declare -f load_fn)"'
  CB='"$CB"'; export PATH='"$SB"'/bin:$PATH
  export GITHUB_REPOSITORY=acme/widgets
  die() { printf "DIE: %s\n" "$*" >&2; exit 1; }
  eval "$(load_fn parse_pr_ids)"; eval "$(load_fn enumerate_candidate_prs)"
  echo "--- all (via gh stub) ---";  PR_SELECTOR=all      enumerate_candidate_prs
  echo "--- ids (no gh) ---";        PR_SELECTOR=ids PR_IDS="21,22" enumerate_candidate_prs
'
```
Expected: `--- all (via gh stub) ---` → `3` / `7` / `9`; `--- ids (no gh) ---` → `21` / `22`.

- [ ] **Step 11: Commit**

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
git add entrypoint.sh
git commit -m "entrypoint: per-PR sessions with configurable PR targeting

Replace the single continuous session with a per-PR session map. The harness
enumerates candidate PRs from exactly one selector (PR_ALL / PR_ASSIGNEE /
PR_IDS / PR_SEARCH; zero or multiple is a hard error) and reviews each in its
own session, sequentially. Prompts are PR-scoped via a {{PR}} token (default
and custom). MAX_PASSES_PER_SESSION now applies per PR.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B4nPWbXrMg1D5bMFLZpYa"
```

---

### Task 2: launcher — selector flags and passthrough

**Files:**
- Modify: `claudebox.sh` — defaults block (`claudebox.sh:16-27` region, after Task-1-of-the-previous-feature edits it ends around the `TAIL=0`/`DRY_RUN=0` lines); arg parser (add four flags + a selector-count guard); `build_run_flags` (append `-e` passthrough); `usage()` heredoc (document the flags).

**Interfaces:**
- Consumes: existing `RUN_FLAGS` array, `show_and_run`, `die`, `set -u` array-expansion idiom.
- Produces: env passthrough `-e PR_ALL=1` / `-e PR_ASSIGNEE=…` / `-e PR_IDS=…` / `-e PR_SEARCH=…` on `run`/`test`.

- [ ] **Step 1: Add selector defaults.** In the defaults block, immediately after the `DRY_RUN=0` line, add:
```bash
# PR selectors (mutually exclusive; passed through to the container as -e VARs).
PR_ALL=0
PR_ASSIGNEE=""
PR_IDS=""
PR_SEARCH=""
PR_SEL_COUNT=0
PR_SEL_NAMES=""
```

- [ ] **Step 2: Add the four parser arms.** In the `while [ $# -gt 0 ]` arg parser, immediately after the `--tail)` arm, add:
```bash
    --all)         PR_ALL=1;      PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --all" ;;
    --assignee)    PR_ASSIGNEE="${2:?--assignee requires a LOGIN}"; PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --assignee"; shift ;;
    --prs)         PR_IDS="${2:?--prs requires a comma/space list of PR numbers}"; PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --prs"; shift ;;
    --search)      PR_SEARCH="${2:?--search requires a gh search query}"; PR_SEL_COUNT=$((PR_SEL_COUNT + 1)); PR_SEL_NAMES="$PR_SEL_NAMES --search"; shift ;;
```

- [ ] **Step 3: Guard against multiple selector flags.** Immediately after the `[ -n "$COMMAND" ] || { usage; exit 2; }` line, add:
```bash
# Selector flags are mutually exclusive (the entrypoint is authoritative and
# also errors when none/multiple are set via the env file; this is the friendly
# early check for CLI flags). Zero flags is fine here — the env file may set one.
[ "$PR_SEL_COUNT" -le 1 ] || die "multiple PR selector flags given ($(echo "$PR_SEL_NAMES" | xargs)); provide exactly one of --all, --assignee, --prs, --search."
```

- [ ] **Step 4: Append the passthrough** in `build_run_flags`. Immediately after the line that appends the hardening flags (`RUN_FLAGS+=(--cap-drop ALL --security-opt no-new-privileges --pids-limit "$PIDS" --memory "$MEMORY")`), add:
```bash

  # Pass any given PR selector through to the container. (An env-file value of
  # the same var is overridden by this -e; two selectors reaching the container
  # is what the entrypoint rejects.)
  [ "$PR_ALL" = 1 ]     && RUN_FLAGS+=(-e "PR_ALL=1")
  [ -n "$PR_ASSIGNEE" ] && RUN_FLAGS+=(-e "PR_ASSIGNEE=$PR_ASSIGNEE")
  [ -n "$PR_IDS" ]      && RUN_FLAGS+=(-e "PR_IDS=$PR_IDS")
  [ -n "$PR_SEARCH" ]   && RUN_FLAGS+=(-e "PR_SEARCH=$PR_SEARCH")
```

- [ ] **Step 5: Document the flags in `usage()`.** In the `OPTIONS` section of the heredoc, immediately after the `--tail` option lines, add:
```
  --all             Review all open PRs.
  --assignee LOGIN  Review open PRs assigned to this GitHub user.
  --prs LIST        Review exactly these PR numbers (comma/space list, e.g.
                    12,15,20).
  --search QUERY    Review PRs matching this gh search query (e.g.
                    "is:open label:needs-review"). You control state via the
                    query. Provide exactly ONE of --all/--assignee/--prs/--search
                    (here or via PR_* in the env file).
```
And in the `EXAMPLES` section, after the `cd ~/src/myrepo && claudebox run --tail` line, add:
```
  claudebox run --all --tail                                 # review every open PR
  claudebox run --assignee alice                             # PRs assigned to alice
  claudebox run --prs 12,15,20                               # just these PRs
```

- [ ] **Step 6: Syntax check**

Run: `bash -n /Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh`
Expected: no output, exit 0.

- [ ] **Step 7: Verify each selector flag maps to the right `-e`.** Uses a repo cwd with a derivable name (so name derivation doesn't error) and an env file.

Run:
```bash
CB=/Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/2555f647-560a-4487-ad3b-1f1abfb0914c/scratchpad
mkdir -p "$SB/sel" && cd "$SB/sel"
printf 'GITHUB_TOKEN=x\nGITHUB_REPOSITORY=acme/widgets\n' > .env.claudebox
echo "--- all ---";      "$CB" --dry-run run --all      2>&1 | grep -oE '\-e PR_[A-Z]+=[^ ]*'
echo "--- assignee ---"; "$CB" --dry-run run --assignee alice 2>&1 | grep -oE '\-e PR_ASSIGNEE=[^ ]*'
echo "--- prs ---";      "$CB" --dry-run run --prs 12,15,20   2>&1 | grep -oE '\-e PR_IDS=[^ ]*'
echo "--- search ---";   "$CB" --dry-run run --search "is:open" 2>&1 | grep -oE "PR_SEARCH=[^']*"
```
Expected: `--- all ---` → `-e PR_ALL=1`; `--- assignee ---` → `-e PR_ASSIGNEE=alice`; `--- prs ---` → `-e PR_IDS=12,15,20`; `--- search ---` → `PR_SEARCH=is:open`.

- [ ] **Step 8: Verify two selector flags error, and plain run passes no PR_ vars.**

Run:
```bash
CB=/Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/2555f647-560a-4487-ad3b-1f1abfb0914c/scratchpad
cd "$SB/sel"
echo "--- two flags ---"; "$CB" --dry-run run --all --assignee alice; echo "exit=$?"
echo "--- none ---";      "$CB" --dry-run run 2>&1 | grep -cE '\-e PR_'
```
Expected: `--- two flags ---` prints `ERROR: multiple PR selector flags given (--all --assignee); …` and `exit=1`; `--- none ---` prints `0` (launcher passes no PR_ vars when no selector flag is given — the entrypoint will error at container start instead).

- [ ] **Step 9: Commit**

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
git add claudebox.sh
git commit -m "launcher: add PR selector flags (--all/--assignee/--prs/--search)

Each maps to a PR_* env var passed through to the container on run/test. Two
selector flags is a friendly early error; the entrypoint remains authoritative.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B4nPWbXrMg1D5bMFLZpYa"
```

---

### Task 3: documentation (README, .env.example, CLAUDE.md, HISTORY.md)

**Files:**
- Modify: `README.md` (How it works + Configuration), `.env.example` (selectors + prompt token notes), `CLAUDE.md` (architecture description), `HISTORY.md` (changelog entry, if the file is a changelog).

**Interfaces:** none (docs only).

- [ ] **Step 1: Update `.env.example`.** Add a PR-selector block after the `GITHUB_REPOSITORY=` line (the `# Target repository…` block). Insert:
```
# --- PR selection (choose EXACTLY ONE) --------------------------------------
# Which PRs to review. Set exactly one of the following (or pass the matching
# launcher flag). Zero or more-than-one is a startup error.
#   PR_ALL=1                     review all open PRs                (--all)
#   PR_ASSIGNEE=alice            open PRs assigned to a GitHub user (--assignee)
#   PR_IDS=12,15,20              exactly these PR numbers           (--prs)
#   PR_SEARCH="is:open label:x"  PRs matching a gh search query     (--search)
# PR_ALL=
# PR_ASSIGNEE=
# PR_IDS=
# PR_SEARCH=
```

- [ ] **Step 2: Update the prompt-override comments in `.env.example`.** Replace the existing `REVIEW_PROMPT` / `FOLLOWUP_PROMPT` comment block:
```
# Override the review instruction for the FIRST pass / a fresh session (defaults
# to a thorough PR-review prompt; see entrypoint.sh).
# REVIEW_PROMPT=

# Override the instruction used on RESUMED passes (the session already holds
# prior context, so this just nudges a re-check; see entrypoint.sh).
# FOLLOWUP_PROMPT=
```
with:
```
# Override the review instruction used when a PR's session STARTS (defaults to a
# thorough per-PR review prompt; see entrypoint.sh). Use the {{PR}} token where
# the PR number should appear — the harness substitutes it per PR.
# REVIEW_PROMPT=

# Override the instruction used when a PR's session is RESUMED on a later cycle
# (it already holds that PR's prior context, so this just nudges a re-check).
# Also supports the {{PR}} token.
# FOLLOWUP_PROMPT=
```

- [ ] **Step 3: Update the `MAX_PASSES_PER_SESSION` comment in `.env.example`.** Replace:
```
# Rotate to a fresh Claude session after this many successful passes, to cap the
# context growth of the long-lived resumed session. 0 = never rotate.
MAX_PASSES_PER_SESSION=0
```
with:
```
# Rotate a PR's Claude session to a fresh one after this many successful passes
# on that PR, to cap the context growth of its long-lived resumed session.
# Applies per PR. 0 = never rotate.
MAX_PASSES_PER_SESSION=0
```

- [ ] **Step 4: Update `README.md` "How it works".** Replace the block from `The reviewer runs as **one continuous, stateful Claude session**.` through the paragraph ending `…check out the latest commits, and post one comment per finding.` (i.e. everything between the `## How it works` heading and the `> Why not \`/loop\`?` blockquote — keep the heading and the blockquote). Replace with:

````markdown
The reviewer runs **one Claude session per PR**. Each cycle the entrypoint enumerates the candidate PRs (see [PR selection](#pr-selection)), then reviews each one in its own session: a PR's first review starts a new session with `REVIEW_PROMPT`; later cycles `--resume` that PR's session with `FOLLOWUP_PROMPT`, so Claude remembers what it already flagged on that PR and avoids duplicate comments. The PR number is substituted into the prompt's `{{PR}}` token.

```bash
# a PR's first review — new session
claude -p --output-format stream-json --verbose --dangerously-skip-permissions \
  --model "$REVIEW_MODEL" "${REVIEW_PROMPT//\{\{PR\}\}/$pr}"
# later cycles — resume that PR's session
claude -p --resume "${PR_SESSION[$pr]}" --output-format stream-json --verbose \
  --dangerously-skip-permissions --model "$REVIEW_MODEL" "${FOLLOWUP_PROMPT//\{\{PR\}\}/$pr}"
```

Each pass streams as `stream-json`; the entrypoint pretty-prints the events live to its log (so `docker logs -f` shows the play-by-play) and recovers the session id from the stream to resume that PR next cycle.

The entrypoint shell is the supervisor: it controls cadence (`git fetch`, enumerate PRs, review each sequentially, then sleep), keeps an in-memory PR→session map, and starts a fresh session for a PR if its pass fails (so it may re-comment once on that PR). Claude itself uses `gh`/`git` to inspect the PR, check out the latest commit, and post one comment per finding. `MAX_PASSES_PER_SESSION` rotates a PR's session after N passes to bound its context growth (per PR).
````

- [ ] **Step 5: Update `README.md` Configuration — required list + new subsection.** First, replace:
```
All configuration is via environment variables — see `.env.example`. Always required:

- `GITHUB_TOKEN`
- `GITHUB_REPOSITORY`
```
with:
```
All configuration is via environment variables — see `.env.example`. Always required:

- `GITHUB_TOKEN`
- `GITHUB_REPOSITORY`
- exactly one PR selector (see [PR selection](#pr-selection) below)
```
Then insert this subsection immediately before the `## Notes & caveats` heading (the first `## ` heading after Configuration; if the exact title differs, insert before whatever section follows Configuration):
````markdown
### PR selection

Set **exactly one** of these (or pass the matching launcher flag). Zero or more than one is a startup error:

| Env var | Launcher flag | Reviews |
|---|---|---|
| `PR_ALL=1` | `--all` | all open PRs |
| `PR_ASSIGNEE=login` | `--assignee login` | open PRs assigned to that user |
| `PR_IDS=12,15,20` | `--prs 12,15,20` | exactly those PR numbers |
| `PR_SEARCH="is:open label:x"` | `--search "…"` | PRs matching a gh search query (you control state) |

`REVIEW_PROMPT`/`FOLLOWUP_PROMPT` use a `{{PR}}` token (substituted with the PR number), and `MAX_PASSES_PER_SESSION` applies per PR.

````

- [ ] **Step 6: Update `CLAUDE.md` architecture.** Make three replacements.

  (a) Replace the supervisor bullet:
```
- **`entrypoint.sh` is the supervisor.** It does auth setup (`gh`/`git`), wires the Claude-Code→provider env, prepares the working clone, then runs the review loop: `git fetch` → one review pass → sleep. It controls cadence and crash-recovery; Claude itself decides *what* to review.
```
  with:
```
- **`entrypoint.sh` is the supervisor.** It does auth setup (`gh`/`git`), wires the Claude-Code→provider env, prepares the working clone, then runs the review loop: `git fetch` → enumerate candidate PRs → review each PR sequentially → sleep. It controls cadence, PR selection, and crash-recovery; Claude reviews the one PR it's handed.
```

  (b) Replace the session bullet:
```
- **One continuous, stateful Claude session.** The first pass starts a new `claude -p` session and the script recovers its `session_id` from the `stream-json` output; every later pass `--resume`s that id so Claude remembers what it already reviewed and won't re-raise findings. A failed pass clears `SESSION_ID`, so the next cycle starts fresh (and may re-comment once — accepted noise). `MAX_PASSES_PER_SESSION` optionally rotates to a fresh session to bound context growth.
```
  with:
```
- **One Claude session per PR.** The harness enumerates candidate PRs from exactly one selector (`PR_ALL`/`PR_ASSIGNEE`/`PR_IDS`/`PR_SEARCH`; zero or multiple is a hard error) and reviews each in its own session, keyed in an in-memory `PR_SESSION` map. A PR's first review starts a new `claude -p` session (recovering its `session_id` from the `stream-json` output); later cycles `--resume` that PR's id so it won't re-raise findings on that PR. Prompts are `{{PR}}`-templated (`REVIEW_PROMPT` on start, `FOLLOWUP_PROMPT` on resume). A failed pass drops that PR's session id, so its next cycle starts fresh (and may re-comment once — accepted noise). `MAX_PASSES_PER_SESSION` optionally rotates a PR's session to bound context growth (per PR). The map is in-memory, so a container restart may re-review each PR once.
```

  (c) Replace the Configuration "Always required" sentence:
```
All config is via environment variables (`.env.example` documents them). Always required: `GITHUB_TOKEN`, `GITHUB_REPOSITORY`.
```
  with:
```
All config is via environment variables (`.env.example` documents them). Always required: `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, and exactly one PR selector (`PR_ALL`/`PR_ASSIGNEE`/`PR_IDS`/`PR_SEARCH`).
```
  (If the CLAUDE.md wording has drifted from these exact strings, locate the corresponding sentence/bullet and apply the equivalent change.)

- [ ] **Step 7: Add a `HISTORY.md` changelog entry.** `HISTORY.md` is a version changelog (newest first; top entry is `## 0.0.3 - 2026-07-17`). Insert a new `## Unreleased` section immediately after the `# History` title line and before `## 0.0.3 - 2026-07-17`:
```
## Unreleased

* Review each PR in its own Claude Code session. The harness now enumerates candidate PRs and iterates, giving each PR an independent, resumable session so re-reviews avoid duplicate comments per PR. `MAX_PASSES_PER_SESSION` now applies per PR.
* Add PR targeting — choose exactly one of: all open PRs (`--all` / `PR_ALL`), open PRs assigned to a user (`--assignee` / `PR_ASSIGNEE`), a specific set of PR numbers (`--prs` / `PR_IDS`), or a `gh` search query (`--search` / `PR_SEARCH`). Zero or more than one is a startup error.
* Prompts are now PR-scoped: `REVIEW_PROMPT` (session start) and `FOLLOWUP_PROMPT` (resume) substitute a `{{PR}}` token with the PR number; custom prompts use the same token.
* Launcher: infer the env file (`.env.claudebox` preferred over `.env`) and repo from the current directory, derive a per-repo container name `claudebox--<org>--<repo>` so several claudeboxes can run at once, announce those inferences loudly, and add `--tail` to follow logs right after `run`.
```

- [ ] **Step 8: Verify docs reference real behavior.**

Run:
```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
bash -n claudebox.sh && echo "launcher OK"
grep -q '{{PR}}' .env.example && echo ".env.example token OK"
grep -Eq 'PR_ALL|PR_ASSIGNEE|PR_IDS|PR_SEARCH' README.md && echo "README selectors OK"
/Users/jonathonfrisby/mrjoy/claudebox/claudebox.sh --help | grep -E '\-\-all|\-\-assignee|\-\-prs|\-\-search' && echo "help OK"
```
Expected: `launcher OK`, `.env.example token OK`, `README selectors OK`, the four help lines, and `help OK`.

- [ ] **Step 9: Commit**

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
git add README.md .env.example CLAUDE.md HISTORY.md
git commit -m "docs: per-PR sessions and PR targeting (README/.env.example/CLAUDE.md/HISTORY.md)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B4nPWbXrMg1D5bMFLZpYa"
```

---

## Notes for the implementer

- `entrypoint.sh` runs on modern bash in the image, so `declare -A`, `${arr[@]+…}`, and `< <(…)` process substitution are all fine there. Do NOT introduce associative arrays into `claudebox.sh` (host bash 3.2).
- The `[ cond ] && action` idiom used for the launcher passthrough and the entrypoint prompt-warnings is safe under `set -e` because the failing command precedes `&&` (bash does not exit on it). This matches idioms already in both scripts.
- Unit tests load the REAL function bodies via `awk` range extraction, which relies on each tested function being written `name() {` on its own line and terminated by a lone `}` at column 0 — keep that formatting for `pr_truthy`, `parse_pr_ids`, `resolve_pr_selection`, `enumerate_candidate_prs`, and `render_prompt`.
- `gh pr list --search` ignores `--state`; the query controls it. The other selectors force `--state open`.
