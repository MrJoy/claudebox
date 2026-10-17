# kindex and Linear under AGENT=codex

Status: design, approved in conversation 2026-10-16. Amends the non-goals of
`2026-10-16-codex-agent-design.md`.

## Intent

Codex should reach parity with Claude as a reviewer, for the operator and
for anyone else running claudebox. The first Codex cut shipped without
kindex and Linear because both are MCP servers. The verification it put off
was whether Codex's per-tool filtering survives its permission-bypass flag.
That has now been measured, and it does.

From the operator's side nothing new appears. Under `AGENT=codex`, the
launcher's kindex flags, the snapshot, the read-only tool set, vector search
and the prompt stanzas all behave as they do under Claude, and so does
`LINEAR_API_KEY`.

## Goals

- `AGENT=codex` gets the kindex snapshot through kin-mcp, limited to
  `kindex_tools.READ_TOOLS`, with vector search when `VOYAGE_API_KEY` is set.
- `AGENT=codex` gets Linear's MCP server when `LINEAR_API_KEY` is set.
- The kindex and Linear stanzas go back into the default prompts under Codex,
  on the same conditions as under Claude.
- No credential reaches Codex's own environment, and so none reaches the
  shells Codex runs.

## Non-goals

- Changing the Claude arm. (It still exports `LINEAR_API_KEY` into every
  pass's environment. This design does better than that under Codex and
  leaves Claude alone.)
- A tool filter on Linear. Claude has none; both rely on a read-only key.
- Investigating the `github.com/openai/plugins.git` clone Codex makes into
  `$CODEX_HOME/.tmp` on startup, observed during the measurements below,
  even under `--ignore-user-config`. It predates this change. Record it as a
  question.

## Anti-goals

- Any credential on a `codex` argv. `-c mcp_servers.kindex.env.VOYAGE_API_KEY=...`
  would be the obvious spelling, and `ps` would show it.
- Any credential in Codex's environment through `env_vars` or
  `bearer_token_env_var`. Measured: Codex's default `shell_environment_policy`
  hands both to every shell it runs.
- A helper or wrapper the reviewer can edit. Codex runs these on every pass,
  so a reviewer-writable one lets one prompt-injected pass own every later
  pass, the same reasoning behind the root-owned `reviewer/` copy.

## Measurements (codex-cli 0.162.1, 2026-10-16)

None of these needed a ChatGPT login. Codex starts its MCP servers and sends
the model request even when unauthenticated, so pointing it at a local fake
Responses API (`-c model_provider="probe"`,
`model_providers.probe.base_url="http://127.0.0.1:PORT/v1"`,
`model_providers.probe.wire_api="responses"`) shows exactly what it
advertises, and a scripted fake can emit any function call. Linear was stood
in for by a local streamable-HTTP MCP server that logs headers. Every run used
the real flags `codex_pass.build_argv` passes plus `--strict-config`, which
accepted every key below.

1. **`mcp_servers.<name>.enabled_tools` is enforced twice under
   `--dangerously-bypass-approvals-and-sandbox`.** The tool list sent to the
   model held exactly the allowed names, as a `namespace` tool called
   `mcp__kindex`. Forged calls to `add` in namespace form, as the flat name
   `mcp__kindex__add`, and as bare `add` each came back `unsupported call`,
   and nothing landed in the store. The same held for a filtered tool on the
   HTTP server. Allowed tools ran normally.
2. **Subagents inherit the filter.** A child spawned through
   `multi_agent_v1.spawn_agent` was advertised the same 18 kindex tools, and
   its forged `add` came back `unsupported call`.
3. **`env_vars` and `bearer_token_env_var` leak.** With the keys passed that
   way, `env` inside `exec_command` printed `LINEAR_API_KEY` and
   `VOYAGE_API_KEY`.
4. **`http_headers_helper` works.** It is a string naming a command. A helper
   that prints the JSON object `{"Authorization":"Bearer <key>"}` got that
   header onto every request to the server. It ran once per session.
