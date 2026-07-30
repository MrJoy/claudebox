# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Docker image that runs an unattended PR reviewer. It bundles the **Claude Code CLI** and the **GitHub CLI**, points Claude Code at a configurable model provider — **Ollama Cloud** by default (`glm-5.2:cloud`), or **Anthropic** or any **Anthropic-compatible** endpoint via the `PROVIDER` env var — and loops over a repo's open PRs in headless "YOLO" mode, posting one comment per finding. The premise: a different model reviewing than the one that wrote the code avoids group-think (which is why Ollama is the default and reviewing Claude-authored code with `PROVIDER=anthropic` re-introduces the group-think the tool exists to avoid).

There is no application code, build system, or test suite. The project is a handful of files: `Dockerfile`, `entrypoint.sh` (where essentially all the runtime logic lives), `claudebox.sh` (a host-side launcher wrapping `docker build`/`run` and the lifecycle commands), `README.md`, `.env.example`, and `HISTORY.md`.

## Commands

The `claudebox.sh` launcher wraps all of this (`build`, `run`, `test`, `logs`, `shell`, `stop`, `status`) and injects the required hardening flags; `./claudebox.sh --help` is the reference, and `--dry-run` prints the docker command any subcommand would run:

```bash
./claudebox.sh build
./claudebox.sh run --repo /path/to/your/repo    # detached + hardened
./claudebox.sh run --repo /path/to/your/repo --mount-claude   # reuse host `claude` login
./claudebox.sh logs                             # watch the live play-by-play
./claudebox.sh test --repo /path/to/your/repo   # one-off, foreground, --rm
```

The equivalent raw docker (what the launcher assembles):

```bash
docker build -t claudebox .
docker run -d --name claudebox --restart unless-stopped --env-file .env \
  -v /path/to/your/repo:/repo:ro \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --memory 4g \
  claudebox
docker logs -f claudebox
docker exec -it claudebox bash  # poke around inside (gh auth status, gh pr list, etc.)
```

There is no linter or test runner. Syntax-check the shell without Docker via `bash -n entrypoint.sh` / `bash -n claudebox.sh`. `claudebox.sh` runs on the **host**, where macOS ships **bash 3.2** — so keep it 3.2-safe (e.g. expand possibly-empty arrays as `${arr[@]+"${arr[@]}"}`, not `"${arr[@]}"`, which trips `set -u`). `entrypoint.sh` runs inside the image (modern bash).

## Architecture

The design is built entirely around one constraint: **the loop runs unattended in YOLO mode (`--dangerously-skip-permissions`), so it must not be able to cause damage.** Three layered safety boundaries, all of which must be preserved when editing:

1. **Unprivileged user** — `Dockerfile` creates and runs as `reviewer`. This is also load-bearing functionally: Claude Code *refuses* `--dangerously-skip-permissions` as root.
2. **Read-only source** — the user's repo is mounted at `/repo:ro`. `entrypoint.sh` makes a cheap **local clone** (`git clone --local --no-hardlinks`) into a writable working dir and only ever touches the clone. `--no-hardlinks` is mandatory: a bind mount is a different device, so the default hardlinking clone fails with "Invalid cross-device link". If no git repo is mounted, it falls back to a network clone of `GITHUB_REPOSITORY`.
3. **Privilege-minimized GitHub token** — read repo/PRs + write PR comments only; no push/merge/admin. This is the real safety boundary; the README stresses verifying it before running unattended.

`entrypoint.sh` enforces boundaries 1–2 at startup (a "Hardening checks" block): it `die`s if running as root, if `no-new-privileges` isn't set (`NoNewPrivs` in `/proc/self/status`), or if capabilities aren't all dropped (`CapBnd` non-zero). Missing `--pids-limit`/`--memory` only warn (resource bounds, not safety, and detection differs across cgroup v1/v2). `ALLOW_UNHARDENED=1` downgrades the hard failures to warnings for non-Docker runtimes or tests. The token (boundary 3) can't be introspected, so it isn't checked.

### Two pieces working together

- **`entrypoint.sh` is the supervisor.** It does auth setup (`gh`/`git`), wires the Claude-Code→provider env, prepares the working clone, then runs the review loop: `git fetch` → enumerate candidate PRs → review each PR sequentially → sleep. It controls cadence, PR selection, and crash-recovery; Claude reviews the one PR it's handed.

- **One Claude session per PR.** The harness enumerates candidate PRs from exactly one selector (`PR_ALL`/`PR_ASSIGNEE`/`PR_IDS`/`PR_SEARCH`; zero or multiple is a hard error) and reviews each in its own session, keyed in an in-memory `PR_SESSION` map. A PR's first review starts a new `claude -p` session (recovering its `session_id` from the `stream-json` output); later cycles `--resume` that PR's id so it won't re-raise findings on that PR. Prompts are `{{PR}}`-templated (`REVIEW_PROMPT` on start, `FOLLOWUP_PROMPT` on resume). A failed pass drops that PR's session id, so its next cycle starts fresh (and may re-comment once — accepted noise). `MAX_PASSES_PER_SESSION` optionally rotates a PR's session to bound context growth (per PR). The map is in-memory, so a container restart may re-review each PR once.

  Why not Claude Code's `/loop`? `/loop` needs a live interactive session; headless `-p` exits after each response. The shell loop + `--resume` gives the same continuous, context-retaining behavior while staying headless and crash-safe.

