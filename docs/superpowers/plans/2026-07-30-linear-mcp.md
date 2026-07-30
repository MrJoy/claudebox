# Linear MCP Passthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let claudebox optionally reach Linear — so the reviewer can compare a PR against the ticket it claims to implement — by setting one env var, `LINEAR_API_KEY`.

**Architecture:** `entrypoint.sh` (the in-container supervisor) gains two small pure functions: one writes a generated MCP config JSON containing Linear's HTTP endpoint plus an `Authorization: Bearer <key>` header, the other emits a prompt stanza telling the reviewer to consult the ticket. Both are no-ops when `LINEAR_API_KEY` is unset. A single `CLAUDE_MCP_ARGS` array is spliced into both `claude -p` invocations inside `run_pass`, and always carries `--strict-mcp-config` so a repo under review cannot inject MCP servers into a `--dangerously-skip-permissions` session. `claudebox.sh` is untouched — credentials already reach the container through `--env-file`.

**Tech Stack:** Bash (modern bash 4+ inside the image) and `jq` (already present and used by `format_stream`). Claude Code CLI flags `--mcp-config <file>` and `--strict-mcp-config` (verified present in the installed CLI). No unit-test runner exists in this repo; verification is `bash -n` plus `awk`-extract + `eval` unit tests of the entrypoint's pure functions, with a stubbed `claude` on `PATH` for the flag-wiring test.

## Global Constraints

- The env var is exactly `LINEAR_API_KEY`, optional. Unset ⇒ no MCP config file is written, `--mcp-config` is not passed, and no prompt stanza is appended; the only behavioral delta from today is `--strict-mcp-config`.
- Generated config path is exactly `$HOME/mcp.json`, written with mode `600` (via `umask 077` in a subshell). The key must never be logged.
- Linear MCP endpoint is exactly `https://mcp.linear.app/mcp`, transport `"type": "http"`, auth header `Authorization: Bearer <LINEAR_API_KEY>`. Server key in the config is `linear`.
- Build the JSON with `jq -n --arg`, never string interpolation — a key containing a quote or backslash must still produce valid JSON.
- `--strict-mcp-config` is passed on **every** `claude -p` call, Linear or not.
- The prompt stanza is appended to `DEFAULT_PROMPT` / `DEFAULT_FOLLOWUP` **only**, before the `${REVIEW_PROMPT:-…}` / `${FOLLOWUP_PROMPT:-…}` fallbacks resolve. An operator-supplied `REVIEW_PROMPT`/`FOLLOWUP_PROMPT` must reach Claude verbatim.
- Docs must state that a **read-only** Linear key is expected: in YOLO mode a write-capable key lets the unattended reviewer mutate tickets. This is the analogue of the privilege-minimized `GITHUB_TOKEN` and, like it, cannot be verified from inside the container.
- Do NOT change: PR enumeration/selection, session handling, provider/backend selection, hardening checks, the working-clone strategy, `--export-sessions`, or `claudebox.sh`.
- Run `bash -n entrypoint.sh` in every task that touches it.

**Test scaffolding used by Task 1 (paths verbatim):**

```
CB=/Users/jonathonfrisby/mrjoy/claudebox
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/dc8eeddd-44b5-4023-97a1-e554172ea486/scratchpad
```

`load_fn` — extracts one multi-line function's source from `entrypoint.sh` so a test can `eval` and call the REAL code (functions under test are written `name() {` … lone `}`):

```bash
load_fn() { awk "/^$1\\(\\) \\{/,/^\\}/" "$CB/entrypoint.sh"; }
```

---

### Task 1: entrypoint — Linear MCP config, flags, and prompt stanza

**Files:**

- Modify: `entrypoint.sh` — add `linear_stanza` + `write_mcp_config` after the `check_resource_limit` helper block (insert before the `# Unprivileged user.` comment at `entrypoint.sh:119`); restructure the prompt defaults block (`entrypoint.sh:171-180`); add the MCP-config block after the shared model-env block (after `entrypoint.sh:286`, before `# --- Prepare a writable working copy` at `:288`); splice `CLAUDE_MCP_ARGS` into both `claude -p` calls in `run_pass` (`entrypoint.sh:365-373`).
- Test: none checked in (no test runner in this repo) — the steps below run throwaway harnesses under `$SB`.

