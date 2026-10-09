# claudebox as a hosted GitHub App: design

**Date:** 2026-10-09
**Status:** plan, revision 9, filed for review under the `plan` label

## Problem

claudebox serves one repository per container. An operator mounts that repo's
`.git`, hands over a fine-grained PAT and one provider credential, and picks
PRs with a selector. An organization that wants every PR in thirty repos
reviewed has to run thirty containers, hold thirty PATs, and edit thirty env
files to change a model.

The ask is a GitHub App that an organization creates and hosts for itself.
Installing it on a repo turns on review for that repo's PRs from branches in
the repo itself; no PR whose head repository is a fork is reviewed in v1. Each repo
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
Revisions 2 to 4 answer claudebox's own review of this PR. Revision 2
phased the build by threat model and deferred approved-fork review, hosted
Linear and the two-uid worker. Revision 3 moved the gateway's provider route
into the first shipped phase, which takes the org's provider credential out
of the worker before any PR is reviewed and makes the gateway the one
witness for budgets and usage limits. Revision 4 brought the GitHub read
route forward to phase 2b, named the gateway as the author of a pair's
provider outcome, gave the outbox terminal states, and added a bring-up path
for each target. Revision 5 stops dropping findings when a PR's head moves,
puts every cap in one table with a key, defines how the deployment file
reaches a running control plane, and creates check runs from dispatch
instead of only from webhooks. Revision 6 raises the bar for what a review
round changes: it fixes what touches an architectural decision, the phase
order or a security property, and moves implementation detail to the phase
specs (see "Decided in the phase specs").

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
                                             └──▶ providers (2a), GitHub reads (2b), Voyage (3b)
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

**gateway.** A separate process in the control-plane deployment, running as
its own uid with only the credentials it injects (see "Secrets"). It injects
real credentials at the edge, enforces per-pair caps, and is the sole witness
of every pair's provider outcome. Its routes arrive in phases: the provider
route in 2a, the GitHub read route in 2b, the worker channel in 2c, the
Voyage route in 3b. The Workers AI translator (LiteLLM and
`workersai-shim.py`) moves behind it.

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
the deployment file (`caps.clone_bytes.overrides`). A pair that hits the cap
parks as `too large`, which names the measured size and the key and pages
the operator once. The pair is retried, and the head tree measured again,
when the repo's `clone_bytes` override changes or the head moves; a retry
that is still over the cap leaves it in the same `too large` state, so it
does not page again. An ask alone does not retry it. A per-installation bare mirror that would make clones
local is deferred until clone volume is measured.

### Resuming a session in a fresh container

