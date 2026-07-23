# Per-PR sessions + PR targeting — design

**Date:** 2026-07-23
**Scope:** `entrypoint.sh` (the in-container supervisor/loop), `claudebox.sh` (host
launcher — new selector flags + passthrough), and docs (`README.md`, `.env.example`).
**Goal:** Make claudebox suitable for team use by (1) running a **separate Claude Code
session per PR** — the harness enumerates candidate PRs and iterates — and (2) adding
CLI/env-file options to **target which PRs** are reviewed.

`entrypoint.sh` runs inside the image on **modern bash** (associative arrays are fine).
`claudebox.sh` runs on the **host** and must stay **bash-3.2-safe** (it only passes new
flags through; it holds no PR→session map).

## Motivation

Today a single continuous Claude session reviews *all* PRs and uses `--resume` to remember
what it already flagged. For a team, control should move to the harness: it selects the
PRs of interest and gives each its own session, so reviews are isolated per PR and the
target set is configurable (all open, a user's assigned PRs, an explicit search, or a
specific list).

## Feature 1 — PR selection (four mutually-exclusive selectors)

Exactly one selector must be provided. **Zero selectors → hard error. More than one →
hard error.** The authoritative check lives in `entrypoint.sh` (so it also governs
env-file-only users); the launcher additionally rejects two selector *flags* early for a
friendlier message.

| Launcher flag       | Env var                | Enumeration                                             |
|---------------------|------------------------|--------------------------------------------------------|
| `--all`             | `PR_ALL=1`             | `gh pr list --state open`                              |
| `--assignee LOGIN`  | `PR_ASSIGNEE=login`    | `gh pr list --state open --assignee LOGIN`             |
| `--prs 12,15,20`    | `PR_IDS=12,15,20`      | the given numbers, verbatim (validated as integers)    |
| `--search "…"`      | `PR_SEARCH="is:open …"`| `gh pr list --search "…"`                              |

Details:
- Enumeration commands run with `-R "$GITHUB_REPOSITORY" --json number --jq '.[].number'`
  and `--limit 100`. `--search` controls its own state (the user includes `is:open` if
  desired); the others force `--state open`.
- `PR_IDS` is split on commas and/or whitespace; each element must match `^[0-9]+$` or the
  entrypoint dies. IDs are used as given (a closed/merged/nonexistent number is passed to
  Claude as-is; a bad number simply produces a failed pass for that PR, not a crash).
- **"Provided" semantics:** `PR_ASSIGNEE`/`PR_IDS`/`PR_SEARCH` count as provided when
  non-empty; `PR_ALL` counts as provided when set to a truthy value (`1`/`true`/`yes`,
  case-insensitive). Empty/unset/`0`/`false` do not count.
- The launcher maps each given flag to `-e VAR=value` on `docker run`/`test`. A CLI flag
  therefore overrides an env-file value of the same var. If the launcher is given two
  selector flags it errors before running docker.

## Feature 2 — Per-PR sessions (the loop, inverted)

The single `SESSION_ID` global and its rotation are replaced by per-PR state held in two
associative arrays in `entrypoint.sh`:

- `PR_SESSION[<num>]` → the Claude Code session id for that PR (unset until first review)
- `PR_PASSES[<num>]`  → successful-pass count for that PR (for `MAX_PASSES_PER_SESSION`)

Each cycle:

```
git fetch --all --prune
enumerate candidate PRs  (Feature 1)
if none: log and skip to sleep
for each PR number (sequentially):
    sid = PR_SESSION[num]
    if sid empty:  template = REVIEW_PROMPT   (start a NEW session)
    else:          template = FOLLOWUP_PROMPT (--resume sid)
    prompt = substitute {{PR}} -> num in template
    run one pass:
        on success: PR_SESSION[num] = recovered session id
                    PR_PASSES[num] += 1
                    if MAX_PASSES_PER_SESSION>0 and PR_PASSES[num] >= cap:
                        rotate — unset PR_SESSION[num], reset PR_PASSES[num]=0
        on failure: unset PR_SESSION[num]  (fresh session next cycle)
sleep REVIEW_INTERVAL_SECONDS
```

