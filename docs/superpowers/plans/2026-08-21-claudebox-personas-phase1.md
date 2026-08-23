# claudebox Multi-Persona Review, Phase 1 (Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace claudebox's single generalist review session per PR with one session per PR per adversarial persona, selectable with `--persona`, and add the usage-limit handling that running several times as many sessions per cycle requires.

**Architecture:** Persona definitions are markdown files shipped in the image (frontmatter `label`/`success`, body = system prompt), imported from advocate by a committed script. `entrypoint.sh` resolves the enabled set at startup, caches each persona's prompt, and keys its session and pass-count maps by `"$pr:$persona"`. Each pass re-passes the persona prompt through `--append-system-prompt`, which is mandatory because that flag does not survive `--resume`. A pass that fails on a usage or rate limit keeps its session, ends the cycle, and backs off.

**Tech Stack:** bash (entrypoint runs on bash 4+ inside the image; `claudebox.sh` must stay bash 3.2 safe for macOS hosts), Docker, `gh`, `jq`, Claude Code CLI, python3 (authoring-time import script only).

**Spec:** `docs/superpowers/specs/2026-08-21-claudebox-personas-design.md`

## Global Constraints

- `claudebox.sh` runs on the host, where macOS ships **bash 3.2**: no `declare -A`, no `${arr[@]}` on a possibly-empty array (use `${arr[@]+"${arr[@]}"}`). It parses no persona configuration; it forwards strings.
- `entrypoint.sh` runs inside the image on modern bash and may use `declare -A`.
- The three hardening boundaries are untouched: unprivileged `reviewer` user, `/repo` mounted read-only with a local clone under `$HOME`, privilege-minimized token. `--strict-mcp-config` stays in `CLAUDE_MCP_ARGS`.
- No credential value may reach a persona prompt, a log line, or any file this plan creates.
- No model fallback anywhere: a model that cannot be used is an error, never a silent substitution.
- Operator-supplied `REVIEW_PROMPT` / `FOLLOWUP_PROMPT` must still reach Claude **verbatim**. Persona text travels in the system prompt, never by concatenation onto the task prompt.
- `--mcp-config` is variadic, so the `--` before the prompt in `run_pass` is load-bearing. Any new flag added to those invocations goes **before** the `--`.
- Apostrophes cannot appear inside `${VAR:?message}` validation messages: quote processing applies inside the expansion and one silently breaks the script's parse.
- Default persona set, in this order: `red_team,adversarial,sme,sage`. `user` and `good_friend` exist and run only when named.
- Reserved persona id: `aggregate`. It cannot be selected in phase 1 and is claimed now so phase 2 does not have to break anything.
- Verified fact this plan depends on: `--append-system-prompt` does **not** survive `--resume`. Re-pass it every invocation.
- Syntax check with `bash -n entrypoint.sh` and `bash -n claudebox.sh`. Suites: `./test-providers.sh`, `./test-personas.sh`, `./test-shim.sh`.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `tools/import-advocate-personas.py` | create | One-shot importer: parses advocate's `personas.py` with `ast`, writes the six persona files with advocate's JSON output contract stripped. Committed for provenance and re-import. |
| `personas/{red_team,adversarial,sage,user,sme,good_friend}.md` | create | One persona each: frontmatter `label`/`success`, body = system prompt. Generated, then committed. |
| `personas/_shared.md` | create | The claudebox output contract and the independence rule, appended to every persona body. Hand-written, not generated. |
| `Dockerfile` | modify | `COPY personas/ /opt/claudebox/personas/`. |
| `entrypoint.sh` | modify | Persona registry (load, validate, cache), loop keyed by `pr:persona`, per-pass system prompt, usage-limit classification and backoff. |
| `claudebox.sh` | modify | `--persona LIST` flag, `-e PERSONAS`, help text. |
| `test-personas.sh` | create | Persona suite: indexed per-invocation capture, a two-cycle `sleep` stub, and a `claude` stub that reports success so sessions get recorded and resumed. |
| `test-providers.sh` | modify | Baseline pins one persona so each case still produces exactly one `claude` invocation. |
| `README.md`, `CLAUDE.md`, `.env.example`, `HISTORY.md` | modify | Operator-facing surface, architecture notes, cadence consequence. |

---

### Task 1: Persona definitions and the registry

**Files:**
- Create: `tools/import-advocate-personas.py`
- Create: `personas/red_team.md`, `personas/adversarial.md`, `personas/sage.md`, `personas/user.md`, `personas/sme.md`, `personas/good_friend.md` (generated)
- Create: `personas/_shared.md`
- Create: `test-personas.sh`
- Modify: `Dockerfile` (after the `COPY --chown=reviewer:reviewer entrypoint.sh ...` line, around line 100)
- Modify: `entrypoint.sh` (new "Persona registry" block after the "PR selection" block, which ends at the `render_prompt` function around line 172)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `PERSONA_DIR` (env, default `/opt/claudebox/personas`)
  - `PERSONAS` (env, comma or whitespace separated ids, or the literal `all`)
  - `resolve_personas()` — validates and fills the global array `PERSONAS_LIST`
  - `persona_meta <id> <key>` — echoes a frontmatter value
  - `persona_prompt <id>` — echoes the full system prompt (body + `_shared.md`, `{{PERSONA}}` substituted with the label)
  - `PERSONA_PROMPT` — `declare -A`, id to prompt text, populated once at startup
  - `PERSONA_LABEL` — `declare -A`, id to label

- [ ] **Step 1: Write the importer**

Create `tools/import-advocate-personas.py`:

```python
#!/usr/bin/env python3
"""Import advocate's persona prompts into claudebox persona definition files.

Run once, commit the output. Re-run when advocate's prompts change:

    ./tools/import-advocate-personas.py ~/wander/advocate/src/advocate/personas.py personas

advocate's personas.py is parsed with `ast` rather than imported, because
importing it pulls in pydantic. Every SYSTEM_PROMPTS entry is an f-string whose
only interpolation is _COMMON_OUTPUT_FORMAT, so dropping every interpolation is
exactly the transformation we want: it removes advocate's JSON output contract
(claudebox posts gh comments, not JSON) and keeps the persona identity. The
assertion below fails loudly if that stops being true upstream.
"""
import ast
import pathlib
import sys

src = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
tree = ast.parse(src.read_text())


def literal_text(node):
    """The literal parts of a string or f-string, interpolations dropped."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    raise TypeError(ast.dump(node)[:120])


def enum_id(node):
    assert isinstance(node, ast.Attribute), ast.dump(node)[:120]
    return node.attr


meta, prompts = {}, {}
for stmt in tree.body:
    if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
        continue
    if stmt.target.id == "PERSONA_META":
        for k, v in zip(stmt.value.keys, stmt.value.values):
            fields = {kk.value: vv for kk, vv in zip(v.keys, v.values)}
            meta[enum_id(k)] = {
                "label": literal_text(fields["name"]),
                "success": literal_text(fields["success"]),
            }
    elif stmt.target.id == "SYSTEM_PROMPTS":
        for k, v in zip(stmt.value.keys, stmt.value.values):
            prompts[enum_id(k)] = literal_text(v).strip()

assert set(meta) == set(prompts), (sorted(meta), sorted(prompts))
out.mkdir(parents=True, exist_ok=True)
for pid in sorted(meta):
    body = prompts[pid]
    assert "JSON" not in body, f"{pid}: advocate's output contract survived extraction"
    (out / f"{pid}.md").write_text(
        "---\n"
        f"label: {meta[pid]['label']}\n"
        f"success: {meta[pid]['success']}\n"
        "---\n"
        f"{body}\n"
    )
    print(f"wrote {pid}.md  label={meta[pid]['label']!r}  {len(body)} chars")
```

