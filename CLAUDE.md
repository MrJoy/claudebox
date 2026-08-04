# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Docker image that runs an unattended PR reviewer. It bundles the **Claude Code CLI** and the **GitHub CLI**, points Claude Code at a configurable model provider — **Ollama Cloud** by default (`glm-5.2:cloud`), or **Anthropic**, a **Cloudflare AI Gateway** (fronting Anthropic/Bedrock/Vertex), or any **Anthropic-compatible** endpoint via the `PROVIDER` env var — and loops over a repo's open PRs in headless "YOLO" mode, posting one comment per finding. The premise: a different model reviewing than the one that wrote the code avoids group-think (which is why Ollama is the default and reviewing Claude-authored code with `PROVIDER=anthropic` re-introduces the group-think the tool exists to avoid).

There is no application code or build system. The project is a handful of files: `Dockerfile`, `entrypoint.sh` (where essentially all the runtime logic lives), `claudebox.sh` (a host-side launcher wrapping `docker build`/`run` and the lifecycle commands), `test-providers.sh` (the only test suite), `README.md`, `.env.example`, and `HISTORY.md`.

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

There is no linter. Syntax-check the shell without Docker via `bash -n entrypoint.sh` / `bash -n claudebox.sh`, and run the provider tests with `./test-providers.sh` (optionally `./test-providers.sh <substring>` to filter by case label).

`test-providers.sh` covers the "Backend selection" block and the model-tier pinning after it — the part of the entrypoint with the most branches and the least visible failure mode (a mis-wired credential var shows up as a per-request 401, not a startup error). It needs no Docker, network, or credentials: it stubs `gh`/`git`/`claude`/`sleep` onto `PATH`, runs the entrypoint under `env -i` with `ALLOW_UNHARDENED=1`, and asserts either the startup error it refused with (`refuses`) or the exact environment it handed `claude` (`wires`, where `<unset>` asserts absence — which is what distinguishes the blank-vs-unset credential handling that several arms depend on). Two mechanics worth knowing before editing it: the stubbed `sleep` exits non-zero so the entrypoint's own `set -e` ends the review loop after one cycle, and the suite needs bash 4+ for `declare -A` (it hunts for one, since macOS `/bin/bash` is 3.2 and can't run the entrypoint at all).

It does NOT prove a provider accepts what gets wired — only that the wiring is what we intended. Validate a new provider live with `claudebox.sh test` before trusting it unattended. `claudebox.sh` runs on the **host**, where macOS ships **bash 3.2** — so keep it 3.2-safe (e.g. expand possibly-empty arrays as `${arr[@]+"${arr[@]}"}`, not `"${arr[@]}"`, which trips `set -u`). `entrypoint.sh` runs inside the image (modern bash).

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
- `cloudflare` — a Cloudflare AI Gateway, per its Claude Code integration page. A second selector, `GATEWAY_UPSTREAM` (`anthropic`|`bedrock`|`vertex`), picks which upstream the gateway fronts, because Claude Code talks to each differently; `REVIEW_MODEL` has no default (all three name models differently). `anthropic` is the plain base-URL-plus-credential shape; `bedrock` needs `ANTHROPIC_BEDROCK_BASE_URL`, `vertex` needs `ANTHROPIC_VERTEX_BASE_URL` + `ANTHROPIC_VERTEX_PROJECT_ID` + `CLOUD_ML_REGION`.