A transcript blob becomes a resumable session only if Claude Code finds it
where `--resume` looks: `~/.claude/projects/<encoded cwd>/<session id>.jsonl`.
So the cwd and `$HOME` must be identical on every target. The worker clones to
`/home/reviewer/work/repo`, under the reviewer's own home, where today's
image already makes it writable, and it creates and proves that directory as
the reviewer the way `entrypoint.sh` does today, dying if it cannot. `HOME`
is set explicitly after the uid change (see "Worker hardening"), never
inherited. Before starting a pair the worker writes that pair's transcript to
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
--regid=reviewer --init-groups --reset-env`. Root holds `CAP_SETPCAP` at that
moment, which is what emptying the bounding set needs, and `--reset-env`
keeps root's environment from leaking into the reviewer's. It also clears the
job spec, which arrives in the environment, so before the uid change root
writes the job spec to a mode-600 file in the reviewer's home, which the
worker reads and unlinks; the single-use token never reaches the worker's
environment or its argv. After the change a fixed wrapper sets exactly
`HOME=/home/reviewer` and a `PATH` that puts the `gh` shim's directory
first, and execs the worker.
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
| `fetch job` | exchanges the bootstrap token for the job capability; returns personas, prompts, round lines, the group's pairs, their transcripts, the kindex store reference, a GitHub read token for the pairs (from 2b a gateway token; in 2a a narrowed installation token) |
| `start pair` | re-checks visibility and access, then returns the pair's provider token; fails closed, leaving the pair owed and its round unchanged |
| `fetch snapshot` | the kindex store, streamed |
| `post finding` | one comment for one pair, with an idempotency key |
| `complete pair` | one pair's transcript and session id, and the worker's own notes |

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
capability, its group's gateway tokens and the snapshot. With them it can
post a comment as the bot (which it can already do by design, subject to the
scan) and read the repo it is reviewing. It cannot misreport how its pairs
ended, because the gateway writes that (see "The commit point"). From 2b it
holds no credential that works anywhere but the gateway.

**Phase 2a is a trial phase.** In 2a, before the read route exists, the
worker holds a real installation token narrowed to its repo and read
permissions for one hour. That token works from anywhere, so a prompt-injected
pass could send it off-host and read the repo for the rest of the hour, the
same shape as the org-store risk on an unconfined target. So a 2a deployment
is for trials on repos whose contributors the operator trusts: the org store
is refused while workers hold read tokens, `hosted doctor` says so, and the
read route in 2b removes the token. A two-uid worker that keeps the
capability from the reviewer is deferred.

### The `gh` shim

The worker puts a `gh` shim ahead of the real `gh` on `PATH`. `gh pr comment`
becomes `post finding`. Other `gh` calls use the read token in 2a and the
gateway's GitHub read route from 2b. Persona text keeps saying `gh pr
comment`. `GH_STANZA` gains one sentence: a read the deployment refuses means
stop the review, not guess at the diff.

`post finding` returns as soon as the control plane has accepted the finding:

- **queued**: exit 0, print "queued". The finding is in the outbox (below),
  and delivery is now the control plane's job, independent of this pass.
- **withheld**: the scan matched a credential. Exit non-zero and tell the
  model the finding contained a credential-shaped string, was not posted, and
  should be restated without the literal value. The review continues, so a
  reviewer that finds a committed secret still reports it without reprinting
  it.
- **too long**: the body is over the comment size cap. Exit non-zero and
  tell the model to shorten it and post again.
- **enough**: the pass has posted `caps.comments_per_pass` findings (default
  25). Exit non-zero and tell the model to stop posting and finish its
  review. The pass is not cut short and its outcome is unaffected. The
  control plane counts accepted findings per pair per job, which is what
  makes this possible; the findings it turns away are counted on the check
  run and in `/metrics` as not posted.

Both caps are enforced here, at acceptance, so the outbox only ever holds
items it can post. A per-pass limit bounds what one prompt-injected pass can
post without ending any review mid-pass.

## Posting and the outbox

Every finding the control plane accepts goes into a durable **outbox**, keyed
by its idempotency key (job, pair, sequence number). One queue per
installation drains it, paced at no more than one comment a second and 400 an
hour by default, honoring `Retry-After` on a secondary limit. An outbox item
outlives its job, so a slow post never costs a review and never forces a
re-review. Each item records the PR head it was written against.

A push does not drop anything. A finding is a true statement about the head
it was written against, and the next round reviews only what the push
changed, so a finding dropped for staleness would be lost for good, and the
findings a push leaves untouched are the ones most likely still valid. So
when the PR's head has moved past an item's head, the item still posts, with
a first line naming the commit it was written against, the way a human
reviewer's late comment would.

A late post must not move what the next round reads. Today's round ladder
finds "the commits since your last review" from the date of the persona's
newest signed comment, and a late post would push that date past commits no
one has reviewed. So in hosted mode the round line the control plane hands
each pass names the head that pair last reviewed (from its committed
fingerprint), and the scope is the commits after that head. Two cases
widen it to the whole PR: if that head is no longer an ancestor of the
current head (a rebase or force-push rewrote the branch), the pass runs as
round 1 at the `should-fix` floor and the check run says the branch was
rewritten (scope only: the pair's round count, which `rounds.cap` measures,
still advances); and if the pass answers an ask at a head the pair has already
reviewed, the scope is the whole PR at the pair's current rung. The decision core
takes the head as an optional input; local mode, which posts synchronously,
passes none and keeps today's procedure.

An item leaves the outbox in exactly one of these ways:

- **posted**;
- **dropped as undeliverable**: the PR closed, the App lost access to the
  repo, the repo went public, or GitHub answered with an error that retrying
  cannot fix (404, 410, 422, a locked conversation, a 403 that is not a rate
  limit).

Only `Retry-After` and 5xx answers are retried. Every drop is recorded and
counted in `/metrics`, and the PR's check run reports findings dropped
alongside findings posted, so its count is honest about what never arrived. A
pass whose findings are still queued when it ends commits as an ordinary
`ok`; posting them is the outbox's job from then on.

Before each post the queue:

1. **Checks the repo's visibility against GitHub, uncached.** A repo that went
   public or became unreachable drops its items as undeliverable.
2. **Scans the body** for known secret shapes and for the literal value of
   every credential the deployment holds, provider and Voyage keys included;
   the scanner reads them from the secret store for that purpose alone. The
   scan runs at acceptance (that is what makes `withheld` synchronous) and
   again before posting. A match is withheld and recorded (see "State").
3. **Adds the `-claudebox` signature** if missing, to every comment the control
   plane posts, so any comment type is signed by construction. An unsigned
   control-plane comment would read as human activity and buy the PR another
   round.

The size and per-pass caps are enforced at acceptance (above), not here, so
they never strand an item. The queue drains round-robin across PRs, so one
PR with a deep backlog cannot hold back another PR's findings.

## Triggers

Webhooks are the primary trigger. A reconciliation poll runs every few minutes
per installation to cover missed deliveries, and it is the only trigger on a
local trial with no public URL.

Each repo is in one of two trigger modes:

- **`auto`.** Every PR is a candidate. The existing change gate
  decides when to re-review: the head moved, an unsigned comment arrived, or
  the mode flipped, after the settle window.
- **`requested`.** A PR is a candidate only once someone with write access
  asks:
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

**What gets dispatched** each scheduling pass: pairs on PRs in `auto` mode
or enrolled whose fingerprint moved, pairs with a pending ask, and owed pairs
that are not parked. Fork PRs are never candidates (below). An owed pair or a pending ask is dropped when its
PR closes, when the App loses access to the repo, or when the repo goes
public.

### Fork PRs

No PR whose head repository is a fork is reviewed in v1, in either mode,
whoever its author is. The test is the head repository alone; it needs no
permission check and is implemented in 2a. A writer's ask on a fork PR gets a
😕 reaction instead of 👀, its row is marked refused, and it is never
redispatched; `hosted status` lists refused asks. Fork PRs get no check run,
and the docs say so. Approved-fork review is deferred.

### The per-PR check run

Every non-fork PR the App sees carries one `claudebox` check run on its
current head. A check run belongs to a commit. The Checks API does not deduplicate runs
by name, so the control plane records each run's id against its head in the
records store and creates a run only for a head with none recorded. It does
so wherever it learns of a head:
on the `pull_request` `opened`, `synchronize` and `reopened` events, for
every candidate head the reconciliation poll enumerates (which is the only
source on a local trial with webhooks off, and covers missed deliveries and
PRs opened before the App was installed), and whenever a pass is scheduled
for a head that has none. It starts in the state that head is in, is queued
when a pass is scheduled, in progress while one runs, and completes with a
conclusion and a one-paragraph summary.

The run for a head keeps counting after its pass completes: the outbox
updates the run of the head each finding was written against as items post
or drop, so its counts settle once the outbox drains.

Its states mirror the pair's: not requested (naming both ways to ask),
reviewed, and one per park reason (budget, round cap, usage limit, failing,
capped, too large, refused read, config invalid), each saying why and what
clears it. The exact wording of each state is decided in the phase 2a spec.

The conclusion is never `failure`, and `success` only ever means "reviewed
and found nothing". claudebox does not assert a PR is acceptable; a check
that can gate a merge would be a separate, deliberate decision.

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

**When the repo file is invalid**, the repo falls back to the org defaults;
if those are invalid too, the repo is not reviewed. There is no "last valid
config" rung: it would need a stored copy of repo content with its own
lifetime, and after a deployment edit it would quietly run a config that was
dropped for a reason.
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
      unit: tokens                    # tokens | passes; checked against the provider
      daily: 40000000
      repo_share: 0.25                # no repo may spend more than this share of the day
    limit_backoff_seconds: 1800       # today's LIMIT_BACKOFF_SECONDS
    per_pair: { requests: 200, tokens: 4000000 }   # per job, reset on every dispatch
max_concurrent_jobs: 4
timing:
  poll_seconds: 300                   # reconciliation poll; 60 when webhooks are off
  settle_seconds: 30                  # today's SETTLE_SECONDS
  max_passes_per_session: 0           # today's MAX_PASSES_PER_SESSION; 0 never rotates
caps:                                 # see "Caps" for every cap and its default
  github_reads_per_pair: 300
  comments_per_pass: 25
  comment_bytes: 60000
  clone_bytes: { default: 2000000000, overrides: { big-monorepo: 8000000000 } }
retention:
  closed_pr_days: 14
kindex:
  org_store:
    share_with: [api, web, infra]     # or `all-private`; see "kindex"
  voyage_credential: VOYAGE_API_KEY   # optional; phase 3b
```

