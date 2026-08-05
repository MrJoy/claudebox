# claudebox — autonomous PR reviewer

Using a different model to review code than the model that wrote it produces a better quality of result by avoiding group-think.  Copilot is nicely integrated into Github, allowing for automated back-and-forth between the model writing the code and the model reviewing it -- but Copilot is not the most effective reviewer model available.

This is a Docker image that runs a hands-off pull-request reviewer, built on the claude CLI infrastructure. It can point Claude Code at whichever backend you like: **Ollama Cloud** (the default — no API costs beyond your Ollama plan, and a *different* model than the one that wrote the code), **Anthropic's own API**, a **Cloudflare AI Gateway** (fronting Anthropic, Bedrock, or Vertex), or **any other Anthropic-compatible endpoint**. Out of the box it runs glm-5.2 on Ollama Cloud, which I've found to be impressively thorough compared to both Copilot and GPT 5.5.

The container bundles the **Claude Code CLI** and the **GitHub CLI**. Claude Code talks directly to the configured provider (via the Anthropic-compatible API — no proxy, and no local `ollama` binary needed) and runs in non-interactive "YOLO" mode on a loop, reviewing open PRs and posting findings as comments.

> **Group-think caveat:** the value of an independent reviewer comes from it being a *different* model than the one that wrote the code. Pointing this at Anthropic to review Claude-authored PRs re-introduces the group-think this tool exists to avoid — and that's just as true of a Claude model reached via Bedrock, Vertex, or a gateway. The non-Ollama providers are there for reviewing code written by other tools, or when you simply prefer a specific model.

It is designed so the loop *cannot cause damage*:

- Runs as an **unprivileged user** inside the container.
- Makes a cheap **local clone** of a **read-only** mount of your primary repo and works only in that clone, so your source is never modified.
- Uses a **privilege-minimized GitHub token** that can read the repo/PRs and write PR comments — nothing else (no push, merge, or admin).

## Choosing a provider

Set `PROVIDER` (default `ollama`) to pick the backend. The entrypoint validates the credentials that provider needs and wires the corresponding Claude Code environment variables for you:

| `PROVIDER` | Credential you set | Endpoint | Default `REVIEW_MODEL` |
| --- | --- | --- | --- |
| `ollama` *(default)* | `OLLAMA_API_KEY` | `https://ollama.com` | `glm-5.2:cloud` |
| `anthropic` | `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, *or* mounted creds | Anthropic's default | `claude-opus-4-8` |
| `custom` | `ANTHROPIC_AUTH_TOKEN` *or* `ANTHROPIC_API_KEY` | your `ANTHROPIC_BASE_URL` | *(none — you must set `REVIEW_MODEL`)* |
| `cloudflare` | depends on `GATEWAY_UPSTREAM` — see [below](#cloudflare-ai-gateway) | your Cloudflare AI Gateway | *(none — you must set `REVIEW_MODEL`)* |
| `workersai` | `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` | Cloudflare Workers AI, via a bundled translator | `@cf/zai-org/glm-5.2` |

- **`ollama`** — Ollama Cloud's native Anthropic-compatible API. Auth goes through `ANTHROPIC_AUTH_TOKEN` (a Bearer token), so `ANTHROPIC_API_KEY` is blanked.
- **`anthropic`** — Anthropic's own API, at its default endpoint. You don't have to paste an API key: the entrypoint takes the first credential it finds, in this order — `ANTHROPIC_API_KEY` (console key), else `CLAUDE_CODE_OAUTH_TOKEN`, else a mounted credentials file — and only errors if none exists. See [Reusing your existing `claude` login](#reusing-your-existing-claude-login).
- **`custom`** — any other Anthropic-compatible endpoint. Set `ANTHROPIC_BASE_URL`, a `REVIEW_MODEL` the endpoint serves, and whichever auth the endpoint expects: `ANTHROPIC_AUTH_TOKEN` for a Bearer header (most gateways/compatible services) or `ANTHROPIC_API_KEY` for `x-api-key`.
- **`cloudflare`** — a [Cloudflare AI Gateway](https://developers.cloudflare.com/ai-gateway/integrations/coding-agents/claude-code/) fronting Anthropic, Amazon Bedrock, or Google Vertex AI. See [Cloudflare AI Gateway](#cloudflare-ai-gateway).
- **`workersai`** — a model Cloudflare itself hosts, from the [Workers AI catalog](https://developers.cloudflare.com/workers-ai/models/). Needs no URL and no gateway; see [Cloudflare Workers AI](#cloudflare-workers-ai).

> **Don't quote values in your env file.** `docker run --env-file` isn't a shell — it keeps everything after the `=` literally, so `ANTHROPIC_BASE_URL="https://…"` yields a value that really begins and ends with a quote, and Claude Code then fails on an unparseable URL at *request* time. Values with spaces or colons (`ANTHROPIC_CUSTOM_HEADERS`, `PR_SEARCH`) need no quoting. The entrypoint strips a matched surrounding pair and warns, and rejects a base URL that isn't `http(s)://` at startup, but the habit is the thing to fix. Shell quoting *is* required for launcher flags like `--search "is:open label:x"`, which is a different context.

