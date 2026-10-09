# claudebox as a hosted GitHub App: design

**Date:** 2026-10-09
**Status:** plan, filed for review under the `plan` label

## Problem

claudebox serves one repository per container. An operator mounts that repo's
`.git`, hands over a fine-grained PAT and one provider credential, and picks
PRs with a selector. An organization that wants every PR in thirty repos
reviewed has to run thirty containers, hold thirty PATs, and edit thirty env
files to change a model.

The ask is a GitHub App that an organization creates and hosts for itself.
Installing it on a repo turns on review for that repo. Each repo chooses its
trigger mode, model, plan label, plan personas and code personas. PRs are
reviewed in parallel up to a limit. Creating the App and managing per-repo
configuration has to be easy. kindex works with either an org-wide store or a
per-repo store. Secrets need a home that no reviewed repo can reach.

The deployment targets are Fly.io (the author's), GCP orchestrated with baton
(the largest expected user), and one person's machine through a Docker VM for
trials. None of them may be baked into the design.

`claudebox.sh run --repo` keeps working exactly as it does today (C050).

## How this plan was made

The design was pinned down with [constrain](https://github.com/wandercom/constrain):
a primed brief, four rounds of understanding and seven rounds of adversarial
challenge, then synthesis. Two of its artifacts ship beside this document:

- [`constraints.yaml`](2026-10-09-hosted-github-app/constraints.yaml): 54
  boundary conditions. This document cites them as `C001` through `C054`.
- [`component_map.yaml`](2026-10-09-hosted-github-app/component_map.yaml):
  the topology, in the shape baton's `constrain` import reads, which is how the
  GCP deployment gets its first `baton.yaml`.

Where this document narrows a constraint, the document governs and the
constraint text was edited to match. The one structural narrowing is the job
unit (see "One job per PR group"), which constrain's interview assumed was one
pair.

The interview spent most of its rounds on what a prompt-injected pass could
reach, because that is the ranked worst outcome. Each round closed a hole the
last design left open, and the end state is simpler than the start: the worker
holds no credential at all and talks to exactly one peer.

## Consequences, worst first

1. A credential, private source, org kindex content or Linear text leaks
   through a PR comment, or through a path the comment scan never sees (worker
   egress, platform logs, job specs kept in platform metadata, transcripts that
   outlive a repo going public).
2. Strangers spend the model budget: fork PRs, repeated pushes, unbounded
   tokens inside one pass, a forged usage limit that parks the whole profile.
3. Junk comments: duplicate findings after a retry, repeated park notices,
   replies to people who may not command the bot.
4. State corruption, the known shape being fingerprints persisted without the
   sessions they describe.
5. Erasure that does not erase, because a backup, a snapshot or a retired key
   version still holds the content.

## Components

```
                 GitHub ── webhooks ──▶ webhook-ingress ─┐
                    ▲                                     ▼
                    │ comments, check runs,        control-plane ──▶ records-store
                    │ token minting                 │  scheduler,     blob-store
                    └───────────────────────────────┤  posting queue  secret-store
                                                    │  admin surface
                                     launcher ◀─────┤
                                        │           └─▶ gateway ──▶ providers,
                                        ▼                 ▲         GitHub reads,
                                     worker               │         Voyage
                                  ┌─────────────┐         │
                                  │ broker uid  │─channel─┘(private network only)
                                  │ reviewer uid│─2 per-job gateway tokens
                                  └─────────────┘
```

**control-plane.** One process, one instance on every target (C047). It holds
the App private key and the deployment encryption key, receives webhooks from
`webhook-ingress`, runs the reconciliation poll, reads repo config, decides
what to review, mints narrowed installation tokens, launches workers through
`launcher`, and is the only component that writes to GitHub (C003). It owns
every durable record (C023). Horizontal scaling is out of scope; a single
tenant's PR volume does not need it, and the in-memory counters below depend
on there being one.

**gateway.** A separate process in the control-plane deployment that never
loads the App key (C031). It serves three routes to workers, injecting the
real credential on each: provider, GitHub read, Voyage. It enforces per-job
caps and classifies usage limits from what upstream returned. The
Workers AI translator (LiteLLM and `workersai-shim.py`) moves behind it, so
workers only ever speak the Anthropic Messages shape.

**launcher.** An interface with three implementations: local Docker, Fly
Machines, Cloud Run Jobs (via baton). It starts a worker from a job spec that
carries an opaque job id, a capability, and the control plane's private URL,
nothing else (C008, C049).

**worker.** One container per job, two uids inside it (C006). The **broker**
is the entry process: it holds the job capability, talks to the control plane,
fetches the job, clones through the gateway, prepares the working tree, serves
the kindex MCP proxy on loopback, and spawns Claude Code as the **reviewer**
uid. The reviewer cannot read the broker's environment, files or `/proc`
entries. It holds two per-job gateway tokens (provider, GitHub read) that are
worthless outside the gateway's private network and expire with the job.

**local-supervisor.** Today's `reviewer/` loop, unchanged in behavior. It keeps
its PAT, posts directly, and keeps its Linear MCP.

### One job per PR group

constrain's interview assumed one job per (PR, mode, persona) pair. This
design launches one worker per **PR group** instead: one PR at one head, with
the personas for its mode, run the way `run_group` runs them today (phase 1
together, then phase 2). Five personas on one PR therefore cost one clone and
one container, and the shared-clone defenses already in `review_loop.py`
(`WORKTREE_STANZA`, `lock_git_dir`) carry over unchanged.

Everything durable stays per pair. Each pair has its own lease, generation,
transcript, fingerprint and round count, and the broker reports each pair's
outcome separately through `complete pair`. Isolation between personas of one
PR is not a security boundary: they read the same repo with the same
permissions.

## The worker channel

The broker talks to the control plane over the private network only (C009).
Every call carries the job capability; the control plane stores a salted hash
of it, never the raw value, and revokes it at completion, timeout, or any
access change (C007).

| Operation | Direction | What it carries |
|---|---|---|
| `fetch job` | in | personas, prompts, round lines, the PR group's pairs with their transcripts (absent for fork pairs and fresh pairs), Linear ticket material, a kindex snapshot reference, the two gateway tokens for the reviewer |
| `fetch snapshot` | in | the job's kindex snapshot, streamed |
| `post finding` | out | one comment body for one pair; blocks until posted, held, or dropped |
| `append pass log` | out | formatted stream-json chunks for one pair |
| `complete pair` | out | one pair's outcome, session id, and transcript, plus the broker's evidence (exit code, result event) |

`fetch job` returns no credential (C005). There is no general load or save:
the control plane decides every key from the job it issued, so a worker can
only ever write the transcripts of the pairs it was given.

The reviewer posts through a `gh` shim installed ahead of the real `gh` on the
reviewer's `PATH`. `gh pr comment` goes to the broker over a unix socket; the
broker relays it as `post finding` and waits. The shim waits up to 90 seconds.
Posted: it prints the comment URL and exits 0. Held or dropped: it exits
non-zero and tells the model the finding was not posted and to end its review,
so the transcript records the truth (C022). Every other `gh` call goes to the
gateway's GitHub read route with the reviewer's read token. How `gh` is aimed
at the gateway (`GH_HOST` as an Enterprise host, or a proxy) is spike S3.
Persona prompts and `GH_STANZA` keep saying `gh pr comment`.

## Triggers

Webhooks are the primary trigger. A reconciliation poll runs every few minutes
per installation to cover missed deliveries, and it is the only trigger on a
local trial with no public URL (the App is created with its webhook inactive
there).

Each repo is in one of two trigger modes:

- **`auto`.** Every PR is a candidate. The existing change gate decides when
  to re-review: the head moved, an unsigned comment arrived, or the mode
  flipped, after the settle window (C053).
- **`requested`.** A PR is a candidate only once someone with write access
  asks. Two ways to ask:
  - **Request review from the configured team** (default `claudebox`). GitHub
    will not list a third-party App in the reviewer picker, so a team stands
    in for it; the `review_requested` event names the team. This **enrolls**
    the PR: while the team request stands, the PR behaves as if the repo were
    in `auto` mode. Removing the request un-enrolls it.
  - **Comment `/claudebox review`**. This reviews the PR once at its current
    head. It does not enroll.

The write-access check calls GitHub's permission API for the comment author or
the review requester; `author_association` is not trusted (C012). A command
from someone without write access produces no review and no reply (C013), so
the command cannot be used to make the bot talk. Which App permission that API
call needs is spike S1.

### Fork PRs

A PR whose head repo is a fork and whose author lacks write access is never
reviewed automatically, in either mode (C012). A writer's ask approves exactly
one head SHA; a later push needs a fresh ask. A stranger's comment on an
approved fork PR never triggers anything on its own.

An approved fork pass runs reduced (C014): no kindex, no Linear, the repo's
`forks.profile` if one is configured, a fresh session for every pass (its
transcript was shaped by content nobody vetted, so it is discarded at commit),
and the round count carried forward. A repo can turn fork review off.

### Parallelism

PRs run in parallel. What bounds them:

- `max_concurrent_jobs` for the deployment (default 4).
- `concurrency` per provider profile, counted in passes, which is the knob
  that keeps a provider's rate limit from being hit by fan-out (C043).
- Within a job, `max_concurrent_personas` as today.

Today's rule that PRs are serialized so that usage-limit pressure is bounded
to one PR's worth of passes is replaced in hosted mode by the per-profile
cap, which bounds the same pressure directly. Local mode keeps serializing.

## Configuration

### Repo and org files

A repo's config lives at `.github/claudebox.yml` and org defaults at
`.github/claudebox.yml` in the org's `.github` repository, both read from the
**default branch only** (C001), so a PR cannot choose who reviews it. A repo
file overrides org defaults key by key. Neither may hold a secret (C002); the
schema has no field that could.

```yaml
version: 1

trigger: auto            # auto | requested
team: claudebox          # team slug whose review request counts as an ask

profile: ollama          # a provider profile defined by the deployment
model: glm-5.2:cloud     # must be one the profile allows; omit for its default

plan:
  label: plan
  personas: all          # same selector syntax as PLAN_PERSONAS today
code:
  personas: red_team,adversarial,sme,minimalist,sage
max_concurrent_personas: 0

rounds:
  cap: 6                 # after this many automatic rounds, an explicit ask is needed

forks:
  enabled: true
  profile: ollama-forks  # optional; defaults to `profile`

kindex:
  store: none            # none | org | repo
  vectors: true          # use vector search when the store and deployment support it

linear: true             # private, non-fork PRs only; needs the deployment's Linear key
```

Validation happens in two places. On every PR that changes
`.github/claudebox.yml`, the control plane posts a check run that validates
the file at the PR's head against the schema and the deployment (unknown
profile, model the profile does not allow, unknown persona, `kindex.store:
org` on a public repo), so a bad config is caught before it merges (C052).
When a config already on the default branch turns out invalid, reviews for
that repo keep using the last valid config and the control plane says so once
on the next PR it reviews.

The same file is optional in local mode: `claudebox.sh` keeps its env
variables, and reading the repo file there is deferred.

### Deployment config

The operator's file, `claudebox.deploy.yml`, lives with the deployment and
never in a reviewed repo. It defines provider profiles, caps, roles, retention
and kindex stores, and names secrets only by reference:

```yaml
profiles:
  ollama:
    provider: ollama
    models: [glm-5.2:cloud, kimi-k2:cloud]
    default_model: glm-5.2:cloud
    credential: OLLAMA_API_KEY        # name in the platform secret store
    concurrency: 8                    # passes in flight
    daily_passes: 400                 # resets 00:00 UTC
    usage: reported                   # reported | estimated
    per_job: { requests: 200, tokens: 4000000 }
  ollama-forks:
    provider: ollama
    models: [glm-5.2:cloud]
    credential: OLLAMA_API_KEY
    concurrency: 2
    daily_passes: 40
max_concurrent_jobs: 4
roles:
  operators: [some-login]
  log_readers: [some-login]
retention:
  closed_pr_days: 14
kindex:
  voyage_credential: VOYAGE_API_KEY   # optional
linear:
  credential: LINEAR_API_KEY          # optional
```

The provider types are today's `PROVIDER` arms (`ollama`, `anthropic`,
`custom`, `cloudflare`, `workersai`), and their validation moves from
`entrypoint.sh`'s `case` block into the gateway's profile loader, with
`test-providers.sh`'s cases ported alongside.