**Budget units are written down, never inferred.** `budget.unit` is
required, so the file says what its numbers mean. `tokens` is valid only for
a provider type known to report usage on every response, and with it
`per_pair.tokens` applies; `passes` is valid for every type, and with it
`per_pair.tokens` is an error. Which of today's five types report usage
(`ollama`, `anthropic`, `custom`, `cloudflare`, `workersai`) is established in
phase 2a from live responses, with a gateway test per type. `custom` is
treated as non-reporting unless the profile adds `usage: reported`, the one
place the operator vouches for an upstream claudebox cannot know. If a
profile set to `tokens` gets a response without usage, the gateway fails that
pair closed and pages, rather than counting it some other way.

The gateway counts both units itself: tokens from each response's usage, and
a pass as a pair token that received at least one successful provider
response. So the noun in the file, the noun in the outcome table and the
noun the gateway counts are the same.

**Caps.** Every cap in the design, in one place:

| Cap | Default | Key | When hit |
|---|---|---|---|
| provider requests per pair | 200 | `profiles.*.per_pair.requests` | pair `capped` |
| provider tokens per pair | 4,000,000 | `profiles.*.per_pair.tokens` | pair `capped` |
| GitHub reads per pair | 300 | `caps.github_reads_per_pair` | pair parked, `refused read` |
| comment size | 60,000 bytes | `caps.comment_bytes` | model told to shorten |
| comments per pass | 25 | `caps.comments_per_pass` | model told to stop posting |
| job deadline | 55 minutes | fixed (under the read token's hour) | running pairs `revoked`, stay owed |
| clone bytes per job | 2 GB | `caps.clone_bytes` | pair parked, `too large` |
| provider request body | 4 MB | fixed | request refused |

Today's environment knobs map as follows: `REVIEW_INTERVAL_SECONDS` becomes
`timing.poll_seconds` (webhooks make it a backstop rather than the cadence);
`SETTLE_SECONDS`, `MAX_PASSES_PER_SESSION` and `LIMIT_BACKOFF_SECONDS` keep
their meaning under the keys above; `PERSONAS`, `PLAN_PERSONAS`, `PLAN_LABEL`
and `MAX_CONCURRENT_PASSES` become repo-file keys. The PR selectors have no
hosted equivalent: the installation's repo selection and the trigger mode
replace them. Neither do the prompt overrides or `MAX_CYCLES` in v1.

**How the file reaches the control plane.** The deployment file is stored in
the records store, not baked into an image. `claudebox.sh hosted config
apply FILE` uploads it through the admin API, the control plane validates it
(and every installed repo against it), and it takes effect without a
restart: jobs already running keep the config they were dispatched with, new
jobs get the new one, and nothing is revoked, with one exception. An apply
that narrows a repo's kindex access (the repo leaves `share_with`, its store
changes away from `org`, or the org store is removed) is an access change
for that repo, handled as loss of access is: its queued items are dropped,
its pairs' sessions and transcripts are deleted so the next pass starts
fresh, and its running jobs are revoked. Applying a file is role-gated
(`operators`), the first time included: admins are platform configuration
that exists before any deployment file (see "Bringing a deployment up"). The file is loaded with a safe YAML loader (no
tags, no anchors that expand past a size cap) and capped at 256 KB, and the
same loader reads repo files, which are capped at 64 KB.