### Backend selection & the all-tiers-mapped-to-one-model trick

A `PROVIDER` env var (default `ollama`) selects the backend in a `case` block in `entrypoint.sh`; each arm validates that provider's credential and wires the Claude Code env:

- `ollama` — `ANTHROPIC_BASE_URL=https://ollama.com`, auth via `ANTHROPIC_AUTH_TOKEN=$OLLAMA_API_KEY` (**not** `ANTHROPIC_API_KEY`, which is blanked). Default `REVIEW_MODEL=glm-5.2:cloud`.
- `anthropic` — Anthropic's default endpoint (base URL left unset). Credential is resolved by falling through, first-available-wins: `ANTHROPIC_API_KEY` (x-api-key), else `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token` on the host), else a mounted `~/.claude/.credentials.json` (the creds `claude` uses outside the container); `die`s only if none exist. For the token/file paths the key vars are `unset` (not blanked) because an *empty* `ANTHROPIC_API_KEY` outranks the OAuth token in Claude Code's precedence and would shadow it. Default `REVIEW_MODEL=claude-opus-4-8`.
- `custom` — caller supplies `ANTHROPIC_BASE_URL`, `REVIEW_MODEL`, and exactly one of `ANTHROPIC_AUTH_TOKEN` (Bearer) or `ANTHROPIC_API_KEY` (x-api-key); the other is blanked. No model default.

Regardless of provider, a shared block then points **every** model env var (`ANTHROPIC_MODEL`, `..._DEFAULT_FABLE/OPUS/SONNET/HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`) at the single `$REVIEW_MODEL`. On non-Anthropic backends this is required — they have no Opus/Sonnet/Haiku models, so an un-overridden tier requested by a subagent or alias would error on an unknown model; on Anthropic it's a deliberate simplification (one model does everything). There is **no** fallback: a wrong model name is a hard error, never a silent switch to another model. `REVIEW_MODEL`'s default is provider-specific and resolved in the entrypoint, so it is intentionally **not** baked into the Dockerfile `ENV`.

### Log formatting

`format_stream()` in `entrypoint.sh` is a `jq` filter that pretty-prints Claude's `stream-json` (one JSON event per line) into readable log lines. The raw stream is also tee'd to a temp file purely to recover the session id (`PIPESTATUS[0]` reads Claude's exit code, not jq's/tee's).

### Optional Linear context

`LINEAR_API_KEY` (optional) gives the reviewer read access to the Linear ticket a PR references. `write_mcp_config` generates `$HOME/mcp.json` (mode 600, built with `jq --arg` so a hostile key can't break the JSON) pointing at `https://mcp.linear.app/mcp` with the key as an `Authorization: Bearer` header — Linear accepts an API key in place of interactive OAuth, which is what keeps the loop headless. `linear_stanza` appends the "check the ticket and its comments" instruction to `DEFAULT_PROMPT`/`DEFAULT_FOLLOWUP` **only**, so an operator-supplied `REVIEW_PROMPT`/`FOLLOWUP_PROMPT` reaches Claude verbatim. Docs tell operators to use a read-only key: in YOLO mode a write-capable key would let the unattended reviewer mutate tickets, and like `GITHUB_TOKEN` its scope can't be checked from inside.

`CLAUDE_MCP_ARGS` carries the MCP flags for both `claude -p` call sites and always includes **`--strict-mcp-config`**, Linear or not. That's load-bearing: `/repo` is untrusted, and without it a reviewed repo shipping a `.mcp.json` could get MCP servers of its choosing loaded into a `--dangerously-skip-permissions` session.

## Configuration

All config is via environment variables (`.env.example` documents them). Always required: `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, and exactly one PR selector (`PR_ALL`/`PR_ASSIGNEE`/`PR_IDS`/`PR_SEARCH`). Provider selection: `PROVIDER` (default `ollama`) plus that provider's credential — `OLLAMA_API_KEY` (ollama), `ANTHROPIC_API_KEY` (anthropic), or `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` (custom); see "Backend selection" above. Optional: `REVIEW_MODEL` (provider-specific default, but required for `custom`), `REVIEW_INTERVAL_SECONDS`, `MAX_PASSES_PER_SESSION`, `ALLOW_UNHARDENED`, `LINEAR_API_KEY` (see "Optional Linear context" above), the prompt overrides `REVIEW_PROMPT` (new session) / `FOLLOWUP_PROMPT` (resumed passes), and `REVIEW_PROMPT_SUFFIX` / `FOLLOWUP_PROMPT_SUFFIX` (append to whichever of those is in effect, default or override). Default prompts live in `entrypoint.sh`.

## Gotchas when editing

- Don't add `--read-only` to the container root fs: the loop must write its working clone under `$HOME`.
- Mount the **primary** repo, not a `git worktree` of it — a worktree keeps objects in its parent and is structurally unusable mounted alone.
- Model versions move fast; the `:cloud` suffix is stable but exact version strings drift (browse https://ollama.com/search?c=cloud).
- The auto-updater is disabled (`DISABLE_AUTOUPDATER=1`) and onboarding is pre-accepted via a baked `~/.claude.json` so headless runs never block on a first-run prompt.
- `--mcp-config` is variadic, so the `--` before the prompt in `run_pass` is load-bearing — without it the CLI parses the prompt as another config path.