- [ ] **Step 2: Run it and check the output**

```bash
chmod +x tools/import-advocate-personas.py
./tools/import-advocate-personas.py ~/wander/advocate/src/advocate/personas.py personas
ls personas/
head -5 personas/red_team.md
grep -rl "JSON" personas/ || echo "no output contract survived"
```

Expected: six files (`adversarial.md`, `good_friend.md`, `red_team.md`, `sage.md`, `sme.md`, `user.md`), each starting with a `---` frontmatter block carrying `label:` and `success:`, and no file mentioning JSON.

- [ ] **Step 3: Write the shared contract**

Create `personas/_shared.md`. The leading underscore keeps it out of the selectable set.

```markdown
## How to report what you find

Post one comment per finding on the pull request with the GitHub CLI, and sign
each one `-claudebox ({{PERSONA}})`. A finding is worth a comment when you can
point at the specific part of the change that demonstrates it and say what to do
about it.

If the change is solid and you have no findings, say so and post nothing. Do not
manufacture findings to appear thorough. Silence from you is a strong signal.

## You are not the only reviewer here

Other personas review this same pull request, each with a different angle of
attack, and their comments are signed `-claudebox (<their label>)`. Those
comments are not yours. Do not defer to them. Do not treat their existence as
coverage of anything. Do not suppress a finding because another persona reached a
similar conclusion from a different direction: a thing that two angles of attack
both hit is more important than a thing only one of them hit, not less. Reaching
your own verdict from your own angle is the entire reason you are a separate
reviewer, and a separate pass exists to reconcile what the personas collectively
said.

Human replies to your own findings are worth reading and worth answering.
```

- [ ] **Step 4: Ship the definitions in the image**

In `Dockerfile`, immediately after the `COPY --chown=reviewer:reviewer entrypoint.sh /usr/local/bin/entrypoint.sh` line, add:

```dockerfile
# Persona definitions for the adversarial review set: one file per persona
# (frontmatter label/success, body = system prompt) plus _shared.md, which every
# persona body gets appended to. Read at runtime from PERSONA_DIR, which an
# operator can point at a read-only mount to supply their own set. Imported from
# advocate by tools/import-advocate-personas.py; see CLAUDE.md.
COPY --chown=reviewer:reviewer personas/ /opt/claudebox/personas/
```

- [ ] **Step 5: Write the failing tests**

Create `test-personas.sh`. This is the whole suite harness plus Task 1's cases; later tasks add cases to it.

```bash
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

printf '#!/bin/sh\nexit 0\n' >"$BIN/gh"
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
# the next cycle resumes it. STUB_FAIL_ON=N makes invocation N fail, and
# STUB_FAIL_MODE picks how: `limit` writes a rate-limit message to stderr,
# anything else writes an ordinary error. A failing invocation still emits its
# init event first, because that is what really happens: the session exists and
# then a request fails, which is exactly the case where throwing the session id
# away is the wrong move.
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
if [ "${STUB_FAIL_ON:-0}" = "$n" ]; then
  printf '{"type":"system","subtype":"init","session_id":"S%s"}\n' "$n"
  if [ "${STUB_FAIL_MODE:-limit}" = "limit" ]; then
    echo "API Error: 429 rate limit exceeded" >&2
  else
    echo "API Error: 400 invalid request" >&2
  fi
  exit 1
fi
printf '{"type":"system","subtype":"init","session_id":"S%s"}\n' "$n"
printf '{"type":"result","subtype":"success","session_id":"S%s","result":"ok"}\n' "$n"
exit 0
STUB
chmod +x "$BIN"/*

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
          ARGV:*)   grep -qF -- "$rest" <(grep '^ARGV ' "$dump") || missing="$missing [argv $n missing: $rest]" ;;
          NOARGV:*) grep -qF -- "$rest" <(grep '^ARGV ' "$dump") && missing="$missing [argv $n should not have: $rest]" ;;
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
  for f in "$SCRIPT_DIR"/personas/*.md; do
    b="$(basename "$f" .md)"
    case "$b" in _*) continue ;; esac
    head -1 "$f" | grep -qx -- "---" || problems="$problems [$b: no frontmatter]"
    grep -qE '^label: [A-Za-z0-9 ._-]+$' "$f" || problems="$problems [$b: no usable label]"
    grep -q '^success: ' "$f" || problems="$problems [$b: no success criterion]"
    grep -qF "JSON" "$f" && problems="$problems [$b: carries an output contract]"
  done
  [ -f "$SCRIPT_DIR/personas/_shared.md" ] || problems="$problems [_shared.md missing]"
  grep -qF '{{PERSONA}}' "$SCRIPT_DIR/personas/_shared.md" || problems="$problems [_shared.md has no {{PERSONA}} token]"
  if [ -n "$problems" ]; then bad "definitions: every persona file is well formed" "$problems"
  else ok "definitions: every persona file is well formed"; fi
fi

# --- selection --------------------------------------------------------------
cycle "selection: default set is the four code-facing personas in order" \
  -- CALLS:8 LOG:"personas: red_team adversarial sme sage"

cycle "selection: an explicit list is honoured, in the order given" \
  PERSONAS=sage,red_team \
  -- CALLS:4 LOG:"personas: sage red_team"

cycle "selection: all expands to every shipped persona" \
  PERSONAS=all \
  -- CALLS:12 LOG:"personas: adversarial good_friend red_team sage sme user"

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

printf '\n%d passed, %d failed' "$PASS" "$FAIL"
[ "$SKIP" -gt 0 ] && printf ', %d skipped' "$SKIP"
printf '\n'
[ "$FAIL" -eq 0 ] || { printf 'failed:%s\n' "$FAILED_LABELS"; exit 1; }
```

- [ ] **Step 6: Run the tests to verify they fail**

