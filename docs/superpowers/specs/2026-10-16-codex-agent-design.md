# Codex as a second review agent

Date: 2026-10-16. Status: awaiting the operator's approval.

## Why

claudebox is the operator's biggest token sink. The operator has a ChatGPT
subscription with spare capacity and several usage-limit resets to spend, one
of which expires in about a week. Moving claudebox's review passes onto that
subscription frees budget on every other provider. An OpenAI Platform API key
is ruled out because it bills per token, which defeats the point.

A ChatGPT login only works through OpenAI's own clients, so this means running
the Codex CLI as the agent in place of Claude Code. `PROVIDER` cannot do it:
every provider today is a backend for Claude Code.

Codex reviewing Claude-written code also serves claudebox's founding premise,
that a different model should review than the one that wrote the code.

## Goals

- `AGENT=codex` runs every review pass through `codex exec`, signed in with the
  operator's ChatGPT subscription, with no per-token billing.
- Ships within a couple of days. The expiring reset sets the deadline, so the
  first cut is the smallest thing that reviews PRs safely.
- Everything above the pass runner behaves as it does today: selectors, review
  modes, personas, the round ladder, the change gate, sessions and resume,
  concurrency, the `.git` lock, cut-and-owed bookkeeping.
- A Codex usage limit is classified as a limit: the pair keeps its session, the
  cycle stops at the group barrier, and the loop backs off. The operator can
  spend a reset and the next cycle carries on.
- Stays as a permanent second agent after this week.

## Non-goals (first cut)

- kindex and Linear access under Codex. Both are MCP servers, and proving that
  Codex's per-tool deny list holds under its bypass flag is the slowest
  verification in this work. With `AGENT=codex` the entrypoint logs one line
  saying each is off for this agent, and the prompts omit their stanzas. A
  second cut can add them.
- Mixing agents inside one container. `AGENT` is container-wide; run two
  containers to split work.
- Reading the reset time out of a limit error to back off precisely.
  `LIMIT_BACKOFF_SECONDS` stays the backoff.
