# Plan Review and Code Review Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route each candidate PR to either code review or plan review by GitHub label, giving each mode its own persona tree and its own prompt defaults.

**Architecture:** A plan arrives as a PR whose diff is the plan document, so the review loop, the four PR selectors, `PR_SESSION`, usage-limit handling and the `gh pr comment` output channel are untouched. `enumerate_candidate_prs` decides mode once per PR from its labels and emits `number<TAB>mode`; the pair key grows a segment to `"$pr:$mode:$persona"`; `PERSONA_DIR` becomes a parent holding `code/` and `plan/`, each resolved at startup.

**Tech Stack:** bash 4+ (`entrypoint.sh`, runs inside the image), `jq`, `gh` CLI, Python 3 `ast` (the persona importer). Test suites are bash with stubs on `PATH`.

**Spec:** `docs/superpowers/specs/2026-08-25-plan-and-code-review-modes-design.md`

## Global Constraints

- `entrypoint.sh` runs inside the image and may use modern bash. `claudebox.sh` runs on the **host**, where macOS ships bash 3.2; it is not touched by this plan, and must not be.
- Every operator-supplied env var goes on the `strip_surrounding_quotes` list at `entrypoint.sh:59-66`. `docker run --env-file` does no quote processing.
- Apostrophes cannot appear inside `${VAR:?message}` validation messages: quote processing applies inside the expansion and one silently breaks the script's parse.
- Stanzas (`_gh_stanza`, `_test_stanza`, `_linear_stanza`, and the new `_plan_stanza`) are appended to the **defaults only**. An operator-supplied prompt reaches Claude verbatim.
- Persona text travels in `--append-system-prompt`, never appended to the task prompt, and must be re-passed on every invocation because the flag does not survive `--resume`.
- Code mode is the default in the strong sense: an operator who labels nothing must see byte-identical behavior to today.
- Syntax-check with `bash -n entrypoint.sh` after every edit. There is no linter.
- Run `./test-personas.sh` and `./test-providers.sh` at the end of every task. Both must be green before committing.
- `./test-shim.sh` is untouched by this plan; do not edit it.

---

### Task 1: Mode routing by PR label

Decide `code` or `plan` per PR inside `enumerate_candidate_prs`, thread the mode through the pair key and the logs. Personas and prompts are unchanged by this task, so the only observable effect is what the log says and how a pair is named. Both test suites' `gh` stubs have to learn to answer label queries first, because the `ids` selector every case uses now makes a `gh pr view` call it did not make before.

**Files:**
- Modify: `entrypoint.sh:59-66` (the `strip_surrounding_quotes` list)
- Modify: `entrypoint.sh:167-175` (`enumerate_candidate_prs`)
- Modify: `entrypoint.sh:1140-1230` (the cycle loop: PR array, pair flattening, key parsing, log lines)
- Test: `test-personas.sh:41` (the `gh` stub) and new cases
- Test: `test-providers.sh:41` (the `gh` stub)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `PLAN_LABEL` — operator var, default `plan`.
  - `pr_modes()` — reads `gh --json number,labels` output on stdin (an array from `gh pr list`, or a single object from `gh pr view`) and echoes `number<TAB>mode` per PR. Non-zero exit on unparseable input.
  - `enumerate_candidate_prs()` — now echoes `number<TAB>mode` lines instead of bare numbers.
  - Shell locals inside the cycle loop: `$mode` is `code` or `plan`; pair keys are `"$pr:$mode:$persona"`.

- [ ] **Step 1: Teach `test-personas.sh`'s `gh` stub to answer label queries**

Replace line 41 of `test-personas.sh` (`printf '#!/bin/sh\nexit 0\n' >"$BIN/gh"`) with:

```bash
# `gh` now gets asked for a PR's labels, because mode routing decides code-vs-plan
# from them. Everything else it is asked still just has to succeed.
#   STUB_PLAN_PRS   -- comma-separated PR numbers that carry the plan label
#   STUB_LABEL_FAIL -- comma-separated PR numbers whose label lookup fails
cat >"$BIN/gh" <<'STUB'
#!/bin/sh
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  n="$3"
  case ",${STUB_LABEL_FAIL:-}," in
    *",$n,"*) echo "gh: could not resolve to a PullRequest" >&2; exit 1 ;;
  esac
  case ",${STUB_PLAN_PRS:-}," in
    *",$n,"*) printf '{"number":%s,"labels":[{"name":"%s"}]}\n' "$n" "${STUB_PLAN_LABEL:-plan}" ;;
    *)        printf '{"number":%s,"labels":[]}\n' "$n" ;;
  esac
  exit 0
fi
exit 0
STUB
```

- [ ] **Step 2: Make the identical change to `test-providers.sh`'s `gh` stub**

Replace line 41 of `test-providers.sh` with the same heredoc block. Its cases never set `STUB_PLAN_PRS`, so every PR there resolves to `code` and each case still produces exactly one `claude` invocation under its pinned `PERSONAS=red_team`. Update the comment at `test-providers.sh:39-40`, which currently claims PR enumeration "needs no gh call":

```bash
# gh answers PR label queries (mode routing reads them) and otherwise only needs
# to succeed. Every case here leaves its PR unlabeled, so every case is code mode.
```

- [ ] **Step 3: Write the failing tests**

Append to `test-personas.sh`, after the "cycles cut short" block (around line 441):