The provider types are today's `PROVIDER` arms. Their validation is extracted
from `entrypoint.sh`'s `case` block into one module under `reviewer/` that
both `entrypoint.sh` (through the existing `--check` seam) and the gateway's
profile loader call. Local mode keeps dying at startup on a mis-wired
credential, and `test-providers.sh` keeps a local-mode case asserting it.

## Secrets

Platform secret mounts hold control-plane secrets only: the App private key,
the webhook secret, the deployment encryption key, each profile's provider
credential, and the optional Voyage key.

Inside the control-plane container, those split by process. The supervisor
starts as root, reads the platform's secrets (an env file, `fly secrets` as
environment, Secret Manager bindings), and starts each process as its own uid
with only its own secrets: the control plane gets the App key, the webhook
secret and the deployment key; the gateway gets the provider and Voyage
credentials, plus the per-job GitHub read tokens the control plane pushes to
it (below). The supervisor then drops to an unprivileged uid itself. The
scanner, which needs every literal, runs in the control plane and is handed
the provider literals by the supervisor at startup.

The point of the split is narrow: the gateway, which proxies worker traffic
and parses provider responses, cannot read the App key, which would let it
mint a token for any installed repo. It does not make the control plane a
low-exposure process. The control plane parses webhook payloads any GitHub
user can trigger and repo-authored config files, and it holds the most, so
what it parses is constrained (signature-verified webhooks, the safe YAML
loader and size caps above), and the one repo-authored input that is a whole
database, a repo's committed `.kin/` store, is never opened in it: per-repo
stores are built by a job the job runner starts, holding no secret (see
"kindex").

**The gateway's GitHub read tokens.** The control plane mints each job's
narrowed installation token and pushes it to the gateway at `fetch job`. The
gateway keeps it in memory only, until the job ends. The token is minted at
`fetch job` and lives an hour, and a job's 55-minute deadline starts at the
same call, so a token always outlives its job.
When the gateway starts, the control plane re-pushes the token of every
unexpired job; a pair whose token is lost mid-pass anyway parks as `refused
read`. No GitHub token is ever written to disk.

| Target | Control-plane secrets | Workers get |
|---|---|---|
| Local Docker | an env file, mode 600, written by setup | a container started with no env file |
| Fly.io | `fly secrets` on the control-plane app | a separate Fly app with no secrets set |
| GCP | Secret Manager, bound to the control-plane service | a Cloud Run job with no secret bindings |

