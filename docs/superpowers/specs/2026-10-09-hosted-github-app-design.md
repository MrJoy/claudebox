# claudebox as a hosted GitHub App: design

**Date:** 2026-10-09
**Status:** plan, revision 3, filed for review under the `plan` label

## Problem

claudebox serves one repository per container. An operator mounts that repo's
`.git`, hands over a fine-grained PAT and one provider credential, and picks
PRs with a selector. An organization that wants every PR in thirty repos
reviewed has to run thirty containers, hold thirty PATs, and edit thirty env
files to change a model.

The ask is a GitHub App that an organization creates and hosts for itself.
Installing it on a repo turns on review for that repo's PRs from branches in
the repo itself; PRs from forks are out of v1 (see "Fork PRs"). Each repo
chooses its trigger mode, model, plan label, plan personas and code personas.
PRs are reviewed in parallel up to a limit. Creating the App and managing
per-repo configuration has to be easy. kindex works with either an org-wide
store or a per-repo store, with a way to supply kindex-specific
configuration. Secrets (`CLAUDE_CODE_OAUTH_TOKEN`, `OLLAMA_API_KEY` and the
rest) need a home that no reviewed repo can reach.

The deployment targets are Fly.io (the author's), GCP orchestrated with baton
(the largest expected user), and one person's machine through a Docker VM for
trials. None of them may be baked into the design.

`claudebox.sh run --repo` keeps working exactly as it does today.

## How this plan was made

Revision 1 was pinned down with [constrain](https://github.com/wandercom/constrain).
Revisions 2 and 3 answer claudebox's own review of this PR. Revision 2
phased the build by threat model and deferred approved-fork review, hosted
Linear and the two-uid worker. Revision 3 moves the provider route of the
gateway forward into the first shipped phase. That route takes the org's
provider credential out of the worker before any untrusted PR is reviewed,
and it is the only trustworthy counter for budgets and usage limits. The
other GitHub-facing half of the gateway still arrives later.

## Consequences, worst first

1. A credential, private source or org kindex content leaks through a PR
   comment, or through a path the comment scan never sees: worker egress,
   platform metadata, stored content that outlives a repo going public.
2. Reviews stop and nobody notices. Across thirty repos, an absence of
   comments looks the same as an absence of PRs.
3. One repo, one runaway pass, or a provider outage spends the budget every
   other repo depends on, or spins forever without spending anything.
4. Junk comments: duplicate findings from a retry or an orphaned worker.
5. State corruption, the known shape being fingerprints persisted without the
   sessions they describe.
6. Erasure that does not erase.

## Components

```
            GitHub ── webhooks ──▶ control-plane ──▶ records-store, blob-store
              ▲                      │  scheduler, decider (reviewer/ core),
              │ comments, checks,    │  outbox, status, admin API
              │ reactions, tokens    │
              └──────────────────────┤
                                     ├──▶ job runner ──▶ worker (one per PR group)
                                     │                     │
                                     └──── gateway ◀───────┘
                                             │
                                             └──▶ providers (2a), GitHub reads (2c), Voyage (3b)
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

**gateway.** A separate process in the control-plane deployment that never
loads the App key. It injects real credentials at the edge and enforces
per-pair caps. Its routes arrive in phases: the provider route in 2a, the
GitHub read route and the worker channel in 2c, the Voyage route in 3b. The
Workers AI translator (LiteLLM and `workersai-shim.py`) moves behind it.

**job runner.** The runtime seam. One interface, three implementations: local
Docker, Fly Machines, Cloud Run Jobs (via baton). ("Launcher" stays the name
of `claudebox.sh`.) It starts a worker from a job spec carrying a job id, a
single-use bootstrap token, and a private URL. It can also list and stop the
workers it started.

**worker.** One container per PR group, running its reviewer as one
unprivileged uid with no capabilities and `no_new_privs` (see "Worker
hardening"). It clones the PR, runs the group's personas as `run_group` does
now, and reports each pair's outcome.

**local mode.** `claudebox.sh run --repo`, unchanged: PAT, direct posting,
Linear MCP, in-memory state. When `.github/claudebox.yml` exists in the repo,
local mode logs once at startup that it does not read it and names
`.env.claudebox` as the local equivalent.

### One job per PR group

A worker serves one **PR group**: one PR at one head, with the personas for
its mode, phase 1 together and then phase 2. Five personas cost one clone and
one container, and the shared-clone defenses in `review_loop.py`
(`WORKTREE_STANZA`, `lock_git_dir`) carry over. Everything durable stays per
pair: session, transcript, fingerprint, round count, owed flag.

The clone is blobless (`--filter=blob:none`) with a full checkout of the PR
head. That saves history, which is fetched lazily when a persona asks for
it, and does not save the head tree, which the checkout materializes. The
per-repo clone-byte cap (default 2 GB) is therefore sized against the size
of the head tree, and a repo whose head tree exceeds it needs an override in
the deployment file. A per-installation bare mirror that would make clones
local is deferred until clone volume is measured.

### Resuming a session in a fresh container

A transcript blob becomes a resumable session only if Claude Code finds it
where `--resume` looks: `~/.claude/projects/<encoded cwd>/<session id>.jsonl`.
So the worker always clones to `/work/repo` under `$HOME=/home/reviewer` on
every target, and before starting a pair it writes that pair's transcript to
the encoded path. The session id is read from the transcript itself. The
worker suite asserts that a pair run twice against a fake control plane
produces a second transcript containing the first one's turns, because a
broken resume degrades silently to a fresh session that re-raises everything.

### Worker hardening

Today's image proves three things at startup: not root, `NoNewPrivs: 1`, and
a zero `CapBnd`. Only the first is a property of the image; the other two
come from `claudebox.sh`'s `--security-opt no-new-privileges` and
`--cap-drop ALL`, and neither Fly Machines nor Cloud Run Jobs exposes those
switches. So on those targets the worker starts as root and its entrypoint
establishes the posture itself, with
`setpriv --no-new-privs --bounding-set=-all --inh-caps=-all --reuid=reviewer
--regid=reviewer --init-groups`, before exec'ing the broker. Root holds
`CAP_SETPCAP` at that moment, which is what emptying the bounding set needs.
The existing hardening checks then run as the reviewer, unchanged. The local
Docker job runner keeps passing the flags as well.

`ALLOW_UNHARDENED` is never set in a worker's environment on any target, so a
target that cannot provide the posture fails its first job loudly. Whether
each target's runtime permits the `setpriv` sequence is spike S7, answered
separately for Fly and for Cloud Run. If a target cannot, it is refused as a
worker host; the plan does not accept a silent downgrade.

## The worker channel

| Operation | What it carries |
|---|---|
| `fetch job` | exchanges the bootstrap token for the job capability; returns personas, prompts, round lines, the group's pairs, their transcripts, the kindex store reference, and (2a, 2b) a narrowed GitHub read token |
| `start pair` | re-checks visibility and access, then returns the pair's provider token; fails closed, leaving the pair owed and its round unchanged |
| `fetch snapshot` | the kindex store, streamed |
| `post finding` | one comment for one pair, with an idempotency key |
| `complete pair` | one pair's outcome, session id and transcript |

In 2a and 2b the worker calls the control plane's channel endpoint directly
over the private network. From 2c the gateway fronts it, and the gateway is
the worker's only network peer.

**Bootstrap.** The job spec lands in platform metadata (Cloud Run execution
overrides, Fly machine config), readable by anyone with platform read access.
So it carries a bootstrap token. The token is single-use: the first `fetch
job` exchanges it for the capability, and it expires five minutes after
dispatch if unused. A copy read from metadata afterwards is dead.

**Durable credentials, revoked at startup.** Every capability and every
gateway token is a row in the records store (a salted hash, the job, the
pair, an expiry). Each channel call and each gateway request checks its row.
A row is revoked at completion, at timeout, on any access change, and, for
all of them at once, when a control-plane instance starts. An instance that
died without draining leaves workers running on other platform resources; the
successor revokes every outstanding row before it serves anything, so an
orphan's next call fails, and asks the job runner to stop every worker it
can find. Leases (which pairs have a live job) stay in memory, which is safe
once no prior instance's worker can write anything.

**Per-pair authorization.** A group's phase-2 personas start minutes after
phase 1. `start pair` re-checks the repo's visibility and the App's access
before each pair runs.

**Per-pair provider tokens.** `start pair` returns a provider token bound to
one pair, and the worker hands it to that pair's `claude` process alone. That
is how the gateway attributes a usage limit, a success, or a cap to a pair.
The worker is a single uid, so a hostile reviewer can read its siblings'
tokens; the most it can do with one is misattribute traffic among the pairs
of its own PR.

**What a prompt-injected reviewer holds.** Everything in the worker: the
capability, its group's pair tokens, the snapshot, and in 2a and 2b a read
token narrowed to its repo for an hour. With them it can post a comment as
the bot (which it can already do by design, subject to the scan), report its
own pairs complete without reviewing them (blunted by the gateway's evidence
rule below), and read the repo it is reviewing. It never holds a provider
credential. A two-uid worker that keeps the capability from the reviewer is
deferred.

### The `gh` shim

The worker puts a `gh` shim ahead of the real `gh` on `PATH`. `gh pr comment`
becomes `post finding`. Other `gh` calls use the read token in 2a and 2b and
the gateway's GitHub read route from 2c. Persona text and `GH_STANZA` keep
saying `gh pr comment`.

`post finding` returns as soon as the control plane has accepted the finding:

- **queued**: exit 0, print "queued". The finding is in the outbox (below),
  and delivery is now the control plane's job, independent of this pass.
- **withheld**: the scan matched a credential. Exit non-zero and tell the
  model the finding contained a credential-shaped string, was not posted, and
  should be restated without the literal value. The review continues, so a
  reviewer that finds a committed secret still reports it without reprinting
  it.

## Posting and the outbox

Every finding the control plane accepts goes into a durable **outbox**, keyed
by its idempotency key (job, pair, sequence number). One queue per
installation drains it, paced at no more than one comment a second and 400 an
hour by default, honoring `Retry-After` on a secondary limit. An outbox item
outlives its job: a finding is retried until it posts, its PR closes, or its
repo goes public, so a slow post never costs a review and never forces a
re-review. Because delivery is guaranteed this way, a pass whose findings are
still queued when it ends commits as an ordinary `ok`.

Before each post the queue:

1. **Checks the repo's visibility against GitHub, uncached.** A repo that went
   public has its outbox items for content-bearing pairs deleted, not posted.
2. **Scans the body** for known secret shapes and for the literal value of
   every credential the deployment holds, provider and Voyage keys included;
   the scanner reads them from the secret store for that purpose alone. The
   scan runs at acceptance (that is what makes `withheld` synchronous) and
   again before posting. A match is withheld and recorded (see "State").
3. **Adds the `-claudebox` signature** if missing, to every comment the control
   plane posts, so any comment type is signed by construction. An unsigned
   control-plane comment would read as human activity and buy the PR another
   round.
4. Enforces per-pass, per-PR and size caps.

## Triggers

Webhooks are the primary trigger. A reconciliation poll runs every few minutes
per installation to cover missed deliveries, and it is the only trigger on a
local trial with no public URL.

Each repo is in one of two trigger modes:

- **`auto`.** Every non-fork PR is a candidate. The existing change gate
  decides when to re-review: the head moved, an unsigned comment arrived, or
  the mode flipped, after the settle window.
- **`requested`.** A non-fork PR is a candidate only once someone with write
  access asks:
  - **Request review from the configured team.** GitHub does not list a
    third-party App in the reviewer picker, so a team stands in for it. This
    **enrolls** the PR: while the request stands, the PR behaves as if the
    repo were in `auto` mode.
  - **Comment `/claudebox review`.** Reviews the PR once at its current head.

**Checking the asker.** A command is first filtered on the
`author_association` GitHub put on the webhook payload: only `OWNER`,
`MEMBER` and `COLLABORATOR` proceed, and anything else is ignored with no API
call and no reply, so strangers cannot spend the control plane's rate limit by
commenting. The survivors are confirmed through GitHub's permission API,
since association alone is not proof of write access, and the result is
cached per (user, repo) for ten minutes. A writer's accepted ask gets a 👀
reaction, and the per-PR check run shows where it stands.

**Asks are durable.** Each ask is a row: PR, head, asker, kind (command or
enrollment), and state (pending, served, refused). The poll recovers a missed
team request from GitHub; a missed command can only be recovered from its
row.

**What gets dispatched** each scheduling pass: pairs on non-fork PRs in `auto`
mode or enrolled whose fingerprint moved, pairs with a pending ask, and owed
pairs that are not parked. An owed pair or a pending ask is dropped when its
PR closes, when the App loses access to the repo, or when the repo goes
public.

### Fork PRs

A PR whose head repo is a fork and whose author lacks write access is not
reviewed in v1, in either mode, and `requested` mode does not change that.
It is not silent: a fork PR gets a `claudebox` check run concluding
`neutral` with "not reviewed: fork PRs are not reviewed by this deployment".
A writer's ask on one gets a 😕 reaction instead of 👀, its row is marked
refused, and it is never redispatched. Approved-fork review is deferred.

### The per-PR check run

Every PR the App sees carries one `claudebox` check run. It is queued when a
pass is scheduled, in progress while one runs, and completes with a
conclusion and a one-paragraph summary:

| State | Conclusion | Summary says |
|---|---|---|
| reviewed | success | how many findings each persona posted, and how many are still queued |
| parked: budget | neutral | which budget (profile or this repo's share), and the UTC reset time |
| parked: round cap | neutral | that an ask is needed to continue |
| parked: usage limit | neutral | the provider is refusing work; retry time |
| parked: failing | neutral | the pair failed three times at this head; it retries when the head moves or someone asks |
| parked: capped | neutral | which per-pair cap was hit and its value; it retries when the head moves or someone asks, and the operator may need to raise the cap |
| retrying | neutral | that a retry is scheduled |
| config invalid | neutral | which file and which field, and what config is in use instead |
| not reviewed: fork | neutral | fork PRs are not reviewed by this deployment |

### Parallelism

PRs run in parallel, bounded by:

- `max_concurrent_jobs` for the deployment (default 4);
- `concurrency` per provider profile, counted in pairs in flight, which keeps
  fan-out from tripping a provider's rate limit;
- `max_concurrent_personas` within a job, as today.

Today's rule that PRs are serialized to bound usage-limit pressure is
replaced in hosted mode by the per-profile cap. Local mode keeps serializing.

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
```

`kindex.vectors` joins the schema in phase 3b, when hosted vector search
ships. Until then the key is unknown, and therefore an error.

**Strictness.** The loader rejects unknown keys, so a typo is an error and
never a silent fall back to the org default. A missing `version` means 1; an
unknown version is an error. Any field the deployment cannot satisfy is an
error: an unknown profile, a model the profile does not allow, an unknown
persona, a team that does not exist, `kindex.store: org` on a repo the org
store is not shared with, or on a public repo.

**Discovery, for authors who are not operators.** A repo author usually
cannot see the deployment file or reach the admin API, so what they need
reaches them through GitHub:

- The generic JSON Schema ships in this repo at a stable path
  (`schema/claudebox.schema.json`), for editor completion; it enumerates
  persona names and the shape of every field.
- The deployment's own catalog (its profiles, each profile's allowed and
  default models, and whether the org store is shared with this repo) is
  printed in the check run on any PR that changes the config file, valid or
  not, and in every `config invalid` check run.

The operator's tools are `claudebox.sh hosted profiles` and `claudebox.sh
config validate FILE --deploy DEPLOY_FILE`; without `--deploy`,
`config validate` checks the schema alone, which anyone can run.

**When validation happens.** On every PR that changes the repo file (at its
head), when the default branch of the repo or of the org `.github` repo
moves, and against every installed repo whenever the deployment config is
loaded (startup and reload), since a deployment edit (a dropped profile, a
narrowed model list, a repo removed from `share_with`) can break many repos
at once.

**When the config in force is invalid**, the repo falls back to the last
config that is valid against the current deployment; if there is none, to
the org defaults; if those are invalid too, the repo is not reviewed.
Whichever applies, the problem shows in every one of that repo's PR check
runs and in `claudebox.sh hosted status`.

**Org defaults need the org `.github` repo in the installation.** An App
installed on selected repositories cannot read one that was not selected.
`app create` tells the installer to include it, and the control plane treats
an unreadable org `.github` repo as "no org defaults", shown in `hosted
status`.

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
      daily: 40000000                 # unit follows the provider; see below
      repo_share: 0.25                # no repo may spend more than this share of the day
    limit_backoff_seconds: 1800       # today's LIMIT_BACKOFF_SECONDS
    per_pair: { requests: 200, tokens: 4000000 }   # per job, reset on every dispatch
max_concurrent_jobs: 4
clone_bytes: { default: 2000000000, overrides: { big-monorepo: 8000000000 } }
admin:
  operators: [some-login]             # status, profiles, kindex push
  transcript_readers: [some-login]
  erasers: [some-login]
retention:
  closed_pr_days: 14
kindex:
  org_store:
    share_with: [api, web, infra]     # or `all-private`; see "kindex"
  voyage_credential: VOYAGE_API_KEY   # optional; phase 3b
```

**Budget units follow the provider type.** The profile loader knows, per
provider type, whether the upstream reports token usage on each response.
Where it does, `budget.daily` and `per_pair.tokens` are tokens; where it does
not, `budget.daily` is passes and `per_pair.tokens` is an error.
`claudebox.sh hosted profiles` prints each profile's unit. The table of which
of today's five provider types report usage (`ollama`, `anthropic`, `custom`,
`cloudflare`, `workersai`) is filled in during phase 2a from live responses,
with a gateway test per type; `custom` is declared by the operator, since its
upstream is unknown.

