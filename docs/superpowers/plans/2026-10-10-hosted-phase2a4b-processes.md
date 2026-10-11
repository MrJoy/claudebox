# Hosted Phase 2a.4b: The Control Plane's Processes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a factory run: the Validator derives a Coder brief (no test code) and a Tester brief (no implementation code) from it, and neither lane opens this file.

**Goal:** The control-plane container that runs 2a.4a's decisions: the scheduler that polls each installation and dispatches jobs, the control-plane process with its lock, startup, loops and `/healthz`, the supervisor that starts the control plane, the gateway and runnerd as three uids with split secrets, and the image and compose file that run them.

**Architecture:** `scheduler.py` is the poll. Per installation and repo it reads the config from the default branch, checks it against the deployment, gives every non-fork PR head a check run, holds what the change gate and the parks hold, and dispatches one job per PR within the deployment's bounds. It also ends jobs and handles loss of access. `control.py` is the control-plane process. It waits for the records store's lock and makes startup true (every credential revoked, every orphan stopped, every transcript deleted, an empty `hello`), then runs the poll, the outbox drain, the gateway's report socket and the worker channel, and drains on `SIGTERM`. `supervisor.py` is PID 1 and splits the platform's secrets by process. `hosted/Dockerfile` and `hosted/compose.json` package it for the local target. Two Phase 1 findings land here too, in `repo_config`, since this is the first plan that serves repo config to authors.