```bash
# --- mode routing ------------------------------------------------------------
# Mode is decided once per PR, inside enumerate_candidate_prs, from its labels.
cycle "mode: an unlabeled PR is reviewed in code mode" \
  PERSONAS=red_team STUB_MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"Candidate PRs (ids): 1:code" \
     LOG:"Reviewing PR #1 [code/red_team]"

cycle "mode: a PR carrying the plan label is reviewed in plan mode" \
  PERSONAS=red_team STUB_PLAN_PRS=1 STUB_MAX_CYCLES=1 \
  -- CALLS:1 \
     LOG:"Candidate PRs (ids): 1:plan" \
     LOG:"Reviewing PR #1 [plan/red_team]"

cycle "mode: PLAN_LABEL names the label that means plan" \
  PERSONAS=red_team PLAN_LABEL=proposal STUB_PLAN_PRS=1 STUB_PLAN_LABEL=proposal STUB_MAX_CYCLES=1 \
  -- CALLS:1 LOG:"1:plan"

cycle "mode: a label that is not PLAN_LABEL leaves the PR in code mode" \
  PERSONAS=red_team PLAN_LABEL=proposal STUB_PLAN_PRS=1 STUB_PLAN_LABEL=plan STUB_MAX_CYCLES=1 \
  -- CALLS:1 LOG:"1:code"

cycle "mode: both modes can appear in one cycle" \
  PR_IDS=1,2 PERSONAS=red_team STUB_PLAN_PRS=2 STUB_MAX_CYCLES=1 \
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

# The pair key carries the mode, so a PR whose label changes orphans its old
# sessions rather than resuming a code session under a plan persona.
cycle "mode: a pair key names its mode" \
  PERSONAS=red_team,sage STUB_FAIL_ON=2 STUB_FAIL_MODE=limit STUB_MAX_CYCLES=1 \
  -- LOG:"Not reviewed this cycle:" \
     LOG:"1:code:sage"
```

Now update the three existing assertions that spell a pair key or a log line without a mode. In `test-personas.sh`:

- line 342: `LOG:"PR #1 [red_team] reached MAX_PASSES_PER_SESSION=1"` becomes `LOG:"PR #1 [code/red_team] reached MAX_PASSES_PER_SESSION=1"`
- lines 417-418: `LOG:"Not reviewed this cycle: 1:sme"` becomes `LOG:"Not reviewed this cycle: 1:code:sme"`, and `LOG:"Starting this cycle at 1:sme, where the last one was cut."` becomes `LOG:"Starting this cycle at 1:code:sme, where the last one was cut."`
- lines 431-432: `LOG:"Not reviewed this cycle: 2:sage 2:sme"` becomes `LOG:"Not reviewed this cycle: 2:code:sage 2:code:sme"`, and `LOG:"The next cycle starts at 2:sage"` becomes `LOG:"The next cycle starts at 2:code:sage"`

- [ ] **Step 4: Run the tests to verify they fail**

Run: `./test-personas.sh mode`
Expected: every `mode:` case FAILs. The first two should report `[log missing: Candidate PRs (ids): 1:code]`, because enumeration still emits a bare `1`.

Run: `./test-personas.sh`
Expected: the four edited assertions above also FAIL, for the same reason.

- [ ] **Step 5: Add `PLAN_LABEL` and put it on the quote-stripping list**

In `entrypoint.sh`, extend the `strip_surrounding_quotes` call at line 59-66 by adding `PLAN_LABEL` to the last line:

```bash
  PERSONAS PERSONA_DIR PLAN_LABEL LIMIT_BACKOFF_SECONDS
```

Then, immediately above `enumerate_candidate_prs` (before line 167), add:

```bash
# --- Review mode -----------------------------------------------------------
# A plan arrives as a pull request whose diff is the plan document, so plan
# review reuses the whole loop and differs only in which personas and which
# prompt a PR gets. Routing is by label rather than by a path heuristic or a
# classifier pass: a label is explicit, per-PR, author-controlled, and puts no
# nondeterministic decision inside the harness's control flow.
PLAN_LABEL="${PLAN_LABEL:-plan}"
```

- [ ] **Step 6: Add `pr_modes` and make `enumerate_candidate_prs` mode-aware**

Replace `enumerate_candidate_prs` (`entrypoint.sh:167-175`) with:

```bash
# Read `gh --json number,labels` output on stdin -- an array from `gh pr list`,
# or a single object from `gh pr view` -- and echo `number<TAB>mode` per PR.
# Unparseable input exits non-zero, which the ids arm below turns into a skip.
pr_modes() {
  jq -r --arg L "$PLAN_LABEL" '
    (if type == "array" then . else [.] end)[]
    | "\(.number)\t\(if any(.labels[]?; .name == $L) then "plan" else "code" end)"
  '
}

# Echo one `number<TAB>mode` line per candidate PR. Mode is decided here, at the
# one seam that already decides what gets reviewed at all, so nothing downstream
# asks GitHub a second time. For the three list selectors the labels ride along
# in the call that was already being made; `ids` has no list call behind it, so
# it costs one `gh pr view` per PR per cycle.
enumerate_candidate_prs() {
  local n raw
  case "$PR_SELECTOR" in
    all)      gh pr list -R "$GITHUB_REPOSITORY" --state open --limit 100 --json number,labels | pr_modes ;;
    assignee) gh pr list -R "$GITHUB_REPOSITORY" --state open --assignee "$PR_ASSIGNEE" --limit 100 --json number,labels | pr_modes ;;
    search)   gh pr list -R "$GITHUB_REPOSITORY" --search "$PR_SEARCH" --limit 100 --json number,labels | pr_modes ;;
    ids)
      for n in $(parse_pr_ids "$PR_IDS"); do
        # A failed lookup skips this PR for the cycle. It does NOT fall back to
        # code mode: a wrong-mode review posts real comments on a real PR and
        # cannot be taken back, where a skip is one log line and a retry next
        # cycle. The log goes to stderr because this function's stdout is the
        # candidate list.
        if raw="$(gh pr view "$n" -R "$GITHUB_REPOSITORY" --json number,labels 2>/dev/null)"; then
          printf '%s\n' "$raw" | pr_modes || log "WARN: could not read labels for PR #$n; skipping it this cycle." >&2
        else
          log "WARN: could not read labels for PR #$n; skipping it this cycle." >&2
        fi
      done ;;
  esac
}
```