The provider types are today's `PROVIDER` arms. Their validation is extracted
from `entrypoint.sh`'s `case` block into one module under `reviewer/` that
both `entrypoint.sh` (through the existing `--check` seam) and the gateway's
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

No worker ever holds a provider credential: the gateway's provider route
injects it from phase 2a on. In 2a and 2b the worker holds a GitHub read
token narrowed to its repo and read permissions for one hour; from 2c the
gateway injects that too.

On GCP the worker's service account is reachable by every process in the
worker through the metadata server. It is granted nothing: no IAM roles, not
even permission to invoke the control plane. The worker reaches the private
endpoints over the VPC and authenticates by capability and token alone.

`CLAUDE_CODE_OAUTH_TOKEN` is supported as an `anthropic` profile credential.
It belongs to one person's Claude subscription, and using it to review an
organization's PRs may not be what that subscription's terms allow; the setup
docs say so and recommend an API key or Ollama for an org deployment. Whether
the gateway can inject it the way Claude Code sends it is spike S3.

## The gateway

**Provider route (phase 2a).** Overwrites `model` with the job's model on
every request. For Anthropic-shaped profiles it allows `POST /v1/messages`
and `POST /v1/messages/count_tokens` and nothing else, so batches and files
are unreachable. Where the provider reports usage it counts input plus
output tokens per pair, clamps `max_tokens` to what remains, and caps body
size; where it does not, it caps requests, body size and `max_tokens`, which
it can count exactly. Per-pair caps count within one job and reset at every
dispatch. Hitting one ends the pair as `capped`.

