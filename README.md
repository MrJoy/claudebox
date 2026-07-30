# claudebox — autonomous PR reviewer

Using a different model to review code than the model that wrote it produces a better quality of result by avoiding group-think.  Copilot is nicely integrated into Github, allowing for automated back-and-forth between the model writing the code and the model reviewing it -- but Copilot is not the most effective reviewer model available.

This is a Docker image that runs a hands-off pull-request reviewer, built on the claude CLI infrastructure. It can point Claude Code at whichever backend you like: **Ollama Cloud** (the default — no API costs beyond your Ollama plan, and a *different* model than the one that wrote the code), **Anthropic's own API**, or **any other Anthropic-compatible endpoint**. Out of the box it runs glm-5.2 on Ollama Cloud, which I've found to be impressively thorough compared to both Copilot and GPT 5.5.

The container bundles the **Claude Code CLI** and the **GitHub CLI**. Claude Code talks directly to the configured provider (via the Anthropic-compatible API — no proxy, and no local `ollama` binary needed) and runs in non-interactive "YOLO" mode on a loop, reviewing open PRs and posting findings as comments.

> **Group-think caveat:** the value of an independent reviewer comes from it being a *different* model than the one that wrote the code. Pointing this at Anthropic to review Claude-authored PRs re-introduces the group-think this tool exists to avoid. Anthropic and custom providers are there for reviewing code written by other tools, or when you simply prefer a specific model.

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

- **`ollama`** — Ollama Cloud's native Anthropic-compatible API. Auth goes through `ANTHROPIC_AUTH_TOKEN` (a Bearer token), so `ANTHROPIC_API_KEY` is blanked.
- **`anthropic`** — Anthropic's own API, at its default endpoint. You don't have to paste an API key: the entrypoint takes the first credential it finds, in this order — `ANTHROPIC_API_KEY` (console key), else `CLAUDE_CODE_OAUTH_TOKEN`, else a mounted credentials file — and only errors if none exists. See [Reusing your existing `claude` login](#reusing-your-existing-claude-login).
- **`custom`** — any other Anthropic-compatible endpoint. Set `ANTHROPIC_BASE_URL`, a `REVIEW_MODEL` the endpoint serves, and whichever auth the endpoint expects: `ANTHROPIC_AUTH_TOKEN` for a Bearer header (most gateways/compatible services) or `ANTHROPIC_API_KEY` for `x-api-key`.

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
# a PR's first review — new session
claude -p --output-format stream-json --verbose --dangerously-skip-permissions \
  --model "$REVIEW_MODEL" "${REVIEW_PROMPT//\{\{PR\}\}/$pr}"
# later cycles — resume that PR's session
claude -p --resume "${PR_SESSION[$pr]}" --output-format stream-json --verbose \
  --dangerously-skip-permissions --model "$REVIEW_MODEL" "${FOLLOWUP_PROMPT//\{\{PR\}\}/$pr}"
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

## Run

1. Create the GitHub token with **only** these permissions (fine-grained token, scoped to the target repo):
   - Contents: **Read**
   - Pull requests: **Read and write** (read PRs/diffs, post comments)
   - Issues: **Read and write** (PR comments use the issues API)
2. Get the credential for your provider:
   - **Ollama Cloud** (default): an API key from the [Ollama settings page](https://ollama.com/settings/keys) → `OLLAMA_API_KEY`.
   - **Anthropic**: set `PROVIDER=anthropic` and provide either an API key from the [Anthropic Console](https://console.anthropic.com/) → `ANTHROPIC_API_KEY`, or reuse your existing `claude` login → [Reusing your existing `claude` login](#reusing-your-existing-claude-login).
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

- `PROVIDER` — `ollama` (default), `anthropic`, or `custom`
- The credential for that provider: `OLLAMA_API_KEY` (ollama); for anthropic one of `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` / a mounted `~/.claude` (see [Reusing your existing `claude` login](#reusing-your-existing-claude-login)); or `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` (custom)

Optional:

- `REVIEW_MODEL` (provider-specific default; **required** for `PROVIDER=custom`)
- `REVIEW_INTERVAL_SECONDS`
- `REVIEW_PROMPT` (a PR's first review, new session; uses the `{{PR}}` token)
- `FOLLOWUP_PROMPT` (a PR's resumed review; uses the `{{PR}}` token)
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
| `PR_SEARCH="is:open label:x"` | `--search "…"` | PRs matching a gh search query (you control state) |

`REVIEW_PROMPT`/`FOLLOWUP_PROMPT` use a `{{PR}}` token (substituted with the PR number), and `MAX_PASSES_PER_SESSION` applies per PR.

### Linear ticket context

Set `LINEAR_API_KEY` and the reviewer also reads the Linear ticket a PR references — its description *and* its comments, where later feedback and revised requirements usually live — and raises divergence from what the ticket asked for as a finding, alongside the usual code findings. Unset, nothing about the review changes.

Get a key from **Settings → Security & access → Personal API keys**. Linear's MCP server accepts an API key straight through as an `Authorization: Bearer` header ([Linear docs](https://linear.app/docs/mcp)), so there is no interactive OAuth step and the loop stays headless.

> **Use a read-only key.** Linear lets you restrict a personal API key to `Read`. The reviewer runs with `--dangerously-skip-permissions`, so a write-capable key would let an unattended session modify your tickets. Like `GITHUB_TOKEN`, the key's scope can't be inspected from inside the container — minimizing it is on you.

The entrypoint writes the key into a generated MCP config at `$HOME/mcp.json` (mode `600`) and passes it to Claude Code with `--mcp-config`. Every review pass also runs with `--strict-mcp-config`, whether or not Linear is configured: `/repo` is untrusted input, and strict mode means a repository that ships its own `.mcp.json` can't get MCP servers of its choosing loaded into a permission-skipped session.

## Notes & caveats

- **Model names move fast, and there is no fallback.** `REVIEW_MODEL` must name a model your chosen provider actually serves; a wrong name is a hard error, not a silent fall-through to some other model. For Ollama the `:cloud` suffix is stable but exact versions change — browse [Ollama's model registry](https://ollama.com/search?c=cloud). For Anthropic, see the current model IDs in the [Anthropic docs](https://docs.anthropic.com/en/docs/about-claude/models).
- The token is the real safety boundary. Verify it has no write access beyond PR comments before running unattended.
- Because each PR is reviewed in its own resumed session, the reviewer remembers what it already flagged on that PR and won't re-raise the same findings. If a PR's pass fails it starts a fresh session for that PR next cycle (losing that in-session memory), so it may occasionally re-comment on it after a failure — harmless, just noise. The PR→session map is in-memory, so a container restart can likewise re-review each PR once.
- Each PR's session context grows over time. Set `MAX_PASSES_PER_SESSION` to rotate a PR's session to a fresh one every N passes and bound that growth (the trade-off: the new session forgets that PR's earlier passes, so it may re-raise findings once after a rotation). Left at `0`, each PR's session runs unbounded until the container restarts.