- [ ] **Step 7: Thread the mode through the cycle loop**

In `entrypoint.sh`, replace the PR-reading block at line 1141-1142:

```bash
  prs=()
  while IFS=$'\t' read -r _n _mode; do
    [ -n "$_n" ] && [ -n "$_mode" ] && prs+=("$_n:$_mode")
  done < <(enumerate_candidate_prs || true)
```

Replace the pair-flattening loop (the `for pr in ${prs[@]+"${prs[@]}"}` block around line 1164-1166) with:

```bash
  pairs=()
  for pr_mode in ${prs[@]+"${prs[@]}"}; do
    pr="${pr_mode%%:*}"; mode="${pr_mode#*:}"
    for persona in "${PERSONAS_LIST[@]}"; do pairs+=("$pr:$mode:$persona"); done
  done
```

Replace the key-parsing line inside the pair loop (`pr="${key%%:*}"; persona="${key#*:}"`) with:

```bash
    pr="${key%%:*}"; _rest="${key#*:}"; mode="${_rest%%:*}"; persona="${_rest#*:}"
```

Then replace `[$persona]` with `[$mode/$persona]` in every log line inside that loop, and `as $persona` with `as $mode/$persona` in the two "Reviewing PR" lines. There are six such lines: the two "Reviewing PR #$pr ..." lines, the "review complete" line, the "reached MAX_PASSES_PER_SESSION" line, the two limit WARN lines, and the ordinary-failure WARN line.

The `Candidate PRs` log line at 1147 needs no edit: `${prs[*]}` now expands to `1:code 2:plan` because the array elements changed.

- [ ] **Step 8: Syntax-check and run the tests**

Run: `bash -n entrypoint.sh`
Expected: no output.

Run: `./test-personas.sh`
Expected: all cases PASS, including the seven new `mode:` cases.

Run: `./test-providers.sh`
Expected: all cases PASS.

- [ ] **Step 9: Commit**

```bash
git add entrypoint.sh test-personas.sh test-providers.sh
git commit -m "feat(modes): route each PR to code or plan review by label

PLAN_LABEL (default 'plan') decides mode inside enumerate_candidate_prs,
which now emits number<TAB>mode. The pair key grows a segment to
pr:mode:persona. A failed label lookup skips the PR rather than guessing
code mode, because a wrong-mode review posts comments that cannot be
taken back."
```

---

### Task 2: Split `personas/` into `code/` and `plan/` trees

`PERSONA_DIR` becomes a parent. Each mode resolves its own persona set at startup, whether or not any PR is labeled, so a broken definition still kills the container at boot.

**Files:**
- Create: `personas/code/` and `personas/plan/`, each with `_shared.md` and six persona bodies (moved and copied, via `git mv` and `cp`)
- Delete: the flat `personas/*.md` (they become `personas/code/*.md`)
- Modify: `entrypoint.sh:182-300` (the persona registry)
- Modify: `entrypoint.sh:451` (the `resolve_personas` call site)
- Modify: `entrypoint.sh:1164-1166` (pair flattening, to use the per-mode list)
- Modify: `entrypoint.sh:1097,1103` (the two `--append-system-prompt` argv sites)
- Modify: `entrypoint.sh:59-66` (add `PLAN_PERSONAS`)
- Modify: `tools/import-advocate-personas.py` (write both trees)
- Test: `test-personas.sh`

**Interfaces:**
- Consumes: `$mode` (`code` or `plan`) from Task 1, available in the cycle loop.
- Produces:
  - `PLAN_PERSONAS` — operator var, the plan-mode counterpart of `PERSONAS`.
  - `PERSONA_PROMPT["$mode:$id"]`, `PERSONA_LABEL["$mode:$id"]` — mode-qualified keys.
  - `MODE_PERSONAS[$mode]` — space-joined persona ids for that mode, in selection order.
  - `resolve_personas MODE` — takes the mode as its one argument.
  - `persona_meta ID KEY MODE`, `persona_body ID MODE`, `persona_prompt ID MODE` — all take the mode as a trailing argument.

- [ ] **Step 1: Restructure the persona files**

```bash
mkdir -p personas/code personas/plan
git mv personas/_shared.md personas/code/_shared.md
for p in adversarial good_friend red_team sage sme user; do
  git mv "personas/$p.md" "personas/code/$p.md"
  cp "personas/code/$p.md" "personas/plan/$p.md"
done
cp personas/code/_shared.md personas/plan/_shared.md
git add personas/plan
```