**Usage limits and failures (phase 2a).** The gateway sees each upstream
response and knows its pair from the token, so it classifies every pass's
provider outcome itself:

- A usage limit (429, or a limit body) parks the pair for the profile's
  `limit_backoff_seconds`. Pairs on two different PRs hitting a limit inside
  that window park the whole profile for the same interval.
- Any other provider failure (connection refused, 5xx, a dead translator) is
  counted per profile. Three consecutive failed pairs on a profile, with no
  success between them, park the profile for `limit_backoff_seconds`, as
  today's `MAX_CONSECUTIVE_FAILURES` stops the cycle. A pair that fails three
  times at the same head parks as `failing` until its head moves or someone
  asks.

**GitHub read route (phase 2c).** Uses an installation token minted per job,
narrowed by GitHub to the job's repo and to read permissions, so GitHub
enforces the repo boundary. The gateway's own filter is a second layer: GET,
GraphQL queries, and git smart-HTTP fetch (`GET .../info/refs?service=
git-upload-pack` and `POST .../git-upload-pack`); `git-receive-pack` and
every other POST are refused, and redirects are not followed off github.com.
Per job it caps requests (default 300) and clone bytes (`clone_bytes`), and
it refuses worker reads once the installation's remaining REST or GraphQL
limit falls below a 20% reserve, which keeps the control plane's own checks
working. The worker points `git` at it with `url.<gateway>.insteadOf
https://github.com/`; how `gh` is pointed at it is spike S2.