**Tech Stack:** Python 3.9+ standard library (`subprocess`, `signal`, `threading`, `http.server`, `sqlite3`, `unittest`); `cryptography` 38.0.4 (bookworm's `python3-cryptography`) in the image; Docker and Docker Compose for the local target.

**Spec:** `docs/superpowers/specs/2026-10-09-hosted-github-app-design.md`, sections "Triggers" (the reconciliation poll), "Fork PRs", "The per-PR check run", "Parallelism", "Repo and org files", "Deployment config", "Secrets", "Visibility", "State", "Single instance, health, and deploys", and "Bringing a deployment up" (the not-configured state). The 2a decisions this plan binds are in `docs/superpowers/plans/2026-10-10-hosted-phase2a-overview.md`. The ones it needs are copied under Global Constraints.

**Prerequisite:** 2a.1 through 2a.4a (PRs #18 to #21) are built. This plan wires their modules together and changes none of them.

## Global Constraints

- **Standard library only** outside the image, and **Python 3.9 syntax** (overview decision 15). Flat modules, imported by plain name (overview decision 19). The image's Python is bookworm's 3.11.
- **Overview decision 1:** pair state stays in memory in 2a, one `MemoryStateStore` and one `Decider` per repo. A restart re-reviews every open PR once per persona, and 2a.4b's startup deletes every transcript blob, since nothing points at one any more.
- **Overview decision 16:** the control plane owns the rewrite test, through `Installation.is_ancestor`.
- The spec's lock: "Until it holds the lock, an instance answers `/healthz` with 200 and 'waiting for lock' ... and dispatches nothing". "On taking the lock, the new instance revokes every outstanding capability and token and stops orphaned workers".
- The spec's drain: "On `SIGTERM` the supervisor tells the control plane to stop dispatching, keeps both processes serving while running jobs finish, up to a drain deadline, then revokes what is left and exits both. The deadline is the platform's grace period: up to 10 minutes locally". "Revoked pairs stay owed and run on the new instance."
- The spec's secrets: "the control plane gets the App key, the webhook secret and the deployment key; the gateway gets the provider and Voyage credentials". "The scanner, which needs every literal, runs in the control plane and is handed the provider literals by the supervisor at startup."
- The spec's not-configured state: "A control plane with no App credentials or deployment file yet starts in a 'not configured' state: `/healthz` answers and nothing is dispatched." The deployment file "has one copy, in the records store, and changes only through `config apply`", and a `config apply` "takes effect without a restart".
- The spec's repo files: read "from the **default branch only**", org defaults "in the org's `.github` repository", and "an unreadable org `.github` repo" is "no org defaults".
- The spec's fork rule: "The test is the head repository alone", and a fork PR gets no review.
- The spec's visibility: a repo going public, an uninstall or removal from the installation "revokes every live capability and token for that repo, and deletes, before any further pass, every content-bearing record keyed to that repo", and "the repo's asks and owed pairs are dropped too".
- The spec's job deadline: "55 minutes", after which "running pairs `revoked`, stay owed".
- Every existing suite (`test-python.sh`, `test-providers.sh`, `test-personas.sh`, `test-shim.sh`, `test-launcher.sh`, `test-worker.sh`) still passes, and local mode's prompts are byte-identical (`tests/fixtures/`).

## Decided here

The spec and the overview leave these open, and each one changes code. They are this plan's answers, for the founder to ratify in Phase 1 of the factory run:

1. **2a reviews in `auto` mode only.** A repo whose config asks for `trigger: requested` is shown as a config problem on its check runs ("arrives in phase 2b") rather than reviewed against its wishes.
2. **The org file is `.github/claudebox.json` in `<owner>/.github`**, read at the default branch. A read that fails is "no org defaults". The org `.github` repo is never itself reviewed.
3. **A poll per installation lists its repos.** A repo that is public is passed over, and one that turned public since the last poll is lost. A repo gone from the list is lost. An installation gone from `App.installations()`, or one carrying `suspended_at`, loses all its repos, and the control plane logs one `PAGE: installation N was uninstalled or suspended; M repo(s) stop being reviewed` for it: the spec pages this, and with no webhook receiver in 2a the poll is its only producer. Losing a repo ends its jobs (their started pairs `revoked`), calls 2a.4a's three deleters (`Outbox.drop_repo`, `Committer.forget_repo`, `CheckRuns.forget_repo`), and forgets its fingerprint cache. The jobs end with no scheduler lock held, since ending one calls into the committer, which calls `decider_for`, which takes that lock. After each poll, a sweep drops the outbox and withheld rows of any repo that no live installation's listing returned as private (one lost or made public while the control plane was down, which nothing in memory names). It keys on the listings, never on how far the poll's walk got before a drain stopped it, and runs only when every live installation's listing succeeded. A loss forgets the repo's last published config answer too, so a repo that comes back publishes its problem again. A completion in flight when its job ends commits nothing (2a.4a's `job_live`, wired to the job registry). Its rounds survive, as the spec's "Visibility" says ("Rounds and asks survive a public flip, since they hold no content"). A returning pair starts a fresh session at its next round, as one does after `MAX_PASSES_PER_SESSION` rotation. Its earlier rounds' findings are still on the PR, signed, and with no committed fingerprint its round line reads the local wording, which works from those comments.
4. **Every non-fork candidate head gets a check run**, created once, whether or not anything is dispatched for it. The config follows the spec's fallback, which `repo_config.resolve` implements: an invalid repo file falls back to the org defaults, an invalid org file under a valid repo file is dropped, and only both invalid stops the review. The dropped file's problems show on each head's run as a notice beside a review that runs. A problem (both files invalid, `trigger: requested`, or a setting the deployment cannot serve) shows on every head's run and dispatches nothing. A read of either file that fails is not a statement about the file: the repo is skipped for that poll and nothing is published. A head is published when its (problem, notice) differs from the one last set on it, a head with none included, so a new head shows a held problem, a fixed one clears without a push, and an unchanged answer costs no GitHub call. A pair at its round cap is painted `round-cap-unread` once on a head no job will run at, or a capped plan PR's new head would read "Queued" for good. It is never painted `round-cap`, which counts as clean: a green check on a commit no persona read would claim a review that did not happen.
5. **The change gate is local mode's**, `signals.Tracker`, one per repo since a Tracker keys by PR number. A PR's fingerprint is its head, its mode and the newest unsigned comment, and comments, reviews and the 30 newest inline comments are read only when the PR's `updated_at` has moved. A failed lookup is cached against that `updated_at` too, so an outage costs one lookup per change, and the PR gates on `updated_at` through `signals.degraded`. A PR younger than the deployment's `settle_seconds` waits.
6. **Dispatch bounds.** A PR with a live job is not dispatched again. A pair parked by the commit point waits while its siblings run, and a parked profile dispatches nothing. `max_concurrent_jobs` counts jobs, and a profile's `concurrency` counts pairs in flight (dispatched and not completed) across its jobs. A group larger than the room left dispatches in parts, phase order first: the pairs left out hold no session, so the gate runs them on a later poll once the job has ended, and a 7-persona plan group under a bound of 4 is never starved. A poll stops dispatching at the first refusal by `max_concurrent_jobs` and keeps walking: every later head and repo still gets its check run and its config answer (decision 4), and only the dispatch stages, which cost lookups, are skipped. The poll logs when the cap starts binding for an installation and when it stops, not on every poll.
7. **Visibility is checked again at dispatch**, uncached, and a check that errors dispatches nothing.
8. **A job's material is built once, at dispatch.** The prompts are `build_hosted`'s, with `WORKTREE_STANZA` exactly when `shared_worktree` is true, and `shared_worktree` is true when the job has more than one pair, whatever their phases or the cap: a phase-1 pass that writes to the clone is read by phase 2, and one-at-a-time passes read each other's writes the same way (PR #20 review). A pair with a session gets the followup prompt. A pair's round line names the head of its committed fingerprint (`Committer.last_reviewed_head`). When that head is no longer an ancestor of the new one, the round line is round 1's and the check run says the branch was rewritten. An ancestry check that errors counts as a rewrite, since a whole review beats a guess.
9. **The reap ends a job** when runnerd lists its worker as gone or exited, when its 55-minute deadline has passed, or when its bootstrap token expired unused. Ending a job revokes its credentials, tells the gateway to revoke its unfinished pairs' tokens, marks each started pair `revoked` (`Committer.revoke`, and `checkruns.REVOKED` on the run), owes each unstarted pair, stops its worker, forgets its outbox keys and `settled` entries, and writes `ended_at` and `end_reason` on its row.
10. **The deployment config is the records store's `deploy_config` row**, re-read on each poll when its `applied_at` moves. A stored config that does not load (one written by another version) keeps the last good one and warns. The outbox's two caps follow it.
11. **The control plane's startup.** A not-configured instance touches nothing on the volume. Otherwise it takes the lock on `<data>/control.lock` (overview decision 13) before it opens the records store, answering `/healthz` "waiting for lock" meanwhile. On taking it: `Creds.revoke_all`, every `jobs` row with no `ended_at` closed as "the control plane restarted" (this instance's registry never holds those jobs, so nothing else would end them), runnerd's `stop_all` (a failure warns, since every credential is already revoked, and that includes a runnerd not yet listening, which raises `OSError` rather than `RunnerError`), every transcript blob deleted, and an empty `hello` (a gateway not yet answering gets its hello on its first reply). The gateway's reports are refused until the lock is held, and its Reporter retries them.
12. **The gateway link opens one connection per call** and waits up to 60 seconds, since a flush may be held for 50. A reply of "hello first" is answered with a `hello` and the call is sent again once. A `hello` carries every live provider token whose job is in memory, with that job's model and route and the token's committed counts. A live token with no job is revoked as the `hello` is built.
13. **`SIGTERM` drains.** The control plane halts the scheduler's walk and waits for one already in progress to finish, so every job it dispatched is counted, then reaps until no job is left or `CLAUDEBOX_DRAIN_SECONDS` (default 600) has passed, and ends what is left as "the control plane is stopping". The supervisor gives it the deadline plus 15 seconds, then stops the gateway and runnerd and kills whatever is left. Reports a gateway sends after the control plane has gone concern revoked tokens only, and 2a has no budgets, so none of them is needed.
14. **`/healthz` is its own server on port 8081.** It answers 200 with the state (`waiting for lock`, `not configured`, `serving` or `draining`) and, for `not configured`, its cause and the command that clears it (no App credentials, or no deployment config in the records store yet), and 503 when the poll or drain loop has died. The log names the cause once, on the poll it changes.
15. **The supervisor's secrets split.** The control plane's are a closed list (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`, `CLAUDEBOX_DEPLOYMENT_KEY`). A provider credential is a variable `CLAUDEBOX_CREDENTIALS` names (comma-separated), and nothing else. The deployment file names them too, but it lives in the records store, which only the control plane can read, so the operator's setup file repeats the list for the supervisor; 2a.5's `hosted deploy` writes it from the deployment file. A list naming a control-plane secret, a supervisor setting, a process variable or a reserved name stops the supervisor at startup, an unset list warns that the gateway holds no credentials, and a named credential the platform lacks is warned about. The gateway gets the provider credentials, the control plane gets each as a candidate `CLAUDEBOX_SCAN_<NAME>`, and runnerd gets none. The scanner uses only the candidates the applied deployment names (its profiles' credentials and `VOYAGE_API_KEY`), rebuilt when the deployment changes, so a platform's own variables (`FLY_REGION`, `K_SERVICE`) are never treated as secrets. A credential the deployment names that the platform lacks is warned about. The secrets between processes (`GATEWAY_SECRET`, `RUNNER_SECRET`) are random per container start. They, the socket paths, the gateway's listener and `CLAUDEBOX_DATA` are reserved: never a credential, and never taken from the platform. The control plane's own settings (`PERSONA_DIR`, `CHANNEL_LISTEN`, `HEALTH_LISTEN`) are forwarded to it alone. Nothing else from the platform environment crosses. A malformed App key or deployment key is named in the log, and the instance is not configured.
16. **The supervisor drops to uid 9004, keeping only `CAP_KILL`**, as the spec says ("then drops to an unprivileged uid itself"). It starts as root with `SETUID`, `SETGID` and `KILL`, starts its three children, then clears its groups, sets `PR_SET_KEEPCAPS`, changes gid and uid, and narrows its permitted and effective sets to `KILL` with `capset`. `CAP_KILL` is what lets it signal the children under their own uids during the drain. It can never become root again, and as uid 9004 in no group it can open neither the process sockets nor the Docker socket that shares its container (a root PID 1 could, as the owner of a `root:docker` socket).
17. **One process exiting stops the container, and the restarts are bounded.** The supervisor logs a `PAGE:` line naming the process, stops the other two in the drain's order and exits 1. Every restart re-reviews every open PR (overview decision 1), so a child that dies on every start would re-review the whole installation each time Docker brought it back. `compose.json` therefore restarts on failure at most three times, and then the container stays down for a person. The cost is that a host reboot does not bring the control plane back by itself: `hosted deploy` (2a.5) starts it.
18. **The image is `debian:bookworm-slim`** with `python3` and `python3-cryptography`, and the Docker CLI copied from `docker:27.3.1-cli`. Its code is root-owned. The three users and the shared group have pinned ids (9001 to 9003, and 9000), because `/data` is a volume that keeps its owner across rebuilds. `/data` is `control`'s, mode 0700. Each socket has its own directory, `/run/claudebox/<user>`, owned by the process that binds it, in the shared group, mode 2750: a peer can connect, and can neither unlink the socket nor bind its own in its place. The build runs `test_hosted_crypto.py` with no pipe between the run and the layer's status, so a failing test fails the build and prints its output, and a skip fails it too.
19. **`hosted/compose.json`** runs the container with `cap_drop: ALL` and the supervisor's three capabilities, `no-new-privileges`, a named volume at `/data`, the Docker socket for runnerd, `restart: on-failure:3` (decision 17), a `claudebox` network the workers join, an 11-minute stop grace period (the drain's 600 seconds plus both 15-second graces fit inside it), and only `/healthz` published, on loopback. Compose discovers no JSON filename, so every command names the file: the worker image is built by `docker compose -f hosted/compose.json --profile build build`, and 2a.5's `up`, `down` and `logs` take the same `-f`. `context` and `env_file` resolve against the compose file's own directory.
20. **The App key may arrive on one line**, with its newlines written as the two characters `\n`, because `docker run --env-file` holds one line per variable. Only that pair is translated, and only in a value with no real newline.
21. **Phase 1 finding F5:** `plan.label` refuses any whitespace inside the label other than a plain space, in the loader and in the schema's pattern. **F6:** an empty persona selector in a repo file is reported against its key (`code.personas` or `plan.personas`), never as an environment variable to unset.
22. **The `rounds.cap` default stays 4.** The founder kept 4 for Phase 1 and recorded an objection. This plan builds a `Decider` per repo from the repo's `rounds.cap`, so the default becomes the hosted default here. The founder rules on it in this run's Phase 1.

## Review Focus

- **Two instances overlapping during a deploy.** Expected: the second answers `/healthz` "waiting for lock", dispatches nothing, refuses reports, and takes over when the first stops. Pinned in Task 3.
- **A repo flipping public between polls, or between a poll and a dispatch.** Expected: its jobs end, its findings and records go, nothing dispatches. Pinned in Task 2.
- **A gateway that restarted alone.** Expected: the next call gets "hello first", the control plane sends a `hello` with the live tokens, and the call goes through. Pinned in Task 3.
- **A worker that never fetches its job.** Expected: the job ends when its bootstrap token expires, and its pairs stay owed. Pinned in Task 2.
- **The gateway reading the App key.** Expected: impossible, since the gateway's environment never holds it. Pinned in Task 4.
- **A secrets file committed by accident.** Expected: `hosted/claudebox.env` is in both `.gitignore` and `.dockerignore`. Pinned in Task 5.
- **A stored deployment config that will not load.** Expected: the last good one keeps serving, with a warning. Pinned in Task 3.

## Not in this plan

- **Checking the config file a PR proposes, and printing the deployment's catalog on the check run** (the spec's "Discovery, for authors who are not operators"). The spec's Phase 2a list does not name them, so they are deferred to 2b, which needs a ref-addressed content read 2a.1's client does not have.

## Not run here

- **A real GitHub App and a real job.** The tests use fakes. Task 5's smoke run signs a real JWT against GitHub with a throwaway key and expects GitHub to refuse it. 2a.5 runs the first job end to end.

---

### Task 1: Repo config, Phase 1 findings F5 and F6

**Files:**
- Modify: `reviewer/repo_config.py` (`_leaf`'s `plan.label` rule, `_personas`)
- Modify: `tools/gen-config-schema.py` (the `plan.label` pattern)
- Modify: `schema/claudebox.schema.json` (regenerated, never edited by hand)
- Test: `tests/test_repo_config.py`, `tests/test_config_schema.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `repo_config.check` refuses a `plan.label` holding any whitespace but a plain space, and reports an empty `code.personas` or `plan.personas` against its key.
  - The schema's `plan.label` pattern `^\S(?:(?:\S| )*\S)?$`, which agrees with the loader on every case `test_the_string_patterns_agree_with_the_loader` tries.

- [ ] **Step 1: Write the failing tests**

Apply this change to `tests/test_repo_config.py` and `tests/test_config_schema.py`:

```diff
diff --git a/tests/test_config_schema.py b/tests/test_config_schema.py
index 6a4cf72..ec4a330 100644
--- a/tests/test_config_schema.py
+++ b/tests/test_config_schema.py
@@ -74,7 +74,8 @@ class SchemaTest(unittest.TestCase):
         props = self.schema["properties"]
         cases = [
             (("plan", "label"), props["plan"]["properties"]["label"]["pattern"],
-             ("plan", "needs plan", " plan", "plan ", " ", "", "plan\n")),
+             ("plan", "needs plan", " plan", "plan ", " ", "", "plan\n", "a\nb", "a\tb",
+              "a\rb", "a\u00a0b", "a b c")),
             (("team",), props["team"]["pattern"], ("rev", "a/b", "", "rev\n", "r v")),
             (("profile",), props["profile"]["pattern"], ("ollama", "", "ollama\n")),
             (("model",), props["model"]["pattern"], ("glm-5.2:cloud", "a b", "", "glm\n")),
diff --git a/tests/test_repo_config.py b/tests/test_repo_config.py
index ef987b2..a1c2196 100644
--- a/tests/test_repo_config.py
+++ b/tests/test_repo_config.py
@@ -121,6 +121,26 @@ class CheckTest(unittest.TestCase):
         _, problems = check({"plan": {"label": " "}})
         self.assertTrue(problems)
 
+    def test_a_plan_label_may_hold_spaces_but_no_other_whitespace(self):
+        # Phase 1 finding F5: "a\nb" matches no GitHub label, so every plan PR
+        # would route to code mode.
+        self.assertFalse(check({"plan": {"label": "needs review"}})[1])
+        for label in ("a\nb", "a\tb", "a\rb", "a\u00a0b"):
+            with self.subTest(label=label):
+                _, problems = check({"plan": {"label": label}})
+                self.assertIn("no whitespace inside it but plain spaces", problems.lines()[0])
+
+    def test_an_empty_persona_selector_names_the_repo_key_not_an_environment_variable(self):
+        # Phase 1 finding F6: a hosted repo author cannot unset PERSONAS.
+        for mode in ("code", "plan"):
+            for value in ("", " ", ",", " , "):
+                with self.subTest(mode=mode, value=value):
+                    _, problems = check({mode: {"personas": value}})
+                    line = problems.lines()[0]
+                    self.assertTrue(line.startswith(f"{mode}.personas: names no persona"), line)
+                    self.assertNotIn("PERSONAS", line)
+                    self.assertNotIn("unset", line)
+
     def test_a_section_that_is_not_an_object_is_an_error(self):
         # Oracle: tester-brief.md, Task 2, Interfaces and signed starting oracle line 323.
         # Mutation: Ignore a non-object plan section.
```

- [ ] **Step 2: Run them to see them fail**

Run: `./test-python.sh -p 'test_repo_config.py'` and `./test-python.sh -p 'test_config_schema.py'`
Expected: FAIL: `a\nb` is accepted, the empty-selector line names `PERSONAS`, and the schema's pattern disagrees with the loader on `a\u00a0b`.

- [ ] **Step 3: Implement**

Apply this change to `reviewer/repo_config.py` and `tools/gen-config-schema.py`:

```diff
diff --git a/reviewer/repo_config.py b/reviewer/repo_config.py
index 81cfb63..f924676 100644
--- a/reviewer/repo_config.py
+++ b/reviewer/repo_config.py
@@ -71,6 +71,13 @@ def _personas(problems, path, value, mode, persona_dir):
     if not is_str(value):
         problems.add(path, "must be a persona selector string, e.g. \"red_team,sage\" or \"all\"")
         return False
+    if not value.replace(",", "").strip():
+        # personas.resolve's own wording for this names an environment variable
+        # a hosted repo author cannot set (Phase 1 finding F6), so the repo
+        # file's key gets its own.
+        problems.add(path, f"names no persona; remove the key for the default set "
+                           f"({personas_mod.DEFAULTS[mode]}), or name one or more")
+        return False
     try:
         personas_mod.resolve(mode, persona_dir, {personas_mod.SELECTOR_VAR[mode]: value})
     except ConfigError as exc:
@@ -92,9 +99,14 @@ def _leaf(problems, path, value, persona_dir) -> bool:
         problems.add(path, "must be a model name with no spaces")
         return False
     if path == "plan.label":
-        if is_str(value) and value.strip() and value == value.strip():
+        # Inside the label only a plain space is allowed: a newline or a tab
+        # matches no GitHub label, so every plan PR would route to code mode
+        # (Phase 1 finding F5). The schema's pattern says the same.
+        if is_str(value) and value.strip() and value == value.strip() and \
+                not any(c.isspace() and c != " " for c in value):
             return True
-        problems.add(path, "must be a non-empty label with no surrounding spaces")
+        problems.add(path, "must be a non-empty label with no surrounding spaces, and no "
+                           "whitespace inside it but plain spaces")
         return False
     if path == "plan.personas":
         return _personas(problems, path, value, "plan", persona_dir)
diff --git a/tools/gen-config-schema.py b/tools/gen-config-schema.py
index dc202c9..98742d1 100755
--- a/tools/gen-config-schema.py
+++ b/tools/gen-config-schema.py
@@ -50,7 +50,7 @@ def build(persona_dir: str) -> dict:
         "model": {"type": "string", "pattern": "^\\S+$",
                   "description": "A model the profile allows; omit for its default."},
         "plan": _obj({
-            "label": {"type": "string", "pattern": "^\\S(?:.*\\S)?$",
+            "label": {"type": "string", "pattern": "^\\S(?:(?:\\S| )*\\S)?$",
                       "description": "The label that makes a PR a plan review."},
             "personas": _personas("plan", plan),
         }),
```

Then regenerate the schema: `python3 tools/gen-config-schema.py`. Its diff is exactly:

```diff
diff --git a/schema/claudebox.schema.json b/schema/claudebox.schema.json
index 4263c94..9d68635 100644
--- a/schema/claudebox.schema.json
+++ b/schema/claudebox.schema.json
@@ -40,7 +40,7 @@
       "properties": {
         "label": {
           "type": "string",
-          "pattern": "^\\S(?:.*\\S)?$",
+          "pattern": "^\\S(?:(?:\\S| )*\\S)?$",
           "description": "The label that makes a PR a plan review."
         },
         "personas": {
```

- [ ] **Step 4: Run them to see them pass, then the whole suite**

Run: `./test-python.sh -p 'test_repo_config.py'`, `./test-python.sh -p 'test_config_schema.py'`, then `./test-python.sh`
Expected: PASS, 48 and 8 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add reviewer/repo_config.py tools/gen-config-schema.py schema/claudebox.schema.json tests/test_repo_config.py tests/test_config_schema.py
git commit -m "repo config: plan.label whitespace and the empty selector's wording (F5, F6)"
```

### Task 2: The scheduler

**Files:**
- Create: `hosted/scheduler.py`
- Test: `tests/test_hosted_scheduler.py`

**Interfaces:**
- Consumes: `repo_config.resolve`, `deploy_config.against_deploy`, `personas.resolve`, `signals`, `prompts.build_hosted`, `decisions.Decider`, `state.MemoryStateStore` (`reviewer/`); `creds.Creds` and `BOOTSTRAP_TTL`, `github_app.GitHubError`, `is_fork_pr` and `is_public` (2a.1); `runner.RunnerError` (2a.3); `channel.Jobs`, `JobMaterial` and `PairMaterial`, `checkruns`, `outbox.Outbox`, `commit.Committer` (2a.4a).
- Produces:
  - `ORG_REPO = ".github"`; `shared_worktree(n_pairs: int) -> bool`.
  - `Scheduler(records, creds, jobs, committer, check_runs, outbox, runner, installation_for, deploy_for, gateway_call, persona_dir, gateway_url, channel_url, clock=time.time, log=print, new_job_id=...)` with `poll(installation_id) -> int`, `poll_repo(inst, installation_id, info, deploy) -> int`, `load_config(inst, full_name, deploy) -> (config, model, problem, notice)` (a failed read raises `GitHubError`), `dispatch(...) -> bool`, `reap() -> int`, `end_job(job, reason)`, `lose_repo(repo, reason)`, `sweep_orphans(live) -> int` and `decider_for(repo) -> Decider`, and the attributes `installation_of` (repo to installation id), `listings` (installation id to the private repos its last successful listing returned), `states`, `runtimes`, `trackers`, `full_installations` (the installations whose last poll met `max_concurrent_jobs`), `halted` (an event the drain sets) and `walk` (a lock the poll's walk holds). `committer` may be set after construction, since the `Committer` takes `decider_for`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_scheduler.py`:

```python
import _path  # noqa: F401

import json
import os
import shutil
import tempfile
import unittest

import checkruns
import creds
import deploy_config
from channel import Jobs
from checkruns import CheckRuns
from commit import Committer
from common import Pair
from github_app import GitHubError
from outbox import Outbox
from records import Records
from runner import RunnerError
from scan import Scanner
from scheduler import Scheduler, shared_worktree

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PERSONAS = os.path.join(ROOT, "personas")
HEAD = "a" * 40
NEW = "b" * 40
T = 1760000000.0


def deploy(**over):
    doc = {"profiles": {"ollama": {"provider": "ollama", "models": ["glm-5.2:cloud"],
                                   "default_model": "glm-5.2:cloud", "credential": "OLLAMA_API_KEY",
                                   "concurrency": 8,
                                   "budget": {"unit": "tokens", "daily": 40000000}}},
           "max_concurrent_jobs": 4, "timing": {"settle_seconds": 30}}
    doc.update(over)
    return deploy_config.load_deploy(json.dumps(doc).encode())


def pr(n=7, head=HEAD, labels=(), updated="2025-10-09T08:00:00Z", fork=False):
    base = {"full_name": "acme/web"}
    return {"number": n, "head": {"sha": head, "repo": {"full_name": "evil/web" if fork else "acme/web"}},
            "base": {"repo": base}, "labels": [{"name": l} for l in labels], "updated_at": updated}


class FakeInst:
    def __init__(self):
        self.repo_list = [{"full_name": "acme/web", "name": "web", "id": 42, "private": True}]
        self.files = {("acme/web", ".github/claudebox.json"): json.dumps(
            {"profile": "ollama", "code": {"personas": "red_team,sage"}}).encode()}
        self.prs = [pr()]
        self.comments = []
        self.ancestor = True
        self.created, self.updated = [], []

    def repos(self):
        return self.repo_list

    def file_at_default(self, repo, path):
        return self.files.get((repo, path))

    def open_prs(self, repo):
        return self.prs

    def issue_comments(self, repo, n):
        return self.comments

    def reviews(self, repo, n):
        return []

    def newest_review_comments(self, repo, n):
        return []

    def repo(self, name):
        return next((r for r in self.repo_list if r["full_name"] == name), None)

    def is_ancestor(self, repo, a, b):
        if isinstance(self.ancestor, Exception):
            raise self.ancestor
        return self.ancestor

    def create_check_run(self, repo, head, status, title, summary, conclusion=None):
        self.created.append(head)
        return len(self.created)

    def update_check_run(self, repo, run_id, status, title, summary, conclusion=None):
        self.updated.append((run_id, status, title, summary, conclusion))


class FakeRunner:
    def __init__(self):
        self.started, self.stopped = [], []
        self.states = {}
        self.refuse = False

    def start(self, job_id, boot, channel_url, gateway_url):
        if self.refuse:
            raise RunnerError("no such image")
        self.started.append((job_id, boot))
        self.states[job_id] = "running"

    def list(self):
        return [{"job_id": j, "state": s} for j, s in self.states.items()]

    def stop(self, job_id):
        self.stopped.append(job_id)
        self.states.pop(job_id, None)


class FakeBlobs(dict):
    def put(self, name, data):
        self[name] = data

    def delete(self, name):
        return self.pop(name, None) is not None


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.records = Records(os.path.join(d, "r.db"))
        self.addCleanup(self.records.close)
        self.now = [T]
        self.inst = FakeInst()
        self.runner = FakeRunner()
        self.jobs = Jobs()
        self.creds = creds.Creds(self.records, clock=lambda: self.now[0])
        self.runs = CheckRuns(self.records, lambda repo: self.inst, clock=lambda: self.now[0])
        self.box = Outbox(self.records, Scanner({}), self.runs, 60000, 25)
        self.logs = []
        self.deploy = deploy()
        self.gw = []
        ids = iter(f"job{i:02d}" for i in range(100))
        self.sched = Scheduler(
            self.records, self.creds, self.jobs, None, self.runs, self.box, self.runner,
            lambda inst_id: self.inst, lambda: self.deploy,
            lambda op, **f: self.gw.append((op, f)) or {"ok": True}, PERSONAS,
            "http://gateway:8090", "http://control:8080", clock=lambda: self.now[0],
            log=self.logs.append, new_job_id=lambda: next(ids))
        self.committer = Committer(self.records, FakeBlobs(), self.sched.decider_for,
                                   lambda p: 1800, clock=lambda: self.now[0], log=self.logs.append)
        self.sched.committer = self.committer

    def poll(self):
        return self.sched.poll(7)


class DispatchTest(Base):
    def test_a_pr_is_dispatched_as_one_job_with_each_persona(self):
        self.assertEqual(self.poll(), 1)
        job = self.jobs.all()[0]
        self.assertEqual((job.repo, job.pr, job.head, job.mode, job.model, job.repo_id),
                         ("acme/web", 7, HEAD, "code", "glm-5.2:cloud", 42))
        self.assertEqual(sorted(p.persona for p in job.pairs), ["red_team", "sage"])
        self.assertEqual(job.auth_env, "ANTHROPIC_AUTH_TOKEN")
        self.assertEqual(job.route["credential"], "OLLAMA_API_KEY")
        job_id, boot = self.runner.started[0]
        self.assertEqual(self.creds.check(boot, "bootstrap").job_id, job_id)
        self.assertEqual(self.records.conn().execute("SELECT repo, pr FROM jobs").fetchone()[:],
                         ("acme/web", 7))

    def test_the_prompts_are_the_hosted_defaults_and_the_round_line_rides_the_system_prompt(self):
        self.poll()
        sage = next(p for p in self.jobs.all()[0].pairs if p.persona == "sage")
        self.assertIn("If the deployment refuses a read", sage.prompt)
        self.assertIn("#7", sage.prompt)
        self.assertIn("round 1 of your review", sage.system_prompt)
        self.assertEqual(sage.phase, 2)

    def test_red_team_then_sage_share_the_worktree_though_their_phases_differ(self):
        # PR #20 review: Sage's phase-2 pass reads whatever red_team left in
        # the clone, so one persona per phase is still a shared clone.
        self.poll()
        job = self.jobs.all()[0]
        self.assertTrue(job.shared_worktree)
        import prompts
        self.assertTrue(all(p.prompt.endswith(prompts.WORKTREE_STANZA) for p in job.pairs))

    def test_a_lone_persona_has_the_worktree_to_itself(self):
        self.inst.files[("acme/web", ".github/claudebox.json")] = json.dumps(
            {"profile": "ollama", "code": {"personas": "sage"}}).encode()
        self.poll()
        job = self.jobs.all()[0]
        self.assertFalse(job.shared_worktree)
        import prompts
        self.assertNotIn(prompts.WORKTREE_STANZA, job.pairs[0].prompt)

    def test_two_phase_one_personas_share_the_worktree_and_say_so(self):
        self.inst.files[("acme/web", ".github/claudebox.json")] = json.dumps(
            {"profile": "ollama", "code": {"personas": "red_team,sme"}}).encode()
        self.poll()
        job = self.jobs.all()[0]
        self.assertTrue(job.shared_worktree)
        import prompts
        self.assertTrue(all(p.prompt.endswith(prompts.WORKTREE_STANZA) for p in job.pairs))

    def test_a_check_run_is_created_once_and_shows_the_pairs_queued(self):
        self.poll()
        self.poll()
        self.assertEqual(self.inst.created, [HEAD])
        self.assertEqual(self.inst.updated[-1][1], "queued")

    def test_a_fork_pr_gets_neither_a_review_nor_a_check_run(self):
        self.inst.prs = [pr(fork=True)]
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.inst.created, [])

    def test_a_plan_label_routes_to_plan_mode(self):
        self.inst.prs = [pr(labels=("plan",))]
        self.poll()
        self.assertEqual(self.jobs.all()[0].mode, "plan")

    def test_a_pr_still_settling_waits(self):
        self.inst.prs = [pr(updated="2025-10-09T08:53:10Z")]   # ten seconds before T
        self.assertEqual(self.poll(), 0)
        self.now[0] += 30
        self.assertEqual(self.poll(), 1)

    def test_a_pr_with_a_live_job_is_not_dispatched_again(self):
        self.poll()
        self.assertEqual(self.poll(), 0)
        self.assertEqual(len(self.runner.started), 1)


class GateTest(Base):
    def finish(self, job, ok=True):
        """Commit every pair of a job as ok, the way complete-pair would."""
        for p in job.pairs:
            pair = Pair(job.pr, job.mode, p.persona)
            decider = self.sched.decider_for("acme/web")
            decider.state.sessions[pair] = "sess-" + p.persona
            decider.record_success(pair, type("R", (), {"session_id": "sess-" + p.persona})(), p.signal)
            job.completed[p.persona] = "ok"
        self.sched.end_job(job, "test")

    def test_a_group_larger_than_the_profiles_concurrency_runs_in_parts(self):
        # PR #22 review: the whole group was compared with the bound, so a
        # group larger than it never dispatched at all.
        d = json.loads(json.dumps({"profiles": {"ollama": {
            "provider": "ollama", "models": ["glm-5.2:cloud"], "default_model": "glm-5.2:cloud",
            "credential": "OLLAMA_API_KEY", "concurrency": 1,
            "budget": {"unit": "tokens", "daily": 1}}}}))
        self.deploy = deploy_config.load_deploy(json.dumps(d).encode())
        self.assertEqual(self.poll(), 1)
        job = self.jobs.all()[0]
        self.assertEqual([p.persona for p in job.pairs], ["red_team"])   # Sage is phase 2
        self.finish(job)
        self.assertEqual(self.poll(), 1)
        job = self.jobs.all()[0]
        self.assertEqual([p.persona for p in job.pairs], ["sage"])
        self.finish(job)
        self.assertEqual(self.poll(), 0)

    def test_an_unchanged_pr_is_not_reviewed_again(self):
        self.poll()
        self.finish(self.jobs.all()[0])
        self.assertEqual(self.poll(), 0)

    def test_a_push_reviews_again_naming_the_head_last_reviewed(self):
        self.poll()
        self.finish(self.jobs.all()[0])
        self.inst.prs = [pr(head=NEW, updated="2025-10-09T08:10:00Z")]
        self.assertEqual(self.poll(), 1)
        sage = next(p for p in self.jobs.all()[0].pairs if p.persona == "sage")
        self.assertIn(f"`git log --oneline {HEAD}..HEAD`", sage.system_prompt)
        self.assertEqual(sage.session_id, "sess-sage")
        self.assertNotIn("Review the whole change", sage.system_prompt)

    def test_a_rewritten_branch_is_reviewed_whole_and_the_check_run_says_so(self):
        self.poll()
        self.finish(self.jobs.all()[0])
        self.inst.prs = [pr(head=NEW, updated="2025-10-09T08:10:00Z")]
        self.inst.ancestor = False
        self.poll()
        sage = next(p for p in self.jobs.all()[0].pairs if p.persona == "sage")
        self.assertIn("round 1 of your review", sage.system_prompt)
        self.assertIn("The branch was rewritten", self.inst.updated[-1][3])

    def test_an_ancestry_check_that_fails_widens_rather_than_guesses(self):
        self.poll()
        self.finish(self.jobs.all()[0])
        self.inst.prs = [pr(head=NEW, updated="2025-10-09T08:10:00Z")]
        self.inst.ancestor = GitHubError(502, "x")
        self.poll()
        self.assertIn("round 1", self.jobs.all()[0].pairs[0].system_prompt)

    def test_a_human_comment_reviews_again(self):
        self.poll()
        self.finish(self.jobs.all()[0])
        self.inst.comments = [{"created_at": "2025-10-09T08:20:00Z", "body": "why?"}]
        self.inst.prs = [pr(updated="2025-10-09T08:20:00Z")]
        self.assertEqual(self.poll(), 1)

    def test_a_failed_comment_lookup_is_not_repeated_until_updated_at_moves(self):
        # PR #22 review: local mode's Tracker caches the failure too.
        calls = []

        def failing(repo, n):
            calls.append(n)
            raise GitHubError(502, "x")
        self.inst.issue_comments = failing
        self.poll()
        self.sched.end_job(self.jobs.all()[0], "test")
        self.poll()
        self.assertEqual(len(calls), 1)
        self.inst.prs = [pr(updated="2025-10-09T08:30:00Z")]
        self.sched.end_job(self.jobs.all()[0], "test") if self.jobs.all() else None
        self.poll()
        self.assertEqual(len(calls), 2)

    def test_claudebox_own_comment_does_not(self):
        self.poll()
        self.finish(self.jobs.all()[0])
        self.inst.comments = [{"created_at": "2025-10-09T08:20:00Z", "body": "x -claudebox (Sage)"}]
        self.inst.prs = [pr(updated="2025-10-09T08:20:00Z")]
        self.assertEqual(self.poll(), 0)

    def test_a_parked_pair_waits_and_its_siblings_run(self):
        self.poll()
        job = self.jobs.all()[0]
        self.sched.end_job(job, "test")   # both pairs owed now
        from commit import Park
        self.committer.parks[("acme/web", Pair(7, "code", "sage"))] = Park("capped", head=HEAD)
        self.poll()
        self.assertEqual([p.persona for p in self.jobs.all()[0].pairs], ["red_team"])

    def test_a_capped_plan_prs_new_head_shows_round_cap_not_queued(self):
        # PR #21 review: nothing dispatches at it, so nothing else would paint it.
        self.inst.prs = [pr(labels=("plan",))]
        self.inst.files[("acme/web", ".github/claudebox.json")] = json.dumps(
            {"profile": "ollama", "plan": {"personas": "sage"}, "rounds": {"cap": 1}}).encode()
        self.poll()
        self.finish(self.jobs.all()[0])
        self.inst.prs = [pr(head=NEW, labels=("plan",), updated="2025-10-09T08:10:00Z")]
        self.assertEqual(self.poll(), 0)
        _, status, title, _, conclusion = self.inst.updated[-1]
        # Never green: no pass read this commit (PR #22 review).
        self.assertEqual((status, title, conclusion),
                         ("completed", "Not reviewed: round cap reached", "neutral"))
        before = len(self.inst.updated)
        self.poll()
        self.assertEqual(len(self.inst.updated), before)   # painted once

    def test_a_parked_profile_dispatches_nothing(self):
        from commit import _Profile
        self.committer.profiles["ollama"] = _Profile(parked_until=int(T) + 100)
        self.assertEqual(self.poll(), 0)


class BoundsTest(Base):
    def test_max_concurrent_jobs_holds_across_prs(self):
        self.deploy = deploy(max_concurrent_jobs=2)
        self.inst.prs = [pr(n) for n in (1, 2, 3)]
        self.assertEqual(self.poll(), 2)

    def test_a_full_deployment_stops_dispatch_and_every_head_still_gets_its_run(self):
        # PR #22 review: the cap broke the walk, so repos past it had no check
        # run and a broken config in one of them never showed.
        heads = {"acme/web": "a", "acme/api": "c", "acme/docs": "d"}
        for name, rid in (("api", 43), ("docs", 44)):
            self.inst.repo_list.append({"full_name": f"acme/{name}", "name": name, "id": rid,
                                        "private": True})
        self.inst.files[("acme/api", ".github/claudebox.json")] = \
            self.inst.files[("acme/web", ".github/claudebox.json")]
        self.inst.files[("acme/docs", ".github/claudebox.json")] = b'{"profile": "nope"}'
        self.inst.open_prs = lambda repo: [pr(n, head=heads[repo] * 39 + str(n)) for n in (1, 2)]
        self.deploy = deploy(max_concurrent_jobs=1)
        self.assertEqual(self.poll(), 1)
        for repo, h in heads.items():
            for n in (1, 2):
                self.assertIsNotNone(self.runs.config_answer(repo, h * 39 + str(n)), (repo, n))
        self.assertTrue(self.runs.config_answer("acme/docs", "d" * 39 + "1")[0])
        self.assertEqual(len(self.inst.created), 6)
        self.poll()
        self.assertEqual(sum("max_concurrent_jobs (1) reached" in l for l in self.logs), 1)

    def test_profile_concurrency_is_counted_in_pairs_in_flight(self):
        d = json.loads(json.dumps({"profiles": {"ollama": {
            "provider": "ollama", "models": ["glm-5.2:cloud"], "default_model": "glm-5.2:cloud",
            "credential": "OLLAMA_API_KEY", "concurrency": 3,
            "budget": {"unit": "tokens", "daily": 1}}}}))
        self.deploy = deploy_config.load_deploy(json.dumps(d).encode())
        self.inst.prs = [pr(1), pr(2)]
        self.assertEqual(self.poll(), 2)    # 2 pairs in flight leave room for 1 of PR 2's
        self.assertEqual([len(j.pairs) for j in self.jobs.all()], [2, 1])

    def test_shared_worktree_rule(self):
        # PR #20 review: red_team then Sage, one phase each, share the clone.
        self.assertTrue(shared_worktree(2))
        self.assertFalse(shared_worktree(1))


class ConfigTest(Base):
    def test_an_invalid_config_shows_on_the_check_run_and_nothing_runs(self):
        self.inst.files[("acme/web", ".github/claudebox.json")] = b'{"profile": "nope"}'
        self.assertEqual(self.poll(), 0)
        status, title, summary = self.inst.updated[-1][1:4]
        self.assertEqual((status, title), ("completed", "Not reviewed: configuration"))
        self.assertIn("'nope' is not a profile of this deployment", summary)

    def test_the_org_defaults_apply_under_the_repo_file(self):
        del self.inst.files[("acme/web", ".github/claudebox.json")]
        self.inst.files[("acme/.github", ".github/claudebox.json")] = json.dumps(
            {"profile": "ollama", "code": {"personas": "sme"}}).encode()
        self.poll()
        self.assertEqual([p.persona for p in self.jobs.all()[0].pairs], ["sme"])

    def test_a_config_problem_fixed_since_stops_showing_on_the_same_head(self):
        # PR #22 review: a deployment-side fix moves no PR's head.
        self.inst.files[("acme/web", ".github/claudebox.json")] = b'{"profile": "nope"}'
        self.poll()
        self.inst.files[("acme/web", ".github/claudebox.json")] = json.dumps(
            {"profile": "ollama"}).encode()
        self.assertEqual(self.poll(), 1)
        self.assertNotEqual(self.inst.updated[-1][2], "Not reviewed: configuration")

    def test_one_broken_file_falls_back_to_the_other_and_the_run_says_so(self):
        # The spec's fallback (PR #22 review, reversing round 2): the broken file
        # is dropped, the other applies, and the problem shows on the run.
        for repo_file, org_file, words in (
                (b'{"plan": {"label": ""}}', b'{"profile": "ollama"}', "repo file"),
                (b'{"profile": "ollama"}', b'{"plan": {"label": ""}}', "org file")):
            with self.subTest(words=words):
                for job in self.jobs.all():
                    self.sched.end_job(job, "test")
                self.sched.lose_repo("acme/web", "test")
                self.inst.files[("acme/web", ".github/claudebox.json")] = repo_file
                self.inst.files[("acme/.github", ".github/claudebox.json")] = org_file
                self.assertEqual(self.poll(), 1)
                summary = next(u[3] for u in reversed(self.inst.updated) if words in u[3])
                self.assertIn("one file was ignored", summary)

    def test_both_files_broken_is_not_reviewed(self):
        self.inst.files[("acme/web", ".github/claudebox.json")] = b'{"plan": {"label": ""}}'
        self.inst.files[("acme/.github", ".github/claudebox.json")] = b'{"plan": {"label": ""}}'
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.inst.updated[-1][2], "Not reviewed: configuration")

    def test_a_failed_read_skips_the_poll_and_publishes_nothing(self):
        # A failed read says nothing about the file (PR #22 review).
        def broken(repo, path):
            raise GitHubError(502, "x")
        self.inst.file_at_default = broken
        self.assertEqual(self.poll(), 0)
        self.assertEqual((self.inst.created, self.inst.updated), ([], []))
        self.assertTrue(any("trying again next poll" in l for l in self.logs))

    def test_a_failed_paint_is_retried_on_the_first_poll_past_its_hold(self):
        # PR #21 review: the gate compares against what the run shows, and a
        # failed write is held for 30 seconds rather than rewritten every poll.
        self.inst.files[("acme/web", ".github/claudebox.json")] = b'{"profile": "nope"}'
        real = self.inst.update_check_run

        def broken(*a, **k):
            raise GitHubError(502, "x")
        self.inst.update_check_run = broken
        self.poll()
        self.inst.update_check_run = real
        before = len(self.inst.updated)
        self.now[0] += 29
        self.poll()
        self.assertEqual(len(self.inst.updated), before)
        self.now[0] += 1
        self.poll()
        self.assertEqual(len(self.inst.updated), before + 1)
        self.assertEqual(self.inst.updated[-1][2], "Not reviewed: configuration")
        self.poll()
        self.assertEqual(len(self.inst.updated), before + 1)   # then quiet again

    def test_a_held_problem_writes_to_github_once_per_head(self):
        # PR #22 review: not once per head per poll.
        self.inst.files[("acme/web", ".github/claudebox.json")] = b'{"profile": "nope"}'
        self.poll()
        before = len(self.inst.updated)
        self.poll()
        self.poll()
        self.assertEqual(len(self.inst.updated), before)

    def test_a_cleared_problem_is_published_on_a_head_the_gate_holds(self):
        # PR #22 review: nothing else would publish a reviewed, unchanged head.
        self.poll()
        GateTest.finish(self, self.jobs.all()[0])
        self.inst.files[("acme/web", ".github/claudebox.json")] = b'{"profile": "nope"}'
        self.poll()
        self.assertEqual(self.inst.updated[-1][2], "Not reviewed: configuration")
        self.inst.files[("acme/web", ".github/claudebox.json")] = json.dumps(
            {"profile": "ollama", "code": {"personas": "red_team,sage"}}).encode()
        self.assertEqual(self.poll(), 0)
        self.assertNotEqual(self.inst.updated[-1][2], "Not reviewed: configuration")
        before = len(self.inst.updated)
        self.poll()
        self.assertEqual(len(self.inst.updated), before)   # unchanged: no extra GitHub call

    def test_a_lost_repo_that_returns_publishes_its_unchanged_problem(self):
        # PR #22 review: the last answer must not outlive the run it was on.
        self.inst.files[("acme/web", ".github/claudebox.json")] = b'{"profile": "nope"}'
        self.poll()
        self.sched.lose_repo("acme/web", "test")
        before = len(self.inst.updated)
        self.poll()
        self.assertGreater(len(self.inst.updated), before)
        self.assertEqual(self.inst.updated[-1][2], "Not reviewed: configuration")

    def test_requested_mode_is_shown_as_not_yet_supported(self):
        self.inst.files[("acme/web", ".github/claudebox.json")] = json.dumps(
            {"profile": "ollama", "trigger": "requested"}).encode()
        self.assertEqual(self.poll(), 0)
        self.assertIn("auto mode only", self.inst.updated[-1][3])

    def test_the_org_github_repo_itself_is_not_reviewed(self):
        self.inst.repo_list.append({"full_name": "acme/.github", "name": ".github", "id": 9,
                                    "private": True})
        self.poll()
        self.assertEqual({j.repo for j in self.jobs.all()}, {"acme/web"})

    def test_a_config_change_rebuilds_the_decider_and_keeps_its_state(self):
        self.poll()
        state = self.sched.decider_for("acme/web").state
        self.sched.end_job(self.jobs.all()[0], "test")
        self.inst.files[("acme/web", ".github/claudebox.json")] = json.dumps(
            {"profile": "ollama", "code": {"personas": "sme"}}).encode()
        self.poll()
        self.assertIs(self.sched.decider_for("acme/web").state, state)
        self.assertEqual(self.sched.decider_for("acme/web").personas["code"], ["sme"])


class JobEndTest(Base):
    def test_a_worker_that_exits_ends_its_job_and_its_pairs_stay_owed(self):
        self.poll()
        job = self.jobs.all()[0]
        self.runner.states[job.job_id] = "exited"
        self.assertEqual(self.sched.reap(), 1)
        self.assertEqual(self.jobs.all(), [])
        state = self.sched.decider_for("acme/web").state
        self.assertEqual(state.owed, {Pair(7, "code", "red_team"), Pair(7, "code", "sage")})
        self.assertIn(job.job_id, self.runner.stopped)
        self.assertEqual(self.records.conn().execute("SELECT end_reason FROM jobs").fetchone()[0],
                         "the worker exited")

    def test_an_unused_bootstrap_expires_the_job(self):
        self.poll()
        self.now[0] += 300
        self.assertEqual(self.sched.reap(), 1)

    def test_the_deadline_ends_a_fetched_job_and_revokes_its_started_pairs(self):
        self.poll()
        job = self.jobs.all()[0]
        job.fetched = True
        job.started["red_team"] = "c" * 16
        with self.records.tx() as c:
            c.execute("UPDATE jobs SET deadline_at = ?", (int(T) + 10,))
        self.now[0] += 10
        self.sched.reap()
        self.assertIn(("revoke", {"credential_ids": ["c" * 16]}), self.gw)
        self.assertEqual(self.records.conn().execute(
            "SELECT COUNT(*) FROM credentials WHERE revoked_at IS NULL").fetchone()[0], 0)

    def test_a_runner_refusal_ends_the_job_at_once(self):
        self.runner.refuse = True
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.jobs.all(), [])
        self.assertTrue(any("job runner refused" in l for l in self.logs))


class AccessTest(Base):
    def test_lose_repo_ends_jobs_without_holding_the_scheduler_lock(self):
        # end_job calls into the committer, which calls decider_for (PR #22 review).
        import threading
        self.poll()
        free = []
        real = self.sched.end_job

        def probe(job, reason):
            t = threading.Thread(target=lambda: free.append(self.sched.lock.acquire(timeout=0.5)
                                                            and (self.sched.lock.release() or True)))
            t.start()
            t.join()
            return real(job, reason)
        self.sched.end_job = probe
        self.sched.lose_repo("acme/web", "test")
        self.assertEqual(free, [True])

    def test_orphaned_rows_are_swept_once_every_live_listing_succeeded(self):
        # Lost while the control plane was down: nothing in memory names them.
        from outbox import JobContext
        self.box.accept(JobContext("old", 3, "gone/repo", 1, HEAD), "sage", 1, "x")
        self.box.accept(JobContext("old", 3, "gone/repo", 1, HEAD), "sme", 1, "token olk-x")
        self.assertEqual(self.sched.sweep_orphans({7}), 0)   # installation 7 not listed yet
        self.poll()
        self.assertEqual(self.sched.sweep_orphans({7}), 1)
        self.assertEqual(self.box.queued("gone/repo", HEAD), 0)
        self.assertEqual(self.records.conn().execute(
            "SELECT COUNT(*) FROM withheld WHERE repo = 'gone/repo'").fetchone()[0], 0)

    def test_a_listed_repo_the_walk_did_not_reach_keeps_its_rows(self):
        # PR #22 review: a drain stops the walk, and the sweep must key on the
        # listing, not on how far the walk got.
        from outbox import JobContext
        self.inst.repo_list.append({"full_name": "acme/docs", "name": "docs", "id": 44,
                                    "private": True})
        self.box.accept(JobContext("old", 7, "acme/docs", 1, HEAD), "sage", 1, "x")
        self.sched.halted.set()
        self.poll()
        self.assertNotIn("acme/docs", self.sched.installation_of)
        self.assertEqual(self.sched.sweep_orphans({7}), 0)
        self.assertEqual(self.box.queued("acme/docs", HEAD), 1)

    def test_a_listed_repo_that_turned_public_while_down_is_swept(self):
        from outbox import JobContext
        self.inst.repo_list.append({"full_name": "acme/pub", "name": "pub", "id": 44,
                                    "private": False})
        self.box.accept(JobContext("old", 7, "acme/pub", 1, HEAD), "sage", 1, "x")
        self.poll()
        self.assertEqual(self.sched.sweep_orphans({7}), 1)

    def test_a_failed_listing_blocks_the_sweep(self):
        from outbox import JobContext
        self.box.accept(JobContext("old", 3, "gone/repo", 1, HEAD), "sage", 1, "x")
        self.poll()

        def broken():
            raise GitHubError(502, "x")
        self.inst.repos = broken
        self.poll()
        self.assertEqual(self.sched.sweep_orphans({7}), 0)
        self.assertEqual(self.box.queued("gone/repo", HEAD), 1)

    def test_a_repo_gone_public_loses_its_jobs_findings_and_sessions_but_keeps_rounds(self):
        self.poll()
        job = self.jobs.all()[0]
        from outbox import JobContext
        self.box.accept(JobContext(job.job_id, 7, "acme/web", 7, HEAD), "sage", 1, "x")
        state = self.sched.decider_for("acme/web").state
        pair = Pair(7, "code", "sage")
        state.sessions[pair], state.rounds[pair] = "s", 3
        self.inst.repo_list[0]["private"] = False
        self.poll()
        self.assertEqual(self.jobs.all(), [])
        self.assertEqual(self.box.queued("acme/web", HEAD), 0)
        self.assertEqual((state.sessions, state.rounds[pair]), ({}, 3))
        before = len(self.logs)
        self.poll()
        self.assertEqual(self.logs[before:], [])   # a repo already lost is passed over quietly

    def test_a_repo_removed_from_the_installation_is_lost(self):
        import threading
        self.poll()
        free = []
        real = self.sched.end_job

        def probe(job, reason):
            t = threading.Thread(target=lambda: free.append(self.sched.lock.acquire(timeout=0.5)
                                                            and (self.sched.lock.release() or True)))
            t.start()
            t.join()
            return real(job, reason)
        self.sched.end_job = probe
        self.inst.repo_list = []
        self.poll()
        self.assertEqual(free, [True])   # decision 3: jobs end with no scheduler lock held
        self.assertEqual(self.jobs.all(), [])
        self.assertTrue(any("removed from the installation" in l for l in self.logs))

    def test_a_dispatch_rechecks_visibility_and_fails_closed(self):
        calls = []
        real = self.inst.repo

        def flaky(name):
            calls.append(name)
            raise GitHubError(502, "x")
        self.inst.repo = flaky
        self.assertEqual(self.poll(), 0)
        self.assertEqual(calls, ["acme/web"])
        self.inst.repo = real

    def test_no_deployment_config_means_no_poll(self):
        self.deploy = None
        self.assertEqual(self.poll(), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_scheduler.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'scheduler'`.

- [ ] **Step 3: Implement**

`hosted/scheduler.py`:

```python
"""The scheduler: what each poll reviews, and what happens to a job afterwards.

Per installation and repo, a poll reads the repo's config from the default
branch (the repo file over the org `.github` repo's), checks it against the
deployment, and walks the open PRs. A fork PR is never a candidate and gets
no check run. Every other PR's head gets one. A head with nothing new for a
pair is held by the decision core's change gate, a pair parked by the commit
point waits, and what is left is dispatched as one job per PR, within
`max_concurrent_jobs` and the profile's `concurrency`, counted in pairs in
flight. `reap` ends the jobs whose worker is gone, whose deadline passed, or
whose bootstrap token expired unused, and the pairs they did not complete
stay owed.

2a reviews in `auto` mode only: a repo asking for `requested` is shown as a
config problem rather than reviewed against its wishes. Pair state is in
memory (overview decision 1): one MemoryStateStore per repo, kept across
config changes, with a Decider rebuilt whenever the config it is built from
changes.
"""

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import checkruns
import creds as creds_mod
import personas as personas_mod
import prompts as prompts_mod
import repo_config
import signals as signals_mod
from channel import JobMaterial, PairMaterial
from common import ConfigError, Pair
from decisions import Decider
from deploy_config import against_deploy
from github_app import GitHubError, is_fork_pr, is_public
from runner import RunnerError
from state import MemoryStateStore

ORG_REPO = ".github"


def shared_worktree(n_pairs: int) -> bool:
    """Whether more than one persona reads the job's clone.

    Not whether two run at once: a phase-1 pass that mutates the clone is read
    by phase 2, and one-at-a-time passes read each other's changes the same
    way, so the cap does not enter into it (PR #20 review). Computed here,
    once, and sent to the worker in the job spec, which locks `.git` by it;
    the prompts carry WORKTREE_STANZA by the same answer.
    """
    return n_pairs > 1


@dataclass
class _Runtime:
    """A repo's decision core, and the config it was built from."""
    key: Tuple
    decider: Decider


class Scheduler:
    def __init__(self, records, creds, jobs, committer, check_runs, outbox, runner,
                 installation_for: Callable[[int], Any], deploy_for: Callable[[], Any],
                 gateway_call: Callable[..., Dict[str, Any]], persona_dir: str,
                 gateway_url: str, channel_url: str, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda line: print(line, flush=True),
                 new_job_id: Callable[[], str] = lambda: os.urandom(8).hex()):
        self.records = records
        self.creds = creds
        self.jobs = jobs
        self.committer = committer
        self.check_runs = check_runs
        self.outbox = outbox
        self.runner = runner
        self.installation_for = installation_for
        self.deploy_for = deploy_for
        self.gateway_call = gateway_call
        self.persona_dir = persona_dir
        self.gateway_url = gateway_url
        self.channel_url = channel_url
        self.clock = clock
        self.log = log
        self.new_job_id = new_job_id
        self.lock = threading.RLock()
        self.states: Dict[str, MemoryStateStore] = {}
        self.runtimes: Dict[str, _Runtime] = {}
        self.installation_of: Dict[str, int] = {}
        self.trackers: Dict[str, signals_mod.Tracker] = {}   # one per repo: a Tracker keys by PR number
        self._full = False
        self.full_installations: Set[int] = set()   # whose last poll met max_concurrent_jobs
        self.halted = threading.Event()   # set by the drain: dispatch nothing more
        self.walk = threading.Lock()
        # installation id -> the private repos its last successful listing
        # returned; an installation whose last listing failed has no entry. The
        # sweep keys on these, never on how far a poll's walk got, and runs only
        # when every live installation has one.
        self.listings: Dict[int, Set[str]] = {}

    # -- the decision core per repo ----------------------------------------------
    def decider_for(self, repo: str) -> Decider:
        with self.lock:
            runtime = self.runtimes.get(repo)
            if runtime is not None:
                return runtime.decider
            # A repo the scheduler has not configured yet (a commit for a job
            # dispatched before a config change, say) still needs somewhere to
            # record its outcome: an empty Decider over the repo's own state.
            state = self.states.setdefault(repo, MemoryStateStore())
            return Decider(state, {"code": [], "plan": []}, {}, {}, 0, 0)

    def _runtime(self, repo: str, cfg, deploy) -> Decider:
        key = (cfg.code_personas, cfg.plan_personas, cfg.rounds_cap, deploy.max_passes_per_session)
        runtime = self.runtimes.get(repo)
        if runtime is not None and runtime.key == key:
            return runtime.decider
        personas, prompts, phases = {}, {}, {}
        for mode, selector in (("code", cfg.code_personas), ("plan", cfg.plan_personas)):
            resolved = personas_mod.resolve(mode, self.persona_dir,
                                            {personas_mod.SELECTOR_VAR[mode]: selector})
            personas[mode] = [p.id for p in resolved]
            for p in resolved:
                prompts[(mode, p.id)] = p.prompt
                phases[(mode, p.id)] = p.phase
        state = self.states.setdefault(repo, MemoryStateStore())
        decider = Decider(state, personas, prompts, phases, deploy.max_passes_per_session,
                          cfg.rounds_cap)
        self.runtimes[repo] = _Runtime(key, decider)
        return decider

    # -- config ----------------------------------------------------------------------
    def load_config(self, inst, full_name: str, deploy):
        """(config, model, problem, notice). A problem means the repo is not reviewed.

        The spec's fallback, which repo_config.resolve implements: an invalid
        repo file falls back to the org defaults, an invalid org file under a
        valid repo file is dropped, and only both invalid means not reviewed.
        The dropped file's problems come back as the notice, shown on the run
        beside a review that runs. A read that fails is not a statement about
        either file: the GitHubError goes to `poll`, which skips the repo for
        this poll. A file that does not exist (404) is None, and an org
        `.github` repo the App cannot see reads as one.
        """
        owner = full_name.split("/", 1)[0]
        repo_data = inst.file_at_default(full_name, repo_config.REPO_FILE)
        org_data = inst.file_at_default(f"{owner}/{ORG_REPO}", repo_config.REPO_FILE)
        res = repo_config.resolve(org_data, repo_data, self.persona_dir)
        problems = res.repo_problems + res.org_problems
        if res.config is None:
            return None, None, "\n".join(problems), ""
        notice = "\n".join(problems)
        cfg = res.config
        if cfg.trigger != "auto":
            return None, None, ("trigger: requested arrives in phase 2b; this deployment "
                                "reviews in auto mode only"), notice
        check = against_deploy(cfg, deploy, full_name)
        if check.problems:
            return None, None, "\n".join(check.problems), notice
        return cfg, check.model, "", notice

    # -- the poll --------------------------------------------------------------------
    def poll(self, installation_id: int) -> int:
        """One installation's poll. Returns how many jobs it dispatched."""
        deploy = self.deploy_for()
        if deploy is None:
            return 0
        inst = self.installation_for(installation_id)
        try:
            repos = inst.repos()
        except GitHubError as exc:
            self.log(f"WARN: installation {installation_id}: could not list repositories: {exc}")
            self.listings.pop(installation_id, None)
            return 0
        seen = {r["full_name"] for r in repos}
        self.listings[installation_id] = {r["full_name"] for r in repos
                                          if r.get("name") != ORG_REPO and not is_public(r)}
        with self.lock:
            removed = [r for r, i in self.installation_of.items()
                       if i == installation_id and r not in seen]
        for repo in removed:   # outside the lock: lose_repo ends jobs (decision 3)
            self.lose_repo(repo, "it was removed from the installation")
        dispatched = 0
        self._full = False
        # The walk holds `walk`, so a drain that sets `halted` can wait for the
        # walk in progress to stop before it counts what is left to end.
        with self.walk:
            for info in repos:
                if self.halted.is_set():
                    break
                if info.get("name") == ORG_REPO:
                    continue
                try:
                    dispatched += self.poll_repo(inst, installation_id, info, deploy)
                except GitHubError as exc:
                    self.log(f"WARN: {info.get('full_name')}: {exc}; trying again next poll")
        # Logged when the cap starts binding and when it stops, not on every poll.
        if self._full and installation_id not in self.full_installations:
            self.full_installations.add(installation_id)
            self.log(f"installation {installation_id}: max_concurrent_jobs "
                     f"({deploy.max_concurrent_jobs}) reached; new work waits, and every head "
                     f"still gets its check run")
        elif not self._full and installation_id in self.full_installations:
            self.full_installations.discard(installation_id)
            self.log(f"installation {installation_id}: below max_concurrent_jobs again")
        return dispatched

    def poll_repo(self, inst, installation_id: int, info: Dict[str, Any], deploy) -> int:
        full = info["full_name"]
        if is_public(info):
            # A repo that turned public loses what it holds; one that always
            # was has nothing to lose, and is passed over without a word.
            if full in self.installation_of:
                self.lose_repo(full, "it is public")
            return 0
        with self.lock:
            self.installation_of[full] = installation_id
        cfg, model, problem, notice = self.load_config(inst, full, deploy)
        label = cfg.plan_label if cfg is not None else repo_config.RepoConfig().plan_label
        dispatched = 0
        now = self.clock()
        for pr in inst.open_prs(full):
            if is_fork_pr(pr):
                continue   # the spec's fork rule: no review, no check run
            n, head = int(pr["number"]), pr["head"]["sha"]
            labels = {l.get("name") for l in pr.get("labels") or []}
            mode = "plan" if label in labels else "code"
            if self.halted.is_set():
                break
            self.check_runs.ensure(full, head)
            # Published when this head's answer differs from the one last set on
            # it, including a head with none: a new head shows a held problem, a
            # fixed one clears without a push, and an unchanged answer costs no
            # GitHub call.
            answer = (problem, notice)
            if self.check_runs.config_answer(full, head) != answer:
                self.check_runs.set_config_problem(full, head, problem, notice)
                self._publish(full, head)
            if problem or self._full:
                # A full deployment stops the dispatch, never the walk: every
                # head past the cap still gets its run and its config answer
                # (decision 4), and the stages below cost lookups.
                continue
            if any(j.repo == full and j.pr == n for j in self.jobs.all()):
                continue
            if signals_mod.is_settling(pr.get("updated_at") or "", deploy.settle_seconds, now):
                continue
            decider = self._runtime(full, cfg, deploy)
            signal = self._signal(inst, full, n, head, mode, pr.get("updated_at") or "")
            group = decider.build_groups([(n, mode)])[0]
            # A pair at its round cap will not run at this head or any later
            # one, so nothing else would ever set its view: it is painted here,
            # once, or a capped plan PR's new head would read "Queued" for good.
            # It is painted as unread, never as reviewed: no pass read this commit.
            capped = [p for p in group.pairs if decider.capped(p)
                      and self.check_runs.view(full, head, p.persona) is None]
            for p in capped:
                self.check_runs.set_pair(full, head, p.persona,
                                         checkruns.PairView(checkruns.CAP_UNREAD))
            if capped:
                self._publish(full, head)
            pairs = [p for p in decider.pairs_to_run(group, signal)
                     if self.committer.parked(full, p, head) is None]
            if not pairs:
                continue
            if self.committer.profile_parked_until(cfg.profile):
                continue
            if len(self.jobs.all()) >= deploy.max_concurrent_jobs:
                self._full = True   # max_concurrent_jobs: this poll dispatches nothing more
                continue
            profile = deploy.profiles[cfg.profile]
            in_flight = sum(len(j.pairs) - len(j.completed) for j in self.jobs.all()
                            if j.profile == cfg.profile)
            room = profile.concurrency - in_flight
            if room <= 0:
                continue
            # A group larger than the room left runs in parts, phase order
            # first, so a 7-persona plan group under a bound of 4 is not
            # starved: the pairs left out hold no session, so the gate runs
            # them on a later poll, once this job has ended (PR #22 review).
            pairs = sorted(pairs, key=decider.phase_of)[:room]
            if self.dispatch(inst, installation_id, info, cfg, model, decider, n, head, mode,
                             pairs, signal):
                dispatched += 1
        return dispatched

    def _signal(self, inst, full: str, n: int, head: str, mode: str, updated_at: str):
        """The PR's fingerprint, through the repo's signals.Tracker, as local mode reads it.

        The Tracker caches a failed lookup against updatedAt too, so a GitHub
        outage costs one lookup per updatedAt change rather than one per poll.
        """
        snapshot = _Snapshot(n, head, mode, updated_at)

        def fetch(snap):
            try:
                entries = [(c.get("created_at"), c.get("body")) for c in inst.issue_comments(full, n)]
                entries += [(r.get("submitted_at"), r.get("body")) for r in inst.reviews(full, n)]
                entries += [(c.get("created_at"), c.get("body"))
                            for c in inst.newest_review_comments(full, n)]
            except GitHubError as exc:
                self.log(f"WARN: {full}#{n}: comment lookup failed ({exc}); gating on updatedAt")
                return None
            return signals_mod.Signal(head, mode, signals_mod.newest_unsigned(entries))
        tracker = self.trackers.setdefault(full, signals_mod.Tracker())
        signal = tracker.signal_for(snapshot, fetch)
        return signal if signal is not None else signals_mod.degraded(snapshot)

    # -- dispatch ----------------------------------------------------------------
    def dispatch(self, inst, installation_id, info, cfg, model, decider, n, head, mode,
                 pairs: List[Pair], signal) -> bool:
        full = info["full_name"]
        try:
            fresh = inst.repo(full)
        except GitHubError as exc:
            self.log(f"WARN: {full}: visibility check failed ({exc}); not dispatching")
            return False
        if fresh is None or is_public(fresh):
            self.lose_repo(full, "it is public" if fresh else "the App can no longer see it")
            return False
        deploy = self.deploy_for()
        profile = deploy.profiles[cfg.profile]
        shared = shared_worktree(len(pairs))
        prompts = prompts_mod.build_hosted(frozenset({mode}) if shared else frozenset())
        materials, rewritten_any = [], False
        for p in pairs:
            session = decider.state.sessions.get(p)
            template = prompts.followup[mode] if session else prompts.review[mode]
            since = self.committer.last_reviewed_head(full, p)
            rewritten = False
            if since is not None and since != head:
                try:
                    rewritten = not inst.is_ancestor(full, since, head)
                except GitHubError:
                    rewritten = True   # cannot tell: review the whole PR rather than a guess
            rewritten_any = rewritten_any or rewritten
            materials.append(PairMaterial(
                p.persona, decider.phase_of(p),
                decider.system_prompt_for(p, since_head=since, rewritten=rewritten),
                prompts_mod.render(template, n), session_id=session, signal=signal))
        job_id = self.new_job_id()
        now = int(self.clock())
        route = {"name": profile.name, "provider": profile.provider, "base_url": profile.base_url,
                 "auth": profile.auth, "credential": profile.credential,
                 "per_pair_requests": profile.per_pair_requests,
                 "per_pair_tokens": profile.per_pair_tokens, "budget_unit": profile.budget_unit}
        job = JobMaterial(
            job_id, installation_id, full, int(info["id"]), n, head, mode, profile.name, model,
            "CLAUDE_CODE_OAUTH_TOKEN" if profile.auth == "oauth_token" else "ANTHROPIC_AUTH_TOKEN",
            route, cfg.max_concurrent_personas, shared, materials, self.gateway_url,
            self.channel_url)
        with self.records.tx() as c:
            c.execute("INSERT INTO jobs (id, installation_id, repo, pr, head, mode, profile, model, "
                      "dispatched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (job_id, installation_id, full, n, head, mode, profile.name, model, now))
        self.jobs.add(job)
        boot = self.creds.mint("bootstrap", job_id, creds_mod.BOOTSTRAP_TTL)
        for p in pairs:
            self.check_runs.set_pair(full, head, p.persona, checkruns.PairView(checkruns.QUEUED),
                                     rewritten=rewritten_any)
        self._publish(full, head)
        try:
            self.runner.start(job_id, boot, self.channel_url, self.gateway_url)
        except RunnerError as exc:
            self.end_job(job, f"the job runner refused it: {exc}")
            return False
        self.log(f"{full}#{n} {mode}: dispatched job {job_id} for "
                 f"{', '.join(p.persona for p in pairs)}")
        return True

    # -- the end of a job ------------------------------------------------------------
    def reap(self) -> int:
        """End every job that is over. Returns how many."""
        try:
            states = {j["job_id"]: j["state"] for j in self.runner.list()}
        except RunnerError as exc:
            self.log(f"WARN: could not list workers: {exc}")
            return 0
        now = int(self.clock())
        ended = 0
        for job in self.jobs.all():
            row = self.records.conn().execute(
                "SELECT dispatched_at, deadline_at FROM jobs WHERE id = ?", (job.job_id,)).fetchone()
            state = states.get(job.job_id)
            reason = None
            if state is None or state in ("exited", "dead"):
                reason = "the worker exited"
            elif row is not None and row[1] is not None and now >= row[1]:
                reason = "the job deadline passed"
            elif not job.fetched and row is not None and now >= row[0] + creds_mod.BOOTSTRAP_TTL:
                reason = "the bootstrap token expired unused"
            if reason:
                self.end_job(job, reason)
                ended += 1
        return ended

    def end_job(self, job: JobMaterial, reason: str) -> None:
        if self.jobs.remove(job.job_id) is None:
            return
        self.creds.revoke_job(job.job_id)
        unfinished = [c for p, c in job.started.items() if p not in job.completed]
        if unfinished:
            try:
                self.gateway_call("revoke", credential_ids=unfinished)
            except Exception as exc:
                self.log(f"WARN: could not tell the gateway to revoke job {job.job_id}'s tokens: {exc}")
        decider = self.decider_for(job.repo)
        for material in job.pairs:
            if material.persona in job.completed:
                continue
            pair = Pair(job.pr, job.mode, material.persona)
            if material.persona in job.started:
                self.committer.revoke(job.repo, pair)   # `revoked`, shown below
            else:
                decider.state.owed.add(pair)
            self.check_runs.set_pair(job.repo, job.head, material.persona,
                                     checkruns.PairView(checkruns.REVOKED))
        try:
            self.runner.stop(job.job_id)
        except RunnerError as exc:
            self.log(f"WARN: could not stop job {job.job_id}'s worker: {exc}")
        self.outbox.forget_job(job.job_id)
        with self.records.tx() as c:
            c.execute("UPDATE jobs SET ended_at = ?, end_reason = ? WHERE id = ?",
                      (int(self.clock()), reason[:200], job.job_id))
        self._publish(job.repo, job.head)
        self.log(f"{job.repo}#{job.pr} {job.mode}: job {job.job_id} ended: {reason}")

    def lose_repo(self, repo: str, reason: str) -> None:
        """Access changed: end the repo's jobs and delete what it holds, keeping its rounds."""
        # The jobs end with no scheduler lock held: end_job calls into the
        # committer, which calls decider_for, which takes this lock (PR #22 review).
        for job in [j for j in self.jobs.all() if j.repo == repo]:
            self.end_job(job, f"access changed: {reason}")
        with self.lock:
            dropped = self.outbox.drop_repo(repo, reason)
            self.committer.forget_repo(repo)
            # The check runs' rows go, and with them the last config answer
            # published, so a repo that comes back publishes it again.
            self.check_runs.forget_repo(repo)
            state = self.states.get(repo)
            if state is not None:
                state.forget()
            self.runtimes.pop(repo, None)
            self.installation_of.pop(repo, None)
            self.trackers.pop(repo, None)
        self.log(f"{repo}: not reviewed, because {reason}; dropped {dropped} queued finding(s)")

    def sweep_orphans(self, live: Set[int]) -> int:
        """Rows of repos no live installation lists: lost while this process was down.

        `installation_of` is in memory, so an uninstall during a restart leaves
        outbox and withheld rows nothing else would ever drop. Runs only after
        every live installation's listing succeeded. Returns how many repos.
        """
        if not live <= set(self.listings):
            return 0
        known = set().union(*(self.listings.get(i, set()) for i in live))
        conn = self.records.conn()
        rows = {r[0] for r in conn.execute("SELECT DISTINCT repo FROM outbox")}
        rows |= {r[0] for r in conn.execute("SELECT DISTINCT repo FROM withheld")}
        orphans = sorted(rows - known)
        for repo in orphans:
            self.lose_repo(repo, "no installation lists it any more")
        return len(orphans)

    def _publish(self, repo: str, head: str) -> None:
        try:
            self.check_runs.publish(repo, head, queued=self.outbox.queued(repo, head))
        except GitHubError as exc:
            self.log(f"WARN: could not update the check run for {repo}@{head[:12]}: {exc}")


@dataclass(frozen=True)
class _Snapshot:
    """The fields signals.Tracker and signals.degraded read, from a PR the REST API listed."""
    number: int
    head_oid: str
    mode: str
    updated_at: str
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_scheduler.py'`, then `./test-python.sh`
Expected: PASS, 51 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/scheduler.py tests/test_hosted_scheduler.py
git commit -m "hosted: the scheduler, from a poll to a dispatched job and back"
```

### Task 3: The control-plane process

**Files:**
- Create: `hosted/control.py`
- Test: `tests/test_hosted_control.py`

**Interfaces:**
- Consumes: `records.Records` and `InstanceLock`, `creds.Creds`, `blobs`, `crypto`, `github_app.App`, `scan.Scanner` (2a.1); `gwproto` (2a.2); `runner.RunnerClient` and `RunnerError` (2a.3); 2a.4a's modules; Task 2.
- Produces:
  - `WAITING`, `NOT_CONFIGURED`, `SERVING`, `DRAINING`; `DRAIN_SECONDS = 600`; `GATEWAY_TIMEOUT = 60.0`; `SCAN_PREFIX = "CLAUDEBOX_SCAN_"`.
  - `GatewayLink(path, secret, tokens, timeout=60.0)`, callable as `gateway_call(op, **fields)`, with `hello()`.
  - `ControlPlane(records, lock, creds, blobs, app, runner, gateway_call, env, persona_dir, channel_url, gateway_url, clock=time.time, log=print)` with `take_lock() -> bool`, `startup()`, `deploy()`, `backoff_for(profile) -> int`, `hello_tokens() -> list`, `handle_report(msg) -> dict`, `poll_once() -> int`, `start_loops()`, `healthy() -> bool`, `drain(deadline_seconds) -> int`, and the attributes `state`, `jobs`, `scheduler`, `committer`, `channel`, `outbox`, `check_runs`.
  - `NO_DEPLOY`, `NO_KEYS`, `LOCK_FILE = "control.lock"`; `health_server(control, host, port)`; `keys_from_env(env, crypto_mod, app_cls, log=print) -> (sealer, app)`; `pem_from_env(value) -> bytes`; `main(env)`, which reads `CLAUDEBOX_DATA`, `CLAUDEBOX_DEPLOYMENT_KEY`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GATEWAY_SECRET`, `GATEWAY_SOCKET`, `CONTROL_SOCKET`, `RUNNER_SECRET`, `RUNNER_SOCKET`, `CLAUDEBOX_CHANNEL_URL`, `CLAUDEBOX_GATEWAY_URL`, `CLAUDEBOX_DRAIN_SECONDS`, `CHANNEL_LISTEN` and `HEALTH_LISTEN`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_control.py`:

```python
import _path  # noqa: F401

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import creds
import gwproto
from blobs import Blobs
from control import (DRAINING, NOT_CONFIGURED, SERVING, WAITING, ControlPlane, GatewayLink,
                     health_server, keys_from_env, pem_from_env, _Status, LOCK_FILE)
from records import InstanceLock, Records

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PERSONAS = os.path.join(ROOT, "personas")
T = 1760000000.0
DEPLOY = {"profiles": {"ollama": {"provider": "ollama", "models": ["glm-5.2:cloud"],
                                  "default_model": "glm-5.2:cloud", "credential": "OLLAMA_API_KEY",
                                  "budget": {"unit": "tokens", "daily": 1}}},
          "caps": {"comment_bytes": 1234}}


class FakeSealer:
    def seal(self, plaintext, aad):
        return aad + b":" + plaintext

    def open(self, sealed, aad):
        return sealed[len(aad) + 1:]


class FakeInst:
    def repos(self):
        return [{"full_name": "acme/web", "name": "web", "id": 42, "private": True}]

    def file_at_default(self, repo, path):
        return json.dumps({"profile": "ollama"}).encode() if repo == "acme/web" else None

    def open_prs(self, repo):
        return []


class FakeApp:
    def __init__(self):
        self.installs = [{"id": 7}]
        self.made = []

    def installations(self):
        return self.installs

    def installation(self, inst_id):
        self.made.append(inst_id)
        return FakeInst()


class FakeRunner:
    def __init__(self):
        self.stopped_all = 0

    def stop_all(self):
        self.stopped_all += 1
        return 2

    def list(self):
        return []

    def stop(self, job_id):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.dir = d
        self.records = Records(os.path.join(d, "r.db"))
        self.addCleanup(self.records.close)
        self.lock = InstanceLock(os.path.join(d, "r.lock"))
        self.addCleanup(self.lock.release)
        self.creds = creds.Creds(self.records, clock=lambda: T)
        self.blobs = Blobs(os.path.join(d, "blobs"), FakeSealer())
        self.app, self.runner = FakeApp(), FakeRunner()
        self.gw, self.logs = [], []
        self.control = ControlPlane(
            self.records, self.lock, self.creds, self.blobs, self.app, self.runner,
            lambda op, **f: self.gw.append((op, f)) or {"ok": True},
            {"CLAUDEBOX_SCAN_OLLAMA_API_KEY": "olk-a-long-secret",
             "CLAUDEBOX_SCAN_FLY_APP_NAME": "claudebox-control", "CLAUDEBOX_SCAN_FLY_REGION": "iad",
             "UNRELATED": "x"},
            PERSONAS, "http://control:8080", "http://gateway:8090", clock=lambda: T,
            log=self.logs.append)

    def apply(self, doc=DEPLOY, stamp=1):
        with self.records.tx() as c:
            c.execute("INSERT OR REPLACE INTO deploy_config (id, body, applied_at, applied_by) "
                      "VALUES (1, ?, ?, 1)", (json.dumps(doc).encode(), stamp))


class StartupTest(Base):
    def test_it_waits_while_another_instance_holds_the_lock(self):
        other = InstanceLock(os.path.join(self.dir, "r.lock"))
        self.assertTrue(other.try_acquire())
        self.assertFalse(self.control.take_lock())
        self.assertEqual(self.control.state, WAITING)
        self.assertEqual(self.runner.stopped_all, 0)
        other.release()
        self.assertTrue(self.control.take_lock())

    def test_taking_the_lock_revokes_stops_and_deletes_before_serving(self):
        self.creds.mint("capability", "old-job", 3600)
        self.blobs.put("transcripts/acme/web/pr-1/code/sage/x.jsonl", b"t")
        self.blobs.put("other/keep", b"k")
        self.apply()
        self.assertTrue(self.control.take_lock())
        self.assertEqual(self.records.conn().execute(
            "SELECT COUNT(*) FROM credentials WHERE revoked_at IS NULL").fetchone()[0], 0)
        self.assertEqual(self.runner.stopped_all, 1)
        self.assertEqual(self.blobs.list("transcripts"), [])
        self.assertEqual(self.blobs.get("other/keep"), b"k")
        self.assertEqual(self.gw[0], ("hello", {"tokens": []}))
        self.assertEqual(self.control.state, SERVING)

    def test_taking_the_lock_closes_the_previous_instances_jobs(self):
        # PR #22 review: the jobs table was the one record of prior work
        # startup left open.
        with self.records.tx() as c:
            for jid, ended in (("old1", None), ("old2", None), ("done", 5)):
                c.execute("INSERT INTO jobs (id, installation_id, repo, pr, head, mode, profile, "
                          "model, dispatched_at, ended_at, end_reason) VALUES (?, 7, 'acme/web', "
                          "1, 'h', 'code', 'ollama', 'glm', 0, ?, ?)",
                          (jid, ended, "finished" if ended else None))
        self.assertTrue(self.control.take_lock())
        rows = dict(self.records.conn().execute("SELECT id, end_reason FROM jobs").fetchall())
        self.assertEqual(rows, {"old1": "the control plane restarted",
                                "old2": "the control plane restarted", "done": "finished"})
        self.assertTrue(any("closed 2 job(s)" in l for l in self.logs))

    def test_a_runner_that_cannot_stop_orphans_does_not_stop_startup(self):
        # PR #22 review: a runnerd not yet listening raises what the socket
        # raises, never RunnerError, and startup must survive both.
        from runner import RunnerError
        for exc in (RunnerError("docker ps failed"), FileNotFoundError(2, "no socket"),
                    ConnectionRefusedError(111, "refused"), gwproto.ProtocolError("short read")):
            with self.subTest(exc=type(exc).__name__):
                self.setUp()

                def refuse(exc=exc):
                    raise exc
                self.runner.stop_all = refuse
                self.creds.mint("capability", "old-job", 3600)
                self.assertTrue(self.control.take_lock())
                self.assertEqual(self.records.conn().execute(
                    "SELECT COUNT(*) FROM credentials WHERE revoked_at IS NULL").fetchone()[0], 0)
                self.assertTrue(any("could not stop orphaned workers" in l for l in self.logs))
                self.assertEqual(self.gw[0], ("hello", {"tokens": []}))

    def test_no_deployment_config_is_not_configured_and_dispatches_nothing(self):
        self.control.take_lock()
        self.assertEqual(self.control.state, NOT_CONFIGURED)
        self.assertEqual(self.control.poll_once(), 0)
        self.assertEqual(self.app.made, [])
        self.apply()
        self.control.poll_once()
        self.assertEqual(self.control.state, SERVING)

    def test_the_scanner_uses_only_the_credentials_the_deployment_names(self):
        # A platform's own variables are not secrets (PR #22 review).
        self.apply()
        self.control.deploy()
        scanner = self.control.outbox.scanner
        self.assertEqual(scanner.first("leaked olk-a-long-secret"), "literal:OLLAMA_API_KEY")
        self.assertIsNone(scanner.first("deployed as claudebox-control"))
        self.assertFalse(any("FLY_" in l for l in self.logs))

    def test_a_credential_the_deployment_names_but_the_platform_lacks_is_warned(self):
        doc = json.loads(json.dumps(DEPLOY))
        doc["profiles"]["ollama"]["credential"] = "MISSING_KEY"
        self.apply(doc)
        self.control.deploy()
        self.assertTrue(any("MISSING_KEY" in l and "does not hold" in l for l in self.logs))

    def test_not_configured_names_its_cause_once(self):
        self.control.take_lock()
        self.control.poll_once()
        self.control.poll_once()
        hits = [l for l in self.logs if "hosted config apply" in l]
        self.assertEqual(len(hits), 1)
        self.assertIn("no deployment config", self.control.reason)


class ReportTest(Base):
    def test_reports_are_refused_until_the_lock_is_held(self):
        self.assertIn("error", self.control.handle_report({"op": "report"}))

    def test_a_report_commits(self):
        self.apply()
        self.control.take_lock()
        token = self.creds.mint("provider", "job1", 600, persona="sage")
        cid = creds.credential_id_of(token)
        import commit
        with self.records.tx() as c:
            commit.ensure_record(c, cid)
        reply = self.control.handle_report({"op": "report", "credential_id": cid, "seq": 1,
                                            "delta": {"requests": 1}})
        self.assertEqual(reply, {"ack": 1})


class DeployTest(Base):
    def test_config_apply_takes_effect_without_a_restart(self):
        self.apply()
        self.assertEqual(self.control.deploy().comment_bytes, 1234)
        self.assertEqual(self.control.outbox.comment_bytes, 1234)
        doc = json.loads(json.dumps(DEPLOY))
        doc["caps"]["comment_bytes"] = 999
        self.apply(doc, stamp=2)
        self.control.deploy()
        self.assertEqual(self.control.outbox.comment_bytes, 999)

    def test_a_stored_config_that_does_not_load_keeps_the_last_good_one(self):
        self.apply()
        self.control.deploy()
        self.apply({"nonsense": 1}, stamp=2)
        self.assertEqual(self.control.deploy().comment_bytes, 1234)
        self.assertTrue(any("does not load" in l for l in self.logs))

    def test_the_backoff_comes_from_the_profile(self):
        self.apply()
        self.control.deploy()
        self.assertEqual(self.control.backoff_for("ollama"), 1800)
        self.assertEqual(self.control.backoff_for("gone"), 1800)


class PollTest(Base):
    def test_an_uninstalled_installation_loses_its_repos_and_its_drainer(self):
        self.apply()
        self.control.take_lock()
        self.control.poll_once()
        self.assertIn("acme/web", self.control.scheduler.installation_of)
        self.assertIn(7, self.control._drainers)
        self.app.installs = []
        self.control.poll_once()
        self.assertNotIn("acme/web", self.control.scheduler.installation_of)
        self.assertEqual(self.control._drainers, {})
        # PR #18 review: the spec pages this, and the poll is its only producer.
        self.assertEqual([l for l in self.logs if l.startswith("PAGE:")],
                         ["PAGE: installation 7 was uninstalled or suspended; 1 repo(s) stop "
                          "being reviewed"])
        self.control.poll_once()
        self.assertEqual(sum(l.startswith("PAGE:") for l in self.logs), 1)   # once

    def test_a_suspended_installation_is_not_polled(self):
        self.apply()
        self.control.take_lock()
        self.app.installs = [{"id": 7, "suspended_at": "2025-10-09T00:00:00Z"}]
        self.control.poll_once()
        self.assertEqual(self.app.made, [])

    def test_draining_dispatches_nothing(self):
        self.apply()
        self.control.take_lock()
        self.control.state = DRAINING
        self.assertEqual(self.control.poll_once(), 0)
        self.assertEqual(self.app.made, [])


class DrainWalkTest(Base):
    def test_the_drain_halts_the_walk_and_waits_for_one_in_progress(self):
        # PR #22 review: a walk past poll_once's check would otherwise keep dispatching.
        import time as time_mod
        self.apply()
        self.control.take_lock()
        entered, walked, results = threading.Event(), threading.Event(), []
        real = self.control.scheduler.poll_repo

        def slow(*a, **k):
            entered.set()
            time_mod.sleep(0.2)
            try:
                return real(*a, **k)
            finally:
                walked.set()
        self.control.scheduler.poll_repo = slow
        t = threading.Thread(target=lambda: results.append(self.control.poll_once()))
        t.start()
        self.assertTrue(entered.wait(5))
        self.control.drain(5)
        self.assertTrue(walked.is_set())   # the drain waited for the walk before counting
        t.join(5)
        self.assertTrue(self.control.scheduler.halted.is_set())
        self.assertEqual(self.control.poll_once(), 0)


class HelloTest(Base):
    def test_hello_carries_each_live_token_with_its_job_and_revokes_an_orphan(self):
        from channel import JobMaterial, PairMaterial
        route = {"name": "ollama"}
        self.control.jobs.add(JobMaterial(
            "job1", 7, "acme/web", 42, 12, "a" * 40, "code", "ollama", "glm", "ANTHROPIC_AUTH_TOKEN",
            route, 0, False, [PairMaterial("sage", 1, "s", "p")], "g", "c"))
        live = creds.credential_id_of(self.creds.mint("provider", "job1", 600, persona="sage"))
        orphan = creds.credential_id_of(self.creds.mint("provider", "gone", 600, persona="sage"))
        tokens = self.control.hello_tokens()
        self.assertEqual([(t["credential_id"], t["model"], t["route"]) for t in tokens],
                         [(live, "glm", route)])
        self.assertIsNotNone(self.records.conn().execute(
            "SELECT revoked_at FROM credentials WHERE id = ?", (orphan,)).fetchone()[0])


class DrainTest(Base):
    def test_the_drain_ends_what_is_left_at_the_deadline(self):
        from channel import JobMaterial, PairMaterial
        self.control.jobs.add(JobMaterial(
            "job1", 7, "acme/web", 42, 12, "a" * 40, "code", "ollama", "glm", "ANTHROPIC_AUTH_TOKEN",
            {}, 0, False, [PairMaterial("sage", 1, "s", "p")], "g", "c"))
        with self.records.tx() as c:
            c.execute("INSERT INTO jobs (id, installation_id, repo, pr, head, mode, profile, model, "
                      "dispatched_at) VALUES ('job1', 7, 'acme/web', 12, 'h', 'code', 'ollama', "
                      "'glm', 0)")
        self.control.runner.list = lambda: [{"job_id": "job1", "state": "running"}]
        self.assertEqual(self.control.drain(0), 1)
        self.assertEqual(self.control.jobs.all(), [])
        self.assertTrue(self.control.stopping.is_set())
        self.assertEqual(self.records.conn().execute("SELECT end_reason FROM jobs").fetchone()[0],
                         "the control plane is stopping")


class HealthTest(Base):
    def get(self):
        server = health_server(self.control, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/healthz"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_waiting_for_the_lock_answers_200_so_the_old_instance_is_told_to_stop(self):
        self.assertEqual(self.get(), (200, "waiting for lock"))

    def test_before_a_control_plane_exists_it_reports_its_own_state(self):
        status = _Status()
        self.assertEqual((status.state, status.healthy()), (WAITING, True))
        status.state = NOT_CONFIGURED
        self.assertEqual(status.state, NOT_CONFIGURED)
        status.current = self.control
        self.control.state = SERVING
        self.assertEqual(status.state, SERVING)

    def test_the_lock_file_has_decision_13s_name(self):
        self.assertEqual(LOCK_FILE, "control.lock")

    def test_a_dead_loop_answers_503(self):
        self.control.state = SERVING
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        self.control.threads["poll"] = dead
        self.assertEqual(self.get()[0], 503)


class PemTest(unittest.TestCase):
    def test_an_env_file_line_becomes_a_pem_and_a_real_one_is_untouched(self):
        real = "-----BEGIN KEY-----\nAAA\\nBBB\n-----END KEY-----\n"
        self.assertEqual(pem_from_env(real), real.encode())
        self.assertEqual(pem_from_env("-----BEGIN KEY-----\\nAAA\\n-----END KEY-----"),
                         b"-----BEGIN KEY-----\nAAA\n-----END KEY-----")


class KeysTest(unittest.TestCase):
    """A malformed key is named and leaves the deployment not configured (PR #22 review)."""

    class Crypto:
        class CryptoError(Exception):
            pass

        def __init__(self, bad=()):
            self.bad = bad

        def decode_key(self, text):
            if "deploy" in self.bad:
                raise self.CryptoError("the deployment key must be 32 bytes; got 3")
            return text

        def AesGcmSealer(self, key):
            return ("sealer", key)

        def Rs256Signer(self, pem):
            if "app" in self.bad:
                raise self.CryptoError("the App private key is not an unencrypted PEM key")
            return ("signer", pem)

    ENV = {"CLAUDEBOX_DEPLOYMENT_KEY": "dk", "GITHUB_APP_PRIVATE_KEY": "pem", "GITHUB_APP_ID": "1"}

    def keys(self, bad=()):
        logs = []
        sealer, app = keys_from_env(self.ENV, self.Crypto(bad), lambda i, s: ("app", i, s),
                                    log=logs.append)
        return sealer, app, logs

    def test_good_keys_build_both(self):
        sealer, app, logs = self.keys()
        self.assertEqual((sealer, app[1], logs), (("sealer", "dk"), "1", []))

    def test_each_malformed_key_is_named_and_there_is_no_app(self):
        for bad, name in (("deploy", "CLAUDEBOX_DEPLOYMENT_KEY"), ("app", "GITHUB_APP_PRIVATE_KEY")):
            with self.subTest(bad=bad):
                _, app, logs = self.keys((bad,))
                self.assertIsNone(app)
                self.assertTrue(logs[0].startswith(name + ": "), logs)


class GatewayLinkTest(unittest.TestCase):
    def test_hello_first_is_answered_with_a_hello_and_the_call_retried(self):
        d = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, d)
        seen = []
        held = {"hello": False}

        def handle(msg):
            seen.append(msg["op"])
            if msg["op"] == "hello":
                held["hello"] = True
                return {"ok": True, "tokens": len(msg["tokens"])}
            if not held["hello"]:
                return {"error": "hello first", "ready": False}
            return {"last_seq": 3, "drained": True}
        server = gwproto.Server(os.path.join(d, "g.sock"), "s3cret", handle).start()
        self.addCleanup(server.stop)
        link = GatewayLink(os.path.join(d, "g.sock"), "s3cret", lambda: [{"credential_id": "x"}])
        self.assertEqual(link("flush", credential_id="x"), {"last_seq": 3, "drained": True})
        self.assertEqual(seen, ["flush", "hello", "flush"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_control.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'control'`.

- [ ] **Step 3: Implement**

`hosted/control.py`:

```python
"""The control-plane process: the lock, startup, the loops, and /healthz.

Until it holds the records store's lock it serves /healthz as "waiting for
lock" and nothing else, so an overlapping deploy's probe passes while the old
instance stops. Holding the lock, it makes the spec's startup true before it
serves anything: every outstanding credential is revoked, every orphaned
worker stopped, every transcript blob deleted (pair state is in memory, so
nothing points at one), and the gateway is sent an empty `hello`, which drops
every token it held. A deployment with no App credentials, or no deployment
file in the records store yet, is "not configured": /healthz answers and
nothing is dispatched.

Then three loops run on threads: the poll (each installation's scheduler
poll, then the reap of ended jobs), the outbox drain (one Drainer per
installation, round-robin), and the gateway's report socket. The worker
channel is its own HTTP server. On SIGTERM dispatching stops, running jobs
get until the drain deadline, and whatever is left is ended as revoked.
"""

import http.server
import os
import signal
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

import blobs as blobs_mod
import commit as commit_mod
import gwproto
from channel import Channel, Jobs, make_server as make_channel_server
from checkruns import CheckRuns
from commit import Committer
from deploy_config import DeployError, load_deploy
from github_app import GitHubError
from outbox import Drainer, Outbox
from runner import RunnerError
from scan import Scanner
from scheduler import Scheduler

WAITING, NOT_CONFIGURED, SERVING, DRAINING = ("waiting for lock", "not configured", "serving",
                                              "draining")
LOCK_RETRY_SECONDS = 5
LOCK_FILE = "control.lock"    # overview decision 13's name, and 2a.1's test's
DRAIN_SECONDS = 600            # the spec's local and Fly grace period
GATEWAY_TIMEOUT = 60.0         # the gateway may hold a flush for 50 seconds
SCAN_PREFIX = "CLAUDEBOX_SCAN_"
NO_DEPLOY = ("no deployment config in the records store yet; apply one with "
             "`claudebox.sh hosted config apply FILE`")
NO_KEYS = ("the App credentials or the deployment key are missing or malformed; "
           "run `claudebox.sh hosted deploy`, then `claudebox.sh app create`")


class GatewayLink:
    """Calls into the gateway, answering "hello first" with a hello.

    One connection per call: a flush may be held for 50 seconds, and a shared
    connection would queue every other call behind it.
    """

    def __init__(self, path: str, secret: str, tokens: Callable[[], list],
                 timeout: float = GATEWAY_TIMEOUT):
        self.path, self.secret, self.tokens, self.timeout = path, secret, tokens, timeout

    def _call(self, op: str, **fields: Any) -> Dict[str, Any]:
        client = gwproto.Client(self.path, self.secret, self.timeout)
        try:
            return client.call(op, **fields)
        finally:
            client.close()

    def hello(self) -> Dict[str, Any]:
        return self._call("hello", tokens=self.tokens())

    def __call__(self, op: str, **fields: Any) -> Dict[str, Any]:
        reply = self._call(op, **fields)
        if reply.get("error") == "hello first":
            self.hello()
            reply = self._call(op, **fields)
        return reply


class ControlPlane:
    def __init__(self, records, lock, creds, blobs, app, runner, gateway_call, env: Mapping[str, str],
                 persona_dir: str, channel_url: str, gateway_url: str,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda line: print(line, flush=True)):
        self.records, self.lock, self.creds, self.blobs = records, lock, creds, blobs
        self.app, self.runner, self.clock, self.log = app, runner, clock, log
        self.state = WAITING
        self.stopping = threading.Event()
        self.threads: Dict[str, threading.Thread] = {}
        self._deploy, self._deploy_stamp = None, None
        self._installations: Dict[int, Any] = {}
        self._drainers: Dict[int, Drainer] = {}
        # Every candidate literal the supervisor handed over. The scanner uses
        # only those the applied deployment names (its profiles' credentials and
        # the Voyage key), so a platform's own variables (FLY_REGION, K_SERVICE)
        # are never treated as secrets. Rebuilt whenever the deployment changes.
        self._candidates = {k[len(SCAN_PREFIX):]: v for k, v in env.items()
                            if k.startswith(SCAN_PREFIX)}
        scanner = Scanner({})
        self.reason = ""   # why the state is what it is, for /healthz and the log
        self.jobs = Jobs()
        self.check_runs = CheckRuns(records, self.installation_for_repo, clock=clock)
        self.outbox = Outbox(records, scanner, self.check_runs, 60000, 25, clock=clock)
        self.gateway_call = gateway_call
        self.scheduler = Scheduler(
            records, creds, self.jobs, None, self.check_runs, self.outbox, runner,
            self.installation, self.deploy, gateway_call, persona_dir, gateway_url, channel_url,
            clock=clock, log=log)
        self.committer = Committer(records, blobs, self.scheduler.decider_for, self.backoff_for,
                                   clock=clock, log=log,
                                   job_live=lambda job_id: self.jobs.get(job_id) is not None)
        self.scheduler.committer = self.committer
        self.channel = Channel(records, creds, self.jobs, self.outbox, self.committer,
                               gateway_call, app, self.installation_for_repo, self.check_runs,
                               self.committer.transcript_for, clock=clock, log=log)

    # -- lookups -------------------------------------------------------------------
    def installation(self, installation_id: int):
        inst = self._installations.get(installation_id)
        if inst is None:
            inst = self._installations[installation_id] = self.app.installation(installation_id)
        return inst

    def installation_for_repo(self, repo: str):
        """The repo's installation: the poll's answer, else a running job's."""
        inst_id = self.scheduler.installation_of.get(repo)
        if inst_id is None:
            inst_id = next((j.installation_id for j in self.jobs.all() if j.repo == repo), None)
        if inst_id is None:
            raise GitHubError(404, f"no installation is known for {repo}")
        return self.installation(inst_id)

    def deploy(self):
        """The deployment config in the records store, re-read when `config apply` changes it."""
        row = self.records.conn().execute(
            "SELECT body, applied_at FROM deploy_config WHERE id = 1").fetchone()
        if row is None:
            self._deploy, self._deploy_stamp = None, None
            return None
        if row[1] != self._deploy_stamp:
            try:
                self._deploy = load_deploy(bytes(row[0]))
            except DeployError as exc:
                # config apply validates before it writes, so this is a store
                # written by another version: keep serving the last good one.
                self.log(f"WARN: the stored deployment config does not load: {exc}")
                return self._deploy
            self._deploy_stamp = row[1]
            self._rescan(self._deploy)
            self.outbox.comment_bytes = self._deploy.comment_bytes
            self.outbox.comments_per_pass = self._deploy.comments_per_pass
        return self._deploy

    def _rescan(self, deploy) -> None:
        names = {p.credential for p in deploy.profiles.values()} | {"VOYAGE_API_KEY"}
        scanner = Scanner({n: v for n, v in self._candidates.items() if n in names})
        for name in scanner.skipped:
            self.log(f"WARN: secret {name} is shorter than the scan's minimum; findings are not "
                     f"checked for it")
        for name in sorted(n for n in names - set(self._candidates) if n != "VOYAGE_API_KEY"):
            self.log(f"WARN: the deployment names credential {name}, which the platform "
                     f"environment does not hold; its profile's tokens will be refused")
        self.outbox.scanner = scanner

    def _not_configured(self, reason: str) -> None:
        if self.state != NOT_CONFIGURED or self.reason != reason:
            self.log(f"not configured: {reason}")
        self.state, self.reason = NOT_CONFIGURED, reason

    def backoff_for(self, profile: str) -> int:
        deploy = self._deploy
        if deploy is not None and profile in deploy.profiles:
            return deploy.profiles[profile].limit_backoff_seconds
        return 1800

    def hello_tokens(self) -> list:
        """The live provider tokens, each with its job's route and its committed counts."""
        out = []
        for row in self.creds.live_provider_rows():
            job = self.jobs.get(row["job_id"])
            if job is None:
                self.creds.revoke(row["credential_id"])   # no job to serve it: it dies here
                continue
            record = commit_mod.record_for(self.records, row["credential_id"])
            out.append(dict(row, model=job.model, route=job.route,
                            counts=record.counts() if record else {}))
        return out

    # -- the lock and startup ---------------------------------------------------
    def take_lock(self) -> bool:
        if not self.lock.try_acquire():
            return False
        self.startup()
        return True

    def startup(self) -> None:
        revoked = self.creds.revoke_all()
        # The jobs table is the third record of the previous instance's work,
        # beside the credentials and the workers, and this instance's registry
        # never holds those jobs, so nothing else would ever end them.
        with self.records.tx() as c:
            closed = c.execute("UPDATE jobs SET ended_at = ?, end_reason = ? "
                               "WHERE ended_at IS NULL",
                               (int(self.clock()), "the control plane restarted")).rowcount
        try:
            stopped = self.runner.stop_all()
        except (RunnerError, OSError, gwproto.ProtocolError) as exc:
            # Every credential is already revoked, so an orphan that keeps
            # running can neither reach the channel nor spend at the gateway.
            # A runnerd not yet listening raises OSError, not RunnerError.
            stopped = 0
            self.log(f"WARN: could not stop orphaned workers: {exc}")
        deleted = self.blobs.delete_prefix(blobs_mod.transcripts_prefix())
        try:
            self.gateway_call("hello", tokens=[])
        except (OSError, gwproto.ProtocolError) as exc:
            self.log(f"WARN: the gateway is not answering yet ({exc}); it gets its hello on "
                     f"its first reply")
        self.log(f"holding the lock: revoked {revoked} credential(s), closed {closed} job(s), "
                 f"stopped {stopped} orphaned worker(s), deleted {deleted} transcript(s)")
        if self.deploy() is not None:
            self.state, self.reason = SERVING, ""
        else:
            self._not_configured(NO_DEPLOY)

    # -- the gateway's reports -------------------------------------------------------
    def handle_report(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        if self.state == WAITING:
            return {"error": "not holding the lock"}   # the gateway retries
        if msg.get("op") != "report":
            return {"error": f"unknown op {msg.get('op')!r}"}
        return commit_mod.apply_report(self.records, msg)

    # -- the loops -------------------------------------------------------------------
    def poll_once(self) -> int:
        """One poll of every installation. Returns how many jobs it dispatched."""
        deploy = self.deploy()
        if deploy is None:
            if self.state != DRAINING:
                self._not_configured(NO_DEPLOY)
            return 0
        if self.state == NOT_CONFIGURED:
            self.state, self.reason = SERVING, ""
        self.scheduler.reap()
        if self.state != SERVING:
            return 0
        try:
            installs = self.app.installations()
        except GitHubError as exc:
            self.log(f"WARN: could not list installations: {exc}")
            return 0
        live = {int(i["id"]) for i in installs if not i.get("suspended_at")}
        lost: Dict[int, List[str]] = {}
        for repo, inst_id in list(self.scheduler.installation_of.items()):
            if inst_id not in live:
                lost.setdefault(inst_id, []).append(repo)
        for inst_id, repos in sorted(lost.items()):
            # The spec pages this, and with no webhook receiver in 2a the poll
            # is its only producer (PR #18 review). Once per installation.
            self.log(f"PAGE: installation {inst_id} was uninstalled or suspended; "
                     f"{len(repos)} repo(s) stop being reviewed")
            for repo in repos:
                self.scheduler.lose_repo(repo, "the App was uninstalled or suspended")
        for inst_id in [i for i in self._drainers if i not in live]:
            del self._drainers[inst_id]
        dispatched = 0
        for inst_id in sorted(live):
            if inst_id not in self._drainers:
                self._drainers[inst_id] = Drainer(self.outbox, self.installation(inst_id), inst_id,
                                                  clock=self.clock)
            dispatched += self.scheduler.poll(inst_id)
        self.scheduler.sweep_orphans(live)
        return dispatched

    def _poll_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                self.poll_once()
            except Exception as exc:   # a poll that raises must not end the loop
                self.log(f"WARN: poll failed: {type(exc).__name__}: {exc}")
            deploy = self._deploy
            self.stopping.wait(deploy.poll_seconds if deploy is not None else 30)

    def _drain_loop(self) -> None:
        while not self.stopping.is_set():
            wait = 5.0
            for drainer in list(self._drainers.values()):
                try:
                    wait = min(wait, drainer.step())
                except Exception as exc:
                    self.log(f"WARN: outbox drain failed: {type(exc).__name__}: {exc}")
            self.stopping.wait(max(wait, 0.05))

    def healthy(self) -> bool:
        return all(t.is_alive() for t in self.threads.values())

    def start_loops(self) -> None:
        for name, target in (("poll", self._poll_loop), ("drain", self._drain_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self.threads[name] = t

    # -- the drain on SIGTERM -----------------------------------------------------
    def drain(self, deadline_seconds: int = DRAIN_SECONDS) -> int:
        """Stop dispatching, let running jobs finish, end the rest. Returns how many were ended."""
        self.state = DRAINING
        until = self.clock() + deadline_seconds
        # Halt the walk, then wait for one already past its checks to finish,
        # so every job it dispatched is in `jobs` before they are counted.
        self.scheduler.halted.set()
        if self.scheduler.walk.acquire(timeout=max(deadline_seconds, 0)):
            self.scheduler.walk.release()
        while self.jobs.all() and self.clock() < until:
            self.scheduler.reap()
            time.sleep(1)
        left = self.jobs.all()
        for job in left:
            self.scheduler.end_job(job, "the control plane is stopping")
        self.stopping.set()
        return len(left)


class _Status:
    """What /healthz reports: "waiting for lock" or "not configured" before a
    ControlPlane exists, and the ControlPlane's own state once it does."""

    def __init__(self):
        self.current: Optional[ControlPlane] = None
        self._state = WAITING
        self._reason = ""

    @property
    def reason(self) -> str:
        return self.current.reason if self.current is not None else self._reason

    @property
    def state(self) -> str:
        return self.current.state if self.current is not None else self._state

    @state.setter
    def state(self, value: str) -> None:
        self._state = value

    def healthy(self) -> bool:
        return self.current.healthy() if self.current is not None else True


def health_server(control, host: str, port: int) -> http.server.ThreadingHTTPServer:
    class Health(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path.split("?", 1)[0] != "/healthz":
                self.send_response(404)
                self.end_headers()
                return
            ok = control.state == WAITING or control.healthy()
            reason = getattr(control, "reason", "")
            body = ((control.state + (f": {reason}" if reason else "")) if ok
                    else "a loop has died").encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer((host, port), Health)
    server.daemon_threads = True
    return server


def pem_from_env(value: str) -> bytes:
    """The App key from one environment line.

    `docker run --env-file` holds one line per variable, so a PEM arrives with
    its newlines written as the two characters `\\n`. Only that pair is
    translated, and only in a value that has no real newline.
    """
    if "\n" not in value:
        value = value.replace("\\n", "\n")
    return value.encode()


def keys_from_env(env: Mapping[str, str], crypto_mod, app_cls,
                  log: Callable[[str], None] = lambda line: print(line, flush=True)):
    """(sealer, app) from the deployment key and the App key, or None for each one malformed.

    A key that is present but malformed is the same unfinished deployment as
    a missing one: named, and not configured, never a crash loop. The App is
    built only when both keys are good, since blobs need the sealer.
    """
    sealer = signer = None
    try:
        sealer = crypto_mod.AesGcmSealer(crypto_mod.decode_key(env["CLAUDEBOX_DEPLOYMENT_KEY"]))
    except crypto_mod.CryptoError as exc:
        log(f"CLAUDEBOX_DEPLOYMENT_KEY: {exc}")
    try:
        signer = crypto_mod.Rs256Signer(pem_from_env(env["GITHUB_APP_PRIVATE_KEY"]))
    except crypto_mod.CryptoError as exc:
        log(f"GITHUB_APP_PRIVATE_KEY: {exc}")
    app = app_cls(env["GITHUB_APP_ID"], signer) if sealer is not None and signer is not None else None
    return sealer, app


def _listen(value: str, default_port: int):
    host, _, port = value.rpartition(":")
    return host or "0.0.0.0", int(port or default_port)


def main(env: Mapping[str, str] = os.environ) -> None:
    """Run the control plane. Its environment is the supervisor's, and only its own secrets."""
    import creds as creds_mod
    import crypto
    from github_app import App
    from records import InstanceLock, Records
    from runner import RunnerClient

    # Blocked before any thread starts, so every thread inherits the mask and
    # the main thread's sigwait is the one place a stop is handled.
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
    data = env.get("CLAUDEBOX_DATA", "/data")
    status = _Status()
    health = health_server(status, *_listen(env.get("HEALTH_LISTEN", "0.0.0.0:8081"), 8081))
    threading.Thread(target=health.serve_forever, daemon=True).start()
    app_id, key = env.get("GITHUB_APP_ID"), env.get("GITHUB_APP_PRIVATE_KEY")
    deployment_key = env.get("CLAUDEBOX_DEPLOYMENT_KEY")
    # Not configured: /healthz answers and nothing else happens (the spec's
    # "Bringing a deployment up"). Nothing is decoded until all three exist,
    # and the records store is not touched.
    sealer, app = (keys_from_env(env, crypto, App) if app_id and key and deployment_key
                   else (None, None))
    if app is None:
        status.state, status._reason = NOT_CONFIGURED, NO_KEYS
        print(f"not configured: {NO_KEYS}", flush=True)
        signal.sigwait({signal.SIGTERM, signal.SIGINT})
        return
    # Overview decision 13: the lock is taken before the records store is opened.
    lock = InstanceLock(os.path.join(data, LOCK_FILE))
    while not lock.try_acquire():
        time.sleep(LOCK_RETRY_SECONDS)
    records = Records(os.path.join(data, "records.db"))
    blob_store = blobs_mod.Blobs(os.path.join(data, "blobs"), sealer)
    creds = creds_mod.Creds(records)
    runner = RunnerClient(gwproto.Client(env["RUNNER_SOCKET"], env["RUNNER_SECRET"]))
    control: Optional[ControlPlane] = None
    link = GatewayLink(env["GATEWAY_SOCKET"], env["GATEWAY_SECRET"],
                       lambda: control.hello_tokens() if control else [])
    control = ControlPlane(records, lock, creds, blob_store, app, runner, link, env,
                           env.get("PERSONA_DIR", "/opt/claudebox/personas"),
                           env["CLAUDEBOX_CHANNEL_URL"], env["CLAUDEBOX_GATEWAY_URL"])
    control.take_lock()   # held already: this runs the startup
    status.current = control
    reports = gwproto.Server(env["CONTROL_SOCKET"], env["GATEWAY_SECRET"],
                             control.handle_report).start()
    channel = make_channel_server(control.channel, *_listen(env.get("CHANNEL_LISTEN",
                                                                    "0.0.0.0:8080"), 8080))
    threading.Thread(target=channel.serve_forever, daemon=True).start()
    control.start_loops()
    signal.sigwait({signal.SIGTERM, signal.SIGINT})
    ended = control.drain(int(env.get("CLAUDEBOX_DRAIN_SECONDS", DRAIN_SECONDS)))
    control.log(f"stopped; ended {ended} running job(s) as revoked")
    channel.shutdown()
    reports.stop()
    lock.release()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_control.py'`, then `./test-python.sh`
Expected: PASS, 27 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/control.py tests/test_hosted_control.py
git commit -m "hosted: the control-plane process, its lock, its loops and /healthz"
```

### Task 4: The supervisor

**Files:**
- Create: `hosted/supervisor.py`
- Test: `tests/test_hosted_supervisor.py`

**Interfaces:**
- Consumes: `hosted/runnerd.py` (2a.3), `hosted/gateway.py` (2a.2) and Task 3's `control.py`, each started as a script.
- Produces:
  - `CONTROL_SECRETS`, `SETTINGS`, `PROCESS_VARS`, `RUN_DIR = "/run/claudebox"`, `DATA_DIR = "/data"`, `GRACE_SECONDS = 15`.
  - `provider_secrets(env) -> dict`, `split_env(env, gateway_secret, runner_secret) -> {"control", "gateway", "runnerd": env}`, `Spec(name, argv, user, extra_groups=())`, `specs(docker_gid) -> List[Spec]`, `docker_gid(path) -> Optional[int]`.
  - `SUPERVISOR_UID = SUPERVISOR_GID = 9004`, `CAP_KILL = 5`, `RESERVED`, `CONTROL_SETTINGS`; `drop_privileges(uid=9004, gid=9004, osmod=os, caps=None)`; `Supervisor(specs, envs, drain_seconds, spawn=..., clock=time.monotonic, sleep=time.sleep, log=print, after_start=lambda: None)` with `start()`, `exited() -> Optional[str]`, `stop()`, `run() -> int` and the attribute `stop_requested`; `main(env) -> int`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_supervisor.py`:

```python
import _path  # noqa: F401

import os
import signal
import tempfile
import unittest
import unittest.mock

from supervisor import (CAP_KILL, Spec, Supervisor, docker_gid, drop_privileges, main,
                        provider_secrets, specs, split_env)

PLATFORM = {
    "GITHUB_APP_ID": "123", "GITHUB_APP_PRIVATE_KEY": "-----BEGIN KEY-----",
    "GITHUB_WEBHOOK_SECRET": "whs", "CLAUDEBOX_DEPLOYMENT_KEY": "dk",
    "OLLAMA_API_KEY": "olk-secret", "ANTHROPIC_API_KEY": "sk-ant-x", "VOYAGE_API_KEY": "vk",
    "WORKER_IMAGE": "claudebox-worker:1", "PATH": "/x", "HOSTNAME": "h", "LC_ALL": "C",
    "lowercase": "nope", "CLAUDEBOX_CHANNEL_URL": "http://cb:8080",
    "CLAUDEBOX_CREDENTIALS": "OLLAMA_API_KEY, ANTHROPIC_API_KEY,VOYAGE_API_KEY",
    # What platforms inject. Credential-shaped, and never a provider credential.
    "FLY_API_TOKEN": "fly-token", "FLY_REGION": "iad", "K_SERVICE": "svc",
    "DATABASE_URL": "postgres://u:p@db/x", "KUBERNETES_SERVICE_HOST": "10.0.0.1",
}


class EnvSplitTest(unittest.TestCase):
    def setUp(self):
        self.envs = split_env(PLATFORM, "gws", "rns")

    def test_the_gateway_cannot_read_the_app_key_or_the_deployment_key(self):
        # The spec's whole point of the split.
        gw = self.envs["gateway"]
        for name in ("GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET", "CLAUDEBOX_DEPLOYMENT_KEY",
                     "GITHUB_APP_ID", "RUNNER_SECRET"):
            self.assertNotIn(name, gw)
        self.assertEqual((gw["OLLAMA_API_KEY"], gw["VOYAGE_API_KEY"]), ("olk-secret", "vk"))

    def test_the_control_plane_gets_its_secrets_and_the_providers_only_as_scan_literals(self):
        c = self.envs["control"]
        self.assertEqual(c["GITHUB_APP_PRIVATE_KEY"], "-----BEGIN KEY-----")
        self.assertNotIn("OLLAMA_API_KEY", c)
        self.assertEqual(c["CLAUDEBOX_SCAN_OLLAMA_API_KEY"], "olk-secret")
        self.assertEqual(c["CLAUDEBOX_CHANNEL_URL"], "http://cb:8080")

    def test_runnerd_gets_no_secret_but_its_socket_secret(self):
        r = self.envs["runnerd"]
        self.assertEqual(r["RUNNER_SECRET"], "rns")
        self.assertEqual(r["WORKER_IMAGE"], "claudebox-worker:1")
        values = set(r.values())
        for secret in ("-----BEGIN KEY-----", "olk-secret", "dk", "gws"):
            self.assertNotIn(secret, values)

    def test_nothing_from_the_platform_crosses_unless_named(self):
        for env in self.envs.values():
            self.assertNotIn("HOSTNAME", env)
            self.assertNotIn("LC_ALL", env)
            self.assertNotIn("lowercase", env)
            self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin:/bin")

    def test_the_control_planes_settings_reach_it_and_are_never_filed_as_credentials(self):
        # PR #22 review: these were read by control.main and never forwarded.
        env = dict(PLATFORM, PERSONA_DIR="/personas", HEALTH_LISTEN="0.0.0.0:9090",
                   CHANNEL_LISTEN="0.0.0.0:9080", CLAUDEBOX_DATA="/elsewhere")
        envs = split_env(env, "g", "r")
        self.assertEqual((envs["control"]["PERSONA_DIR"], envs["control"]["HEALTH_LISTEN"],
                          envs["control"]["CHANNEL_LISTEN"]), ("/personas", "0.0.0.0:9090",
                                                               "0.0.0.0:9080"))
        self.assertEqual(envs["control"]["CLAUDEBOX_DATA"], "/data")
        for name in ("PERSONA_DIR", "HEALTH_LISTEN", "CHANNEL_LISTEN", "CLAUDEBOX_DATA"):
            self.assertNotIn(name, envs["gateway"])
            self.assertNotIn("CLAUDEBOX_SCAN_" + name, envs["control"])

    def test_the_platform_cannot_set_what_the_supervisor_generates(self):
        # PR #22 review: these crossed as "credentials" and overrode the gateway's.
        env = dict(PLATFORM, GATEWAY_SECRET="pinned", CONTROL_SOCKET="/elsewhere",
                   GATEWAY_LISTEN="0.0.0.0:1", RUNNER_SECRET="x")
        envs = split_env(env, "gws", "rns")
        self.assertEqual(envs["gateway"]["GATEWAY_SECRET"], "gws")
        self.assertEqual(envs["gateway"]["CONTROL_SOCKET"], envs["control"]["CONTROL_SOCKET"])
        self.assertEqual(envs["gateway"]["GATEWAY_LISTEN"], "0.0.0.0:8090")
        self.assertNotIn("RUNNER_SECRET", envs["gateway"])
        self.assertNotIn("CLAUDEBOX_SCAN_GATEWAY_SECRET", envs["control"])

    def test_provider_secrets_are_exactly_the_named_ones(self):
        self.assertEqual(set(provider_secrets(PLATFORM)),
                         {"OLLAMA_API_KEY", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY"})

    def test_a_platforms_own_variables_reach_no_process(self):
        # PR #22 review: a shape rule handed the gateway, which listens on the
        # worker network, whatever the platform injected.
        for env in self.envs.values():
            for name in ("FLY_API_TOKEN", "FLY_REGION", "K_SERVICE", "DATABASE_URL",
                         "KUBERNETES_SERVICE_HOST", "CLAUDEBOX_CREDENTIALS"):
                self.assertNotIn(name, env)
                self.assertNotIn("CLAUDEBOX_SCAN_" + name, env)

    def test_with_no_list_the_gateway_holds_no_credential(self):
        env = {k: v for k, v in PLATFORM.items() if k != "CLAUDEBOX_CREDENTIALS"}
        self.assertEqual(provider_secrets(env), {})

    def test_the_list_cannot_name_a_control_secret_or_a_supervisor_value(self):
        for name in ("GITHUB_APP_PRIVATE_KEY", "CLAUDEBOX_DEPLOYMENT_KEY", "GATEWAY_SECRET",
                     "WORKER_IMAGE", "PATH"):
            with self.subTest(name=name):
                lines = []
                env = dict(PLATFORM, CLAUDEBOX_CREDENTIALS=f"OLLAMA_API_KEY,{name}")
                self.assertEqual(main(env, log=lines.append), 2)
                self.assertIn(name, lines[0])
                with self.assertRaises(ValueError):
                    split_env(env, "g", "r")

    def test_a_named_credential_the_platform_lacks_is_warned_about(self):
        lines = []
        env = dict(PLATFORM, CLAUDEBOX_CREDENTIALS="OLLAMA_API_KEY,MISSING_KEY")
        with unittest.mock.patch("supervisor.Supervisor.run", return_value=0), \
                unittest.mock.patch("supervisor.docker_gid", return_value=999), \
                unittest.mock.patch("signal.signal"):
            self.assertEqual(main(env, log=lines.append), 0)
        self.assertEqual(lines, ["WARN: CLAUDEBOX_CREDENTIALS names MISSING_KEY, which the "
                                 "platform environment does not hold"])

    def test_the_gateway_and_control_share_the_gateway_secret_and_nothing_else_does(self):
        self.assertEqual(self.envs["control"]["GATEWAY_SECRET"], "gws")
        self.assertEqual(self.envs["gateway"]["GATEWAY_SECRET"], "gws")
        self.assertNotIn("GATEWAY_SECRET", self.envs["runnerd"])


class SpecTest(unittest.TestCase):
    def test_each_process_has_its_own_uid_and_only_runnerd_joins_docker(self):
        by_name = {s.name: s for s in specs(999)}
        self.assertEqual({s.user for s in by_name.values()}, {"runner", "gateway", "control"})
        self.assertEqual(tuple(by_name["runnerd"].extra_groups), (999,))
        self.assertEqual(tuple(by_name["gateway"].extra_groups), ())
        self.assertEqual([s.name for s in specs(None)][-1], "control")

    def test_docker_gid_reads_the_socket_and_none_without_one(self):
        with tempfile.NamedTemporaryFile() as f:
            self.assertEqual(docker_gid(f.name), os.stat(f.name).st_gid)
        self.assertIsNone(docker_gid("/nonexistent/docker.sock"))


class DropTest(unittest.TestCase):
    def test_the_supervisor_leaves_root_keeping_only_kill(self):
        # PR #22 review: root in the container that mounts the Docker socket.
        calls = []

        class Os:
            def setgroups(self, g):
                calls.append(("setgroups", g))

            def setgid(self, g):
                calls.append(("setgid", g))

            def setuid(self, u):
                calls.append(("setuid", u))

        class Caps:
            def keepcaps(self, on):
                calls.append(("keepcaps", on))

            def set_only(self, cap):
                calls.append(("caps", cap))
        drop_privileges(9004, 9004, Os(), Caps())
        self.assertEqual(calls, [("setgroups", []), ("keepcaps", True), ("setgid", 9004),
                                 ("setuid", 9004), ("caps", CAP_KILL), ("keepcaps", False)])

    def test_the_drop_runs_after_the_children_start(self):
        order = []
        sup = Supervisor([Spec("control", ("c",), "control")], {"control": {}}, 0,
                         spawn=lambda spec, env: order.append("spawn") or FakeProc("c", []),
                         log=lambda line: None, after_start=lambda: order.append("drop"))
        sup.stop_requested = True
        sup.run()
        self.assertEqual(order[:2], ["spawn", "drop"])


class FakeProc:
    def __init__(self, name, log):
        self.name, self.log, self.rc = name, log, None
        self.exits_on = {signal.SIGTERM, signal.SIGKILL}

    def poll(self):
        return self.rc

    def send_signal(self, sig):
        self.log.append((self.name, sig))
        if sig in self.exits_on:
            self.rc = -sig


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.signals, self.now = [], [0.0]
        self.procs = {}

        def spawn(spec, env):
            self.procs[spec.name] = FakeProc(spec.name, self.signals)
            return self.procs[spec.name]
        self.sup = Supervisor([Spec("runnerd", ("r",), "runner"), Spec("gateway", ("g",), "gateway"),
                               Spec("control", ("c",), "control")],
                              {"runnerd": {}, "gateway": {}, "control": {}}, 600, spawn=spawn,
                              clock=lambda: self.now[0], sleep=self.tick, log=lambda line: None)

    def tick(self, seconds):
        self.now[0] += seconds

    def test_a_stop_drains_the_control_plane_before_the_gateway_stops(self):
        self.sup.stop_requested = True
        self.assertEqual(self.sup.run(), 0)
        self.assertEqual(self.signals[0], ("control", signal.SIGTERM))
        self.assertEqual(set(self.signals[1:]), {("runnerd", signal.SIGTERM),
                                                 ("gateway", signal.SIGTERM)})

    def test_the_control_plane_gets_its_whole_drain_deadline(self):
        self.sup.start()
        self.procs["control"].exits_on = {signal.SIGKILL}   # draining, not yet gone
        self.sup.stop()
        self.assertGreaterEqual(self.now[0], 600)
        self.assertIn(("control", signal.SIGKILL), self.signals)

    def test_one_process_exiting_stops_the_rest_and_the_container(self):
        self.sup.start()
        self.procs["gateway"].rc = 1
        self.sup.start = lambda: None
        self.assertEqual(self.sup.run(), 1)
        self.assertIn(("control", signal.SIGTERM), self.signals)
        self.assertNotIn(("gateway", signal.SIGTERM), self.signals)

    def test_a_child_exiting_pages(self):
        logs = []
        self.sup.log = logs.append
        self.sup.start()
        self.procs["runnerd"].rc = 1
        self.sup.start = lambda: None
        self.sup.run()
        self.assertTrue(any(l.startswith("PAGE: runnerd exited") for l in logs))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_supervisor.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'supervisor'`.

- [ ] **Step 3: Implement**

`hosted/supervisor.py`:

```python
"""PID 1 of the control-plane container: three processes, three uids, split secrets.

The spec's "Secrets": the supervisor reads the platform's secrets from its
environment and starts each process as its own uid with only its own. The
control plane gets the App key, the webhook secret and the deployment key.
The gateway gets the provider credentials. runnerd, the one process holding
the Docker socket, gets no secret but the runner socket's. The control plane
also gets each provider credential as a scan literal (`CLAUDEBOX_SCAN_<NAME>`),
because the scanner needs every literal and runs there.

A provider credential is a variable `CLAUDEBOX_CREDENTIALS` names, and
nothing else: the deployment file, which also names them, lives in the
records store, which only the control plane can read, so the operator's
setup file repeats the list for the supervisor. A platform's own variables
(`FLY_API_TOKEN`, `DATABASE_URL`, `K_SERVICE`) reach no process unless that
list names them, and the list may not name a control-plane secret or
anything the supervisor sets itself.

On SIGTERM the control plane drains first (it stops dispatching and lets
running jobs finish up to its deadline), then the gateway and runnerd stop.
Any process exiting on its own stops the other two and the container, for
the platform's restart policy to bring back.
"""

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Optional, Sequence

CONTROL_SECRETS = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET",
                   "CLAUDEBOX_DEPLOYMENT_KEY")
# The supervisor's own settings, and the control plane's that it forwards.
# CLAUDEBOX_DATA is listed so it is never filed as a credential; the volume's
# path is fixed by the image, so its value is not forwarded.
CONTROL_SETTINGS = ("PERSONA_DIR", "CHANNEL_LISTEN", "HEALTH_LISTEN")
CREDENTIALS_VAR = "CLAUDEBOX_CREDENTIALS"
SETTINGS = (CREDENTIALS_VAR, "CLAUDEBOX_CHANNEL_URL", "CLAUDEBOX_GATEWAY_URL", "CLAUDEBOX_DRAIN_SECONDS",
            "WORKER_IMAGE", "WORKER_NETWORK", "DOCKER_SOCKET", "CLAUDEBOX_DATA") + CONTROL_SETTINGS
PROCESS_VARS = frozenset({"PATH", "HOME", "HOSTNAME", "TERM", "LANG", "PWD", "SHLVL", "_",
                          "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "TZ"})
RUN_DIR = "/run/claudebox"
# Generated or fixed by the supervisor for its children; never a credential,
# and never taken from the platform environment.
RESERVED = frozenset({"GATEWAY_SECRET", "RUNNER_SECRET", "GATEWAY_SOCKET", "CONTROL_SOCKET",
                      "RUNNER_SOCKET", "GATEWAY_LISTEN", "PYTHONPATH", "CLAUDEBOX_DATA"})
DATA_DIR = "/data"
HOSTED = "/opt/claudebox/hosted"
GRACE_SECONDS = 15
# The supervisor's own identity once its children run: its own uid and group,
# in no other group, so it can open neither the sockets nor the Docker socket.
SUPERVISOR_UID = SUPERVISOR_GID = 9004
CAP_KILL = 5
_PR_SET_KEEPCAPS = 8
_CAP_V3 = 0x20080522


class _LinuxCaps:
    """prctl and capset, the two calls the drop needs that `os` does not have."""

    def __init__(self):
        import ctypes
        self.ctypes = ctypes
        self.libc = ctypes.CDLL(None, use_errno=True)

    def keepcaps(self, on: bool) -> None:
        if self.libc.prctl(_PR_SET_KEEPCAPS, 1 if on else 0, 0, 0, 0) != 0:
            raise OSError(self.ctypes.get_errno(), "prctl(PR_SET_KEEPCAPS) failed")

    def set_only(self, cap: int) -> None:
        ct = self.ctypes

        class Header(ct.Structure):
            _fields_ = [("version", ct.c_uint32), ("pid", ct.c_int)]

        class Data(ct.Structure):
            _fields_ = [("effective", ct.c_uint32), ("permitted", ct.c_uint32),
                        ("inheritable", ct.c_uint32)]
        data = (Data * 2)()
        data[0].effective = data[0].permitted = 1 << cap
        if self.libc.capset(ct.byref(Header(_CAP_V3, 0)), data) != 0:
            raise OSError(ct.get_errno(), "capset failed")


def drop_privileges(uid: int = SUPERVISOR_UID, gid: int = SUPERVISOR_GID, osmod=os,
                    caps=None) -> None:
    """Leave root, keeping only CAP_KILL, which signals the children under their uids.

    The spec's "The supervisor then drops to an unprivileged uid itself". Run
    after the children start, since starting them needs SETUID and SETGID. With
    KEEPCAPS the permitted set survives setuid; capset then narrows it to KILL,
    so the supervisor can never become root again, and as uid 9004 in no group
    it can open neither the process sockets nor the Docker socket that shares
    its container.
    """
    caps = caps if caps is not None else _LinuxCaps()
    osmod.setgroups([])
    caps.keepcaps(True)
    osmod.setgid(gid)
    osmod.setuid(uid)
    caps.set_only(CAP_KILL)
    caps.keepcaps(False)


def credential_names(env: Mapping[str, str]) -> List[str]:
    """The names `CLAUDEBOX_CREDENTIALS` lists. Raises ValueError for a name
    that would hand the gateway a control-plane secret or a supervisor value."""
    names = [n.strip() for n in env.get(CREDENTIALS_VAR, "").split(",") if n.strip()]
    forbidden = sorted(set(names) & (set(CONTROL_SECRETS) | set(SETTINGS) | PROCESS_VARS
                                     | RESERVED))
    if forbidden:
        raise ValueError(f"{CREDENTIALS_VAR} names {', '.join(forbidden)}, which is not a "
                         f"provider credential")
    return names


def provider_secrets(env: Mapping[str, str]) -> Dict[str, str]:
    return {n: env[n] for n in credential_names(env) if env.get(n)}


def split_env(env: Mapping[str, str], gateway_secret: str, runner_secret: str) -> Dict[str, Dict[str, str]]:
    """Each process's whole environment. Nothing else crosses."""
    base = {"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": f"/opt/claudebox/reviewer:{HOSTED}"}
    # One directory per socket, owned by the process that binds it: a peer in
    # the shared group can connect but can neither unlink nor replace it.
    sockets = {"GATEWAY_SOCKET": f"{RUN_DIR}/gateway/gateway.sock",
               "CONTROL_SOCKET": f"{RUN_DIR}/control/control.sock",
               "RUNNER_SOCKET": f"{RUN_DIR}/runner/runner.sock"}
    providers = provider_secrets(env)
    drain = env.get("CLAUDEBOX_DRAIN_SECONDS", "600")
    control = dict(base, HOME="/home/control", CLAUDEBOX_DATA=DATA_DIR,
                   GATEWAY_SECRET=gateway_secret, RUNNER_SECRET=runner_secret,
                   CLAUDEBOX_CHANNEL_URL=env.get("CLAUDEBOX_CHANNEL_URL",
                                                 "http://claudebox:8080"),
                   CLAUDEBOX_GATEWAY_URL=env.get("CLAUDEBOX_GATEWAY_URL",
                                                 "http://claudebox:8090"),
                   CLAUDEBOX_DRAIN_SECONDS=drain, **sockets)
    control.update({k: env[k] for k in CONTROL_SECRETS + CONTROL_SETTINGS if env.get(k)})
    control.update({f"CLAUDEBOX_SCAN_{k}": v for k, v in providers.items() if v})
    gateway = dict(providers)
    gateway.update(base, HOME="/home/gateway", GATEWAY_SECRET=gateway_secret,
                   GATEWAY_SOCKET=sockets["GATEWAY_SOCKET"], CONTROL_SOCKET=sockets["CONTROL_SOCKET"],
                   GATEWAY_LISTEN="0.0.0.0:8090")
    runner = dict(base, HOME="/home/runner", RUNNER_SECRET=runner_secret,
                  RUNNER_SOCKET=sockets["RUNNER_SOCKET"],
                  WORKER_IMAGE=env.get("WORKER_IMAGE", "claudebox-worker"),
                  WORKER_NETWORK=env.get("WORKER_NETWORK", "claudebox"))
    return {"control": control, "gateway": gateway, "runnerd": runner}


@dataclass(frozen=True)
class Spec:
    name: str
    argv: Sequence[str]
    user: str
    extra_groups: Sequence[int] = ()


def specs(docker_gid: Optional[int]) -> List[Spec]:
    """Start order: runnerd and the gateway first, so the control plane finds both."""
    py = sys.executable or "/usr/bin/python3"
    return [Spec("runnerd", (py, f"{HOSTED}/runnerd.py"), "runner",
                 (docker_gid,) if docker_gid is not None else ()),
            Spec("gateway", (py, f"{HOSTED}/gateway.py"), "gateway"),
            Spec("control", (py, f"{HOSTED}/control.py"), "control")]


def docker_gid(path: str) -> Optional[int]:
    """The Docker socket's group, which runnerd joins; None when there is no socket."""
    try:
        return os.stat(path).st_gid
    except OSError:
        return None


def _spawn(spec: Spec, env: Dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(list(spec.argv), env=env, user=spec.user, group=spec.user,
                            extra_groups=["claudebox", *spec.extra_groups], close_fds=True)


class Supervisor:
    def __init__(self, specs: Sequence[Spec], envs: Dict[str, Dict[str, str]],
                 drain_seconds: int, spawn: Callable[[Spec, Dict[str, str]], object] = _spawn,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda line: print(line, flush=True),
                 after_start: Callable[[], None] = lambda: None):
        self.after_start = after_start
        self.specs, self.envs, self.drain_seconds = list(specs), envs, drain_seconds
        self.spawn, self.clock, self.sleep, self.log = spawn, clock, sleep, log
        self.procs: Dict[str, object] = {}
        self.stop_requested = False

    def start(self) -> None:
        for spec in self.specs:
            self.procs[spec.name] = self.spawn(spec, self.envs[spec.name])
            self.log(f"started {spec.name} as {spec.user}")

    def exited(self) -> Optional[str]:
        return next((n for n, p in self.procs.items() if p.poll() is not None), None)

    def _wait(self, names: Sequence[str], seconds: float) -> None:
        until = self.clock() + seconds
        while self.clock() < until and any(self.procs[n].poll() is None for n in names):
            self.sleep(0.2)

    def _signal(self, names: Sequence[str], sig: int) -> None:
        for n in names:
            p = self.procs[n]
            if p.poll() is None:
                p.send_signal(sig)

    def stop(self) -> None:
        """The control plane drains first; then the gateway and runnerd."""
        self._signal(["control"], signal.SIGTERM)
        self._wait(["control"], self.drain_seconds + GRACE_SECONDS)
        others = [n for n in self.procs if n != "control"]
        self._signal(others, signal.SIGTERM)
        self._wait(others, GRACE_SECONDS)
        self._signal(list(self.procs), signal.SIGKILL)

    def run(self) -> int:
        self.start()
        self.after_start()
        while True:
            if self.stop_requested:
                self.stop()
                return 0
            gone = self.exited()
            if gone is not None:
                self.log(f"PAGE: {gone} exited with status {self.procs[gone].poll()}; stopping "
                         f"the rest")
                self.stop()
                return 1
            self.sleep(0.5)


def main(env: Mapping[str, str] = os.environ, log: Callable[[str], None] = print) -> int:
    try:
        names = credential_names(env)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 2
    if not names:
        log(f"WARN: {CREDENTIALS_VAR} is not set, so the gateway holds no provider credentials")
    for name in names:
        if not env.get(name):
            log(f"WARN: {CREDENTIALS_VAR} names {name}, which the platform environment does "
                f"not hold")
    envs = split_env(env, os.urandom(32).hex(), os.urandom(32).hex())
    sup = Supervisor(specs(docker_gid(env.get("DOCKER_SOCKET", "/var/run/docker.sock"))), envs,
                     int(env.get("CLAUDEBOX_DRAIN_SECONDS", "600")), after_start=drop_privileges)

    def request_stop(*_):
        sup.stop_requested = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_supervisor.py'`, then `./test-python.sh`
Expected: PASS, 20 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/supervisor.py tests/test_hosted_supervisor.py
git commit -m "hosted: the supervisor, three uids and split secrets"
```

### Task 5: The image and the compose file

**Files:**
- Create: `hosted/Dockerfile`, `hosted/compose.json`
- Modify: `.dockerignore`, `.gitignore`
- Test: `tests/test_hosted_image.py`

**Interfaces:**
- Consumes: Tasks 3 and 4.
- Produces:
  - The `claudebox-control` image, built from the repo root with `docker build -f hosted/Dockerfile .`, whose entrypoint is the supervisor.
  - `hosted/compose.json`, which runs it with `hosted/claudebox.env` (written by 2a.5's setup, mode 600, ignored by the build context) and builds the worker image under the `build` profile.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_image.py`:

```python
import _path  # noqa: F401

import json
import os
import re
import unittest

import control
import supervisor

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


class ComposeTest(unittest.TestCase):
    def setUp(self):
        self.svc = json.loads(read("hosted/compose.json"))["services"]["claudebox"]

    def test_the_supervisor_gets_exactly_the_capabilities_it_uses(self):
        # SETUID and SETGID start each process as its own uid and groups; KILL
        # signals processes under other uids. Nothing else.
        self.assertEqual(self.svc["cap_drop"], ["ALL"])
        self.assertEqual(sorted(self.svc["cap_add"]), ["KILL", "SETGID", "SETUID"])
        self.assertIn("no-new-privileges:true", self.svc["security_opt"])

    def test_the_grace_period_covers_the_whole_ordered_stop(self):
        # The control plane's drain plus its grace, then the other two's grace,
        # all before the platform's SIGKILL (PR #22 review).
        m = re.fullmatch(r"(\d+)m", self.svc["stop_grace_period"])
        self.assertGreater(int(m.group(1)) * 60,
                           control.DRAIN_SECONDS + 2 * supervisor.GRACE_SECONDS)

    def test_a_crash_loop_stops_rather_than_re_reviewing_every_minute(self):
        # Every restart re-reviews every open PR (overview decision 1), so the
        # restarts are bounded (PR #22 review).
        self.assertRegex(self.svc["restart"], r"^on-failure:\d+$")

    def test_data_is_a_volume_and_runnerd_has_the_docker_socket(self):
        self.assertIn("claudebox-data:/data", self.svc["volumes"])
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", self.svc["volumes"])
        self.assertEqual(supervisor.DATA_DIR, "/data")

    def test_only_health_is_published_and_only_on_loopback(self):
        self.assertEqual(self.svc["ports"], ["127.0.0.1:8081:8081"])

    def test_workers_reach_the_control_plane_by_the_urls_it_hands_them(self):
        env = self.svc["environment"]
        self.assertEqual(env["CLAUDEBOX_CHANNEL_URL"], "http://claudebox:8080")
        self.assertEqual(env["CLAUDEBOX_GATEWAY_URL"], "http://claudebox:8090")
        self.assertEqual(env["WORKER_NETWORK"], "claudebox")
        self.assertIn("claudebox", self.svc["networks"])


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        self.text = read("hosted/Dockerfile")

    def pairs(self):
        return [p.split(":") for p in re.search(r"for pair in ([a-z0-9: ]+); do",
                                                 self.text).group(1).split()]

    def test_it_creates_every_user_the_supervisor_starts_with_a_pinned_id(self):
        # /data is a volume that keeps its owner across rebuilds (PR #22 review).
        self.assertEqual(sorted(u for u, _ in self.pairs()),
                         sorted(s.user for s in supervisor.specs(None)))
        self.assertTrue(all(i.isdigit() for _, i in self.pairs()))
        self.assertIn("groupadd --gid 9000 claudebox", self.text)
        self.assertIn("useradd --uid $id --gid $id", self.text)

    def test_each_socket_has_a_directory_only_its_binder_can_write(self):
        # A peer in the shared group could otherwise unlink and replace it (PR #22 review).
        self.assertIn("install -d -o $u -g claudebox -m 2750 /run/claudebox/$u", self.text)
        self.assertNotIn("2770", self.text)
        envs = supervisor.split_env({}, "g", "r")
        owner = {"GATEWAY_SOCKET": "gateway", "CONTROL_SOCKET": "control", "RUNNER_SOCKET": "runner"}
        for name, user in owner.items():
            self.assertEqual(os.path.dirname(envs["control"].get(name) or envs["gateway"].get(name)
                                             or envs["runnerd"][name]),
                             f"{supervisor.RUN_DIR}/{user}")
        self.assertIn("install -d -o control -g control -m 0700 /data", self.text)

    def test_the_code_is_root_owned(self):
        for line in re.findall(r"^COPY (?!--from).*$", self.text, re.M):
            self.assertNotIn("--chown", line)

    def test_the_crypto_tests_run_and_a_skip_fails_the_build(self):
        self.assertIn("test_hosted_crypto.py", self.text)
        self.assertIn("! grep -q 'skipped'", self.text)
        # A failing test must fail the build: no pipe may stand between the test
        # run and the layer's status (PR #22 review).
        run = self.text[self.text.index("python3 -m unittest"):self.text.index("! grep")]
        self.assertNotIn("|", run.replace("||", ""))
        self.assertIn("|| { cat /tmp/crypto.out; exit 1; }", run)

    def test_the_build_context_keeps_the_files_the_build_runs(self):
        ignore = read(".dockerignore")
        self.assertIn("!tests/_path.py", ignore)
        self.assertIn("!tests/test_hosted_crypto.py", ignore)
        self.assertIn("hosted/claudebox.env", ignore)

    def test_the_secrets_file_is_never_committed(self):
        self.assertIn("hosted/claudebox.env", read(".gitignore").splitlines())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_image.py'`
Expected: FAIL: `FileNotFoundError` for `hosted/compose.json`.

- [ ] **Step 3: Implement**

`hosted/Dockerfile`:

```dockerfile
# The control-plane image: the supervisor, the control plane, the gateway and
# runnerd, each its own uid (the spec's "Secrets"). Built from the repo root:
#   docker build -f hosted/Dockerfile -t claudebox-control .
# The worker image is the repo's own Dockerfile.
FROM debian:bookworm-slim

# python3-cryptography is crypto.py's one library (overview decision 4). The
# Docker CLI is runnerd's, copied from the official image rather than pulling
# Debian's docker.io and the daemon it depends on.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-cryptography ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=docker:27.3.1-cli /usr/local/bin/docker /usr/local/bin/docker

# One uid per process, and a shared group for the sockets between them.
# The ids are pinned, because /data is a volume that keeps its owner across
# rebuilds: an id picked by useradd would move with the base image.
# Each socket gets its own directory, owned by the process that binds it and
# setgid to the shared group, so a peer can connect and cannot unlink it.
RUN groupadd --gid 9004 supervisor \
 && useradd --uid 9004 --gid 9004 --no-create-home --shell /usr/sbin/nologin supervisor \
 && groupadd --gid 9000 claudebox \
 && for pair in control:9001 gateway:9002 runner:9003; do \
      u=${pair%%:*}; id=${pair##*:}; \
      groupadd --gid $id $u; \
      useradd --uid $id --gid $id --groups claudebox --create-home --home-dir /home/$u \
              --shell /usr/sbin/nologin $u; \
      install -d -o $u -g claudebox -m 2750 /run/claudebox/$u; \
    done \
 && install -d -o control -g control -m 0700 /data

# Root-owned, like the worker's code: no process here may rewrite it.
COPY reviewer/ /opt/claudebox/reviewer/
COPY hosted/ /opt/claudebox/hosted/
COPY personas/ /opt/claudebox/personas/

# crypto.py's tests skip on a host without cryptography; here they run, so a
# build against a cryptography that changed under it fails rather than ships.
# No pipe: a pipeline's status is its last command's, which would hide a
# failing test. A failure prints the output, and a skip fails the build too.
COPY tests/_path.py tests/test_hosted_crypto.py /tmp/build-tests/tests/
RUN ln -s /opt/claudebox/reviewer /opt/claudebox/hosted /tmp/build-tests/ \
 && cd /tmp/build-tests \
 && { python3 -m unittest discover -s tests -p 'test_hosted_crypto.py' > /tmp/crypto.out 2>&1 \
      || { cat /tmp/crypto.out; exit 1; }; } \
 && ! grep -q 'skipped' /tmp/crypto.out \
 && rm -rf /tmp/build-tests /tmp/crypto.out

ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080 8081 8090
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=4)"
# The supervisor starts as root, with only SETUID, SETGID and KILL (compose.json),
# starts each process under its own uid, then drops to uid 9004 keeping KILL alone.
ENTRYPOINT ["python3", "/opt/claudebox/hosted/supervisor.py"]
```

`hosted/compose.json`:

```json
{
  "name": "claudebox",
  "services": {
    "claudebox": {
      "build": {"context": "..", "dockerfile": "hosted/Dockerfile"},
      "image": "claudebox-control",
      "hostname": "claudebox",
      "env_file": ["claudebox.env"],
      "environment": {
        "WORKER_IMAGE": "claudebox-worker",
        "WORKER_NETWORK": "claudebox",
        "CLAUDEBOX_CHANNEL_URL": "http://claudebox:8080",
        "CLAUDEBOX_GATEWAY_URL": "http://claudebox:8090"
      },
      "networks": {"claudebox": {"aliases": ["claudebox"]}},
      "ports": ["127.0.0.1:8081:8081"],
      "volumes": ["claudebox-data:/data", "/var/run/docker.sock:/var/run/docker.sock"],
      "cap_drop": ["ALL"],
      "cap_add": ["SETUID", "SETGID", "KILL"],
      "security_opt": ["no-new-privileges:true"],
      "pids_limit": 512,
      "mem_limit": "2g",
      "stop_grace_period": "11m",
      "restart": "on-failure:3"
    },
    "worker": {
      "build": {"context": "..", "dockerfile": "Dockerfile"},
      "image": "claudebox-worker",
      "profiles": ["build"]
    }
  },
  "networks": {"claudebox": {"name": "claudebox"}},
  "volumes": {"claudebox-data": {}}
}
```

Apply this change to `.dockerignore` and `.gitignore`:

```diff
diff --git a/.dockerignore b/.dockerignore
index 077abe2..d53f170 100644
--- a/.dockerignore
+++ b/.dockerignore
@@ -10,7 +10,12 @@ test-personas.sh
 test-shim.sh
 test-python.sh
 tests/
+# Except what hosted/Dockerfile runs at build time.
+!tests/_path.py
+!tests/test_hosted_crypto.py
 tools/
+# The control plane's secrets, written by setup beside hosted/compose.json.
+hosted/claudebox.env
 # Host-built bytecode, for a Python that need not match the image's. The `**/`
 # is load-bearing: docker matches ignore patterns against the whole relative
 # path and `*` does not cross a `/`, so a bare `__pycache__/` would exclude only
diff --git a/.gitignore b/.gitignore
index 27d9749..92f24bb 100644
--- a/.gitignore
+++ b/.gitignore
@@ -1,4 +1,6 @@
 .env
+# The control plane's secrets, written by setup beside hosted/compose.json.
+hosted/claudebox.env
 .env.*
 !.env.example
 __pycache__/
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_image.py'`, then `./test-python.sh`
Expected: PASS, 12 tests. The whole suite passes.

- [ ] **Step 5: Build the image and run it**

The Validator runs these on a host with Docker and records the output as evidence. Each command is followed by what it must show.

```bash
docker build -f hosted/Dockerfile -t claudebox-control-test .
```

The build passes, and its crypto step reports `OK` with no `skipped`.

```bash
docker run -d --name cbx-smoke --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add KILL \
  --security-opt no-new-privileges --pids-limit 512 -p 127.0.0.1:18081:8081 claudebox-control-test
sleep 4; curl -s -w ' %{http_code}\n' http://127.0.0.1:18081/healthz
```

A body starting `not configured:` and the status `200`, because there are no App credentials and no deployment key.

```bash
docker exec cbx-smoke sh -c 'for p in /proc/[0-9]*; do printf "%s uid=%s caps=%s nnp=%s\n" \
  "$(tr "\0" " " < $p/cmdline | cut -c1-50)" "$(awk "/^Uid/{print \$2}" $p/status)" \
  "$(awk "/^CapEff/{print \$2}" $p/status)" "$(awk "/^NoNewPrivs/{print \$2}" $p/status)"; done'
```

The supervisor runs as uid 9004, in no group, with `caps=0000000000000020` (`KILL` alone). `runnerd.py`, `gateway.py` and `control.py` each run under a different non-zero uid with `caps=0000000000000000`. Every process shows `nnp=1`.

```bash
docker stop -t 30 cbx-smoke; docker inspect cbx-smoke --format '{{.State.ExitCode}}'; docker rm cbx-smoke
```

The stop takes about a second, and the exit code is `0`.

```bash
touch hosted/claudebox.env && docker compose -f hosted/compose.json --profile build config --services
```

`claudebox` and `worker`. Run from `hosted/` without `-f`, the same command says `no configuration file provided: not found`, which is why every compose command names the file. Remove the empty `hosted/claudebox.env` if this step created it.

Then the configured path. Make an env file holding a throwaway RSA key in one line, with `GITHUB_APP_ID=1`, `CLAUDEBOX_DEPLOYMENT_KEY` from `crypto.new_key()`, `OLLAMA_API_KEY=olk-smoke-test-value` and `CLAUDEBOX_CREDENTIALS=OLLAMA_API_KEY`. Run the container on its own network, with a named volume at `/data` and the Docker socket mounted. The log says `holding the lock: revoked 0 credential(s), closed 0 job(s), stopped 0 orphaned worker(s), deleted 0 transcript(s)`, and `/healthz` says `not configured`. Insert a `deploy_config` row as the `control` user (with `timing.poll_seconds: 10`). Within a poll, `/healthz` says `serving`, and the log shows `WARN: could not list installations: GitHub 404: Integration not found`, which shows a JWT signed by the image's `cryptography` reaching GitHub. Start a second container on the same volume. Its `/healthz` says `waiting for lock`. Stop the first, and the second's log says `holding the lock` and its `/healthz` says `serving`. As the `gateway` user, `rm /run/claudebox/control/control.sock` fails with `Permission denied`. `/data` and `control.lock` are owned by uid 9001. Remove both containers, the volume, the network and the env file.

- [ ] **Step 6: Commit**

```bash
git add hosted/Dockerfile hosted/compose.json .dockerignore .gitignore tests/test_hosted_image.py
git commit -m "hosted: the control-plane image and its compose file"
```

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md` (a paragraph in "Hosted App libraries", after the 2a.4a paragraph)
- Modify: `HISTORY.md` (one entry at the top of "Unreleased")

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: nothing code depends on.

- [ ] **Step 1: Describe the processes**

In `CLAUDE.md`'s "Hosted App libraries" subsection, after the paragraph about the control plane's decisions, add:

```markdown
Phase 2a.4b runs them. `scheduler.py` is the poll: per installation and repo it reads the config from the default branch (the repo file over the org `.github` repo's), checks it against the deployment, gives every non-fork head a check run, holds what the change gate and the parks hold, and dispatches one job per PR within `max_concurrent_jobs` and each profile's `concurrency`, counted in pairs in flight. It ends jobs (the worker gone, the 55-minute deadline, an unused bootstrap) and handles loss of access, which calls the three deleters 2a.4a provides. `control.py` is the control-plane process. Until it holds the records store's lock it answers `/healthz` as "waiting for lock" and does nothing else. Holding it, it revokes every credential, stops orphaned workers, deletes every transcript and sends the gateway an empty `hello`, then runs the poll, the outbox drain, the gateway's report socket and the worker channel, and on `SIGTERM` drains running jobs up to `CLAUDEBOX_DRAIN_SECONDS`. `supervisor.py` is PID 1 of `hosted/Dockerfile`'s image: it starts runnerd, the gateway and the control plane as three uids with only their own secrets (provider credentials, which are exactly the variables `CLAUDEBOX_CREDENTIALS` names, go to the gateway, and to the control plane only as scan literals), and, once its three children are running, drops to uid 9004 keeping `CAP_KILL` alone, so it can still signal their uids during the drain and can never be root again. `hosted/compose.json` runs it for the local target.
```

- [ ] **Step 2: Add the history entry**

At the top of "Unreleased" in `HISTORY.md`, add:

```markdown
* **Phase 2a.4b of the hosted GitHub App: the control plane runs.** A container image whose supervisor starts the control plane, the gateway and the job runner as separate users, each holding only its own secrets. The control plane polls each installation, reviews every open PR from a repo whose config allows it (never a fork's), shows a check run on every head, and ends jobs that time out or lose their worker. Only one instance runs at a time, and a stop lets running reviews finish first. A repo-config label with a tab or newline in it is now an error, and an empty persona list names the key to fix.
```

- [ ] **Step 3: Run every suite**

Run: `./test-python.sh && ./test-providers.sh && ./test-personas.sh && ./test-shim.sh && /bin/bash ./test-launcher.sh && /bin/bash ./test-worker.sh`
Expected: all six pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md HISTORY.md
git commit -m "docs: hosted phase 2a.4b, the control plane's processes"
```