Both trees now hold identical bodies. That is the intended state: advocate has one body per persona, so the importer cannot invent two, and hand-tuning is protected by the fact that a re-run of the importer produces a diff that gets reviewed before it is committed.

- [ ] **Step 2: Write the failing tests**

In `test-personas.sh`, the two broken-directory fixtures at lines 125-130 have to grow a level, because the checks they exercise now live one directory down. Replace that block with:

```bash
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
```

Update the static definitions check at lines 221-235 to walk both trees:

```bash
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
```

Then append the new cases, after the `mode:` block from Task 1:

```bash
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

refuses "modes: a plan persona that is only frontmatter refuses at startup" \
  "empty prompt body" \
  -- PERSONA_DIR="$HOLLOW" PERSONAS=hollow PLAN_PERSONAS=hollow

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
```

That last case needs one more stub knob. In the `gh` stub written in Task 1, add a cycle-aware branch immediately before the `STUB_PLAN_PRS` case:

```sh
  # STUB_PLAN_AFTER=N: the PR is unlabeled until cycle N has finished, then
  # labeled. Cycles are counted by the sleep stub, which writes $HOME/sleeps.
  if [ -n "${STUB_PLAN_AFTER:-}" ]; then
    c=$(cat "$HOME/sleeps" 2>/dev/null || echo 0)
    if [ "$c" -ge "$STUB_PLAN_AFTER" ]; then
      printf '{"number":%s,"labels":[{"name":"%s"}]}\n' "$n" "${STUB_PLAN_LABEL:-plan}"
      exit 0
    fi
  fi
```

Finally, update the existing selection cases at lines 238-247, whose log assertion string gains a mode prefix:

- line 239: `LOG:"personas: red_team adversarial sme sage"` becomes `LOG:"code personas: red_team adversarial sme sage"`
- line 243: `LOG:"personas: sage red_team"` becomes `LOG:"code personas: sage red_team"`
- line 247: `LOG:"personas: adversarial good_friend red_team sage sme user"` becomes `LOG:"code personas: adversarial good_friend red_team sage sme user"`

- [ ] **Step 3: Run the tests to verify they fail**

Run: `./test-personas.sh`
Expected: the `modes:` cases FAIL. The `definitions:` case FAILs only if Step 1 was skipped. The three edited `selection:` cases FAIL with `[log missing: code personas: ...]`.

- [ ] **Step 4: Rewrite the persona registry**

In `entrypoint.sh`, replace the registry block (lines 182-300, from the `# --- Persona registry` comment through the end of `resolve_personas`) with:

```bash
# --- Persona registry ------------------------------------------------------
# Each review pass runs as one of advocate's adversarial personas rather than as
# a generalist reviewer. advocate's personas are PLAN-review personas: they were
# written to interrogate a proposal before the work happens. Code mode runs the
# subset of them that survives contact with a diff; plan mode runs all six.
#
# PERSONA_DIR is a parent holding one tree per mode. A persona is a file in
# PERSONA_DIR/<mode>: frontmatter (label, success) plus a body that becomes the
# pass's system prompt. Files starting with an underscore are not personas;
# _shared.md is the output contract appended to every persona body in that tree.
#
# Definitions live in files rather than inline here for three reasons: it keeps
# ~200 lines of prompt text out of this script, it gives an operator an override
# by mounting their own directory at PERSONA_DIR, and it keeps the imported text
# close to its provenance (tools/import-advocate-personas.py).
PERSONA_DIR="${PERSONA_DIR:-/opt/claudebox/personas}"
REVIEW_MODES="code plan"
# The code default is the subset: advocate's `user` and `good_friend` were
# written against designs and whole projects, so on a narrow diff they reach for
# material that isn't in it. Plan mode is where they finally have something to
# bite on, which is why the plan default is everything.
DEFAULT_PERSONAS_CODE="red_team,adversarial,sme,sage"
DEFAULT_PERSONAS_PLAN="adversarial,good_friend,red_team,sage,sme,user"
# Claimed now, used in phase 2: the pass that reconciles what the personas said
# is the only one allowed to read their findings, which is why it is not itself
# a persona and cannot be selected as one.
RESERVED_PERSONAS="aggregate"

# Keyed "$mode:$id", so the same persona name in two trees is two entries.
declare -A PERSONA_PROMPT=()
declare -A PERSONA_LABEL=()
# Keyed "$mode", holding that mode's selected persona ids space-joined, in order.
declare -A MODE_PERSONAS=()

# Echo frontmatter key $2 from persona $1 in mode $3.
persona_meta() {
  awk -v k="$2" '
    NR == 1 && $0 == "---" { fm = 1; next }
    fm && $0 == "---" { exit }
    fm && index($0, k ":") == 1 { sub(/^[^:]*:[[:space:]]*/, ""); print; exit }
  ' "$PERSONA_DIR/$3/$1.md"
}

# Echo persona $1's own body in mode $2: everything after its frontmatter.
# Separate from persona_prompt because resolve_personas has to judge the body on
# its own -- a body-plus-shared-contract string is never empty, so a persona file
# that is nothing but frontmatter would resolve and then review a PR with no
# identity at all, signing findings with a label it has no angle of attack behind.
persona_body() {
  awk '
    NR == 1 && $0 == "---" { fm = 1; next }
    fm && $0 == "---" { fm = 0; body = 1; next }
    body
  ' "$PERSONA_DIR/$2/$1.md"
}

# Echo persona $1's full system prompt in mode $2: its body, then that tree's
# shared contract, with {{PERSONA}} replaced by its label. The label is validated
# in resolve_personas to contain no slash, so it is safe as a sed replacement.
persona_prompt() {
  local id="$1" mode="$2" label="${PERSONA_LABEL[$2:$1]}"
  {
    persona_body "$id" "$mode"
    printf '\n'
    cat "$PERSONA_DIR/$mode/_shared.md"
  } | sed "s|{{PERSONA}}|$label|g"
}

# Fill MODE_PERSONAS[$1], PERSONA_LABEL and PERSONA_PROMPT for mode $1, from that
# mode's selector var or its default set. Dies on anything it can't resolve: a
# typo that silently narrowed the review to one persona, or to none, would look
# exactly like a working run in the log. Called for EVERY mode at startup, even
# one no PR currently uses, so a broken definition fails at boot rather than the
# first time somebody adds a label to a PR.
resolve_personas() {
  local mode="$1" dir="$PERSONA_DIR/$mode" avail="" f b tok raw def list=""
  case "$mode" in
    code) def="$DEFAULT_PERSONAS_CODE" ;;
    plan) def="$DEFAULT_PERSONAS_PLAN" ;;
  esac
  [ -d "$PERSONA_DIR" ] || die "no persona definitions: PERSONA_DIR=$PERSONA_DIR is not a directory."
  # The flat layout phase 1 shipped is reachable by exactly the mount-your-own-
  # personas workflow the docs advertise, so it has to say what changed rather
  # than dying on a missing file three checks later.
  if [ ! -d "$dir" ]; then
    for f in "$PERSONA_DIR"/*.md; do
      [ -e "$f" ] && die "PERSONA_DIR now holds one tree per review mode: $PERSONA_DIR needs code/ and plan/ subdirectories, but its persona files sit directly in it."
    done
    die "no persona definitions for $mode review: $dir is not a directory."
  fi
  # Every persona body is appended to _shared.md, so without it persona_prompt's
  # `cat` fails, pipefail fails the command substitution and set -e exits with
  # nothing but cat's own message -- under --restart unless-stopped, a silent
  # crash loop reachable by the documented "mount your own personas" workflow.
  [ -f "$dir/_shared.md" ] || die "no output contract: $dir/_shared.md is missing; every persona body is appended to it."
  for f in "$dir"/*.md; do
    [ -e "$f" ] || continue
    b="$(basename "$f" .md)"
    case "$b" in _*) continue ;; esac
    avail="$avail $b"
  done
  [ -n "$avail" ] || die "no persona definitions found in $dir."

  case "$mode" in
    code) raw="${PERSONAS-$def}" ;;
    plan) raw="${PLAN_PERSONAS-$def}" ;;
  esac
  case "$(printf '%s' "$raw" | tr 'A-Z' 'a-z')" in
    all) raw="$(printf '%s' "$avail")" ;;
  esac

  # set -f for the split, for the same reason as parse_pr_ids: unquoted is what
  # splits on the separators, and unquoted also globs, so PERSONAS=* would be
  # resolved against the current directory instead of dying as an unknown name.
  set -f
  for tok in $(printf '%s' "$raw" | tr ',' ' '); do
    case " $RESERVED_PERSONAS " in
      *" $tok "*) die "persona '$tok' is reserved and cannot be selected." ;;
    esac
    case " $avail " in
      *" $tok "*) ;;
      *) die "unknown persona '$tok' for $mode review; available:$avail" ;;
    esac
    case " $list " in
      *" $tok "*) die "persona '$tok' is listed twice for $mode review." ;;
    esac
    list="${list:+$list }$tok"
  done
  set +f
  [ -n "$list" ] || die "the $mode persona list is set but names no persona; unset it for the default set ($def), or name one of:$avail"

  # Resolve labels and prompts once, so a pass is a string lookup rather than
  # three file reads, and so a broken definition fails at startup.
  local id label body
  for id in $list; do
    label="$(persona_meta "$id" label "$mode")"
    case "$label" in
      '') die "persona '$mode/$id' has no label: in its frontmatter." ;;
      *[!A-Za-z0-9\ ._-]*) die "persona '$mode/$id' has a label with unexpected characters: '$label' (letters, digits, spaces, dot, underscore and hyphen only)." ;;
    esac
    body="$(persona_body "$id" "$mode")"
    [ -n "${body//[[:space:]]/}" ] || die "persona '$mode/$id' has an empty prompt body."
    PERSONA_LABEL[$mode:$id]="$label"
    PERSONA_PROMPT[$mode:$id]="$(persona_prompt "$id" "$mode")"
  done
  MODE_PERSONAS[$mode]="$list"
  log "$mode personas: $list"
}
```

- [ ] **Step 5: Update the call site and the pair loop**

At `entrypoint.sh:451`, replace `resolve_personas` with:

```bash
for _mode in $REVIEW_MODES; do resolve_personas "$_mode"; done
unset _mode
```

In the cycle loop, replace the pair-flattening block from Task 1 with:

```bash
  pairs=()
  for pr_mode in ${prs[@]+"${prs[@]}"}; do
    pr="${pr_mode%%:*}"; mode="${pr_mode#*:}"
    for persona in ${MODE_PERSONAS[$mode]}; do pairs+=("$pr:$mode:$persona"); done
  done
```