**Voyage route (phase 3b).** Single-input query embeddings under a length
cap, with a per-pair request cap.

The gateway streams. It does not buffer, cache or log any body, and it does
not retry for the client.

## kindex

A repo chooses `kindex.store: none | org | repo`.

- **`org`** is one store per deployment, pushed by the operator with
  `claudebox.sh hosted kindex push --org`. A private repo using it can quote
  any of its content into its own PRs, including notes about repos its
  contributors cannot see. So it is shared only with the repos the operator
  lists (`kindex.org_store.share_with`), or with every private repo if the
  operator writes `all-private` as a deliberate choice. Public repos can
  never use it. On a target whose worker egress is not confined (Fly, unless
  spike S4 says otherwise) the org store is refused outright, because one
  injected pass there could send the whole store off-host.
- **`repo`** is one store per repository: an operator push (`claudebox.sh
  hosted kindex push --repo owner/name`), or a build from the repo's own
  committed `.kin/` repo-memory on the default branch, rebuilt when that
  branch moves.

Pushes go through the admin API, and the control plane validates each with
the procedure `graph_snapshot.py` uses today (stability check, `quick_check`,
restamp to the `claudebox` profile, warm-up) before replacing the stored copy.
The stored copies are deployment-level and are the only copies the control
plane keeps; a worker streams one with `fetch snapshot` and it dies with the
worker.