## Secrets

Platform secret mounts hold **control-plane** secrets only (C011): the App
private key, the webhook secret, the deployment encryption key, each
profile's provider credential, and the optional Voyage and Linear keys. Where
they live per target:

| Target | Control-plane secrets | Workers get |
|---|---|---|
| Local Docker | an env file, mode 600, written by setup | a container started with no env file |
| Fly.io | `fly secrets` on the control-plane app | a separate Fly app with no secrets set |
| GCP | Secret Manager, bound to the control-plane service | a Cloud Run job with no secret bindings and a service account that can only call the control plane |

The gateway reads provider, Voyage and GitHub credentials; the control plane
reads the App key, webhook secret, deployment key and Linear key. No worker
process, broker included, ever holds any of them (C005).

`CLAUDE_CODE_OAUTH_TOKEN` is supported as an `anthropic` profile credential,
because it works today and some operators will want it. It belongs to one
person's Claude subscription, and using it to review an organization's PRs may
not be what that subscription's terms allow. The setup docs say so and
recommend an API key or Ollama for an org deployment. Whether the gateway can
inject an OAuth token the way Claude Code itself sends one is spike S6.

## The gateway

Every route checks the reviewer's per-job token, and every route's limits are
counted in the control-plane instance's memory and checkpointed (C047).

