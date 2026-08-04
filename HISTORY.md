# History

## 0.0.5 - 2026-08-04

* Add `PROVIDER=cloudflare` for a Cloudflare AI Gateway, covering every environment variable on Cloudflare's Claude Code integration page. `GATEWAY_UPSTREAM` (`anthropic` | `bedrock` | `vertex`) says which upstream the gateway fronts, since Claude Code speaks to each differently: `anthropic` takes `ANTHROPIC_BASE_URL` plus an API key or Bearer token, `bedrock` takes `ANTHROPIC_BEDROCK_BASE_URL`, and `vertex` takes `ANTHROPIC_VERTEX_BASE_URL` + `ANTHROPIC_VERTEX_PROJECT_ID` + `CLOUD_ML_REGION`. `REVIEW_MODEL` is required (all three name models differently).
* This is a gateway-only path on purpose: the gateway holds the cloud credentials, so the entrypoint sets `CLAUDE_CODE_USE_BEDROCK`/`CLAUDE_CODE_USE_VERTEX` and `CLAUDE_CODE_SKIP_BEDROCK_AUTH`/`CLAUDE_CODE_SKIP_VERTEX_AUTH` itself. A switch that contradicts `GATEWAY_UPSTREAM`, or one asking Claude Code to authenticate to AWS/GCP directly, is a startup error rather than a silent override — and the bedrock/vertex arms drop any leftover `ANTHROPIC_BASE_URL`/`_API_KEY`/`_AUTH_TOKEN` so a stale key can't confuse which endpoint is in use.
* Tolerate quoted env-file values. `docker run --env-file` does no shell quote processing, so `ANTHROPIC_BASE_URL="https://..."` arrived with literal quotes and every request then failed on an unparseable URL — late, and nowhere near the real cause. The entrypoint now strips one matched surrounding pair from the operator-supplied variables (with a warning) and rejects a base URL that isn't `http(s)://` at startup instead. The quoted `PR_SEARCH` example in `.env.example`/`README.md` was itself wrong, and is fixed.
* Add `test-providers.sh`, the project's first test suite: 46 cases over the provider-selection block, asserting either the startup error each misconfiguration refuses with or the exact environment the entrypoint hands `claude`. No Docker, network, or credentials needed — `gh`/`git`/`claude`/`sleep` are stubbed. Run `./test-providers.sh`, or pass a substring to filter by case label.
* Support `ANTHROPIC_CUSTOM_HEADERS` on **any** provider (a gateway can front a custom or Ollama endpoint too). It carries the `cf-aig-authorization` gateway token, and on the bedrock/vertex upstreams — where Claude Code's own cloud auth is skipped — it is the only credential there is, so it's required for those two.
* Make it possible to send **several** custom headers. Claude Code takes them as one multi-line value, which an env file cannot express at all (one `KEY=VALUE` per line, no continuation, no escape processing), so the entrypoint now accepts two one-line spellings and assembles the multi-line value itself: a literal `\n` between headers, and/or `ANTHROPIC_CUSTOM_HEADERS_1`…`_20`. Only `\n` is translated, so a backslash inside a token survives; each line is validated separately, and an error names the header but never its value.
* `GATEWAY_UPSTREAM` is now optional, defaulting to `anthropic` — the one upstream where it selects nothing (no switches to flip, and the base URL and credential are the ordinary `ANTHROPIC_*` ones). `bedrock` and `vertex` still have to be named.

## 0.0.4 - 2026-07-30

* Review each PR in its own Claude Code session. The harness now enumerates candidate PRs and iterates, giving each PR an independent, resumable session so re-reviews avoid duplicate comments per PR. `MAX_PASSES_PER_SESSION` now applies per PR.
* Add PR targeting — choose exactly one of: all open PRs (`--all` / `PR_ALL`), open PRs assigned to a user (`--assignee` / `PR_ASSIGNEE`), a specific set of PR numbers (`--prs` / `PR_IDS`), or a `gh` search query (`--search` / `PR_SEARCH`). Zero or more than one is a startup error.
* Prompts are now PR-scoped: `REVIEW_PROMPT` (session start) and `FOLLOWUP_PROMPT` (resume) substitute a `{{PR}}` token with the PR number; custom prompts use the same token.
* Launcher: infer the env file (`.env.claudebox` preferred over `.env`) and repo from the current directory, derive a per-repo container name `claudebox--<org>--<repo>` so several claudeboxes can run at once, announce those inferences loudly, and add `--tail` to follow logs right after `run`.
* Optional Linear ticket context: set `LINEAR_API_KEY` and the reviewer reads the Linear ticket a PR references — description and comments — and flags where the change diverges from what the ticket asked for. Use a read-only key; the reviewer runs with permissions skipped.
* Always run review passes with `--strict-mcp-config`, so a repository under review can't inject MCP servers of its own choosing into a permission-skipped session.
* Add `REVIEW_PROMPT_SUFFIX`/`FOLLOWUP_PROMPT_SUFFIX` to append extra instructions to whichever prompt is in effect (default or an operator override), without replacing it.

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
