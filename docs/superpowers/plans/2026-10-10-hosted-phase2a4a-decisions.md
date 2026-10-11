# Hosted Phase 2a.4a: The Control Plane's Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a factory run: the Validator derives a Coder brief (no test code) and a Tester brief (no implementation code) from it, and neither lane opens this file.

**Goal:** The control plane's decision libraries: the hosted round line and prompts, the per-PR check run and its wording, the outbox with its acceptance caps and paced drain, the commit point that turns a gateway record into an outcome and applies it, and the worker channel's four HTTP calls. 2a.4b wires them into running processes.

**Architecture:** Five modules under `hosted/` and one change under `reviewer/`, each testable with fakes. `prompts.round_stanza` and `Decider.system_prompt_for` take the head a pair last reviewed, so a hosted round line names it, and `prompts.build_hosted` gives the hosted defaults. `checkruns.py` holds the check-run wording and one run per head. `outbox.py` accepts findings synchronously (withheld, too long, enough, or queued) and drains them per installation, paced and round-robin across PRs. `commit.py` commits gateway reports to token rows, decides each completed pair's outcome in the spec's order, and applies the outcome's row of the spec's table to the repo's `Decider` and to the parks it keeps. `channel.py` is the worker channel: fetch-job, start-pair, post-finding and complete-pair, over HTTP.

**Tech Stack:** Python 3.9+ standard library (`sqlite3`, `http.server`, `threading`, `hashlib`, `json`, `unittest`).

**Spec:** `docs/superpowers/specs/2026-10-09-hosted-github-app-design.md`, sections "The worker channel", "The `gh` shim" (the acceptance answers), "Posting and the outbox", "The per-PR check run", "Visibility", "State" and "The commit point", and "The gateway" (what counts as a provider failure, the profile parks). The 2a decisions this plan binds are in `docs/superpowers/plans/2026-10-10-hosted-phase2a-overview.md`. The ones it needs are copied under Global Constraints.

**Prerequisite:** 2a.1 (PR #18), 2a.2 (PR #19) and 2a.3 (PR #20) are built. This plan imports `records`, `creds`, `blobs`, `scan` and `github_app` (2a.1), speaks to the gateway in 2a.2's protocol through an injected call, and serves 2a.3's worker its job spec.

## Global Constraints

- **Standard library only**, and **Python 3.9 syntax** (overview decision 15). Flat modules, imported by plain name (overview decision 19). Nothing here runs a process: 2a.4b does.
- **Overview decision 1:** pair state stays in memory in 2a. The control plane holds one `MemoryStateStore` and one `Decider` per repo, and this plan's parks and check-run pair states live in memory beside them. Token rows, check-run counts, outbox items and withheld records are the records store's.
- **Overview decision 2:** 2a's outcomes are `ok`, `capped`, `usage-limited`, `deferred`, `failing`, `failed` and `revoked`. `usage_missing` is `capped`. A rejected-only record (`first_rejected`, no success, no failure) is `deferred`, with the transcript discarded and the session dropped (`commit.rejected_only`): a 4xx cannot say whether claudebox's request or an overgrown session was refused, so it is a claudebox fault, and resuming a refused session would be refused again. `first_abandoned` takes the place of `ok` and nothing else.
- **Overview decision 16:** `prompts.round_stanza(mode, round, since_head=None)` names the commits after `since_head` when it is given, and keeps today's wording otherwise. `Decider.system_prompt_for(pair, since_head=None, rewritten=False)` passes it through, so local mode's prompts stay byte-identical, which `tests/fixtures/` pins. A rewritten branch runs as round 1 at the `should-fix` floor, and the round count still advances.
- **Overview decision 17:** hosted prompts are today's defaults with no Linear and no kindex stanza, plus one sentence in `GH_STANZA`: "If the deployment refuses a read, stop the review and say so; do not guess at the diff."
- **Overview decision 18:** the check-run wording is decided here.
- The spec's commit order: "a transcript that will not parse, or a session id that is not the pair's, is `failing`, checked first so a bad session id is never committed; otherwise an incomplete flush or an empty record is `deferred`; otherwise a record of upstream failures with no success is `failed`; otherwise the record decides (`ok`, `capped`, `usage-limited`, `budget`)". "An `ok` additionally needs the job's capability to be live".
- The spec's outcome table, for 2a's rows: `ok` commits the transcript and the fingerprint, adds a round and clears owed; `capped` and `failing` park until the head moves and page; `usage-limited`, `deferred` and `failed` park for the profile's `limit_backoff_seconds`; `failing`, `failed` and `revoked` discard the transcript and drop the session.
- The spec's profile rules: "Pairs on two different PRs hitting a limit inside that window park the whole profile". "Three consecutive failed pairs on a profile, with no success between them, park the profile", and "any success on the profile resets every pair's failure count". "A repo whose pairs are deferred three times in a row pages".
- The spec's outbox: keyed by "(job, pair, sequence number)", "paced at no more than one comment a second and 400 an hour", "round-robin across PRs", "Only `Retry-After` and 5xx answers are retried", dropped as undeliverable on "the PR closed, the App lost access to the repo, the repo went public, or GitHub answered with an error that retrying cannot fix (404, 410, 422, a locked conversation, a 403 that is not a rate limit)". Before each post: visibility "against GitHub, uncached", a second scan, and the `-claudebox` signature. A finding whose head the PR has moved past "still posts, with a first line naming the commit it was written against".
- The spec's acceptance answers: **queued** (exit 0, print "queued"), **withheld** ("contained a credential-shaped string, was not posted, and should be restated without the literal value"), **too long**, and **enough** ("stop posting and finish its review"), with "the findings it turns away ... counted on the check run ... as not posted".
- The spec's check run: one per head, created only for a head with none recorded; "The conclusion is never `failure`, and `success` only ever means 'reviewed and found nothing'"; it reports "findings dropped alongside findings posted".
- The overview's channel: `fetch-job` (bootstrap, `200 {capability, job}`, `401` once used or expired), `start-pair` (`200 {provider_token}` or `409 {reason}`), `post-finding` (`200 {result: "queued"}` or `422 {result, message}`), `complete-pair` (`200 {}`). Request bodies are capped at 1 MiB, and `complete-pair` at 96 MiB. A decoded transcript is capped at 64 MiB.
- The overview's records schema v1, unchanged: this plan adds rows, never columns.
- Every existing suite still passes, and local mode's prompts are byte-identical (`tests/fixtures/`).

## Decided here

The spec and the overview leave these open, and each one changes code. They are this plan's answers, for the founder to ratify in Phase 1 of the factory run:

1. **2a.4 is two plans.** 2a.4a is the decision libraries, testable with fakes. 2a.4b is the processes: the scheduler, the control-plane main, the supervisor, `hosted/Dockerfile` and `hosted/compose.json`. Each is one factory run. The overview's split table says so.
2. **A hosted round line from round 2 on reads "Read closely only the commits after `<head>`: the ones `git log --oneline <head>..HEAD` lists."** (plan mode: "the revisions after commit `<head>`", keeping its last sentence). `since_head` must be a full commit id. A rewritten branch gets round 1's line whatever the round, and the summary of the check run says the branch was rewritten.
3. **`build_hosted` is `build({})` with `HOSTED_GH_SENTENCE` after every copy of `GH_STANZA`.** It raises if a default ever loses `GH_STANZA`, so the sentence can never silently go missing.
4. **The check-run states in 2a** are queued, running, reviewed, deferred, usage-limited, failed, failing, capped, round-cap, revoked and config-invalid. Budget, too-large and refused-read arrive with 2b. When a head's pairs differ, the title names the state that waits on a person before one that clears itself, before one that is done. The wording is the table in `checkruns._WORDS`, with a retry time in UTC where one exists. The summary has one line per persona and the counts: posted, waiting to post, dropped, withheld, and turned away by the per-pass limit. A config problem stops the review and is titled "Not reviewed: configuration", with one sentence true of every cause (an invalid file, a setting the deployment cannot serve) and the problem text below it. A config notice (one file broken and the other applied, the spec's fallback) shows below a review that runs and makes its conclusion `neutral`. `CheckRuns.config_answer` and `CheckRuns.view` let 2a.4b publish a head only when its answer changes, and paint a pair no job will ever set.
5. **A head's conclusion is `success` only when every pair is reviewed (or at its round cap) and nothing was found**, counting findings queued, posted, dropped, withheld and turned away. Everything else that is complete is `neutral`.
6. **Acceptance runs scan, then size, then the per-pass count.** A credential is withheld whatever else is wrong with the finding. Size is measured in UTF-8 bytes against `caps.comment_bytes`. A too-long finding is not counted against the pass, since the model is told to shorten it and post again. A finding past `caps.comments_per_pass` is counted on the check run as not posted. A retried `post-finding` with the same key answers what the first one did, and queues once. The key's result is kept in memory for the job's life.
7. **A withheld record stores a fresh 16-byte salt followed by `sha256(salt || body)`**, and the body with every match replaced by `[withheld:<detector>]`. The day's count per detector is a `withheld_counts` row.
8. **The drain is one `Drainer` per installation, and `step()` posts at most one item** and returns how long to wait. Pacing is a sliding window: one second between posts and 400 in any hour. A retryable failure with no `Retry-After` waits 30 seconds, doubling per attempt up to 30 minutes. Every drop is an `audit` row (`outbox.drop`, the key and the reason) and a check-run count. Whether a repo is public is `github_app.is_public`, which reads GitHub's three-valued `visibility` field, so an internal repo counts as private (the spec's "Visibility") whatever `private` says for one. `private` is read only when `visibility` is absent, and a repo that says neither counts as public.
9. **`Installation.pull(full_name, pr)` and `is_public(repo)` join 2a.1's client**, returning None on a 404, because the drain must see whether a PR is open before it posts.
10. **A pass with no transcript and an empty record is `deferred`**, ahead of the spec's transcript check. Nothing parsed badly: the pass never started `claude`. A missing transcript with real traffic is `failing`. A transcript parses when every non-empty line is a JSON object and its first `sessionId` is a plausible id. `decide` never returns `revoked`: the spec decides it "elsewhere, without a `complete pair`", and that is decision 18. A dead capability never reaches `decide`, because the channel answers it `401`.
11. **Reports commit only in order.** A report whose seq is not the committed one plus one changes nothing, and the ack names the committed seq, so a resend is harmless and a gap is visible. A `first_*` marker is never overwritten, and its text is cut at 500 characters. A report for a token the control plane never minted is refused.
12. **The commit point is in memory, under one lock, and never calls `decider_for` while holding it.** The decider is resolved first, because 2a.4b's `decider_for` takes the scheduler's lock and the scheduler calls into the committer; a call under the lock would deadlock the two (2a.4b's review). The transcript blob is written first and the pair pointed at it second, so a crash between them leaves an unreferenced blob for 2b's sweep, and 2a.4b's startup deletes every transcript blob anyway (overview decision 1). A job does not outlive the control plane that started it because 2a.4b's startup makes it so: 2a.1's rule, "the successor revokes every outstanding row before it serves anything", revokes every capability and provider token (`Creds.revoke_all`), the restarted `hello` carries no live token so the gateway drops them all, and the runner's orphans are stopped. An in-flight pass is lost on a restart, and its pair stays owed. A newer transcript deletes the blob it replaces.
13. **Parks live beside the pair state, keyed by repo.** The profile's two-PR limit rule counts `(repo, pr)`, since a profile spans repos and every repo has a #1. A head park lifts when the PR's head differs. A timed park lifts at its time. A profile park stops `start-pair`, and its scheduling consequence is 2a.4b's. A page is one `PAGE:` line (overview decision 14).
14. **`fetch-job` mints the read token and starts the 55-minute deadline in the same call**, so the token outlives the job, and a read token GitHub refuses revokes the new capability at once. A pair's committed transcript travels in the job spec with its session id, and a pair without one gets neither.
15. **`start-pair` fails closed, and reserves the pair before any network call.** A visibility check that errors, a repo that is public or out of reach, a parked profile, a pair already started or starting, and a gateway that refuses the token all answer `409`. The check and the reservation happen under the job's lock, so two concurrent starts for one persona mint one token, and a refusal clears the reservation so the pair stays startable. The provider token lives until the job's deadline, and its `token_records` row exists before the gateway hears of it.
16. **`complete-pair` records the pair completed as soon as the commit point returns**, with its outcome, in the same critical section that ends its reservation, and answers a resend `200 {}`, the way `post-finding` answers one. After the record, the pair's check-run view is set (in memory, and published), and only then is the token revoked, so a fault in the revocation leaves the run showing the true outcome and the token for the job's end to revoke. A fault before the commit point leaves the pair started, which a retry re-enters. A concurrent duplicate is `409`. A resend runs the settle step again from the recorded outcome, since every part of it is idempotent, so a fault in that step the first time cannot leave the run showing the pair as running. `job.completed` is the one record that a pair is done. A fault inside `apply` itself, before the record, is re-entered and may spend its round twice: the window is accepted, and closing it needs a durable commit, which 2b's SQL state brings. A completion whose job is no longer registered (`job_live`, checked under the commit point's lock) commits nothing and answers `revoked`: the job's end already revoked and showed the pair, and a loss of access may already have deleted what it would write back (2a.4b's review). It turns a bad transcript into one that will not parse. That covers bad base64, a transcript over 64 MiB, and the worker's own `over-cap` note, so all three are `failing`. A flush the gateway cannot answer is no flush, so the pair is `deferred`. The pair's token is revoked in the records store and then at the gateway, and a gateway that misses the revoke drops the token at its next `hello`.
17. **Body caps and the credential are checked before reading.** A missing length is `411`, and a body over the cap is `413`. The token is in a header, so a request with no live credential for its call (a bootstrap for `fetch-job`, a capability with a registered job for the rest), or to no known call, is answered `401` or `404` without reading its body: the bodies run to 96 MiB, and the worker network is the untrusted side. A body that does not parse, a deeply nested one included, is `400`. An oversized `complete-pair` is refused unread, so its pair is revoked when the job ends (the overview, as revised).
18. **`revoked` is `Committer.revoke`.** A started pair whose job ended without `complete-pair` (the deadline, an access change, a restart) has its transcript discarded and its failure recorded, and stays owed. `revoke` returns `REVOKED` for the job-end handler, which knows the head, to show as `checkruns.REVOKED`. 2a.4b's job end is that handler.
19. **Loss of access has two deleters, both here.** `Committer.forget_repo` deletes the repo's transcripts and parks and calls `StateStore.forget`, which drops sessions, fingerprints, pass counts, owed pairs and the cut, and keeps rounds, since they hold no content and survive a public flip, as the spec says. The policy lives in the store, beside the fields it names, so a field added later is forgotten or kept on purpose. `Outbox.drop_repo` deletes the repo's queued items and its `withheld` rows in one transaction, and keeps the per-detector day counts. 2a.4b's loss-of-access path calls both, plus `CheckRuns.forget_repo`, and revokes the repo's jobs.
20. **The head a round line names is the committed fingerprint's**, read by `Committer.last_reviewed_head` from `state.reviewed`. There is no second record of it to drift.
21. **A count moves in the transaction that settles its item**, so a crash cannot lose one. That covers acceptance's withheld row as well as the drain. The drain decides an item's fate inside its `try` and settles it after, so a check-run failure is never read as the comment's, and a failed publish waits for the next one. A head this process holds no pair state for is not repainted: after a restart the outbox still posts its findings, and an empty map would turn a finished run back to queued. Pair states sit under a lock, since a group's personas finish together, and a head's run is created under a second lock, so personas reaching a new head together make one run.

## Review Focus

- **A worker claiming an outcome.** Expected: the worker's notes never decide; the record does. Pinned in Task 5 (a limited record commits `usage-limited` whatever the notes say) and Task 4.
- **A hangup armed to turn a cap into a retry.** Expected: a record with `cap_hit`, `usage_missing` or a limit decides that outcome even with `first_abandoned`. Pinned in Task 4.
- **A finding retried after a timeout.** Expected: queued once, answered the same both times. Pinned in Task 3.
- **A credential in a finding, at acceptance or slipped in before posting.** Expected: withheld, recorded redacted, counted, never posted. Pinned in Task 3.
- **One PR's backlog starving another.** Expected: round-robin. Pinned in Task 3.
- **A repo that goes public or out of reach mid-job.** Expected: `start-pair` refuses, and the outbox drops its items before posting. Pinned in Tasks 3 and 5.
- **A worker racing two `start-pair`s for one persona.** Expected: one provider token. Pinned in Task 5.
- **A repo lost while it held withheld findings.** Expected: they are deleted with its queued items. Pinned in Task 3.
- **A gateway that loses reports, or never answers the flush.** Expected: `deferred`, never `ok`. Pinned in Tasks 4 and 5.

## Not run here

- **The check run against GitHub.** The tests use a fake installation. 2a.5's end-to-end run is the first real one.
- **The channel served to a real worker.** 2a.4b starts the server, and 2a.5 runs the first job.

---

### Task 1: The hosted round line and prompts

**Files:**
- Modify: `reviewer/prompts.py` (`round_stanza`, `HOSTED_GH_SENTENCE`, `build_hosted`)
- Modify: `reviewer/decisions.py` (`Decider.system_prompt_for`)
- Test: `tests/test_prompts.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `prompts.round_stanza(mode, round, since_head=None, rewritten=False) -> str`, raising `ValueError` for a `since_head` that is not a full commit id.
  - `prompts.HOSTED_GH_SENTENCE` and `prompts.build_hosted(shared_worktree_modes=frozenset()) -> Prompts`.
  - `Decider.system_prompt_for(pair, since_head=None, rewritten=False) -> str`.

- [ ] **Step 1: Write the failing tests**

Apply this change to `tests/test_prompts.py`:

```diff
diff --git a/tests/test_prompts.py b/tests/test_prompts.py
index f9eb0a7..5482e5a 100644
--- a/tests/test_prompts.py
+++ b/tests/test_prompts.py
@@ -238,6 +238,92 @@ class RenderTest(unittest.TestCase):
         self.assertEqual(prompts.render("{{PR}}", 7), "7")
 
 