- The hosted GitHub App. Its design assumes Claude Code and is untouched.
- Any OpenAI Platform API key path. That is `PROVIDER=openai`, a separate
  change (draft PR #24) that keeps Claude Code as the agent.

## Anti-goals

These would make it a failure even if reviews get posted:

- A reviewed repo can load MCP servers, hooks or config of its choosing into a
  permission-bypassed Codex session.
- The container shares the operator's host Codex login, so that one side's token
  refresh signs the other out (see "Login").
- A resumed pass runs without its persona.
- Codex silently switches to a different model when the operator hits a limit.
- A limit reads as an ordinary failure, so sessions are dropped and findings
  re-posted, which spends more of the exhausted quota.
- `AGENT=claude` (the default) changes behavior in any way.

## What recon established (2026-10-16, codex-cli 0.162.1, operator's Mac)

Measured with throwaway repos and the operator's ChatGPT login:

1. `codex exec --json` prints JSONL events. The session id arrives first as
   `{"type":"thread.started","thread_id":"<uuid>"}`; replies arrive as
   `item.completed` with `item.type == "agent_message"`; `turn.completed` carries
   token usage.
2. `codex exec` blocks reading stdin when stdin is open, even when a prompt
   argument is given. The pass must run with stdin closed.
3. `-c developer_instructions="<text>"` reached a fresh session, and was honored
   on `codex exec resume <id>` when passed again. Whether it survives resume
   without being passed again was ambiguous, so it is passed on every call,
   matching what claudebox already does for Claude.
4. A repo's `.codex/config.toml` declaring an MCP server **started that server**
   under an empty `CODEX_HOME`, with no trust entry for the repo. That is the
   container's situation by default. With `--ignore-user-config` it did not start.
   A direct `-c mcp_servers.<name>.command=...` positive control did start, so
   the negative result is real. `--ignore-user-config` is therefore the Codex
   counterpart of `--strict-mcp-config`, and is mandatory.
5. `--ignore-user-config` skips `$CODEX_HOME/config.toml` but still reads auth
   from `CODEX_HOME`, so all Codex settings must come from `-c` flags.
6. Repo `AGENTS.md` is read, like `CLAUDE.md` today. It is the same injection
   surface, handled the same way: nothing is added for it. The container
   boundary and the minimized token are what contain a hostile repo, under
   either agent.
7. Hooks: neither a repo `.codex/hooks.json`, a repo config hook, nor a `-c`
   hook fired in these runs, so the test was inconclusive.
   `--dangerously-bypass-hook-trust` exists, which implies hooks are gated on
   trust. It must never be passed.
8. Refresh tokens are single-use. The binary carries "your refresh token was
   already used. Please log out and sign in again."
9. Codex can switch models on a usage limit ("Automatically switched to … due
   to usage limits"). The binary has an `allow_provider_model_fallback` key.
10. The binary carries "waiting for OAuth refresh lock" and "failed to lock
    OAuth refresh lock", which suggests Codex serializes token refreshes across
    processes sharing one `CODEX_HOME`. Not yet demonstrated.
11. Limit errors include "You've hit your usage limit" (several variants, some
    with "try again at …"), and an error kind `UsageLimitReached` with
    `resets_at`.

## Design decisions

### Selection

A new variable, `AGENT` (`claude` default, `codex`). It is validated at the
existing `--check` seam, so a typo fails before the clone. Under `AGENT=codex`
the entrypoint skips the provider block entirely (no `PROVIDER` validation, no
model-tier pinning, no LiteLLM). `PROVIDER` set alongside `AGENT=codex` logs a
WARN naming it as ignored rather than failing, since a shared env file may
carry it.

`REVIEW_MODEL` keeps its meaning and is passed as `-m`. Under `AGENT=codex` it
defaults to `gpt-6.1-sol` (operator's decision). No fallback: a wrong model
name is an error, as today.

### Login

The container has its own Codex login, never the host's. Because refresh
tokens are single-use, a copied or bind-mounted host `~/.codex/auth.json` would
sign out whichever side refreshed second.

- `CODEX_HOME` in the container is a Docker named volume, `claudebox-codex`,
  mounted read-write at `/home/reviewer/.codex`. Read-write because Codex
  rewrites `auth.json` on every refresh.
- `claudebox.sh codex-login` runs `codex login --device-auth` inside the image
  with that volume attached, interactively. The operator signs in once, from any
  browser. `claudebox.sh codex-login --status` runs `codex login status`.
- `claudebox.sh run`/`test` with `AGENT=codex` attach the volume. The entrypoint
  `die`s at startup if `codex login status` does not report a ChatGPT login,
  naming `claudebox.sh codex-login` as the fix.
- One named volume per host, so one running container per host under
  `AGENT=codex`. `claudebox.sh run`/`test` refuse to start a second container
  with the volume attached, naming the one already running. *(Builder's call.)*
- Concurrent passes inside the one container share `CODEX_HOME`. Recon 10
  suggests Codex locks the refresh; verification step 6 proves it. If it fails,
  `MAX_CONCURRENT_PASSES` defaults to `1` under `AGENT=codex` (an explicit
  operator value still wins), and the doc records why. *(Builder's call.)*

### The pass under Codex

`passes.py` gains a Codex counterpart of `build_argv`, its event formatter and
its limit classifier, selected once by `AGENT`. `Supervisor`, the decision core
and everything above `run_pass` do not branch on the agent.

Invocation, fresh and resumed:

```
codex exec [resume <thread_id>] --json --ignore-user-config
  --dangerously-bypass-approvals-and-sandbox
  -m <model>
  -c developer_instructions=<persona + round stanza, TOML-quoted>
  -c allow_provider_model_fallback=false
  -c <update check off>
  -- <prompt>
```

- Run with `stdin=DEVNULL` (recon 2). The Claude path gets it too; it is
  harmless there.
- `--dangerously-bypass-approvals-and-sandbox` is the counterpart of
  `--dangerously-skip-permissions`. The container stays the boundary, as today;
  Codex's own sandbox needs kernel features `--cap-drop ALL` removes.
- `developer_instructions` carries what `--append-system-prompt` carries for
  Claude: the persona followed by the round stanza, on every call. `-c` parses
  its value as TOML and falls back to a raw string if that fails, so the value is
  always emitted as a valid TOML basic string, never left to the fallback.
- No `--mcp-config` equivalent, no MCP servers, no tool deny list (non-goal).
- The session id comes from `thread.started.thread_id`, recovered as the stream
  arrives, before the exit code is judged, exactly as `session_id` is today.
- The task prompts are unchanged, except that the kindex and Linear stanzas are
  absent because neither is wired. `GH_STANZA`, `TEST_STANZA`, `PLAN_STANZA` and
  `WORKTREE_STANZA` apply as they do under Claude.

### Limits

The Codex classifier matches recon 11's texts in the JSONL **error** events
(`error`, `turn.failed`, or whatever the error event types prove to be) and in
stderr, regardless of exit status, since whether a limit exits non-zero is not
known. It never reads `agent_message` text, so a reviewer writing about usage
limits is not misread as one; this is the Codex counterpart of the
`_API_ERROR_PREFIX` guard. A failed turn with exit status 0 counts as a failure.
A match behaves exactly like a Claude limit today. *(Builder's call.)*

`allow_provider_model_fallback=false` is what prevents a model switch, and
verification step 3 proves it is accepted. If a switch is still announced in
the event stream, the pass is not killed (nothing is killed, ever): it is
recorded as a **success**, since its comments are already posted and re-running
it would re-post them; it logs a WARN naming both models; and the cycle ends at
the group barrier with the limit backoff, so no further pass runs on the
substitute model. *(Builder's call.)*

### Image

`@openai/codex` is installed from npm beside `@anthropic-ai/claude-code`,
pinned by a build arg, root-owned like the other tool installs. Its update check
is disabled, as `DISABLE_AUTOUPDATER=1` does for Claude.

### Docs and tests

- `test-python.sh`: unit tests for the Codex argv (fresh and resumed both carry
  the persona, `--ignore-user-config` and the fallback-off flag; no
  `--dangerously-bypass-hook-trust`), the event formatter, session-id recovery,
  and the limit classifier against recon 11's texts.
- A bash suite case, in the style of `test-personas.sh`, with a stubbed `codex`
  over two cycles. Its central assertion is that the resumed invocation still
  carries `developer_instructions` and `--ignore-user-config`.
- README, `.env.example`, `CLAUDE.md`, `HISTORY.md` updated. `CLAUDE.md` records
  recon items 4 and 8 as load-bearing gotchas.

## Verification before it runs unattended

The plan must include, in the built image with the real login:

1. Repeat recon 4 inside the container: a reviewed repo's `.codex/config.toml`
   MCP server does not start.
2. Repeat recon 7 with a hook that provably fires when trusted, then show a repo
   hook does not fire. If no repo hook can be shown not to fire, stop and bring
   it back.
3. Confirm `allow_provider_model_fallback=false` is accepted, and that
   `--strict-config` does not reject the `-c` set.
4. One real `claudebox.sh test` against a PR, reviewing it to completion and
   posting signed comments, then a second cycle that resumes.
5. Confirm a host `codex` session still works after the container's login has
   refreshed at least once.
6. Run a group of four concurrent passes across a token refresh (force one by
   backdating `last_refresh`/the access token expiry in the volume's copy) and
   confirm every pass succeeds and the login survives. If not, apply the
   `MAX_CONCURRENT_PASSES` fallback under "Login".

## Left to the builder

- Event-formatter detail: which item types get a log line, and how.
- How the agent is selected inside `passes.py` (module split or functions).
- Exact names of the launcher subcommand flags, within the shape above.
- The pinned Codex version.

## Decided by the operator

- Default model under `AGENT=codex`: `gpt-6.1-sol`.
- The first cut ships without kindex and Linear.
- Built on its own branch off `main`, independent of `PROVIDER=openai`.