- PRs are processed **sequentially** — they share the single writable working clone, so
  concurrent sessions would fight over git state.
- `--all`/`--assignee`/`--search` re-enumerate every cycle, so newly-matching PRs get
  picked up automatically; `--prs` is a fixed set.
- `MAX_PASSES_PER_SESSION` now applies **per PR** (bounds each PR's session context growth
  exactly as it bounded the single session before).
- **Durability:** the map is in-memory only. A container restart loses it, so each PR may
  be re-reviewed once (possibly re-commenting once) — the same accepted trade-off the
  single-session design already had on restart. Persisting the map to disk is explicitly
  out of scope (YAGNI) unless duplicate-after-restart comments prove painful in practice.

### `run_pass` refactor

`run_pass` currently mutates the global `SESSION_ID`. It is refactored to take the
current session id as an argument and expose the recovered id via a well-defined output
(a dedicated global, e.g. `RUN_PASS_SESSION_ID`, set on success), so the loop can store it
into `PR_SESSION[num]`. Streaming/format/exit-code handling (the `PIPESTATUS[0]` trick,
the tee-to-tempfile for session-id recovery) is otherwise unchanged.

## Feature 3 — Prompt templating

Default and custom prompts target one PR via the `{{PR}}` token, substituted with the PR
number using bash parameter expansion: `prompt="${template//\{\{PR\}\}/$num}"`.

- `REVIEW_PROMPT` (new session) and `FOLLOWUP_PROMPT` (resumed session) keep their names
  and env-override behavior; only their default text changes to be per-PR.
- If the resolved template (default or custom) does **not** contain `{{PR}}`, the harness
  logs a **warning** once (the PR won't be named in the prompt) but proceeds.

Default text:

- **REVIEW_PROMPT:** "Perform a thorough review of pull request #{{PR}} in this
  repository. Inspect it with `gh pr view {{PR}}` and `gh pr diff {{PR}}`, and be sure
  you're looking at the most recent commit on its branch. Pay particular attention to test
  quality/robustness, security, correctness, and architectural coherence/consistency, and
  whether the approach the PR takes is prudent and robust in light of the issue it
  addresses. Post findings as comments on the PR, one comment per finding. Sign your
  comments with '-claudebox'."
- **FOLLOWUP_PROMPT:** "I've fetched the latest refs. Re-check pull request #{{PR}} for new
  commits or changes since your last review of it. Apply the same review standard, and only
  post findings you haven't already raised on this PR. Be sure you're looking at the most
  recent commit on its branch. Sign your comments with '-claudebox'."

## Configuration summary

Always required: `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, **and exactly one PR selector**
(`PR_ALL` / `PR_ASSIGNEE` / `PR_IDS` / `PR_SEARCH`). Provider config unchanged. Optional,
unchanged in meaning: `REVIEW_MODEL`, `REVIEW_INTERVAL_SECONDS`, `MAX_PASSES_PER_SESSION`
(now per-PR), `REVIEW_PROMPT`/`FOLLOWUP_PROMPT` (now `{{PR}}`-templated), `ALLOW_UNHARDENED`.

## Testing approach

No unit-test runner exists. Verify with:
- `bash -n entrypoint.sh` and `bash -n claudebox.sh`.
- Launcher `--dry-run` assertions that each selector flag becomes the right `-e VAR=…` and
  that two selector flags error.
- For entrypoint logic that can be exercised without Docker/Claude (selector validation,
  `{{PR}}` substitution, PR_IDS parsing), extract or invoke the relevant functions under
  `ALLOW_UNHARDENED=1` with `gh`/`claude` stubbed on `PATH`, asserting on log output and
  the enumerated PR list. The plan will specify exact stubs and assertions.

## Out of scope

- Persisting the PR→session map across restarts.
- Parallel per-PR review.
- Changes to provider selection, hardening, the working-clone strategy, or
  `--export-sessions`.
- Per-PR container naming (the launcher's container name is still per-repo).