+class HostedRoundStanzaTest(unittest.TestCase):
+    """Overview decision 16: the hosted round line names the head last reviewed."""
+
+    HEAD = "c" * 40
+
+    def test_without_a_head_every_line_is_unchanged(self):
+        for mode in ("code", "plan"):
+            for n in (1, 2, 3, 6):
+                with self.subTest(mode=mode, round=n):
+                    self.assertEqual(prompts.round_stanza(mode, n, since_head=None),
+                                     prompts.round_stanza(mode, n))
+
+    def test_a_head_replaces_the_signed_comment_search_from_round_two(self):
+        for mode in ("code", "plan"):
+            for n in (2, 5):
+                with self.subTest(mode=mode, round=n):
+                    s = prompts.round_stanza(mode, n, since_head=self.HEAD)
+                    self.assertIn(f"`git log --oneline {self.HEAD}..HEAD`", s)
+                    self.assertNotIn("signed with your label", s)
+                    self.assertIn(f"round {n} of your review", s)
+
+    def test_the_rest_of_the_rung_is_kept(self):
+        s = prompts.round_stanza("code", 3, since_head=self.HEAD)
+        self.assertIn("Post only `blocking` findings", s)
+        s = prompts.round_stanza("plan", 2, since_head=self.HEAD)
+        self.assertIn("Raise something in a part of the plan no revision touched", s)
+        self.assertIn("Post nothing tagged below `should-fix`", s)
+
+    def test_round_one_ignores_the_head(self):
+        self.assertEqual(prompts.round_stanza("code", 1, since_head=self.HEAD),
+                         prompts.round_stanza("code", 1))
+
+    def test_a_rewritten_branch_is_reviewed_whole_at_round_ones_floor(self):
+        for mode in ("code", "plan"):
+            with self.subTest(mode=mode):
+                self.assertEqual(prompts.round_stanza(mode, 4, since_head=self.HEAD, rewritten=True),
+                                 prompts.round_stanza(mode, 1))
+
+    def test_a_head_that_is_not_a_commit_id_is_refused(self):
+        for bad in ("main", "c" * 39, "C" * 40, "c" * 40 + "; rm -rf /"):
+            with self.subTest(bad=bad), self.assertRaises(ValueError):
+                prompts.round_stanza("code", 2, since_head=bad)
+
+
+class DeciderRoundLineTest(unittest.TestCase):
+    def test_the_decider_passes_the_hosted_arguments_through(self):
+        from common import Pair
+        from decisions import Decider
+        from state import MemoryStateStore
+        state = MemoryStateStore()
+        d = Decider(state, {"code": ["sage"]}, {("code", "sage"): "you are sage"}, {}, 0, 4)
+        pair = Pair(7, "code", "sage")
+        state.rounds[pair] = 2
+        head = "d" * 40
+        self.assertEqual(d.system_prompt_for(pair),
+                         "you are sage\n" + prompts.round_stanza("code", 3))
+        self.assertEqual(d.system_prompt_for(pair, since_head=head),
+                         "you are sage\n" + prompts.round_stanza("code", 3, since_head=head))
+        self.assertEqual(d.system_prompt_for(pair, since_head=head, rewritten=True),
+                         "you are sage\n" + prompts.round_stanza("code", 1))
+
+
+class HostedPromptsTest(unittest.TestCase):
+    def test_hosted_prompts_are_the_defaults_plus_one_gh_sentence(self):
+        local = prompts.build({})
+        hosted = prompts.build_hosted()
+        for kind in ("review", "followup"):
+            for mode in ("code", "plan"):
+                with self.subTest(kind=kind, mode=mode):
+                    h = getattr(hosted, kind)[mode]
+                    self.assertEqual(h.replace(" " + prompts.HOSTED_GH_SENTENCE, ""),
+                                     getattr(local, kind)[mode])
+                    self.assertEqual(h.count(prompts.HOSTED_GH_SENTENCE), 1)
+
+    def test_hosted_prompts_carry_no_linear_or_kindex_stanza(self):
+        hosted = prompts.build_hosted()
+        for text in list(hosted.review.values()) + list(hosted.followup.values()):
+            self.assertNotIn(prompts.KINDEX_STANZA.strip(), text)
+            self.assertNotIn("Linear", text)
+
+    def test_the_worktree_stanza_follows_the_shared_modes(self):
+        hosted = prompts.build_hosted(frozenset({"code"}))
+        self.assertTrue(hosted.review["code"].endswith(prompts.WORKTREE_STANZA))
+        self.assertNotIn(prompts.WORKTREE_STANZA, hosted.review["plan"])
+
+
 if __name__ == "__main__":
     unittest.main()
 
```

- [ ] **Step 2: Run them to see them fail**

Run: `./test-python.sh -p 'test_prompts.py'`
Expected: FAIL: `round_stanza() got an unexpected keyword argument 'since_head'`, and `module 'prompts' has no attribute 'build_hosted'`.

- [ ] **Step 3: Implement**

Apply this change to `reviewer/prompts.py` and `reviewer/decisions.py`:

```diff
diff --git a/reviewer/decisions.py b/reviewer/decisions.py
index c5f8a4d..f0b3cf4 100644
--- a/reviewer/decisions.py
+++ b/reviewer/decisions.py
@@ -76,14 +76,18 @@ class Decider:
         """The round the pair's next pass runs at."""
         return self.state.rounds.get(pair, 0) + 1
 
-    def system_prompt_for(self, pair: Pair) -> str:
+    def system_prompt_for(self, pair: Pair, since_head: Optional[str] = None,
+                          rewritten: bool = False) -> str:
         """Persona, then the round line. Composed per pass, since the round moves.
 
         This is the whole --append-system-prompt value. The task prompt is
         not touched, which keeps the verbatim-operator-prompt guarantee.
+        `since_head` and `rewritten` are the hosted control plane's, passed
+        straight to round_stanza; local mode passes neither.
         """
         base = self.persona_prompts[(pair.mode, pair.persona)]
-        line = prompts_mod.round_stanza(pair.mode, self.round_for(pair))
+        line = prompts_mod.round_stanza(pair.mode, self.round_for(pair),
+                                        since_head=since_head, rewritten=rewritten)
         return f"{base}\n{line}"
 
     def pairs_to_run(
diff --git a/reviewer/prompts.py b/reviewer/prompts.py
index c592e31..09277d3 100644
--- a/reviewer/prompts.py
+++ b/reviewer/prompts.py
@@ -12,8 +12,9 @@ The bare names (REVIEW_PROMPT, FOLLOWUP_PROMPT) mean code mode, so tuning the
 code prompt cannot silently change what a plan PR gets asked.
 """
 
+import re
 from dataclasses import dataclass
-from typing import Dict, FrozenSet, Mapping
+from typing import Dict, FrozenSet, Mapping, Optional
 
 # Extracted from entrypoint.sh by tools/extract-stanzas.py. See the long
 # comments there for why each exists; the short version:
@@ -161,16 +162,47 @@ _LADDERS = {
 }
 
 
-def round_stanza(mode: str, round: int) -> str:
-    """The round line for the system prompt."""
+_HEAD_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
+
+
+def _since_head_delta(mode: str, head: str) -> str:
+    """The delta instruction naming a head, in place of the signed-comment search.
+
+    Hosted mode posts through an outbox, so a persona's newest signed comment
+    can land after commits nobody has reviewed. The control plane knows the
+    head each pair last reviewed, so it names it instead.
+    """
+    if mode == "plan":
+        return (f"Read closely only the revisions after commit {head}: the commits "
+                f"`git log --oneline {head}..HEAD` lists. Raise something in a part of "
+                "the plan no revision touched only if a revision broke it.")
+    return (f"Read closely only the commits after {head}: the ones "
+            f"`git log --oneline {head}..HEAD` lists.")
+
+
+def round_stanza(mode: str, round: int, since_head: Optional[str] = None,
+                 rewritten: bool = False) -> str:
+    """The round line for the system prompt.
+
+    `since_head` and `rewritten` are hosted mode's (overview decision 16), and
+    local mode passes neither, so its lines are unchanged. `since_head` names
+    the head this pair last reviewed, and replaces the signed-comment search
+    from round 2 on. `rewritten` says that head is no longer an ancestor of
+    the current one: the pass reviews the whole PR at round 1's floor, though
+    the round count it is told about is still its own.
+    """
     if round < 1:
         raise ValueError(f"round must be >= 1, got {round}")
+    if since_head is not None and not _HEAD_RE.fullmatch(since_head):
+        raise ValueError(f"since_head must be a full commit id, got {since_head!r}")
     first, second, later = _LADDERS[mode]
-    if round == 1:
+    if round == 1 or rewritten:
         return first
-    if round == 2:
-        return second
-    return later.format(n=round, k=round - 1)
+    text = second if round == 2 else later.format(n=round, k=round - 1)
+    if since_head is not None:
+        delta = _PLAN_ROUND_DELTA if mode == "plan" else _ROUND_DELTA
+        text = text.replace(delta, _since_head_delta(mode, since_head))
+    return text
 
 
 @dataclass(frozen=True)
@@ -247,6 +279,32 @@ def build(
     return Prompts(review=review, followup=followup)
 
 
+# Hosted mode's one addition to GH_STANZA (overview decision 17). From 2b a
+# read goes through the deployment, which can refuse it, and a persona that
+# guesses at a diff it could not read posts findings about code it never saw.
+HOSTED_GH_SENTENCE = ("If the deployment refuses a read, stop the review and say so; "
+                      "do not guess at the diff.")
+
+
+def build_hosted(shared_worktree_modes: FrozenSet[str] = frozenset()) -> Prompts:
+    """Hosted mode's prompts: the defaults with no Linear or kindex stanza.
+
+    There are no operator overrides in hosted v1 (spec, "Caps"), so this is
+    build() with an empty environment plus HOSTED_GH_SENTENCE after every
+    copy of GH_STANZA.
+    """
+    base = build({}, shared_worktree_modes)
+    hosted = GH_STANZA + " " + HOSTED_GH_SENTENCE
+
+    def add(text: str) -> str:
+        if GH_STANZA not in text:
+            raise ValueError("a default prompt lost GH_STANZA")
+        return text.replace(GH_STANZA, hosted)
+
+    return Prompts(review={m: add(t) for m, t in base.review.items()},
+                   followup={m: add(t) for m, t in base.followup.items()})
+
+
 def render(template: str, pr: int) -> str:
     """Substitute the {{PR}} token. Overrides included, per entrypoint.sh:231-233."""
     return template.replace("{{PR}}", str(pr))
```

- [ ] **Step 4: Run them to see them pass, then the whole suite**

Run: `./test-python.sh -p 'test_prompts.py'`, then `./test-python.sh`
Expected: PASS, 55 tests. The whole suite passes. `tests/fixtures/` still matches, byte for byte.

- [ ] **Step 5: Commit**

```bash
git add reviewer/prompts.py reviewer/decisions.py tests/test_prompts.py
git commit -m "prompts: a hosted round line names the head last reviewed"
```

### Task 2: The check run

**Files:**
- Create: `hosted/checkruns.py`
- Test: `tests/test_hosted_checkruns.py`

**Interfaces:**
- Consumes: `records.Records` (2a.1); an installation with `create_check_run` and `update_check_run` (2a.1's `Installation`).
- Produces:
  - The state names `QUEUED`, `RUNNING`, `REVIEWED`, `DEFERRED`, `USAGE_LIMITED`, `FAILED`, `FAILING`, `CAPPED`, `ROUND_CAP`, `REVOKED`, `CONFIG_INVALID`, and `STATES`.
  - `PairView(state, until=None)` (frozen) and `Counts(queued, posted, dropped, withheld, not_posted)` with `found() -> int`.
  - `render(pairs: Dict[str, PairView], counts, rewritten=False, config_problem="", config_notice="") -> (status, conclusion, title, summary)`.
  - `CheckRuns(records, installation_for, clock=time.time)` with `run_id`, `counts(repo, head, queued=0)`, `ensure(repo, head) -> int`, `set_pair(repo, head, persona, view, rewritten=None)`, `set_config_problem(repo, head, problem, notice=)`, `config_answer(repo, head) -> Optional[Tuple[str, str]]`, `view(repo, head, persona) -> Optional[PairView]`, `add(repo, head, conn=None, **deltas)` (with `conn`, inside the caller's transaction), `publish(repo, head, queued=0)` (a no-op for a head with no pair state) and `forget_repo(repo)`; `lock` guards the pair states.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_checkruns.py`:

```python
import _path  # noqa: F401

import os
import shutil
import tempfile
import unittest

import checkruns
from checkruns import CheckRuns, Counts, PairView, render
from records import Records

HEAD = "a" * 40
T = 1760000000  # 2025-10-09 08:53 UTC


class RenderTest(unittest.TestCase):
    def test_a_head_with_nothing_scheduled_is_queued(self):
        status, conclusion, title, _ = render({}, Counts())
        self.assertEqual((status, conclusion, title), ("queued", None, "Queued"))

    def test_a_pass_in_flight_is_in_progress_and_names_each_persona(self):
        status, conclusion, title, summary = render(
            {"sage": PairView(checkruns.QUEUED), "sme": PairView(checkruns.RUNNING)}, Counts())
        self.assertEqual((status, conclusion), ("queued", None))
        status, _, title, summary = render(
            {"sage": PairView(checkruns.REVIEWED), "sme": PairView(checkruns.RUNNING)}, Counts())
        self.assertEqual((status, title), ("in_progress", "Reviewing"))
        self.assertIn("- **sme** is reviewing.", summary)

    def test_success_means_reviewed_and_found_nothing(self):
        views = {"sage": PairView(checkruns.REVIEWED), "sme": PairView(checkruns.REVIEWED)}
        self.assertEqual(render(views, Counts())[1], "success")
        for counts in (Counts(posted=1), Counts(queued=1), Counts(dropped=1), Counts(withheld=1),
                       Counts(not_posted=1)):
            with self.subTest(counts=counts):
                self.assertEqual(render(views, counts)[1], "neutral")

    def test_the_conclusion_is_never_failure(self):
        for state in checkruns.STATES:
            if state in (checkruns.QUEUED, checkruns.RUNNING):
                continue
            with self.subTest(state=state):
                conclusion = render({"sage": PairView(state, T)}, Counts())[1]
                self.assertIn(conclusion, ("success", "neutral"))
        self.assertEqual(render({}, Counts(), config_problem="x")[1], "neutral")

    def test_a_state_that_waits_on_a_person_names_the_run(self):
        views = {"a": PairView(checkruns.REVIEWED), "b": PairView(checkruns.DEFERRED, T),
                 "c": PairView(checkruns.CAPPED)}
        self.assertEqual(render(views, Counts())[2], "Waiting on a person")
        views["c"] = PairView(checkruns.REVIEWED)
        self.assertEqual(render(views, Counts())[2], "Retry scheduled")

    def test_a_retrying_state_names_its_retry_time(self):
        summary = render({"sage": PairView(checkruns.USAGE_LIMITED, T)}, Counts())[3]
        self.assertIn("retries on its own at 2025-10-09 08:53 UTC", summary)

    def test_the_counts_are_honest_about_what_never_arrived(self):
        summary = render({"sage": PairView(checkruns.REVIEWED)},
                         Counts(queued=1, posted=2, dropped=3, withheld=4, not_posted=5))[3]
        self.assertIn("2 posted, 1 waiting to post, 3 dropped as undeliverable, 4 withheld", summary)
        self.assertIn("5 turned away by the per-pass limit", summary)

    def test_a_rewritten_branch_is_said_once(self):
        summary = render({"sage": PairView(checkruns.REVIEWED)}, Counts(), rewritten=True)[3]
        self.assertIn("The branch was rewritten", summary)

    def test_an_invalid_config_says_so_and_carries_the_problem(self):
        status, conclusion, title, summary = render({}, Counts(), config_problem="code.personas: x")
        self.assertEqual((status, title), ("completed", "Not reviewed: configuration"))
        self.assertIn("code.personas: x", summary)

    def test_every_settled_state_has_words(self):
        for state in checkruns.STATES:
            if state not in (checkruns.QUEUED, checkruns.RUNNING):
                self.assertIn(state, checkruns._WORDS)
                self.assertIn(state, checkruns._RANK)

    def test_an_unknown_state_is_a_bug(self):
        with self.assertRaises(ValueError):
            render({"sage": PairView("great")}, Counts())


class FakeInstallation:
    def __init__(self):
        self.created, self.updated = [], []

    def create_check_run(self, repo, head, status, title, summary, conclusion=None):
        self.created.append((repo, head, status))
        return 100 + len(self.created)

    def update_check_run(self, repo, run_id, status, title, summary, conclusion=None):
        self.updated.append((run_id, status, conclusion, title, summary))


class CheckRunsTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.records = Records(os.path.join(d, "r.db"))
        self.addCleanup(self.records.close)
        self.inst = FakeInstallation()
        self.runs = CheckRuns(self.records, lambda repo: self.inst, clock=lambda: T)

    def test_a_head_gets_one_run_however_often_it_is_seen(self):
        first = self.runs.ensure("acme/web", HEAD)
        self.assertEqual(self.runs.ensure("acme/web", HEAD), first)
        self.assertEqual(len(self.inst.created), 1)
        self.runs.ensure("acme/web", "b" * 40)
        self.assertEqual(len(self.inst.created), 2)

    def test_publish_renders_the_pairs_and_the_counts(self):
        self.runs.set_pair("acme/web", HEAD, "sage", PairView(checkruns.REVIEWED))
        self.runs.ensure("acme/web", HEAD)
        self.runs.add("acme/web", HEAD, posted=2, dropped=1)
        self.runs.publish("acme/web", HEAD, queued=1)
        run_id, status, conclusion, title, summary = self.inst.updated[-1]
        self.assertEqual((status, conclusion, title), ("completed", "neutral", "Reviewed"))
        self.assertIn("2 posted, 1 waiting to post, 1 dropped", summary)

    def test_counts_survive_the_in_memory_state(self):
        self.runs.ensure("acme/web", HEAD)
        self.runs.add("acme/web", HEAD, withheld=1)
        again = CheckRuns(self.records, lambda repo: self.inst)
        self.assertEqual(again.counts("acme/web", HEAD).withheld, 1)

    def test_add_refuses_a_column_that_is_not_a_count(self):
        self.runs.ensure("acme/web", HEAD)
        with self.assertRaises(ValueError):
            self.runs.add("acme/web", HEAD, run_id=5)

    def test_two_threads_seeing_a_new_head_make_one_run(self):
        # A group's personas start together on a new head (PR #21 review).
        import threading
        import time as time_mod
        real = self.inst.create_check_run

        def slow(*a, **k):
            time_mod.sleep(0.05)
            return real(*a, **k)
        self.inst.create_check_run = slow
        ids = []
        threads = [threading.Thread(target=lambda: ids.append(self.runs.ensure("acme/web", HEAD)))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.inst.created), 1)
        self.assertEqual(len(set(ids)), 1)

    def test_forget_repo_drops_its_pair_states(self):
        self.runs.set_pair("acme/web", HEAD, "sage", PairView(checkruns.CAPPED))
        self.runs.forget_repo("acme/web")
        self.runs.set_pair("acme/web", HEAD, "red_team", PairView(checkruns.QUEUED))
        self.runs.publish("acme/web", HEAD)
        self.assertNotIn("sage", self.inst.updated[-1][3])

    def test_a_head_with_no_pair_state_is_left_as_it_last_rendered(self):
        # After a restart the drain still posts a head's findings; an empty map
        # would repaint a finished run as queued (PR #21 review).
        self.runs.ensure("acme/web", HEAD)
        self.runs.add("acme/web", HEAD, posted=1)
        before = list(self.inst.updated)
        self.runs.publish("acme/web", HEAD)
        self.assertEqual(self.inst.updated, before)

    def test_add_inside_a_callers_transaction_commits_with_it(self):
        self.runs.ensure("acme/web", HEAD)
        try:
            with self.records.tx() as c:
                self.runs.add("acme/web", HEAD, conn=c, posted=1)
                raise RuntimeError("crash before commit")
        except RuntimeError:
            pass
        self.assertEqual(self.runs.counts("acme/web", HEAD).posted, 0)

    def test_concurrent_set_and_publish_do_not_race(self):
        import threading
        self.runs.ensure("acme/web", HEAD)
        errors = []

        def setter():
            for i in range(300):
                self.runs.set_pair("acme/web", HEAD, f"p{i}", PairView(checkruns.RUNNING))

        def publisher():
            try:
                for _ in range(100):
                    self.runs.publish("acme/web", HEAD)
            except RuntimeError as exc:
                errors.append(exc)
        threads = [threading.Thread(target=setter), threading.Thread(target=publisher)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class ConfigNoticeTest(unittest.TestCase):
    def test_a_notice_shows_below_a_review_that_runs_and_is_never_green(self):
        # The spec's fallback: one file broken, the other applied, the problem shown.
        done = {"sage": PairView(checkruns.REVIEWED)}
        status, conclusion, title, summary = render(done, Counts(),
                                                    config_notice="org file: plan.label: x")
        self.assertEqual((status, conclusion, title), ("completed", "neutral", "Reviewed"))
        self.assertIn("org file: plan.label: x", summary)
        self.assertEqual(render(done, Counts())[1], "success")

    def test_the_problem_wording_claims_no_cause_the_text_below_does_not(self):
        title, sentence = checkruns._WORDS[checkruns.CONFIG_INVALID]
        self.assertNotIn("invalid", (title + sentence).lower())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_checkruns.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'checkruns'`.

- [ ] **Step 3: Implement**

`hosted/checkruns.py`:

```python
"""The per-PR check run: one `claudebox` run per head, and its wording.

A head's run reflects every pair reviewing that head. `render` turns the
pairs' states and the head's finding counts into the run's status,
conclusion, title and summary, and is the only place the wording lives (the
spec defers "the exact wording of each state" to this phase). `CheckRuns`
records each run's id against its head, since the Checks API does not
deduplicate runs by name, and creates a run only for a head with none.

The conclusion is never `failure`, and `success` means only "reviewed, and
found nothing": claudebox does not assert a PR is acceptable.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

# A pair's state on a head. The first two are a pass in flight; the rest are
# where its last pass left it. 2a has no budget, too-large or refused-read
# states: those outcomes arrive with 2b.
QUEUED = "queued"
RUNNING = "running"
REVIEWED = "reviewed"
DEFERRED = "deferred"
USAGE_LIMITED = "usage-limited"
FAILED = "failed"
FAILING = "failing"
CAPPED = "capped"
ROUND_CAP = "round-cap"
REVOKED = "revoked"
CONFIG_INVALID = "config-invalid"
STATES = (QUEUED, RUNNING, REVIEWED, DEFERRED, USAGE_LIMITED, FAILED, FAILING, CAPPED,
          ROUND_CAP, REVOKED, CONFIG_INVALID)

# Which state names the run when its pairs differ. A state that waits on a
# person outranks one that clears itself, which outranks one that is done.
_RANK = (CONFIG_INVALID, FAILING, CAPPED, FAILED, USAGE_LIMITED, DEFERRED, REVOKED, ROUND_CAP,
         REVIEWED)

# Per state: the title, and the one sentence a person reads in the summary
# about why the pair stopped and what clears it. {until} is a UTC time.
_WORDS = {
    REVIEWED: ("Reviewed", "reviewed this head."),
    DEFERRED: ("Retry scheduled",
               "stopped on a claudebox fault, not a provider's. It retries on its own at {until}."),
    USAGE_LIMITED: ("Paused on a usage limit",
                    "hit the provider's usage limit. It retries on its own at {until}."),
    FAILED: ("Paused on a provider failure",
             "stopped because the provider failed. It retries on its own at {until}."),
    FAILING: ("Waiting on a person",
              "failed in a way only it hit, so it will not retry on its own. A new commit clears it, "
              "and the operator can see why in `claudebox.sh hosted status`."),
    CAPPED: ("Waiting on a person",
             "reached a per-pair provider cap. A new commit clears it; if the cap is too low for "
             "this PR, the operator raises it in the deployment file."),
    ROUND_CAP: ("Reviewed; round cap reached",
                "has reviewed this plan as many rounds as `rounds.cap` allows, and will not review "
                "it again."),
    REVOKED: ("Interrupted",
              "was interrupted (a timeout, an access change, or a restart). It runs again on its "
              "own."),
    # One sentence for every reason a repo's configuration stops a review: a
    # file that is invalid, a setting this deployment cannot serve, or both
    # files broken. The problem text below it says which.
    CONFIG_INVALID: ("Not reviewed: configuration",
                     "is not reviewing this PR, because of the configuration problem below. "
                     "It reviews again on its own once the problem is gone."),
}


@dataclass(frozen=True)
class PairView:
    state: str
    until: Optional[int] = None   # Unix seconds, for the states that retry on their own


@dataclass
class Counts:
    queued: int = 0
    posted: int = 0
    dropped: int = 0
    withheld: int = 0
    not_posted: int = 0

    def found(self) -> int:
        return self.queued + self.posted + self.dropped + self.withheld + self.not_posted


def _utc(stamp: Optional[int]) -> str:
    if stamp is None:
        return "a time not yet known"
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(stamp))