`run_pass` needs the mode, because the persona prompt is now keyed by it. Change its signature line from

```bash
  local prompt="$1" sid="$2" persona="$3" rc errfile rawfile got
```

to

```bash
  local prompt="$1" sid="$2" persona="$3" mode="$4" rc errfile rawfile got
```

and at both `--append-system-prompt` sites inside it (`entrypoint.sh:1097` and `:1103`), change `"${PERSONA_PROMPT[$persona]}"` to `"${PERSONA_PROMPT[$mode:$persona]}"`. At its one call site in the cycle loop, `if run_pass "$prompt" "$sid" "$persona"; then` becomes:

```bash
    if run_pass "$prompt" "$sid" "$persona" "$mode"; then
```

- [ ] **Step 6: Add `PLAN_PERSONAS` to the quote-stripping list**

In `entrypoint.sh:59-66`, the last line becomes:

```bash
  PERSONAS PLAN_PERSONAS PERSONA_DIR PLAN_LABEL LIMIT_BACKOFF_SECONDS
```

- [ ] **Step 7: Teach the importer to write both trees**

In `tools/import-advocate-personas.py`, find where it writes each persona file into `out` and wrap that write in a loop over the two trees. Update the module docstring to add:

```
Both review-mode trees get the same imported body. advocate has one body per
persona, so this importer cannot invent two, and hand-tuning one tree is
protected by the fact that a re-run produces a diff reviewed before it is
committed -- a hand edit shows up there as a reverted line to keep.
```

The write loop becomes, for each persona `name` and its `text`:

```python
for tree in ("code", "plan"):
    d = out / tree
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text, encoding="utf-8")
```

If the script also writes `_shared.md`, write it into both trees the same way. If it does not (the file is hand-maintained), leave it alone.

- [ ] **Step 8: Syntax-check and run the tests**

Run: `bash -n entrypoint.sh`
Expected: no output.

Run: `python3 -c "import ast; ast.parse(open('tools/import-advocate-personas.py').read())"`
Expected: no output.

Run: `./test-personas.sh`
Expected: all cases PASS.

Run: `./test-providers.sh`
Expected: all cases PASS. Its `PERSONA_DIR="$SCRIPT_DIR/personas"` still resolves, because the parent is what it points at.

- [ ] **Step 9: Commit**

```bash
git add personas entrypoint.sh tools/import-advocate-personas.py test-personas.sh
git commit -m "feat(personas): one persona tree per review mode

PERSONA_DIR becomes a parent holding code/ and plan/. Both modes resolve
at startup, whether or not any PR is labeled, so a broken plan persona
still kills the container at boot. Code keeps the four-persona subset;
plan runs all six, which is what user and good_friend were written for."
```

---

### Task 3: Per-mode prompt defaults

Plan mode drops `_test_stanza`, keeps `_gh_stanza` and `_linear_stanza`, and gains `_plan_stanza`. Four `PLAN_`-prefixed operator overrides join the existing four.

**Files:**
- Modify: `entrypoint.sh:400-455` (the prompt block and its validation)
- Modify: `entrypoint.sh:59-66` (four more vars on the strip list)
- Modify: the cycle loop's two `render_prompt` calls
- Test: `test-personas.sh`

**Interfaces:**
- Consumes: `$mode` from Task 1, `MODE_PERSONAS` from Task 2.
- Produces:
  - `MODE_REVIEW_PROMPT[$mode]`, `MODE_FOLLOWUP_PROMPT[$mode]` — the resolved prompt for each mode, suffixes already applied.
  - Operator vars `PLAN_REVIEW_PROMPT`, `PLAN_FOLLOWUP_PROMPT`, `PLAN_REVIEW_PROMPT_SUFFIX`, `PLAN_FOLLOWUP_PROMPT_SUFFIX`.

- [ ] **Step 1: Write the failing tests**

Append to `test-personas.sh`:

```bash
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

# Repeated on resumed passes for the same reason the gh stanza is: a long-resumed
# session's earliest turns are the first thing a context summary drops.
cycle "prompts: a resumed plan pass repeats the plan stanza" \
  PLAN_PERSONAS=red_team STUB_PLAN_PRS=1 \
  -- CALLS:2 \
     ARGV:2:"--resume S1" \
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

# The code-mode overrides must not leak into plan mode, or an operator who tuned
# their code prompt would silently get it on plan PRs too.
cycle "prompts: a code override does not reach plan mode" \
  PR_IDS=1,2 PERSONAS=red_team PLAN_PERSONAS=red_team STUB_PLAN_PRS=2 \
  REVIEW_PROMPT='code only 1' STUB_MAX_CYCLES=1 \
  -- CALLS:2 \
     ARGV:1:"code only 1" \
     NOARGV:2:"code only 1" \
     ARGV:2:"proposes an approach rather than implementing one"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test-personas.sh prompts:`
Expected: the seven new cases FAIL. The first should report `[argv 2 should not have: Treat the tests in this PR as code under review]`, because plan mode is still using the one shared `REVIEW_PROMPT`.

- [ ] **Step 3: Add the plan stanza and the plan defaults**

In `entrypoint.sh`, immediately after the `_gh_stanza` assignment (line 418), add:

```bash
# The plan stanza does two jobs. It says what to review, and it says what NOT to
# flag: a code-shaped reviewer handed a design document will reliably report
# missing error handling in code nobody has written, and a review full of that is
# a review nobody reads. Like the others it is appended to the DEFAULTS only.
_plan_stanza="This pull request proposes an approach rather than implementing one. Review the proposal itself: whether the problem is stated correctly, whether this is the simplest thing that solves it, what it fails to account for, what it forecloses, and what would have to be true for it to work. Where you object, say what you would do instead. There is no implementation to inspect, so do not ask for tests, error handling, or input validation in code that does not exist yet; a gap in the plan's own reasoning is a finding, a gap in code it has not written is not."
```

Then, after the existing `DEFAULT_FOLLOWUP` assignment (line 429), add the plan pair:

```bash
DEFAULT_PLAN_PROMPT="Review the plan or design proposed in pull request #{{PR}} in this repository. Read it with \`gh pr diff {{PR}}\` and \`gh pr view {{PR}} --json number,title,body,author,url,state,isDraft,headRefName,headRefOid,baseRefName,labels,files,commits,comments,reviews\`, and be sure you're looking at the most recent commit on its branch. $_gh_stanza $_plan_stanza Post findings as comments on the PR, one comment per finding."
DEFAULT_PLAN_FOLLOWUP="I've fetched the latest refs. Re-read the plan in pull request #{{PR}} for revisions since your last review of it. Apply the same review standard, and only post findings you haven't already raised on this PR. A point you raised that the revision addresses is settled; say nothing further about it. Be sure you're looking at the most recent commit on its branch. $_gh_stanza $_plan_stanza"
```

Update the `_linear_stanza` block just below to cover all four defaults, and extend the `unset` line:

```bash
DEFAULT_PROMPT="${DEFAULT_PROMPT}${_linear_stanza}"
DEFAULT_FOLLOWUP="${DEFAULT_FOLLOWUP}${_linear_stanza}"
DEFAULT_PLAN_PROMPT="${DEFAULT_PLAN_PROMPT}${_linear_stanza}"
DEFAULT_PLAN_FOLLOWUP="${DEFAULT_PLAN_FOLLOWUP}${_linear_stanza}"
unset _linear_stanza _gh_stanza _test_stanza _plan_stanza
```

- [ ] **Step 4: Resolve prompts per mode**

Replace the `REVIEW_PROMPT=`/`FOLLOWUP_PROMPT=` assignments and the two suffix `if` blocks (lines 436-448) with:

```bash
# Keyed "$mode". An operator override replaces that mode's default only, so
# tuning the code prompt cannot silently change what a plan PR is asked.
declare -A MODE_REVIEW_PROMPT=()
declare -A MODE_FOLLOWUP_PROMPT=()
MODE_REVIEW_PROMPT[code]="${REVIEW_PROMPT:-$DEFAULT_PROMPT}"
MODE_FOLLOWUP_PROMPT[code]="${FOLLOWUP_PROMPT:-$DEFAULT_FOLLOWUP}"
MODE_REVIEW_PROMPT[plan]="${PLAN_REVIEW_PROMPT:-$DEFAULT_PLAN_PROMPT}"
MODE_FOLLOWUP_PROMPT[plan]="${PLAN_FOLLOWUP_PROMPT:-$DEFAULT_PLAN_FOLLOWUP}"
# Suffixes append to whichever prompt is now in effect (default or operator
# override) -- unlike the Linear stanza above, they apply either way. A single
# space joins them since the prompts above end in '.'.
[ -n "${REVIEW_PROMPT_SUFFIX:-}" ]        && MODE_REVIEW_PROMPT[code]="${MODE_REVIEW_PROMPT[code]} $REVIEW_PROMPT_SUFFIX"
[ -n "${FOLLOWUP_PROMPT_SUFFIX:-}" ]      && MODE_FOLLOWUP_PROMPT[code]="${MODE_FOLLOWUP_PROMPT[code]} $FOLLOWUP_PROMPT_SUFFIX"
[ -n "${PLAN_REVIEW_PROMPT_SUFFIX:-}" ]   && MODE_REVIEW_PROMPT[plan]="${MODE_REVIEW_PROMPT[plan]} $PLAN_REVIEW_PROMPT_SUFFIX"
[ -n "${PLAN_FOLLOWUP_PROMPT_SUFFIX:-}" ] && MODE_FOLLOWUP_PROMPT[plan]="${MODE_FOLLOWUP_PROMPT[plan]} $PLAN_FOLLOWUP_PROMPT_SUFFIX"
:
```

The bare `:` on the last line is load-bearing: `set -e` is in effect and the last `[ -n ... ] && ...` short-circuits to a non-zero status when the suffix is unset, which would exit the script.

Replace the two `{{PR}}` warnings (lines 453-454) with a loop over all four:

```bash
for _mode in $REVIEW_MODES; do
  case "${MODE_REVIEW_PROMPT[$_mode]}"   in *'{{PR}}'*) : ;; *) log "WARN: the $_mode review prompt has no {{PR}} token; reviews won't name the specific PR." ;; esac
  case "${MODE_FOLLOWUP_PROMPT[$_mode]}" in *'{{PR}}'*) : ;; *) log "WARN: the $_mode followup prompt has no {{PR}} token; reviews won't name the specific PR." ;; esac
done
unset _mode
```

Keep it exactly where the old two-line version was, immediately after the `resolve_pr_selection` and persona-resolution calls. It reads `MODE_REVIEW_PROMPT` and `REVIEW_MODES`, both of which are set further up the file, so that position is already valid.