**Provider route (C028, C029).** Overwrites `model` with the job's model on
every request; Claude Code asks for tier aliases on its own, and today's
design already pins every tier to one model. For Anthropic-shaped profiles it
allows `POST /v1/messages` and `POST /v1/messages/count_tokens` and nothing
else, so batches and files are unreachable. It counts input plus output
tokens per job from reported usage, clamps `max_tokens` to the remaining
budget, refuses a request whose estimated input exceeds what is left, and caps
body size. A profile marked `usage: estimated` counts from body and stream
sizes, and the docs call that cap approximate. Hitting a cap ends the pass as
`budget-exceeded`, a failure (C027).

**Usage limits (C027).** The gateway, not the worker, sees the upstream 429 or
limit body, so it classifies the limit. One limit parks only its own pair. A
whole profile parks only when jobs for two different PRs hit a limit within
the backoff window, and fork jobs never count toward that.

**GitHub read route (C004, C030).** Uses an installation token minted per job,
narrowed by GitHub to the job's repo and to read permissions, so GitHub is the
primary repo boundary. Path and GraphQL filtering is a second layer: GET only,
GraphQL queries only, no redirects followed off github.com. Per job it caps
requests (default 300) and clone bytes (default 2 GB). It tracks the
installation's remaining REST and GraphQL limits separately from response
headers and refuses worker reads once either falls below a 20% reserve, which
keeps the control plane's own visibility and permission checks working.