```bash
chmod +x test-personas.sh
./test-personas.sh
```

Expected: the `definitions:` case PASSES (Steps 2 and 3 created the files). Every `selection:` case FAILS, because `entrypoint.sh` knows nothing about personas yet: the `refuses` cases fail with "expected a startup failure, but a review pass ran", and the `cycle` cases fail on the invocation count (2, one per cycle) and the missing log line.

- [ ] **Step 7: Implement the registry in `entrypoint.sh`**

Insert this block immediately after the `render_prompt` function (end of the "PR selection" block, around line 172):

```bash
# --- Persona registry ------------------------------------------------------
# Each review pass runs as one of advocate's adversarial personas rather than as
# a generalist reviewer. A persona is a file in PERSONA_DIR: frontmatter (label,
# success) plus a body that becomes the pass's system prompt. Files starting with
# an underscore are not personas; _shared.md is the output contract appended to
# every persona body.
#
# Definitions live in files rather than inline here for three reasons: it keeps
# ~200 lines of prompt text out of this script, it gives an operator an override
# by mounting their own directory at PERSONA_DIR, and it keeps the imported text
# close to its provenance (tools/import-advocate-personas.py).
PERSONA_DIR="${PERSONA_DIR:-/opt/claudebox/personas}"
# The default set is code-facing. advocate's `user` and `good_friend` were written
# against designs and whole projects; on a narrow diff they reach for material
# that isn't in it, so they ship but are opt-in.
DEFAULT_PERSONAS="red_team,adversarial,sme,sage"
# Claimed now, used in phase 2: the pass that reconciles what the personas said
# is the only one allowed to read their findings, which is why it is not itself
# a persona and cannot be selected as one.
RESERVED_PERSONAS="aggregate"

declare -A PERSONA_PROMPT=()
declare -A PERSONA_LABEL=()
PERSONAS_LIST=()

# Echo frontmatter key $2 from persona $1.
persona_meta() {
  awk -v k="$2" '
    NR == 1 && $0 == "---" { fm = 1; next }
    fm && $0 == "---" { exit }
    fm && index($0, k ":") == 1 { sub(/^[^:]*:[[:space:]]*/, ""); print; exit }
  ' "$PERSONA_DIR/$1.md"
}

# Echo persona $1's full system prompt: its body, then the shared contract, with
# {{PERSONA}} replaced by its label. The label is validated in resolve_personas
# to contain no slash, so it is safe as a sed replacement.
persona_prompt() {
  local id="$1" label="${PERSONA_LABEL[$1]}"
  {
    awk '
      NR == 1 && $0 == "---" { fm = 1; next }
      fm && $0 == "---" { fm = 0; body = 1; next }
      body
    ' "$PERSONA_DIR/$id.md"
    printf '\n'
    cat "$PERSONA_DIR/_shared.md"
  } | sed "s|{{PERSONA}}|$label|g"
}

# Fill PERSONAS_LIST (order-preserving), PERSONA_LABEL and PERSONA_PROMPT from
# PERSONAS, or from DEFAULT_PERSONAS when it is unset. Dies on anything it can't
# resolve: a typo that silently narrowed the review to one persona, or to none,
# would look exactly like a working run in the log.
resolve_personas() {
  local avail="" f b tok raw
  [ -d "$PERSONA_DIR" ] || die "no persona definitions: PERSONA_DIR=$PERSONA_DIR is not a directory."
  for f in "$PERSONA_DIR"/*.md; do
    [ -e "$f" ] || continue
    b="$(basename "$f" .md)"
    case "$b" in _*) continue ;; esac
    avail="$avail $b"
  done
  [ -n "$avail" ] || die "no persona definitions found in $PERSONA_DIR."

  raw="${PERSONAS-$DEFAULT_PERSONAS}"
  case "$(printf '%s' "$raw" | tr 'A-Z' 'a-z')" in
    all) raw="$(printf '%s' "$avail")" ;;
  esac

  for tok in $(printf '%s' "$raw" | tr ',' ' '); do
    case " $RESERVED_PERSONAS " in
      *" $tok "*) die "persona '$tok' is reserved and cannot be selected." ;;
    esac
    case " $avail " in
      *" $tok "*) ;;
      *) die "unknown persona '$tok'; available:$avail" ;;
    esac
    case " ${PERSONAS_LIST[*]-} " in
      *" $tok "*) die "persona '$tok' is listed twice in PERSONAS." ;;
    esac
    PERSONAS_LIST+=("$tok")
  done
  [ "${#PERSONAS_LIST[@]}" -gt 0 ] || die "PERSONAS is set but names no persona; unset it for the default set ($DEFAULT_PERSONAS), or name one of:$avail"

  # Resolve labels and prompts once, so a pass is a string lookup rather than
  # three file reads, and so a broken definition fails at startup.
  local id label
  for id in "${PERSONAS_LIST[@]}"; do
    label="$(persona_meta "$id" label)"
    case "$label" in
      '') die "persona '$id' has no label: in its frontmatter." ;;
      *[!A-Za-z0-9\ ._-]*) die "persona '$id' has a label with unexpected characters: '$label' (letters, digits, spaces, dot, underscore and hyphen only)." ;;
    esac
    PERSONA_LABEL[$id]="$label"
    PERSONA_PROMPT[$id]="$(persona_prompt "$id")"
    [ -n "${PERSONA_PROMPT[$id]}" ] || die "persona '$id' has an empty prompt body."
  done
  log "personas: ${PERSONAS_LIST[*]}"
}
```

- [ ] **Step 8: Call it during startup validation**

In the block that validates PR selection (around line 316, `resolve_pr_selection` followed by the two `{{PR}}` warnings), add the persona resolution immediately after `resolve_pr_selection`:

```bash
resolve_pr_selection
resolve_personas
```

- [ ] **Step 9: Run the tests**

```bash
bash -n entrypoint.sh && ./test-personas.sh
```

Expected: every `refuses` case and the three `LOG:` expectations PASS. The `CALLS:` expectations still FAIL (8, 4 and 12 expected against 2 actual) because the loop does not yet run one pass per persona. That is Task 2.

- [ ] **Step 10: Commit**

```bash
git add tools/import-advocate-personas.py personas/ test-personas.sh Dockerfile entrypoint.sh
git commit -m "feat(personas): import advocate's persona definitions and resolve a selected set

Persona definitions ship as files in the image (PERSONA_DIR) rather than inline
prompt strings: it keeps the prompt text next to its provenance, and lets an
operator mount their own set. Default set is code-facing (red_team, adversarial,
sme, sage); advocate's user and good_friend ship but are opt-in, because they
were written against designs rather than diffs. advocate's JSON output contract
is stripped on import, since claudebox posts gh comments; its
do-not-manufacture-findings rule is kept in _shared.md, along with the rule that
personas do not defer to each other.

An unresolvable selector is a startup error: a typo that silently narrowed the
review to one persona would read as a working run."
```

