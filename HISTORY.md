# History

## Unreleased

* Review each PR in its own Claude Code session. The harness now enumerates candidate PRs and iterates, giving each PR an independent, resumable session so re-reviews avoid duplicate comments per PR. `MAX_PASSES_PER_SESSION` now applies per PR.
* Add PR targeting — choose exactly one of: all open PRs (`--all` / `PR_ALL`), open PRs assigned to a user (`--assignee` / `PR_ASSIGNEE`), a specific set of PR numbers (`--prs` / `PR_IDS`), or a `gh` search query (`--search` / `PR_SEARCH`). Zero or more than one is a startup error.
* Prompts are now PR-scoped: `REVIEW_PROMPT` (session start) and `FOLLOWUP_PROMPT` (resume) substitute a `{{PR}}` token with the PR number; custom prompts use the same token.
* Launcher: infer the env file (`.env.claudebox` preferred over `.env`) and repo from the current directory, derive a per-repo container name `claudebox--<org>--<repo>` so several claudeboxes can run at once, announce those inferences loudly, and add `--tail` to follow logs right after `run`.
* Optional Linear ticket context: set `LINEAR_API_KEY` and the reviewer reads the Linear ticket a PR references — description and comments — and flags where the change diverges from what the ticket asked for. Use a read-only key; the reviewer runs with permissions skipped.
* Always run review passes with `--strict-mcp-config`, so a repository under review can't inject MCP servers of its own choosing into a permission-skipped session.

## 0.0.3 - 2026-07-17

* Generalize the backend beyond Ollama Cloud: a new `PROVIDER` env var (`ollama` | `anthropic` | `custom`) selects the model provider. `ollama` remains the default, so existing configs are unchanged; `anthropic` targets Anthropic's own API, and `custom` targets any Anthropic-compatible endpoint. Every model tier is still pinned to the one `REVIEW_MODEL`.
* `PROVIDER=anthropic` no longer requires an `ANTHROPIC_API_KEY`: it falls back to `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or a mounted `~/.claude` credentials file, so you can reuse your existing `claude` login instead of a separate key.
* Add a `claudebox.sh` launcher that wraps `docker build`/`run` and the lifecycle commands (`build`, `run`, `test`, `logs`, `shell`, `stop`, `status`) with the required hardening flags baked in. Self-describing via `--help`; `--dry-run` prints the docker command without executing, and `--mount-claude` bind-mounts `~/.claude` for Anthropic login reuse.
* Move the provider-specific `REVIEW_MODEL` default out of the Dockerfile so each provider can supply its own (ollama: `glm-5.2:cloud`, anthropic: `claude-opus-4-8`).
* Tweak the default prompt to encourage a slightly bigger-picture perspective.

## 0.0.2 - 2026-07-01

* Add verbiage to prompts to allow addressing claudebox to force a re-review.
* Add verbiage to prompts to get claudebox to sign its comments.  This should make it a little easier to identify who's talking to who when Claude and Claudebox are both going through the same Github account.

## 0.0.1 - 2026-06-28

* Initial version with support for the Ollama cloud API, and default prompts tuned for glm-5.2.