- `workersai` — a model from Cloudflare's **Workers AI** catalog, reached through a **LiteLLM proxy running inside the container** (see "The Workers AI translator" below). Requires `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`; the base URL is derived (`https://api.cloudflare.com/client/v4/accounts/$ID/ai/v1`), and `ANTHROPIC_BASE_URL` points at `127.0.0.1:$LITELLM_PORT` instead. Default `REVIEW_MODEL=@cf/zai-org/glm-5.2`.

  **Gateway-only by design.** The container holds no AWS/GCP credentials and mounts none, so the entrypoint sets `CLAUDE_CODE_USE_BEDROCK`/`CLAUDE_CODE_USE_VERTEX` and `CLAUDE_CODE_SKIP_*_AUTH=1` itself rather than reading them from the operator; a `USE_*` switch selecting a different upstream than `GATEWAY_UPSTREAM`, or a `SKIP_*_AUTH` other than `1`, is a hard error (inside Claude Code the `USE_*` switch — not our selector — decides the API, so a stale one in an env file would silently win). Because cloud auth is skipped, `ANTHROPIC_CUSTOM_HEADERS` (the `cf-aig-authorization` header) is the *only* credential on those two arms and is therefore required there; both also drop any `ANTHROPIC_BASE_URL`/`_API_KEY`/`_AUTH_TOKEN` left in the environment.

`ANTHROPIC_CUSTOM_HEADERS` is validated (each line must contain a `:`) and exported for **any** provider — a gateway can front a custom or Ollama endpoint too. Its value is a credential, so it's never logged. Note that apostrophes can't appear in the `${VAR:?message}` validation messages: quote processing applies inside the expansion, so one silently breaks the script's parse.

`build_custom_headers` (an "Extra request headers" block, deliberately placed *before* the provider `case` — the bedrock/vertex arms require the assembled value) exists because Claude Code takes several headers as **one multi-line value** and `docker run --env-file` cannot express one: strictly one `KEY=VALUE` per line, no continuation, no escape processing. So it accepts two one-line spellings — a literal `\n` between headers, and/or `ANTHROPIC_CUSTOM_HEADERS_1`…`_$CUSTOM_HEADER_MAX` — and joins them (unnumbered first, then numbered in index order) into the real multi-line value. Only the two-character `\n` is translated, *not* via `printf '%b'`, which would also eat `\t`/`\\`/`\xNN` and could quietly mangle a token. A non-contiguous index set warns but still sends everything (silently dropping a credential header would be worse). Comma separation is deliberately not supported: it's claimed only secondhand, undocumented, and a header value may legitimately contain a comma. The function's stdout **is** the result, so any `log`/`strip_surrounding_quotes` call inside it must be redirected to stderr or the warning lands inside a header value.

Regardless of provider, a shared block then points **every** model env var (`ANTHROPIC_MODEL`, `..._DEFAULT_FABLE/OPUS/SONNET/HAIKU_MODEL`, `ANTHROPIC_SMALL_FAST_MODEL`) at the single `$REVIEW_MODEL`. On non-Anthropic backends this is required — they have no Opus/Sonnet/Haiku models, so an un-overridden tier requested by a subagent or alias would error on an unknown model; on Anthropic it's a deliberate simplification (one model does everything). There is **no** fallback: a wrong model name is a hard error, never a silent switch to another model. `REVIEW_MODEL`'s default is provider-specific and resolved in the entrypoint, so it is intentionally **not** baked into the Dockerfile `ENV`.

### The Workers AI translator

`PROVIDER=cloudflare` cannot reach Cloudflare's own models: Cloudflare's REST API docs state that its Anthropic-shaped `/ai/v1/messages` endpoint does not serve Workers AI (`@cf/…`) models, which are available only over the OpenAI-compatible `/ai/v1/chat/completions`. Claude Code speaks nothing but the Anthropic Messages API. `PROVIDER=workersai` closes that gap by running **LiteLLM's proxy** (installed into `/opt/litellm` by the `Dockerfile`, pinned via the `LITELLM_VERSION` build arg) as an in-container translator: Anthropic `/v1/messages` in, OpenAI `/chat/completions` out, streaming and tool calls included. An off-the-shelf translator was chosen over writing one because tool-call fidelity **is** the product — a shim that mistranslates streamed `tool_use` blocks yields a reviewer that silently stops reading the diff.

Three things about it are load-bearing rather than incidental:

- **`--host 127.0.0.1`.** LiteLLM's proxy defaults to `0.0.0.0` and is unauthenticated unless a master key is set. It holds a Cloudflare token, so it must not be reachable off-container. The entrypoint also generates a random per-container `LITELLM_MASTER_KEY` and hands *that* (not the Cloudflare token) to Claude Code as `ANTHROPIC_AUTH_TOKEN` — so a prompt-injected review can't read the real credential out of its environment. `test-providers.sh` asserts both.
- **`--num_workers 1`.** The default is one worker per CPU; the loop reviews one PR at a time, and extra workers just eat into `--pids-limit`/`--memory`.
- **Startup is synchronous.** `start_litellm` blocks on the proxy's unauthenticated `/health/liveliness` probe (up to 120s) and `die`s with the tail of `$HOME/litellm.log` if it exits or never answers. Starting it lazily would fail the first review pass, and a failed pass throws away that PR's session. `check_litellm` re-checks each cycle so a dead translator is one loud error rather than every pass failing on connection refused; a `trap … EXIT` stops it with us.

**`use_chat_completions_url_for_anthropic_messages: true` in the generated config is required, not a tuning knob.** For the `openai` provider LiteLLM translates an incoming `/v1/messages` request into the OpenAI **Responses** API by default — `input`/`instructions`/`max_output_tokens`, and flat `{type, name, parameters}` tools. Cloudflare's `/ai/v1` surface serves Responses only for a couple of models (GPT-OSS), not glm-5.2, so without this flag every request fails Cloudflare's schema union with a wall of `required properties at '/' are 'messages'` plus, once per tool, `required properties at '/tools/N/function' are 'name'` and `enum function not in custom at '/tools/N/type'`. The wall of tool errors is misdirection — the real fault is the request body being Responses-shaped. With the flag, LiteLLM emits `messages` and nested `{type: function, function: {name, …}}` tools, which is what Cloudflare accepts. The switch is `_should_route_to_responses_api` in `litellm/llms/anthropic/experimental_pass_through/messages/handler.py`; re-check it when bumping `LITELLM_VERSION`.

To see what the translator actually put on the wire, set `LITELLM_DEBUG=1` (adds `--detailed_debug`). It logs full request bodies **including the Authorization header**, so it warns and must stay off for unattended runs. To capture the outbound shape without a Cloudflare token at all, point `api_base` at a local echo server — that is how the Responses-vs-chat bug above was found.

**The `fastapi` pin in the `Dockerfile` is not optional.** `litellm[proxy]` under-constrains fastapi, and fastapi removed `fastapi.dependencies.utils.get_flat_dependant` in **0.140.7 — a patch release** — which litellm 1.95.0 imports. An unconstrained resolve therefore installs a fastapi whose proxy cannot be imported at all, and litellm's CLI catches the real `ImportError` and retries a relative `from proxy_server import …`, so the only symptom is a baffling `ModuleNotFoundError: No module named 'proxy_server'`. The `RUN` step ends with `python -c "import litellm.proxy.proxy_server"` so this fails the **build** rather than producing an image that starts and never serves. When bumping `LITELLM_VERSION`, re-bisect the fastapi boundary — don't widen the range and assume.

`write_litellm_config` emits `$HOME/litellm.yaml` at mode 600 with scalars quoted through `jq` (a JSON scalar is a valid YAML scalar), so a model id full of `@` and `/` can't break the file. The Cloudflare token is referenced as `api_key: os.environ/CLOUDFLARE_API_TOKEN` and therefore never written to disk. `drop_params: true` is set because Claude Code sends Anthropic-specific parameters with no OpenAI equivalent, and dropping them beats failing the request.

### Log formatting