**Interfaces:**

- Produces: `linear_stanza` — echoes the Linear review stanza (with a single leading space) when `LINEAR_API_KEY` is non-empty, echoes nothing otherwise; `write_mcp_config PATH` — writes the generated MCP JSON to `PATH` with mode 600 and returns 0 when `LINEAR_API_KEY` is non-empty, writes nothing and returns 1 otherwise. Global array `CLAUDE_MCP_ARGS` — the MCP flags every `claude -p` call passes.
- Consumes: existing `log`, `$HOME`, `jq`, and (in `run_pass`) `$REVIEW_MODEL`, `format_stream`.

- [ ] **Step 1: Add the two functions.** Insert this block into `entrypoint.sh` immediately after the closing `}` of `check_resource_limit` and the blank line following it, directly before the `# Unprivileged user.` comment (currently `entrypoint.sh:119`):

```bash
# --- Optional Linear context ------------------------------------------------
# LINEAR_API_KEY (optional) lets the reviewer read the Linear ticket a PR claims
# to implement. Linear's MCP server accepts an API key passed straight through as
# `Authorization: Bearer <key>` (https://linear.app/docs/mcp) instead of the
# interactive OAuth flow, so the unattended loop stays headless. Use a READ-ONLY
# key: this loop runs with --dangerously-skip-permissions, so a write-capable key
# would let it mutate your tickets. Same trust model as GITHUB_TOKEN — the key's
# scope can't be checked from in here, so it's on the operator.

# Echo the review-prompt stanza that puts the Linear tools to work, or nothing
# when Linear isn't configured. Leading space: it's appended to a prompt.
linear_stanza() {
  [ -n "${LINEAR_API_KEY:-}" ] || return 0
  printf '%s' " If the PR title, body, or branch name references a Linear ticket, look that ticket up with the Linear MCP tools and read both its description and its comments — comments often carry later feedback, scope changes, and revised requirements that the description doesn't. Judge the change against what the ticket actually asks for, and raise any divergence from its stated requirements or acceptance criteria as a finding like any other. If no ticket is referenced, or you can't resolve the reference, review the code as usual — a missing ticket is not itself a finding."
}

# Write the MCP server config to $1 and return 0, or return 1 when there's
# nothing to configure. jq --arg does the JSON escaping so a key containing a
# quote or backslash can't produce a broken file. umask in a subshell makes the
# file 600 at creation, so the key is never briefly world-readable.
write_mcp_config() {
  [ -n "${LINEAR_API_KEY:-}" ] || return 1
  ( umask 077
    jq -n --arg key "$LINEAR_API_KEY" '{
      mcpServers: {
        linear: {
          type: "http",
          url: "https://mcp.linear.app/mcp",
          headers: { Authorization: ("Bearer " + $key) }
        }
      }
    }' >"$1" )
}
```

- [ ] **Step 2: Write a failing test for both functions.** Save as `$SB/test-linear.sh`:

```bash
#!/usr/bin/env bash
set -uo pipefail
CB=/Users/jonathonfrisby/mrjoy/claudebox
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/dc8eeddd-44b5-4023-97a1-e554172ea486/scratchpad
load_fn() { awk "/^$1\\(\\) \\{/,/^\\}/" "$CB/entrypoint.sh"; }
eval "$(load_fn linear_stanza)"
eval "$(load_fn write_mcp_config)"
fails=0
ok()   { printf 'ok   %s\n' "$1"; }
bad()  { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2' want '$3')"; fi; }

out="$SB/mcp-test.json"

# 1. No key: no stanza, no file, return 1.
unset LINEAR_API_KEY
rm -f "$out"
check "stanza empty without key" "$(linear_stanza)" ""
write_mcp_config "$out" && bad "write_mcp_config returned 0 without a key" || ok "write_mcp_config returns 1 without a key"
[ -e "$out" ] && bad "wrote a config file without a key" || ok "no config file without a key"

# 2. With a key: stanza mentions comments, config is valid and complete.
export LINEAR_API_KEY='lin_api_test123'
case "$(linear_stanza)" in
  ' If the PR title'*'its comments'*) ok "stanza present, leading space, mentions comments" ;;
  *) bad "stanza wrong: $(linear_stanza)" ;;
esac
write_mcp_config "$out" && ok "write_mcp_config returns 0 with a key" || bad "write_mcp_config returned non-zero with a key"
check "transport"  "$(jq -r '.mcpServers.linear.type' "$out")" "http"
check "url"        "$(jq -r '.mcpServers.linear.url' "$out")" "https://mcp.linear.app/mcp"
check "auth"       "$(jq -r '.mcpServers.linear.headers.Authorization' "$out")" "Bearer lin_api_test123"
check "mode 600"   "$(stat -c %a "$out" 2>/dev/null || stat -f %Lp "$out")" "600"

# 3. A key with JSON metacharacters still yields valid JSON.
export LINEAR_API_KEY='we"ird\key'
write_mcp_config "$out"
jq -e . "$out" >/dev/null 2>&1 && ok "hostile key yields valid JSON" || bad "hostile key broke the JSON"
check "hostile key round-trips" "$(jq -r '.mcpServers.linear.headers.Authorization' "$out")" 'Bearer we"ird\key'

rm -f "$out"
printf '\n%s\n' "$([ "$fails" -eq 0 ] && echo 'ALL PASS' || echo "$fails FAILURE(S)")"
[ "$fails" -eq 0 ]
```

- [ ] **Step 3: Run the test.** Run: `bash $SB/test-linear.sh`
Expected: `ALL PASS`. (If Step 1 was skipped it fails at `eval` with an empty function body — that is the failing state this step guards.)

- [ ] **Step 4: Restructure the prompt defaults block.** In `entrypoint.sh`, replace lines 171-180 (the comment starting `# Prompts are PR-scoped:` through `FOLLOWUP_PROMPT="${FOLLOWUP_PROMPT:-$DEFAULT_FOLLOWUP}"`) with this — both defaults are now defined before either fallback resolves, so the stanza can be appended to the defaults without ever touching an operator's override:

```bash
# Prompts are PR-scoped: the harness runs one session per PR and substitutes the
# {{PR}} token with that PR's number. REVIEW_PROMPT starts a PR's session;
# FOLLOWUP_PROMPT is used when resuming it on a later cycle. Custom overrides use
# the same {{PR}} token.
DEFAULT_PROMPT="Perform a thorough review of pull request #{{PR}} in this repository. Inspect it with \`gh pr view {{PR}}\` and \`gh pr diff {{PR}}\`, and be sure you're looking at the most recent commit on its branch. Pay particular attention to test quality/robustness, security, correctness, and architectural coherence/consistency, and whether the approach the PR takes is prudent and robust in light of the issue it addresses. Post findings as comments on the PR, one comment per finding. Sign your comments with '-claudebox'."
# Prompt used when RESUMING a PR's session (it already holds context from prior
# passes on that PR, so this nudges a re-check rather than re-introducing the task).
DEFAULT_FOLLOWUP="I've fetched the latest refs. Re-check pull request #{{PR}} for new commits or changes since your last review of it. Apply the same review standard, and only post findings you haven't already raised on this PR. Be sure you're looking at the most recent commit on its branch. Sign your comments with '-claudebox'."
# Linear context is added to the DEFAULTS only: an operator who supplied their own
# prompt gets exactly that prompt, unedited. No-op when LINEAR_API_KEY is unset.
_linear_stanza="$(linear_stanza)"
DEFAULT_PROMPT="${DEFAULT_PROMPT}${_linear_stanza}"
DEFAULT_FOLLOWUP="${DEFAULT_FOLLOWUP}${_linear_stanza}"
unset _linear_stanza
REVIEW_PROMPT="${REVIEW_PROMPT:-$DEFAULT_PROMPT}"
FOLLOWUP_PROMPT="${FOLLOWUP_PROMPT:-$DEFAULT_FOLLOWUP}"
```

- [ ] **Step 5: Add the MCP-config block.** Insert after the shared model-env block — after `export ANTHROPIC_SMALL_FAST_MODEL="$REVIEW_MODEL"` (`entrypoint.sh:286`) and its blank line, directly before `# --- Prepare a writable working copy` — this block:

```bash
# --- MCP servers -----------------------------------------------------------
# --strict-mcp-config is passed ALWAYS: /repo is untrusted input, and without it
# a repo under review that ships its own .mcp.json could get MCP servers of its
# choosing loaded into a --dangerously-skip-permissions session. Strict mode
# means the reviewer loads only what we generate here, or nothing at all.
CLAUDE_MCP_ARGS=(--strict-mcp-config)
MCP_CONFIG_FILE="$HOME/mcp.json"
rm -f "$MCP_CONFIG_FILE"
if write_mcp_config "$MCP_CONFIG_FILE"; then
  CLAUDE_MCP_ARGS+=(--mcp-config "$MCP_CONFIG_FILE")
  log "Linear MCP enabled (expects a READ-ONLY Linear API key)."
fi
```

- [ ] **Step 6: Splice the flags into both `claude -p` calls.** In `run_pass`, replace the `if [ -n "$sid" ]; then … fi` invocation pair (`entrypoint.sh:365-373`) with:

```bash
  if [ -n "$sid" ]; then
    claude -p --resume "$sid" --output-format stream-json --verbose \
      --dangerously-skip-permissions --model "$REVIEW_MODEL" \
      "${CLAUDE_MCP_ARGS[@]}" "$prompt" \
      2>"$errfile" | stdbuf -oL tee "$rawfile" | format_stream
  else
    claude -p --output-format stream-json --verbose \
      --dangerously-skip-permissions --model "$REVIEW_MODEL" \
      "${CLAUDE_MCP_ARGS[@]}" "$prompt" \
      2>"$errfile" | stdbuf -oL tee "$rawfile" | format_stream
  fi
```

- [ ] **Step 7: Syntax-check.** Run: `bash -n /Users/jonathonfrisby/mrjoy/claudebox/entrypoint.sh`
Expected: no output, exit 0.

- [ ] **Step 8: Write a failing test for the flag wiring and prompt appending.** This test stubs `claude` on `PATH` so it can assert what the real `run_pass` passes, for both the new-session and `--resume` paths. Save as `$SB/test-wiring.sh`:

```bash
#!/usr/bin/env bash
set -uo pipefail
CB=/Users/jonathonfrisby/mrjoy/claudebox
SB=/private/tmp/claude-501/-Users-jonathonfrisby-mrjoy-claudebox/dc8eeddd-44b5-4023-97a1-e554172ea486/scratchpad
load_fn() { awk "/^$1\\(\\) \\{/,/^\\}/" "$CB/entrypoint.sh"; }
fails=0
ok()  { printf 'ok   %s\n' "$1"; }
bad() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }

# Stub claude: record argv, emit one stream-json event so format_stream is exercised.
mkdir -p "$SB/bin"
cat >"$SB/bin/claude" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$CLAUDE_ARGS_FILE"
printf '%s\n' '{"type":"system","subtype":"init","session_id":"stub-sid"}'
STUB
chmod +x "$SB/bin/claude"
# run_pass pipes through `stdbuf`, which is GNU-only and absent on macOS (these
# tests run on the host). Shim it to a pass-through so the pipeline works here.
command -v stdbuf >/dev/null || { cat >"$SB/bin/stdbuf" <<'SHIM'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do case "$1" in -*) shift ;; *) break ;; esac; done
exec "$@"
SHIM
chmod +x "$SB/bin/stdbuf"; }
PATH="$SB/bin:$PATH"
export CLAUDE_ARGS_FILE="$SB/claude-args.txt"

REVIEW_MODEL=stub-model
eval "$(load_fn format_stream)"
eval "$(load_fn run_pass)"
log() { :; }
CLAUDE_MCP_ARGS=(--strict-mcp-config --mcp-config /home/reviewer/mcp.json)

check_args() {  # $1=label, then the argv strings that must all be present
  local label="$1"; shift
  local missing=""
  for want in "$@"; do
    grep -qxF -- "$want" "$CLAUDE_ARGS_FILE" || missing="$missing '$want'"
  done
  [ -z "$missing" ] && ok "$label" || bad "$label — missing:$missing"
}

run_pass "PROMPT-NEW" "" >/dev/null
check_args "new session passes MCP flags + prompt" --strict-mcp-config --mcp-config /home/reviewer/mcp.json PROMPT-NEW
grep -qxF -- "--resume" "$CLAUDE_ARGS_FILE" && bad "new session should not pass --resume" || ok "new session has no --resume"

run_pass "PROMPT-RESUME" "abc-123" >/dev/null
check_args "resume passes MCP flags + prompt" --strict-mcp-config --mcp-config /home/reviewer/mcp.json PROMPT-RESUME --resume abc-123

# Prompt stanza: appended to the default, never to an operator override.
prompt_for() {  # $1=LINEAR_API_KEY value ('' = unset), $2=REVIEW_PROMPT override ('' = none)
  ( if [ -n "$1" ]; then export LINEAR_API_KEY="$1"; else unset LINEAR_API_KEY; fi
    if [ -n "$2" ]; then export REVIEW_PROMPT="$2"; else unset REVIEW_PROMPT; fi
    eval "$(load_fn linear_stanza)"
    eval "$(awk '/^DEFAULT_PROMPT=/,/^FOLLOWUP_PROMPT=/' "$CB/entrypoint.sh")"
    printf '%s' "$REVIEW_PROMPT" )
}
case "$(prompt_for '' '')"            in *'Linear'*) bad "stanza leaked in with no key" ;; *) ok "no stanza without a key" ;; esac
case "$(prompt_for 'k' '')"           in *'Linear MCP tools'*) ok "stanza appended to the default" ;; *) bad "stanza missing from the default" ;; esac
check_override() { [ "$(prompt_for 'k' 'MY OWN PROMPT')" = "MY OWN PROMPT" ] && ok "override untouched" || bad "override was modified: $(prompt_for 'k' 'MY OWN PROMPT')"; }
check_override

printf '\n%s\n' "$([ "$fails" -eq 0 ] && echo 'ALL PASS' || echo "$fails FAILURE(S)")"
[ "$fails" -eq 0 ]
```

- [ ] **Step 9: Run both tests.** Run: `bash $SB/test-linear.sh && bash $SB/test-wiring.sh`
Expected: `ALL PASS` from each.

- [ ] **Step 10: Commit.**

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
git add entrypoint.sh
git commit -m "entrypoint: optional Linear MCP access via LINEAR_API_KEY

Generate an MCP config pointing at Linear's HTTP endpoint with the key as a
Bearer header, pass it to both claude -p call sites, and append a stanza to the
default prompts telling the reviewer to check the referenced ticket and its
comments. Always pass --strict-mcp-config so a repo under review can't inject
MCP servers into a YOLO-mode session."
```

---

### Task 2: docs — `.env.example`, README, CLAUDE.md, HISTORY.md

**Files:**

- Modify: `.env.example` — add a `LINEAR_API_KEY` block in the `--- Optional ---` section, after the `MAX_PASSES_PER_SESSION=0` entry (`.env.example:67`).
- Modify: `README.md` — add to the `Optional:` list under `## Configuration` (after the `MAX_PASSES_PER_SESSION` bullet, `README.md:251`); add a `### Linear ticket context` subsection after the `### PR selection` table's trailing paragraph (`README.md:266`).
- Modify: `CLAUDE.md` — extend the `## Configuration` optional list, and add a short paragraph to `## Architecture`.
- Modify: `HISTORY.md` — add two bullets to `## Unreleased`.

**Interfaces:**

- Consumes: `LINEAR_API_KEY`, `linear_stanza`, `write_mcp_config`, `CLAUDE_MCP_ARGS`, `$HOME/mcp.json`, `--strict-mcp-config` — all from Task 1. No new interfaces produced.

- [ ] **Step 1: `.env.example`.** Insert after the `MAX_PASSES_PER_SESSION=0` line and its following blank line:

```
# Optional Linear access. Set this and the reviewer can read the Linear ticket a
# PR references (description AND comments) and flag where the change diverges
# from what the ticket asked for. Linear's MCP server takes an API key directly,
# so no interactive login is needed: Settings -> Security & access -> Personal
# API keys (https://linear.app/settings/account/security).
# USE A READ-ONLY KEY. Give it the Read permission only. The reviewer runs with
# all permission prompts skipped, so a write-capable key would let it change your
# tickets — same reasoning as the read-mostly GITHUB_TOKEN.
# Leave unset to disable Linear entirely.
# LINEAR_API_KEY=

```

- [ ] **Step 2: README `Optional:` list.** Insert this bullet immediately after the `MAX_PASSES_PER_SESSION` bullet:

```markdown
- `LINEAR_API_KEY` (optional Linear ticket context; use a **read-only** key — see [Linear ticket context](#linear-ticket-context))
```

- [ ] **Step 3: README subsection.** Insert after the paragraph ending `…applies per PR.` that closes `### PR selection`:

```markdown
### Linear ticket context

Set `LINEAR_API_KEY` and the reviewer also reads the Linear ticket a PR references — its description *and* its comments, where later feedback and revised requirements usually live — and raises divergence from what the ticket asked for as a finding, alongside the usual code findings. Unset, nothing about the review changes.

Get a key from **Settings → Security & access → Personal API keys**. Linear's MCP server accepts an API key straight through as an `Authorization: Bearer` header ([Linear docs](https://linear.app/docs/mcp)), so there is no interactive OAuth step and the loop stays headless.

> **Use a read-only key.** Linear lets you restrict a personal API key to `Read`. The reviewer runs with `--dangerously-skip-permissions`, so a write-capable key would let an unattended session modify your tickets. Like `GITHUB_TOKEN`, the key's scope can't be inspected from inside the container — minimizing it is on you.

The entrypoint writes the key into a generated MCP config at `$HOME/mcp.json` (mode `600`) and passes it to Claude Code with `--mcp-config`. Every review pass also runs with `--strict-mcp-config`, whether or not Linear is configured: `/repo` is untrusted input, and strict mode means a repository that ships its own `.mcp.json` can't get MCP servers of its choosing loaded into a permission-skipped session.
```

- [ ] **Step 4: CLAUDE.md.** In `## Configuration`, replace the sentence beginning `Optional: \`REVIEW_MODEL\`` so the list also names the new var:

Find: `Optional: \`REVIEW_MODEL\` (provider-specific default, but required for \`custom\`), \`REVIEW_INTERVAL_SECONDS\`, \`MAX_PASSES_PER_SESSION\`, \`ALLOW_UNHARDENED\`, and the prompt overrides \`REVIEW_PROMPT\` (new session) / \`FOLLOWUP_PROMPT\` (resumed passes). Default prompts live in \`entrypoint.sh\`.`

Replace with: `Optional: \`REVIEW_MODEL\` (provider-specific default, but required for \`custom\`), \`REVIEW_INTERVAL_SECONDS\`, \`MAX_PASSES_PER_SESSION\`, \`ALLOW_UNHARDENED\`, \`LINEAR_API_KEY\` (see "Optional Linear context" above), and the prompt overrides \`REVIEW_PROMPT\` (new session) / \`FOLLOWUP_PROMPT\` (resumed passes). Default prompts live in \`entrypoint.sh\`.`

- [ ] **Step 5: CLAUDE.md architecture paragraph.** Insert as a new subsection at the end of `## Architecture`, immediately before `## Configuration`:

```markdown
### Optional Linear context

`LINEAR_API_KEY` (optional) gives the reviewer read access to the Linear ticket a PR references. `write_mcp_config` generates `$HOME/mcp.json` (mode 600, built with `jq --arg` so a hostile key can't break the JSON) pointing at `https://mcp.linear.app/mcp` with the key as an `Authorization: Bearer` header — Linear accepts an API key in place of interactive OAuth, which is what keeps the loop headless. `linear_stanza` appends the "check the ticket and its comments" instruction to `DEFAULT_PROMPT`/`DEFAULT_FOLLOWUP` **only**, so an operator-supplied `REVIEW_PROMPT`/`FOLLOWUP_PROMPT` reaches Claude verbatim. Docs tell operators to use a read-only key: in YOLO mode a write-capable key would let the unattended reviewer mutate tickets, and like `GITHUB_TOKEN` its scope can't be checked from inside.