def render(pairs: Dict[str, PairView], counts: Counts, rewritten: bool = False,
           config_problem: str = "", config_notice: str = "") -> Tuple[str, Optional[str], str, str]:
    """(status, conclusion, title, summary) for one head.

    `pairs` maps persona to its view. An empty map with no config problem is a
    head nothing has been scheduled for yet, which shows as queued. A config
    problem stops the review; a config notice (one file broken, the other
    applied) is shown below a review that runs.
    """
    for persona, view in pairs.items():
        if view.state not in STATES:
            raise ValueError(f"{persona}: unknown state {view.state!r}")
    lines = []
    if config_problem:
        title, sentence = _WORDS[CONFIG_INVALID]
        summary = f"claudebox {sentence}\n\n{config_problem}"
        return "completed", "neutral", title, summary
    if not pairs or any(v.state == QUEUED for v in pairs.values()):
        status, title = "queued", "Queued"
    elif any(v.state == RUNNING for v in pairs.values()):
        status, title = "in_progress", "Reviewing"
    else:
        status = "completed"
        worst = min((v.state for v in pairs.values()), key=_RANK.index)
        title = _WORDS[worst][0]
    for persona in sorted(pairs):
        view = pairs[persona]
        if view.state == QUEUED:
            lines.append(f"- **{persona}** is queued.")
        elif view.state == RUNNING:
            lines.append(f"- **{persona}** is reviewing.")
        else:
            lines.append(f"- **{persona}** " + _WORDS[view.state][1].format(until=_utc(view.until)))
    tally = (f"Findings: {counts.posted} posted, {counts.queued} waiting to post, "
             f"{counts.dropped} dropped as undeliverable, {counts.withheld} withheld for "
             f"containing a credential, {counts.not_posted} turned away by the per-pass limit.")
    parts = []
    if rewritten:
        parts.append("The branch was rewritten since the last review, so this head is reviewed "
                     "as a whole.")
    parts.append("\n".join(lines))
    parts.append(tally)
    if config_notice:
        parts.append("Configuration: one file was ignored, so this PR is reviewed under the "
                     "other one.\n\n" + config_notice)
    summary = "\n\n".join(p for p in parts if p)
    conclusion = None
    if status == "completed":
        clean = all(v.state in (REVIEWED, ROUND_CAP) for v in pairs.values())
        # A broken file shown in the summary is never a green check.
        conclusion = "success" if clean and counts.found() == 0 and not config_notice else "neutral"
    return status, conclusion, title, summary


@dataclass
class _Head:
    pairs: Dict[str, PairView] = field(default_factory=dict)
    rewritten: bool = False
    config_problem: str = ""
    config_notice: str = ""


class CheckRuns:
    """One run per (repo, head), its id in the records store, its pair states in memory.

    Pair states are in memory, like the rest of 2a's pair state (overview
    decision 1); the counts are rows, because the outbox moves them after the
    job that wrote the findings is gone.
    """

    def __init__(self, records, installation_for: Callable[[str], object],
                 clock: Callable[[], float] = time.time):
        self.records = records
        self.installation_for = installation_for
        self.clock = clock
        # Written by request threads (a group's personas finish together) and
        # read by the drain; one lock covers every change and every render.
        self.lock = threading.Lock()
        self._create_lock = threading.Lock()   # held across a GitHub call, so not `lock`
        self._heads: Dict[Tuple[str, str], _Head] = {}

    def run_id(self, repo: str, head: str) -> Optional[int]:
        row = self.records.conn().execute(
            "SELECT run_id FROM check_runs WHERE repo = ? AND head = ?", (repo, head)).fetchone()
        return row[0] if row else None

    def counts(self, repo: str, head: str, queued: int = 0) -> Counts:
        row = self.records.conn().execute(
            "SELECT posted, dropped, withheld, not_posted FROM check_runs "
            "WHERE repo = ? AND head = ?", (repo, head)).fetchone()
        if row is None:
            return Counts(queued=queued)
        return Counts(queued, row[0], row[1], row[2], row[3])

    def ensure(self, repo: str, head: str) -> int:
        """The head's run id, creating the run when the head has none recorded.

        The check and the create happen under one lock, so a group's personas
        reaching a new head together make one run, never one each.
        """
        existing = self.run_id(repo, head)
        if existing is not None:
            return existing
        with self._create_lock:
            existing = self.run_id(repo, head)
            if existing is not None:
                return existing
            status, conclusion, title, summary = render({}, Counts())
            run_id = self.installation_for(repo).create_check_run(repo, head, status, title,
                                                                  summary, conclusion)
            with self.records.tx() as c:
                c.execute("INSERT OR IGNORE INTO check_runs (repo, head, run_id, created_at) "
                          "VALUES (?, ?, ?, ?)", (repo, head, run_id, int(self.clock())))
            return self.run_id(repo, head)

    def set_pair(self, repo: str, head: str, persona: str, view: PairView,
                 rewritten: Optional[bool] = None) -> None:
        with self.lock:
            h = self._heads.setdefault((repo, head), _Head())
            h.pairs[persona] = view
            if rewritten is not None:
                h.rewritten = rewritten

    def set_config_problem(self, repo: str, head: str, problem: str, notice: str = "") -> None:
        with self.lock:
            h = self._heads.setdefault((repo, head), _Head())
            h.config_problem, h.config_notice = problem, notice

    def view(self, repo: str, head: str, persona: str) -> Optional[PairView]:
        """A persona's view on a head, or None when nothing has set one."""
        with self.lock:
            h = self._heads.get((repo, head))
            return None if h is None else h.pairs.get(persona)

    def config_answer(self, repo: str, head: str) -> Optional[Tuple[str, str]]:
        """The (problem, notice) last set on a head, or None for a head with no state."""
        with self.lock:
            h = self._heads.get((repo, head))
            return None if h is None else (h.config_problem, h.config_notice)

    def add(self, repo: str, head: str, conn=None, **deltas: int) -> None:
        """Move the head's counts: posted, dropped, withheld or not_posted.

        Pass `conn` to move them inside the caller's transaction, which is how
        the drain deletes an item and counts it in one commit.
        """
        for name in deltas:
            if name not in ("posted", "dropped", "withheld", "not_posted"):
                raise ValueError(f"not a count: {name}")
        sets = ", ".join(f"{n} = {n} + ?" for n in deltas)
        sql = f"UPDATE check_runs SET {sets} WHERE repo = ? AND head = ?"
        args = tuple(deltas.values()) + (repo, head)
        if conn is not None:
            conn.execute(sql, args)
            return
        with self.records.tx() as c:
            c.execute(sql, args)

    def publish(self, repo: str, head: str, queued: int = 0) -> None:
        """Render the head's current state onto its run.

        A head this process holds no pair state for (the control plane
        restarted since its pass, and the outbox is still posting its findings)
        is left as it last rendered: an empty map would repaint a finished run
        as queued. Its counts are rows, and the next publish with pair state
        shows them.
        """
        with self.lock:
            h = self._heads.get((repo, head))
            if h is None:
                return
            pairs = dict(h.pairs)
            rewritten, problem, notice = h.rewritten, h.config_problem, h.config_notice
        run_id = self.ensure(repo, head)
        status, conclusion, title, summary = render(pairs, self.counts(repo, head, queued),
                                                    rewritten, problem, notice)
        self.installation_for(repo).update_check_run(repo, run_id, status, title, summary,
                                                     conclusion)

    def forget_repo(self, repo: str) -> None:
        with self.lock:
            for key in [k for k in self._heads if k[0] == repo]:
                del self._heads[key]
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_checkruns.py'`, then `./test-python.sh`
Expected: PASS, 22 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/checkruns.py tests/test_hosted_checkruns.py
git commit -m "hosted: the per-PR check run and its wording"
```

### Task 3: The outbox

**Files:**
- Create: `hosted/outbox.py`
- Modify: `hosted/github_app.py` (`Installation.pull`, `is_public`)
- Test: `tests/test_hosted_outbox.py`
- Modify: `tests/test_hosted_github_app.py`

**Interfaces:**
- Consumes: `records.Records`, `scan.Scanner`, `scan.sign`, `github_app.GitHubError` (2a.1); Task 2's `CheckRuns.add` and `publish`.
- Produces:
  - `Installation.pull(full_name, pr) -> Optional[dict]`, and `is_public(repo) -> bool`, which reads `visibility` before `private` and fails closed.
  - `QUEUED`, `WITHHELD`, `TOO_LONG`, `ENOUGH`, `MESSAGES`, `MIN_INTERVAL = 1.0`, `PER_HOUR = 400`, `BACKOFF_BASE = 30`, `BACKOFF_MAX = 1800`.
  - `JobContext(job_id, installation_id, repo, pr, head)` (frozen).
  - `Outbox(records, scanner, check_runs, comment_bytes, comments_per_pass, clock=time.time)` with `accept(job, persona, seq, body) -> (result, message)`, `queued(repo, head) -> int`, `forget_job(job_id)` and `drop_repo(repo, reason) -> int`, which also deletes the repo's `withheld` rows.
  - `Drainer(outbox, installation, installation_id, clock=time.time, min_interval=MIN_INTERVAL, per_hour=PER_HOUR)` with `step() -> float`.

- [ ] **Step 1: Write the failing tests**

`tests/test_hosted_outbox.py`:

```python
import _path  # noqa: F401

import os
import shutil
import tempfile
import unittest

import outbox
from github_app import GitHubError
from outbox import Drainer, JobContext, Outbox
from records import Records
from scan import Scanner

HEAD = "a" * 40
NEW_HEAD = "b" * 40
JOB = JobContext("job1", 7, "acme/web", 12, HEAD)


class FakeRuns:
    def __init__(self):
        self.counts = {}
        self.published = []
        self.outside_tx = []     # count moves made outside a caller's transaction
        self.publish_error = None

    def add(self, repo, head, conn=None, **deltas):
        if conn is None:
            self.outside_tx.append(deltas)
        for k, v in deltas.items():
            self.counts[(repo, head, k)] = self.counts.get((repo, head, k), 0) + v

    def publish(self, repo, head, queued=0):
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((repo, head, queued))


class FakeInstallation:
    def __init__(self):
        self.repo_info = {"private": True}
        self.pull_info = {"state": "open", "head": {"sha": HEAD}}
        self.posted = []
        self.fail = []        # GitHubErrors to raise from comment(), in order

    def repo(self, name):
        return self.repo_info

    def pull(self, name, pr):
        return self.pull_info

    def comment(self, repo, pr, body):
        if self.fail:
            raise self.fail.pop(0)
        self.posted.append((repo, pr, body))
        return len(self.posted)


class Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.records = Records(os.path.join(d, "r.db"))
        self.addCleanup(self.records.close)
        self.now = [1000.0]
        self.runs = FakeRuns()
        self.box = Outbox(self.records, Scanner({"OLLAMA_API_KEY": "olk-secret-value"}), self.runs,
                          comment_bytes=100, comments_per_pass=2, clock=lambda: self.now[0])

    def rows(self, table, cols="*"):
        return [tuple(r) for r in self.records.conn().execute(f"SELECT {cols} FROM {table}")]


class AcceptTest(Base):
    def test_a_clean_finding_is_queued_with_its_head(self):
        self.assertEqual(self.box.accept(JOB, "sage", 1, "should-fix: x"), ("queued", ""))
        self.assertEqual(self.rows("outbox", "key, repo, pr, head, body"),
                         [("job1/sage/1", "acme/web", 12, HEAD, "should-fix: x")])

    def test_a_retried_post_answers_as_the_first_did_and_queues_once(self):
        self.box.accept(JOB, "sage", 1, "x")
        self.assertEqual(self.box.accept(JOB, "sage", 1, "x"), ("queued", ""))
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_a_credential_is_withheld_recorded_redacted_and_counted(self):
        result, message = self.box.accept(JOB, "sage", 1, "the key is ghp_" + "a" * 36)
        self.assertEqual(result, "withheld")
        self.assertIn("Restate it without the literal value", message)
        self.assertEqual(self.rows("outbox"), [])
        detector, redacted = self.rows("withheld", "detector, redacted_body")[0]
        self.assertNotIn("ghp_", redacted)
        self.assertEqual(self.rows("withheld_counts", "detector, count"), [(detector, 1)])
        self.assertEqual(self.runs.counts[("acme/web", HEAD, "withheld")], 1)
        # The count moves in the transaction that writes the row (PR #21 review).
        self.assertEqual(self.runs.outside_tx, [])

    def test_a_deployment_literal_is_withheld_by_name(self):
        self.box.accept(JOB, "sage", 1, "found olk-secret-value in config")
        self.assertEqual(self.rows("withheld", "detector"), [("literal:OLLAMA_API_KEY",)])

    def test_the_withheld_record_holds_no_plain_text_hash(self):
        import hashlib
        body = "ghp_" + "a" * 36
        self.box.accept(JOB, "sage", 1, body)
        stored = self.rows("withheld", "salted_hash")[0][0]
        self.assertNotEqual(stored[16:], hashlib.sha256(body.encode()).digest())
        self.assertEqual(stored[16:], hashlib.sha256(stored[:16] + body.encode()).digest())

    def test_too_long_is_refused_and_not_counted_against_the_pass(self):
        result, message = self.box.accept(JOB, "sage", 1, "x" * 101)
        self.assertEqual((result, "100 bytes" in message), ("too_long", True))
        self.assertEqual(self.box.accept(JOB, "sage", 2, "a")[0], "queued")
        self.assertEqual(self.box.accept(JOB, "sage", 3, "b")[0], "queued")

    def test_size_is_measured_in_bytes(self):
        self.assertEqual(self.box.accept(JOB, "sage", 1, "é" * 51)[0], "too_long")

    def test_the_per_pass_limit_is_per_pair_and_counts_what_it_turns_away(self):
        self.box.accept(JOB, "sage", 1, "a")
        self.box.accept(JOB, "sage", 2, "b")
        result, message = self.box.accept(JOB, "sage", 3, "c")
        self.assertEqual(result, "enough")
        self.assertIn("Stop posting", message)
        self.assertEqual(self.runs.counts[("acme/web", HEAD, "not_posted")], 1)
        self.assertEqual(self.box.accept(JOB, "sme", 1, "d")[0], "queued")

    def test_forget_job_releases_its_memory(self):
        self.box.accept(JOB, "sage", 1, "a")
        self.box.forget_job("job1")
        self.assertEqual(self.box._accepted, {})
        self.assertEqual(self.box._results, {})

    def test_drop_repo_drops_and_counts_every_item(self):
        self.box.accept(JOB, "sage", 1, "a")
        self.box.accept(JOB, "sme", 1, "b")
        self.assertEqual(self.box.drop_repo("acme/web", "the repository went public"), 2)
        self.assertEqual(self.rows("outbox"), [])
        self.assertEqual(self.runs.counts[("acme/web", HEAD, "dropped")], 2)
        self.assertEqual(len(self.rows("audit")), 2)
        self.assertEqual(self.runs.outside_tx, [])

    def test_drop_repo_deletes_its_withheld_records_and_keeps_the_day_counts(self):
        # The spec's Visibility list names withheld-finding records (PR #21 review).
        self.box.accept(JOB, "sage", 1, "token olk-secret-value here")
        self.box.accept(JobContext("j2", 7, "acme/api", 3, HEAD), "sage", 1, "olk-secret-value")
        self.box.drop_repo("acme/web", "the repository went public")
        self.assertEqual(self.rows("withheld", "repo"), [("acme/api",)])
        self.assertNotEqual(self.rows("withheld_counts"), [])