**One kindex version.** The control-plane image and the worker image take
their kindex from one build argument. The operator's host kindex only has to
be old enough that the deployment's kindex can migrate its stores on arrival;
a store from a newer host is refused at push time with "upgrade the
deployment's kindex and redeploy both images", the hosted equivalent of
today's "run `claudebox.sh build` again".

**kindex-specific configuration.** The repo file carries only what a repo
owns: which store (and, from 3b, whether to use vectors). The deployment
carries the rest: which repos share the org store, the Voyage key, and per
store the embedding configuration recorded at push time, so the worker's
`kin.yaml` matches it and `ensure_vec_table` keeps the copy's vectors.

In the worker, `kin-mcp` serves the snapshot with `--disallowedTools` built
from `kindex_tools.READ_TOOLS`, as today.

## Visibility

A public repo gets no org store. An internal repo counts as private.
Visibility is checked at dispatch, at `start pair`, and before every post.
A check that errors fails closed: nothing is dispatched or started, and the
pair stays owed with no round counted.

A repo going public, an uninstall, removal from the installation, or deletion
revokes every live capability and token for that repo. A repo going public
also deletes, before any further pass, every content-bearing record keyed to
that repo: transcripts, sessions, outbox items, and withheld-finding records.
If it was on the org store, its access to that store ends with it. Rounds and
asks survive, since they hold no content.