---

### Task 2: One session per PR per persona

**Files:**
- Modify: `entrypoint.sh` (the "Review loop" block: the `PR_SESSION`/`PR_PASSES` declarations around line 874, `run_pass` around lines 906-936, and the `while true` loop around lines 937-985)
- Modify: `test-personas.sh` (add cases)

**Interfaces:**
- Consumes: `PERSONAS_LIST`, `PERSONA_PROMPT` (Task 1)
- Produces:
  - `run_pass <prompt> <session-id> <persona>` — third parameter added; still sets `RUN_PASS_SESSION_ID` and returns claude's exit code
  - `PR_SESSION` / `PR_PASSES` keys change from `"$pr"` to `"$pr:$persona"`

- [ ] **Step 1: Write the failing tests**

Append these cases to `test-personas.sh`, before the final `printf '\n%d passed...'` summary block:

```bash
# --- per-persona passes -----------------------------------------------------
cycle "passes: each persona gets its own pass, in the selected order" \
  PERSONAS=red_team,sage \
  -- CALLS:4 \
     ARGV:1:"You are a Red Team security reviewer" \
     ARGV:2:"You are a Sage" \
     ARGV:3:"You are a Red Team security reviewer" \
     ARGV:4:"You are a Sage"

cycle "passes: the persona travels in the system prompt, not the task prompt" \
  PERSONAS=red_team \
  -- ARGV:1:"--append-system-prompt" \
     ARGV:1:"Perform a thorough review of pull request #1"

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
     LOG:"PR #1 [red_team] reached MAX_PASSES_PER_SESSION=1"

cycle "model: every tier still points at the one review model" \
  PERSONAS=red_team \
  -- ENV:1:ANTHROPIC_MODEL=glm-5.2:cloud \
     ENV:1:ANTHROPIC_DEFAULT_OPUS_MODEL=glm-5.2:cloud \
     ENV:1:ANTHROPIC_SMALL_FAST_MODEL=glm-5.2:cloud
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./test-personas.sh passes
./test-personas.sh resume
```

Expected: FAIL. `CALLS:4` reports 2 invocations (one generic pass per cycle), and every `ARGV:` expectation naming `--append-system-prompt` or persona text is missing, because `run_pass` does not yet take a persona.

- [ ] **Step 3: Add the persona parameter to `run_pass`**

Replace the `run_pass` function (currently `run_pass() { local prompt="$1" sid="$2" ... }`) with this. Two changes beyond the new parameter: the session id is recovered **before** the exit-code check, and the persona prompt is re-passed on both invocation forms.

```bash
# Run one review pass for $1=prompt as persona $3, resuming $2=session id when
# non-empty. Sets RUN_PASS_SESSION_ID to the recovered id (falling back to the
# passed one) and returns claude's exit code.
#
# --append-system-prompt is passed on BOTH forms, and that is not redundant:
# measured 2026-08-21, the flag does NOT survive --resume. Pass it only on the
# first pass and cycle one is adversarial while every later cycle is the old
# generalist reviewer wearing this persona's name in the log.
#
# It goes before the `--`, like every other flag: --mcp-config is variadic, so
# the `--` is what stops the CLI reading the prompt as another config path.
RUN_PASS_SESSION_ID=""
run_pass() {
  local prompt="$1" sid="$2" persona="$3" rc errfile rawfile got
  RUN_PASS_SESSION_ID="$sid"
  errfile="$(mktemp)"; rawfile="$(mktemp)"
  # set +e around the pipeline so a formatter hiccup can't abort the script and
  # so we can read Claude's own exit code via PIPESTATUS[0] (not tee's/jq's).
  set +e
  if [ -n "$sid" ]; then
    claude -p --resume "$sid" --output-format stream-json --verbose \
      --dangerously-skip-permissions --model "$REVIEW_MODEL" \
      --append-system-prompt "${PERSONA_PROMPT[$persona]}" \
      "${CLAUDE_MCP_ARGS[@]}" -- "$prompt" \
      2>"$errfile" | stdbuf -oL tee "$rawfile" | format_stream
  else
    claude -p --output-format stream-json --verbose \
      --dangerously-skip-permissions --model "$REVIEW_MODEL" \
      --append-system-prompt "${PERSONA_PROMPT[$persona]}" \
      "${CLAUDE_MCP_ARGS[@]}" -- "$prompt" \
      2>"$errfile" | stdbuf -oL tee "$rawfile" | format_stream
  fi
  rc=${PIPESTATUS[0]}
  set -e
  # session_id appears in the init and result events; take the last one seen.
  # Recovered before the exit-code check on purpose: a pass that started a
  # session and then failed still has a resumable session, and Task 3's
  # usage-limit path depends on knowing its id.
  got="$(jq -r -R '(fromjson? // empty) | select(.session_id) | .session_id' "$rawfile" 2>/dev/null | tail -n 1 || true)"
  [ -n "$got" ] && RUN_PASS_SESSION_ID="$got"
  if [ "$rc" -ne 0 ]; then
    log "WARN: claude exited $rc:"; tail -n 5 "$errfile" >&2
    rm -f "$errfile" "$rawfile"; return "$rc"
  fi
  rm -f "$errfile" "$rawfile"
  return 0
}
```

- [ ] **Step 4: Re-key the state maps**

Replace the comment and declarations above `format_stream` (currently "Per-PR state: session id and successful-pass count, keyed by PR number...") with:

```bash
# Per-(PR, persona) state: session id and successful-pass count, keyed by
# "$pr:$persona". These are in-memory only, so a container restart re-reviews
# each PR once per persona (and may re-comment once) — the same trade-off the
# single-session design had, multiplied by the size of the enabled set.
declare -A PR_SESSION=()
declare -A PR_PASSES=()
```

- [ ] **Step 5: Run each persona over each PR**

Replace the body of the `for pr in ${prs[@]+"${prs[@]}"}; do ... done` loop with the nested form:

```bash
  # Review each PR with each enabled persona, sequentially: they share one
  # working clone, and more importantly running them concurrently would multiply
  # instantaneous usage-limit pressure. Personas are blind to each other by
  # design (see personas/_shared.md), so nothing about the order is semantic —
  # but it is stable, so a cycle cut short is interpretable.
  for pr in ${prs[@]+"${prs[@]}"}; do
    for persona in "${PERSONAS_LIST[@]}"; do
      key="$pr:$persona"
      sid="${PR_SESSION[$key]:-}"
      if [ -z "$sid" ]; then
        log "Reviewing PR #$pr as $persona (new session)..."
        prompt="$(render_prompt "$REVIEW_PROMPT" "$pr")"
      else
        log "Reviewing PR #$pr as $persona (resuming session $sid)..."
        prompt="$(render_prompt "$FOLLOWUP_PROMPT" "$pr")"
      fi

      if run_pass "$prompt" "$sid" "$persona"; then
        PR_SESSION[$key]="$RUN_PASS_SESSION_ID"
        PR_PASSES[$key]=$(( ${PR_PASSES[$key]:-0} + 1 ))
        log "PR #$pr [$persona] review complete (session ${PR_SESSION[$key]}, pass ${PR_PASSES[$key]})."
        # Rotate this pair's session once its cap is hit, to bound context growth.
        if [ "$MAX_PASSES_PER_SESSION" -gt 0 ] && [ "${PR_PASSES[$key]}" -ge "$MAX_PASSES_PER_SESSION" ]; then
          log "PR #$pr [$persona] reached MAX_PASSES_PER_SESSION=$MAX_PASSES_PER_SESSION; rotating its session next cycle."
          unset 'PR_SESSION[$key]'
          PR_PASSES[$key]=0
        fi
      else
        log "WARN: PR #$pr [$persona] review failed; starting a fresh session for it next cycle."
        unset 'PR_SESSION[$key]'
        PR_PASSES[$key]=0
      fi
    done
  done
```

- [ ] **Step 6: Run the tests**

```bash
bash -n entrypoint.sh && ./test-personas.sh
```

Expected: every case PASSES, including the three `CALLS:` expectations from Task 1.

- [ ] **Step 7: Commit**

```bash
git add entrypoint.sh test-personas.sh
git commit -m "feat(personas): one review session per PR per persona

PR_SESSION and PR_PASSES are keyed by pr:persona, and run_pass takes the persona
so it can pass its system prompt with --append-system-prompt on every
invocation. Re-passing it is mandatory, not belt-and-braces: measured, the flag
does not survive --resume, so passing it once would make cycle one adversarial
and every later cycle the old generalist under a persona's name.

run_pass now recovers the session id before checking the exit code, because a
pass that started a session and then failed still has a resumable one."
```

---

### Task 3: Usage-limit handling

**Files:**
- Modify: `entrypoint.sh` (new `is_usage_limit` helper near `run_pass`; the failure branch of the persona loop; the sleep at the end of the cycle; a `LIMIT_BACKOFF_SECONDS` default in the "Defaults" block around line 255)
- Modify: `test-personas.sh` (add cases)

**Interfaces:**
- Consumes: `run_pass` and `RUN_PASS_SESSION_ID` (Task 2)
- Produces:
  - `is_usage_limit <stderr-file>` — returns 0 when the failure looks like a usage, rate or capacity limit
  - `RUN_PASS_LIMITED` — `1` when the last pass failed that way, else `0`
  - `LIMIT_BACKOFF_SECONDS` (env, default 1800)

- [ ] **Step 1: Write the failing tests**

Append to `test-personas.sh`, before the summary block:

```bash
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

cycle "limits: an unrecognised failure degrades to the ordinary path" \
  PERSONAS=red_team STUB_FAIL_ON=1 STUB_FAIL_MODE=other STUB_MAX_CYCLES=1 \
  -- CALLS:1 \
     NOLOG:"Backing off"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
./test-personas.sh limits
```

Expected: FAIL. The first case reports `[argv 2 missing: --resume S1]`, because today's failure branch drops the session unconditionally, and both `LOG:` expectations are missing.

- [ ] **Step 3: Add the backoff default**

In the "Defaults" block, after the `REVIEW_INTERVAL_SECONDS` line:

```bash
# How long to wait after a pass fails on a usage or rate limit, instead of the
# normal interval. Long by default: the limit that stopped us is measured in
# hours on most plans, and retrying into it costs the same allowance twice.
LIMIT_BACKOFF_SECONDS="${LIMIT_BACKOFF_SECONDS:-1800}"
case "$LIMIT_BACKOFF_SECONDS" in ''|*[!0-9]*) die "LIMIT_BACKOFF_SECONDS must be a non-negative integer";; esac
```

- [ ] **Step 4: Add the classifier and set the flag**

Immediately above `run_pass`, add:

```bash
# True when the stderr in $1 reads as a usage, rate or capacity limit rather than
# a real failure. Worth distinguishing because the two want opposite handling: a
# broken session should be replaced, a throttled one should be resumed.
#
# This matches on provider error text, which is an upstream surface that can
# change without notice, so the failure mode of a miss matters: a missed match
# falls through to the ordinary path (drop the session, carry on), which is
# exactly today's behaviour. A false positive keeps a session that will fail
# again next cycle and be dropped then. Neither wedges the loop.
is_usage_limit() {
  grep -qiE 'rate.?limit|usage limit|too many requests|quota|overloaded|(^|[^0-9])(429|529)([^0-9]|$)' "$1"
}
```

In `run_pass`, add `RUN_PASS_LIMITED=0` beside the existing `RUN_PASS_SESSION_ID=""` initialiser (both the global and the in-function reset), and set it in the failure branch:

```bash
RUN_PASS_SESSION_ID=""
RUN_PASS_LIMITED=0
run_pass() {
  local prompt="$1" sid="$2" persona="$3" rc errfile rawfile got
  RUN_PASS_SESSION_ID="$sid"
  RUN_PASS_LIMITED=0
```

and in the `if [ "$rc" -ne 0 ]; then` branch, before the `log "WARN: claude exited $rc:"` line:

```bash
    if is_usage_limit "$errfile"; then RUN_PASS_LIMITED=1; fi
```

- [ ] **Step 5: Handle it in the loop**

In the persona loop, replace the single `else` failure branch with two branches:

```bash
      elif [ "$RUN_PASS_LIMITED" = 1 ]; then
        # Keep the session. Abandon the rest of the cycle rather than walking the
        # remaining personas into the same wall, and back off before the next one.
        [ -n "$RUN_PASS_SESSION_ID" ] && PR_SESSION[$key]="$RUN_PASS_SESSION_ID"
        log "WARN: PR #$pr [$persona] hit a usage or rate limit; keeping its session and ending this cycle early."
        limited=1
        break 2
      else
        log "WARN: PR #$pr [$persona] review failed; starting a fresh session for it next cycle."
        unset 'PR_SESSION[$key]'
        PR_PASSES[$key]=0
      fi
```

Reset the flag at the top of each cycle, immediately after `check_litellm`:

```bash
  limited=0
```

And replace the final `log "Sleeping ${REVIEW_INTERVAL_SECONDS}s..."` / `sleep "$REVIEW_INTERVAL_SECONDS"` pair with:

```bash
  if [ "$limited" = 1 ]; then
    log "Backing off ${LIMIT_BACKOFF_SECONDS}s after a usage limit..."
    sleep "$LIMIT_BACKOFF_SECONDS"
  else
    log "Sleeping ${REVIEW_INTERVAL_SECONDS}s..."
    sleep "$REVIEW_INTERVAL_SECONDS"
  fi
```

- [ ] **Step 6: Run the tests**

```bash
bash -n entrypoint.sh && ./test-personas.sh
```

Expected: every case PASSES.

- [ ] **Step 7: Commit**

```bash
git add entrypoint.sh test-personas.sh
git commit -m "feat(personas): keep the session and back off on a usage limit

Running one pass per persona multiplies sessions per cycle by the size of the
enabled set, which moves hitting a plan's usage limit from theoretical to
routine. The existing failure path is the worst available response to that: it
drops the session, so the next cycle re-reads the whole PR and re-posts findings
already posted, spending more of the allowance that just ran out.

A limited pass now keeps its session, abandons the rest of the cycle instead of
walking the remaining personas into the same wall, and backs off for
LIMIT_BACKOFF_SECONDS. Detection matches provider error text, so a miss degrades
to the old behaviour rather than to a crash."
```

---

### Task 4: The `--persona` launcher flag

**Files:**
- Modify: `claudebox.sh` (defaults block around line 25-39, `usage()` around line 89-95, argument parsing around line 140, `build_run_flags` around line 280)
- Modify: `test-providers.sh` (the `env -i` baseline in `run_entrypoint`, around line 116)

**Interfaces:**
- Consumes: `PERSONAS` env var read by `resolve_personas` (Task 1)
- Produces: `--persona LIST` on the launcher, forwarded as `-e PERSONAS=LIST`

- [ ] **Step 1: Pin one persona in the provider suite**

`test-providers.sh` exists to prove credential and model wiring, and every case in it assumes exactly one `claude` invocation per cycle (its stub overwrites `$HOME/dump`). Personas would give it four. Rather than rewrite 70 cases for reasons unrelated to what they test, pin the persona in the baseline. In `run_entrypoint`, add to the `env -i` line, after the `REPO_PATH=... REVIEW_INTERVAL_SECONDS=1` line:

```bash
    PERSONA_DIR="$SCRIPT_DIR/personas" PERSONAS=red_team \
```

with this comment above the `env -i` call, appended to the existing one:

```bash
  # PERSONAS is pinned to a single persona so each case still produces exactly
  # ONE `claude` invocation: the stub below overwrites its dump per call, and
  # what this suite asserts (credentials, endpoints, model tiers) is identical
  # for every persona. Multi-persona behaviour lives in test-personas.sh, which
  # captures per invocation.
```

- [ ] **Step 2: Run both suites to confirm the pin works**

```bash
./test-providers.sh && ./test-personas.sh
```

Expected: both green. `test-providers.sh` reports the same case count as before this plan started.

- [ ] **Step 3: Add the flag to the launcher**

In the defaults block, after the PR selector variables:

```bash
# Persona selection (parsed and validated by the entrypoint, not here — this is
# bash 3.2 on macOS, and the entrypoint is authoritative anyway).
PERSONAS=""
```

In the `case` statement, after the `--search)` line:

```bash
    --persona)     PERSONAS="${2:?--persona requires a comma-separated list of persona names}"; shift ;;
```

In `build_run_flags`, beside the PR selector pass-through and **before** the trailing `true`:

```bash
  [ -n "$PERSONAS" ] && RUN_FLAGS+=(-e "PERSONAS=$PERSONAS")
```

In `usage()`, after the `--search` entry:

```
  --persona LIST    Review with these adversarial personas only (comma list, or
                    'all'). Default: red_team,adversarial,sme,sage. Also
                    available: user, good_friend. One session per PR per persona,
                    so a cycle is (PRs x personas) sequential reviews.
```

- [ ] **Step 4: Verify the flag reaches docker**

```bash
bash -n claudebox.sh
./claudebox.sh run --dry-run --repo . --prs 1 --persona red_team,sage | tr ' ' '\n' | grep -A0 'PERSONAS'
./claudebox.sh run --dry-run --repo . --prs 1 | grep -c 'PERSONAS' || echo "absent when not given: correct"
./claudebox.sh run --persona 2>&1 | head -2
./claudebox.sh --help | grep -A3 -- '--persona'
```

Expected: the first prints `PERSONAS=red_team,sage`; the second prints `absent when not given: correct` (no `-e PERSONAS` when the flag is absent, so an env-file value still applies); the third errors on the missing value; the fourth shows the help entry.

- [ ] **Step 5: Commit**

```bash
git add claudebox.sh test-providers.sh
git commit -m "feat(personas): add --persona to the launcher

The launcher parses nothing: it forwards the string, because it runs on the host
where macOS ships bash 3.2 and the entrypoint is authoritative about persona
names anyway. Omitting the flag emits no -e, so an env-file PERSONAS still
applies.

test-providers.sh pins a single persona in its baseline so each case still
produces exactly one claude invocation. That suite asserts credentials,
endpoints and model tiers, which are identical for every persona; multi-persona
behaviour is test-personas.sh's job."
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md` ("How it works" around line 116-137, "Tests" around line 210, "Configuration" around line 322)
- Modify: `CLAUDE.md` ("What this is", "Commands", "Architecture", "Configuration", "Gotchas when editing")
- Modify: `.env.example` (new persona block before the prompt-override block)
- Modify: `HISTORY.md` (new version section at the top)

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: nothing code depends on.

- [ ] **Step 1: README, "How it works"**

Replace the opening paragraph ("The reviewer runs **one Claude session per PR**. …") with:

```markdown
The reviewer runs **one Claude session per PR per persona**. Each cycle the entrypoint enumerates the candidate PRs (see [PR selection](#pr-selection)), then reviews each one with each enabled persona in its own session: a pair's first review starts a new session with `REVIEW_PROMPT`; later cycles `--resume` that pair's session with `FOLLOWUP_PROMPT`, so a persona remembers what it already flagged and avoids duplicate comments. The PR number is substituted into the prompt's `{{PR}}` token.

A persona is an angle of attack, borrowed from [advocate](https://github.com/jmcentire/advocate): Red Team wants the change to survive assault, Adversarial wants its logic to hold under challenge, Sage wants it simplified, Subject Matter Expert wants a peer to sign off, User wants a stranger to navigate it, Good Friend applies the 3am test. The first four run by default; `user` and `good_friend` were written against designs rather than diffs, so they ship but are opt-in via `--persona`.

Personas are deliberately **blind to each other**. Nothing tells a persona to defer to another's comments, because that would anchor it to a review it did not do, and avoiding that kind of group-think is the reason this tool exists. Overlapping findings between two angles of attack are a signal that something is worth two comments, not noise to suppress. Each comment is signed with the persona that raised it, e.g. `-claudebox (Red Team)`.

**A cycle is now (candidate PRs x enabled personas) sequential sessions.** `REVIEW_INTERVAL_SECONDS` is the gap *after* a cycle, so four PRs and four personas is sixteen reviews before the interval starts. Set `--persona` to one name for the cheapest run.
```

