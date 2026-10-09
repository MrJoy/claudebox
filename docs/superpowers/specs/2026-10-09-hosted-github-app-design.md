# claudebox as a hosted GitHub App: design

**Date:** 2026-10-09
**Status:** plan, revision 2, filed for review under the `plan` label

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
per-repo store, with a way to supply kindex-specific configuration. Secrets
(`CLAUDE_CODE_OAUTH_TOKEN`, `OLLAMA_API_KEY` and the rest) need a home that
no reviewed repo can reach.

The deployment targets are Fly.io (the author's), GCP orchestrated with baton
(the largest expected user), and one person's machine through a Docker VM for
trials. None of them may be baked into the design.

`claudebox.sh run --repo` keeps working exactly as it does today.

## How this plan was made

Revision 1 was pinned down with [constrain](https://github.com/wandercom/constrain)
and then reviewed by claudebox itself on this PR. That review found the
design front-loading its most expensive and least proven machinery, and
carrying several features no requirement asked for. Revision 2 keeps
constrain's threat analysis and restructures around the review:

- The build is phased by threat model. A working pipeline comes first, with a
  worker that holds its credentials the way local mode does today; credential
  stripping arrives once the pipeline is proven.
- Approved-fork review, hosted Linear, the pass-log store and the two-uid
  worker are deferred or dropped (see "Deferred").
- constrain's `constraints.yaml` and `component_map.yaml` are no longer
  shipped. Kept beside this document they were a second copy of its
  decisions, and the first edit already made them disagree. Phase 5 derives
  `baton.yaml` from the design as it stands then.

## Consequences, worst first

1. A credential, private source or org kindex content leaks through a PR
   comment, or through a path the comment scan never sees: worker egress,
   platform metadata, transcripts that outlive a repo going public.
2. Reviews stop and nobody notices. Across thirty repos, an absence of
   comments looks the same as an absence of PRs.
3. One repo, one runaway pass, or a provider outage spends the budget every
   other repo depends on.
4. Junk comments: duplicate findings after a retry, notices posted where a
   status would do.
5. State corruption, the known shape being fingerprints persisted without the
   sessions they describe.
6. Erasure that does not erase.

## Components

```
            GitHub ── webhooks ──▶ control-plane ──▶ records-store, blob-store
              ▲                      │  scheduler, decider (reviewer/ core),
              │ comments, checks,    │  posting queue, status, admin
              │ reactions, tokens    │
              └──────────────────────┤
                                     ├──▶ job runner ──▶ worker (one per PR group)
                                     │                     │
                                     └──── gateway ◀───────┘ (only peer)
                                             │
                                             └──▶ providers, GitHub reads, Voyage
```

**control-plane.** One process, one instance, enforced (see "Single
instance"). It receives webhooks, runs the reconciliation poll, reads repo
config, decides what to review, mints narrowed installation tokens, starts
workers through the job runner, and is the only component that writes to
GitHub. It owns every durable record.

The decisions it makes are today's, made by today's code. Phase 0 splits
`reviewer/` into a **decision core** (`signals.py`, the gate and round logic
now in `review_loop.py`, `personas.py`, `prompts.py`, `_stanzas.py`) and a
**group runner** (`passes.py`, `run_group`). The control plane imports the
core and supplies a SQL-backed state store and a remote runner; local mode
imports the same core with today's in-memory store and in-process runner.
There is one change gate, one round ladder and one prompt assembler, and the
existing suites keep testing them.

**gateway.** A separate process in the control-plane deployment that never
loads the App key. From phase 2c it is the worker's only network peer: it
fronts the worker channel to the control plane, and it serves the provider,
GitHub read and Voyage routes, injecting the real credential on each. It
enforces per-pair caps and classifies usage limits from what upstream
returned. The Workers AI translator (LiteLLM and `workersai-shim.py`) moves
behind it.

**job runner.** The runtime seam. One interface, three implementations: local
Docker, Fly Machines, Cloud Run Jobs (via baton). ("Launcher" stays the name
of `claudebox.sh`.) It starts a worker from a job spec carrying a job id, a
single-use bootstrap token, and the gateway's private URL.

**worker.** One container per PR group, running as one unprivileged uid, the
posture today's image already validates. It clones the PR, runs the group's
personas as `run_group` does now, and reports each pair's outcome.

**local mode.** `claudebox.sh run --repo`, unchanged: PAT, direct posting,
Linear MCP, in-memory state. When `.github/claudebox.yml` exists in the repo,
local mode logs once at startup that it does not read it and names
`.env.claudebox` as the local equivalent.

### One job per PR group

A worker serves one **PR group**: one PR at one head, with the personas for
its mode, phase 1 together and then phase 2. Five personas cost one clone and
one container, and the shared-clone defenses in `review_loop.py`
(`WORKTREE_STANZA`, `lock_git_dir`) carry over unchanged. Everything durable
stays per pair: session, transcript, fingerprint, round count, owed flag.

The clone is partial (`--filter=blob:none`) by default, since a reviewer reads
a diff and its surroundings, which cuts both clone time and the gateway's byte
accounting. A per-installation bare mirror behind the gateway would make
clones local again; it is deferred until clone volume is measured on a real
deployment.

### Resuming a session in a fresh container

A transcript blob becomes a resumable session only if Claude Code finds it
where `--resume` looks: `~/.claude/projects/<encoded cwd>/<session id>.jsonl`.
So the worker always clones to the same path (`/work/repo`) under the same
`$HOME` (`/home/reviewer`) on every target, and before starting a pair it
writes that pair's transcript to the encoded path. The session id is read
from the transcript itself, never from the worker's report. The worker suite
asserts that a pair run twice against a fake control plane produces a second
transcript containing the first one's turns, because a broken resume degrades
silently to a fresh session that re-raises everything.

## The worker channel

| Operation | What it carries |
|---|---|
| `fetch job` | exchanges the bootstrap token for the job capability; returns personas, prompts, round lines, the group's pairs, their transcripts, the kindex snapshot reference |
| `start pair` | re-checks visibility and access, then returns the pair's provider token; fails closed, leaving the pair owed and its round unchanged |
| `fetch snapshot` | the job's kindex snapshot, streamed |
| `post finding` | one comment for one pair, with an idempotency key |
| `complete pair` | one pair's outcome, session id and transcript |

**Bootstrap.** The job spec lands in platform metadata (Cloud Run execution
overrides, Fly machine config), readable by anyone with platform read access.
So it carries a bootstrap token instead of the capability. The token is
single-use: the first `fetch job` exchanges it for the capability, and it
expires five minutes after dispatch if unused. A copy read from metadata
afterwards is dead. The capability itself is stored only as a salted hash and
is revoked at completion, timeout, and any access change.

**Per-pair authorization.** A group's phase-2 personas start minutes after
phase 1, so a check made only at dispatch can be stale by then. `start pair`
re-checks the repo's visibility and the App's access before each pair runs.

**Per-pair provider tokens.** `start pair` returns a provider token bound to
that one pair, and the worker hands it to that pair's `claude` process alone.
That gives the gateway what it needs to attribute a usage limit or a success
to a pair. In phases 2c and later the worker is a single uid, so a hostile
reviewer can read its siblings' tokens; the most it can do with one is
misattribute traffic among the pairs of its own PR, which is the same repo
under the same permissions.

**What a prompt-injected reviewer holds.** In one uid, the reviewer can read
everything in the worker: the capability, its group's pair tokens, the read
token, the snapshot. With them it can post a comment as the bot (which it can
already do by design, subject to the scan), and report its own pairs complete
without reviewing them. The gateway's evidence rule below blunts the second:
an `ok` needs provider traffic the gateway saw for that pair. A two-uid worker
that keeps the capability from the reviewer is deferred, gated on a spike
that first establishes the container can reach a zero bounding set (see
"Deferred").

### The `gh` shim

The worker puts a `gh` shim ahead of the real `gh` on `PATH`. `gh pr comment`
becomes `post finding`; every other `gh` call goes to the gateway's GitHub
read route. Persona text and `GH_STANZA` keep saying `gh pr comment`, because
the persona bodies are pinned and shared with local mode.

`post finding` returns as soon as the control plane has accepted the finding
into its posting queue, so a slow post never blocks a review. What it returns:

- **queued**: exit 0, print "queued". The control plane owns posting from
  here, and retries a transient failure (secondary limit, GitHub error) until
  the job deadline. The idempotency key (job, pair, sequence number) makes a
  retry safe.
- **withheld**: the scan matched a credential. Exit non-zero and tell the
  model the finding contained a credential-shaped string, was not posted, and
  should be restated without the literal value. The review continues. A
  reviewer that finds a committed secret therefore still reports it, without
  reprinting it.

If a queued finding still has not posted when its job ends, it is dropped and
its pair is committed without its fingerprint, so the pair stays owed and the
next round runs. The next round's persona reads its own signed comments to
see what it has already raised, so a missing finding is raised again. GitHub's
thread is authoritative for what the bot has said.

## Triggers

Webhooks are the primary trigger. A reconciliation poll runs every few minutes
per installation to cover missed deliveries, and it is the only trigger on a
local trial with no public URL.

Each repo is in one of two trigger modes:

- **`auto`.** Every PR is a candidate. The existing change gate decides when
  to re-review: the head moved, an unsigned comment arrived, or the mode
  flipped, after the settle window.
- **`requested`.** A PR is a candidate only once someone with write access
  asks:
  - **Request review from the configured team.** GitHub does not list a
    third-party App in the reviewer picker, so a team stands in for it. This
    **enrolls** the PR: while the request stands, the PR behaves as if the
    repo were in `auto` mode.
  - **Comment `/claudebox review`.** Reviews the PR once at its current head.

The control plane checks the asker's write access through GitHub's permission
API; `author_association` is not trusted. A command from someone without
write access gets no review and no reply. A writer's ask gets a 👀 reaction
when it is accepted and the per-PR check run (below) shows where it stands.

**Asks are durable.** Each ask is a row: PR, head, asker, kind (command or
enrollment), and whether it has been served. A missed webhook for a standing
team request is recovered by the poll, which can see the request on GitHub; a
missed command cannot be re-derived from GitHub, so the row is what keeps it.

**What gets dispatched** each scheduling pass: pairs on PRs in `auto` mode or
enrolled whose fingerprint moved, pairs with an unserved ask, and owed pairs.
An owed pair or an unserved ask is dropped when its PR closes, when the App
loses access to the repo, or when the repo goes public with an ask still
pending against org-store content.

**Fork PRs.** A PR whose head repo is a fork and whose author lacks write
access is never reviewed, in either mode. An approved-fork path is deferred.

### The per-PR check run

Every reviewed PR carries one `claudebox` check run. It is queued when a pass
is scheduled, in progress while one runs, and completes with a conclusion and
a one-paragraph summary:

| State | Conclusion | Summary says |
|---|---|---|
| reviewed | success | how many findings each persona posted |
| parked: budget | neutral | which budget, and the UTC reset time |
| parked: round cap | neutral | that an ask is needed to continue |
| parked: usage limit | neutral | the provider is refusing work; retry time |
| failed / owed | neutral | that a retry is scheduled |
| config invalid | neutral | which file and which field, and what config is in use instead |

That replaces the park comments of revision 1. A PR author can now tell "it
reviewed and found nothing" from "it never ran", and the PR's comment thread
holds only findings. Every comment the control plane does post carries the
`-claudebox` marker, added in the posting queue so any future comment type is
signed by construction; an unsigned control-plane comment would read as human
activity and buy the PR another round.

### Parallelism

PRs run in parallel, bounded by:

- `max_concurrent_jobs` for the deployment (default 4);
- `concurrency` per provider profile, counted in pairs in flight, which keeps
  fan-out from tripping a provider's rate limit;
- `max_concurrent_personas` within a job, as today.

Today's rule that PRs are serialized to bound usage-limit pressure is
replaced in hosted mode by the per-profile cap, which bounds the same
pressure directly. Local mode keeps serializing.

## Configuration

### Repo and org files

A repo's config lives at `.github/claudebox.yml`, and org defaults at
`.github/claudebox.yml` in the org's `.github` repository. Both are read from
the **default branch only**, so a PR cannot choose who reviews it. A repo
file overrides org defaults key by key. Neither may hold a secret; the schema
has no field that could.

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

kindex:
  store: none            # none | org | repo
  vectors: true          # only takes effect once hosted vector search ships (phase 3b)
```

**Strictness.** The loader rejects unknown keys, so a typo is an error and
never a silent fall back to the org default. A missing `version` means 1; an
unknown version is an error. The general rule is that any field the
deployment cannot satisfy is an error: an unknown profile, a model the
profile does not allow, an unknown persona, a team that does not exist,
`kindex.store: org` on a repo the org store is not shared with, or on a
public repo.

**Discovery.** The repo author does not see the deployment file, so the
deployment publishes what they need:

- a JSON Schema for the file, with persona names enumerated, for editor
  completion;
- `claudebox.sh profiles`, which prints the deployment's profiles with their
  allowed and default models (also served by the admin surface);
- `claudebox.sh config validate FILE [--deploy DEPLOY_FILE]`, which checks a
  repo file against the schema and, given the deployment file, against its
  profiles, before any PR exists.

Every error message names the command that shows the catalog.

**When validation happens.** The control plane validates the file at the
head of every PR that changes it and reports the result in that PR's check
run, so a bad config is caught before it merges. Separately it validates the
effective config of every installed repo whenever the default branch of the
repo or of the org `.github` repo moves, because an org-default edit can
break many repos at once with no PR on any of them.

**When the config in force is invalid**, the repo falls back to the last
config that validated for it; if it never had one, to the org defaults; if
those are invalid too, the repo is not reviewed. Whichever applies, the
problem shows in every one of that repo's PR check runs and in `claudebox.sh
status`, so it is visible without waiting for a particular PR.

**Org defaults need the org `.github` repo in the installation.** An App
installed on selected repositories cannot read one that was not selected.
`app create` tells the installer to include it, and the control plane treats
an unreadable org `.github` repo as "no org defaults", shown in `status`.

### Deployment config

The operator's file, `claudebox.deploy.yml`, lives with the deployment and
never in a reviewed repo. It names secrets only by reference:

```yaml
profiles:
  ollama:
    provider: ollama
    models: [glm-5.2:cloud, kimi-k2:cloud]
    default_model: glm-5.2:cloud
    credential: OLLAMA_API_KEY        # name in the platform secret store
    concurrency: 8                    # pairs in flight
    budget:
      daily_tokens: 40000000          # needs a provider that reports usage
      # daily_passes: 400             # the alternative for one that does not
      repo_share: 0.25                # no repo may spend more than this share of the day
    limit_backoff_seconds: 1800       # today's LIMIT_BACKOFF_SECONDS
    per_pair: { requests: 200, tokens: 4000000 }
max_concurrent_jobs: 4
operators: [some-login]
retention:
  closed_pr_days: 14
kindex:
  org_store:
    share_with: [api, web, infra]     # or `all-private`; see "kindex"
  voyage_credential: VOYAGE_API_KEY   # optional; phase 3b
```

The provider types are today's `PROVIDER` arms (`ollama`, `anthropic`,
`custom`, `cloudflare`, `workersai`). Their validation is extracted from
`entrypoint.sh`'s `case` block into one module under `reviewer/` that both
`entrypoint.sh` (through the existing `--check` seam) and the gateway's
profile loader call. Local mode keeps dying at startup on a mis-wired
credential, and `test-providers.sh` keeps a local-mode case asserting it.

## Secrets

Platform secret mounts hold control-plane secrets only: the App private key,
the webhook secret, the deployment encryption key, each profile's provider
credential, and the optional Voyage key.

| Target | Control-plane secrets | Workers get |
|---|---|---|
| Local Docker | an env file, mode 600, written by setup | a container started with no env file |
| Fly.io | `fly secrets` on the control-plane app | a separate Fly app with no secrets set |
| GCP | Secret Manager, bound to the control-plane service | a Cloud Run job with no secret bindings |

In phases 2a and 2b the worker receives its provider credential and a
narrowed read token through `fetch job`, which is local mode's posture with
a shorter-lived token. From phase 2c no worker holds a credential: the
gateway injects them all.

On GCP the worker's service account is reachable by every process in the
worker through the metadata server. It is granted nothing: no IAM roles, not
even permission to invoke the control plane. The gateway is reached over the
VPC and authenticates the worker by capability alone.

`CLAUDE_CODE_OAUTH_TOKEN` is supported as an `anthropic` profile credential.
It belongs to one person's Claude subscription, and using it to review an
organization's PRs may not be what that subscription's terms allow; the setup
docs say so and recommend an API key or Ollama for an org deployment. Whether
the gateway can inject it the way Claude Code sends it is spike S3.

## The gateway (phase 2c)

**Provider route.** Overwrites `model` with the job's model on every request.
For Anthropic-shaped profiles it allows `POST /v1/messages` and `POST
/v1/messages/count_tokens` and nothing else, so batches and files are
unreachable. On a profile whose provider reports usage it counts input plus
output tokens per pair, clamps `max_tokens` to what remains, and caps body
size. On one that does not, it caps requests, body size and `max_tokens`,
which it can count exactly, and keeps no token estimate. Hitting a cap ends
the pair as `capped`, which keeps its session (see "State").

**Usage limits.** The gateway sees the upstream 429 or limit body, and the
per-pair token tells it whose request it was. A limited pair is parked for
the profile's `limit_backoff_seconds` and is not redispatched before then. A
whole profile parks for the same interval when pairs on two different PRs hit
a limit inside it.

**GitHub read route.** Uses an installation token minted per job, narrowed by
GitHub to the job's repo and to read permissions, so GitHub enforces the repo
boundary. The gateway's own filter is a second layer: GET, GraphQL queries,
and git smart-HTTP fetch (`GET .../info/refs?service=git-upload-pack` and
`POST .../git-upload-pack`); `git-receive-pack` and every other POST are
refused, and redirects are not followed off github.com. Per job it caps
requests (default 300) and clone bytes (default 2 GB, overridable per repo in
the deployment file), and it refuses worker reads once the installation's
remaining REST or GraphQL limit falls below a 20% reserve, which keeps the
control plane's own checks working. The worker points `git` at it with
`url.<gateway>.insteadOf https://github.com/`; how `gh` is pointed at it is
spike S2.

**Voyage route (phase 3b).** Single-input query embeddings under a length
cap, with a per-pair request cap.

The gateway streams. It does not buffer, cache or log any body, and it does
not retry for the client.

## Posting

The control plane posts through one queue per installation, paced at no more
than one comment a second and 400 an hour by default, and honoring
`Retry-After` on a secondary limit. Before each post it:

1. **Checks the repo's visibility against GitHub, uncached.** Posts are paced
   at one a second, so one request per post is cheap, and this is the last
   check standing between org-store content and a repo that just went public.
   A change revokes the job. An error retries until the deadline.
2. **Scans the body** for known secret shapes and for the literal value of
   every credential the deployment holds, provider and Voyage keys included;
   the scanner reads them from the secret store for that purpose alone. A
   match is withheld and recorded with the detector name, a salted hash, and
   the redacted body. The scan covers credentials only; kindex content is
   governed by which repos may use which store.
3. **Adds the `-claudebox` signature** if missing, and enforces per-pass,
   per-PR and size caps.

## kindex

A repo chooses `kindex.store: none | org | repo`.

- **`org`** is one store per deployment, pushed by the operator with
  `claudebox.sh kindex push --org`. A private repo using it can quote any of
  its content into its own PRs, including notes about repos its contributors
  cannot see. So the store is not available to every repo by default: the
  operator lists the repos it is shared with (`kindex.org_store.share_with`)
  or writes `all-private` as a deliberate choice. Public repos can never use
  it. On a target whose worker egress is not confined (Fly, unless spike S4
  says otherwise) the org store is refused outright, because one injected
  pass there could send the whole store off-host.
- **`repo`** is one store per repository: an operator push (`claudebox.sh
  kindex push --repo owner/name`), or a build from the repo's own committed
  `.kin/` repo-memory on the default branch, rebuilt when that branch moves.

Pushes go through the admin surface, and the control plane validates each
with the procedure `graph_snapshot.py` uses today (stability check,
`quick_check`, restamp to the `claudebox` profile, warm-up) before replacing
the stored copy.

**One kindex version.** The control-plane image and the worker image take
their kindex from one build argument, so they always agree. The operator's
host kindex only has to be old enough that the deployment's kindex can
migrate its stores on arrival; a store from a newer host is refused at push
time with "upgrade the deployment's kindex and redeploy both images", which
is the hosted equivalent of today's "run `claudebox.sh build` again".

**kindex-specific configuration.** The repo file carries only what a repo
owns: which store, and whether to use vectors. The deployment carries the
rest: which repos share the org store, the Voyage key, and per store the
embedding configuration recorded at push time, so the worker's `kin.yaml`
matches it and `ensure_vec_table` keeps the copy's vectors.

In the worker, `kin-mcp` serves the snapshot with `--disallowedTools` built
from `kindex_tools.READ_TOOLS`, as today. Hosted vector search (phase 3b)
adds the Voyage route and spike S5 (can kindex's Voyage client take a base
URL).

## Visibility

A public repo gets no org store. An internal repo counts as private.
Visibility is checked at dispatch, at `start pair`, and before every post.
A check that errors fails closed: nothing is dispatched or started, and the
pair stays owed with no round counted.

A repo going public, an uninstall, removal from the installation, or deletion
revokes every live capability for that repo. A repo going public also
discards its transcripts and sessions before any further pass; rounds and
asks survive, since they hold no content.

## State

The control plane is authoritative for everything durable.

| Item | Store | Content |
|---|---|---|
| pair state: fingerprint, round, owed, session id, transcript ref, parked-until | records | none |
| asks, budgets, park events, audit rows | records | logins only |
| withheld-finding records | records | redacted body |
| transcripts | blob, encrypted | yes |
| kindex snapshots | blob, encrypted | yes |
| job material (prompts, queued findings) | blob, encrypted | yes, deleted when the job ends |

The records store is SQLite on a volume for the local trial and Fly, with
`secure_delete=ON`, and Cloud SQL Postgres on GCP. The blob store is a
directory, the Fly volume, or a GCS bucket created with soft delete off
(retention 0) and versioning off. The control plane encrypts every blob with
the deployment key before writing it.

Leases (which pairs have a live job) live in the control plane's memory.
That is safe because exactly one instance runs, and a restart ends every live
job (see "Single instance").

### The commit point

Fingerprints, transcripts, rounds and owed flags persist together or not at
all. On `complete pair` the control plane writes the transcript under a new
self-describing immutable name (repo, PR, pair, timestamp), then in one
transaction points the pair at it and applies the outcome's row below. A
crash between the two leaves an unreferenced blob, which the sweep below
removes.

An `ok` needs evidence: the job's capability is live, the transcript parses as
a Claude Code session under a size cap, its session id is the pair's, and
(from phase 2c) the gateway saw at least one successful provider response on
that pair's token. A pass with no provider traffic is `failed`.

| Outcome | Transcript | Fingerprint | Round | Owed | Budget |
|---|---|---|---|---|---|
| `ok` | committed | committed | +1 | cleared | spent |
| `ok`, a queued finding never posted | committed | unchanged | +1 | stays | spent |
| `capped` (a per-pair gateway cap) | committed | unchanged | unchanged | stays | spent |
| `usage-limited` | committed | unchanged | unchanged | stays, parked for the backoff | spent only if a provider response succeeded |
| `failed` | discarded; session dropped | unchanged | unchanged | stays | spent only if a provider response succeeded |
| `revoked` (timeout, access change, restart) | discarded | unchanged | unchanged | stays | spent only if a provider response succeeded |

A usage-limited or capped pair keeps a resumable session because its
transcript is committed, which a fresh container needs in order to resume at
all. A pass the provider refused outright costs no budget, so a provider
outage cannot exhaust a day's budget and then report it as a budget park.

### Budgets

A profile's daily budget is in tokens when its provider reports usage, and in
passes when it does not. It resets at 00:00 UTC. An ask does not bypass it.
No single repo may spend more than `repo_share` of a day's budget (default
0.25), so one busy repo parks only itself. `claudebox.sh status` shows each
repo's consumption against its share and the profile's, so an operator can
see a repo draining the pool before anything parks.

### Retention and erasure

A PR's transcripts and session state are deleted 14 days after it closes or
merges (configurable), immediately when the App loses access to the repo,
and on demand through `claudebox.sh erase --repo R [--pr N]`. Job material is
deleted when its job ends. Asks, park events and audit rows carry no content,
are kept for a year, and survive erasure.

Erasure works from both sides. The records side deletes the rows; the blob
side deletes every blob whose self-describing name matches the repo or PR.
An hourly sweep deletes any blob that no row references and that is older
than an hour, which covers a crash between the commit's two writes.

Erasure is immediate in the live stores and complete when backups age out.
The docs state the window per target: Cloud SQL's backup and point-in-time
recovery retention on GCP (the blob bucket has soft delete off, so it adds
nothing), Fly's volume snapshot retention on Fly, and nothing beyond the live
volume locally. Setup proposes the shortest retention each target allows.

## Single instance, health, and deploys

**Enforced, not assumed.** Cloud Run and Fly both overlap the old and new
instance during a deploy by default, and two control planes would each hold
their own leases and counters. So the control plane takes a lock in the
records store at startup (a Postgres advisory lock; on SQLite, the file lock
of a single writer) and does not serve until it holds it. A second instance
waits. Local and Fly deploys stop the old machine before starting the new one.

**Deploys drain.** On `SIGTERM` the control plane stops dispatching, lets
running jobs finish up to a drain deadline (default 10 minutes, under the
platform's grace period where the platform allows one that long), then
revokes what is left. Revoked pairs stay owed and run on the new instance.
Budget counters are committed with each `complete pair`, not checkpointed on
a timer, so a crash cannot loosen them.

**Health.** The control plane serves:

- `/healthz`, for the platform's liveness and the operator's uptime check;
- `/metrics` (Prometheus text): passes by outcome, queue depth, oldest owed
  pair's age, last successful review per repo, webhook deliveries rejected,
  GitHub rate limit remaining, budget remaining per profile;
- `claudebox.sh status`, the same facts for a person.

The docs name the conditions worth paging on, with suggested thresholds:
`/healthz` failing; no successful review anywhere for an hour while PRs are
open; the oldest owed pair older than two hours; webhook signature failures;
the installation suspended or uninstalled (from the `installation` event);
any repo whose config is invalid. Paging itself is the platform's job (Cloud
Monitoring, Fly's metrics, or whatever scrapes `/metrics` locally).

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
4. The script writes them to the target's secret store (`fly secrets set`,
   `gcloud secrets versions add`, or the local env file) and generates the
   deployment encryption key beside them.
5. It prints what is left, as a checklist it can re-check with `claudebox.sh
   doctor`: add each profile's provider credential to the secret store,
   write `claudebox.deploy.yml`, create the review team if any repo will use
   `requested` mode, and install the App, including the org's `.github` repo
   if org defaults will be used.

For `--target local` the manifest leaves the webhook inactive and the poll
does the work.

The manifest asks for these permissions, all used by the control plane:

| Permission | Why |
|---|---|
| Metadata: read | required by GitHub for any repo access |
| Contents: read | clone, read config files, build per-repo kindex stores |
| Pull requests: read | read PRs, diffs, reviews and review comments |
| Issues: write | post PR conversation comments, which are issue comments, and react to asks |
| Checks: write | the per-PR check run |
| Members: read | confirm the configured team exists (spike S1 decides whether the write-access check needs more) |

Events: `pull_request`, `pull_request_review`, `pull_request_review_comment`,
`issue_comment`, `installation`, `installation_repositories`, `repository`,
`push` (default-branch config and `.kin/` changes).

Worker tokens are narrowed at mint time to contents, pull requests, issues and
metadata, read only.

## Spikes

Each is a short, throwaway investigation, run before the phase that needs it.

| | Question | Needed by |
|---|---|---|
| S1 | Which App permission lets the control plane check a user's write access to a repo? | 2a |
| S2 | How does `gh` reach the gateway: `GH_HOST` as an Enterprise host, or an HTTPS proxy? | 2c |
| S3 | Can the gateway inject `CLAUDE_CODE_OAUTH_TOKEN` the way Claude Code sends it? | 2c |
| S4 | Can Fly Machines be confined to the private network for egress? | 4 |
| S5 | Can kindex's Voyage client take a base URL? | 3b |
| S6 | Can baton's Cloud Run provider pin a service to one instance with CPU always allocated, and launch Cloud Run Jobs? If not, the control plane runs on one GCE VM. | 5 |

## Build order

Each phase ships something usable, and each is split so that a wrong call is
found while it is still cheap to abandon.

**Phase 0: one decision core.** A pure refactor of `reviewer/`: split the
decision core from the group runner, put the state behind a `StateStore`
interface whose only implementation is today's dicts, and extract provider
validation into the shared module. Every existing suite passes unchanged.

**Phase 1: repo config.** The schema, the JSON Schema, the strict loader with
org-default merging, `claudebox.sh config validate` with `--deploy`, and the
local-mode startup notice.

**Phase 2a: the pipeline.** Control plane (SQLite, local blob dir, polling,
the single-instance lock), the local Docker job runner, a single-uid worker
that holds its provider credential and a narrowed read token and clones
directly, the posting queue with the scan and the signature, the per-PR check
run, `auto` mode only, `app create --target local`, `/healthz` and `status`.
At the end an org can create an App, install it, and get reviews from a
laptop.

**Phase 2b: the state machinery.** The change gate against SQL state, the
commit point and every outcome row, durable asks and `requested` mode,
budgets with repo shares, usage-limit parking, retention, erasure and the
sweep, drain on deploy, `/metrics`.

**Phase 2c: the gateway.** The provider and GitHub read routes, per-pair
tokens, the bootstrap token, the `gh` shim's read half, workers holding no
credential. This is the part only an unattended hosted deployment strictly
needs, and where spikes S2 and S3 land.

**Phase 3a: hosted kindex.** Store pushes, `share_with`, per-repo stores from
`.kin/`, the one-version build argument.

**Phase 3b: hosted vector search.** The Voyage route and per-store embedding
configuration, after spike S5.

**Phase 4: Fly.** The Fly Machines job runner, Fly secrets in `app create`,
webhooks, volume-backed stores, the egress decision from spike S4.

**Phase 5: GCP via baton.** The Cloud Run Jobs job runner, Postgres and GCS
stores, Secret Manager, a `baton.yaml`, the network rules that confine
workers to the gateway.

## Testing

The no-Docker, no-network suites stay the main line of defense.

- **Phase 0** adds no tests and changes none; every existing suite passing
  untouched is the proof.
- **Phases 1 to 3** add `unittest` suites that stub at the seams, as
  `test-python.sh` does: the config schema, strictness and fallbacks; trigger
  and ask decisions against recorded webhook payloads; every outcome row
  against an in-memory `StateStore`; budget shares and parking; the gateway's
  allowlists (including the git smart-HTTP pair and a refused
  `git-receive-pack`) and caps against a fake upstream; the scan; and the
  channel contract tested from both ends against one shared fixture set.
- **The worker** gets a bash suite in the style of `test-personas.sh`: stub
  `claude` and `gh`, run it against a fake control plane, and assert that a
  pair run twice resumes its own session.
- **Each job runner** gets a live smoke test run by hand against its target,
  like `claudebox.sh test` today.

## Deferred

- **Approved-fork review.** Forks are never reviewed in v1. When it comes
  back it needs its own profile with no fallback to the repo's, a per-head
  approval, and a reduced worker without kindex.
- **Hosted Linear.** Local mode keeps its Linear MCP. Nothing in the ask
  named Linear for the hosted shape.
- **The two-uid worker.** It would keep the capability from the reviewer, at
  the cost of a root entry process. It starts with a spike that answers the
  bounding-set question (dropping capabilities from the bounding set needs
  `CAP_SETPCAP`, so "only SETUID/SETGID" does not reach a zero `CapBnd`), and
  with a written account of what the reviewer would gain from the capability
  that it does not already have.
- **A per-installation git mirror**, once clone volume is measured.
- **Matching posted comments against retrieved kindex notes.**

## Out of scope

- More than one control-plane instance.
- Multi-tenant hosting.
- A web UI for config.
- A pass-log store separate from transcripts; the operator reads transcripts
  through the admin surface, and each read is audited.
- Per-PR encryption keys; the documented erasure window replaces them.
- Persisting state in local mode.