`format_stream()` in `entrypoint.sh` is a `jq` filter that pretty-prints Claude's `stream-json` (one JSON event per line) into readable log lines. The raw stream is also tee'd to a temp file purely to recover the session id (`PIPESTATUS[0]` reads Claude's exit code, not jq's/tee's).

### Optional Linear context

`LINEAR_API_KEY` (optional) gives the reviewer read access to the Linear ticket a PR references. `write_mcp_config` generates `$HOME/mcp.json` (mode 600, built with `jq --arg` so a hostile key can't break the JSON) pointing at `https://mcp.linear.app/mcp` with the key as an `Authorization: Bearer` header — Linear accepts an API key in place of interactive OAuth, which is what keeps the loop headless. `linear_stanza` appends the "check the ticket and its comments" instruction to `DEFAULT_PROMPT`/`DEFAULT_FOLLOWUP` **only**, so an operator-supplied `REVIEW_PROMPT`/`FOLLOWUP_PROMPT` reaches Claude verbatim. Docs tell operators to use a read-only key: in YOLO mode a write-capable key would let the unattended reviewer mutate tickets, and like `GITHUB_TOKEN` its scope can't be checked from inside.

`CLAUDE_MCP_ARGS` carries the MCP flags for both `claude -p` call sites and always includes **`--strict-mcp-config`**, Linear or not. That's load-bearing: `/repo` is untrusted, and without it a reviewed repo shipping a `.mcp.json` could get MCP servers of its choosing loaded into a `--dangerously-skip-permissions` session.

## Configuration

All config is via environment variables (`.env.example` documents them). Always required: `GITHUB_TOKEN`, `GITHUB_REPOSITORY`, and exactly one PR selector (`PR_ALL`/`PR_ASSIGNEE`/`PR_IDS`/`PR_SEARCH`). Provider selection: `PROVIDER` (default `ollama`) plus that provider's credential — `OLLAMA_API_KEY` (ollama), `ANTHROPIC_API_KEY` (anthropic), `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` (custom), `GATEWAY_UPSTREAM` (optional, default `anthropic`) + that upstream's base URL/project/region and `ANTHROPIC_CUSTOM_HEADERS` (cloudflare), or `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` (workersai); see "Backend selection" above. Optional `LITELLM_PORT` (default 4000) for the workersai translator. Optional: `REVIEW_MODEL` (provider-specific default, but required for `custom` and `cloudflare`), `ANTHROPIC_CUSTOM_HEADERS` (see below), `REVIEW_INTERVAL_SECONDS`, `MAX_PASSES_PER_SESSION`, `ALLOW_UNHARDENED`, `LINEAR_API_KEY` (see "Optional Linear context" above), the prompt overrides `REVIEW_PROMPT` (new session) / `FOLLOWUP_PROMPT` (resumed passes), and `REVIEW_PROMPT_SUFFIX` / `FOLLOWUP_PROMPT_SUFFIX` (append to whichever of those is in effect, default or override). Default prompts live in `entrypoint.sh`.

## Gotchas when editing

- Don't add `--read-only` to the container root fs: the loop must write its working clone under `$HOME`.
- Mount the **primary** repo, not a `git worktree` of it — a worktree keeps objects in its parent and is structurally unusable mounted alone.
- Model versions move fast; the `:cloud` suffix is stable but exact version strings drift (browse https://ollama.com/search?c=cloud).
- The auto-updater is disabled (`DISABLE_AUTOUPDATER=1`) and onboarding is pre-accepted via a baked `~/.claude.json` so headless runs never block on a first-run prompt.
- `docker run --env-file` does no shell quote processing, so a quoted env-file value arrives with literal quotes and fails late and confusingly (a quoted `ANTHROPIC_BASE_URL` produces `"https://…"/v1/messages`, an unparseable URL, at request time rather than startup). `strip_surrounding_quotes` removes one matched pair from the operator-supplied vars and warns; `check_url` rejects a non-`http(s)` base URL at startup, and also rejects one ending in an endpoint path (`/v1/messages`, `/v1/chat/completions`, …) because Claude Code appends `/v1/messages` itself and the doubled path 404s every request — a bare trailing `/v1` is left alone, since the Vertex base URL requires one. Keep new operator-facing vars on that list, and don't write quoted examples in `.env.example`.
- `--mcp-config` is variadic, so the `--` before the prompt in `run_pass` is load-bearing — without it the CLI parses the prompt as another config path.