Replace the code block below it with the current invocation shape:

```bash
# CLAUDE_MCP_ARGS is always (--strict-mcp-config), plus (--mcp-config "$MCP_CONFIG_FILE")
# when LINEAR_API_KEY is set. The "--" is load-bearing: --mcp-config is variadic, so
# without it the prompt would be parsed as another MCP config path.

# a (PR, persona) pair's first review — new session
claude -p --output-format stream-json --verbose --dangerously-skip-permissions \
  --model "$REVIEW_MODEL" --append-system-prompt "${PERSONA_PROMPT[$persona]}" \
  "${CLAUDE_MCP_ARGS[@]}" -- "${REVIEW_PROMPT//\{\{PR\}\}/$pr}"
# later cycles — resume that pair's session
claude -p --resume "${PR_SESSION[$pr:$persona]}" --output-format stream-json --verbose \
  --dangerously-skip-permissions --model "$REVIEW_MODEL" \
  --append-system-prompt "${PERSONA_PROMPT[$persona]}" \
  "${CLAUDE_MCP_ARGS[@]}" -- "${FOLLOWUP_PROMPT//\{\{PR\}\}/$pr}"
```

And add, after the paragraph describing the supervisor:

```markdown
`--append-system-prompt` is passed on *both* forms, which is not redundant: the flag does not survive `--resume`. Passed only on the first pass, cycle one would be adversarial and every later cycle would be a generic reviewer wearing the persona's name in the log.

A pass that fails on a usage or rate limit is treated differently from one that fails for any other reason: it keeps its session, ends the cycle early instead of walking the remaining personas into the same limit, and waits `LIMIT_BACKOFF_SECONDS` (default 1800). Dropping the session there would make the next attempt re-read the whole PR and re-post findings already posted, spending more of the allowance that just ran out.
```

- [ ] **Step 2: README, "Tests"**

Add `./test-personas.sh` to the command block and a paragraph after the `test-shim.sh` one:

```markdown
`test-personas.sh` covers persona selection and the per-persona review loop. It runs **two** cycles rather than one, because the property that matters most cannot be observed in a single cycle: `--append-system-prompt` does not survive `--resume`, so the assertion that has to exist is that a *resumed* pass still carries its persona. It captures one dump per `claude` invocation and asserts the invocation count, each invocation's argv, the resume targets, and the usage-limit path.
```

- [ ] **Step 3: README, "Configuration"**

Add a `### Personas` subsection after "PR selection":

```markdown
### Personas

| Variable | Flag | Default | Meaning |
|---|---|---|---|
| `PERSONAS` | `--persona` | `red_team,adversarial,sme,sage` | Comma list of persona ids, or `all`. Order is honoured. An unknown name is a startup error. |
| `PERSONA_DIR` | — | `/opt/claudebox/personas` | Where definitions are read from. Point it at a read-only mount to supply your own set. |
| `LIMIT_BACKOFF_SECONDS` | — | `1800` | How long to wait after a pass fails on a usage or rate limit, instead of `REVIEW_INTERVAL_SECONDS`. |

Available ids: `red_team`, `adversarial`, `sage`, `sme`, `user`, `good_friend`. A definition file is frontmatter (`label`, `success`) plus a body that becomes the pass's system prompt; `personas/_shared.md` is appended to every body and carries the output contract and the independence rule. `aggregate` is reserved.

Each persona multiplies the sessions per cycle. On a fixed-price plan the binding resource is usage allowance, so start with one or two personas and widen once you have seen what a cycle costs you.
```

- [ ] **Step 4: CLAUDE.md**

Four edits.

In "What this is", after the sentence describing the loop, add:

```markdown
Reviews run as **adversarial personas** borrowed from `~/wander/advocate` (a six-persona review engine that cannot be used directly, because it calls provider APIs and so cannot run against a fixed-price plan). One session per PR per persona.
```

In "Commands", add to the file inventory: `personas/` (definitions), `tools/import-advocate-personas.py` (the importer), `test-personas.sh` (the suite), and note `./test-personas.sh` alongside the other two suites.

In "Architecture", add a subsection after "Two pieces working together":