## State

The control plane is authoritative for everything durable. Every
content-bearing store is keyed by repo and PR, so the same deletion paths
reach all of them.

| Item | Store | Content | Lifetime |
|---|---|---|---|
| pair state: fingerprint, round, owed, session id, transcript ref, parked-until, failure count | records | none | the PR, then 14 days |
| capabilities and gateway tokens (hashes) | records | none | until expiry or revocation |
| asks, budgets, park events, audit rows | records | logins only | one year; survive erasure |
| per-detector withheld counts | records | none | one year; survive erasure |
| withheld-finding records (detector, salted hash, redacted body) | records | yes | as transcripts |
| outbox items | records | yes | until posted, PR closed, or repo public |
| transcripts | blob, encrypted | yes | 14 days after close |
| kindex stores | blob, encrypted | yes | until replaced, or the repo or org store is removed |
| job material (prompts) | blob, encrypted | yes | deleted when the job ends |

The records store is SQLite on a volume for the local trial and Fly, with
`secure_delete=ON`, and Cloud SQL Postgres on GCP. The blob store is a
directory, the Fly volume, or a GCS bucket created with soft delete off
(retention 0) and versioning off. The control plane encrypts every blob with
the deployment key before writing it.

### The commit point

Fingerprints, transcripts, rounds and owed flags persist together or not at
all. On `complete pair` the control plane writes the transcript under a new
self-describing immutable name (repo, PR, pair, timestamp), then in one
transaction points the pair at it and applies the outcome's row below. A
crash between the two leaves an unreferenced blob, which the sweep below
removes.

An `ok` needs evidence: the job's capability is live, the transcript parses as
a Claude Code session under a size cap, its session id is the pair's, and the
gateway saw at least one successful provider response on that pair's token.
A pass with no successful provider traffic is `failed`.

| Outcome | Transcript | Fingerprint | Round | Owed | Budget |
|---|---|---|---|---|---|
| `ok` | committed | committed | +1 | cleared | spent |
| `capped` | committed | unchanged | unchanged | stays, parked until the head moves or an ask | spent |
| `usage-limited` | committed | unchanged | unchanged | stays, parked for the backoff | spent only if a provider response succeeded |
| `failed` | discarded; session dropped | unchanged | unchanged | stays; failure count +1 | spent only if a provider response succeeded |
| `revoked` (timeout, access change, restart) | discarded | unchanged | unchanged | stays | spent only if a provider response succeeded |

"Spent" means the gateway's count for the pair is charged: tokens on a
reporting profile, one pass otherwise. A pass the provider refused outright
costs nothing, so an outage cannot exhaust a day's budget; the failure park
above stops it from spinning instead. Usage-limited and capped pairs keep a
resumable session because their transcripts are committed.

### Budgets

A profile's daily budget resets at 00:00 UTC, and an ask does not bypass it.
No single repo may spend more than `repo_share` of a day's budget (default
0.25), so one busy repo parks only itself. `claudebox.sh hosted status` shows
each repo's consumption against its share and the profile's. Every budget is
enforced from the gateway's own counts from phase 2a, so no worker's report
enters it.

### Retention and erasure