Any provider also honors **`ANTHROPIC_CUSTOM_HEADERS`**, which Claude Code sends on every request to the provider. It's how a gateway token travels; required for two of the `cloudflare` upstreams, optional everywhere else.

Claude Code wants **one header per line**, which an env file cannot express — it's strictly one `KEY=VALUE` per line, with no continuation and no escape processing. So write several headers either way, and the entrypoint assembles the real multi-line value:

```bash
# a literal backslash-n between headers...
ANTHROPIC_CUSTOM_HEADERS=cf-aig-gateway-id: my-gw\ncf-aig-authorization: Bearer <CF_AIG_TOKEN>

# ...or one numbered variable each (up to _20). Mixing both is fine; the
# numbered ones are appended after the unnumbered one, in index order.
ANTHROPIC_CUSTOM_HEADERS_1=cf-aig-gateway-id: my-gw
ANTHROPIC_CUSTOM_HEADERS_2=cf-aig-authorization: Bearer <CF_AIG_TOKEN>
```

Only the two-character sequence `\n` is translated — a `\t` or `\\` inside a token is left exactly as written. Each resulting line must look like `Name: value`; one that doesn't is a startup error naming the offending header (never its value, which is a credential).

### Cloudflare Workers AI

Cloudflare's own model catalog — glm-5.2, the Kimi models, and the rest — is a different thing from an AI Gateway, and needs a different provider. Set two variables and nothing else:

```bash
PROVIDER=workersai
CLOUDFLARE_ACCOUNT_ID=<your account id>
CLOUDFLARE_API_TOKEN=<token with the Workers AI Read permission>
# REVIEW_MODEL=@cf/zai-org/glm-5.2   # the default; see the catalog for others
```

Create the token at [dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens) — an **API token** with **Workers AI: Read**, not the Global API Key. The endpoint is derived from your account id, so there is no URL to paste and no chance of the doubled-path mistake.

Pick a model that lists **function calling** in the catalog. A reviewer that can't call tools can't read the diff, so a model without it fails in a confusing rather than obvious way. `@cf/zai-org/glm-5.2` (the default) and `@cf/moonshotai/kimi-k2.7-code` both do.