**Voyage route (C032).** Single-input query embeddings under a length cap,
with a per-job request cap. Whether kindex can be pointed at a Voyage base URL
is spike S4; until it can, hosted workers run kindex without vector search.

The gateway streams. It does not buffer, cache, or log any body, and it does
not retry for the client (C031).

## Posting

The control plane posts every finding through one queue per installation,
paced at no more than one comment a second and 400 an hour by default (C021).
Before posting it:

1. Re-checks the repo's visibility against GitHub (cached at most 60 seconds).
   A change revokes the job. An error or rate limit holds the finding and
   retries until the job's deadline, then drops it and parks the job
   `github-unavailable` (C017).
2. Scans the body for known secret shapes and for the literal value of every
   credential the deployment holds. A match is dropped, never posted, and
   recorded with the detector name, a salted hash, and the redacted body
   (C019). The scan covers credentials only; kindex content is governed by
   store choice and the visibility floor.
3. Adds the `-claudebox` signature if missing and enforces per-pass, per-PR
   and size caps (C020).

A 403 or 429 on the post itself (GitHub's secondary content limits) takes the
same hold path and honors `Retry-After`.

### Park notices

A pair parks when its profile's daily budget is spent or its repo's round cap
is reached. Each park event posts exactly one control-plane comment naming the
reason and the way forward: the UTC reset time for a budget park, an ask for a
round-cap park (C045). An ask does not bypass a spent budget (C043). After the
round cap, an ask runs at the round ladder's blocking-only rung (C044).
`github-unavailable` parks silently; the cause is transient and GitHub is the
thing failing.

## Visibility floor

A public repo may use only its own kindex store or none, and gets no Linear
(C015). An internal repo counts as private. A config asking for the org store
on a public repo fails the config check run, and reviews run without kindex.

The floor is a floor. A private repo on the org store can quote any org-store
content into its own PRs, and the docs say so plainly. Matching posted text
against retrieved notes is deferred; it first needs the kindex MCP server to
log what it returned.

Visibility is checked before every dispatch, and errors fail closed: no
dispatch, the pair stays owed, no round counted (C016). A repo going public,
an uninstall, removal from the installation, or deletion bumps the generation
of every pair in that repo and revokes every live capability for it (C018). A
repo going public also discards its transcripts and sessions before any further
pass; rounds and approvals survive, since they hold no content.

## State

The control plane is authoritative for everything durable. Workers are
authoritative for nothing (C023).

| Item | Store | Writer | Content |
|---|---|---|---|
| pair state: lease, generation, fingerprint, round, owed, session id, transcript ref | records | control plane | none |
| transcripts | blob, encrypted | control plane, from `complete pair` | yes |
| pass logs | blob, encrypted | control plane, from `append pass log` | yes, redacted |
| job material (Linear tickets, prompts, held findings) | blob, encrypted | control plane | yes |
| kindex snapshots | blob, encrypted | control plane | yes |
| dropped-finding records | records | control plane | redacted body |
| approvals, park events, pass-log reads, budgets, cost | records | control plane | none (logins only) |

The records store is SQLite on a volume for the local trial and Fly, with
`secure_delete=ON`, and Cloud SQL Postgres on GCP. The blob store is a
directory, the Fly volume, or a GCS bucket created with soft delete and
versioning off. The control plane encrypts every blob with the deployment key
before it reaches the blob store, on every target (C038).

### The commit point

Fingerprints, transcripts, rounds and owed pairs persist together or not at
all, because a fingerprint persisted without its session is the known bug
shape. On `complete pair` with an `ok` outcome the control plane writes the
transcript under a new immutable name, then in one transaction points the pair
at it, records the fingerprint the job was issued against, increments the
round and clears the owed entry, and only if the pair's generation is still
the one the job started from (C023). A crash between the two writes leaves an
orphan blob and the old state.

`ok` also needs evidence (C026): the job is live, the transcript parses as a
Claude Code session under a size cap with the reported session id, the broker
saw a stream-json result event, and the gateway saw at least one successful
provider response for that pair. A pass with zero provider calls is `failed`.

| Outcome | Transcript | Fingerprint | Round | Owed | Budget unit |
|---|---|---|---|---|---|
| `ok` | committed | committed | +1 | cleared | spent |
| `superseded` (stale generation) | discarded | unchanged | unchanged | unchanged | spent |
| `failed` | discarded; session dropped | unchanged | unchanged | stays | spent |
| `budget-exceeded` | discarded; session dropped | unchanged | unchanged | stays | spent |
| `usage-limited` | discarded; session kept | unchanged | unchanged | stays | spent |
| `github-unavailable` | committed, held finding marked failed | unchanged | unchanged | stays | spent |
| `revoked` (timeout, access change, restart) | discarded | unchanged | unchanged | stays | not spent |

A budget unit is spent when a pass reached the provider, which is every
outcome except `revoked` before the first provider call.

Fork pairs commit everything above except the transcript, which is never
stored.

### Leases and generations

A per-pair lease stops the control plane dispatching a second live job for a
pair, which absorbs the webhook-plus-poll duplicate and a re-ask during a
running pass (C025). An ask that arrives while a pass for an older head is
running is recorded and dispatched once the lease frees. On timeout the
control plane revokes the capability, then releases the lease, then
dispatches any retry, so a zombie worker's late calls are refused. The job
deadline defaults to 55 minutes, under Cloud Run's 60-minute request ceiling
(C048).

A control-plane restart, a deploy included, revokes every live job; their
pairs stay owed and are redispatched by the new instance (C047). Deploys
replace the instance rather than overlapping two.

### Retention and erasure

Transcripts, session state, pass logs and job material for a PR are deleted 14
days after it closes or merges (deployment-configurable), immediately when the
App loses access to the repo, and on demand through the operator CLI by repo
or PR (C039). That CLI is the erasure path for a stranger whose fork PR text
sits in a pass log or for the owner of a leaked secret. Approvals, park events
and pass-log-read audit records carry no content, are kept for a year, and
survive erasure (C041).

Erasure is immediate in the live stores and complete only when backups age
out. The docs state the window per target (C040): Cloud SQL's backup and
point-in-time recovery retention on GCP, Fly's volume snapshot retention on
Fly, and nothing beyond the live volume locally. Setup proposes the shortest
setting each target allows. A key rotation re-encrypts live blobs and then
destroys the retired key version, so the window is the shorter of backup
retention and the time to the next rotation.

Linear deletions are not tracked. Ticket text in a transcript or pass log
stays until that PR's retention ends or the erasure CLI removes it.

### Logs

Worker and control-plane stdout carry job ids, lifecycle phases, outcomes and
timings, nothing else (C036), because platform log pipelines sit outside
every rule above. The play-by-play that `docker logs -f` shows today becomes a
pass log, sent through `append pass log`, redacted by the same detectors,
capped at 5 MB per job, and readable through the admin surface by the
log-reader role (C037, C042). Each read is audited.

## kindex

A repo chooses `kindex.store: none | org | repo`.

- **`org`** is one store per deployment. The operator pushes it with
  `claudebox kindex push --org`, from a `--data-dir` or a host `--profile`,
  reusing the launcher's existing resolution.
- **`repo`** is one store per repository. Two sources, chosen per repo by the
  operator: an operator push (`claudebox kindex push --repo owner/name`), or a
  build from the repo's own committed `.kin/` repo-memory on the default
  branch, rebuilt by the control plane when that branch moves. The second
  needs no operator action at all, which is what makes per-repo stores cheap.

Pushes go through the admin surface. The control plane validates each upload
with the same procedure `graph_snapshot.py` uses today (stability check,
`quick_check`, restamp to the `claudebox` profile, warm-up by the image's own
kindex) before it replaces the stored copy. A store whose schema is newer than
the worker image's kindex is refused with the same rebuild instruction as
today.

kindex-specific configuration lives in two places. The repo file carries only
the choices a repo owns: which store, and whether to use vectors. The
deployment carries everything else per store: the embedding configuration
recorded when the store was pushed (so the worker's `kin.yaml` matches it and
`ensure_vec_table` does not empty the copy's vectors, which today only works
for a host on kindex's defaults), and the Voyage credential.

In the worker, the broker fetches the job's snapshot through the channel and
serves `kin-mcp` over loopback HTTP behind a filtering MCP proxy that exposes
only `kindex_tools.READ_TOOLS`, with `--disallowedTools` kept as a second
layer (C033). That proxy runs as the broker uid, which closes the gap today's
design admits ("closing that would take a second uid"): the reviewer can no
longer read kindex's process environment.

## Linear

Hosted workers get no Linear MCP (C034). At dispatch the control plane
resolves the ticket ids the PR references (title, body and branch name at the
head the job was issued against), fetches each ticket and its comments with
the deployment's Linear key, and hands them to the worker as read-only job
material. Private, non-fork PRs only. The reviewer reads exactly those tickets
and cannot ask for others.

## Creating the App

`claudebox.sh app create --org ORG --target local|fly|gcp [--url URL]` uses
GitHub's App manifest flow:

1. It starts a one-shot HTTP server on localhost and opens the browser on a
   page that POSTs a generated manifest to
   `https://github.com/organizations/ORG/settings/apps/new`.
2. The org owner reviews the permissions and clicks Create.
3. GitHub redirects back to localhost with a code, which the script exchanges
   at `POST /app-manifests/{code}/conversions` for the App id, private key,
   webhook secret and client credentials.
4. The script writes them to the target's secret store directly (`fly secrets
   set`, `gcloud secrets versions add`, or the local env file), generates the
   deployment encryption key alongside them, and prints the install URL.

For `--target local`, the manifest sets the webhook inactive and the poll does
the work.

The manifest asks for these permissions (C051), each needed by the control
plane alone:

| Permission | Why |
|---|---|
| Metadata: read | required by GitHub for any repo access |
| Contents: read | clone, read the config file, build per-repo kindex stores |
| Pull requests: write | read PRs; read and post review comments |
| Issues: write | post PR conversation comments (they are issue comments) |
| Checks: write | the config-validation check run |
| Members: read | resolve the configured team (spike S1 decides whether the write-access check needs more) |

Events: `pull_request`, `pull_request_review`, `pull_request_review_comment`,
`issue_comment`, `installation`, `installation_repositories`, `repository`,
`push` (default-branch config and `.kin/` changes).

Workers' tokens are narrowed at mint time to contents, pull requests, issues
and metadata, read only.

## Spikes

Each is a short, throwaway investigation whose answer the build depends on.
They run before the phase that needs them.

| | Question | Needed by |
|---|---|---|
| S1 | Which App permission lets the control plane check a user's write access to a repo? | phase 2 |
| S2 | Can a worker container start as two uids with only SETUID/SETGID added, drop them, and pass today's hardening checks in the reviewer? Can each target do it, or does one need a sidecar? | phase 2 |
| S3 | How does `gh` reach the gateway: `GH_HOST` as an Enterprise host, or an HTTPS proxy? | phase 2 |
| S4 | Can kindex's Voyage client be pointed at a base URL? | phase 3 |
| S5 | Can Fly Machines be confined to the private network for egress? If not, the Fly docs name open egress as an accepted path and what it exposes (one repo and its store). | phase 4 |
| S6 | Can the gateway inject a `CLAUDE_CODE_OAUTH_TOKEN` for Anthropic the way Claude Code sends it? | phase 2 |
| S7 | Can baton's Cloud Run provider pin a service to min = max = 1 with CPU always allocated, and launch Cloud Run Jobs? If not, the control plane runs on one GCE VM. | phase 5 |

## Build order

Each phase ships something usable on its own, and the local trial comes first.

**Phase 0: extract the seams.** A pure refactor of `reviewer/`, with no
behavior change. Split the supervisor into the part that decides (candidates,
gate, owed, rounds, sessions) and the part that runs a PR group, behind a
`GroupRunner` interface whose only implementation is today's in-process one.
Move the per-pair decisions behind a `StateStore` interface whose only
implementation is today's dicts. Every existing suite passes unchanged.

**Phase 1: repo config.** The `.github/claudebox.yml` schema, its loader with
org-default merging, and `claudebox config validate PATH` for use before a
hosted control plane exists. Nothing reads it yet in local mode.

**Phase 2: the local hosted trial.** The control plane (SQLite, local blob
dir, polling only), the gateway (provider and GitHub read routes), the Docker
launcher, the worker image with broker, reviewer and `gh` shim, the posting
queue with the scan, the commit point, leases and generations, both trigger
modes, fork policy, park notices, retention and the erasure CLI, the admin
CLI, and `claudebox.sh app create --target local`. At the end of this phase
an org can create an App, install it, and get reviews from a laptop.

**Phase 3: hosted kindex.** Store pushes, per-repo stores from `.kin/`, the
broker's filtering MCP proxy, the Voyage route.

**Phase 4: Fly.** The Fly Machines launcher, Fly secrets wiring in `app
create`, webhooks with ingress, the volume-backed stores, and the documented
erasure window.

**Phase 5: GCP via baton.** The Cloud Run Jobs launcher, Postgres and GCS
stores, Secret Manager wiring, a `baton.yaml` derived from
`component_map.yaml`, the network rules that confine workers to the
gateway.

**Phase 6: Linear and the config check run.** Ticket resolution at dispatch,
and the check run on config changes.

## Testing

The no-Docker, no-network suites stay the main line of defense (C054).

- **Phase 0** adds no tests and changes none; it is proven by every existing
  suite passing untouched.
- **Phases 1 to 3** add `unittest` suites under `tests/` that stub at the
  seams, as `test-python.sh` does today: the config schema and merge rules,
  the trigger and fork decisions against recorded webhook payloads, the
  commit point and every outcome row in the table above against an in-memory
  `StateStore`, lease and generation races driven step by step, the gateway's
  route allowlists and caps against a fake upstream, the scan's detectors,
  and the channel contract tested from both ends against one shared fixture
  set so the control plane and broker cannot drift.
- **The worker** gets a bash suite in the style of `test-personas.sh`: stub
  `claude` and `gh`, run the broker against a fake control plane, and assert
  that the reviewer uid cannot read the capability or any credential.
- **Each launcher** gets a live smoke test run by hand against its target,
  like `claudebox.sh test` today. A provider is not trusted unattended until
  it has passed one.

## Out of scope

- More than one control-plane instance.
- Multi-tenant hosting (one deployment serving several organizations).
- A web UI for config. The repo file and the check run are the management
  surface.
- Matching posted comments against retrieved kindex notes.
- Per-PR encryption keys. The documented erasure window replaces them.
- Persisting state in local mode. It stays in memory, as today.