- [ ] **Step 5: Use the per-mode prompt in the cycle loop**

In the cycle loop, replace the two `render_prompt` calls:

```bash
      prompt="$(render_prompt "${MODE_REVIEW_PROMPT[$mode]}" "$pr")"
```

and

```bash
      prompt="$(render_prompt "${MODE_FOLLOWUP_PROMPT[$mode]}" "$pr")"
```

- [ ] **Step 6: Add the four new vars to the quote-stripping list**

In `entrypoint.sh:59-66`, add a line before the `PERSONAS` line:

```bash
  PLAN_REVIEW_PROMPT PLAN_FOLLOWUP_PROMPT \
  PLAN_REVIEW_PROMPT_SUFFIX PLAN_FOLLOWUP_PROMPT_SUFFIX \
```

- [ ] **Step 7: Syntax-check and run the tests**

Run: `bash -n entrypoint.sh`
Expected: no output.

Run: `./test-personas.sh`
Expected: all cases PASS.

Run: `./test-providers.sh`
Expected: all cases PASS. Its two prompt cases at `test-providers.sh:515-528` assert code-mode text and are unaffected, because every PR there is unlabeled.

- [ ] **Step 8: Commit**

```bash
git add entrypoint.sh test-personas.sh
git commit -m "feat(prompts): per-mode prompt defaults

Plan mode drops the test stanza (there is no implementation to mutate),
keeps the gh and Linear stanzas, and gains a plan stanza that says what
to review and what not to flag. PLAN_REVIEW_PROMPT and friends override
plan mode only, so tuning the code prompt cannot change what a plan PR
is asked."
```

---

### Task 4: Documentation

**Files:**
- Modify: `CLAUDE.md` (the Personas section, the Architecture intro, the Configuration section)
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `HISTORY.md`

- [ ] **Step 1: Rewrite `CLAUDE.md`'s Personas section**

The section currently states outright that personas are how claudebox reviews PRs, which is the claim this whole change contradicts. Rewrite it rather than amending it. It must carry:

- The correction: advocate's personas are plan-review personas. Phase 1 shipped them as the general PR-review mechanism, which was a misreading of what they are for.
- Mode routing by `PLAN_LABEL`, decided in `enumerate_candidate_prs`, which emits `number<TAB>mode` so nothing downstream asks GitHub twice.
- Why a failed label lookup skips rather than defaulting to code: a wrong-mode review posts comments that cannot be taken back.
- Why label rather than a path heuristic or a classifier: explicit, per-PR, author-controlled, no nondeterminism in the harness's control flow.
- `PERSONA_DIR` as a parent of `code/` and `plan/`, both resolved at startup so a broken plan persona dies at boot.
- The pair key `"$pr:$mode:$persona"`, and that a PR whose label changes orphans its old sessions by design.
- The per-mode defaults, and that the code subset needs no new justification because it is the reasoning already at `DEFAULT_PERSONAS_CODE`.
- The importer tension: identical bodies in both trees, drift caught by reviewing the importer's diff, deliberately no machinery.
- That `--resume` is load-bearing for plan mode rather than incidental, because feedback on a plan produces a revised plan on the same branch.
- That `_plan_stanza` says what not to flag as well as what to review, and why.

Also update the "Two pieces working together" bullet, which describes the loop as reviewing "each PR with each enabled persona": it now reviews each PR with each persona enabled **for that PR's mode**.

Update the Configuration section with the six new vars: `PLAN_LABEL` (default `plan`), `PLAN_PERSONAS` (default all six), `PLAN_REVIEW_PROMPT`, `PLAN_FOLLOWUP_PROMPT`, `PLAN_REVIEW_PROMPT_SUFFIX`, `PLAN_FOLLOWUP_PROMPT_SUFFIX`.

Update the "Gotchas when editing" bullet about `test-providers.sh` pinning `PERSONAS=red_team` to add that its `gh` stub answers label queries and every case there is code mode.

- [ ] **Step 2: Update `README.md`**

Add the two-mode framing and the label workflow: label a PR `plan` and it gets reviewed as a proposal by all six personas, leave it unlabeled and it gets today's code review by the four. Document the six new vars in whatever table or list the file already uses for the others. Correct the advocate provenance note to say these are plan-review personas on loan, with code mode running the subset that survives contact with a diff.

- [ ] **Step 3: Update `.env.example`**

Add the six new vars with commented defaults, following the file's existing style. Write no quoted values: `docker run --env-file` does no quote processing, and a quoted value arrives with its quotes attached.

- [ ] **Step 4: Update `HISTORY.md`**

Add an entry recording the correction: what was misread, what changed, and the two decisions most likely to be questioned later (skip-on-failed-lookup, and no importer divergence detection).

- [ ] **Step 5: Verify the docs match the code**

Run: `grep -n 'PLAN_LABEL\|PLAN_PERSONAS\|PLAN_REVIEW_PROMPT\|PLAN_FOLLOWUP_PROMPT' entrypoint.sh .env.example README.md CLAUDE.md`
Expected: every var appears in all four files.

Run: `./test-personas.sh && ./test-providers.sh && ./test-shim.sh`
Expected: all three suites green.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md README.md .env.example HISTORY.md
git commit -m "docs: two review modes, and the correction that produced them

advocate's personas are plan-review personas; phase 1 shipped them as
the general PR-review mechanism. Document mode routing, the per-mode
persona trees, and the six new operator vars."
```