> **Why this needs a translator, and `cloudflare` doesn't.** Workers AI models are served only over an OpenAI-compatible schema — [Cloudflare's REST API docs](https://developers.cloudflare.com/ai-gateway/usage/rest-api/) state that the Anthropic-shaped `/ai/v1/messages` endpoint does *not* serve `@cf/` models — and Claude Code speaks nothing but the Anthropic Messages API. So this provider starts a [LiteLLM](https://docs.litellm.ai/docs/anthropic_unified/) proxy inside the container as an Anthropic→OpenAI translator, and points Claude Code at it. It's baked into the image (pinned; override with `--build-arg LITELLM_VERSION=…`), listens on **loopback only**, and runs only for this provider — every other provider still talks straight to its endpoint with no extra process. Your Cloudflare token stays behind the translator: Claude Code is given a random per-container key instead, so a PR that tries to prompt-inject its way to your credentials doesn't find them. Set `LITELLM_PORT` if 4000 is taken.
>
> Bundling it roughly doubles the image (~1.1GB to ~1.9GB) — it drags in Python and LiteLLM's dependency tree. That only affects `PROVIDER=workersai`; the other providers ignore it entirely, but they do carry the bytes.
>
> If a model rejects the translated requests, set `LITELLM_DEBUG=1` to log the actual request bodies to `litellm.log`. They include your token, so turn it back off for unattended runs.
>
> The translator is started before the first review pass and waited on, and re-checked every cycle — if it dies, the container fails loudly instead of grinding through passes that can't reach a model. `docker exec <container> cat litellm.log` has its output.
>
> **And one more hop, for the Kimi models.** LiteLLM leaves the `content` field out entirely on an assistant message that carries only a tool call. That's legal OpenAI and glm-5.2 accepts it, but the Kimi models reject it outright — `Invalid value at messages[N].content` — and Claude Code produces such a message on *every* tool call, so those models would fail on essentially every review. So a small normalizer (`workersai-shim.py`, ~150 lines of Python standard library) runs between the translator and Cloudflare and fills in `content: ""`. It has to be a separate process because the omission happens in LiteLLM's output, after the translation its own plugin hooks can reach. Like the translator, it's loopback-only and this provider only; `SHIM_PORT` moves it off 4001 and `SHIM_NORMALIZE=0` removes it, though there's little reason to — an empty string is valid for every model, so it's one path that always gets exercised rather than a special case for one family. Its output is in `shim.log`.

### Cloudflare AI Gateway

`PROVIDER=cloudflare` points the reviewer at an AI Gateway, and `GATEWAY_UPSTREAM` says which upstream that gateway fronts — Claude Code speaks to each of the three differently. It defaults to `anthropic`, the one upstream where it changes nothing; name `bedrock` or `vertex` explicitly, since each reads a different base-URL variable and switches the wire protocol. `REVIEW_MODEL` is always required, because each upstream names models its own way.

| `GATEWAY_UPSTREAM` | Set these | Example `REVIEW_MODEL` |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_BASE_URL` (`…/<GATEWAY_ID>/anthropic`) + `ANTHROPIC_API_KEY` (an **Anthropic** key — see below) | `claude-opus-4-8` |
| `bedrock` | `ANTHROPIC_BEDROCK_BASE_URL` (`…/aws-bedrock/bedrock-runtime/<AWS_REGION>/`) + `ANTHROPIC_CUSTOM_HEADERS` | `us.anthropic.claude-opus-4-5-v1:0` |
| `vertex` | `ANTHROPIC_VERTEX_BASE_URL` (`…/google-vertex-ai/v1`) + `ANTHROPIC_VERTEX_PROJECT_ID` + `CLOUD_ML_REGION` + `ANTHROPIC_CUSTOM_HEADERS` | `claude-opus-4-5@20251101` |

This is a **gateway-only** path, deliberately: the gateway holds the cloud credentials and Claude Code skips its own AWS/GCP auth, so the entrypoint sets `CLAUDE_CODE_USE_BEDROCK`/`CLAUDE_CODE_USE_VERTEX` and `CLAUDE_CODE_SKIP_BEDROCK_AUTH`/`CLAUDE_CODE_SKIP_VERTEX_AUTH` itself. Don't set those four yourself — a value that contradicts your `GATEWAY_UPSTREAM`, or one asking Claude Code to authenticate to AWS/GCP directly, is a startup error rather than something quietly overridden. There are no AWS or GCP credentials in this container and nothing mounts any, so on `bedrock` and `vertex` the `cf-aig-authorization` header **is** the only credential — hence `ANTHROPIC_CUSTOM_HEADERS` being required there. For the same reason, those two arms drop any `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` left in the environment, so a stale key can't confuse which endpoint is really in use.

On the `anthropic` upstream, `ANTHROPIC_API_KEY` and `ANTHROPIC_CUSTOM_HEADERS` are **two different credentials**: the first is an Anthropic API key for the upstream, the second is the gateway token. Cloudflare's page shows the gateway token reused as `ANTHROPIC_API_KEY`, which works only if the gateway has a stored provider key to inject — otherwise Anthropic answers `x-api-key header is required`, which Claude Code reports as the misleading "Invalid API key". Note also that `ANTHROPIC_AUTH_TOKEN` is *not* an equivalent alternative here the way it is for `PROVIDER=custom`: Anthropic accepts `Authorization: Bearer` only for OAuth subscription tokens, so a console key put there starts up cleanly and then fails every request. The entrypoint warns if you do that.

```bash
PROVIDER=cloudflare
GATEWAY_UPSTREAM=bedrock
REVIEW_MODEL=us.anthropic.claude-opus-4-5-v1:0
ANTHROPIC_BEDROCK_BASE_URL=https://gateway.ai.cloudflare.com/v1/<ACCOUNT_ID>/<GATEWAY_ID>/aws-bedrock/bedrock-runtime/us-east-1/
ANTHROPIC_CUSTOM_HEADERS=cf-aig-authorization: Bearer <CF_AIG_TOKEN>
```

### Reusing your existing `claude` login

With `PROVIDER=anthropic` you can authenticate the reviewer with the same subscription/OAuth credentials `claude` already uses on your machine, instead of a separate API key. The container has its own home and can't see your host credentials automatically, so pick one of:

- **Long-lived token (recommended, cross-platform).** On your host run `claude setup-token` (needs a Pro/Max/Team/Enterprise plan) and pass the result as `CLAUDE_CODE_OAUTH_TOKEN` in your `.env`. This is Anthropic's supported headless/CI path and works regardless of host OS.
- **Mount your credentials (Linux host only).** Bind-mount your host `~/.claude` into the reviewer's home so Claude Code finds `~/.claude/.credentials.json` itself — the launcher's `--mount-claude` flag does exactly this:

    ```bash
    ./claudebox.sh run --repo /path/to/repo --mount-claude
    # which adds:  -v "$HOME/.claude:/home/reviewer/.claude"   # NOT :ro — see below
    ```

  Caveats: mount it **read-write** (Claude Code refreshes the token and a `:ro` mount fails on refresh); the 0600 file must be readable by the container's `reviewer` user; and a **macOS** host keeps these credentials in the Keychain, not a file, so there's nothing to mount — use the token instead.

Whichever provider you pick, the entrypoint pins **every** model tier (`ANTHROPIC_MODEL` and each `..._DEFAULT_OPUS/SONNET/HAIKU/FABLE_MODEL`, plus the legacy `..._SMALL_FAST_MODEL`) to your one `REVIEW_MODEL`. On a non-Anthropic backend that's required: it has no Opus/Sonnet/Haiku models, so if a subagent or alias requested an un-overridden tier, Claude Code would error out on an unknown model. On Anthropic it's a deliberate simplification — one model does every part of the review, including any subagent work.

## How it works

The reviewer runs **one Claude session per PR**. Each cycle the entrypoint enumerates the candidate PRs (see [PR selection](#pr-selection)), then reviews each one in its own session: a PR's first review starts a new session with `REVIEW_PROMPT`; later cycles `--resume` that PR's session with `FOLLOWUP_PROMPT`, so Claude remembers what it already flagged on that PR and avoids duplicate comments. The PR number is substituted into the prompt's `{{PR}}` token.

```bash
# CLAUDE_MCP_ARGS is always (--strict-mcp-config), plus (--mcp-config "$MCP_CONFIG_FILE")
# when LINEAR_API_KEY is set. The "--" is load-bearing: --mcp-config is variadic, so
# without it the prompt would be parsed as another MCP config path.

# a PR's first review — new session
claude -p --output-format stream-json --verbose --dangerously-skip-permissions \
  --model "$REVIEW_MODEL" "${CLAUDE_MCP_ARGS[@]}" -- "${REVIEW_PROMPT//\{\{PR\}\}/$pr}"
# later cycles — resume that PR's session
claude -p --resume "${PR_SESSION[$pr]}" --output-format stream-json --verbose \
  --dangerously-skip-permissions --model "$REVIEW_MODEL" "${CLAUDE_MCP_ARGS[@]}" -- "${FOLLOWUP_PROMPT//\{\{PR\}\}/$pr}"
```

Each pass streams as `stream-json`; the entrypoint pretty-prints the events live to its log (so `docker logs -f` shows the play-by-play) and recovers the session id from the stream to resume that PR next cycle.

The entrypoint shell is the supervisor: it controls cadence (`git fetch`, enumerate PRs, review each sequentially, then sleep), keeps an in-memory PR→session map, and starts a fresh session for a PR if its pass fails (so it may re-comment once on that PR). Claude itself uses `gh`/`git` to inspect the PR, check out the latest commit, and post one comment per finding. `MAX_PASSES_PER_SESSION` rotates a PR's session after N passes to bound its context growth (per PR).

> Why not `/loop`? Claude Code's `/loop` needs a live *interactive* session — scheduled wake-ups only fire while a session is running and idle, and headless `-p` mode exits after each response. The shell loop + `--resume` gives the same continuous, context-retaining behavior while staying headless and crash-safe.

## The `claudebox.sh` launcher

`claudebox.sh` wraps the whole lifecycle — build, run, logs, shell, stop, status — and bakes in the required hardening flags so you can't forget them. It's the easiest way to drive the container; `docker` is always there if you'd rather do it by hand ([Run](#run) shows the raw commands).

```bash
./claudebox.sh --help                          # self-describing reference
./claudebox.sh build
./claudebox.sh run --repo /path/to/your/repo   # detached + hardened
./claudebox.sh run --repo /path/to/your/repo --mount-claude   # reuse your `claude` login
./claudebox.sh run --repo /path/to/your/repo --export-sessions   # export transcripts to host ~/.claude
./claudebox.sh logs                            # follow the play-by-play
./claudebox.sh test --repo /path/to/your/repo  # one-off, foreground, --rm
./claudebox.sh stop
```

**Per-repo config & naming.** Run the launcher from inside a repo's working copy and it
infers everything from the cwd, announcing each inference loudly:

- **Env file:** it auto-selects `.env.claudebox` (preferred) or `.env` from the current
  directory, so a repo can carry its own claudebox credentials in `.env.claudebox` without
  disturbing the project's own `.env`. Override with `--env-file PATH`.
- **Repo:** defaults to the current directory (override with `--repo PATH`).
- **Container name:** derived as `claudebox--<org>--<repo>` from `GITHUB_REPOSITORY` (in the
  env file) or the repo's git `origin` remote — e.g. `claudebox--mrjoy--hordes-of-orcs-next`.
  This is what lets several claudeboxes run at once, one per repo. Override with `--name`.

The same inference runs for `logs`, `shell`, `stop`, and `status`, so from a repo's working copy
`claudebox logs` / `stop` target that repo's container with no flags. Add `--tail` to `run`
to start the container and immediately follow its logs.

Add `--dry-run` to any command to print the exact `docker` invocation without running it. The sections below document the underlying `docker` commands the launcher assembles.

### Exporting review sessions to your host

By default the reviewer's Claude Code transcripts live on the container's
ephemeral filesystem and vanish when it's removed. `--export-sessions` writes
them to your host `~/.claude` instead, filed under the **same** project folder
your host `claude` uses for this repo — so you can compare in-container review
sessions against your own sessions for the repo (e.g. in a session-viewer tool)
and have them grouped together.

```bash
./claudebox.sh run --repo /path/to/your/repo --export-sessions
```

It works by two coupled steps: it bind-mounts your host
`~/.claude/projects/<repo-folder>` into the container read-write, and it runs
the review clone at your repo's *host path* inside the container so Claude Code
encodes the session folder to that same `<repo-folder>` name. It therefore
requires a mounted repo (it can't be combined with `--no-repo`). If you're
already using `--mount-claude`, all of `~/.claude` is mounted, so the narrow
mount is skipped and only the path alignment is added.

> **Safety trade-off:** this bind-mounts one host session folder **read-write**
> into a container running in YOLO mode — the container can read and rewrite
> *this repo's* transcripts. The mount is deliberately narrow (just this one
> repo's folder), so every other project's transcripts stay untouched. It's
> off by default; enable it only when you want the export.

> **Host caveat:** this reliably works on **macOS/Windows Docker Desktop**,
> which squashes bind-mount ownership to your host user. On a **native Linux
> host**, the container's `reviewer` user (uid 1001) may not be able to write
> to a host folder owned by your uid, so the export can silently fail to
> write — same caveat as `--mount-claude`.

## Build

```bash
./claudebox.sh build     # or: docker build -t claudebox .
```

### Tests

Provider wiring — which credential and endpoint variables each `PROVIDER` ends up handing Claude Code — is covered by a test suite that needs no Docker, network, or credentials:

```bash
./test-providers.sh              # all cases
./test-providers.sh cloudflare   # only cases whose label matches
./test-shim.sh                   # the workersai normalizer
bash -n entrypoint.sh && bash -n claudebox.sh   # syntax only
```

It stubs `gh`/`git`/`claude` and checks either the startup error the entrypoint refused with or the exact environment it built. That's a narrow claim on purpose: it proves the wiring matches intent, not that a provider accepts it. Before trusting a newly configured provider unattended, do one live `./claudebox.sh test --repo …` and watch it actually get a response.

`test-shim.sh` covers the `workersai` normalizer, which the suite above only ever sees stubbed. It runs the real script against a local echo server — still no Docker, network, or credentials — and checks the content injection *and its restraint* (nothing else in the request is rewritten), that a streamed response is relayed as it arrives rather than buffered to the end, and that the listener stays on loopback.

## Run

1. Create the GitHub token with **only** these permissions (fine-grained token, scoped to the target repo):
   - Contents: **Read**
   - Pull requests: **Read and write** (read PRs/diffs, post comments)
   - Issues: **Read and write** (PR comments use the issues API)
2. Get the credential for your provider:
   - **Ollama Cloud** (default): an API key from the [Ollama settings page](https://ollama.com/settings/keys) → `OLLAMA_API_KEY`.
   - **Anthropic**: set `PROVIDER=anthropic` and provide either an API key from the [Anthropic Console](https://console.anthropic.com/) → `ANTHROPIC_API_KEY`, or reuse your existing `claude` login → [Reusing your existing `claude` login](#reusing-your-existing-claude-login).
   - **Cloudflare AI Gateway**: set `PROVIDER=cloudflare`, `GATEWAY_UPSTREAM` (`anthropic`/`bedrock`/`vertex`), a `REVIEW_MODEL`, and that upstream's base URL plus its credential. See [Cloudflare AI Gateway](#cloudflare-ai-gateway).
   - **Custom endpoint**: set `PROVIDER=custom`, `ANTHROPIC_BASE_URL`, a `REVIEW_MODEL`, and `ANTHROPIC_AUTH_TOKEN` (or `ANTHROPIC_API_KEY`). See [Choosing a provider](#choosing-a-provider).
3. Fill in env vars (copy `.env.example` to `.env`):

    ```bash
    cp .env.example .env   # then edit
    ```

4. Run, mounting your **primary repo read-only** at `/repo`. It's a long-running unattended service, so run it **detached** (`-d`), give it a **name** so you can attach to its logs, and let it **restart** if it crashes. Easiest via the launcher:

    ```bash
    ./claudebox.sh run --repo /path/to/your/repo
    ./claudebox.sh logs                            # watch it (see Monitoring)
    ```

    which is exactly this `docker run`:

    ```bash
    docker run -d --name claudebox --restart unless-stopped \
      --env-file .env \
      -v /path/to/your/repo:/repo:ro \
      --cap-drop ALL \
      --security-opt no-new-privileges \
      --pids-limit 512 \
      --memory 4g \
      claudebox

    docker logs -f claudebox   # watch it (see Monitoring)
    ```

    For a quick one-off test, run it in the foreground with `--rm` (ephemeral — the container and its logs are removed on exit):

    ```bash
    ./claudebox.sh test --repo /path/to/your/repo
    # equivalently:
    docker run --rm -it --env-file .env -v /path/to/your/repo:/repo:ro --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --memory 4g claudebox
    ```

On startup the reviewer makes a cheap **local clone** of `/repo` into its own writable working dir — it reuses the local git object store, so no objects are downloaded over the network, and your repo is never written to. It then repoints `origin` at GitHub and fetches only the new PR refs each cycle.

The mount is optional: if you omit it, the reviewer does a full network clone of `GITHUB_REPOSITORY` on startup. Mounting your primary repo just avoids that initial download.

> **Note:** mount the *primary* repo, not a `git worktree` of it. A worktree keeps its objects in the parent repo and only holds a link back to it, so a worktree mounted on its own is structurally unusable inside the container.

### Hardening (enforced)

Because the loop runs unattended in YOLO mode, the command above locks the container down. What each flag buys you:

- `--cap-drop ALL` — drop all Linux capabilities; the reviewer needs none.
- `--security-opt no-new-privileges` — block privilege escalation via setuid.
- `--pids-limit 512` — cap runaway process spawning.
- `--memory 4g` — bound memory use.

The entrypoint **verifies these on startup** and refuses to run if a security boundary is missing: it aborts when running as root, or without `no-new-privileges`, or without `--cap-drop ALL`. The two resource bounds (`--pids-limit`, `--memory`) only print a `WARN` if absent, since they cap runaway use rather than form a safety boundary. To run somewhere these checks don't apply (e.g. a non-Docker runtime, or a deliberate test), set `ALLOW_UNHARDENED=1` to downgrade the hard failures to warnings.

Don't add `--read-only` to the root filesystem: the loop needs to write its working copy under the user's home.

## Monitoring

The reviewer logs its whole heartbeat — and a live play-by-play of each pass — to stdout. The detached, **named** container from [Run](#run) (named `claudebox--<org>--<repo>`) is what makes its logs attachable. `./claudebox.sh logs` / `shell` / `status` re-derive that name from the cwd, so they cover the common views with no flags; the raw commands:

```bash
docker logs -f claudebox          # follow live  (./claudebox.sh logs)
docker logs --tail 100 claudebox  # last 100 lines
docker logs --since 10m claudebox # last 10 minutes
```

Each cycle you'll see the scaffolding (`Fetching latest refs…`, `Starting review pass (new|resuming session <id>)…`, `Review pass complete (session <id>, pass N)`, rotations, `Sleeping Ns…`, and `WARN:` lines on failures) interleaved with the streamed pass detail:

```text
  ▸ session <id> started
  → Bash: {"command":"gh pr list ..."}
  ← <tool result, truncated>
  ✓ result (success): <Claude's summary of the pass>
```

Other useful views:

```bash
docker exec -it claudebox bash    # poke around inside:
#   gh auth status                          # token working?
#   gh pr list                              # what it sees
#   git -C ~/work/repo log --oneline -5     # working-clone state
docker stats claudebox            # CPU / memory / network
```

The real deliverable, of course, is on GitHub — the comments it posts. Watch those with `gh pr list --repo owner/repo` and `gh pr view <num> --repo owner/repo --comments`.

## Configuration

All configuration is via environment variables — see `.env.example`. Always required:

- `GITHUB_TOKEN`
- `GITHUB_REPOSITORY`
- exactly one PR selector (see [PR selection](#pr-selection) below)

Provider selection and its credential (see [Choosing a provider](#choosing-a-provider)):

- `PROVIDER` — `ollama` (default), `anthropic`, `custom`, or `cloudflare`
- The credential for that provider: `OLLAMA_API_KEY` (ollama); for anthropic one of `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` / a mounted `~/.claude` (see [Reusing your existing `claude` login](#reusing-your-existing-claude-login)); `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` (custom); or, for cloudflare, `GATEWAY_UPSTREAM` and that upstream's base URL and credential (see [Cloudflare AI Gateway](#cloudflare-ai-gateway))

Optional:

- `REVIEW_MODEL` (provider-specific default; **required** for `PROVIDER=custom` and `PROVIDER=cloudflare`)
- `ANTHROPIC_CUSTOM_HEADERS` (optional on any provider; **required** for `GATEWAY_UPSTREAM=bedrock`/`vertex`) — extra request headers, `Name: value` per line
- `REVIEW_INTERVAL_SECONDS`
- `REVIEW_PROMPT` (a PR's first review, new session; uses the `{{PR}}` token)
- `FOLLOWUP_PROMPT` (a PR's resumed review; uses the `{{PR}}` token)

  > The default prompts tell the reviewer how to work within the minimized token: pass an explicit `--json` field list to `gh pr view`, and don't use `gh pr checks` at all. Both need a permission a fine-grained PAT can't be granted — a bare `gh pr view` implicitly fetches `statusCheckRollup` and fails outright, which reads like a broken token rather than a missing permission. **If you override `REVIEW_PROMPT`/`FOLLOWUP_PROMPT` you get your text verbatim, so carry those constraints over yourself** (or add them via the `_SUFFIX` variables, which apply to overrides too). CI status is simply unavailable to the reviewer; it judges the code, not the build.
- `REVIEW_PROMPT_SUFFIX` / `FOLLOWUP_PROMPT_SUFFIX` (append extra instructions to the corresponding prompt — default or overridden; also supports the `{{PR}}` token)
- `MAX_PASSES_PER_SESSION` (rotate a PR's session to a fresh one every N passes, per PR; `0` = never)
- `LINEAR_API_KEY` (optional Linear ticket context; use a **read-only** key — see [Linear ticket context](#linear-ticket-context))
- `--export-sessions` (launcher flag, not an env var) — export review transcripts to the host and align the session folder; see [Exporting review sessions to your host](#exporting-review-sessions-to-your-host)

### PR selection

Set **exactly one** of these (or pass the matching launcher flag). Zero or more than one is a startup error:

| Env var | Launcher flag | Reviews |
|---|---|---|
| `PR_ALL=1` | `--all` | all open PRs |
| `PR_ASSIGNEE=login` | `--assignee login` | open PRs assigned to that user |
| `PR_IDS=12,15,20` | `--prs 12,15,20` | exactly those PR numbers |
| `PR_SEARCH=is:open label:x` | `--search "…"` | PRs matching a gh search query (you control state) |

`REVIEW_PROMPT`/`FOLLOWUP_PROMPT` use a `{{PR}}` token (substituted with the PR number), and `MAX_PASSES_PER_SESSION` applies per PR.

### Linear ticket context

Set `LINEAR_API_KEY` and the reviewer also reads the Linear ticket a PR references — its description *and* its comments, where later feedback and revised requirements usually live — and raises divergence from what the ticket asked for as a finding, alongside the usual code findings. Unset, nothing about the review changes. This only happens with the default `REVIEW_PROMPT`/`FOLLOWUP_PROMPT`, though: the Linear instructions are appended to those defaults, not injected independently, so if you override either prompt, your prompt runs verbatim with the Linear MCP server available but no instruction to use it — tell the reviewer yourself to consult the ticket if you want that behavior with a custom prompt. If you just want to add your own instructions on top of the defaults (Linear stanza included), `REVIEW_PROMPT_SUFFIX`/`FOLLOWUP_PROMPT_SUFFIX` are the cleaner route — they append to whichever prompt is in effect instead of replacing it.

Get a key from **Settings → Security & access → Personal API keys**. Linear's MCP server accepts an API key straight through as an `Authorization: Bearer` header ([Linear docs](https://linear.app/docs/mcp)), so there is no interactive OAuth step and the loop stays headless.

> **Use a read-only key.** Linear lets you restrict a personal API key to `Read`. The reviewer runs with `--dangerously-skip-permissions`, so a write-capable key would let an unattended session modify your tickets. Like `GITHUB_TOKEN`, the key's scope can't be inspected from inside the container — minimizing it is on you.

Read-only bounds what the reviewer can change, not what it can see: a personal API key is scoped to your whole Linear workspace, not to the one ticket a PR claims to reference. The reviewer already treats PR titles, bodies, and diffs as untrusted input, and it can post PR comments — so Linear ticket content becomes a second untrusted input channel into a permission-skipped session, and a hostile or careless PR body can in principle steer it into reading unrelated tickets and pasting their contents into a comment on a possibly-public PR. Don't enable `LINEAR_API_KEY` on repos that take PRs from untrusted contributors, and prefer a key from an account with minimal Linear visibility over your main one.

The entrypoint writes the key into a generated MCP config at `$HOME/mcp.json` (mode `600`) and passes it to Claude Code with `--mcp-config`. Every review pass also runs with `--strict-mcp-config`, whether or not Linear is configured: `/repo` is untrusted input, and strict mode means a repository that ships its own `.mcp.json` can't get MCP servers of its choosing loaded into a permission-skipped session.

## Notes & caveats

- **Model names move fast, and there is no fallback.** `REVIEW_MODEL` must name a model your chosen provider actually serves; a wrong name is a hard error, not a silent fall-through to some other model. For Ollama the `:cloud` suffix is stable but exact versions change — browse [Ollama's model registry](https://ollama.com/search?c=cloud). For Anthropic, see the current model IDs in the [Anthropic docs](https://docs.anthropic.com/en/docs/about-claude/models).
- The token is the real safety boundary. Verify it has no write access beyond PR comments before running unattended.
- Because each PR is reviewed in its own resumed session, the reviewer remembers what it already flagged on that PR and won't re-raise the same findings. If a PR's pass fails it starts a fresh session for that PR next cycle (losing that in-session memory), so it may occasionally re-comment on it after a failure — harmless, just noise. The PR→session map is in-memory, so a container restart can likewise re-review each PR once.
- Each PR's session context grows over time. Set `MAX_PASSES_PER_SESSION` to rotate a PR's session to a fresh one every N passes and bound that growth (the trade-off: the new session forgets that PR's earlier passes, so it may re-raise findings once after a rotation). Left at `0`, each PR's session runs unbounded until the container restarts.