5. **A stdio wrapper keeps the key out of Codex.** With `command` naming a
   wrapper that reads the Voyage key from a mode-600 file and execs kin-mcp,
   the shell's `env` showed neither key, and a `/proc/*/environ` scan found
   the key only in kin-mcp. That matches the Claude arm's documented exposure.
7. **The helper string is split into argv.** `http_headers_helper="python3
   /probe/hdr.py ok"` ran with `argv=['/probe/hdr.py', 'ok']`, so one module
   with subcommands serves both helpers.
8. **The helper works against a non-loopback server.** Pointed at
   `https://mcp.linear.app/mcp` with a fake key, the helper ran and Linear
   answered `invalid_token`, so the header reached it. The binary's string
   "http_headers_helper is only supported for local MCP servers" does not
   refer to the URL's host.
9. **A server that fails to start is skipped by default.** A stdio command
   that exits at once, and a header helper that exits 1, each left the pass
   running to completion with rc 0, the tools absent, and only an `ERROR`
   line on stderr. With `required=true` on that server, the pass itself
   failed (rc 1, "required MCP servers failed to initialize"). A failed
   stdio server writes nothing to stderr at Codex's default log level; a
   failed header helper writes an `ERROR ... MCP HTTP headers helper exited`
   line.
10. **Non-required servers lose a race on the first request.** Codex waits for
    a server's initialize handshake but not its `tools/list`, so the first
    model request of every pass carried no tools from it (kin-mcp started
    directly, 18 tools allowed: `[0, 18, 18]` across three requests). A
    server slower than the turn never appeared. `startup_readiness`
    (`"connection"` or `"catalog"`) and `startup_timeout_sec` changed
    nothing. With `required = true` the first request carried all 18.
6. **MCP resources go around `enabled_tools`.** `list_mcp_resources` and
   `read_mcp_resource` reach every server's resources. Claude's resource tools
   do the same. kin-mcp's four resources (`kindex://status`,
   `kindex://node/{id}`, `kindex://recent`, `kindex://orphans`) are all reads.

Not measured: a real model doing a real review with these tools, and the real
`https://mcp.linear.app/mcp`. The live login volume was in use by a running
reviewer, and mounting it into a probe container was refused. The operator's
`claudebox.sh test` under `AGENT=codex` is the acceptance check for both.

## Design

### Credentials

The entrypoint, under `AGENT=codex`, writes each configured key to its own
file in a mode-700 directory under `$HOME` (mode 600 files, written with
`umask 077`, the value never on an argv), then unsets the variable. It takes
the place of `mcp.json`, which Codex has no use for. `voyage_api_key` keeps
its current handling (unset at the top of the file, held in a shell
variable) and is written from there. `LINEAR_API_KEY` is unset after its file
is written, which is the change from today's Codex arm where it is unset and
discarded.

A stale directory from a previous run is removed before deciding what to
write, for the same reason the stale `mcp.json` is: a run without a key must
not inherit the last run's.

### Helpers

One stdlib module in `reviewer/` (root-owned already, per the `COPY` line
that omits `--chown`) with two entry points, invoked as
`python3 /opt/claudebox/reviewer/<module>.py <subcommand>`:

- **kindex**: sets `KIN_PROFILE=claudebox`, adds `VOYAGE_API_KEY` from its
  file when the file exists, and `os.execv`s kin-mcp, so kin-mcp's pid and
  environment are what they are under Claude.
- **linear-headers**: prints `{"Authorization": "Bearer <key>"}` from the
  Linear key file and exits 0, or exits non-zero with a message on stderr
  when the file is missing or empty.

Both take paths from their environment or arguments only as far as the
builder finds tidy; the paths themselves are fixed by the entrypoint.

### Flags

`main()` builds the Codex MCP flags once, the way it builds `mcp_args` for
Claude, and hands them to `Supervisor`. `codex_pass.build_argv` splices them
into every pass, fresh and resumed, before the `--`. Per server:

- kindex, only when `KINDEX_ENABLED=1`: `command` set to `python3`, `args`
  naming the helper, `enabled_tools` set to `sorted(READ_TOOLS)`, and
  `required = true`. The
  allowlist is native here, so `tools.txt` is not consulted on this arm.
  The Dockerfile's existing check that every `READ_TOOLS` name still exists
  keeps covering a renamed tool.