`CLAUDE_MCP_ARGS` carries the MCP flags for both `claude -p` call sites and always includes **`--strict-mcp-config`**, Linear or not. That's load-bearing: `/repo` is untrusted, and without it a reviewed repo shipping a `.mcp.json` could get MCP servers of its choosing loaded into a `--dangerously-skip-permissions` session.
```

- [ ] **Step 6: HISTORY.md.** Add these two bullets to the end of the `## Unreleased` list:

```markdown
* Optional Linear ticket context: set `LINEAR_API_KEY` and the reviewer reads the Linear ticket a PR references — description and comments — and flags where the change diverges from what the ticket asked for. Use a read-only key; the reviewer runs with permissions skipped.
* Always run review passes with `--strict-mcp-config`, so a repository under review can't inject MCP servers of its own choosing into a permission-skipped session.
```

- [ ] **Step 7: Verify the docs agree with the code.** Run:

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
grep -c LINEAR_API_KEY .env.example README.md CLAUDE.md HISTORY.md entrypoint.sh
grep -n "strict-mcp-config" entrypoint.sh README.md CLAUDE.md HISTORY.md
```

Expected: every file reports at least 1 for `LINEAR_API_KEY` (`HISTORY.md` counts 1, `entrypoint.sh` ≥ 3); `--strict-mcp-config` appears in all four listed files. No file should mention a variable name the entrypoint doesn't read.

- [ ] **Step 8: Commit.**

```bash
cd /Users/jonathonfrisby/mrjoy/claudebox
git add .env.example README.md CLAUDE.md HISTORY.md
git commit -m "docs: LINEAR_API_KEY (read-only) and always-on --strict-mcp-config"
```

---

### Task 3: live verification against a real repo

Runs the real container end-to-end. Pick the target PR first — an open PR whose title, body, or branch references a Linear ticket:

```bash
gh pr list -R wandercom/wander --assignee MrJoy --state open --json number,title,headRefName
```

Use that number as `PRNUM` and that repo path (`~/wandercom/wander`) as `REPO` in the steps below.

**Files:** none modified. If a defect surfaces, fix it in `entrypoint.sh` and re-run Task 1 Step 9's tests before committing.

**Interfaces:** Consumes everything from Tasks 1-2.

- [ ] **Step 1: Rebuild.** `./claudebox.sh build` — required; `run` does not rebuild, so an old image would silently review with the previous entrypoint.

- [ ] **Step 2: Baseline, Linear disabled.** With `LINEAR_API_KEY` unset in the env file, run a single foreground pass:

```bash
./claudebox.sh test --repo ~/wandercom/wander --prs PRNUM 2>&1 | tee /tmp/cb-baseline.log
```

Expected: the review runs as before, and `/tmp/cb-baseline.log` contains no `Linear MCP enabled` line. (`test` uses `--rm`, so inspect the captured log rather than the container.)

- [ ] **Step 3: Enable Linear.** Put a **read-only** `LINEAR_API_KEY` in the env file, then:

```bash
./claudebox.sh test --repo ~/wandercom/wander --prs PRNUM 2>&1 | tee /tmp/cb-linear.log
```

Expected in the streamed log: a `Linear MCP enabled (expects a READ-ONLY Linear API key).` line at startup, and at least one `→ mcp__linear__…` tool call during the review (for a PR that references a ticket).

- [ ] **Step 4: Confirm the key doesn't leak into logs.** Substitute the actual key value for `THEKEY`:

```bash
grep -c THEKEY /tmp/cb-linear.log
```

Expected: `0`.

- [ ] **Step 5: Confirm the config file's permissions in a live container.** Start a detached run against the same repo (`./claudebox.sh run --repo ~/wandercom/wander --prs PRNUM`), then:

```bash
docker exec claudebox--wandercom--wander stat -c %a /home/reviewer/mcp.json
```

Expected: `600`.

- [ ] **Step 6: Commit nothing / or commit fixes.** No commit if all checks pass. If a fix was needed, re-run `bash $SB/test-linear.sh && bash $SB/test-wiring.sh && bash -n entrypoint.sh`, then commit the fix with a message naming the failing check.