The lifetimes are in the state table. Erasure on demand is
`claudebox.sh hosted erase --repo R [--pr N]`, and it is built to be hard to
misfire:

- By default it is a preview: it lists what matches in each store (rows by
  table, blobs by count and bytes) and changes nothing. `--yes` performs it.
- It deletes from both sides: the rows, and every blob whose self-describing
  name matches the repo or PR.
- It prints a deletion manifest and records it as an audit row (who, when,
  scope, counts), so the operator can say what was erased.

An hourly sweep deletes any blob that no row references and that is older
than an hour, which covers a crash between the commit's two writes.

Erasure is immediate in the live stores and complete when backups age out.
The docs state the window per target: Cloud SQL's backup and point-in-time
recovery retention on GCP (the blob bucket has soft delete off, so it adds
nothing), Fly's volume snapshot retention on Fly, and nothing beyond the live
volume locally. Setup proposes the shortest retention each target allows.

## The admin API

`claudebox.sh hosted ...` talks to the control plane's admin API: `status`,
`profiles`, `doctor`, `kindex push`, `erase`, and transcript reads. The
`hosted` namespace keeps local mode's `status`, which shows the repo's
container, meaning what it means today.

**Reachability.** The admin API and `/metrics` are never public:

| Target | How an operator reaches them |
|---|---|
| Local Docker | loopback on the host |
| Fly.io | the app's private network, through `fly proxy` or WireGuard |
| GCP | internal ingress, through IAP or a VPC connection |

`/healthz` carries no data and may be exposed to the platform's health
checker and an external uptime check.

**Authentication.** `claudebox.sh hosted login` runs GitHub's device flow for
the App's user OAuth and caches a user token. Every admin call presents it;
the control plane resolves the GitHub login and checks it against the
deployment's role lists. There are three roles, because the powers differ:
`operators` (status, profiles, doctor, kindex pushes), `transcript_readers`,
and `erasers`. Every transcript read and every erase is an audit row. On the
local trial, whoever holds the box can also read the stores directly, and the
docs say so.

## Single instance, health, and deploys

**Enforced, not assumed.** Cloud Run and Fly both overlap the old and new
instance during a deploy by default. The control plane takes a lock in the
records store at startup (a Postgres advisory lock; on SQLite, the file lock
of a single writer) and does not serve until it holds it. A second instance
waits. Local and Fly deploys stop the old machine before starting the new one.
On taking the lock, the new instance revokes every outstanding capability and
token and stops orphaned workers (see "The worker channel").

**Deploys drain, gateway included.** The control plane and the gateway run
in one container under a small supervisor process. On `SIGTERM` the
supervisor tells the control plane to stop dispatching, keeps both processes
serving while running jobs finish, up to a drain deadline (default 10
minutes, under the platform's grace period where the platform allows one that
long), then revokes what is left and exits both. Revoked pairs stay owed and
run on the new instance. Budget counters are committed as the gateway counts
them, so a crash cannot loosen them.

**Health.** The control plane serves `/healthz`; `/metrics` (Prometheus text:
passes by outcome, outbox depth and oldest item, oldest owed pair's age, last
successful review per repo, webhook deliveries rejected, GitHub rate limit
remaining, budget remaining per profile and per repo); and `claudebox.sh
hosted status`, the same facts for a person.

The docs name the conditions worth paging on, with suggested thresholds:
`/healthz` failing; no successful review anywhere for an hour while PRs are
open; the oldest owed pair older than two hours; the oldest outbox item older
than an hour; a profile parked for failures; webhook signature failures; the
installation suspended or uninstalled; any repo whose config is invalid.
Paging itself is the platform's job.

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
5. It prints what is left, as a checklist `claudebox.sh hosted doctor`
   re-checks against the running deployment: add each profile's provider
   credential to the secret store, write `claudebox.deploy.yml`, create the
   review team if any repo will use `requested` mode, and install the App,
   including the org's `.github` repo if org defaults will be used.

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
| S1 | Which App permission lets the control plane check a user's write access to a repo? | 2b |
| S2 | How does `gh` reach the gateway: `GH_HOST` as an Enterprise host, or an HTTPS proxy? | 2c |
| S3 | Can the gateway inject `CLAUDE_CODE_OAUTH_TOKEN` the way Claude Code sends it? | 2a |
| S4 | Can Fly Machines be confined to the private network for egress? | 4 |
| S5 | Can kindex's Voyage client take a base URL? | 3b |
| S6 | Can baton's Cloud Run provider pin a service to one instance with CPU always allocated, and launch Cloud Run Jobs? If not, the control plane runs on one GCE VM. | 5 |
| S7 | Does the worker's `setpriv` sequence reach `NoNewPrivs: 1` and a zero `CapBnd` on Fly Machines? On Cloud Run Jobs? Answered per target. | 4 (Fly), 5 (GCP) |