No worker ever holds a provider credential: the gateway's provider route
injects it from phase 2a on. In 2a only, the worker holds a GitHub read token
narrowed to its repo and read permissions for one hour (see "Phase 2a is a
trial phase"); from 2b the gateway injects that too.

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

**The gateway is the author of every pair's provider outcome (phase 2a).** It
sees each upstream response and knows its pair from the token, and it writes
a per-token record: successes, tokens, the first usage limit, the first
failure, a cap hit. The control plane is the records store's only writer.
The gateway enforces per-pair caps from its own counts, so provider traffic
never waits on the control plane, and reports each response's counts to it
asynchronously; the control plane commits them and acknowledges. What a
gateway crash can lose is the reports not yet acknowledged, a window the
per-pair caps bound. If the control plane is unreachable, the gateway keeps
serving within the caps and buffers its reports; a failure inside claudebox
is never classified as a provider failure and never parks a profile.

The two processes talk over a unix socket in their shared container, owned
by a group only those two uids belong to, and every message in both
directions carries a per-deployment secret the supervisor hands to both at
startup. A worker, in its own container, cannot reach it. When the gateway
starts, the control plane pushes it the committed counts it enforces against
(each live pair's usage so far, and the day's budget use per profile and per
repo) along with the live jobs' read tokens. Until it has them, a starting
gateway serves no provider traffic, so a restart never resets a cap; the
requests it holds back in that window are a claudebox fault (below), not a
provider failure.

At `complete pair` the control plane first asks the gateway to flush that
pair's token. Reports carry a per-token sequence number and the flush
answers with the last one sent, so the control plane knows when it holds
every report, not just that the gateway is up. Then it applies the record,
together with its own record of clone outcomes. The worker contributes the
transcript, the session id and its own notes, never the outcome. If the
record is complete and shows no successful provider response, the pair is
`failed`.

If the flush cannot complete (the gateway is unreachable, or reports are
missing), that is a claudebox fault, never a provider failure, and the pair
is held **pending**: its transcript is stored, nothing else is applied, it is
not redispatched, and the control plane retries the flush in the background
until it completes and the ordinary row applies. A pending pair costs no
pass and loses nothing. A pair pending for more than ten minutes pages, as a
claudebox fault.

- A usage limit (429, or a limit body) parks the pair for the profile's
  `limit_backoff_seconds`. Pairs on two different PRs hitting a limit inside
  that window park the whole profile for the same interval.
- Any other provider failure (connection refused, 5xx, a dead translator) is
  counted per profile. Three consecutive failed pairs on a profile, with no
  success between them, park the profile for `limit_backoff_seconds`, as
  today's `MAX_CONSECUTIVE_FAILURES` stops the cycle. A provider failure parks
  a single pair for the same time-boxed interval, never longer, and any
  success on the profile resets every pair's failure count.
- A failure the control plane can pin on the pair itself (a transcript that
  will not parse, a session id that is not the pair's) parks it as `failing`
  until its head moves or someone asks. Those are the only failures that
  wait on a human.

**GitHub read route (phase 2b).** Uses an installation token minted per job,
narrowed by GitHub to the job's repo and to read permissions, so GitHub
enforces the repo boundary. The gateway's own filter is a second layer: GET,
GraphQL queries, and git smart-HTTP fetch (`GET .../info/refs?service=
git-upload-pack` and `POST .../git-upload-pack`); `git-receive-pack` and
every other POST are refused, and redirects are not followed off github.com.
It caps requests per pair (default 300, so a group of eight plan personas
does not share one allowance) and clone bytes per job (`clone_bytes`), and it
refuses worker reads once the installation's remaining REST or GraphQL limit
falls below a 20% reserve, which keeps the control plane's own checks
working. A refused read parks the pair the way a usage limit does, with the
`refused read` check-run state, and `GH_STANZA` tells the persona that a
refused read means stop. The worker points `git` at the route with
`url.<gateway>.insteadOf https://github.com/`; how `gh` is pointed at it is
spike S2.

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

Pushes go through the admin API. The stored copy is replaced only after a
store job (a worker started by the job runner, holding no secret and no
token beyond a read of the store's source) has validated it with the
procedure `graph_snapshot.py` uses today: stability check, `quick_check`,
restamp to the `claudebox` profile, warm-up. A repo's committed `.kin/` store
is built the same way. Neither ever opens in the control plane.
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
revokes every live capability and token for that repo, and deletes, before
any further pass, every content-bearing record keyed to that repo:
transcripts, sessions, outbox items, withheld-finding records, and its
per-repo kindex store. A public repo also loses the org store. Rounds and asks
survive a public flip, since they hold no content; on loss of access the
repo's asks and owed pairs are dropped too, since nothing can serve them.

## State

The control plane is authoritative for everything durable. Every
content-bearing store except the org kindex store is keyed by repo (and PR
where it has one), so the same deletion paths reach all of them. The org
store is the exception: it is one blob for the deployment, and kindex nodes
carry no repo key, so nothing can delete "what the org store says about repo
R" by name (see "Retention and erasure").

| Item | Store | Content | Lifetime |
|---|---|---|---|
| pair state: fingerprint, round, owed, session id, transcript ref, parked-until, failure count | records | none | the PR, then 14 days |
| capabilities and gateway tokens (hashes) | records | none | until expiry or revocation |
| check-run ids per head | records | none | the PR, then 14 days |
| per-token outcome records (successes, tokens, limits, failures, cap hits) | records | none | the pair |
| asks, budgets, park events, audit rows | records | user ids only | one year; survive erasure |
| per-detector withheld counts | records | none | one year; survive erasure |
| withheld-finding records (detector, salted hash, redacted body) | records | yes | as transcripts |
| outbox items | records | yes | until posted or dropped (see "Posting and the outbox") |
| transcripts | blob, encrypted | yes | 14 days after close |
| per-repo kindex stores | blob, encrypted | yes | until replaced, or the repo is removed or goes public |
| the org kindex store | blob, encrypted | yes, about many repos | until the operator replaces or removes it |
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

The outcome comes from the gateway's record for the pair's token (see "The
gateway"), never from the worker. An `ok` additionally needs the job's
capability to be live, the transcript to parse as a Claude Code session under
a size cap, and its session id to be the pair's. A pair whose token has no
gateway record, or no successful provider response, is `failed`.

| Outcome | Transcript | Fingerprint | Round | Owed | Budget |
|---|---|---|---|---|---|
| `ok` | committed | committed | +1 | cleared | spent |
| `capped` (a provider cap) | committed | unchanged | unchanged | stays, parked until the head moves or an ask; pages | spent |
| `too large` (the clone cap) | none | unchanged | unchanged | stays, parked until the override changes or the head moves; pages once | not spent |
| `usage-limited` | committed | unchanged | unchanged | stays, parked for the backoff | spent only if a provider response succeeded |
| `budget` (the day's budget ran out mid-pass) | committed | unchanged | unchanged | stays, parked until the UTC reset; does not page | spent |
| pending (a claudebox fault; see "The gateway") | stored | unchanged until the flush completes | unchanged | not redispatched | not spent until the flush completes |
| `failed` | discarded; session dropped | unchanged | unchanged | stays, parked for the backoff (provider failure) or until the head moves (pair failure) | spent only if a provider response succeeded |
| `refused read` | committed | unchanged | unchanged | stays, parked for the backoff | spent |
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
- The manifest always lists the org kindex store as **not erased**, with the
  reason, when the repo was among those it is shared with. Erasing what the
  org store holds about a repo means the operator pushes a store rebuilt
  without those notes, or removes the store.

An hourly sweep deletes any blob that no row references and that is older
than an hour, which covers a crash between the commit's two writes.

Erasure is immediate in the live stores and complete when backups age out.
The docs state the window per target: Cloud SQL's backup and point-in-time
recovery retention on GCP (the blob bucket has soft delete off, so it adds
nothing), Fly's volume snapshot retention on Fly, and nothing beyond the live
volume locally. Setup proposes the shortest retention each target allows.

## The admin API

Two kinds of command live under `claudebox.sh`. **Platform-scoped** commands
use the operator's own platform credentials (Docker, `fly`, `gcloud`/baton)
and never touch the admin API: `app create` and `hosted deploy`. **Role-gated**
commands go through the control plane's admin API: `hosted login`, `config
apply`, `status`, `profiles`, `doctor`, `kindex push`, `erase`, `withheld`
(withheld-finding records), and transcript reads. The `hosted` namespace
keeps local mode's `status`, which shows the repo's container, meaning what
it means today.

**Reachability.** The admin API and `/metrics` are never public:

| Target | How an operator reaches them |
|---|---|
| Local Docker | loopback on the host |
| Fly.io | the app's private network, through `fly proxy` or WireGuard |
| GCP | internal ingress, through IAP or a VPC connection |

`/healthz` carries no data and may be exposed to the platform's health
checker and an external uptime check.

**Authentication.** `claudebox.sh hosted login` runs GitHub's device flow for
the App's user OAuth and caches the user token. The device flow keeps the
credential-bearing code off any local port: an interceptor gets nothing it
can redeem. GitHub requires the device flow to be enabled in the App's
settings, so `app create` lists that as a checklist step unless spike S8
finds the manifest can set it, and `hosted doctor` checks it. Every admin
call presents the token; the
control plane resolves it to a GitHub numeric user id and checks that against
the role lists (platform configuration; see "Bringing a deployment up"). Logins are never matched, because a renamed
account releases its login for someone else to claim. There are three roles,
because the powers differ: `operators` (status, profiles, doctor, kindex
pushes), `transcript_readers` (transcripts and withheld-finding records), and
`erasers`. Every transcript read and every erase is an audit row. On the
local trial, whoever holds the box can also read the stores directly, and the
docs say so.

## Single instance, health, and deploys

**Enforced, not assumed.** Cloud Run and Fly can both overlap the old and new
instance during a deploy. The control plane takes a lock in the records store
at startup (a Postgres advisory lock; on SQLite, the file lock of a single
writer). Until it holds the lock, an instance answers `/healthz` with 200 and
"waiting for lock", so the platform's probe passes and the old instance is
told to stop, and it answers webhooks with 503 and dispatches nothing; the
deliveries it refuses are recovered by the reconciliation poll once it holds
the lock. On taking the lock, the new instance revokes every outstanding
capability and token and stops orphaned workers (see "The worker channel").

Deploy order per target: local, `docker compose` stops the old container
first; Fly, `hosted deploy` uses the `immediate` strategy for the one
control-plane machine, which replaces it in place; Cloud Run, overlap cannot
be avoided, and the lock is what makes it safe.

**Deploys drain, gateway included.** The control plane and the gateway run
in one container under a small supervisor process. On `SIGTERM` the
supervisor tells the control plane to stop dispatching, keeps both processes
serving while running jobs finish, up to a drain deadline, then revokes what
is left and exits both. The deadline is the platform's grace period: up to 10
minutes locally and on Fly (where `kill_timeout` is set to match), and
seconds on Cloud Run, where a deploy therefore revokes running jobs almost at
once. That is a real cost on GCP, and spike S6's fallback (one GCE VM)
removes it. Revoked pairs stay owed and
run on the new instance. Budget counters are committed as the gateway
reports them (see "The gateway"), so a crash loosens them by at most the
responses in flight.

**Health.** The control plane serves `/healthz`; `/metrics` (Prometheus text:
passes by outcome, outbox depth and oldest item, oldest owed pair's age, last
successful review per repo, webhook deliveries rejected, GitHub rate limit
remaining, budget remaining per profile and per repo, findings withheld by
detector, outbox items dropped by reason, pairs parked by reason); and
`claudebox.sh hosted status`, the same facts for a person, including the
latest withheld finding per repo.

The docs name the conditions worth paging on, with suggested thresholds:
`/healthz` failing; no successful review anywhere for an hour while PRs are
open; the oldest owed pair that is not parked older than two hours; any pair
parked as `failing`, `capped` or `too large` (each waits on a person: a
broken pair, a cap value, a clone override); the oldest outbox item
older than an hour; a profile parked for failures; withheld findings on any
repo (a reviewer quoting a credential is the earliest sign of consequence
#1); webhook signature failures; the installation suspended or uninstalled;
any repo whose config is invalid. Parked pairs that are waiting by design
(round cap, budget) are excluded from the owed-age condition. Paging itself
is the platform's job.

## Bringing a deployment up

`claudebox.sh hosted deploy --target local|fly|gcp` creates or updates
everything the control plane runs on, in the same numbered way `app create`
does, and is safe to re-run:

- **local**: builds both images and starts the control-plane container with
  `docker compose`, on a Docker network the workers will join.
- **fly**: creates the control-plane app with a volume and the workers' app
  with no secrets, then deploys the control-plane image.
- **gcp**: generates a `baton.yaml` for the control plane and gateway and runs
  `baton` to create the service, the job definition, the Cloud SQL database,
  the GCS bucket and the network rules.

A control plane with no App credentials or deployment file yet starts in a
"not configured" state: `/healthz` answers and nothing is dispatched. The
order is:

1. `hosted deploy`, which creates the platform resources.
2. `app create`, which writes the App's secrets into them.
3. The provider credentials, added to the platform secret store by the
   operator.
4. `hosted deploy --admins ADMINS`, which writes the admin roles into the
   platform secret store and restarts the control plane. `ADMINS` is a small
   YAML file mapping each role (`operators`, `transcript_readers`,
   `erasers`) to a list of numeric GitHub user ids, and only ids;
   `claudebox.sh hosted whois LOGIN` prints a login's id for the operator to
   check and copy, and nothing resolves logins on the write path.
5. Enable the device flow in the App's settings (unless spike S8 lets the
   manifest do it).
6. `claudebox.sh hosted login`, then `claudebox.sh hosted config apply FILE`,
   an ordinary role-gated call, since an operator now exists.

**Admins are platform configuration, not deployment configuration.** The
role lists live only in the platform secret store, never in the deployment
file, and only `hosted deploy --admins` changes them; `config apply` cannot.
So there is one source of truth for who may administer the deployment, its
root is platform access (the same access that can already read every
secret), and revoking an admin is a `hosted deploy --admins` that takes
effect on the restart it causes. There is no code to intercept and no admin
call that works without a role. `hosted login` prints the numeric id it
resolved, and `hosted deploy` warns when the operators list does not contain
the id of the person running it.

The deployment file has one copy, in the records store, and changes only
through `config apply`. There is no seed: operators keep the file in version
control, and after a lost store they apply it again. `hosted deploy` also reports the control plane's state, which is
how an operator diagnoses a deployment no one can log into yet. The control
plane itself gets a live smoke test per target like the job runners do.

## Creating the App

`claudebox.sh app create --org ORG --target local|fly|gcp [--url URL]` uses
GitHub's App manifest flow:

1. It binds a one-shot HTTP server to an ephemeral loopback port chosen by
   the OS, and only once it is listening opens the browser on a page that
   POSTs a generated manifest, with a random `state`, to
   `https://github.com/organizations/ORG/settings/apps/new`. The server
   accepts exactly one request whose path and `state` match, exchanges the
   code itself, at once, and exits. The manifest code is the credential
   (`POST /app-manifests/{code}/conversions` needs nothing else), so it never
   leaves that process.
2. The org owner reviews the permissions and clicks Create.
3. GitHub redirects back to localhost with a code, which the script exchanges
   at `POST /app-manifests/{code}/conversions` for the App id, private key,
   webhook secret and client credentials.
4. The script writes them to the target's secret store (`fly secrets set`,
   `gcloud secrets versions add`, or the local env file) and generates the
   deployment encryption key beside them.
5. It prints what is left, as a checklist `claudebox.sh hosted doctor`
   re-checks against the running deployment: add each profile's provider
   credential to the secret store, write the admin roles (numeric ids, from
   `hosted whois`), run `hosted deploy --admins ADMINS`, enable the device
   flow in the App's settings, log in and apply `claudebox.deploy.yml`,
   create the review team if any repo will use
   `requested` mode, and install the App, including the org's `.github` repo
   if org defaults will be used.

It needs the deployment created by `hosted deploy` (see "Bringing a
deployment up") to write secrets into.

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
| S2 | How does `gh` reach the gateway: `GH_HOST` as an Enterprise host, or an HTTPS proxy? | 2b |
| S3 | Can the gateway inject `CLAUDE_CODE_OAUTH_TOKEN` the way Claude Code sends it? | 2a |
| S4 | Can Fly Machines be confined to the private network for egress? | 4 |
| S5 | Can kindex's Voyage client take a base URL? | 3b |
| S6 | Can baton's Cloud Run provider pin a service to one instance with CPU always allocated, and launch Cloud Run Jobs? If not, the control plane runs on one GCE VM. | 5 |
| S7 | Does the worker's `setpriv` sequence reach `NoNewPrivs: 1` and a zero `CapBnd` on Fly Machines? On Cloud Run Jobs? Answered per target. | 4 (Fly), 5 (GCP) |
| S8 | Can an App manifest enable the device flow, so `app create` need not list it as a manual step? | 2a |

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

**Phase 2a: the trial pipeline.** `hosted deploy --target local`, the control
plane (SQLite, local blob dir, polling, the single-instance lock, durable
capabilities revoked at startup), the supervisor with per-process secrets,
the gateway's provider route as the author of pair outcomes, the local Docker
job runner, a hardened single-uid worker holding a narrowed read token and no
provider credential, the fork rule, the outbox with the scan, the signature
and its terminal states, the per-PR check run, `auto` mode only, `app create
--target local`, the admin roles, `hosted login` with roles and the
role-gated `status` and `config apply`, and `/healthz`. At the end an org can create
an App, install it, and get reviews from a laptop on repos whose contributors
it trusts, and no worker holds the org's provider credential.

**Phase 2b: the state machinery and GitHub reads.** The GitHub read route
(after spike S2), which takes the last real credential out of the worker; the
change gate against SQL state, the commit point and every outcome row,
durable asks and `requested` mode, budgets with repo shares, retention,
erasure and the sweep, the remaining admin commands, the drain, `/metrics`.

**Phase 2c: the worker's only peer.** The worker channel behind the gateway,
the worker's single-use bootstrap token, and egress confinement on local Docker. After it the
gateway is the only thing a worker can reach.

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
  `test-python.sh` does: every decision this plan states (config strictness,
  triggers and asks, each outcome row with the outcome taken from a fake
  gateway record, the outbox's paths, startup revocation, budgets and parks,
  the gateway's allowlists and caps, the scan, erase's preview) gets a case,
  and the channel contract is tested from both ends against one shared
  fixture set. Each phase spec lists its cases.
- **The worker** gets a bash suite in the style of `test-personas.sh`: stub
  `claude`, `gh` and `setpriv`, run it against a fake control plane, and
  assert that a pair run twice resumes its own session, that the entrypoint
  invokes `setpriv` with exactly the hardening arguments, that the job spec
  reaches the worker through the mode-600 file and not the environment, and
  that the wrapper sets exactly `HOME` and a shim-first `PATH`. The suite
  checks the sequence; it runs unprivileged on the host, so it cannot observe
  the resulting posture.
- **The posture itself** (`NoNewPrivs: 1`, a zero `CapBnd`, no inherited root
  environment) is asserted by each target's live smoke test, which runs a
  job whose only pass prints `/proc/self/status` and its environment.
- **Each job runner and each `hosted deploy` target** gets a live smoke test
  run by hand, like `claudebox.sh test` today.

## Decided in the phase specs

Implementation detail that review raised and that changes no decision in
this plan is settled in the spec for the phase that builds it:

- The wording of each check-run state (2a).
- The stage-one lookup cache that keeps a quiet poll cheap, and metering the
  poll against the installation's rate limit (2b).
- Paging thresholds (2b). This plan fixes only that a parked state pages once
  when a pair enters it, never again per push while it stays parked.

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