class DrainTest(Base):
    def setUp(self):
        super().setUp()
        self.inst = FakeInstallation()
        self.drain = Drainer(self.box, self.inst, 7, clock=lambda: self.now[0], per_hour=3)

    def test_a_finding_is_posted_signed_and_leaves_the_outbox(self):
        self.box.accept(JOB, "sage", 1, "should-fix: x")
        self.drain.step()
        repo, pr, body = self.inst.posted[0]
        self.assertEqual((repo, pr), ("acme/web", 12))
        self.assertTrue(body.startswith("should-fix: x"))
        self.assertIn("-claudebox", body)
        self.assertEqual(self.rows("outbox"), [])
        self.assertEqual(self.runs.counts[("acme/web", HEAD, "posted")], 1)
        self.assertEqual(self.runs.published[-1], ("acme/web", HEAD, 0))

    def test_a_finding_written_against_an_older_head_still_posts_and_names_it(self):
        self.box.accept(JOB, "sage", 1, "x")
        self.inst.pull_info = {"state": "open", "head": {"sha": NEW_HEAD}}
        self.drain.step()
        self.assertTrue(self.inst.posted[0][2].startswith(f"Written against commit {HEAD[:12]}"))

    def test_posts_are_paced_one_a_second(self):
        for n in (1, 2):
            self.box.accept(JOB, "sage", n, "x")
        self.assertEqual(self.drain.step(), 0.0)
        self.assertGreater(self.drain.step(), 0)
        self.assertEqual(len(self.inst.posted), 1)
        self.now[0] += 1
        self.drain.step()
        self.assertEqual(len(self.inst.posted), 2)

    def test_posts_are_capped_per_hour(self):
        for n in range(1, 5):
            self.box.accept(JobContext(f"j{n}", 7, "acme/web", 12, HEAD), "sage", 1, "x")
        for _ in range(3):
            self.drain.step()
            self.now[0] += 1
        wait = self.drain.step()
        self.assertEqual(len(self.inst.posted), 3)
        self.assertGreater(wait, 3000)

    def test_prs_are_served_round_robin(self):
        a = JobContext("ja", 7, "acme/web", 1, HEAD)
        b = JobContext("jb", 7, "acme/web", 2, HEAD)
        for n in (1, 2, 3):
            self.box.accept(a, "sage", n, f"a{n}")
        self.box.accept(b, "sage", 1, "b1")
        for _ in range(3):
            self.drain.step()
            self.now[0] += 1
        self.assertEqual([p[1] for p in self.inst.posted], [1, 2, 1])

    def test_retry_after_and_5xx_are_retried_and_nothing_else(self):
        self.box.accept(JOB, "sage", 1, "x")
        self.inst.fail = [GitHubError(403, "secondary rate limit", retry_after=60)]
        self.drain.step()
        self.assertEqual(self.rows("outbox", "attempts, next_attempt_at"), [(1, 1060)])
        self.now[0] += 60
        self.inst.fail = [GitHubError(502, "bad gateway")]
        self.drain.step()
        self.assertEqual(self.rows("outbox", "attempts")[0][0], 2)
        self.now[0] += 10000
        self.drain.step()
        self.assertEqual(len(self.inst.posted), 1)

    def test_an_error_retrying_cannot_fix_drops_the_item_and_records_why(self):
        for status, message in ((404, "Not Found"), (410, "Gone"), (422, "Unprocessable"),
                                (403, "Unable to create comment because issue is locked")):
            with self.subTest(status=status):
                self.box.accept(JobContext(f"j{status}", 7, "acme/web", 12, HEAD), "sage", 1, "x")
                self.inst.fail = [GitHubError(status, message)]
                self.drain.step()
                self.now[0] += 1
                self.assertEqual(self.rows("outbox"), [])
        self.assertEqual(self.runs.counts[("acme/web", HEAD, "dropped")], 4)
        self.assertEqual([r[0] for r in self.rows("audit", "action")], ["outbox.drop"] * 4)

    def test_a_repo_gone_public_or_out_of_reach_drops_before_posting(self):
        for info, why in (({"private": False}, "public"), (None, "can no longer see")):
            with self.subTest(why=why):
                self.box.accept(JobContext(f"j{why}", 7, "acme/web", 12, HEAD), "sage", 1, "x")
                self.inst.repo_info = info
                self.drain.step()
                self.now[0] += 1
                self.assertIn(why, self.rows("audit", "detail")[-1][0])
        self.assertEqual(self.inst.posted, [])

    def test_every_settle_moves_its_count_inside_the_deleting_transaction(self):
        self.box.accept(JOB, "sage", 1, "a")
        self.box.accept(JOB, "sme", 1, "b")
        self.drain.step()
        self.now[0] += 1
        self.inst.pull_info = {"state": "closed", "head": {"sha": HEAD}}
        self.drain.step()
        self.assertEqual(self.runs.outside_tx, [])

    def test_a_check_run_failure_after_a_post_is_not_read_as_the_comments(self):
        self.box.accept(JOB, "sage", 1, "x")
        self.runs.publish_error = GitHubError(404, "gone")
        self.drain.step()
        self.assertEqual(len(self.inst.posted), 1)
        self.assertEqual(self.runs.counts, {("acme/web", HEAD, "posted"): 1})
        self.assertEqual(self.rows("audit"), [])

    def test_an_internal_repo_is_posted_to(self):
        self.box.accept(JOB, "sage", 1, "x")
        self.inst.repo_info = {"private": False, "visibility": "internal"}
        self.drain.step()
        self.assertEqual(len(self.inst.posted), 1)

    def test_a_closed_or_missing_pr_drops(self):
        self.box.accept(JOB, "sage", 1, "x")
        self.inst.pull_info = {"state": "closed", "head": {"sha": HEAD}}
        self.drain.step()
        self.assertEqual(self.inst.posted, [])
        self.assertIn("closed", self.rows("audit", "detail")[0][0])

    def test_the_body_is_scanned_again_before_posting(self):
        self.box.accept(JOB, "sage", 1, "x")
        with self.records.tx() as c:
            c.execute("UPDATE outbox SET body = ?", ("now it holds olk-secret-value",))
        self.drain.step()
        self.assertEqual(self.inst.posted, [])
        self.assertEqual(self.rows("withheld", "detector, persona, job_id"),
                         [("literal:OLLAMA_API_KEY", "sage", "job1")])
        self.assertEqual(self.runs.counts[("acme/web", HEAD, "withheld")], 1)

    def test_an_unreachable_github_is_retried(self):
        self.box.accept(JOB, "sage", 1, "x")
        self.inst.fail = [GitHubError(0, "connection refused")]
        self.drain.step()
        self.assertEqual(len(self.rows("outbox")), 1)

    def test_an_empty_queue_says_wait(self):
        self.assertGreater(self.drain.step(), 0)


if __name__ == "__main__":
    unittest.main()
```

And apply this change to `tests/test_hosted_github_app.py`:

```diff
diff --git a/tests/test_hosted_github_app.py b/tests/test_hosted_github_app.py
index 5d64031..d7ea215 100644
--- a/tests/test_hosted_github_app.py
+++ b/tests/test_hosted_github_app.py
@@ -5,7 +5,7 @@ import json
 import unittest
 
 from github_app import (API, CHECK_RUN_NAME, READ_PERMISSIONS, SUMMARY_MAX, App, GitHubError,
-                        Response, app_jwt, is_fork_pr, parse_time, public_user, user_for_token)
+                        Response, app_jwt, is_fork_pr, is_public, parse_time, public_user, user_for_token)
 
 
 class FakeSigner:
@@ -126,6 +126,12 @@ class InstallationTest(unittest.TestCase):
         self.routes[("GET", "/repos/acme/gone")] = Response(404, {}, b'{"message":"Not Found"}')
         self.assertIsNone(self.inst.repo("acme/gone"))
 
+    def test_a_pull_request_is_fetched_and_a_missing_one_is_none(self):
+        self.routes[("GET", "/repos/acme/web/pulls/7")] = {"number": 7, "state": "open"}
+        self.routes[("GET", "/repos/acme/web/pulls/8")] = Response(404, {}, b'{"message":"Not Found"}')
+        self.assertEqual(self.inst.pull("acme/web", 7)["state"], "open")
+        self.assertIsNone(self.inst.pull("acme/web", 8))
+
     def test_repos_come_from_the_repositories_key(self):
         self.routes[("GET", "/installation/repositories?per_page=100")] = {
             "total_count": 1, "repositories": [{"full_name": "acme/web"}]}
@@ -281,6 +287,25 @@ class RedirectTest(unittest.TestCase):
         self.assertEqual(seen, [])
 
 
