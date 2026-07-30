# Linear MCP passthrough — design

**Date:** 2026-07-30
**Status:** approved, ready for implementation plan

## Problem

Outside the container, the host has a Linear MCP server configured (project-scoped,
`{"type":"http","url":"https://mcp.linear.app/mcp"}`, authenticated interactively via OAuth).
Inside claudebox neither that configuration nor its credentials exist, so the unattended
reviewer cannot see the ticket a PR claims to implement. The host OAuth token lives in the
macOS Keychain, not a file, so `--mount-claude` does not carry it in.

## Decisions

1. **Linear-specific, not a generic MCP passthrough.** One env var, no new launcher flags,
   no generic MCP plumbing to document. (Generic passthrough was considered and rejected as
   unneeded surface; the generated-config mechanism below would make it easy to add later.)
2. **API key, not OAuth.** Linear's MCP server accepts `Authorization: Bearer <token>` with a
   Personal API key instead of the interactive flow (<https://linear.app/docs/mcp>), so the
   container stays fully headless. No Keychain extraction, no in-container login step,
   no persistent-volume state.
3. **Read-only key is the security boundary.** Linear supports restricted API keys with only
   `Read` permission. This is the direct analogue of the privilege-minimized `GITHUB_TOKEN`
   (safety boundary 3): in YOLO mode a write-capable key would let the unattended reviewer
   mutate tickets. Docs must state this plainly. It is a documentation boundary — the key's
   scope cannot be introspected from inside the container, exactly like the GitHub token.

## Design

### 1. Configuration

`LINEAR_API_KEY` (optional) in the env file. Credentials already reach the container via
`--env-file`, so `claudebox.sh` needs **no** change.

Unset → no MCP config file, no MCP flags beyond `--strict-mcp-config`; behavior otherwise
identical to today.

Set → at startup `entrypoint.sh` writes a generated config to `$HOME/mcp.json` with mode
`600`, and never logs the key:

```json
{
  "mcpServers": {
    "linear": {
      "type": "http",
      "url": "https://mcp.linear.app/mcp",
      "headers": { "Authorization": "Bearer <LINEAR_API_KEY>" }
    }
  }
}
```

Both `claude -p` invocations in `run_pass()` (the new-session form and the `--resume` form)
gain `--mcp-config "$HOME/mcp.json"`. The flags are assembled once into an array so the two
call sites cannot drift.

### 2. `--strict-mcp-config` unconditionally

Passed on every `claude -p` call whether or not Linear is enabled.

Rationale: `/repo` is untrusted input. If a reviewed repository ships a `.mcp.json`, the
combination of project-config discovery and `--dangerously-skip-permissions` means the
container could launch MCP servers chosen by the code under review — a hole in safety
boundary 1. Strict mode makes the container load *only* the config we generate, or none.

### 3. Prompt stanza

When `LINEAR_API_KEY` is set, a Linear stanza is appended to `DEFAULT_PROMPT` and
`DEFAULT_FOLLOWUP` **before** the `${REVIEW_PROMPT:-$DEFAULT_PROMPT}` /
`${FOLLOWUP_PROMPT:-$DEFAULT_FOLLOWUP}` fallbacks are resolved. An explicit
`REVIEW_PROMPT`/`FOLLOWUP_PROMPT` override is therefore left exactly as the operator wrote it.

The stanza instructs the reviewer to:

- find any Linear ticket referenced in the PR title, body, or branch name, and look it up
  through the Linear MCP tools;
- read the ticket description **and its comments** — comments carry later feedback, scope
  changes, and requirement revisions that the description does not;
- judge the diff against what the ticket actually asks for, and raise divergence from the
  stated requirements or acceptance criteria as a finding, in the same one-comment-per-finding
  form as other findings;
- proceed with the normal review if no ticket is referenced or the reference cannot be
  resolved — a missing ticket is not itself a finding.

## Non-goals

- Generic multi-server MCP config passthrough.
- Reusing the host's MCP OAuth tokens (Keychain extraction) or a one-time in-container OAuth
  flow with persistent state — both rejected in favor of the API key.
- Enforcing the read-only scope of the Linear key from inside the container (not possible).
- Any change to `claudebox.sh`.

## Verification

- `bash -n entrypoint.sh`.
- With `LINEAR_API_KEY` unset: `claudebox test` runs a review as today; no `mcp.json` written;
  `--strict-mcp-config` present.
- With a read-only key set: `mcp.json` exists with mode `600`; the key does not appear in
  `claudebox logs`; a review of a PR whose title references a ticket shows Linear MCP tool
  calls in the streamed log.
- With an explicit `REVIEW_PROMPT` override plus a key set: the prompt sent to Claude is the
  override verbatim, with no Linear stanza appended.
