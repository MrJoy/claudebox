# claudebox — autonomous PR reviewer

A Docker image that runs a hands-off pull-request reviewer. It bundles the
**Claude Code CLI** and the **GitHub CLI**. Claude Code talks directly to
**Ollama Cloud** (via its native Anthropic-compatible API — no proxy, and no
local `ollama` binary needed) and runs in non-interactive "YOLO" mode on a loop,
reviewing open PRs and posting findings as comments.

It is designed so the loop *cannot cause damage*:

- Runs as an **unprivileged user** inside the container.
- Makes a cheap **local clone** of a **read-only** mount of your primary repo and
  works only in that clone, so your source is never modified.
- Uses a **privilege-minimized GitHub token** that can read the repo/PRs and
  write PR comments — nothing else (no push, merge, or admin).

## How it works

The image points Claude Code at Ollama Cloud with these environment variables
(set automatically by the entrypoint):

| Variable | Value |
|---|---|
| `ANTHROPIC_BASE_URL` | `https://ollama.com` |
| `ANTHROPIC_AUTH_TOKEN` | your `OLLAMA_API_KEY` |
| `ANTHROPIC_API_KEY` | `""` (blanked to avoid conflicts) |
| `ANTHROPIC_MODEL` + every tier (`..._DEFAULT_OPUS/SONNET/HAIKU_MODEL`, `..._SMALL_FAST_MODEL`) | your `REVIEW_MODEL` |

All model tiers are mapped to the one Ollama model on purpose: the backend has no
Anthropic models, so if a subagent or alias requests Opus/Sonnet/Haiku and that
tier weren't overridden, Claude Code would error out on an unknown model.

The reviewer runs as **one continuous, stateful Claude session**. The first pass
starts a new session; every later pass `--resume`s it, so Claude remembers what
it already reviewed and avoids duplicate comments:

```
# first pass — new session, capture its id
claude -p --output-format json --dangerously-skip-permissions \
  --model "$REVIEW_MODEL" "$REVIEW_PROMPT"
# later passes — resume the same session
claude -p --resume "$SESSION_ID" --output-format json \
  --dangerously-skip-permissions --model "$REVIEW_MODEL" "$FOLLOWUP_PROMPT"
```

The entrypoint shell is the supervisor: it controls cadence (`git fetch`, then a
review pass, then sleep) and relaunches a fresh session if a pass fails. Claude
itself uses `gh`/`git` to enumerate open PRs, check out the latest commits, and
post one comment per finding.

> Why not `/loop`? Claude Code's `/loop` needs a live *interactive* session —
> scheduled wake-ups only fire while a session is running and idle, and headless
> `-p` mode exits after each response. The shell loop + `--resume` gives the same
> continuous, context-retaining behavior while staying headless and crash-safe.

## Build

```bash
docker build -t claudebox .
```

## Run

1. Create the GitHub token with **only** these permissions (fine-grained token, scoped to the target repo):
   - Contents: **Read**
   - Pull requests: **Read and write** (read PRs/diffs, post comments)
   - Issues: **Read and write** (PR comments use the issues API)
2. Get an Ollama Cloud API key: https://ollama.com/settings/keys
3. Fill in env vars (copy `.env.example` to `.env`):

    ```bash
    cp .env.example .env   # then edit
    ```

4. Run, mounting your **primary repo read-only** at `/repo`:

    ```bash
    docker run --rm \
      --env-file .env \
      -v /path/to/your/repo:/repo:ro \
      --cap-drop ALL \
      --security-opt no-new-privileges \
      --pids-limit 512 \
      --memory 4g \
      claudebox
    ```

On startup the reviewer makes a cheap **local clone** of `/repo` into its own
writable working dir — it reuses the local git object store, so no objects are
downloaded over the network, and your repo is never written to. It then repoints
`origin` at GitHub and fetches only the new PR refs each cycle.

The mount is optional: if you omit it, the reviewer does a full network clone of
`GITHUB_REPOSITORY` on startup. Mounting your primary repo just avoids that
initial download.

> **Note:** mount the *primary* repo, not a `git worktree` of it. A worktree
> keeps its objects in the parent repo and only holds a link back to it, so a
> worktree mounted on its own is structurally unusable inside the container.

### Recommended hardening

Because the loop runs unattended in YOLO mode, run the container locked down:

```bash
docker run --rm \
  --env-file .env \
  -v /path/to/your/repo:/repo:ro \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 512 \
  --memory 4g \
  claudebox
```

(Don't add `--read-only` to the root filesystem: the loop needs to write its
working copy under the user's home.)

## Configuration

All configuration is via environment variables — see `.env.example`. Required:

- `OLLAMA_API_KEY`
- `GITHUB_TOKEN`
- `GITHUB_REPOSITORY`

Optional:

- `REVIEW_MODEL`
- `REVIEW_INTERVAL_SECONDS`
- `REVIEW_PROMPT` (first pass, new session)
- `FOLLOWUP_PROMPT` (resumed passes)
- `MAX_PASSES_PER_SESSION` (rotate to a fresh session every N passes; `0` = never)

## Notes & caveats

- **Model names move fast.** The `:cloud` suffix is stable, but exact versions change. Browse current options at [Ollama's model registry](https://ollama.com/search?c=cloud) and set `REVIEW_MODEL` accordingly. If the model name is wrong, Claude Code errors out — it does **not** fall back to Anthropic's API.
- The token is the real safety boundary. Verify it has no write access beyond PR comments before running unattended.
- Because passes share one resumed session, the reviewer remembers what it
  reviewed earlier and won't re-raise the same findings. If a pass fails it
  starts a fresh session next cycle (losing that in-session memory), so it may
  occasionally re-comment after a failure — harmless, just noise.
- Session context grows over time. Set `MAX_PASSES_PER_SESSION` to rotate to a
  fresh session every N passes and bound that growth (the trade-off: the new
  session forgets earlier passes, so it may re-raise findings once after a
  rotation). Left at `0`, the session runs unbounded until the container restarts.
