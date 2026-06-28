# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Docker image that runs an unattended PR reviewer. It bundles the **Claude Code CLI** and the **GitHub CLI**, points Claude Code at **Ollama Cloud** (a non-Anthropic model, default `glm-5.2:cloud`), and loops over a repo's open PRs in headless "YOLO" mode, posting one comment per finding. The premise: a different model reviewing than the one that wrote the code avoids group-think.

There is no application code, build system, or test suite — the entire project is three files: `Dockerfile`, `entrypoint.sh`, and `README.md`. `entrypoint.sh` is where essentially all the logic lives.

## Commands

```bash
docker build -t claudebox .

# Run (detached, hardened) — see README "Run" for the full flag set
docker run -d --name claudebox --restart unless-stopped --env-file .env \
  -v /path/to/your/repo:/repo:ro \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --memory 4g \
  claudebox

docker logs -f claudebox        # watch the live play-by-play
docker exec -it claudebox bash  # poke around inside (gh auth status, gh pr list, etc.)
```

There is no linter or test runner. To test changes to `entrypoint.sh`, rebuild and run; `bash -n entrypoint.sh` syntax-checks it without Docker.

## Architecture

The design is built entirely around one constraint: **the loop runs unattended in YOLO mode (`--dangerously-skip-permissions`), so it must not be able to cause damage.** Three layered safety boundaries, all of which must be preserved when editing:

1. **Unprivileged user** — `Dockerfile` creates and runs as `reviewer`. This is also load-bearing functionally: Claude Code *refuses* `--dangerously-skip-permissions` as root.
2. **Read-only source** — the user's repo is mounted at `/repo:ro`. `entrypoint.sh` makes a cheap **local clone** (`git clone --local --no-hardlinks`) into a writable working dir and only ever touches the clone. `--no-hardlinks` is mandatory: a bind mount is a different device, so the default hardlinking clone fails with "Invalid cross-device link". If no git repo is mounted, it falls back to a network clone of `GITHUB_REPOSITORY`.
3. **Privilege-minimized GitHub token** — read repo/PRs + write PR comments only; no push/merge/admin. This is the real safety boundary; the README stresses verifying it before running unattended.

### Two pieces working together

- **`entrypoint.sh` is the supervisor.** It does auth setup (`gh`/`git`), wires the Claude-Code→Ollama env, prepares the working clone, then runs the review loop: `git fetch` → one review pass → sleep. It controls cadence and crash-recovery; Claude itself decides *what* to review.

- **One continuous, stateful Claude session.** The first pass starts a new `claude -p` session and the script recovers its `session_id` from the `stream-json` output; every later pass `--resume`s that id so Claude remembers what it already reviewed and won't re-raise findings. A failed pass clears `SESSION_ID`, so the next cycle starts fresh (and may re-comment once — accepted noise). `MAX_PASSES_PER_SESSION` optionally rotates to a fresh session to bound context growth.

  Why not Claude Code's `/loop`? `/loop` needs a live interactive session; headless `-p` exits after each response. The shell loop + `--resume` gives the same continuous, context-retaining behavior while staying headless and crash-safe.

### The all-tiers-mapped-to-one-model trick

`entrypoint.sh` points **every** model env var (`ANTHROPIC_MODEL`, `..._DEFAULT_OPUS/SONNET/HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`) at the single `$REVIEW_MODEL`. The Ollama backend has no Anthropic models, so if a subagent or alias requested an un-overridden Opus/Sonnet/Haiku tier, Claude Code would error on an unknown model. Auth uses `ANTHROPIC_AUTH_TOKEN` (**not** `ANTHROPIC_API_KEY`, which is blanked) against `ANTHROPIC_BASE_URL=https://ollama.com`. There is no fallback to Anthropic — a wrong model name is a hard error.

### Log formatting

`format_stream()` in `entrypoint.sh` is a `jq` filter that pretty-prints Claude's `stream-json` (one JSON event per line) into readable log lines. The raw stream is also tee'd to a temp file purely to recover the session id (`PIPESTATUS[0]` reads Claude's exit code, not jq's/tee's).

## Configuration

All config is via environment variables (`.env.example` documents them). Required: `OLLAMA_API_KEY`, `GITHUB_TOKEN`, `GITHUB_REPOSITORY`. Optional: `REVIEW_MODEL`, `REVIEW_INTERVAL_SECONDS`, `MAX_PASSES_PER_SESSION`, and the prompt overrides `REVIEW_PROMPT` (new session) / `FOLLOWUP_PROMPT` (resumed passes). Default prompts live in `entrypoint.sh`.

## Gotchas when editing

- Don't add `--read-only` to the container root fs: the loop must write its working clone under `$HOME`.
- Mount the **primary** repo, not a `git worktree` of it — a worktree keeps objects in its parent and is structurally unusable mounted alone.
- Model versions move fast; the `:cloud` suffix is stable but exact version strings drift (browse https://ollama.com/search?c=cloud).
- The auto-updater is disabled (`DISABLE_AUTOUPDATER=1`) and onboarding is pre-accepted via a baked `~/.claude.json` so headless runs never block on a first-run prompt.