## Build order

Each phase ships something usable, and each is split so that a wrong call is
found while it is still cheap to abandon.

**Phase 0: one decision core.** A pure refactor of `reviewer/`: split the
decision core from the group runner, put the state behind a `StateStore`
interface whose only implementation is today's dicts, and extract provider
validation into the shared module. Every existing suite passes unchanged.

**Phase 1: repo config.** The schema, the JSON Schema, the strict loader with
org-default merging, `claudebox.sh config validate` (with `--deploy`), and the
local-mode startup notice.

**Phase 2a: the pipeline.** Control plane (SQLite, local blob dir, polling,
the single-instance lock, durable capabilities revoked at startup), the
gateway's provider route with per-pair tokens, caps, limit and failure
classification, the local Docker job runner, a hardened single-uid worker
holding a narrowed read token and no provider credential, the outbox with the
scan and the signature, the per-PR check run, `auto` mode only, `app create
--target local`, `/healthz` and `hosted status`. At the end an org can create
an App, install it, and get reviews from a laptop, and no worker holds the
org's provider credential.

**Phase 2b: the state machinery.** The change gate against SQL state, the
commit point and every outcome row, durable asks and `requested` mode with
the fork refusals, budgets with repo shares, retention, erasure and the sweep,
the admin API's authentication and roles, the supervisor's drain, `/metrics`.

**Phase 2c: GitHub through the gateway.** The GitHub read route, the worker
channel behind the gateway, the bootstrap token, and egress confinement on
local Docker. After it, no worker holds any credential and its only peer is
the gateway.

**Phase 3a: hosted kindex.** Store pushes, `share_with`, per-repo stores from
`.kin/`, the one-version build argument.

**Phase 3b: hosted vector search.** The Voyage route, `kindex.vectors`, and
per-store embedding configuration, after spike S5.

**Phase 4: Fly.** The Fly Machines job runner, Fly secrets in `app create`,
webhooks, volume-backed stores, the egress decision from spike S4, the
hardening answer from spike S7.

**Phase 5: GCP via baton.** The Cloud Run Jobs job runner, Postgres and GCS
stores, Secret Manager, a `baton.yaml`, the network rules that confine
workers to the gateway, the hardening answer from spike S7.

## Testing

The no-Docker, no-network suites stay the main line of defense.

- **Phase 0** adds no tests and changes none; every existing suite passing
  untouched is the proof.
- **Phases 1 to 3** add `unittest` suites that stub at the seams, as
  `test-python.sh` does: the config schema, strictness and fallbacks
  (including a deployment edit that invalidates an installed repo); trigger,
  ask and fork-refusal decisions against recorded webhook payloads; every
  outcome row against an in-memory `StateStore`; the outbox's retry,
  idempotency and public-flip deletion; startup revocation fencing an
  orphan's calls; budget shares, failure parking and usage-limit parking; the
  gateway's allowlists (including the git smart-HTTP pair and a refused
  `git-receive-pack`) and caps against a fake upstream; the scan; erase's
  preview and manifest; and the channel contract tested from both ends against
  one shared fixture set.
- **The worker** gets a bash suite in the style of `test-personas.sh`: stub
  `claude` and `gh`, run it against a fake control plane, and assert that a
  pair run twice resumes its own session and that the reviewer runs with
  `NoNewPrivs: 1` and a zero `CapBnd` after the `setpriv` sequence.
- **Each job runner** gets a live smoke test run by hand against its target,
  like `claudebox.sh test` today.

## Deferred

- **Approved-fork review.** When it comes back it needs its own profile with
  no fallback to the repo's, a per-head approval stored on the ask row, and a
  reduced worker without kindex.
- **Hosted Linear.** Local mode keeps its Linear MCP.
- **The two-uid worker,** which would keep the capability from the reviewer.
  It starts with a written account of what the reviewer would gain from the
  capability that it does not already have.
- **A per-installation git mirror,** once clone volume is measured.
- **Matching posted comments against retrieved kindex notes.**

## Out of scope

- More than one control-plane instance.
- Multi-tenant hosting.
- A web UI for config.
- A pass-log store separate from transcripts; transcript readers read
  transcripts through the admin API.
- Per-PR encryption keys; the documented erasure window replaces them.
- Persisting state in local mode.
