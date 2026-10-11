# Hosted Phase 2a: Overview and Cross-Cutting Decisions

This is the index for Phase 2a of `docs/superpowers/specs/2026-10-09-hosted-github-app-design.md` ("Phase 2a: the trial pipeline"). Phase 2a is too large for one plan, so it ships as six stacked sub-plans. Each one is a factory run of its own and produces something testable without the ones after it. This file holds what more than one sub-plan depends on: the split, the decisions the spec leaves open, the module map, the records schema, and the three wire protocols. A sub-plan restates the parts it uses, so an executor never needs this file open next to their task. If the two ever disagree, this file wins, and the sub-plan is the one to fix.

**Spec:** `docs/superpowers/specs/2026-10-09-hosted-github-app-design.md`. The founder rulings of 2026-10-09 still bind: hosted files are JSON for now, and `config validate` runs inside the image.

**Prerequisite:** Phase 1 (`feat/hosted-phase1`, PR #17) is merged or is the base of the stack. 2a reads repo and deployment files through `config_load`, `repo_config` and `deploy_config`.

## What 2a ends with

The spec's own words: "At the end an org can create an App, install it, and get reviews from a laptop on repos whose contributors it trusts, and no worker holds the org's provider credential."

## The split

| Sub-plan | Builds | Depends on |
|---|---|---|
| 2a.1 foundations | `hosted/crypto.py`, `records.py`, `creds.py`, `blobs.py`, `scan.py`, `github_app.py`: libraries with no process of their own | Phase 1 |
| 2a.2 gateway | `hosted/gateway.py`, `gwproto.py`, the provider route for `ollama`, `anthropic` and `custom`, per-token records, per-pair caps; `deploy_config` gains `base_url` and `auth` | 2a.1 |
| 2a.3 worker and job runner | `worker/` (root entrypoint with `setpriv`, wrapper, `worker.py`, the `gh` shim, transcript placement for `--resume`), `hosted/runner.py` and `runnerd.py` (local Docker) | 2a.1, 2a.2's token shape |
| 2a.4 control plane | `hosted/control.py`, `scheduler.py`, `channel.py`, `commit.py`, `outbox.py`, `checkruns.py`, `supervisor.py`, `/healthz`, `hosted/Dockerfile`, `hosted/compose.json` | 2a.1–2a.3 |
| 2a.5 operator surface | `hosted/admin.py`, `hosted/hosted_cli.py`, and in `claudebox.sh`: `hosted deploy --target local [--admins FILE]`, `app create --target local`, `hosted whois`, `hosted login`, `hosted status`, `hosted config apply` | 2a.4 |
| 2a.6 the remaining providers | `cloudflare` (all three upstreams) and `workersai` behind the gateway, with LiteLLM and `workersai-shim.py` moved into the control-plane image | 2a.2, 2a.4 |

The first end-to-end review happens at the end of 2a.5, with an `ollama`, `anthropic` or `custom` profile. 2a.6 completes the provider list. Splitting it off keeps 2a.2 to the three Anthropic-shaped types, and those three need nothing but header rewriting.

## Decided here

The spec leaves these open, and each one changes code. They are this overview's answers, for the founder to ratify. Every sub-plan treats them as Global Constraints.

1. **Pair state stays in memory in 2a.** The spec puts "the change gate against SQL state, the commit point and every outcome row" in 2b. So in 2a the control plane holds one `MemoryStateStore` and one `Decider` per repo, as local mode does. Persisting part of that state (fingerprints and rounds without sessions) is consequence 5, the known corruption shape, and persisting all of it is 2b's work. Transcript blobs are encrypted and written to the blob store as the spec says. The in-memory pair state references them, and at startup the control plane deletes every transcript blob, since nothing references them any more. Consequence: a control-plane restart re-reviews every open PR once per persona, which is exactly what a local-mode restart costs today. The trial-phase docs say so.
2. **2a's outcomes are `ok`, `capped`, `usage-limited`, `deferred`, `failing`, `failed` and `revoked`.** Each comes from the spec's commit-point order and outcome table. Two gateway records the spec does not name are mapped here, in the order's existing slots. A record with `usage_missing` set is `capped`: a provider-side stop that waits on a person, pages, and keeps the transcript. The gateway has already failed that pair closed, and without this mapping the order would fall through to `ok`. A record carrying `first_abandoned`, with no failure, is `deferred`: the pass was cut off, so it is neither a success nor the provider's fault, and the pair runs again. A record whose only upstream answers are rejected requests (`first_rejected`, with no success and no failure) is `failing`: the requests were the pair's own, so it parks until its head moves or someone asks, and no profile counter moves. `budget` waits for budgets, `too large` for clone measurement at the read route, and `refused read` for the read route itself, which are 2b's in the spec's build order. Their check-run states are not emitted until then.
3. **No webhook receiver in 2a.** 2a's only target is local Docker, and the spec says "For `--target local` the manifest leaves the webhook inactive and the poll does the work." Webhooks arrive with the first public target (phase 4), and so does the lock-holder's "503 to webhooks" behavior. `timing.poll_seconds` defaults to 60, as Phase 1 already decided.
4. **Cryptography comes from Debian's `python3-cryptography`, in the control-plane image only.** The standard library has neither RS256, which the GitHub App JWT needs, nor an AEAD cipher, which encrypting blobs with the deployment key needs. The package is installed with `apt` from bookworm, so the image still runs no `pip install` for claudebox's own code, and it sits behind `hosted/crypto.py`, the one module that imports it. `reviewer/` and the worker stay standard-library only, as CLAUDE.md requires. The host's test run does not have the package, so `crypto.py`'s own tests skip there, and `hosted/Dockerfile` runs them at build time, which makes an image whose crypto is broken fail to build (the same technique the main `Dockerfile` uses for LiteLLM's import). Everything else that encrypts or signs takes a `Sealer` or `Signer` argument, and the host tests pass a fake.
5. **The Docker socket belongs to a third process, `runnerd`, never to the control plane.** The local job runner needs the Docker socket, which is root on the Docker host. The control plane parses repo-authored config and holds the App key, so it must not also hold host root. `runnerd` runs as its own uid under the supervisor and listens on a unix socket shared only with the control plane's group. It accepts four requests, each validated field by field: `start` (job id, bootstrap token, channel URL, gateway URL), `list`, `stop` (job id) and `stop_all`. It builds the `docker run` argv itself from a fixed template. The control plane can make it start a worker, and nothing else.
6. **A worker starts as root on every target, local included, and establishes its own posture with `setpriv`.** On local Docker the runner passes `--cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add SETPCAP --security-opt no-new-privileges --pids-limit 512 --memory 4g --user 0`. `setpriv --reuid` needs the first two capabilities, and emptying the bounding set needs the third. `no_new_privs` does not stop a process that holds `CAP_SETUID` from changing uid. After `setpriv`, every capability is gone, and the image's existing hardening checks prove it, unchanged. This keeps one code path on every target, which is how a target that cannot provide the posture "fails its first job loudly". It departs from the spec's "The local Docker job runner keeps passing the flags as well": the runner passes them with three capabilities re-added, and `setpriv` spends those before the reviewer runs.
7. **The worker image is today's `claudebox` image with `worker/` added.** It is started with `--entrypoint /opt/claudebox/worker/entrypoint.sh`. The control-plane image is new: `hosted/Dockerfile`, on `debian:bookworm-slim`, with `python3`, `python3-cryptography`, `ca-certificates`, `git` and the Docker CLI (for `runnerd`). As in the main image, `COPY reviewer/ hosted/` stays root-owned.
8. **2a.2's provider types are `ollama`, `anthropic` and `custom`.** `deploy_config` gains two profile keys these need: `base_url` (required for `custom`, refused elsewhere) and `auth` (`anthropic`: `api_key` or `oauth_token`, default `api_key`; `custom`: `bearer` or `x_api_key`, required; refused on `ollama`). Today's `custom` arm reads both from the environment, and a hosted profile has no environment. The loader has no other place to learn whether a credential is an OAuth token or an API key, and a prefix guess is the kind of inference the spec refuses. Until 2a.6, `cloudflare` and `workersai` profiles load in `config validate` and are refused by `hosted config apply` with "not served by the hosted gateway until phase 2a.6".
9. **Spike S3: an OAuth-token profile hands the worker its pair token as `CLAUDE_CODE_OAUTH_TOKEN`.** This was measured on 2026-10-10 with claude 2.1.296 against a local echo server and a dummy token. With that variable set, Claude Code honors `ANTHROPIC_BASE_URL` and sends `Authorization: Bearer <value>` with `oauth-2025-04-20` in `anthropic-beta`. It does not check the value's format, and an unprefixed pair token goes out the same way. So for an `oauth_token` profile the gateway swaps only the bearer value, and every other header already matches a real Claude Code OAuth request. Every other profile gets the token as `ANTHROPIC_AUTH_TOKEN`, and the gateway sets the upstream's credential header. Not verified offline: that Anthropic accepts the forwarded request. That is a live check the human runs (2a.2's "Not run here"). Claude Code also sends `HEAD /api/hello` at startup. The gateway answers it with 200 and an empty body without contacting upstream, and refuses every other path that is not on the allowlist.
10. **Spike S8: the manifest cannot enable the device flow.** The manifest's keys are `name`, `url`, `hook_attributes`, `redirect_url`, `callback_urls`, `setup_url`, `description`, `public`, `default_events`, `default_permissions`, `request_oauth_on_install` and `setup_on_update`, and no REST or GraphQL endpoint edits an App's settings. So `app create`'s checklist carries "tick Enable Device Flow at `https://github.com/organizations/ORG/settings/apps/SLUG`". Until that box is ticked the device-flow endpoints answer 400, and `hosted login` reports that answer as the unticked box.
11. **Every hosted file is JSON**, which extends the founder's ruling: the admin roles file (`ADMINS`), the compose file, the job spec, and every wire message.
12. **Until 2c, workers can reach the admin port on the deployment's Docker network.** Compose publishes the admin port to `127.0.0.1` on the host, which meets "loopback on the host". But a published port also listens on the container's network interface, where workers can reach it. Every admin call requires a GitHub user token that resolves to a role, so reaching the port gives a worker nothing it can use. 2c's egress confinement, which makes the gateway a worker's only peer, closes the reachability itself. This is stated rather than fixed, because fixing it before 2c would be 2c.
13. **The single-instance lock is `fcntl.flock(LOCK_EX | LOCK_NB)` on `<data>/control.lock`**, taken before the records store is opened for writing. The spec says "on SQLite, the file lock of a single writer". SQLite's own locks are per transaction, so a long-held lock file is how one process holds the database between transactions.
14. **"Pages" in 2a means one `PAGE:` line in the control plane's log and an entry in `hosted status`.** `/metrics` and the paging conditions are 2b's. Every page in the spec still produces its line in 2a, so 2b only adds the metric.
15. **The hosted code runs under the host's Python 3.9.** `test-python.sh` runs on the host, so nothing under `hosted/` or `worker/` may use syntax newer than 3.9: no `match`, no `X | Y` in annotations evaluated at runtime, no `datetime.UTC` (use `datetime.timezone.utc`). The images run 3.11.
16. **The decision core takes the head a pair last reviewed as an optional input, and the control plane owns the rewrite test.** This is the spec's "The decision core takes the head as an optional input". `prompts.round_stanza(mode, round, since_head=None)` names the commits after `since_head` when it is given, and keeps today's wording otherwise. `Decider.system_prompt_for(pair, since_head=None, rewritten=False)` passes it through, so local mode's prompts stay byte-identical, which `tests/fixtures/` pins. The spec widens the scope in two cases, and both are the control plane's to decide, because it composes the round line and writes the check run. (a) When the pair's last reviewed head is no longer an ancestor of the current head, the pass runs as round 1 at the `should-fix` floor, the check run says the branch was rewritten, and the pair's round count still advances. The test is `Installation.is_ancestor` (2a.1), a compare call made per dispatched pair whose fingerprint head differs from the current head. A 404 counts as rewritten. (b) A pass that answers an ask at an already-reviewed head is whole-PR at the current rung, but asks are 2b's. This lands in 2a.4, the first sub-plan that has a head to pass.
17. **Hosted prompts are today's defaults** with no Linear stanza (hosted Linear is deferred) and no kindex stanza (3a), plus one added sentence in the hosted `GH_STANZA`: "If the deployment refuses a read, stop the review and say so; do not guess at the diff." Operator prompt overrides have no hosted equivalent in v1 (spec, "Caps": "Neither do the prompt overrides or `MAX_CYCLES` in v1").
18. **The check-run wording table is decided in 2a.4**, which the spec defers to "the phase 2a spec".
19. **Hosted modules are flat, imported by plain name, like `reviewer/`.** `hosted/` and `worker/` each put themselves and `reviewer/` on `sys.path`. `tests/_path.py` adds both directories. New test modules are `tests/test_hosted_*.py` and `tests/test_worker_*.py`, discovered by the existing `test-python.sh`.

## Carried from Phase 1

Phase 1's verdict deferred four items to the Phase 2a plan (kindex watch `fdfd1cd2865b`). Each has an owner here:

- **F5, `plan.label` with an internal newline.** The loader accepts `"a\nb"` and the schema rejects it, and a label no GitHub label can equal routes every plan PR to code mode. 2a.4, when it first serves repo config: the loader refuses any whitespace other than a plain space inside `plan.label`, so the loader and the schema's `^\S(?:.*\S)?$` agree, and a test pins the agreement on the shared cases.
- **F6, the empty persona selector's message.** It tells a repo author to unset `PERSONAS`, an environment variable a hosted author cannot set. 2a.4: the hosted config path reports the repo-file key (`code.personas` or `plan.personas`) and an empty selector in its own words, and never passes `personas.resolve`'s environment wording through to a check run.
- **F7, `claudebox.sh config` ignoring `--repo` and trailing extras.** 2a.5, which edits `claudebox.sh`: `config` refuses any argument it does not take, `--repo` included, naming `--repo-name` as the likely intent.
- **The `rounds.cap` default** (4, where the spec's example shows 6). The founder kept 4 for Phase 1 and recorded an objection. 2a.4 makes it the hosted default the moment it builds a `Decider` per repo, so 2a.4's ratification asks the founder for a ruling, and the default is whatever that ruling says. It is not inherited.

## Module map

| Module | Sub-plan | One job |
|---|---|---|
| `hosted/crypto.py` | 2a.1 | `AesGcmSealer`, `Rs256Signer`; the only importer of `cryptography` |
| `hosted/records.py` | 2a.1 | SQLite records store: schema v1, per-thread connections, `tx()`, `InstanceLock` |
| `hosted/creds.py` | 2a.1 | bootstrap tokens, capabilities and provider tokens as salted-hash rows: mint, check, exchange, revoke, revoke all |
| `hosted/blobs.py` | 2a.1 | encrypted blob directory with self-describing names |
| `hosted/scan.py` | 2a.1 | secret-shape detectors, literal matching, redaction, the `-claudebox` signature |
| `hosted/github_app.py` | 2a.1 | App JWT, narrowed installation tokens, a REST client over a transport seam |
| `hosted/gwproto.py` | 2a.2 | control-plane ↔ gateway messages over the unix socket |
| `hosted/gateway.py` | 2a.2 | provider route, per-token records, caps |
| `hosted/runner.py`, `hosted/runnerd.py` | 2a.3 | job-runner interface and local Docker implementation behind its own uid |
| `worker/entrypoint.sh`, `worker/wrapper.sh`, `worker/worker.py`, `worker/gh` | 2a.3 | the worker |
| `hosted/checkruns.py` | 2a.4 | check-run states and wording |
| `hosted/outbox.py` | 2a.4 | acceptance and drain |
| `hosted/commit.py` | 2a.4 | the outcome order and what each outcome does to pair state |
| `hosted/scheduler.py` | 2a.4 | the poll, repo config, fork rule, dispatch bounds |
| `hosted/channel.py` | 2a.4 | the worker channel's HTTP handlers |
| `hosted/control.py` | 2a.4 | control-plane main: lock, startup revocation, threads, `/healthz` |
| `hosted/supervisor.py` | 2a.4 | PID 1: per-process secrets and uids, drain on `SIGTERM` |
| `hosted/admin.py` | 2a.5 | admin API: user-token auth, roles, `status`, `config apply` |
| `hosted/hosted_cli.py` | 2a.5 | the in-image half of `app create`, `whois`, `login`, `status`, `config apply` |

## Records schema, version 1

2a.1 creates every table that 2a uses. Later sub-plans add rows, and never columns. 2b brings schema version 2 with the pair-state tables. Times are integer Unix seconds, UTC. `repo` is `owner/name`, lowercased.

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- meta('schema_version', '1')

CREATE TABLE deploy_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  body BLOB NOT NULL, applied_at INTEGER NOT NULL, applied_by INTEGER NOT NULL);

CREATE TABLE credentials (
  id TEXT PRIMARY KEY,                    -- 16 hex chars, also inside the token
  kind TEXT NOT NULL CHECK (kind IN ('bootstrap', 'capability', 'provider')),
  salt BLOB NOT NULL, hash BLOB NOT NULL,
  job_id TEXT NOT NULL, persona TEXT,     -- persona only for 'provider'
  expires_at INTEGER NOT NULL, used_at INTEGER, revoked_at INTEGER);
CREATE INDEX credentials_job ON credentials (job_id);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY, installation_id INTEGER NOT NULL, repo TEXT NOT NULL,
  pr INTEGER NOT NULL, head TEXT NOT NULL, mode TEXT NOT NULL,
  profile TEXT NOT NULL, model TEXT NOT NULL,
  dispatched_at INTEGER NOT NULL, fetched_at INTEGER, deadline_at INTEGER,
  ended_at INTEGER, end_reason TEXT);

CREATE TABLE token_records (
  credential_id TEXT PRIMARY KEY REFERENCES credentials (id),
  seq INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0,
  successes INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  first_limit TEXT, first_failure TEXT, first_rejected TEXT, first_abandoned TEXT, cap_hit TEXT,
  usage_missing INTEGER NOT NULL DEFAULT 0);

CREATE TABLE check_runs (
  repo TEXT NOT NULL, head TEXT NOT NULL, run_id INTEGER NOT NULL,
  posted INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0,
  withheld INTEGER NOT NULL DEFAULT 0, not_posted INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, PRIMARY KEY (repo, head));

CREATE TABLE outbox (
  key TEXT PRIMARY KEY,                   -- job_id/persona/seq
  installation_id INTEGER NOT NULL, repo TEXT NOT NULL, pr INTEGER NOT NULL,
  head TEXT NOT NULL, body TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL);
CREATE INDEX outbox_due ON outbox (installation_id, next_attempt_at);

CREATE TABLE withheld (
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, pr INTEGER NOT NULL,
  job_id TEXT NOT NULL, persona TEXT NOT NULL, detector TEXT NOT NULL,
  salted_hash BLOB NOT NULL, redacted_body TEXT NOT NULL, created_at INTEGER NOT NULL);

CREATE TABLE withheld_counts (
  detector TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
  PRIMARY KEY (detector, day));

CREATE TABLE audit (
  id INTEGER PRIMARY KEY, at INTEGER NOT NULL, actor_id INTEGER,
  action TEXT NOT NULL, scope TEXT NOT NULL, detail TEXT NOT NULL);
```

An outbox row is deleted when it posts or drops. The posted or dropped count moves onto the `check_runs` row for its head in the same transaction. That is how "outbox items: until posted or dropped" holds while the check run keeps an honest count.

## Wire protocols

**Credentials.** Every claudebox-minted secret has the form `cbx_<k>_<id>_<secret>`. `<k>` is `b` (bootstrap), `c` (capability) or `p` (provider). `<id>` is 16 lowercase hex characters and is the row's primary key. `<secret>` is 43 URL-safe base64 characters (32 random bytes). The row stores a 16-byte random salt and `sha256(salt || secret)`, and checks with `hmac.compare_digest`. The prefix lets the scanner find a leaked claudebox credential by shape.

**Worker channel** (HTTP, JSON, `Authorization: Bearer <token>`; 2a.3 is the client, 2a.4 the server):

| Call | Token | Request | Response |
|---|---|---|---|
| `POST /channel/fetch-job` | bootstrap | `{}` | `200 {capability, job}`, where `job` is the job spec below; `401` once the bootstrap token is used or expired |
| `POST /channel/start-pair` | capability | `{persona}` | `200 {provider_token}`, or `409 {reason}`, which leaves the pair owed with its round unchanged |
| `POST /channel/post-finding` | capability | `{persona, seq, body}` | `200 {result: "queued"}` or `422 {result: "withheld" \| "too_long" \| "enough", message}` |
| `POST /channel/complete-pair` | capability | `{persona, session_id, transcript_b64, notes}` | `200 {}`; `413` for a body over 96 MiB, which ends the pair as `failing` |

**Size caps on the channel.** The spec's "the transcript to parse as a Claude Code session under a size cap" is a decoded transcript of at most 64 MiB (`TRANSCRIPT_MAX = 64 * 1024 * 1024`), so the control plane refuses a `complete-pair` body over 96 MiB before it decodes anything (base64 is 4/3 the size, plus the other fields). A refused or over-cap transcript is the spec's "a transcript that will not parse": the outcome is `failing`, the session is dropped, and the pair stays owed and parks until its head moves. Every other channel request body is capped at 1 MiB, and a `post-finding` body is also held to `caps.comment_bytes` at acceptance. The worker checks the same 64 MiB cap before sending, and reports an over-cap transcript as `failing` in its notes rather than sending it.

The job spec is `{job_id, repo, pr, head, mode, clone_url, read_token, model, auth_env, gateway_url, max_concurrent_personas, pairs: [{persona, phase, system_prompt, prompt, session_id, transcript_b64}]}`. `auth_env` is `CLAUDE_CODE_OAUTH_TOKEN` for an `oauth_token` profile and `ANTHROPIC_AUTH_TOKEN` otherwise. `session_id` and `transcript_b64` are `null` for a pair with no session. `fetch snapshot` is 3a's.

**Gateway socket** (2a.2): newline-delimited JSON over a unix socket. Each message is `{"secret": S, "op": ..., ...}`, where `S` is the per-deployment secret the supervisor hands to both processes. A message whose secret does not match closes the connection.
- Control plane to gateway, on the gateway's socket: `hello {tokens: [{credential_id, hash_hex, salt_hex, job_id, persona, model, expires_at, route: {name, provider, base_url, auth, credential, per_pair_requests, per_pair_tokens, budget_unit}, counts}]}`. Each token carries the profile it was dispatched under, so a `config apply` reaches new jobs on their own tokens, running jobs keep what they were dispatched with (the spec's "takes effect without a restart"), and a token whose profile cannot be served is refused alone. `credential` is a secret's name, and the gateway reads its value from its own environment. `counts` is the committed record. A `hello` is a full snapshot of the live tokens. A token the gateway already holds keeps whichever copy has the higher `seq`, so a `hello` never regresses a count, and a token the snapshot leaves out is dropped. The gateway serves nothing before its first `hello`, and answers every other op with `{error: "hello first", ready: false}` until one arrives. The control plane answers that reply by sending a `hello`, which is how a gateway that restarted alone gets its tokens back. Then `add_token {token}`, which never resets a token already held, `revoke {credential_ids}`, and `flush {credential_id}`, answered by `{last_seq, drained}`. `last_seq` is the last sequence number the gateway emitted for that token, which the control plane compares with the last one it committed. A gap, or `drained: false`, is the spec's incomplete flush. The gateway waits up to 50 seconds before answering, so the control plane's client must time out later than that.
- Gateway to control plane, on the control plane's socket: `report {credential_id, seq, delta: {requests, successes, input_tokens, output_tokens}, first_limit?, first_failure?, first_rejected?, first_abandoned?, cap_hit?, usage_missing?}`, answered by `{ack: seq}`. A cap hit is reported once and is terminal for the token. `first_abandoned` is a request whose response was not relayed in full, because the worker hung up or the gateway itself faulted. `first_rejected` is a 4xx the request itself caused (anything but 401, 403 and a limit), which is the worker's doing and never a provider failure.

**Runner socket** (2a.3): the same framing and secret scheme. `start {job_id, bootstrap_token, channel_url, gateway_url}`, then `list` (answered by `{job_ids}`), `stop {job_id}` and `stop_all`.

## Not in 2a

These belong to later phases, by the spec's build order. 2a never pretends to do them.
- Requested mode, asks and reactions (2b).
- The GitHub read route (2b).
- Budgets and repo shares (2b).
- Retention, erasure and the hourly sweep (2b).
- `/metrics` (2b).
- `hosted doctor` and the other admin commands (2b).
- Persisted pair state (2b).
- Egress confinement and the gateway-fronted channel (2c).
- kindex in hosted mode (3a).
- Fly and GCP (phases 4 and 5).