```markdown
### Personas

Definitions live in `personas/*.md`, read at runtime from `PERSONA_DIR` and shipped in the image. They are files rather than inline strings for three reasons: ~200 lines of prompt text stays out of `entrypoint.sh`, an operator can override the set with a read-only mount, and the imported text stays next to its provenance (`tools/import-advocate-personas.py`, which parses advocate's `personas.py` with `ast` because importing it needs pydantic).

Two transformations happen on import and both are load-bearing. advocate's `_COMMON_OUTPUT_FORMAT` tail is **dropped**: it demands a JSON findings array, and claudebox's output channel is `gh pr comment`. Its "do not manufacture findings, silence from you is a strong signal" rule is **kept**, in `personas/_shared.md`, because it is what lets a persona correctly say nothing.

`resolve_personas` fills `PERSONAS_LIST`, `PERSONA_LABEL` and `PERSONA_PROMPT` once at startup, so a pass is a lookup rather than three file reads, and a broken definition fails at startup rather than mid-review. An unresolvable selector is a hard error: a typo that silently narrowed the review to one persona would read as a working run.

`--append-system-prompt` carries the persona, and it is re-passed on **every** invocation. That is not defensive: measured 2026-08-21, the flag does not survive `--resume`. It also means an operator-supplied `REVIEW_PROMPT` still reaches Claude verbatim, since the persona never touches the task prompt.

Personas are blind to each other on purpose. `_shared.md` tells them explicitly not to defer to another persona's comments, which is the opposite of what a noise-reduction instinct would write: advocate runs its personas in parallel and blind, and that blindness is what makes six perspectives worth more than one. Reconciliation belongs to a separate pass (phase 2), not inside a persona.

`PR_SESSION`/`PR_PASSES` are keyed `"$pr:$persona"`, so `MAX_PASSES_PER_SESSION` rotates per pair.

**Usage limits are a first-class failure.** `is_usage_limit` inspects claude's stderr; a match keeps the pair's session, breaks out of the cycle, and backs off `LIMIT_BACKOFF_SECONDS`. Without that, a limit makes the next cycle re-read every PR and re-post findings already posted, which spends more of the exhausted resource. The classifier matches provider error text, an upstream surface that can change, so a miss deliberately degrades to the ordinary drop-the-session path rather than to a crash. `run_pass` recovers the session id **before** checking the exit code, because a pass that started a session and then hit a limit still has a resumable one.
```

In "Gotchas when editing", add:

```markdown
- Persona text goes in `--append-system-prompt`, never appended to `REVIEW_PROMPT`: the verbatim-operator-prompt guarantee depends on that separation. And it must be re-passed on resumed passes, because the flag does not survive `--resume`.
- `test-providers.sh` pins `PERSONAS=red_team` in its baseline so each case still produces exactly one `claude` invocation; its stub overwrites a single dump file. Multi-persona assertions belong in `test-personas.sh`, which captures per invocation and runs two cycles.
- A cycle is (PRs x personas) sequential sessions. Concurrency is available (blind personas are unordered, and the clone is only read) and declined, because it multiplies instantaneous usage-limit pressure.
```

- [ ] **Step 5: .env.example**

Insert before the `REVIEW_PROMPT` block:

```bash
# Which adversarial personas review each PR. Comma list, or 'all'. Order is
# honoured; an unknown name is a startup error. Available: red_team, adversarial,
# sage, sme, user, good_friend. Default is the four code-facing ones -- user and
# good_friend were written against designs rather than diffs.
# Each persona gets its own session per PR, so a cycle is (PRs x personas)
# sequential reviews and REVIEW_INTERVAL_SECONDS is the gap after all of them.
# PERSONAS=red_team,adversarial,sme,sage

# Where persona definitions are read from. Only worth setting to override the
# set shipped in the image with your own, mounted read-only.
# PERSONA_DIR=/opt/claudebox/personas

# How long to wait after a review pass fails on a usage or rate limit, instead of
# REVIEW_INTERVAL_SECONDS. The pass keeps its session and resumes on the next
# cycle, so this is a pause rather than a retry.
# LIMIT_BACKOFF_SECONDS=1800
```

- [ ] **Step 6: HISTORY.md**

Add at the top, under `# History`:

```markdown
## 0.0.6 - 2026-08-21

* Reviews now run as **adversarial personas** rather than as one generalist reviewer, borrowed from [advocate](https://github.com/jmcentire/advocate) — Red Team (survive assault), Adversarial (hold under challenge), Sage (simplify), Subject Matter Expert (peer sign-off), and opt-in User (navigate it cold) and Good Friend (the 3am test). advocate itself cannot be used here: it calls provider APIs directly, so it cannot run against a fixed-price plan, which is the whole reason claudebox drives the CLI. Definitions ship as files (`personas/*.md`, `PERSONA_DIR`) imported by a committed script, with advocate's JSON output contract stripped (claudebox posts `gh` comments) and its "silence from you is a strong signal" rule kept. `--persona` / `PERSONAS` selects the set; an unknown name is a startup error, because a typo that silently narrowed the review to one persona would read as a working run.
* One session per PR **per persona** (`PR_SESSION` keyed `pr:persona`, so `MAX_PASSES_PER_SESSION` rotates per pair). The persona travels in `--append-system-prompt`, which keeps an operator-supplied `REVIEW_PROMPT` verbatim, and is re-passed on every single pass: measured, the flag does **not** survive `--resume`, so passing it once would make cycle one adversarial and every cycle after it the old generalist wearing a persona's name in the log.
* Personas are blind to each other, deliberately. Nothing tells a persona to skip what another already raised: that anchors it to a review it did not do, and avoiding that flavour of group-think is why this tool exists. Two angles of attack hitting the same thing is a signal it deserves two comments. Comments are signed `-claudebox (<Persona>)`.
* Usage and rate limits are now a distinct failure. A pass that hits one keeps its session, ends the cycle instead of walking the remaining personas into the same wall, and waits `LIMIT_BACKOFF_SECONDS` (default 1800). The old path dropped the session, which under a limit meant the next cycle re-read every PR and re-posted findings already posted — spending more of the allowance that had just run out. Multiplying sessions per cycle by the persona count is what turned that from a theoretical shape into a routine one.
* New suite `test-personas.sh`. It runs **two** cycles where `test-providers.sh` runs one, because a single cycle produces no resumed invocation and the resumed invocation is where the most important assertion lives. `test-providers.sh` pins one persona in its baseline so its 70 cases keep asserting exactly one invocation each.
```

- [ ] **Step 7: Verify and commit**

```bash
bash -n entrypoint.sh && bash -n claudebox.sh
./test-providers.sh && ./test-personas.sh && ./test-shim.sh
grep -n "PERSONAS" README.md .env.example CLAUDE.md | head
```

Expected: all three suites green, and the variable documented in all three places.

```bash
git add README.md CLAUDE.md .env.example HISTORY.md
git commit -m "docs(personas): document the persona surface, cadence cost and limit backoff

Says plainly that a cycle is now (PRs x personas) sequential sessions and that
REVIEW_INTERVAL_SECONDS is the gap after it, because an operator reading the
interval as a period would conclude the loop is hung. Records the two
non-obvious invariants: the persona goes in the system prompt and must be
re-passed on resume, and personas are blind to each other on purpose."
```

---

## Before this runs unattended

The suites prove the wiring matches intent and nothing more. Do one live pass first:

```bash
./claudebox.sh build
./claudebox.sh test --repo /path/to/a/repo --prs <a real PR> --persona red_team
```

Watch for the persona's voice in the posted comment and for the signature naming it. Then repeat with the default four and check that the comments read as four different reviewers rather than four copies of one.

## Phase 1 exit criteria

- [ ] `bash -n` clean on `entrypoint.sh` and `claudebox.sh`
- [ ] `./test-providers.sh`, `./test-personas.sh`, `./test-shim.sh` all green
- [ ] `--persona` documented in `./claudebox.sh --help`, `README.md`, `.env.example`
- [ ] An unknown persona name refuses at startup, naming the available set
- [ ] A resumed pass demonstrably still carries its persona (asserted, not assumed)
- [ ] A live `claudebox.sh test` posted at least one persona-signed comment on a real PR

## Deferred to later phases

Phase 2: the aggregation pass over what the personas said, the marker-based create-or-edit comment helper it shares with the coverage comment, the coverage comment itself, and the persisted `(pr, persona)` session map. When phase 2 lands, `test-providers.sh`'s baseline pin needs `AGGREGATE=0` alongside `PERSONAS=red_team`, or every case there gains a second `claude` invocation and its single-dump stub starts reporting the aggregation pass instead of the review.

Phase 3: the per-persona model map, both sources (`.claudebox/personas.yml` read from the default-branch clone only, plus `PERSONA_MODELS` which overrides it per persona), with startup validation against `^[A-Za-z0-9._:@/-]{1,200}$`. That is also where `run_pass` starts applying a per-invocation model to `--model` and to every model-tier env var, which the `ENV:1:` expectations in `test-personas.sh` currently pin to the single global `REVIEW_MODEL`.