- linear, only when the Linear key file exists: `url` set to
  `https://mcp.linear.app/mcp`, and `http_headers_helper` naming the helper.

Every value goes through `codex_pass.toml_string`, or an equivalent TOML
array builder, for the reason `developer_instructions` already does: `-c`
falls back to raw text on invalid TOML.

The comment at `review_loop.py:579` that says no MCP flags are passed under
Codex goes.

### A server that fails to start, and the first-request race

Measurement 10 changed the operator's first answer ("carry on without")
into a split, decided by the operator 2026-10-17:

- **kindex is `required = true`.** Codex then waits for kin-mcp's tool list,
  so the persona has the graph from its first request. kin-mcp is local and
  the supervisor has already opened and checked the snapshot before cycle 1,
  so a failure to start is a fault in the image, and failing the pass makes
  it loud: the existing WARN tail carries "required MCP servers failed to
  initialize: kindex", and three in a row back the cycle off like any other
  dead dependency.
- **Linear is not required.** It is remote, and an outage there must not
  fail reviews. Its tools are missing from each pass's first request and
  present after. When its header helper fails, Codex prints an `ERROR` line
  on stderr at its default log level; `codex_pass` echoes the first line
  naming an MCP failure into the pass log on a pass that otherwise
  succeeded, truncated to 400 characters as `passes.usage_limit_line` does.

### Prompts

`prompts.linear_stanza` keys on `LINEAR_API_KEY` in the supervisor's
environment, which the entrypoint now unsets under Codex. The builder picks a
signal that survives the unset and that only the entrypoint sets, in the
spirit of `KINDEX_ENABLED`. `kindex_stanza` needs no change once the
entrypoint stops skipping `KINDEX_ENABLED=1` under Codex.

### Entrypoint

- Drop the two "not available under AGENT=codex yet" lines and their
  `unset`/skip.
- Under Codex, write the key directory in place of `mcp.json`, log the same
  "Linear MCP enabled" / "kindex MCP enabled" / vector-search lines the
  Claude arm logs, and export what the supervisor needs to find the files.
- The kin.yaml write and the snapshot are already agent-neutral once the
  skip is gone.

### Tests

- `test-python.sh`: the Codex argv with and without each server, TOML
  quoting of the values, `enabled_tools` matching `READ_TOOLS`, both helpers
  (key present, absent, empty), and the stanza conditions under Codex.
- `test-providers.sh` (or whichever suite already drives the Codex
  entrypoint arm): under `AGENT=codex` with a store and both keys, the key
  files exist with the right modes, neither key is in the environment handed
  to `codex`, and no key appears on its argv.

### Docs

- Codex spec: move kindex and Linear out of the non-goals, pointing here.
- CLAUDE.md: the Codex section's mentions of Linear and kindex being off,
  the "Read-only kindex access" and "Optional Linear context" sections where
  they say how the server is wired, and a gotcha that the helpers must stay
  root-owned and that Codex keys must never travel through `env_vars` or
  `bearer_token_env_var`.
- README and `.env.example`: wherever they say kindex or Linear is
  unavailable under Codex.
- kindex: a question node on the plugins clone.

## Left to the builder

- The helper module's name and the key directory's path.
- The signal `linear_stanza` uses under Codex.
- How `Supervisor` carries the Codex flags (a second list beside `mcp_args`,
  or one field whose meaning depends on the agent).
- Which bash suite gets the entrypoint assertions.

## My calls (not the operator's)

- A Python helper in `reviewer/` instead of new shell scripts, to inherit
  the root-owned `COPY` without a new Dockerfile line and stay stdlib-only.
- `enabled_tools` alone, without a `disabled_tools` belt. It failed closed in
  every measurement, and `disabled_tools` would need `tools.txt` for no gain.
- No Linear tool filter, for parity with Claude.
- Echoing Linear's MCP startup error into the pass log (making a skipped
  server visible was my addition to the operator's split).