+class VisibilityTest(unittest.TestCase):
+    def test_an_internal_repo_counts_as_private_whatever_private_says(self):
+        # The spec's Visibility; PR #21 review: judge on `visibility`, the field
+        # that names internal, not on what `private` reports for one.
+        cases = [
+            ({"visibility": "internal", "private": False}, False),
+            ({"visibility": "internal", "private": True}, False),
+            ({"visibility": "private"}, False),
+            ({"visibility": "public", "private": True}, True),
+            ({"private": True}, False),
+            ({"private": False}, True),
+            ({}, True),
+            ({"private": "true"}, True),
+        ]
+        for repo, public in cases:
+            with self.subTest(repo=repo):
+                self.assertEqual(is_public(repo), public)
+
+
 class ForkTest(unittest.TestCase):
     def pr(self, head, base="acme/web"):
         return {"head": {"repo": None if head is None else {"full_name": head}},
```

- [ ] **Step 2: Run them to see them fail**

Run: `./test-python.sh -p 'test_hosted_outbox.py'` and `./test-python.sh -p 'test_hosted_github_app.py'`
Expected: FAIL: `No module named 'outbox'`, and `cannot import name 'is_public'`.

- [ ] **Step 3: Implement**

Apply this change to `hosted/github_app.py`:

```diff
diff --git a/hosted/github_app.py b/hosted/github_app.py
index 73f58ad..d8bdb86 100644
--- a/hosted/github_app.py
+++ b/hosted/github_app.py
@@ -248,6 +248,15 @@ class Installation(_Client):
                 return None
             raise
 
+    def pull(self, full_name: str, pr: int) -> Optional[dict]:
+        """One pull request, or None when GitHub does not have it (or the App cannot see it)."""
+        try:
+            return self._call("GET", f"/repos/{check_repo(full_name)}/pulls/{int(pr)}")
+        except GitHubError as e:
+            if e.status == 404:
+                return None
+            raise
+
     def open_prs(self, full_name: str) -> List[dict]:
         return self._pages(f"/repos/{check_repo(full_name)}/pulls?state=open&per_page=100")
 
@@ -324,6 +333,20 @@ class Installation(_Client):
         self._call("PATCH", f"/repos/{check_repo(full_name)}/check-runs/{int(run_id)}", payload)
 
 
+def is_public(repo: Mapping[str, Any]) -> bool:
+    """Whether a repository GitHub described is public, for the spec's "Visibility".
+
+    `visibility` is the three-valued field (public, private, internal), and an
+    internal repo counts as private. `private` is read only when `visibility`
+    is absent, and a repo that says neither counts as public: the rule fails
+    closed.
+    """
+    visibility = repo.get("visibility")
+    if isinstance(visibility, str):
+        return visibility != "private" and visibility != "internal"
+    return repo.get("private") is not True
+
+
 def is_fork_pr(pr: Mapping[str, Any]) -> bool:
     """The spec's fork rule: the head repository alone decides.
 
```

`hosted/outbox.py`:

```python
"""The outbox: findings accepted from workers, and the queue that posts them.

Acceptance is synchronous, so the worker's `gh pr comment` learns at once
whether its finding was queued, withheld for containing a credential, too
long, or one past the per-pass limit. The size and per-pass caps are applied
here, so the outbox only ever holds items it can post.

The drain posts per installation, paced at one comment a second and 400 an
hour by default, round-robin across PRs. Before each post it checks the
repo's visibility and the PR's state against GitHub uncached, scans the body
again, names the head the finding was written against when the PR has moved
past it, and adds the `-claudebox` signature. An item leaves the outbox by
being posted, withheld, or dropped as undeliverable, and nothing else: only
Retry-After, a 5xx and an unreachable GitHub are retried.
"""

import hashlib
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional, Set, Tuple

import scan
from github_app import GitHubError, is_public

QUEUED = "queued"
WITHHELD = "withheld"
TOO_LONG = "too_long"
ENOUGH = "enough"

MESSAGES = {
    WITHHELD: ("This finding contained a credential-shaped string, so it was not posted. "
               "Restate it without the literal value, and carry on with the review."),
    TOO_LONG: "This finding is longer than {limit} bytes. Shorten it and post it again.",
    ENOUGH: ("This pass has posted {limit} findings, the most one pass may. Stop posting "
             "and finish your review."),
}

# Pacing defaults (spec, "Posting and the outbox").
MIN_INTERVAL = 1.0
PER_HOUR = 400
# A retryable failure with no Retry-After waits this long, doubling per attempt.
BACKOFF_BASE = 30
BACKOFF_MAX = 1800


@dataclass(frozen=True)
class JobContext:
    """What acceptance needs to know about the job a finding came from."""
    job_id: str
    installation_id: int
    repo: str
    pr: int
    head: str


class Outbox:
    def __init__(self, records, scanner: scan.Scanner, check_runs, comment_bytes: int,
                 comments_per_pass: int, clock: Callable[[], float] = time.time):
        self.records = records
        self.scanner = scanner
        self.check_runs = check_runs
        self.comment_bytes = comment_bytes
        self.comments_per_pass = comments_per_pass
        self.clock = clock
        self.lock = threading.Lock()
        # Per (job, persona): accepted findings, and the result each sequence
        # number got, so a retried post-finding answers as the first one did.
        # In memory: a job does not outlive the control plane that started it.
        self._accepted: Dict[Tuple[str, str], int] = {}
        self._results: Dict[str, str] = {}

    def accept(self, job: JobContext, persona: str, seq: int, body: str) -> Tuple[str, str]:
        """(result, message). The message is for the model, and empty when queued."""
        key = f"{job.job_id}/{persona}/{int(seq)}"
        with self.lock:
            if key in self._results:
                result = self._results[key]
                return result, self._message(result)
            result = self._decide(job, persona, key, body)
            self._results[key] = result
            return result, self._message(result)

    def _message(self, result: str) -> str:
        if result == QUEUED:
            return ""
        limit = self.comment_bytes if result == TOO_LONG else self.comments_per_pass
        return MESSAGES[result].format(limit=limit)

    def _decide(self, job: JobContext, persona: str, key: str, body: str) -> str:
        now = int(self.clock())
        detector = self.scanner.first(body)
        if detector is not None:
            with self.records.tx() as c:
                self._withhold(job.repo, job.pr, job.job_id, persona, detector, body, now, c)
                self.check_runs.add(job.repo, job.head, conn=c, withheld=1)
            return WITHHELD
        if len(body.encode("utf-8")) > self.comment_bytes:
            return TOO_LONG
        pair = (job.job_id, persona)
        if self._accepted.get(pair, 0) >= self.comments_per_pass:
            self.check_runs.add(job.repo, job.head, not_posted=1)
            return ENOUGH
        with self.records.tx() as c:
            c.execute("INSERT INTO outbox (key, installation_id, repo, pr, head, body, "
                      "next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (key, job.installation_id, job.repo, job.pr, job.head, body, now, now))
        self._accepted[pair] = self._accepted.get(pair, 0) + 1
        return QUEUED

    def _withhold(self, repo, pr, job_id, persona, detector, body, now, conn=None) -> None:
        salt = os.urandom(16)
        digest = salt + hashlib.sha256(salt + body.encode("utf-8")).digest()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))

        def write(c):
            c.execute("INSERT INTO withheld (repo, pr, job_id, persona, detector, salted_hash, "
                      "redacted_body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (repo, pr, job_id, persona, detector, digest, self.scanner.redact(body), now))
            c.execute("INSERT INTO withheld_counts (detector, day, count) VALUES (?, ?, 1) "
                      "ON CONFLICT (detector, day) DO UPDATE SET count = count + 1",
                      (detector, day))
        if conn is not None:
            write(conn)
        else:
            with self.records.tx() as c:
                write(c)

    def queued(self, repo: str, head: str) -> int:
        return self.records.conn().execute(
            "SELECT COUNT(*) FROM outbox WHERE repo = ? AND head = ?", (repo, head)).fetchone()[0]

    def forget_job(self, job_id: str) -> None:
        with self.lock:
            for pair in [p for p in self._accepted if p[0] == job_id]:
                del self._accepted[pair]
            for key in [k for k in self._results if k.startswith(job_id + "/")]:
                del self._results[key]

    def drop_repo(self, repo: str, reason: str) -> int:
        """Loss of access: drop the repo's queued items and delete its withheld records.

        Both are content-bearing rows keyed to the repo, which the spec's
        "Visibility" deletes "before any further pass". The per-detector day
        counts are not content and survive. Returns how many items dropped.
        """
        with self.records.tx() as c:
            rows = c.execute("SELECT key, head FROM outbox WHERE repo = ?", (repo,)).fetchall()
            c.execute("DELETE FROM outbox WHERE repo = ?", (repo,))
            c.execute("DELETE FROM withheld WHERE repo = ?", (repo,))
            heads: Dict[str, int] = {}
            for key, head in rows:
                self._audit_drop(c, repo, key, reason)
                heads[head] = heads.get(head, 0) + 1
            for head, n in heads.items():
                self.check_runs.add(repo, head, conn=c, dropped=n)
        return len(rows)

    def _audit_drop(self, c, repo: str, key: str, reason: str) -> None:
        c.execute("INSERT INTO audit (at, actor_id, action, scope, detail) VALUES (?, NULL, ?, ?, ?)",
                  (int(self.clock()), "outbox.drop", repo, f"{key}: {reason}"))


class Drainer:
    """One installation's queue. `step()` posts at most one item and says when to call again."""

    def __init__(self, outbox: Outbox, installation, installation_id: int,
                 clock: Callable[[], float] = time.time, min_interval: float = MIN_INTERVAL,
                 per_hour: int = PER_HOUR):
        self.outbox = outbox
        self.inst = installation
        self.installation_id = installation_id
        self.clock = clock
        self.min_interval = min_interval
        self.per_hour = per_hour
        self._posted_at: Deque[float] = deque()
        self._last_pr: Optional[Tuple[str, int]] = None

    def _pace_wait(self, now: float) -> float:
        while self._posted_at and now - self._posted_at[0] >= 3600:
            self._posted_at.popleft()
        wait = 0.0
        if self._posted_at:
            wait = max(wait, self._posted_at[-1] + self.min_interval - now)
        if len(self._posted_at) >= self.per_hour:
            wait = max(wait, self._posted_at[0] + 3600 - now)
        return wait

    def _next_item(self, now: float):
        rows = self.outbox.records.conn().execute(
            "SELECT key, repo, pr, head, body, attempts FROM outbox WHERE installation_id = ? "
            "AND next_attempt_at <= ? ORDER BY created_at, key", (self.installation_id, int(now))
        ).fetchall()
        if not rows:
            return None
        prs = []
        for row in rows:
            if (row[1], row[2]) not in prs:
                prs.append((row[1], row[2]))
        # Round-robin: the first PR after the one served last, wrapping.
        start = 0
        if self._last_pr in prs:
            start = (prs.index(self._last_pr) + 1) % len(prs)
        target = prs[start]
        return next(r for r in rows if (r[1], r[2]) == target)

    def step(self) -> float:
        """Post or settle one due item. Returns seconds until the next call is useful."""
        now = self.clock()
        wait = self._pace_wait(now)
        if wait > 0:
            return wait
        item = self._next_item(now)
        if item is None:
            return 5.0
        key, repo, pr, head, body, attempts = item
        self._last_pr = (repo, pr)
        # Decide the item's fate inside the try, and settle it after: a check-run
        # failure is never read as a failure of the comment.
        try:
            reason = self._undeliverable(repo, pr)
            if reason is None:
                detector = self.outbox.scanner.first(body)
                if detector is None:
                    current = (self._pull or {}).get("head", {}).get("sha")
                    text = body
                    if current and current != head:
                        text = (f"Written against commit {head[:12]}; this PR has moved on "
                                f"since.\n\n" + body)
                    self.inst.comment(repo, pr, scan.sign(text))
        except GitHubError as exc:
            if exc.retryable:
                return self._retry(key, attempts, exc.retry_after)
            reason, detector = f"GitHub {exc.status}: {exc.message}"[:200], None
        if reason is not None:
            self._drop(key, repo, head, reason)
        elif detector is not None:
            self._withhold(key, repo, pr, head, body, detector)
        else:
            self._posted_at.append(self.clock())
            with self.outbox.records.tx() as c:
                c.execute("DELETE FROM outbox WHERE key = ?", (key,))
                self.outbox.check_runs.add(repo, head, conn=c, posted=1)
        self._publish(repo, head)
        return 0.0

    def _undeliverable(self, repo: str, pr: int) -> Optional[str]:
        self._pull = None
        info = self.inst.repo(repo)
        if info is None:
            return "the App can no longer see the repository"
        if is_public(info):
            return "the repository is public"
        pull = self.inst.pull(repo, pr)
        if pull is None:
            return "the pull request is gone"
        if pull.get("state") != "open":
            return "the pull request is closed"
        self._pull = pull
        return None

    def _publish(self, repo: str, head: str) -> None:
        try:
            self.outbox.check_runs.publish(repo, head, queued=self.outbox.queued(repo, head))
        except GitHubError:
            pass   # the item is settled; the run catches up at its next publish

    def _drop(self, key: str, repo: str, head: str, reason: str) -> None:
        with self.outbox.records.tx() as c:
            c.execute("DELETE FROM outbox WHERE key = ?", (key,))
            self.outbox._audit_drop(c, repo, key, reason)
            self.outbox.check_runs.add(repo, head, conn=c, dropped=1)

    def _withhold(self, key, repo, pr, head, body, detector) -> None:
        job_id, persona, _ = key.split("/", 2)
        with self.outbox.records.tx() as c:
            c.execute("DELETE FROM outbox WHERE key = ?", (key,))
            self.outbox._withhold(repo, pr, job_id, persona, detector, body, int(self.clock()), c)
            self.outbox.check_runs.add(repo, head, conn=c, withheld=1)

    def _retry(self, key: str, attempts: int, retry_after: Optional[int]) -> float:
        delay = retry_after if retry_after is not None else min(BACKOFF_MAX,
                                                                BACKOFF_BASE * 2 ** attempts)
        with self.outbox.records.tx() as c:
            c.execute("UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ? WHERE key = ?",
                      (int(self.clock()) + delay, key))
        return 0.0
```

- [ ] **Step 4: Run them to see them pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_outbox.py'`, `./test-python.sh -p 'test_hosted_github_app.py'`, then `./test-python.sh`
Expected: PASS, 26 and 33 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/outbox.py hosted/github_app.py tests/test_hosted_outbox.py tests/test_hosted_github_app.py
git commit -m "hosted: the outbox, its acceptance caps and its paced drain"
```

### Task 4: The commit point

**Files:**
- Create: `hosted/commit.py`
- Modify: `reviewer/state.py` (`StateStore.forget`)
- Test: `tests/test_hosted_commit.py`

**Interfaces:**
- Consumes: `records.Records`, `blobs.transcript_name` (2a.1); `decisions.Decider` and `common.Pair` (`reviewer/`).
- Produces:
  - The outcome names `OK`, `CAPPED`, `USAGE_LIMITED`, `DEFERRED`, `FAILING`, `FAILED`, `REVOKED`, and `OUTCOMES`; `PROFILE_FAILURES_TO_PARK = 3`; `REPO_DEFERS_TO_PAGE = 3`.
  - `Record` (frozen) with `from_row(row)` and `counts() -> dict` (the `counts` a gateway `hello` carries).
  - `ensure_record(conn, credential_id)`, `record_for(records, credential_id) -> Optional[Record]`, `apply_report(records, msg) -> {ack} | {error}`, `flush_complete(flush, record) -> bool`, `transcript_session(data: bytes) -> Optional[str]`.
  - `rejected_only(record) -> bool`; `decide(record, flush_ok, transcript, reported_session, expected_session) -> str`, which never returns `REVOKED`.
  - `Park(reason, until=None, head=None)`; `Completion(repo, pair, head, profile, credential_id, transcript, session_id, expected_session, flush, signal=None, job_id=None)` (frozen).
  - `Committer(records, blob_store, decider_for, backoff_for, clock=time.time, log=print, job_live=lambda job_id: True)` with `commit(completion) -> str`, `apply(completion, outcome, record)`, `revoke(repo, pair) -> str` (returns `REVOKED`; the caller publishes it), `parked(repo, pair, head) -> Optional[Park]`, `profile_parked_until(profile) -> int`, `transcript_for(repo, pair) -> Optional[bytes]`, `last_reviewed_head(repo, pair) -> Optional[str]` (the committed fingerprint's head), `forget_repo(repo)`, and the attributes `parks`, `transcripts` and `profiles`. `commit` answers `REVOKED` for a completion whose `job_id` is not `job_live`, and applies nothing.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_commit.py`:

```python
import _path  # noqa: F401

import json
import os
import shutil
import tempfile
from signals import Signal
import unittest

import commit
import creds
from commit import (Completion, Committer, Record, apply_report, decide, flush_complete,
                    record_for, transcript_session)
from common import Pair
from decisions import Decider
from records import Records
from state import MemoryStateStore

HEAD = "a" * 40
SID = "11111111-2222-3333-4444-555555555555"


def transcript(sid=SID, turns=1):
    lines = [json.dumps({"sessionId": sid, "type": "user", "n": i}) for i in range(turns)]
    return ("\n".join(lines) + "\n").encode()


def rec(**over):
    base = dict(seq=1, requests=1, successes=1)
    base.update(over)
    return Record(**base)


class DecideTest(unittest.TestCase):
    def d(self, record=None, flush_ok=True, data=None, reported=SID, expected=None):
        return decide(record, flush_ok, transcript() if data is None else data, reported,
                      expected)

    def test_the_spec_order(self):
        cases = [
            ("ok", dict(record=rec()), "ok"),
            ("bad transcript beats everything", dict(record=rec(cap_hit="tokens"), data=b"not json\n"),
             "failing"),
            ("wrong session id", dict(record=rec(), reported="other"), "failing"),
            ("not the pair's session", dict(record=rec(), expected="another-session"), "failing"),
            ("incomplete flush", dict(record=rec(), flush_ok=False), "deferred"),
            ("empty record", dict(record=rec(requests=0, successes=0)), "deferred"),
            ("no record", dict(record=None), "deferred"),
            ("failures, no success", dict(record=rec(successes=0, first_failure="upstream 502")),
             "failed"),
            ("failures with a success", dict(record=rec(first_failure="upstream 502")), "ok"),
            ("cap hit", dict(record=rec(cap_hit="requests")), "capped"),
            ("usage missing", dict(record=rec(usage_missing=True)), "capped"),
            ("limit", dict(record=rec(first_limit="upstream 429")), "usage-limited"),
            ("rejected only", dict(record=rec(successes=0, first_rejected="upstream 400")), "deferred"),
            ("rejected with a success", dict(record=rec(first_rejected="upstream 400")), "ok"),
            ("abandoned replaces ok", dict(record=rec(first_abandoned="hung up")), "deferred"),
            ("abandoned never beats a cap", dict(record=rec(first_abandoned="x", cap_hit="requests")),
             "capped"),
            ("abandoned never beats usage missing",
             dict(record=rec(first_abandoned="x", usage_missing=True)), "capped"),
            ("abandoned never beats a limit", dict(record=rec(first_abandoned="x", first_limit="l")),
             "usage-limited"),
        ]
        for name, kw, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.d(**kw), expected)

    def test_a_pass_that_never_started_is_a_claudebox_fault(self):
        self.assertEqual(decide(None, True, None, None, None), "deferred")
        self.assertEqual(decide(rec(requests=0, successes=0), True, None, None, None), "deferred")

    def test_a_missing_transcript_after_real_traffic_is_failing(self):
        self.assertEqual(decide(rec(), True, None, SID, None), "failing")


class TranscriptTest(unittest.TestCase):
    def test_a_session_parses_and_names_its_id(self):
        self.assertEqual(transcript_session(transcript(turns=3)), SID)
        self.assertEqual(transcript_session(b'\n{"type":"summary"}\n' + transcript()), SID)

    def test_anything_else_does_not_parse(self):
        for data in (b"", b"[1]\n", b'{"sessionId": "../x"}\n', b'{"a": 1}\n',
                     transcript() + b"garbage\n"):
            with self.subTest(data=data):
                self.assertIsNone(transcript_session(data))


class FlushTest(unittest.TestCase):
    def test_complete_only_when_drained_and_nothing_uncommitted(self):
        self.assertTrue(flush_complete({"last_seq": 3, "drained": True}, rec(seq=3)))
        self.assertTrue(flush_complete({"last_seq": 0, "drained": True}, None))
        self.assertFalse(flush_complete({"last_seq": 4, "drained": True}, rec(seq=3)))
        self.assertFalse(flush_complete({"last_seq": 3, "drained": False}, rec(seq=3)))
        self.assertFalse(flush_complete(None, rec()))
        self.assertFalse(flush_complete({"error": "hello first", "ready": False}, rec()))


class ReportTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.records = Records(os.path.join(d, "r.db"))
        self.addCleanup(self.records.close)
        token = creds.Creds(self.records).mint("provider", "job1", 600, persona="sage")
        self.cid = creds.credential_id_of(token)

    def report(self, seq, **extra):
        msg = {"credential_id": self.cid, "seq": seq,
               "delta": {"requests": 1, "successes": 1, "input_tokens": 10, "output_tokens": 5}}
        msg.update(extra)
        return apply_report(self.records, msg)

    def test_reports_commit_in_order(self):
        self.assertEqual(self.report(1), {"ack": 1})
        self.assertEqual(self.report(2, first_limit="upstream 429"), {"ack": 2})
        r = record_for(self.records, self.cid)
        self.assertEqual((r.seq, r.requests, r.input_tokens, r.first_limit), (2, 2, 20, "upstream 429"))

    def test_a_resend_changes_nothing_and_acks_where_the_record_stands(self):
        self.report(1)
        self.assertEqual(self.report(1), {"ack": 1})
        self.assertEqual(record_for(self.records, self.cid).requests, 1)

    def test_a_gap_is_not_applied(self):
        self.assertEqual(self.report(2), {"ack": 0})
        self.assertIsNotNone(record_for(self.records, self.cid))
        self.assertEqual(record_for(self.records, self.cid).requests, 0)

    def test_a_first_marker_is_never_overwritten(self):
        self.report(1, first_failure="upstream 502")
        self.report(2, first_failure="upstream 500")
        self.assertEqual(record_for(self.records, self.cid).first_failure, "upstream 502")

    def test_usage_missing_sticks(self):
        self.report(1, usage_missing=True)
        self.report(2)
        self.assertTrue(record_for(self.records, self.cid).usage_missing)

    def test_an_unknown_token_or_a_bad_message_is_refused(self):
        self.assertIn("error", apply_report(self.records, {"credential_id": "f" * 16, "seq": 1}))
        self.assertIn("error", apply_report(self.records, {"seq": 1}))

    def test_counts_round_trip_into_a_hello(self):
        self.report(1, cap_hit="tokens")
        counts = record_for(self.records, self.cid).counts()
        self.assertEqual((counts["seq"], counts["requests"], counts["cap_hit"]), (1, 1, "tokens"))
        self.assertIs(counts["usage_missing"], False)


class FakeBlobs:
    def __init__(self):
        self.data = {}

    def put(self, name, data):
        self.data[name] = data

    def get(self, name):
        return self.data.get(name)

    def delete(self, name):
        return self.data.pop(name, None) is not None


class CommitterTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        self.records = Records(os.path.join(d, "r.db"))
        self.addCleanup(self.records.close)
        self.creds = creds.Creds(self.records)
        self.state = MemoryStateStore()
        self.decider = Decider(self.state, {"code": ["sage"], "plan": []},
                               {("code", "sage"): "you are sage"}, {}, 0, 4)
        self.blobs = FakeBlobs()
        self.now = [1000]
        self.logs = []
        self.c = Committer(self.records, self.blobs, lambda repo: self.decider, lambda p: 1800,
                           clock=lambda: self.now[0], log=self.logs.append)
        self.pair = Pair(7, "code", "sage")

    def complete(self, report=None, data=None, session=SID, expected=None, pr=7, repo="acme/web",
                 flush=None, signal="fp1", head=HEAD):
        token = self.creds.mint("provider", "job1", 600, persona="sage")
        cid = creds.credential_id_of(token)
        seq = 0
        if report is not None:
            msg = {"credential_id": cid, "seq": 1, "delta": {"requests": 1, "successes": 1}}
            msg.update(report)
            apply_report(self.records, msg)
            seq = 1
        pair = Pair(pr, "code", "sage")
        return self.c.commit(Completion(
            repo, pair, head, "ollama", cid, transcript() if data is None else data, session,
            expected, flush or {"last_seq": seq, "drained": True}, signal))

    def test_decider_for_is_never_called_under_the_committers_lock(self):
        # Lock order (PR #22 review): decider_for takes the scheduler's lock,
        # and the scheduler calls into the committer.
        import threading
        held = []
        real = self.c.decider_for

        def probe(repo):
            free = []
            t = threading.Thread(target=lambda: free.append(self.c.lock.acquire(timeout=0.5)
                                                            and (self.c.lock.release() or True)))
            t.start()
            t.join()
            held.append(not free[0])
            return real(repo)
        self.c.decider_for = probe
        self.complete({})
        self.c.revoke("acme/web", self.pair)
        self.c.forget_repo("acme/web")
        self.assertEqual(held and set(held), {False})

    def test_a_completion_whose_job_has_ended_commits_nothing(self):
        # A completion in flight when its repo is lost (PR #22 review) must not
        # write back what the loss deleted.
        self.c.job_live = lambda job_id: False
        token = self.creds.mint("provider", "job1", 600, persona="sage")
        cid = creds.credential_id_of(token)
        c = Completion("acme/web", self.pair, HEAD, "ollama", cid, transcript(), SID, None,
                       {"last_seq": 0, "drained": True}, "fp1", "job1")
        self.assertEqual(self.c.commit(c), "revoked")
        self.assertEqual((self.blobs.data, self.state.sessions, self.state.rounds), ({}, {}, {}))

    def test_a_rejected_only_pass_is_deferred_with_a_fresh_session(self):
        # PR #18 review: a 4xx can be claudebox's request or an overgrown
        # session, so it never parks the pair, and the session is not resumed.
        self.state.sessions[self.pair] = "old"
        out = self.complete({"first_rejected": "upstream 400 invalid_request_error",
                             "delta": {"requests": 1, "successes": 0}})
        self.assertEqual(out, "deferred")
        self.assertNotIn(self.pair, self.state.sessions)
        self.assertEqual(self.blobs.data, {})
        self.assertIn(self.pair, self.state.owed)
        self.assertNotIn(("acme/web", self.pair), {k: v for k, v in self.c.parks.items()
                                                  if v.head is not None})
        self.assertFalse(any(l.startswith("PAGE:") for l in self.logs))

    def test_the_store_forgets_everything_but_rounds_and_clears_in_place(self):
        from state import MemoryStateStore
        store = MemoryStateStore()
        owed = store.owed
        store.sessions[self.pair], store.rounds[self.pair] = "s", 2
        store.reviewed[self.pair], store.passes_done[self.pair] = "fp", 1
        owed.add(self.pair)
        store.cut_group = (7, "code")
        store.forget()
        self.assertEqual((store.sessions, store.reviewed, store.passes_done, store.cut_group),
                         ({}, {}, {}, None))
        self.assertIs(store.owed, owed)
        self.assertEqual(owed, set())
        self.assertEqual(store.rounds, {self.pair: 2})

    def test_ok_commits_everything_together(self):
        self.assertEqual(self.complete({}), "ok")
        self.assertEqual(self.state.sessions[self.pair], SID)
        self.assertEqual(self.state.rounds[self.pair], 1)
        self.assertEqual(self.state.reviewed[self.pair], "fp1")
        self.assertNotIn(self.pair, self.state.owed)
        self.assertEqual(self.c.transcript_for("acme/web", self.pair), transcript())

    def test_the_last_reviewed_head_is_the_committed_fingerprints(self):
        # One source for the round line's head: state.reviewed (PR #21 review).
        self.assertIsNone(self.c.last_reviewed_head("acme/web", self.pair))
        self.complete({}, signal=Signal(HEAD, "code", ""))
        self.assertEqual(self.c.last_reviewed_head("acme/web", self.pair), HEAD)
        self.complete({}, signal=None)    # an ok with no fingerprint moves nothing
        self.assertEqual(self.c.last_reviewed_head("acme/web", self.pair), HEAD)

    def test_a_newer_transcript_replaces_the_older_blob(self):
        self.complete({})
        self.complete({}, data=transcript(turns=2), expected=SID)
        self.assertEqual(len(self.blobs.data), 1)
        self.assertEqual(self.c.transcript_for("acme/web", self.pair), transcript(turns=2))

    def test_capped_keeps_the_session_parks_on_the_head_and_pages(self):
        self.assertEqual(self.complete({"cap_hit": "requests"}), "capped")
        self.assertEqual(self.state.sessions[self.pair], SID)
        self.assertNotIn(self.pair, self.state.rounds)
        self.assertNotIn(self.pair, self.state.reviewed)
        self.assertIn(self.pair, self.state.owed)
        self.assertIsNotNone(self.c.parked("acme/web", self.pair, HEAD))
        self.assertIsNone(self.c.parked("acme/web", self.pair, "b" * 40))
        self.assertTrue(any(l.startswith("PAGE: acme/web#7 code/sage capped (requests)")
                            for l in self.logs))

    def test_failing_discards_the_transcript_and_drops_the_session(self):
        self.complete({})
        self.assertEqual(self.complete({}, data=b"not json\n", expected=SID), "failing")
        self.assertNotIn(self.pair, self.state.sessions)
        self.assertIsNone(self.c.transcript_for("acme/web", self.pair))
        self.assertEqual(self.blobs.data, {})
        self.assertTrue(any(l.startswith("PAGE: ") and "failing" in l for l in self.logs))

    def test_timed_parks_lift_after_the_backoff(self):
        self.assertEqual(self.complete({"first_limit": "upstream 429"}), "usage-limited")
        self.assertEqual(self.c.parked("acme/web", self.pair, HEAD).until, 2800)
        self.now[0] = 2800
        self.assertIsNone(self.c.parked("acme/web", self.pair, HEAD))

    def test_limits_on_two_prs_inside_the_window_park_the_profile(self):
        self.complete({"first_limit": "x"}, pr=7)
        self.assertEqual(self.c.profile_parked_until("ollama"), 0)
        self.now[0] += 100
        self.complete({"first_limit": "x"}, pr=8)
        self.assertEqual(self.c.profile_parked_until("ollama"), 1100 + 1800)

    def test_limits_on_same_numbered_prs_in_two_repos_park_the_profile(self):
        # A profile spans repos, and every repo has a #7 (PR #21 review).
        self.complete({"first_limit": "x"}, pr=7)
        self.complete({"first_limit": "x"}, pr=7, repo="acme/api")
        self.assertGreater(self.c.profile_parked_until("ollama"), 0)

    def test_limits_outside_the_window_do_not(self):
        self.complete({"first_limit": "x"}, pr=7)
        self.now[0] += 1800
        self.complete({"first_limit": "x"}, pr=8)
        self.assertEqual(self.c.profile_parked_until("ollama"), 0)

    def test_three_failed_pairs_in_a_row_park_the_profile_and_a_success_resets(self):
        fail = {"delta": {"requests": 1, "successes": 0}, "first_failure": "upstream 502"}
        self.complete(fail)
        self.complete(fail)
        self.complete({})
        self.complete(fail)
        self.complete(fail)
        self.assertEqual(self.c.profile_parked_until("ollama"), 0)
        self.complete(fail)
        self.assertGreater(self.c.profile_parked_until("ollama"), 0)

    def test_a_repo_deferred_three_times_in_a_row_pages(self):
        for _ in range(3):
            self.complete({}, flush={"last_seq": 1, "drained": False})
        self.assertTrue(any("deferred 3 times in a row" in l for l in self.logs))

    def test_revoke_discards_and_owes(self):
        self.complete({})
        self.assertEqual(self.c.revoke("acme/web", self.pair), "revoked")
        self.assertIn(self.pair, self.state.owed)
        self.assertNotIn(self.pair, self.state.sessions)
        self.assertEqual(self.blobs.data, {})

    def test_forget_repo_deletes_its_transcripts(self):
        self.complete({})
        self.complete({"first_limit": "x"}, pr=8)
        self.state.rounds[self.pair] = 3
        self.c.forget_repo("acme/web")
        self.assertEqual(self.blobs.data, {})
        self.assertEqual(self.c.parks, {})
        # The spec's Visibility list names sessions, and owed pairs go "since
        # nothing can serve them"; rounds hold no content and survive.
        self.assertEqual((self.state.sessions, self.state.reviewed, self.state.owed),
                         ({}, {}, set()))
        self.assertEqual(self.state.rounds[self.pair], 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_commit.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'commit'`.

- [ ] **Step 3: Implement**

Apply this change to `reviewer/state.py`:

```diff
diff --git a/reviewer/state.py b/reviewer/state.py
index 56137dc..306452f 100644
--- a/reviewer/state.py
+++ b/reviewer/state.py
@@ -79,6 +79,17 @@ class StateStore(ABC):
     @abstractmethod
     def cut_group(self, value: Optional[Tuple[int, str]]) -> None: ...
 
+    @abstractmethod
+    def forget(self) -> None:
+        """Drop everything remembered except `rounds`.
+
+        The hosted control plane's answer to a repo losing access: sessions,
+        fingerprints, pass counts, owed pairs and the cut all go, and rounds
+        stay, since they hold no content and "survive a public flip" (the
+        hosted spec's "Visibility"). The policy lives here, beside the fields
+        it names, so a field added later is forgotten or kept on purpose.
+        """
+
 
 class MemoryStateStore(StateStore):
     """Today's dicts. Lost on restart, deliberately.
@@ -128,3 +139,10 @@ class MemoryStateStore(StateStore):
     @cut_group.setter
     def cut_group(self, value: Optional[Tuple[int, str]]) -> None:
         self._cut_group = value
+
+    def forget(self) -> None:
+        self._sessions.clear()
+        self._passes_done.clear()
+        self._reviewed.clear()
+        self._owed.clear()
+        self._cut_group = None
```

`hosted/commit.py`:

```python
"""The commit point: one outcome per completed pair, and what it does to state.

Three pieces. `apply_report` commits a gateway report to the token's row, in
order, idempotently. `decide` is the spec's outcome order, stated once: a
transcript that will not parse or a session id that is not the pair's is
`failing`; otherwise an incomplete flush or an empty record is `deferred`;
otherwise upstream failures with no success are `failed`; otherwise the
record decides. `Committer` applies the outcome's row of the spec's table to
the repo's decision core and to the parks this module keeps.

Pair state lives in memory in 2a (overview decision 1): the Decider's
StateStore, and the parks here. Token rows are the records store's, because
the gateway reports them while a job runs and they must survive a gateway
restart. Transcripts are encrypted blobs; a pair points at its newest one.
"""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import blobs as blobs_mod
from common import Pair

OK = "ok"
CAPPED = "capped"
USAGE_LIMITED = "usage-limited"
DEFERRED = "deferred"
FAILING = "failing"
FAILED = "failed"
REVOKED = "revoked"
OUTCOMES = (OK, CAPPED, USAGE_LIMITED, DEFERRED, FAILING, FAILED, REVOKED)

# Outcomes whose transcript is committed (the pair can resume from it).
_KEEPS_TRANSCRIPT = (OK, CAPPED, USAGE_LIMITED, DEFERRED)
# Parked until the head moves: these wait on a person (or a push).
_PARK_ON_HEAD = (CAPPED, FAILING)
# Parked for the profile's limit backoff.
_PARK_TIMED = (USAGE_LIMITED, DEFERRED, FAILED)
# Pages, by the spec's paging list.
_PAGES = (CAPPED, FAILING)

# The profile-level rules (spec, "The gateway").
PROFILE_FAILURES_TO_PARK = 3
REPO_DEFERS_TO_PAGE = 3

_SESSION_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_REPORT_FIELDS = ("first_limit", "first_failure", "first_rejected", "first_abandoned", "cap_hit")
_DELTA_FIELDS = ("requests", "successes", "input_tokens", "output_tokens")


@dataclass(frozen=True)
class Record:
    """A token's committed gateway record."""
    seq: int = 0
    requests: int = 0
    successes: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    first_limit: Optional[str] = None
    first_failure: Optional[str] = None
    first_rejected: Optional[str] = None
    first_abandoned: Optional[str] = None
    cap_hit: Optional[str] = None
    usage_missing: bool = False

    @classmethod
    def from_row(cls, row) -> "Record":
        d = dict(row)
        d.pop("credential_id", None)
        d["usage_missing"] = bool(d.get("usage_missing"))
        return cls(**d)

    def counts(self) -> Dict[str, Any]:
        """The `counts` a gateway hello carries for this token."""
        d = {k: getattr(self, k) for k in ("seq",) + _DELTA_FIELDS + _REPORT_FIELDS}
        d["usage_missing"] = self.usage_missing
        return d


def ensure_record(conn, credential_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO token_records (credential_id) VALUES (?)", (credential_id,))


def record_for(records, credential_id: str) -> Optional[Record]:
    row = records.conn().execute("SELECT * FROM token_records WHERE credential_id = ?",
                                 (credential_id,)).fetchone()
    return Record.from_row(row) if row is not None else None


def apply_report(records, msg: Dict[str, Any]) -> Dict[str, Any]:
    """Commit one gateway report. Answers `{ack: seq}` with the token's committed seq.

    Reports arrive in order per token. A seq at or below the committed one is a
    resend and changes nothing. A seq more than one ahead is a gap the gateway
    would never produce, so it is not applied, and the ack tells the gateway
    where the record really stands.
    """
    cid = msg.get("credential_id")
    seq = msg.get("seq")
    if not isinstance(cid, str) or not isinstance(seq, int):
        return {"error": "a report needs credential_id and seq"}
    with records.tx() as c:
        row = c.execute("SELECT seq FROM token_records WHERE credential_id = ?", (cid,)).fetchone()
        if row is None:
            known = c.execute("SELECT 1 FROM credentials WHERE id = ? AND kind = 'provider'",
                              (cid,)).fetchone()
            if known is None:
                return {"error": "unknown token"}
            ensure_record(c, cid)
            committed = 0
        else:
            committed = row[0]
        if seq != committed + 1:
            return {"ack": committed}
        delta = msg.get("delta") or {}
        sets = ["seq = ?"] + [f"{k} = {k} + ?" for k in _DELTA_FIELDS]
        args: List[Any] = [seq] + [int(delta.get(k, 0)) for k in _DELTA_FIELDS]
        for k in _REPORT_FIELDS:
            if isinstance(msg.get(k), str):
                sets.append(f"{k} = COALESCE({k}, ?)")
                args.append(msg[k][:500])
        if msg.get("usage_missing"):
            sets.append("usage_missing = 1")
        c.execute(f"UPDATE token_records SET {', '.join(sets)} WHERE credential_id = ?",
                  args + [cid])
    return {"ack": seq}


def flush_complete(flush: Optional[Dict[str, Any]], record: Optional[Record]) -> bool:
    """The spec's complete flush: drained, and nothing emitted that is not committed."""
    if not flush or flush.get("drained") is not True or not isinstance(flush.get("last_seq"), int):
        return False
    return flush["last_seq"] == (record.seq if record else 0)


def transcript_session(data: bytes) -> Optional[str]:
    """The session id of a transcript that parses as a Claude Code session, or None.

    Every non-empty line must be a JSON object, and the first `sessionId` names
    the session. Anything else will not parse.
    """
    sid = None
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            return None
        if not isinstance(entry, dict):
            return None
        if sid is None and isinstance(entry.get("sessionId"), str):
            sid = entry["sessionId"]
    if sid is None or not _SESSION_RE.fullmatch(sid):
        return None
    return sid


def decide(record: Optional[Record], flush_ok: bool, transcript: Optional[bytes],
           reported_session: Optional[str], expected_session: Optional[str]) -> str:
    """The outcome of a pair that reached complete-pair, in the spec's order.

    `expected_session` is the session the pair was dispatched to resume, or
    None for a fresh one. A transcript that is absent altogether (the pass
    never started claude) with an empty record is a claudebox fault, not a
    pair failure: nothing parsed badly, nothing happened at all. `revoked` is
    never decided here: the spec decides it "elsewhere, without a complete
    pair", which is Committer.revoke.
    """
    empty = record is None or record.requests == 0
    if transcript is None and empty:
        return DEFERRED
    sid = transcript_session(transcript) if transcript is not None else None
    if sid is None or sid != reported_session or (expected_session not in (None, sid)):
        return FAILING
    if not flush_ok or empty:
        return DEFERRED
    if record.first_failure and record.successes == 0:
        return FAILED
    if record.cap_hit or record.usage_missing:
        return CAPPED
    if record.first_limit:
        return USAGE_LIMITED
    if rejected_only(record):
        # A 4xx does not say who built the part the provider refused: the
        # gateway's request shape and an overgrown session both look like
        # this. So it is a claudebox fault, never the pair's (PR #18 review).
        return DEFERRED
    if record.first_abandoned:
        return DEFERRED
    return OK


def rejected_only(record: Optional[Record]) -> bool:
    """Every upstream answer was a rejected request: no success and no failure."""
    return (record is not None and bool(record.first_rejected) and record.successes == 0
            and not record.first_failure)


@dataclass
class Park:
    reason: str                     # an outcome name
    until: Optional[int] = None     # timed parks
    head: Optional[str] = None      # head parks: cleared when the PR's head differs


@dataclass
class _Profile:
    failed_in_a_row: int = 0
    parked_until: int = 0
    # (repo, pr) -> when: a profile spans every repo, and every repo has a #1.
    recent_limits: Dict[Tuple[str, int], int] = field(default_factory=dict)


@dataclass(frozen=True)
class Completion:
    """Everything the commit point needs about one completed pair."""
    repo: str
    pair: Pair
    head: str
    profile: str
    credential_id: str
    transcript: Optional[bytes]
    session_id: Optional[str]
    expected_session: Optional[str]
    flush: Optional[Dict[str, Any]]
    signal: Any = None              # the fingerprint an ok commits, its head included
    job_id: Optional[str] = None    # checked against job_live under the commit point's lock


class Committer:
    def __init__(self, records, blob_store, decider_for: Callable[[str], Any],
                 backoff_for: Callable[[str], int], clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda line: print(line, flush=True),
                 job_live: Callable[[str], bool] = lambda job_id: True):
        self.records = records
        # Whether a job is still registered. A completion whose job has ended
        # (its repo lost, its deadline passed) commits nothing: the job's end
        # already revoked the pair, and a loss may already have deleted what
        # this would write back.
        self.job_live = job_live
        self.blobs = blob_store
        self.decider_for = decider_for
        self.backoff_for = backoff_for
        self.clock = clock
        self.log = log
        self.lock = threading.RLock()   # commit holds it across apply, which takes it too
        self.parks: Dict[Tuple[str, Pair], Park] = {}
        self.transcripts: Dict[Tuple[str, Pair], str] = {}
        self.profiles: Dict[str, _Profile] = {}
        self.defers_in_a_row: Dict[str, int] = {}

    def commit(self, c: Completion) -> str:
        """Decide and apply a pair's outcome.

        The channel's `job.completed` is the one record that a pair is done, so
        a resend never reaches here. A completion whose job has ended commits
        nothing and answers `revoked`, checked under this lock, which
        `forget_repo` and `revoke` also take.
        """
        # Lock order: the decider is resolved before this lock is taken, never
        # under it, because decider_for takes the scheduler's lock and the
        # scheduler calls in here.
        decider = self.decider_for(c.repo)
        with self.lock:
            if c.job_id is not None and not self.job_live(c.job_id):
                return REVOKED
            record = record_for(self.records, c.credential_id)
            outcome = decide(record, flush_complete(c.flush, record), c.transcript, c.session_id,
                             c.expected_session)
            self.apply(c, outcome, record, decider)
            return outcome

    def revoke(self, repo: str, pair: Pair) -> str:
        """`revoked`: a started pair whose job ended without complete-pair.

        A timeout, an access change or a restart. Its transcript is discarded and
        it stays owed. The caller, which knows the head, shows it on the check
        run (checkruns.REVOKED).
        """
        decider = self.decider_for(repo)   # before the lock: see commit
        with self.lock:
            self._drop_transcript(repo, pair)
            decider.record_failure(pair)
            decider.state.owed.add(pair)
        return REVOKED

    def apply(self, c: Completion, outcome: str, record: Optional[Record],
              decider=None) -> None:
        key = (c.repo, c.pair)
        now = int(self.clock())
        decider = decider if decider is not None else self.decider_for(c.repo)
        # A rejected-only pass is deferred with a fresh session: if the session
        # is what the provider refused (past its context window), resuming it
        # would be refused the same way.
        keeps = outcome in _KEEPS_TRANSCRIPT and not (outcome == DEFERRED and rejected_only(record))
        with self.lock:
            if keeps and c.transcript is not None:
                name = blobs_mod.transcript_name(c.repo, c.pair.pr, c.pair.mode, c.pair.persona, now)
                self.blobs.put(name, c.transcript)
                old = self.transcripts.get(key)
                self.transcripts[key] = name
                if c.session_id:
                    decider.state.sessions[c.pair] = c.session_id
                if old:
                    self.blobs.delete(old)
            elif not keeps:
                self._drop_transcript(c.repo, c.pair)
                decider.record_failure(c.pair)
            profile = self.profiles.setdefault(c.profile, _Profile())
            if outcome == OK:
                decider.record_success(c.pair, _Result(c.session_id), c.signal)
                decider.state.owed.discard(c.pair)
                self.parks.pop(key, None)
            else:
                decider.state.owed.add(c.pair)
            if record is not None and record.successes > 0:
                # Any success on the profile resets every pair's failure count.
                profile.failed_in_a_row = 0
            if outcome in _PARK_ON_HEAD:
                self.parks[key] = Park(outcome, head=c.head)
            elif outcome in _PARK_TIMED:
                self.parks[key] = Park(outcome, until=now + self.backoff_for(c.profile))
            if outcome == FAILED:
                profile.failed_in_a_row += 1
                if profile.failed_in_a_row >= PROFILE_FAILURES_TO_PARK:
                    profile.parked_until = now + self.backoff_for(c.profile)
                    self.log(f"profile {c.profile}: {profile.failed_in_a_row} failed pairs in a "
                             f"row; parked until {profile.parked_until}")
            if outcome == USAGE_LIMITED:
                window = self.backoff_for(c.profile)
                profile.recent_limits = {pr: t for pr, t in profile.recent_limits.items()
                                         if now - t < window}
                profile.recent_limits[(c.repo, c.pair.pr)] = now
                if len(profile.recent_limits) >= 2:
                    profile.parked_until = now + window
                    self.log(f"profile {c.profile}: usage limits on two PRs; parked until "
                             f"{profile.parked_until}")
            if outcome == DEFERRED:
                n = self.defers_in_a_row[c.repo] = self.defers_in_a_row.get(c.repo, 0) + 1
                if n >= REPO_DEFERS_TO_PAGE:
                    self.log(f"PAGE: {c.repo} deferred {n} times in a row, a claudebox fault")
            elif outcome == OK:
                self.defers_in_a_row[c.repo] = 0
            if outcome in _PAGES:
                why = ""
                if record is not None:
                    why = record.cap_hit or ("usage missing" if record.usage_missing else "")
                self.log(f"PAGE: {c.repo}#{c.pair.pr} {c.pair.mode}/{c.pair.persona} {outcome}"
                         + (f" ({why})" if why else ""))

    def _drop_transcript(self, repo: str, pair: Pair) -> None:
        name = self.transcripts.pop((repo, pair), None)
        if name:
            self.blobs.delete(name)

    def parked(self, repo: str, pair: Pair, head: str) -> Optional[Park]:
        """The pair's park, if it still holds at this head and time; clears it otherwise."""
        key = (repo, pair)
        with self.lock:
            park = self.parks.get(key)
            if park is None:
                return None
            now = int(self.clock())
            if (park.head is not None and park.head != head) or \
                    (park.until is not None and now >= park.until):
                del self.parks[key]
                return None
            return park

    def profile_parked_until(self, profile: str) -> int:
        p = self.profiles.get(profile)
        return p.parked_until if p and p.parked_until > int(self.clock()) else 0

    def transcript_for(self, repo: str, pair: Pair) -> Optional[bytes]:
        name = self.transcripts.get((repo, pair))
        return self.blobs.get(name) if name else None

    def last_reviewed_head(self, repo: str, pair: Pair) -> Optional[str]:
        """The head of the pair's committed fingerprint: the spec's one source for it."""
        signal = self.decider_for(repo).state.reviewed.get(pair)
        return getattr(signal, "head_oid", None) or None

    def forget_repo(self, repo: str) -> None:
        """Loss of access: the repo's transcripts, sessions and pair state go; its rounds stay.

        The spec's "Visibility": every content-bearing record keyed to the repo,
        and its owed pairs, "since nothing can serve them". Rounds hold no
        content and "survive a public flip". Outbox items and withheld records
        are rows, and Outbox.drop_repo deletes them.
        """
        decider = self.decider_for(repo)   # before the lock: see commit
        with self.lock:
            for key in [k for k in self.transcripts if k[0] == repo]:
                self.blobs.delete(self.transcripts.pop(key))
            for key in [k for k in self.parks if k[0] == repo]:
                del self.parks[key]
            self.defers_in_a_row.pop(repo, None)
            for profile in self.profiles.values():
                for key in [k for k in profile.recent_limits if k[0] == repo]:
                    del profile.recent_limits[key]
            decider.state.forget()   # the store's own policy: everything but rounds


@dataclass(frozen=True)
class _Result:
    """The slice of passes.PassResult the decision core's record_success reads."""
    session_id: Optional[str]
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_commit.py'`, then `./test-python.sh`
Expected: PASS, 30 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/commit.py tests/test_hosted_commit.py
git commit -m "hosted: the commit point, from the gateway's record to the pair's state"
```

### Task 5: The worker channel

**Files:**
- Create: `hosted/channel.py`
- Test: `tests/test_hosted_channel.py`

**Interfaces:**
- Consumes: `creds.Creds` and `github_app.GitHubError` (2a.1); 2a.2's gateway ops (`add_token`, `flush`, `revoke`) through an injected `gateway_call(op, **fields)`; Tasks 2–4.
- Produces:
  - `JOB_DEADLINE = 3300`, `BODY_MAX = 1048576`, `COMPLETE_BODY_MAX = 100663296`, `TRANSCRIPT_MAX = 67108864`.
  - `PairMaterial(persona, phase, system_prompt, prompt, session_id=None, signal=None)` and `JobMaterial(...)` with `pair_for(persona)`, the in-memory record of a dispatch, which 2a.4b's scheduler builds. Its `started` maps persona to credential id, `completed` maps persona to outcome, and `busy` with `lock` reserves a pair between its check and its record.
  - `Jobs` with `add`, `get`, `remove` and `all`.
  - `Channel(records, creds, jobs, outbox, committer, gateway_call, app, installation_for, check_runs, transcript_for, clock=time.time, log=print)` with `handle(path, token, body) -> (status, dict)`.
  - `make_server(channel, host, port) -> ThreadingHTTPServer`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_channel.py`:

```python
import _path  # noqa: F401

import base64
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest

import channel
import checkruns
import commit
import creds
from channel import Channel, JobMaterial, Jobs, PairMaterial
from commit import Committer, apply_report
from common import Pair
from decisions import Decider
from github_app import GitHubError
from outbox import Outbox
from records import Records
from scan import Scanner
from state import MemoryStateStore

HEAD = "a" * 40
SID = "11111111-2222-3333-4444-555555555555"
ROUTE = {"name": "ollama", "provider": "ollama", "base_url": None, "auth": None,
         "credential": "OLLAMA_API_KEY", "per_pair_requests": 200, "per_pair_tokens": 4000000,
         "budget_unit": "tokens"}


def transcript(sid=SID):
    return (json.dumps({"sessionId": sid}) + "\n").encode()


class FakeApp:
    def __init__(self):
        self.minted = []
        self.fail = None

    def read_token(self, installation_id, repo_id):
        if self.fail:
            raise self.fail
        self.minted.append((installation_id, repo_id))
        return "ghs_read", 0


class FakeInst:
    def __init__(self):
        self.info = {"private": True}
        self.fail = None

    def repo(self, name):
        if self.fail:
            raise self.fail
        return self.info


class FakeGateway:
    def __init__(self, records):
        self.records = records
        self.calls = []
        self.refuse_add = False

    def __call__(self, op, **fields):
        self.calls.append((op, fields))
        if op == "add_token":
            return {"error": "no"} if self.refuse_add else {"ok": True}
        if op == "flush":
            row = self.records.conn().execute("SELECT seq FROM token_records WHERE credential_id = ?",
                                              (fields["credential_id"],)).fetchone()
            return {"last_seq": row[0] if row else 0, "drained": True}
        return {"ok": True}


class FakeRuns:
    def __init__(self):
        self.views = {}
        self.counts = {}
        self.published = 0

    def set_pair(self, repo, head, persona, view, rewritten=None):
        self.views[persona] = view

    def add(self, repo, head, conn=None, **deltas):
        for k, v in deltas.items():
            self.counts[k] = self.counts.get(k, 0) + v

    def publish(self, repo, head, queued=0):
        self.published += 1


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
        self.creds = creds.Creds(self.records)
        self.jobs = Jobs()
        self.runs = FakeRuns()
        self.box = Outbox(self.records, Scanner({}), self.runs, 60000, 25)
        self.state = MemoryStateStore()
        self.decider = Decider(self.state, {"code": ["sage", "sme"], "plan": []},
                               {("code", "sage"): "s", ("code", "sme"): "m"}, {}, 0, 4)
        self.blobs = FakeBlobs()
        self.logs = []
        self.committer = Committer(self.records, self.blobs, lambda r: self.decider,
                                   lambda p: 1800, log=self.logs.append)
        self.gw = FakeGateway(self.records)
        self.app, self.inst = FakeApp(), FakeInst()
        self.ch = Channel(self.records, self.creds, self.jobs, self.box, self.committer, self.gw,
                          self.app, lambda repo: self.inst, self.runs, self.committer.transcript_for,
                          log=self.logs.append)
        self.job = JobMaterial(
            "job1", 7, "acme/web", 42, 12, HEAD, "code", "ollama", "glm", "ANTHROPIC_AUTH_TOKEN",
            ROUTE, 0, True, [PairMaterial("sage", 2, "you are sage", "review #12", signal="fp"),
                             PairMaterial("sme", 1, "you are sme", "review #12", signal="fp")],
            "http://gateway:8090", "http://control:8080")
        self.jobs.add(self.job)
        with self.records.tx() as c:
            c.execute("INSERT INTO jobs (id, installation_id, repo, pr, head, mode, profile, model, "
                      "dispatched_at) VALUES ('job1', 7, 'acme/web', 12, ?, 'code', 'ollama', 'glm', 0)",
                      (HEAD,))
        self.boot = self.creds.mint("bootstrap", "job1", 300)

    def fetch(self):
        status, reply = self.ch.handle("/channel/fetch-job", self.boot, {})
        self.assertEqual(status, 200, reply)
        return reply["capability"], reply["job"]

    def start(self, cap, persona="sme"):
        return self.ch.handle("/channel/start-pair", cap, {"persona": persona})


class FetchJobTest(Base):
    def test_the_bootstrap_buys_the_capability_and_the_spec_once(self):
        cap, job = self.fetch()
        self.assertIsNotNone(self.creds.check(cap, "capability"))
        self.assertEqual((job["repo"], job["head"], job["read_token"], job["shared_worktree"]),
                         ("acme/web", HEAD, "ghs_read", True))
        self.assertEqual(job["clone_url"], "https://github.com/acme/web.git")
        self.assertEqual(self.app.minted, [(7, 42)])
        self.assertEqual(self.ch.handle("/channel/fetch-job", self.boot, {})[0], 401)
        deadline = self.records.conn().execute("SELECT deadline_at - fetched_at FROM jobs").fetchone()[0]
        self.assertEqual(deadline, 55 * 60)

    def test_a_pair_with_a_committed_transcript_gets_it_and_its_session(self):
        self.committer.transcripts[("acme/web", Pair(12, "code", "sage"))] = "t"
        self.blobs["t"] = transcript()
        self.job.pairs[0].session_id = SID
        _, job = self.fetch()
        sage = next(p for p in job["pairs"] if p["persona"] == "sage")
        self.assertEqual((sage["session_id"], base64.b64decode(sage["transcript_b64"])),
                         (SID, transcript()))
        sme = next(p for p in job["pairs"] if p["persona"] == "sme")
        self.assertEqual((sme["session_id"], sme["transcript_b64"]), (None, None))

    def test_a_read_token_github_refuses_kills_the_capability(self):
        self.app.fail = GitHubError(502, "x")
        status, _ = self.ch.handle("/channel/fetch-job", self.boot, {})
        self.assertEqual(status, 503)
        self.assertEqual(self.records.conn().execute(
            "SELECT COUNT(*) FROM credentials WHERE kind = 'capability' AND revoked_at IS NULL"
        ).fetchone()[0], 0)

    def test_an_ended_job_refuses(self):
        self.jobs.remove("job1")
        self.assertEqual(self.ch.handle("/channel/fetch-job", self.boot, {})[0], 401)


class StartPairTest(Base):
    def test_a_start_mints_a_provider_token_the_gateway_learns_with_its_route(self):
        cap, _ = self.fetch()
        status, reply = self.start(cap)
        self.assertEqual(status, 200)
        cred = self.creds.check(reply["provider_token"], "provider")
        self.assertEqual((cred.job_id, cred.persona), ("job1", "sme"))
        op, fields = self.gw.calls[-1]
        self.assertEqual(op, "add_token")
        self.assertEqual((fields["token"]["route"], fields["token"]["model"]), (ROUTE, "glm"))
        self.assertNotIn(reply["provider_token"], json.dumps(fields))
        self.assertEqual(self.runs.views["sme"].state, checkruns.RUNNING)

    def test_an_internal_repo_starts(self):
        cap, _ = self.fetch()
        self.inst.info = {"private": False, "visibility": "internal"}
        self.assertEqual(self.start(cap)[0], 200)

    def test_each_refusal_leaves_the_pair_unstarted(self):
        cap, _ = self.fetch()
        cases = [
            ("public", lambda: setattr(self.inst, "info", {"private": False}), "public"),
            ("no access", lambda: setattr(self.inst, "info", None), "can no longer see"),
            ("error", lambda: setattr(self.inst, "fail", GitHubError(502, "x")), "check failed"),
        ]
        for name, arrange, words in cases:
            with self.subTest(name):
                self.inst.info, self.inst.fail = {"private": True}, None
                arrange()
                status, reply = self.start(cap)
                self.assertEqual(status, 409)
                self.assertIn(words, reply["reason"])
                self.assertNotIn("sme", self.job.started)

    def test_a_parked_profile_refuses(self):
        cap, _ = self.fetch()
        self.committer.profiles["ollama"] = commit._Profile(parked_until=10 ** 10)
        self.assertEqual(self.start(cap)[0], 409)

    def test_a_gateway_that_refuses_the_token_revokes_it(self):
        cap, _ = self.fetch()
        self.gw.refuse_add = True
        self.assertEqual(self.start(cap)[0], 409)
        self.assertEqual(self.creds.live_provider_rows(), [])

    def test_a_pair_starts_once(self):
        cap, _ = self.fetch()
        self.start(cap)
        self.assertEqual(self.start(cap)[0], 409)

    def test_two_concurrent_starts_for_one_pair_mint_one_token(self):
        # A prompt-injected worker can fire both at once (PR #21 review): the
        # second must be refused before the first has reached the gateway.
        import threading
        cap, _ = self.fetch()
        entered, release = threading.Event(), threading.Event()
        real = self.gw.__call__

        def slow(op, **fields):
            if op == "add_token":
                entered.set()
                release.wait(5)
            return real(op, **fields)
        self.ch.gateway_call = slow
        results = []
        first = threading.Thread(target=lambda: results.append(self.start(cap)[0]))
        first.start()
        self.assertTrue(entered.wait(5))
        results.append(self.start(cap)[0])
        release.set()
        first.join()
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(sum(1 for op, _ in self.gw.calls if op == "add_token"), 1)

    def test_a_refused_start_leaves_the_pair_startable(self):
        cap, _ = self.fetch()
        self.gw.refuse_add = True
        self.assertEqual(self.start(cap)[0], 409)
        self.gw.refuse_add = False
        self.assertEqual(self.start(cap)[0], 200)

    def test_a_persona_outside_the_job_or_a_dead_capability_is_refused(self):
        cap, _ = self.fetch()
        self.assertEqual(self.start(cap, "red_team")[0], 400)
        self.assertEqual(self.start("cbx_c_0000000000000000_" + "A" * 43)[0], 401)


class FindingAndCompleteTest(Base):
    def test_a_finding_goes_to_the_outbox(self):
        cap, _ = self.fetch()
        self.assertEqual(self.ch.handle("/channel/post-finding", cap,
                                        {"persona": "sme", "seq": 1, "body": "should-fix: x"}),
                         (200, {"result": "queued"}))
        self.assertEqual(self.box.queued("acme/web", HEAD), 1)
        status, reply = self.ch.handle("/channel/post-finding", cap,
                                       {"persona": "sme", "seq": 2, "body": "ghp_" + "a" * 36})
        self.assertEqual((status, reply["result"]), (422, "withheld"))

    def complete(self, cap, persona="sme", data=None, session=SID, notes=None):
        payload = {"persona": persona, "session_id": session,
                   "transcript_b64": base64.b64encode(data).decode() if data is not None else None,
                   "notes": notes or {}}
        return self.ch.handle("/channel/complete-pair", cap, payload)

    def report(self, cid, **extra):
        msg = {"credential_id": cid, "seq": 1, "delta": {"requests": 1, "successes": 1}}
        msg.update(extra)
        apply_report(self.records, msg)

    def test_a_completed_pair_commits_ok_and_its_token_dies(self):
        cap, _ = self.fetch()
        token = self.start(cap)[1]["provider_token"]
        cid = token.split("_")[2]
        self.report(cid)
        self.assertEqual(self.complete(cap, data=transcript()), (200, {}))
        pair = Pair(12, "code", "sme")
        self.assertEqual(self.state.sessions[pair], SID)
        self.assertEqual(self.state.rounds[pair], 1)
        self.assertIsNone(self.creds.check(token, "provider"))
        self.assertIn(("revoke", {"credential_ids": [cid]}), self.gw.calls)
        self.assertEqual(self.runs.views["sme"].state, checkruns.REVIEWED)

    def test_the_outcome_comes_from_the_record_never_the_worker(self):
        cap, _ = self.fetch()
        cid = self.start(cap)[1]["provider_token"].split("_")[2]
        self.report(cid, first_limit="upstream 429")
        self.complete(cap, data=transcript(), notes={"rc": 0, "limited": False})
        self.assertEqual(self.runs.views["sme"].state, checkruns.USAGE_LIMITED)
        self.assertIsNotNone(self.runs.views["sme"].until)

    def test_an_over_cap_transcript_is_failing(self):
        cap, _ = self.fetch()
        cid = self.start(cap)[1]["provider_token"].split("_")[2]
        self.report(cid)
        self.complete(cap, data=None, notes={"transcript": "over-cap"})
        self.assertEqual(self.runs.views["sme"].state, checkruns.FAILING)

    def test_a_bad_transcript_encoding_is_failing(self):
        cap, _ = self.fetch()
        cid = self.start(cap)[1]["provider_token"].split("_")[2]
        self.report(cid)
        self.ch.handle("/channel/complete-pair", cap,
                       {"persona": "sme", "session_id": SID, "transcript_b64": "!!!"})
        self.assertEqual(self.runs.views["sme"].state, checkruns.FAILING)

    def test_a_pair_completes_once_and_only_after_it_started(self):
        cap, _ = self.fetch()
        self.assertEqual(self.complete(cap, data=transcript())[0], 409)
        self.start(cap)
        self.complete(cap, data=transcript())
        rounds = dict(self.state.rounds)
        # A resend is answered as committed, and commits nothing again.
        self.assertEqual(self.complete(cap, data=transcript()), (200, {}))
        self.assertEqual(self.state.rounds, rounds)

    def test_a_concurrent_duplicate_complete_commits_once(self):
        import threading
        cap, _ = self.fetch()
        self.start(cap)
        self.report(self.job.started["sme"])
        entered, release = threading.Event(), threading.Event()
        real = self.committer.commit

        def slow(c):
            entered.set()
            release.wait(5)
            return real(c)
        self.committer.commit = slow
        first = threading.Thread(target=lambda: self.complete(cap, data=transcript()))
        first.start()
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.complete(cap, data=transcript())[0], 409)
        release.set()
        first.join()
        self.assertEqual(self.complete(cap, data=transcript()), (200, {}))
        self.assertEqual(self.state.rounds[Pair(12, "code", "sme")], 1)

    def test_a_fault_after_the_commit_point_is_not_a_second_pass(self):
        # A fault in the tail (PR #21 review): the outcome is already recorded,
        # so the retry is answered as a resend and spends no second round.
        cap, _ = self.fetch()
        self.start(cap)
        self.report(self.job.started["sme"])
        real = self.ch._publish

        def broken(*a, **k):
            raise RuntimeError("render failed")
        self.ch._publish = broken
        with self.assertRaises(RuntimeError):
            self.complete(cap, data=transcript())
        self.ch._publish = real
        self.assertEqual(self.runs.views["sme"].state, "running")
        self.assertEqual(self.complete(cap, data=transcript()), (200, {}))
        self.assertEqual(self.state.rounds[Pair(12, "code", "sme")], 1)
        # The resend repaints from the recorded outcome (PR #21 review).
        self.assertEqual(self.runs.views["sme"].state, "reviewed")

    def test_a_revoke_that_faults_still_leaves_the_true_outcome_on_the_run(self):
        # The view is set before the fallible revocations (PR #21 review).
        cap, _ = self.fetch()
        self.start(cap)
        self.report(self.job.started["sme"])

        def broken(cid):
            raise OSError("database is locked")
        self.creds.revoke = broken
        with self.assertRaises(OSError):
            self.complete(cap, data=transcript())
        self.assertEqual(self.runs.views["sme"].state, "reviewed")

    def test_a_completion_for_an_ended_job_commits_nothing_and_shows_nothing(self):
        cap, _ = self.fetch()
        self.start(cap)
        self.committer.job_live = lambda job_id: False
        self.assertEqual(self.complete(cap, data=transcript()), (200, {}))
        self.assertEqual(self.job.completed["sme"], "revoked")
        self.assertNotIn("sme", self.state.rounds)
        self.assertEqual(self.runs.views["sme"].state, "running")   # the job's end shows it

    def test_a_commit_that_faults_leaves_the_pair_completable(self):
        # The pair is recorded completed only after the commit point returns
        # (PR #21 review); a fault before then is re-entered by a retry.
        cap, _ = self.fetch()
        self.start(cap)
        real = self.committer.commit

        def broken(c):
            raise OSError("disk full")
        self.committer.commit = broken
        with self.assertRaises(OSError):
            self.complete(cap, data=transcript())
        self.assertNotIn("sme", self.job.completed)
        self.committer.commit = real
        self.assertEqual(self.complete(cap, data=transcript())[0], 200)
        self.assertIn("sme", self.job.completed)

    def test_a_flush_the_gateway_cannot_answer_is_deferred(self):
        cap, _ = self.fetch()
        cid = self.start(cap)[1]["provider_token"].split("_")[2]
        self.report(cid)

        def broken(op, **fields):
            if op == "flush":
                raise OSError("socket closed")
            return {"ok": True}
        self.ch.gateway_call = broken
        self.complete(cap, data=transcript())
        self.assertEqual(self.runs.views["sme"].state, checkruns.DEFERRED)


class HttpTest(Base):
    def setUp(self):
        super().setUp()
        self.server = channel.make_server(self.ch, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path, token, body=b"{}", length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if length is not None:
            headers["Content-Length"] = str(length)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def test_fetch_job_over_http(self):
        status, reply = self.post("/channel/fetch-job", self.boot)
        self.assertEqual((status, reply["job"]["pr"]), (200, 12))

    def test_bodies_over_the_cap_are_refused_unread(self):
        self.assertEqual(self.post("/channel/post-finding", "x", body=b"{}",
                                   length=channel.BODY_MAX + 1)[0], 413)
        self.assertEqual(self.post("/channel/complete-pair", "x", body=b"{}",
                                   length=channel.COMPLETE_BODY_MAX + 1)[0], 413)

    def capability(self):
        status, reply = self.post("/channel/fetch-job", self.boot)
        return reply["capability"]

    def test_a_body_that_is_not_a_json_object_is_refused(self):
        cap = self.capability()
        self.assertEqual(self.post("/channel/start-pair", cap, body=b"[1]")[0], 400)
        self.assertEqual(self.post("/channel/start-pair", cap, body=b"nope")[0], 400)
        self.assertEqual(self.post("/channel/start-pair", cap, body=b"[" * 100000)[0], 400)

    def test_no_credential_is_refused_before_the_body_is_read(self):
        # PR #21 review: a declared 90 MiB body from a caller with no live
        # credential is answered from its headers, and the body never sent.
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.putrequest("POST", "/channel/complete-pair")
        conn.putheader("Authorization", "Bearer x")
        conn.putheader("Content-Length", str(90 * 1024 * 1024))
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 401)
        conn.close()
        self.assertEqual(self.post("/channel/nope", "x")[0], 404)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_channel.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'channel'`.

- [ ] **Step 3: Implement**

`hosted/channel.py`:

```python
"""The worker channel: four HTTP calls a worker makes to the control plane.

`POST /channel/fetch-job` spends the single-use bootstrap token on the job's
capability and returns the job spec, with a read token narrowed to the repo.
`start-pair` re-checks the repo's visibility and the App's access, and mints
the pair's provider token, handing it to the gateway with the route it was
dispatched under. `post-finding` hands a finding to the outbox. `complete-pair`
flushes the gateway, applies the commit point, and revokes the pair's token.

Every call but fetch-job carries the capability. A check that errors fails
closed: a start refused for any reason leaves the pair owed with its round
unchanged. The worker never reports an outcome; the gateway's record does.
"""

import base64
import binascii
import http.server
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import checkruns
import commit as commit_mod
from common import Pair
from github_app import GitHubError, is_public
from outbox import JobContext

JOB_DEADLINE = 55 * 60          # the spec's job deadline, under the read token's hour
BODY_MAX = 1024 * 1024          # every channel request but complete-pair
COMPLETE_BODY_MAX = 96 * 1024 * 1024
TRANSCRIPT_MAX = 64 * 1024 * 1024

_VIEW = {
    commit_mod.OK: checkruns.REVIEWED,
    commit_mod.CAPPED: checkruns.CAPPED,
    commit_mod.USAGE_LIMITED: checkruns.USAGE_LIMITED,
    commit_mod.DEFERRED: checkruns.DEFERRED,
    commit_mod.FAILING: checkruns.FAILING,
    commit_mod.FAILED: checkruns.FAILED,
}


@dataclass
class PairMaterial:
    persona: str
    phase: int
    system_prompt: str
    prompt: str
    session_id: Optional[str] = None      # the session this dispatch resumes
    signal: Any = None                     # the fingerprint an ok commits


@dataclass
class JobMaterial:
    """What a dispatch decided, held in memory until the job ends."""
    job_id: str
    installation_id: int
    repo: str
    repo_id: int
    pr: int
    head: str
    mode: str
    profile: str
    model: str
    auth_env: str
    route: Dict[str, Any]
    max_concurrent_personas: int
    shared_worktree: bool
    pairs: List[PairMaterial]
    gateway_url: str
    channel_url: str
    started: Dict[str, str] = field(default_factory=dict)   # persona -> provider credential id
    completed: Dict[str, str] = field(default_factory=dict)  # persona -> its outcome
    fetched: bool = False
    # Pairs between their check and their record. A request thread reserves a
    # pair under `lock` before any network call, so two start-pairs (or two
    # complete-pairs) for one persona cannot both get past the check.
    busy: Set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def pair_for(self, persona: str) -> Optional[PairMaterial]:
        return next((p for p in self.pairs if p.persona == persona), None)


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self._jobs: Dict[str, JobMaterial] = {}

    def add(self, job: JobMaterial) -> None:
        with self.lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> Optional[JobMaterial]:
        with self.lock:
            return self._jobs.get(job_id)

    def remove(self, job_id: str) -> Optional[JobMaterial]:
        with self.lock:
            return self._jobs.pop(job_id, None)

    def all(self) -> List[JobMaterial]:
        with self.lock:
            return list(self._jobs.values())


class Channel:
    def __init__(self, records, creds, jobs: Jobs, outbox, committer, gateway_call, app,
                 installation_for, check_runs, transcript_for,
                 clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda line: print(line, flush=True)):
        self.records = records
        self.creds = creds
        self.jobs = jobs
        self.outbox = outbox
        self.committer = committer
        self.gateway_call = gateway_call
        self.app = app
        self.installation_for = installation_for
        self.check_runs = check_runs
        self.transcript_for = transcript_for
        self.clock = clock
        self.log = log

    CALLS = ("/channel/fetch-job", "/channel/start-pair", "/channel/post-finding",
             "/channel/complete-pair")

    def refusal(self, path: str, token: str) -> Optional[Tuple[int, Dict[str, Any]]]:
        """The answer for a request refused on its path and token alone, or None.

        The handler asks this before it reads a body, so a caller with no live
        credential costs no read and no parse: the bodies run to 96 MiB, and
        the worker network that can reach this socket is the untrusted side.
        `handle` checks again, since a token can die between the two.
        """
        if path not in self.CALLS:
            return 404, {"reason": "no such call"}
        if path == "/channel/fetch-job":
            if self.creds.check(token, "bootstrap") is None:
                return 401, {"reason": "the bootstrap token is used, expired or unknown"}
            return None
        cred = self.creds.check(token, "capability")
        if cred is None:
            return 401, {"reason": "the capability is not live"}
        if self.jobs.get(cred.job_id) is None:
            return 401, {"reason": "the job has ended"}
        return None

    def handle(self, path: str, token: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if path == "/channel/fetch-job":
            return self.fetch_job(token)
        cred = self.creds.check(token, "capability")
        if cred is None:
            return 401, {"reason": "the capability is not live"}
        job = self.jobs.get(cred.job_id)
        if job is None:
            return 401, {"reason": "the job has ended"}
        persona = body.get("persona")
        pair = job.pair_for(persona) if isinstance(persona, str) else None
        if pair is None:
            return 400, {"reason": "not a persona of this job"}
        if path == "/channel/start-pair":
            return self.start_pair(job, pair)
        if path == "/channel/post-finding":
            return self.post_finding(job, pair, body)
        if path == "/channel/complete-pair":
            return self.complete_pair(job, pair, body)
        return 404, {"reason": "no such call"}

    # -- fetch-job ---------------------------------------------------------------
    def fetch_job(self, token: str) -> Tuple[int, Dict[str, Any]]:
        exchanged = self.creds.exchange_bootstrap(token, JOB_DEADLINE)
        if exchanged is None:
            return 401, {"reason": "the bootstrap token is used, expired or unknown"}
        capability, cred = exchanged
        job = self.jobs.get(cred.job_id)
        if job is None:
            self.creds.revoke(cred.id)
            return 401, {"reason": "the job has ended"}
        try:
            read_token, _ = self.app.read_token(job.installation_id, job.repo_id)
        except GitHubError as exc:
            self.creds.revoke(cred.id)
            return 503, {"reason": f"could not mint the read token: {exc}"}
        now = int(self.clock())
        with self.records.tx() as c:
            c.execute("UPDATE jobs SET fetched_at = ?, deadline_at = ? WHERE id = ?",
                      (now, now + JOB_DEADLINE, job.job_id))
        job.fetched = True
        pairs = []
        for p in job.pairs:
            data = self.transcript_for(job.repo, Pair(job.pr, job.mode, p.persona))
            pairs.append({"persona": p.persona, "phase": p.phase, "system_prompt": p.system_prompt,
                          "prompt": p.prompt, "session_id": p.session_id if data else None,
                          "transcript_b64": base64.b64encode(data).decode("ascii") if data else None})
        return 200, {"capability": capability, "job": {
            "job_id": job.job_id, "repo": job.repo, "pr": job.pr, "head": job.head,
            "mode": job.mode, "clone_url": f"https://github.com/{job.repo}.git",
            "read_token": read_token, "model": job.model, "auth_env": job.auth_env,
            "gateway_url": job.gateway_url, "channel_url": job.channel_url,
            "max_concurrent_personas": job.max_concurrent_personas,
            "shared_worktree": job.shared_worktree, "pairs": pairs}}

    # -- start-pair ----------------------------------------------------------------
    def _refuse_start(self, job: JobMaterial, persona: str, reason: str) -> Tuple[int, Dict[str, Any]]:
        self.log(f"start-pair refused for {job.repo}#{job.pr} {job.mode}/{persona}: {reason}")
        return 409, {"reason": reason}

    def start_pair(self, job: JobMaterial, pair: PairMaterial) -> Tuple[int, Dict[str, Any]]:
        with job.lock:
            if pair.persona in job.started or pair.persona in job.busy:
                return self._refuse_start(job, pair.persona, "this pair has already started")
            job.busy.add(pair.persona)
        try:
            return self._start(job, pair)
        finally:
            with job.lock:
                job.busy.discard(pair.persona)

    def _start(self, job: JobMaterial, pair: PairMaterial) -> Tuple[int, Dict[str, Any]]:
        try:
            info = self.installation_for(job.repo).repo(job.repo)
        except GitHubError as exc:
            return self._refuse_start(job, pair.persona, f"the visibility check failed: {exc}")
        if info is None:
            return self._refuse_start(job, pair.persona, "the App can no longer see the repository")
        if is_public(info):
            return self._refuse_start(job, pair.persona, "the repository is public")
        if self.committer.profile_parked_until(job.profile):
            return self._refuse_start(job, pair.persona, f"profile {job.profile} is parked")
        deadline = self.records.conn().execute(
            "SELECT deadline_at FROM jobs WHERE id = ?", (job.job_id,)).fetchone()
        now = int(self.clock())
        ttl = max(60, (deadline[0] if deadline and deadline[0] else now + JOB_DEADLINE) - now)
        token = self.creds.mint("provider", job.job_id, ttl, persona=pair.persona)
        cid = token.split("_")[2]
        with self.records.tx() as c:
            commit_mod.ensure_record(c, cid)
        row = next(r for r in self.creds.live_provider_rows() if r["credential_id"] == cid)
        row = dict(row, model=job.model, route=job.route, counts={})
        try:
            reply = self.gateway_call("add_token", token=row)
        except Exception as exc:          # the gateway is down or its socket broke
            reply = {"error": str(exc)}
        if "error" in reply:
            self.creds.revoke(cid)
            return self._refuse_start(job, pair.persona, f"the gateway refused the token: "
                                                         f"{reply['error']}")
        with job.lock:
            job.started[pair.persona] = cid
        self._publish(job, pair.persona, checkruns.PairView(checkruns.RUNNING))
        return 200, {"provider_token": token}

    # -- post-finding --------------------------------------------------------------
    def post_finding(self, job: JobMaterial, pair: PairMaterial,
                     body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        seq, text = body.get("seq"), body.get("body")
        if not isinstance(seq, int) or not isinstance(text, str):
            return 400, {"reason": "a finding needs seq and body"}
        ctx = JobContext(job.job_id, job.installation_id, job.repo, job.pr, job.head)
        result, message = self.outbox.accept(ctx, pair.persona, seq, text)
        if result == "queued":
            return 200, {"result": "queued"}
        return 422, {"result": result, "message": message}

    # -- complete-pair -------------------------------------------------------------
    def complete_pair(self, job: JobMaterial, pair: PairMaterial,
                      body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        with job.lock:
            cid = job.started.get(pair.persona)
            if cid is None:
                return 409, {"reason": "this pair never started"}
            done = job.completed.get(pair.persona)
            if done is None:
                if pair.persona in job.busy:
                    return 409, {"reason": "this pair is completing"}
                job.busy.add(pair.persona)
        if done is not None:
            # A resend: the outcome is committed, and answered as post-finding
            # answers one. The settle step runs again from the recorded outcome,
            # since a fault in it the first time would otherwise leave the run
            # showing this pair as running for good; every part of it is
            # idempotent.
            self._settle(job, pair, cid, done)
            return 200, {}
        try:
            outcome = self._commit(job, pair, cid, body)
        except BaseException:
            # A fault leaves the pair started and not completed, which a retry
            # re-enters and the job's end revokes.
            with job.lock:
                job.busy.discard(pair.persona)
            raise
        # One critical section, so no duplicate can pass between the
        # reservation's end and the record it protects.
        with job.lock:
            job.busy.discard(pair.persona)
            job.completed[pair.persona] = outcome
        # The rest follows the record. The pair's view is set first, in memory,
        # so any later fault leaves the run able to show the true outcome at
        # its next publish, and the token for the job's end to revoke.
        self._settle(job, pair, cid, outcome)
        return 200, {}

    def _commit(self, job: JobMaterial, pair: PairMaterial, cid: str,
                body: Dict[str, Any]) -> str:
        transcript = self._transcript(body.get("transcript_b64"))
        notes = body.get("notes") if isinstance(body.get("notes"), dict) else {}
        if transcript is None and notes.get("transcript") == "over-cap":
            transcript = b""   # the worker held back an over-cap transcript: it will not parse
        try:
            flush = self.gateway_call("flush", credential_id=cid)
        except Exception:
            flush = None
        session = body.get("session_id") if isinstance(body.get("session_id"), str) else None
        p = Pair(job.pr, job.mode, pair.persona)
        return self.committer.commit(commit_mod.Completion(
            job.repo, p, job.head, job.profile, cid, transcript, session, pair.session_id,
            flush, pair.signal, job.job_id))

    def _settle(self, job: JobMaterial, pair: PairMaterial, cid: str, outcome: str) -> None:
        p = Pair(job.pr, job.mode, pair.persona)
        if outcome != commit_mod.REVOKED:
            # A revoked completion's job has ended, and its end showed the pair.
            park = self.committer.parked(job.repo, p, job.head)
            state = _VIEW[outcome]
            if outcome == commit_mod.OK and self.committer.decider_for(job.repo).capped(p):
                state = checkruns.ROUND_CAP
            self._publish(job, pair.persona, checkruns.PairView(state, park.until if park else None))
        self.creds.revoke(cid)
        try:
            self.gateway_call("revoke", credential_ids=[cid])
        except Exception:
            pass   # revoked in the records store; a gateway that missed it drops it at the next hello
        self.log(f"{job.repo}#{job.pr} {job.mode}/{pair.persona}: {outcome}")

    def _transcript(self, value: Any) -> Optional[bytes]:
        """The decoded transcript; b"" (which will not parse) when it is bad or over the cap."""
        if value is None:
            return None
        if not isinstance(value, str):
            return b""
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return b""
        return data if len(data) <= TRANSCRIPT_MAX else b""

    def _publish(self, job: JobMaterial, persona: str, view: checkruns.PairView) -> None:
        self.check_runs.set_pair(job.repo, job.head, persona, view)
        try:
            self.check_runs.publish(job.repo, job.head, queued=self.outbox.queued(job.repo, job.head))
        except GitHubError as exc:
            self.log(f"WARN: could not update the check run for {job.repo}@{job.head[:12]}: {exc}")


class Handler(http.server.BaseHTTPRequestHandler):
    channel: Channel = None   # set on the subclass make_server builds
    timeout = 120

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        limit = COMPLETE_BODY_MAX if path == "/channel/complete-pair" else BODY_MAX
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._send(411, {"reason": "Content-Length is required"})
        if length < 0 or length > limit:
            # Refused before reading: a complete-pair over the cap ends the pair
            # as failing when the worker's notes say so; this request is simply too big.
            self.close_connection = True
            return self._send(413, {"reason": f"the body is over {limit} bytes"})
        auth = self.headers.get("Authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        refused = self.channel.refusal(path, token)
        if refused is not None:
            self.close_connection = True   # the body is never read
            return self._send(*refused)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, RecursionError, MemoryError):
            return self._send(400, {"reason": "the body is not JSON"})
        if not isinstance(body, dict):
            return self._send(400, {"reason": "the body is not a JSON object"})
        status, payload = self.channel.handle(path, token, body)
        self._send(status, payload)


def make_server(channel: Channel, host: str, port: int) -> http.server.ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"channel": channel})
    server = http.server.ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_channel.py'`, then `./test-python.sh`
Expected: PASS, 29 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/channel.py tests/test_hosted_channel.py
git commit -m "hosted: the worker channel's four calls"
```

### Task 6: Documentation

**Files:**
- Modify: `CLAUDE.md` (a paragraph in "Hosted App libraries", after the 2a.3 paragraph)
- Modify: `HISTORY.md` (one entry at the top of "Unreleased")

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: nothing code depends on.

- [ ] **Step 1: Describe the decision libraries**

In `CLAUDE.md`'s "Hosted App libraries" subsection, after the paragraph about the worker and the job runner, add:

```markdown
Phase 2a.4a adds the control plane's decisions, as libraries 2a.4b wires into processes. `commit.py` is the commit point. It commits the gateway's reports to `token_records` in order, decides each completed pair's outcome in the spec's order (a transcript that will not parse, then an incomplete flush or an empty record, then failures with no success, then the record itself, where `first_abandoned` only ever replaces `ok`), and applies the outcome's row of the spec's table to the repo's `Decider`, plus the parks it keeps beside it: per pair (until the head moves, or for the profile's backoff) and per profile (limits on two PRs, three failed pairs in a row). The worker's notes never decide. `outbox.py` accepts a finding synchronously (withheld, too long, enough or queued), so the worker's `gh pr comment` learns its fate at once. It drains per installation, one comment a second and 400 an hour, round-robin across PRs, re-checking visibility and the PR's state and scanning again before each post. `checkruns.py` holds one check run per head and its wording, which is never `failure` and only `success` when every pair reviewed and found nothing. `channel.py` serves the worker's four calls: `fetch-job` spends the bootstrap token, `start-pair` re-checks access and mints the pair's provider token, `post-finding` goes to the outbox, and `complete-pair` flushes the gateway and commits. Pair state, parks and check-run pair states are in memory (2a's trial phase), while token rows, check-run counts, outbox items and withheld records are rows. In `reviewer/`, `round_stanza` and `Decider.system_prompt_for` take the head a pair last reviewed, so a hosted round line names it where local mode's searches for the persona's newest signed comment, and `build_hosted` gives the hosted defaults: no Linear or kindex stanza, and one more sentence in `GH_STANZA`.
```

- [ ] **Step 2: Add the history entry**

At the top of "Unreleased" in `HISTORY.md`, add:

```markdown
* **Phase 2a.4a of the hosted GitHub App: the control plane's decisions.** The libraries that decide what each review pass amounts to and what happens next: the outcome of every pair, taken from what the gateway saw rather than what the worker says; a durable outbox that posts findings paced and fairly across PRs, withholding any that carry a credential; one check run per PR head that says honestly where each persona stands and how many findings never arrived; and the worker channel the hosted worker talks to. Nothing runs them yet. Local mode is unchanged, and its prompts are byte-identical.
```

- [ ] **Step 3: Run every suite**

Run: `./test-python.sh && ./test-providers.sh && ./test-personas.sh && ./test-shim.sh && /bin/bash ./test-launcher.sh && /bin/bash ./test-worker.sh`
Expected: all six pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md HISTORY.md
git commit -m "docs: hosted phase 2a.4a, the control plane's decisions"
```
