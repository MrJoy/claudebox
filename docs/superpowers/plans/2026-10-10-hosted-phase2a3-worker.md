# Hosted Phase 2a.3: The Worker and the Job Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This plan is executed by a factory run: the Validator derives a Coder brief (no test code) and a Tester brief (no implementation code) from it, and neither lane opens this file.

**Goal:** The hosted worker, which reviews one PR group as an unprivileged reviewer that holds no provider credential, and the local Docker job runner, which starts and stops workers from a process the control plane can reach only through a unix socket.

**Architecture:** A worker is today's image started with `--user 0` and `--entrypoint /opt/claudebox/worker/entrypoint.sh`. As root, that entrypoint hands the job spec to `setpriv` on stdin and runs it, which empties the bounding set, sets `no_new_privs`, changes to the reviewer and resets the environment. `wrapper.sh` then sets exactly `HOME` and `PATH` and runs `worker.py`. `worker.py` proves its posture, exchanges the single-use bootstrap token for the job's capability over the worker channel, clones the PR head, and runs each pair's persona through `reviewer/passes.py`, phase 1 together and then phase 2. Each pair first asks the channel for its own provider token, and afterwards hands back its transcript and session id. It never reports an outcome. A pair with a session gets its transcript written where `claude --resume` looks for it. The `gh` shim, first on `PATH`, turns `gh pr comment` into the channel's `post-finding`, sends reads to the real `gh`, and refuses everything that writes. `hosted/runner.py` holds the job-runner interface and its local Docker implementation, which builds every `docker` command from one fixed template. `hosted/runnerd.py` is the only process holding the Docker socket, and serves four requests over `gwproto`.

**Tech Stack:** Python 3.9+ standard library (`subprocess`, `urllib`, `concurrent.futures`, `fcntl`, `unittest`); bash and POSIX `sh`; `setpriv` from util-linux.

**Spec:** `docs/superpowers/specs/2026-10-09-hosted-github-app-design.md`, sections "Components" (job runner, worker), "One job per PR group", "Resuming a session in a fresh container", "Worker hardening", "The worker channel" (calls, bootstrap), and "The `gh` shim". The 2a decisions this plan binds are in `docs/superpowers/plans/2026-10-10-hosted-phase2a-overview.md`. The ones it needs are copied under Global Constraints.

**Prerequisite:** the 2a.1 foundations plan (PR #18) and the 2a.2 gateway plan (PR #19) are built. `runner.py` imports `creds.TOKEN_RE` and `creds.KINDS` (2a.1) and `gwproto` (2a.2). The worker's provider token is 2a.2's token shape. The control plane that answers the worker channel is 2a.4's, so this plan tests the worker against a fake one.

## Global Constraints

- **Standard library only**, and **Python 3.9 syntax** (overview decision 15). Flat modules, imported by plain name. `worker/worker.py` puts `reviewer/` on `sys.path` itself, and `tests/_path.py` adds `worker/` (overview decision 19).
- **Overview decision 5:** the Docker socket belongs to `runnerd`, never to the control plane. A compromised control plane can make `runnerd` start a worker from the fixed template, list workers and stop them, and nothing else.
- **Overview decision 6:** a worker starts as root on every target. On local Docker the runner passes `--cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add SETPCAP --security-opt no-new-privileges --pids-limit 512 --memory 4g --user 0`, and `setpriv` spends those three capabilities before the reviewer runs.
- **Overview decision 7:** the worker image is today's `claudebox` image with `worker/` added, started with `--entrypoint /opt/claudebox/worker/entrypoint.sh`. `worker/` is root-owned in the image, like `reviewer/`.
- The spec's `setpriv` line: `setpriv --no-new-privs --bounding-set=-all --inh-caps=-all --reuid=reviewer --regid=reviewer --init-groups --reset-env`.
- The spec's job spec handling: "the single-use token never reaches the worker's environment or its argv. After the change a fixed wrapper sets exactly `HOME=/home/reviewer` and a `PATH` that puts the `gh` shim's directory first, and execs the worker." The spec has root write the spec to a mode-600 file in the reviewer's home; this plan hands it over on stdin instead (decision 2), because root holding only overview decision 6's three capabilities cannot write that file.
- The spec's posture rule: "`ALLOW_UNHARDENED` is never set in a worker's environment on any target, so a target that cannot provide the posture fails its first job loudly."
- The spec's resume: "`~/.claude/projects/<encoded cwd>/<session id>.jsonl`. So the cwd and `$HOME` must be identical on every target. The worker clones to `/home/reviewer/work/repo` ... it creates and proves that directory as the reviewer ..., dying if it cannot." "The session id is read from the transcript itself." "The worker suite asserts that a pair run twice against a fake control plane produces a second transcript containing the first one's turns".
- The spec's clone: "blobless (`--filter=blob:none`) with a full checkout of the PR head". The spec's group: "phase 1 together and then phase 2", and "the shared-clone defenses in `review_loop.py` (`WORKTREE_STANZA`, `lock_git_dir`) carry over".
- The spec's shim: "`gh pr comment` becomes `post finding`. Other `gh` calls use the read token in 2a". `post finding` answers **queued** ("exit 0, print 'queued'") or a refusal the model is told about (exit non-zero).
- The spec's worker "reports each pair's outcome" only as evidence: "The worker contributes the transcript, the session id and its own notes, never the outcome."
- The overview's worker channel: `POST /channel/fetch-job` (bootstrap token, `200 {capability, job}`), `POST /channel/start-pair` (`{persona}`, `200 {provider_token}` or `409 {reason}`, which leaves the pair owed), `POST /channel/post-finding` (`{persona, seq, body}`, `200 {result: "queued"}` or `422 {result, message}`), and `POST /channel/complete-pair` (`{persona, session_id, transcript_b64, notes}`). The job spec is `{job_id, repo, pr, head, mode, clone_url, read_token, model, auth_env, gateway_url, max_concurrent_personas, shared_worktree, pairs: [{persona, phase, system_prompt, prompt, session_id, transcript_b64}]}`.
- The overview's transcript cap: `TRANSCRIPT_MAX = 64 * 1024 * 1024` decoded. "The worker checks the same 64 MiB cap before sending, and reports an over-cap transcript as `failing` in its notes rather than sending it."
- The overview's runner socket: `start {job_id, bootstrap_token, channel_url, gateway_url}`, `list` (answered by `{jobs: [{job_id, state}]}`), `stop {job_id}` and `stop_all`, in `gwproto`'s framing.
- **Spike S3** (overview decision 9): an `oauth_token` profile hands the worker its pair token as `CLAUDE_CODE_OAUTH_TOKEN`; every other profile as `ANTHROPIC_AUTH_TOKEN`. The job spec's `auth_env` names which.
- Every existing suite still passes. `tests/_path.py` gains `worker/`. A sixth suite, `test-worker.sh`, joins them, and it runs on the host's `/bin/bash` 3.2 as `test-launcher.sh` does.

## Decided here

The spec and the overview leave these open, and each one changes code. They are this plan's answers, for the founder to ratify in Phase 1 of the factory run:

1. **The job spec reaches the container in `CLAUDEBOX_JOB`**, passed to `docker run` as `-e CLAUDEBOX_JOB` with the value in `docker`'s own environment, so it never appears in `runnerd`'s argv or in `ps`. It is still in the container's metadata (`docker inspect`), which is why the bootstrap token in it is single-use. The worker file holds `{job_id, bootstrap_token, channel_url, gateway_url}`, and everything else comes from `fetch-job`.
2. **Root hands the job spec to the worker on stdin and writes no file.** The spec says root writes it to a mode-600 file in the reviewer's home, but root here holds only `SETUID`, `SETGID` and `SETPCAP` (overview decision 6): writing into `/home/reviewer` (owned by the reviewer, mode 755) needs `CAP_DAC_OVERRIDE`, and handing the file over needs `CAP_CHOWN`. Run under exactly the runner's flags, the file-writing entrypoint dies with `Permission denied` before `setpriv`. So the entrypoint copies the spec into an unexported shell variable, unsets `CLAUDEBOX_JOB`, and runs `exec setpriv ... "$WRAPPER" <<<"$job"`. The spec still never touches the reviewer's environment or argv, and there is no file left to plant a symlink at. `worker.py` reads at most 64 KiB from stdin, then points fd 0 at `/dev/null` so neither `claude` nor any shell a pass spawns inherits the pipe.
3. **The posture check lives in `worker.py`.** The image's three checks live in `entrypoint.sh`, which a worker never runs. `posture_problems` repeats them (not root, `NoNewPrivs: 1`, `CapBnd` zero) with no escape hatch, reading `/proc/self/status`. An unreadable status fails both kernel checks.
4. **Every `docker` field is checked before `docker` runs.** A job id matches `[a-z0-9][a-z0-9-]{0,62}`, the bootstrap token must be a `cbx_b_` token by 2a.1's pattern, and both URLs must be plain `http(s)` URLs with no spaces or query. The container is named `claudebox-job-<job_id>` and labelled `claudebox.job=<job_id>`, and `list` reads only containers carrying that label. `runnerd` passes `docker` only `PATH`, `HOME` and `DOCKER_*` from its own environment.
5. **`stop` is `docker rm -f`**, and a container that is already gone is a success, so `stop_all` after a crash is idempotent.
6. **The clone authenticates with an `http.extraheader` scoped to `https://github.com/`** and kept in the clone's config, because a blobless clone fetches history lazily whenever a persona asks for it. The read token sits in the reviewer's reach anyway in 2a (the spec's "in 2a only, the worker holds a GitHub read token"). A clone URL off `https://github.com/` and a head that is not a full commit id are refused before `git` runs, and a checkout that lands anywhere but the job's head is an error.
7. **`claude` gets a built environment, never an inherited one**: `HOME`, `PATH`, `DISABLE_AUTOUPDATER=1`, `ANTHROPIC_BASE_URL` (the gateway), the pair's provider token under `auth_env`, `GH_TOKEN` (the read token), the channel URL, the capability, the persona and the PR number for the shim, and the job's model in every model-tier variable the local entrypoint pins. `ANTHROPIC_API_KEY` is never set. Every pass carries `--strict-mcp-config` and no other MCP flag, since hosted Linear is deferred and kindex is 3a.
8. **Whether the personas share the worktree is decided once, by the control plane, and carried in the job spec as `shared_worktree`.** The control plane puts `WORKTREE_STANZA` in the prompts by that answer (2a.4), and the worker locks `.git` with `review_loop.lock_git_dir`, unchanged, exactly when it is true. The worker never recomputes it, so the stanza and the lock cannot disagree. A job spec without a boolean `shared_worktree` is refused at `fetch-job`.
9. **The session id is read from the transcript.** The first line whose `sessionId` is a plausible id wins, and a session id that could escape the projects directory is refused. A transcript with no id starts a fresh session with a `WARN`. When the job's `session_id` disagrees with the transcript's, the transcript's resumes, with a `WARN`. A `session_id` with no transcript starts fresh.
10. **A pass that cannot start is an ordinary failed pass**, as in local mode's `_run_one`: any exception from spawning `claude` (`EAGAIN` at the PID limit, `ENOMEM`, no `claude` on `PATH`) becomes a failed `PassResult`, and the pair still sends `complete-pair`. Letting it out would abort the pool, and the started pair, its siblings and the next phase would never complete.
11. **A refused `start-pair` runs nothing and completes nothing**: the pair stays owed, and the log names the refusal's reason. A usage limit in phase 1 withholds phase 2, as `run_group` does, and those pairs are never started, so the control plane still owes them.
12. **`complete-pair` notes are `{rc, limited, transcript}`**, where `transcript` is `ok`, `missing` or `over-cap`. They are evidence for the control plane, never an outcome.
13. **The shim allows a fixed set of reads** (`pr view|diff|list|status`, `issue view|list`, `repo view`, `auth status`, `run view|list`, `search`, and `api`) and refuses everything else with one message that names `gh pr comment` as the only way to post. `gh api` is checked by allowlist, because `gh` parses flags with pflag, which accepts attached short values (`-XPOST`, `-fbody=x`) and combined flags, so no list of write spellings is complete. It takes exactly one REST path, and only `--paginate`, `--slurp`, `-i`/`--include`, `--silent`, `--verbose`, `-q`/`--jq`, `-t`/`--template` and `--cache`. Every other flag is refused, `--hostname`, `-H` and `--method` included, and so are `graphql` (always a `POST`) and a full URL. `pr checks` is left out because the token cannot read check status (`GH_STANZA` already says so).
14. **`gh pr comment` takes `--body`, `-b`, `--body=`, and `--body-file`/`-F` (with `-` for stdin)**, and one PR number, which must be the job's. Any other flag is refused with a message naming the two that work. The finding number `seq` is counted per persona in a mode-600 file under the reviewer's home, under `flock`, so a pair's concurrent shells never reuse one.
15. **A refused finding exits 1 and prints the control plane's `message`** on stderr. An unreachable channel exits 1 and says the finding was not posted.

## Review Focus

- **The root phase needing a capability it was not given.** Expected: the entrypoint writes nothing and changes no owner, so `SETUID`, `SETGID` and `SETPCAP` are all it spends. Pinned in Task 2, and run for real under the runner's flags before this plan went up (below).
- **A token reaching the reviewer's environment or argv.** Expected: `CLAUDEBOX_JOB` is unset before `setpriv`, and the token is in neither `setpriv`'s argv nor the wrapper's environment. Pinned in Task 2.
- **A field crafted to smuggle a `docker` flag or path through `start` or `stop`.** Expected: refused before `docker` runs. Pinned in Task 1.
- **A prompt-injected reviewer writing to GitHub through `gh`.** Expected: every write verb, `gh api` with a method, field, header or host in any spelling (`-XPOST`, `-fbody=x`, `-iXPOST`), and a comment on another PR are refused without reaching the real `gh` or the channel. Pinned in Task 3.
- **A resume that silently starts fresh.** Expected: a pair run twice in two fresh homes resumes the first run's session, and the second transcript holds both runs' turns. Pinned in Task 4.
- **Personas racing on one clone.** Expected: the clone's `.git` is locked exactly when the job spec's `shared_worktree` says so, and a spec that does not say is refused. Pinned in Task 4.

## Not run here

- **The full worker image under the runner's flags.** `test-worker.sh` stubs `setpriv`. Before this plan went up, the entrypoint ran for real in `node:22-bookworm-slim` (the image's base) under `HARDENING` exactly, against a stand-in user and a wrapper that printed its state: uid 1000, `CapInh`/`CapPrm`/`CapEff`/`CapBnd`/`CapAmb` all zero, `NoNewPrivs: 1`, only `setpriv`'s reset environment, and the spec on stdin. The Validator repeats that against the built image with the real wrapper and `worker.py`, whose posture line is the evidence, against a stub channel. Fly and Cloud Run are spike S7's.
- **`runnerd` against a real Docker daemon.** The tests drive it with a fake `subprocess.run`. The first real start is 2a.4's, when a control plane can mint a bootstrap token.
- **A real `claude` resuming a transcript written by another container.** The tests use a fake `claude` that appends to the transcript at the path the real one uses. 2a.5's end-to-end run is the first live check.

---

### Task 1: The job runner and runnerd

**Files:**
- Create: `hosted/runner.py`
- Create: `hosted/runnerd.py`
- Test: `tests/test_hosted_runner.py`

**Interfaces:**
- Consumes: `creds.TOKEN_RE` and `creds.KINDS` (2a.1); `gwproto.Server`, `gwproto.Client` (2a.2).
- Produces:
  - `RunnerError(Exception)`; `check_start(job_id, bootstrap_token, channel_url, gateway_url) -> None`, raising `RunnerError`.
  - `JOB_ID_RE`, `CONTAINER_PREFIX = "claudebox-job-"`, `LABEL = "claudebox.job"`, `ENTRYPOINT = "/opt/claudebox/worker/entrypoint.sh"`, and `HARDENING`, the tuple of `docker run` flags from overview decision 6.
  - `JobRunner` (abstract): `start(job_id, bootstrap_token, channel_url, gateway_url)`, `list() -> List[{job_id, state}]`, `stop(job_id)`, and `stop_all() -> int`.
  - `LocalDockerRunner(image, network, docker="docker", run=subprocess.run, base_env=None)`, with `start_argv(job_id) -> List[str]`.
  - `RunnerClient(client: gwproto.Client)`, the control plane's `JobRunner`, which raises `RunnerError` on an `{error}` reply.
  - `runnerd.make_handler(runner) -> Callable[[dict], dict]` (ops `start`, `list`, `stop`, `stop_all`) and `runnerd.main(env)`, reading `RUNNER_SECRET`, `RUNNER_SOCKET`, `WORKER_IMAGE` and `WORKER_NETWORK`.

- [ ] **Step 1: Write the failing test**

`tests/test_hosted_runner.py`:

```python
import _path  # noqa: F401

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

import gwproto
import runnerd
from runner import (CONTAINER_PREFIX, ENTRYPOINT, HARDENING, LocalDockerRunner, RunnerClient,
                    RunnerError, check_start)

BOOT = "cbx_b_0123456789abcdef_" + "A" * 43


class FakeRun:
    """Records each docker call; answers from a queue of (rc, stdout, stderr)."""

    def __init__(self, *answers):
        self.calls = []
        self.answers = list(answers)

    def __call__(self, argv, env=None, capture_output=None, text=None, timeout=None):
        self.calls.append({"argv": argv, "env": env})
        rc, out, err = self.answers.pop(0) if self.answers else (0, "", "")
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class CheckStartTest(unittest.TestCase):
    def test_a_good_request_passes(self):
        check_start("job-1", BOOT, "http://control:8080", "http://gateway:8090")

    def test_each_bad_field_is_refused(self):
        cases = [
            ("Job_1", BOOT, "http://c:1", "http://g:1"),
            ("job;rm -rf /", BOOT, "http://c:1", "http://g:1"),
            ("job-1", "cbx_c_0123456789abcdef_" + "A" * 43, "http://c:1", "http://g:1"),
            ("job-1", "junk", "http://c:1", "http://g:1"),
            ("job-1", BOOT, "file:///etc/passwd", "http://g:1"),
            ("job-1", BOOT, "http://c:1", "http://g:1/x y"),
            ("job-1", BOOT, "http://c:1 --privileged", "http://g:1"),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(RunnerError):
                check_start(*case)


class LocalDockerRunnerTest(unittest.TestCase):
    def runner(self, *answers):
        run = FakeRun(*answers)
        return LocalDockerRunner("claudebox:latest", "claudebox-hosted", run=run,
                                 base_env={"PATH": "/usr/bin"}), run

    def test_start_runs_the_fixed_template_with_the_spec_in_the_environment_only(self):
        r, run = self.runner()
        r.start("job-1", BOOT, "http://control:8080", "http://gateway:8090")
        call = run.calls[0]
        self.assertEqual(call["argv"], [
            "docker", "run", "-d", "--name", CONTAINER_PREFIX + "job-1",
            "--label", "claudebox.job=job-1", "--network", "claudebox-hosted", *HARDENING,
            "--entrypoint", ENTRYPOINT, "-e", "CLAUDEBOX_JOB", "claudebox:latest"])
        self.assertNotIn(BOOT, " ".join(call["argv"]))
        self.assertEqual(json.loads(call["env"]["CLAUDEBOX_JOB"]),
                         {"job_id": "job-1", "bootstrap_token": BOOT,
                          "channel_url": "http://control:8080", "gateway_url": "http://gateway:8090"})
        self.assertEqual(call["env"]["PATH"], "/usr/bin")

    def test_the_hardening_is_three_capabilities_and_no_new_privileges(self):
        self.assertEqual(HARDENING, ("--user", "0", "--cap-drop", "ALL", "--cap-add", "SETUID",
                                     "--cap-add", "SETGID", "--cap-add", "SETPCAP",
                                     "--security-opt", "no-new-privileges",
                                     "--pids-limit", "512", "--memory", "4g"))

    def test_a_bad_start_never_reaches_docker(self):
        r, run = self.runner()
        with self.assertRaises(RunnerError):
            r.start("job-1", "junk", "http://c:1", "http://g:1")
        self.assertEqual(run.calls, [])

    def test_a_failed_docker_run_is_a_runner_error_naming_its_last_line(self):
        r, _ = self.runner((125, "", "pulling\nError: no such image\n"))
        with self.assertRaises(RunnerError) as ctx:
            r.start("job-1", BOOT, "http://c:1", "http://g:1")
        self.assertIn("no such image", str(ctx.exception))

    def test_list_reads_the_label_and_state(self):
        r, run = self.runner((0, "job-1\trunning\njob-2\texited\n\tjunk\n", ""))
        self.assertEqual(r.list(), [{"job_id": "job-1", "state": "running"},
                                    {"job_id": "job-2", "state": "exited"}])
        self.assertIn("label=claudebox.job", run.calls[0]["argv"])

    def test_stop_removes_by_name_and_a_missing_container_is_fine(self):
        r, run = self.runner((1, "", "Error: No such container: claudebox-job-job-1\n"))
        r.stop("job-1")
        self.assertEqual(run.calls[0]["argv"], ["docker", "rm", "-f", CONTAINER_PREFIX + "job-1"])

    def test_stop_refuses_a_bad_id(self):
        r, run = self.runner()
        with self.assertRaises(RunnerError):
            r.stop("../x")
        self.assertEqual(run.calls, [])

    def test_stop_all_stops_everything_listed(self):
        r, run = self.runner((0, "job-1\trunning\njob-2\texited\n", ""))
        self.assertEqual(r.stop_all(), 2)
        self.assertEqual([c["argv"][-1] for c in run.calls[1:]],
                         [CONTAINER_PREFIX + "job-1", CONTAINER_PREFIX + "job-2"])


class RunnerdTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir)
        self.run = FakeRun()
        runner = LocalDockerRunner("img", "net", run=self.run, base_env={})
        path = os.path.join(self.dir, "r.sock")
        self.server = gwproto.Server(path, "s", runnerd.make_handler(runner)).start()
        self.addCleanup(self.server.stop)
        self.client = RunnerClient(gwproto.Client(path, "s"))

    def test_the_client_starts_a_worker_through_runnerd(self):
        self.client.start("job-1", BOOT, "http://c:1", "http://g:1")
        self.assertEqual(self.run.calls[0]["argv"][:3], ["docker", "run", "-d"])

    def test_a_refusal_comes_back_as_a_runner_error(self):
        with self.assertRaises(RunnerError):
            self.client.start("job-1", "junk", "http://c:1", "http://g:1")
        self.assertEqual(self.run.calls, [])

    def test_an_op_runnerd_does_not_know_is_refused(self):
        reply = gwproto.Client(self.server.path, "s").call("exec", argv=["docker", "rm", "-f", "x"])
        self.assertIn("unknown op", reply["error"])
        self.assertEqual(self.run.calls, [])

    def test_list_and_stop_all_round_trip(self):
        self.run.answers = [(0, "job-1\trunning\n", ""), (0, "job-1\trunning\n", ""), (0, "", "")]
        self.assertEqual(self.client.list(), [{"job_id": "job-1", "state": "running"}])
        self.assertEqual(self.client.stop_all(), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_hosted_runner.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'runnerd'`.

- [ ] **Step 3: Implement**

`hosted/runner.py`:

```python
"""The job runner: the runtime seam that starts, lists and stops workers.

One interface, three implementations in the spec (local Docker, Fly
Machines, Cloud Run Jobs); 2a builds the first. On local Docker the runner
needs the Docker socket, which is root on the Docker host, so it never runs
in the control plane: runnerd.py holds the socket as its own uid, and the
control plane reaches it through RunnerClient over a unix socket. What the
control plane can make runnerd do is start a worker from a fixed template,
list the workers, and stop them, and nothing else.

A worker starts as root with exactly the three capabilities setpriv needs
(overview decision 6) and drops to the reviewer in its own entrypoint. The
job spec reaches it in CLAUDEBOX_JOB, passed to `docker run` by name so the
value never appears in runnerd's argv; it is still in the container's
metadata, which is why the bootstrap token in it is single-use.
"""

import json
import os
import re
import subprocess
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

import creds
import gwproto

JOB_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_URL_RE = re.compile(r"https?://[A-Za-z0-9.\-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/\-]*)?")
CONTAINER_PREFIX = "claudebox-job-"
LABEL = "claudebox.job"
ENTRYPOINT = "/opt/claudebox/worker/entrypoint.sh"
# Started as root so the entrypoint can run setpriv, holding only the three
# capabilities setpriv spends, with no_new_privs and the resource bounds the
# local image has always run under.
HARDENING = ("--user", "0", "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID",
             "--cap-add", "SETPCAP", "--security-opt", "no-new-privileges",
             "--pids-limit", "512", "--memory", "4g")


class RunnerError(Exception):
    """A request runnerd refused, or a docker command that failed."""


def check_start(job_id: str, bootstrap_token: str, channel_url: str, gateway_url: str) -> None:
    """Every field of a start request, checked before anything reaches docker."""
    if not JOB_ID_RE.fullmatch(job_id or ""):
        raise RunnerError(f"not a job id: {job_id!r}")
    m = creds.TOKEN_RE.fullmatch(bootstrap_token or "")
    if not m or m.group(1) != creds.KINDS["bootstrap"]:
        raise RunnerError("not a bootstrap token")
    for name, url in (("channel_url", channel_url), ("gateway_url", gateway_url)):
        if not _URL_RE.fullmatch(url or ""):
            raise RunnerError(f"{name} is not a plain http(s) URL")


class JobRunner(ABC):
    @abstractmethod
    def start(self, job_id: str, bootstrap_token: str, channel_url: str, gateway_url: str) -> None:
        ...

    @abstractmethod
    def list(self) -> List[Dict[str, str]]:
        """[{job_id, state}] for every worker this runner started and has not removed."""

    @abstractmethod
    def stop(self, job_id: str) -> None:
        ...

    def stop_all(self) -> int:
        jobs = self.list()
        for job in jobs:
            self.stop(job["job_id"])
        return len(jobs)


class LocalDockerRunner(JobRunner):
    def __init__(self, image: str, network: str, docker: str = "docker",
                 run: Callable[..., Any] = subprocess.run, base_env: Optional[Dict[str, str]] = None):
        self.image = image
        self.network = network
        self.docker = docker
        self.run = run
        # docker reads DOCKER_HOST and friends from its environment; pass only those.
        self.base_env = base_env if base_env is not None else {
            k: v for k, v in os.environ.items() if k in ("PATH", "HOME") or k.startswith("DOCKER_")}

    def _docker(self, args: List[str], extra_env: Optional[Dict[str, str]] = None) -> str:
        env = dict(self.base_env)
        env.update(extra_env or {})
        result = self.run([self.docker] + args, env=env, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            tail = (result.stderr or "").strip().splitlines()[-1:] or ["no output"]
            raise RunnerError(f"docker {args[0]} failed: {tail[0][:300]}")
        return result.stdout

    def start_argv(self, job_id: str) -> List[str]:
        return (["run", "-d", "--name", CONTAINER_PREFIX + job_id, "--label", f"{LABEL}={job_id}",
                 "--network", self.network] + list(HARDENING)
                + ["--entrypoint", ENTRYPOINT, "-e", "CLAUDEBOX_JOB", self.image])

    def start(self, job_id: str, bootstrap_token: str, channel_url: str, gateway_url: str) -> None:
        check_start(job_id, bootstrap_token, channel_url, gateway_url)
        spec = json.dumps({"job_id": job_id, "bootstrap_token": bootstrap_token,
                           "channel_url": channel_url, "gateway_url": gateway_url})
        self._docker(self.start_argv(job_id), {"CLAUDEBOX_JOB": spec})

    def list(self) -> List[Dict[str, str]]:
        out = self._docker(["ps", "-a", "--filter", f"label={LABEL}",
                            "--format", "{{.Label \"" + LABEL + "\"}}\t{{.State}}"])
        jobs = []
        for line in out.splitlines():
            job_id, _, state = line.partition("\t")
            if JOB_ID_RE.fullmatch(job_id):
                jobs.append({"job_id": job_id, "state": state.strip()})
        return jobs

    def stop(self, job_id: str) -> None:
        if not JOB_ID_RE.fullmatch(job_id or ""):
            raise RunnerError(f"not a job id: {job_id!r}")
        try:
            self._docker(["rm", "-f", CONTAINER_PREFIX + job_id])
        except RunnerError as exc:
            if "No such container" not in str(exc):
                raise


class RunnerClient(JobRunner):
    """The control plane's side: a JobRunner whose every call goes to runnerd."""

    def __init__(self, client: gwproto.Client):
        self.client = client

    def _call(self, op: str, **fields: Any) -> Dict[str, Any]:
        reply = self.client.call(op, **fields)
        if "error" in reply:
            raise RunnerError(reply["error"])
        return reply

    def start(self, job_id: str, bootstrap_token: str, channel_url: str, gateway_url: str) -> None:
        self._call("start", job_id=job_id, bootstrap_token=bootstrap_token,
                   channel_url=channel_url, gateway_url=gateway_url)

    def list(self) -> List[Dict[str, str]]:
        return self._call("list")["jobs"]

    def stop(self, job_id: str) -> None:
        self._call("stop", job_id=job_id)

    def stop_all(self) -> int:
        return self._call("stop_all")["stopped"]
```

`hosted/runnerd.py`:

```python
"""runnerd: the only process holding the Docker socket.

It answers four requests from the control plane over a unix socket (start,
list, stop, stop_all), checks every field, and builds the docker command
from runner.py's fixed template. A control plane that is compromised can
make it start a worker, and nothing else: no other image, no other flag, no
other docker command.

Environment: RUNNER_SECRET, RUNNER_SOCKET, WORKER_IMAGE, WORKER_NETWORK.
"""

import os
import signal
import sys
import threading
from typing import Any, Dict, Mapping

import gwproto
from runner import JobRunner, LocalDockerRunner, RunnerError


def make_handler(runner: JobRunner):
    def handle(msg: Dict[str, Any]) -> Dict[str, Any]:
        op = msg.get("op")
        try:
            if op == "start":
                runner.start(str(msg.get("job_id", "")), str(msg.get("bootstrap_token", "")),
                             str(msg.get("channel_url", "")), str(msg.get("gateway_url", "")))
                return {"ok": True}
            if op == "list":
                return {"jobs": runner.list()}
            if op == "stop":
                runner.stop(str(msg.get("job_id", "")))
                return {"ok": True}
            if op == "stop_all":
                return {"stopped": runner.stop_all()}
        except RunnerError as exc:
            return {"error": str(exc)}
        return {"error": f"unknown op {op!r}"}
    return handle


def main(env: Mapping[str, str] = os.environ) -> None:
    runner = LocalDockerRunner(env["WORKER_IMAGE"], env["WORKER_NETWORK"])
    server = gwproto.Server(env["RUNNER_SOCKET"], env["RUNNER_SECRET"], make_handler(runner)).start()
    print(f"runnerd listening on {env['RUNNER_SOCKET']}", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()
    server.stop()


if __name__ == "__main__":
    main()
    sys.exit(0)
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_hosted_runner.py'`, then `./test-python.sh`
Expected: PASS, 14 tests. The whole suite passes.

- [ ] **Step 5: Commit**

```bash
git add hosted/runner.py hosted/runnerd.py tests/test_hosted_runner.py
git commit -m "hosted: the job runner, and runnerd, the one process holding the Docker socket"
```

### Task 2: The worker's root entrypoint and wrapper

**Files:**
- Create: `worker/entrypoint.sh`
- Create: `worker/wrapper.sh`
- Test: `test-worker.sh`

**Interfaces:**
- Consumes: nothing from earlier tasks. `wrapper.sh` names `/opt/claudebox/worker/worker.py`, which Task 4 creates.
- Produces:
  - `worker/entrypoint.sh`: needs root and `CLAUDEBOX_JOB`; unsets `CLAUDEBOX_JOB` and execs `setpriv` with the spec's flags and `/opt/claudebox/worker/wrapper.sh`, with the spec on stdin. It writes no file. Its one fixed path is the line `WRAPPER=`, which the suite rewrites.
  - `worker/wrapper.sh`: `exec env -i HOME=/home/reviewer PATH=/opt/claudebox/worker/bin:/usr/local/bin:/usr/bin:/bin python3 /opt/claudebox/worker/worker.py`.
  - `test-worker.sh`: the sixth suite, 10 checks, runnable under `/bin/bash` 3.2.

- [ ] **Step 1: Write the failing suite**

`test-worker.sh` (mode 755):

```bash
#!/usr/bin/env bash
# Tests worker/entrypoint.sh and worker/wrapper.sh, the hosted worker's two
# shell steps, with no Docker and no root: `id`, `chown` and `setpriv` are
# stubbed onto PATH, and the fixed path at the top of each script is
# rewritten to a temp dir. What this proves is the hand-off: the job spec
# reaches setpriv on stdin and nowhere else, CLAUDEBOX_JOB is gone before
# setpriv runs, root writes no file and changes no owner (it holds no
# capability that would let it), setpriv gets exactly the flags that give the
# reviewer its posture, and the wrapper hands the worker exactly HOME and PATH. Whether the kernel then honours
# those flags is the worker's own posture check, run in the image (test the
# image with `claudebox.sh` before trusting a new runtime).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL %s\n     %s\n' "$1" "$2"; }

setup() {
  T="$(mktemp -d)"
  mkdir -p "$T/bin" "$T/home"
  # id -u answers $FAKE_UID; chown records that it ran at all; setpriv records
  # its argv, the environment it was handed and its stdin, then stops.
  cat > "$T/bin/id" <<STUB
#!/bin/sh
[ "\$1" = "-u" ] && { echo "\${FAKE_UID:-0}"; exit 0; }
exec /usr/bin/id "\$@"
STUB
  cat > "$T/bin/chown" <<STUB
#!/bin/sh
echo "\$@" >> "$T/chown.log"
STUB
  cat > "$T/bin/setpriv" <<STUB
#!/bin/sh
printf '%s\n' "\$@" > "$T/setpriv.argv"
env > "$T/setpriv.env"
cat > "$T/setpriv.stdin"
STUB
  chmod +x "$T/bin/"*
  sed -e "s|^WRAPPER=.*|WRAPPER=$T/wrapper.sh|" \
      "$SCRIPT_DIR/worker/entrypoint.sh" > "$T/entrypoint.sh"
  chmod +x "$T/entrypoint.sh"
}

run_entrypoint() {
  (cd "$T/home" && env -i PATH="$T/bin:/usr/bin:/bin" HOME="$T/home" "$@" bash "$T/entrypoint.sh" \
    > "$T/out" 2>&1 < /dev/null)
  echo $? > "$T/rc"
}

file_mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }

# --- refusals -------------------------------------------------------------------
setup
run_entrypoint FAKE_UID=1000 CLAUDEBOX_JOB='{}'
if [ "$(cat "$T/rc")" = 1 ] && grep -q 'must start as root' "$T/out" && [ ! -e "$T/setpriv.argv" ]; then
  ok "a non-root start is refused before anything runs"
else
  bad "a non-root start is refused before anything runs" "rc=$(cat "$T/rc") out=$(cat "$T/out")"
fi
rm -rf "$T"

setup
run_entrypoint FAKE_UID=0
if [ "$(cat "$T/rc")" = 1 ] && grep -q 'CLAUDEBOX_JOB is not set' "$T/out" && [ ! -e "$T/setpriv.argv" ]; then
  ok "a missing job spec is refused"
else
  bad "a missing job spec is refused" "rc=$(cat "$T/rc") out=$(cat "$T/out")"
fi
rm -rf "$T"

# --- the hand-off -----------------------------------------------------------------
setup
spec='{"job_id":"j1","bootstrap_token":"cbx_b_0123456789abcdef_SECRET"}'
run_entrypoint FAKE_UID=0 CLAUDEBOX_JOB="$spec" OTHER=leftover
if [ "$(cat "$T/rc")" = 0 ] && [ "$(cat "$T/setpriv.stdin")" = "$spec" ]; then
  ok "the spec reaches setpriv on stdin, verbatim"
else
  bad "the spec reaches setpriv on stdin" "rc=$(cat "$T/rc") out=$(cat "$T/out") stdin=$(cat "$T/setpriv.stdin" 2>/dev/null)"
fi
if [ ! -e "$T/chown.log" ] && [ -z "$(ls -A "$T/home")" ]; then
  ok "root writes no file and changes no owner"
else
  bad "root writes no file and changes no owner" "chown: $(cat "$T/chown.log" 2>/dev/null) home: $(ls -A "$T/home")"
fi
if ! grep -q '^CLAUDEBOX_JOB=' "$T/setpriv.env" && ! grep -q SECRET "$T/setpriv.env"; then
  ok "CLAUDEBOX_JOB is unset before setpriv, so the token never reaches the reviewer's environment"
else
  bad "CLAUDEBOX_JOB is unset before setpriv" "$(grep -n 'CLAUDEBOX_JOB\|SECRET' "$T/setpriv.env")"
fi
expected="--no-new-privs
--bounding-set=-all
--inh-caps=-all
--reuid=reviewer
--regid=reviewer
--init-groups
--reset-env
$T/wrapper.sh"
if [ "$(cat "$T/setpriv.argv")" = "$expected" ]; then
  ok "setpriv drops every capability, sets no_new_privs, changes uid and resets the environment"
else
  bad "setpriv's argv" "$(cat "$T/setpriv.argv")"
fi
if ! grep -q SECRET "$T/setpriv.argv"; then
  ok "the token is not in setpriv's argv"
else
  bad "the token is not in setpriv's argv" "$(cat "$T/setpriv.argv")"
fi
rm -rf "$T"

# --- the wrapper ------------------------------------------------------------------
T="$(mktemp -d)"
mkdir -p "$T/bin"
cat > "$T/bin/python3" <<STUB
#!/bin/sh
printf '%s\n' "\$@" > "$T/python.argv"
env > "$T/python.env"
STUB
chmod +x "$T/bin/python3"
sed -e "s|PATH=/opt/claudebox/worker/bin:|PATH=$T/bin:/opt/claudebox/worker/bin:|" \
    "$SCRIPT_DIR/worker/wrapper.sh" > "$T/wrapper.sh"
env -i LEAK=1 GH_TOKEN=x "$(command -v sh)" "$T/wrapper.sh"
names="$(cut -d= -f1 "$T/python.env" | grep -v '^PWD$' | grep -v '^SHLVL$' | grep -v '^_$' | sort | tr '\n' ' ')"
if [ "$names" = "HOME PATH " ]; then
  ok "the wrapper hands the worker exactly HOME and PATH"
else
  bad "the wrapper hands the worker exactly HOME and PATH" "got: $names"
fi
if grep -qx 'HOME=/home/reviewer' "$T/python.env" && grep -q '^PATH=.*/opt/claudebox/worker/bin:/usr/local/bin' "$T/python.env"; then
  ok "the gh shim's directory is ahead of the real gh on PATH"
else
  bad "the gh shim's directory is ahead of the real gh" "$(grep '^PATH\|^HOME' "$T/python.env")"
fi
if [ "$(cat "$T/python.argv")" = /opt/claudebox/worker/worker.py ]; then
  ok "the wrapper runs the worker"
else
  bad "the wrapper runs the worker" "$(cat "$T/python.argv")"
fi
rm -rf "$T"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
```

- [ ] **Step 2: Run it to see it fail**

Run: `/bin/bash ./test-worker.sh`
Expected: FAIL: `sed` cannot read `worker/entrypoint.sh`, and every check fails.

- [ ] **Step 3: Implement**

`worker/entrypoint.sh` (mode 755):

```bash
#!/usr/bin/env bash
# The hosted worker's entrypoint. It starts as root on every target and
# establishes the reviewer's posture itself, because Fly Machines and Cloud Run
# Jobs expose no --cap-drop or no-new-privileges switch (spec, "Worker
# hardening"). Root holds CAP_SETPCAP here, which emptying the bounding set
# needs, and CAP_SETUID/CAP_SETGID, which the uid change needs; the local
# Docker runner starts the container with exactly those three and no others.
#
# The job spec arrives in CLAUDEBOX_JOB. setpriv --reset-env clears the
# environment, and the spec must not reach the reviewer's environment or argv
# anyway, so root hands it to the worker on stdin. Root writes no file: with
# only those three capabilities it holds neither CAP_DAC_OVERRIDE, to write
# into the reviewer's home, nor CAP_CHOWN, to hand a file over.
set -euo pipefail

# A fixed path, not configuration. test-worker.sh rewrites this line.
WRAPPER=/opt/claudebox/worker/wrapper.sh

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "the worker entrypoint must start as root; it drops to the reviewer itself"
[ -n "${CLAUDEBOX_JOB:-}" ] || die "CLAUDEBOX_JOB is not set; the job runner passes the job spec in it"

job="$CLAUDEBOX_JOB"
unset CLAUDEBOX_JOB

exec setpriv --no-new-privs --bounding-set=-all --inh-caps=-all \
  --reuid=reviewer --regid=reviewer --init-groups --reset-env "$WRAPPER" <<<"$job"
```

`worker/wrapper.sh` (mode 755):

```sh
#!/bin/sh
# Runs as the reviewer, after setpriv. Sets exactly HOME and a PATH with the
# gh shim's directory first, and nothing else, then execs the worker. Nothing
# from root's environment survives setpriv --reset-env, and nothing is added
# here beyond these two. test-worker.sh rewrites the exec line's paths.
exec env -i HOME=/home/reviewer PATH=/opt/claudebox/worker/bin:/usr/local/bin:/usr/bin:/bin python3 /opt/claudebox/worker/worker.py
```

- [ ] **Step 4: Run it to see it pass**

Run: `/bin/bash ./test-worker.sh` and `bash -n worker/entrypoint.sh && sh -n worker/wrapper.sh`
Expected: `10 passed, 0 failed`.

- [ ] **Step 5: Commit**

```bash
git add worker/entrypoint.sh worker/wrapper.sh test-worker.sh
git commit -m "worker: the root entrypoint that drops to the reviewer with setpriv"
```

### Task 3: The gh shim

**Files:**
- Create: `worker/bin/gh` (mode 755, no `.py` suffix: it must be named `gh` on `PATH`)
- Test: `tests/test_worker_gh.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. At run time it reads `CLAUDEBOX_CHANNEL_URL`, `CLAUDEBOX_CAPABILITY`, `CLAUDEBOX_PERSONA`, `CLAUDEBOX_PR` and `HOME`, which Task 4 sets.
- Produces:
  - `REAL_GH = "/usr/bin/gh"`, `READS`, `API_BOOL_FLAGS`, `API_VALUE_FLAGS` and `REFUSED`.
  - `api_problem(args) -> Optional[str]`: why `gh api <args>` is refused, or None.
  - `parse_comment(args) -> (pr, body)`, raising `ValueError` naming what is wrong.
  - `next_seq(home, persona) -> int`, and `post_finding(args) -> int`.
  - `main(argv) -> int`, which `os.execv`s the real `gh` for an allowed read.

- [ ] **Step 1: Write the failing test**

`tests/test_worker_gh.py`:

```python
import _path  # noqa: F401

import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def load_shim():
    """worker/bin/gh has no .py suffix, because it must be named gh on PATH."""
    path = os.path.join(ROOT, "worker", "bin", "gh")
    loader = importlib.machinery.SourceFileLoader("gh_shim", path)
    spec = importlib.util.spec_from_loader("gh_shim", loader)
    module = importlib.util.module_from_spec(spec)
    # No bytecode beside it: worker/ is copied into the image whole.
    saved, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = saved
    return module


class ShimParseTest(unittest.TestCase):
    def setUp(self):
        self.gh = load_shim()

    def test_body_and_body_file_spellings(self):
        self.assertEqual(self.gh.parse_comment(["7", "--body", "x"]), ("7", "x"))
        self.assertEqual(self.gh.parse_comment(["7", "-b", "x"]), ("7", "x"))
        self.assertEqual(self.gh.parse_comment(["--body=x", "7"]), ("7", "x"))
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("from file")
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(self.gh.parse_comment(["7", "-F", f.name]), ("7", "from file"))

    def test_other_comment_flags_and_a_missing_body_are_refused(self):
        for args in (["7", "--edit-last", "--body", "x"], ["7"], ["7", "8", "--body", "x"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.gh.parse_comment(args)

    def test_writes_are_refused_before_the_real_gh(self):
        with mock.patch.object(self.gh.os, "execv") as execv, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            for argv in (["pr", "review", "7", "--approve"], ["pr", "merge", "7"],
                         ["issue", "comment", "7", "-b", "x"],
                         ["api", "-X", "POST", "repos/a/b/issues/7/comments"],
                         ["api", "repos/a/b/pulls/7/comments", "-f", "body=x"],
                         ["api", "--method=PATCH", "x"],
                         ["api", "-XPOST", "repos/a/b/issues/7/comments"],
                         ["api", "-X=POST", "repos/a/b/issues/7/comments"],
                         ["api", "-XDELETE", "repos/a/b"],
                         ["api", "repos/a/b/issues/7/comments", "-f=body=x"],
                         ["api", "repos/a/b/issues/7/comments", "-fbody=x"],
                         ["api", "repos/a/b/issues/7/comments", "-Fbody=@f"],
                         ["api", "-iXPOST", "repos/a/b/issues/7/comments"],
                         ["api", "--input=f", "repos/a/b/issues/7/comments"],
                         ["api", "graphql"],
                         ["api", "https://evil.example/x"],
                         ["api", "--hostname", "evil.example", "user"],
                         ["api", "-H", "X-HTTP-Method-Override: POST", "x"],
                         ["api", "a", "b"]):
                with self.subTest(argv=argv):
                    self.assertEqual(self.gh.main(argv), 1)
            execv.assert_not_called()

    def test_reads_go_to_the_real_gh(self):
        with mock.patch.object(self.gh.os, "execv") as execv:
            for argv in (["pr", "diff", "7"], ["pr", "view", "7", "--json", "title"],
                         ["api", "repos/a/b/pulls/7/comments"], ["search", "prs", "x"],
                         ["api", "--paginate", "-q", ".[].body", "repos/a/b/pulls/7/comments"],
                         ["api", "--jq=.title", "repos/a/b/pulls/7"], ["api", "-q.title", "x"]):
                with self.subTest(argv=argv):
                    self.gh.main(argv)
                    self.assertEqual(execv.call_args.args[1][1:], argv)


class Channel(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.headers["Authorization"], body))
        status, reply = self.server.reply
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ShimPostTest(unittest.TestCase):
    def setUp(self):
        self.gh = load_shim()
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Channel)
        self.server.seen, self.server.reply = [], (200, {"result": "queued"})
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {"HOME": self.home, "CLAUDEBOX_PERSONA": "sage", "CLAUDEBOX_PR": "7",
                    "CLAUDEBOX_CAPABILITY": "cbx_c_cap",
                    "CLAUDEBOX_CHANNEL_URL": f"http://127.0.0.1:{self.server.server_port}"}

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            rc = self.gh.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_a_queued_finding_prints_queued_and_counts_per_pair(self):
        self.assertEqual(self.call(["pr", "comment", "7", "--body", "one"]), (0, "queued\n", ""))
        self.call(["pr", "comment", "7", "--body", "two"])
        self.assertEqual([(a, b["seq"], b["body"]) for a, b in self.server.seen],
                         [("Bearer cbx_c_cap", 1, "one"), ("Bearer cbx_c_cap", 2, "two")])
        self.assertEqual(self.server.seen[0][1]["persona"], "sage")

    def test_a_withheld_finding_exits_non_zero_with_the_deployments_message(self):
        self.server.reply = (422, {"result": "withheld", "message": "contained a credential"})
        rc, out, err = self.call(["pr", "comment", "7", "--body", "ghp_x"])
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("contained a credential", err)

    def test_another_pr_is_refused_without_a_request(self):
        rc, _, err = self.call(["pr", "comment", "8", "--body", "x"])
        self.assertEqual(rc, 1)
        self.assertIn("#7", err)
        self.assertEqual(self.server.seen, [])

    def test_the_seq_file_is_private(self):
        self.call(["pr", "comment", "7", "--body", "x"])
        mode = os.stat(os.path.join(self.home, ".claudebox", "seq-sage")).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it to see it fail**

Run: `./test-python.sh -p 'test_worker_gh.py'`
Expected: FAIL: `FileNotFoundError` for `worker/bin/gh`.

- [ ] **Step 3: Implement**

`worker/bin/gh`:

```python
#!/usr/bin/env python3
"""claudebox's gh shim: first on the worker's PATH, ahead of the real gh.

`gh pr comment` becomes the channel's post-finding, which hands the finding
to the control plane's outbox, so the reviewer never writes to GitHub. Other
reads go to the real gh with the job's read token. Anything that would write
is refused with a message telling the model what to do instead, rather than
left to fail at GitHub with a permissions error that reads like a broken
token.

Persona text keeps saying `gh pr comment`, so nothing in the prompts changes.
"""

import fcntl
import json
import os
import sys
import urllib.error
import urllib.request

# test-worker.sh rewrites this line.
REAL_GH = "/usr/bin/gh"

# Reads the reviewer may make. Everything else is refused. `gh api` is checked
# separately, by api_problem.
READS = {("pr", "view"), ("pr", "diff"), ("pr", "list"), ("pr", "status"),
         ("issue", "view"), ("issue", "list"), ("repo", "view"), ("auth", "status"),
         ("search", None), ("run", "view"), ("run", "list")}
# `gh api` flags allowed, by allowlist rather than by spotting writes: pflag
# accepts attached short values (-XPOST, -fbody=x) and combined flags, so a
# list of forbidden spellings is never complete. None of these changes the
# method, the host or the body.
API_BOOL_FLAGS = {"--paginate", "--slurp", "-i", "--include", "--silent", "--verbose"}
API_VALUE_FLAGS = {"-q": "--jq", "--jq": "--jq", "-t": "--template", "--template": "--template",
                   "--cache": "--cache"}
REFUSED = ("This deployment posts review findings only with `gh pr comment <number> --body "
           "...`, one comment per finding, and allows nothing else that writes to GitHub.")


def fail(message: str) -> int:
    sys.stderr.write(message + "\n")
    return 1


def parse_comment(args):
    """(pr, body) from `gh pr comment` arguments, or raise ValueError naming what is wrong."""
    pr, body = None, None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-b", "--body"):
            if i + 1 >= len(args):
                raise ValueError(f"{a} needs a value")
            body, i = args[i + 1], i + 2
            continue
        if a.startswith("--body="):
            body, i = a[len("--body="):], i + 1
            continue
        if a in ("-F", "--body-file"):
            if i + 1 >= len(args):
                raise ValueError(f"{a} needs a file")
            name = args[i + 1]
            body = sys.stdin.read() if name == "-" else open(name, encoding="utf-8").read()
            i += 2
            continue
        if a.startswith("-"):
            raise ValueError(f"`gh pr comment {a}` is not available here; use --body or --body-file")
        if pr is not None:
            raise ValueError("give one pull request number")
        pr, i = a, i + 1
    if body is None:
        raise ValueError("give the finding with --body or --body-file")
    return pr, body


def next_seq(home: str, persona: str) -> int:
    """This pair's next finding number, counted in a file the pair's own processes share."""
    directory = os.path.join(home, ".claudebox")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, f"seq-{persona}")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        current = int(f.read().strip() or 0) + 1
        f.seek(0)
        f.truncate()
        f.write(str(current))
    return current


def post_finding(args) -> int:
    env = os.environ
    try:
        pr, body = parse_comment(args)
    except (ValueError, OSError) as exc:
        return fail(f"gh pr comment: {exc}")
    if pr is not None and pr.lstrip("#") != env.get("CLAUDEBOX_PR"):
        return fail(f"gh pr comment: this review is of pull request #{env.get('CLAUDEBOX_PR')}; "
                    f"comment on that one only")
    persona = env.get("CLAUDEBOX_PERSONA", "")
    payload = {"persona": persona, "seq": next_seq(env.get("HOME", ""), persona), "body": body}
    req = urllib.request.Request(
        env["CLAUDEBOX_CHANNEL_URL"].rstrip("/") + "/channel/post-finding",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {env['CLAUDEBOX_CAPABILITY']}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read() or b"{}")
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            data = json.loads(exc.read() or b"{}")
        except ValueError:
            data = {}
    except (urllib.error.URLError, OSError) as exc:
        return fail(f"gh pr comment: the deployment could not be reached ({exc}); the finding "
                    "was not posted")
    if status == 200 and data.get("result") == "queued":
        print("queued")
        return 0
    return fail(data.get("message") or f"gh pr comment: refused ({status}); the finding was not posted")


def api_problem(args):
    """Why `gh api <args>` is refused, or None for a plain GET of one endpoint."""
    endpoints = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in API_BOOL_FLAGS:
            i += 1
            continue
        if a in API_VALUE_FLAGS:
            if i + 1 >= len(args):
                return f"{a} needs a value"
            i += 2
            continue
        name = a.split("=", 1)[0]
        if a.startswith("--") and "=" in a and name in API_VALUE_FLAGS:
            i += 1
            continue
        if a[:2] in ("-q", "-t") and len(a) > 2:      # attached value, e.g. -q.title
            i += 1
            continue
        if a.startswith("-"):
            return f"{name} is not available"
        endpoints.append(a)
        i += 1
    if len(endpoints) != 1:
        return "give exactly one endpoint"
    endpoint = endpoints[0]
    if "://" in endpoint or endpoint.lstrip("/").split("?")[0] == "graphql":
        return "only REST paths on this repository's host are allowed"
    return None


def main(argv) -> int:
    if argv[:2] == ["pr", "comment"]:
        return post_finding(argv[2:])
    verb = (argv[0] if argv else None, argv[1] if len(argv) > 1 and not argv[1].startswith("-") else None)
    allowed = verb in READS or (verb[0], None) in READS
    if argv[:1] == ["api"]:
        problem = api_problem(argv[1:])
        if problem:
            return fail(f"gh api: {problem}; only a plain GET of one REST path is allowed. {REFUSED}")
        allowed = True
    if not allowed:
        return fail(f"gh {' '.join(argv[:2])}: not available. {REFUSED}")
    os.execv(REAL_GH, [REAL_GH] + argv)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run it to see it pass, then the whole suite**

Run: `chmod 755 worker/bin/gh`, then `./test-python.sh -p 'test_worker_gh.py'`, then `./test-python.sh`
Expected: PASS, 8 tests. The whole suite passes, and no `__pycache__` appears under `worker/bin/`.

- [ ] **Step 5: Commit**

```bash
git add worker/bin/gh tests/test_worker_gh.py
git commit -m "worker: the gh shim, which turns gh pr comment into post-finding"
```

### Task 4: The worker

**Files:**
- Create: `worker/worker.py`
- Modify: `tests/_path.py` (add `worker/`)
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `passes.run_pass` and `passes.PassResult`, `common.Pair`, `common.log`, `common.ConfigError`, and `review_loop.lock_git_dir`, all from `reviewer/` unchanged. Task 3's environment variables.
- Produces:
  - `WorkerError(Exception)`; `TRANSCRIPT_MAX = 64 * 1024 * 1024`; `MODEL_VARS`; `AUTH_ENVS = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")`.
  - `posture_problems(status_text, uid) -> List[str]`; `encode_cwd(path) -> str`; `transcript_path(home, cwd, session_id) -> str`; `session_id_of(transcript: bytes) -> Optional[str]`; `JOB_SPEC_MAX = 65536`; `read_job_spec(stream) -> dict`, from the binary stdin.
  - `Channel(base_url, opener=None)` with `post(path, token, payload) -> (status, dict)`, following no redirect.
  - `git(args, cwd=None, run=subprocess.run) -> str`, `clone(job, repo_dir, run=subprocess.run)`, and `claude_env(job, home, path, provider_token, capability, persona) -> Dict[str, str]`.
  - `Worker(home, path, channel, run=subprocess.run, popen=subprocess.Popen)` with `repo_dir` (`<home>/work/repo`), `fetch_job(bootstrap_token) -> (capability, job)`, `prepare(job)`, `run_pair(job, capability, pair) -> Optional[PassResult]`, `complete(capability, tag, result)` and `run_group(job, capability)`.
  - `main() -> int`, whose first log line names every environment variable it was started with (names only).

- [ ] **Step 1: Put worker/ on the test path**

Apply this change to `tests/_path.py`:

```diff
diff --git a/tests/_path.py b/tests/_path.py
index 5fb27c7..1c7c4d4 100644
--- a/tests/_path.py
+++ b/tests/_path.py
@@ -1,4 +1,4 @@
-"""Puts reviewer/ and hosted/ on sys.path, and guards the checkout against the test suite.
+"""Puts reviewer/, hosted/ and worker/ on sys.path, and guards the checkout against the test suite.
 
 Every test module imports this, which is the only reason the guard lives in a
 path shim: it is the one place the whole suite passes through, so it covers a
@@ -14,8 +14,9 @@ import stat
 import sys
 
 _ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
-# hosted/ first, reviewer/ in front of it: a hosted module imports the decision
-# core by plain name, exactly as it does inside the control-plane image.
+# worker/, then hosted/, then reviewer/ in front of both: a hosted or worker
+# module imports the decision core by plain name, as it does inside its image.
+sys.path.insert(0, os.path.join(_ROOT, "worker"))
 sys.path.insert(0, os.path.join(_ROOT, "hosted"))
 sys.path.insert(0, os.path.join(_ROOT, "reviewer"))
 
```

- [ ] **Step 2: Write the failing test**

`tests/test_worker.py`:

```python
import _path  # noqa: F401

import base64
import io
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import worker
from worker import (Worker, session_id_of, WorkerError, claude_env, clone, encode_cwd,
                    posture_problems, read_job_spec, transcript_path)

GOOD_STATUS = "Name:\tpython3\nNoNewPrivs:\t1\nCapBnd:\t0000000000000000\n"
HEAD = "a" * 40


class PostureTest(unittest.TestCase):
    def test_the_hardened_posture_passes(self):
        self.assertEqual(posture_problems(GOOD_STATUS, 1000), [])

    def test_each_missing_property_is_named(self):
        self.assertEqual(posture_problems(GOOD_STATUS, 0), ["running as root"])
        self.assertEqual(posture_problems(GOOD_STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"), 1000),
                         ["NoNewPrivs is not 1"])
        self.assertEqual(posture_problems(GOOD_STATUS.replace("0000000000000000", "00000000a80425fb"), 1000),
                         ["the capability bounding set is not empty (CapBnd 00000000a80425fb)"])

    def test_an_unreadable_status_fails_every_kernel_check(self):
        self.assertEqual(posture_problems("", 1000),
                         ["NoNewPrivs is not 1",
                          "the capability bounding set is not empty (CapBnd absent)"])


class PathTest(unittest.TestCase):
    def test_the_cwd_is_encoded_as_claude_code_names_its_project_directory(self):
        self.assertEqual(encode_cwd("/home/reviewer/work/repo"), "-home-reviewer-work-repo")
        self.assertEqual(encode_cwd("/a/b.c_d"), "-a-b-c-d")

    def test_the_transcript_path_is_where_resume_looks(self):
        self.assertEqual(transcript_path("/home/reviewer", "/home/reviewer/work/repo", "abc-123"),
                         "/home/reviewer/.claude/projects/-home-reviewer-work-repo/abc-123.jsonl")

    def test_a_session_id_that_could_escape_is_refused(self):
        for bad in ("../x", "a/b", "", "a" * 65):
            with self.subTest(bad=bad), self.assertRaises(WorkerError):
                transcript_path("/h", "/h/w", bad)


class JobSpecTest(unittest.TestCase):
    SPEC = {"job_id": "j1", "bootstrap_token": "cbx_b_x", "channel_url": "http://c",
            "gateway_url": "http://g"}

    def test_the_spec_is_read_from_stdin_with_the_here_strings_newline(self):
        self.assertEqual(read_job_spec(io.BytesIO(json.dumps(self.SPEC).encode() + b"\n")),
                         self.SPEC)

    def test_an_empty_stdin_says_where_the_spec_should_come_from(self):
        with self.assertRaises(WorkerError) as ctx:
            read_job_spec(io.BytesIO(b""))
        self.assertIn("no job spec on stdin", str(ctx.exception))

    def test_an_oversized_spec_is_refused(self):
        with self.assertRaises(WorkerError):
            read_job_spec(io.BytesIO(b" " * (worker.JOB_SPEC_MAX + 1)))

    def test_a_spec_that_is_not_an_object_or_lacks_a_field_is_refused(self):
        for raw, words in ((b"[1]", "not a JSON object"), (b"nope", "not JSON"),
                           (json.dumps(dict(self.SPEC, gateway_url="")).encode(), "no gateway_url")):
            with self.subTest(raw=raw), self.assertRaises(WorkerError) as ctx:
                read_job_spec(io.BytesIO(raw))
            self.assertIn(words, str(ctx.exception))


class FakeRun:
    def __init__(self, head=HEAD):
        self.calls = []
        self.head = head

    def __call__(self, argv, cwd=None, capture_output=None, text=None):
        self.calls.append(argv)
        out = self.head + "\n" if argv[1:] == ["rev-parse", "HEAD"] else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


def job(**over):
    j = {"job_id": "j1", "repo": "acme/web", "pr": 7, "head": HEAD, "mode": "code",
         "clone_url": "https://github.com/acme/web.git", "read_token": "ghs_read",
         "model": "glm-5.2:cloud", "auth_env": "ANTHROPIC_AUTH_TOKEN",
         "gateway_url": "http://gateway:8090", "channel_url": "http://control:8080",
         "max_concurrent_personas": 0, "shared_worktree": False, "pairs": []}
    j.update(over)
    return j


class CloneTest(unittest.TestCase):
    def test_a_blobless_clone_of_the_head_with_the_token_as_a_github_scoped_header(self):
        run = FakeRun()
        clone(job(), "/w/repo", run=run)
        basic = base64.b64encode(b"x-access-token:ghs_read").decode()
        self.assertEqual(run.calls[0], ["git", "-c",
                                        f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}",
                                        "clone", "--filter=blob:none", "--no-checkout",
                                        "https://github.com/acme/web.git", "/w/repo"])
        self.assertIn(["git", "checkout", "--detach", HEAD], run.calls)

    def test_a_clone_url_off_github_is_refused_before_git_runs(self):
        run = FakeRun()
        with self.assertRaises(WorkerError):
            clone(job(clone_url="https://evil.example/acme/web.git"), "/w/repo", run=run)
        self.assertEqual(run.calls, [])

    def test_a_head_that_is_not_a_commit_id_is_refused(self):
        with self.assertRaises(WorkerError):
            clone(job(head="main"), "/w/repo", run=FakeRun())

    def test_a_checkout_that_lands_elsewhere_is_an_error(self):
        with self.assertRaises(WorkerError) as ctx:
            clone(job(), "/w/repo", run=FakeRun(head="b" * 40))
        self.assertIn("not the job's head", str(ctx.exception))


class EnvTest(unittest.TestCase):
    def test_claude_gets_exactly_what_it_needs(self):
        env = claude_env(job(), "/home/reviewer", "/shim:/usr/bin", "cbx_p_t", "cbx_c_cap", "sage")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "cbx_p_t")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://gateway:8090")
        self.assertEqual(env["GH_TOKEN"], "ghs_read")
        self.assertEqual((env["CLAUDEBOX_PERSONA"], env["CLAUDEBOX_PR"]), ("sage", "7"))
        self.assertEqual({env[v] for v in worker.MODEL_VARS}, {"glm-5.2:cloud"})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(set(env), {"HOME", "PATH", "DISABLE_AUTOUPDATER", "ANTHROPIC_BASE_URL",
                                    "ANTHROPIC_AUTH_TOKEN", "GH_TOKEN", "CLAUDEBOX_CHANNEL_URL",
                                    "CLAUDEBOX_CAPABILITY", "CLAUDEBOX_PERSONA", "CLAUDEBOX_PR",
                                    *worker.MODEL_VARS})

    def test_an_oauth_profile_gets_its_token_as_the_oauth_variable(self):
        env = claude_env(job(auth_env="CLAUDE_CODE_OAUTH_TOKEN"), "/h", "/p", "cbx_p_t", "c", "sage")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "cbx_p_t")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)


class HelpersTest(unittest.TestCase):
    def test_session_id_of_reads_the_first_valid_id(self):
        data = b'not json\n{"type": "summary"}\n{"sessionId": "../x"}\n{"sessionId": "abc-1"}\n'
        self.assertEqual(session_id_of(data), "abc-1")
        self.assertIsNone(session_id_of(b'{"x": 1}\n'))


class PrepareTest(unittest.TestCase):
    def test_the_git_dir_is_locked_exactly_when_the_job_says_the_worktree_is_shared(self):
        for shared in (True, False):
            home = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, home)
            w = Worker(home, "/p", None, run=FakeRun())
            os.makedirs(os.path.join(w.repo_dir, ".git", "refs", "heads"))
            with self.subTest(shared=shared), mock.patch.object(worker, "clone"):
                w.prepare(job(shared_worktree=shared, pairs=[{"persona": "a"}]))
                writable = os.access(os.path.join(w.repo_dir, ".git"), os.W_OK)
                self.assertEqual(writable, not shared)
            for d in (".git", ".git/refs", ".git/refs/heads"):
                os.chmod(os.path.join(w.repo_dir, d), 0o700)

    def test_a_job_that_does_not_say_is_refused(self):
        class C:
            def post(self, path, token, payload):
                return 200, {"capability": "c", "job": dict(job(), shared_worktree=None)}
        with self.assertRaises(WorkerError) as ctx:
            Worker("/h", "/p", C()).fetch_job("b")
        self.assertIn("share the worktree", str(ctx.exception))


class FakeChannel:
    def __init__(self, refuse=()):
        self.calls = []
        self.refuse = set(refuse)

    def post(self, path, token, payload):
        self.calls.append((path, token, payload))
        if path == "/channel/start-pair":
            if payload["persona"] in self.refuse:
                return 409, {"reason": "visibility changed"}
            return 200, {"provider_token": f"cbx_p_{payload['persona']}"}
        return 200, {}


class FakeClaude:
    """Stands in for `claude -p`: writes a transcript where Claude Code would and streams its id."""

    def __init__(self, home, rc=0, stderr="", turn="T"):
        self.home, self.rc, self.stderr, self.turn = home, rc, stderr, turn
        self.calls = []

    def __call__(self, argv, stdout=None, stderr=None, cwd=None, text=None, bufsize=None, env=None):
        self.calls.append({"argv": argv, "env": env, "cwd": cwd})
        persona = env["CLAUDEBOX_PERSONA"]
        sid = argv[argv.index("--resume") + 1] if "--resume" in argv else f"sess-{persona.replace('_', '-')}"
        path = transcript_path(self.home, cwd, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps({"sessionId": sid, "turn": self.turn, "persona": persona}) + "\n")
        stderr.write(self.stderr)
        line = json.dumps({"type": "system", "subtype": "init", "session_id": sid}) + "\n"
        return SimpleNamespace(stdout=io.StringIO(line), wait=lambda: self.rc)


class RunGroupTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)

    def make(self, channel, claude):
        w = Worker(self.home, "/shim:/usr/bin", channel, run=FakeRun(), popen=claude)
        os.makedirs(w.repo_dir)
        return w

    def pair(self, persona, phase=1, **over):
        p = {"persona": persona, "phase": phase, "system_prompt": f"you are {persona}",
             "prompt": "review #7", "session_id": None, "transcript_b64": None}
        p.update(over)
        return p

    def test_phase_two_starts_only_after_phase_one(self):
        channel, claude = FakeChannel(), FakeClaude(self.home)
        w = self.make(channel, claude)
        w.run_group(job(pairs=[self.pair("sage", 2), self.pair("red_team"), self.pair("sme")]), "cap")
        started = [c[2]["persona"] for c in channel.calls if c[0] == "/channel/start-pair"]
        self.assertEqual(started[-1], "sage")
        self.assertEqual(set(started[:2]), {"red_team", "sme"})

    def test_a_usage_limit_in_phase_one_withholds_phase_two(self):
        channel = FakeChannel()
        claude = FakeClaude(self.home, rc=1, stderr="API Error: 429 rate limit exceeded\n")
        w = self.make(channel, claude)
        w.run_group(job(pairs=[self.pair("red_team"), self.pair("sage", 2)]), "cap")
        self.assertNotIn("sage", [c[2]["persona"] for c in channel.calls])

    def test_a_refused_start_pair_runs_nothing_and_completes_nothing(self):
        channel, claude = FakeChannel(refuse={"sme"}), FakeClaude(self.home)
        w = self.make(channel, claude)
        w.run_group(job(pairs=[self.pair("sme")]), "cap")
        self.assertEqual(claude.calls, [])
        self.assertEqual([c[0] for c in channel.calls], ["/channel/start-pair"])

    def test_each_pair_runs_with_its_own_provider_token_and_the_capability(self):
        channel, claude = FakeChannel(), FakeClaude(self.home)
        w = self.make(channel, claude)
        w.run_group(job(pairs=[self.pair("red_team"), self.pair("sme")]), "cbx_c_cap")
        tokens = {c["env"]["CLAUDEBOX_PERSONA"]: c["env"]["ANTHROPIC_AUTH_TOKEN"] for c in claude.calls}
        self.assertEqual(tokens, {"red_team": "cbx_p_red_team", "sme": "cbx_p_sme"})
        self.assertEqual({c["env"]["CLAUDEBOX_CAPABILITY"] for c in claude.calls}, {"cbx_c_cap"})
        self.assertEqual({c["argv"][c["argv"].index("--append-system-prompt") + 1]
                          for c in claude.calls}, {"you are red_team", "you are sme"})

    def test_a_pair_run_twice_in_fresh_containers_resumes_with_the_first_runs_turns(self):
        # The spec's resume assertion: a broken resume degrades silently to a
        # fresh session that re-raises everything, so run the pair twice, each
        # time in its own home, handing the second run what the first sent back.
        first = FakeChannel()
        self.make(first, FakeClaude(self.home, turn="T1")).run_group(
            job(pairs=[self.pair("sage")]), "cap")
        sent = [c[2] for c in first.calls if c[0] == "/channel/complete-pair"][0]
        second_home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, second_home)
        second, claude = FakeChannel(), FakeClaude(second_home, turn="T2")
        w = Worker(second_home, "/shim:/usr/bin", second, run=FakeRun(), popen=claude)
        os.makedirs(w.repo_dir)
        w.run_group(job(pairs=[self.pair("sage", session_id=sent["session_id"],
                                         transcript_b64=sent["transcript_b64"])]), "cap")
        argv = claude.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], sent["session_id"])
        done = [c[2] for c in second.calls if c[0] == "/channel/complete-pair"][0]
        text = base64.b64decode(done["transcript_b64"]).decode()
        self.assertEqual([json.loads(l)["turn"] for l in text.splitlines()], ["T1", "T2"])
        self.assertEqual((done["session_id"], done["notes"]["transcript"]), (sent["session_id"], "ok"))

    def test_the_session_id_comes_from_the_transcript_not_the_field_beside_it(self):
        channel, claude = FakeChannel(), FakeClaude(self.home)
        w = self.make(channel, claude)
        earlier = base64.b64encode(json.dumps({"sessionId": "sess-real"}).encode() + b"\n").decode()
        w.run_group(job(pairs=[self.pair("sage", session_id="sess-other",
                                         transcript_b64=earlier)]), "cap")
        argv = claude.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], "sess-real")

    def test_a_transcript_with_no_session_id_starts_fresh(self):
        channel, claude = FakeChannel(), FakeClaude(self.home)
        w = self.make(channel, claude)
        junk = base64.b64encode(b'{"turn": 1}\nnot json\n').decode()
        w.run_group(job(pairs=[self.pair("sage", session_id="sess-7", transcript_b64=junk)]), "cap")
        self.assertNotIn("--resume", claude.calls[0]["argv"])

    def test_a_pass_that_cannot_spawn_completes_as_a_failure_and_the_group_goes_on(self):
        channel = FakeChannel()
        claude = FakeClaude(self.home)

        def popen(argv, **kw):
            if kw["env"]["CLAUDEBOX_PERSONA"] == "red_team":
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return claude(argv, **kw)

        w = self.make(channel, popen)
        w.run_group(job(pairs=[self.pair("red_team"), self.pair("sme"), self.pair("sage", 2)]), "cap")
        done = {c[2]["persona"]: c[2] for c in channel.calls if c[0] == "/channel/complete-pair"}
        self.assertEqual(set(done), {"red_team", "sme", "sage"})
        self.assertEqual((done["red_team"]["notes"]["rc"], done["red_team"]["notes"]["limited"]), (1, False))

    def test_a_session_id_with_no_transcript_starts_fresh(self):
        channel, claude = FakeChannel(), FakeClaude(self.home)
        w = self.make(channel, claude)
        w.run_group(job(pairs=[self.pair("sage", session_id="sess-7")]), "cap")
        self.assertNotIn("--resume", claude.calls[0]["argv"])

    def test_an_over_cap_transcript_is_reported_and_not_sent(self):
        channel, claude = FakeChannel(), FakeClaude(self.home)
        w = self.make(channel, claude)
        with mock.patch.object(worker, "TRANSCRIPT_MAX", 10):
            w.run_group(job(pairs=[self.pair("sage")]), "cap")
        done = [c[2] for c in channel.calls if c[0] == "/channel/complete-pair"][0]
        self.assertIsNone(done["transcript_b64"])
        self.assertEqual(done["notes"]["transcript"], "over-cap")

    def test_the_concurrency_cap_bounds_each_phase_pool(self):
        channel, claude = FakeChannel(), FakeClaude(self.home)
        w = self.make(channel, claude)
        with mock.patch.object(worker, "ThreadPoolExecutor", wraps=worker.ThreadPoolExecutor) as pool:
            w.run_group(job(max_concurrent_personas=1,
                            pairs=[self.pair("red_team"), self.pair("sme")]), "cap")
        self.assertEqual(pool.call_args.kwargs["max_workers"], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run it to see it fail**

Run: `./test-python.sh -p 'test_worker.py'`
Expected: FAIL: `ModuleNotFoundError: No module named 'worker'`.

- [ ] **Step 4: Implement**

`worker/worker.py`:

```python
"""The hosted worker: one PR group, reviewed as the reviewer uid.

Started by wrapper.sh after setpriv, with exactly HOME and PATH set. It
proves its posture (not root, NoNewPrivs, an empty bounding set), reads the
job spec root handed it on stdin, exchanges the bootstrap token for the
job's capability, clones the PR head, and runs each pair's persona as
run_group does in local mode: phase 1 together, then phase 2. Every pair
asks the control plane for its own provider token first, and hands its
transcript back after. The worker reports no outcome: the gateway decides
that from what it saw.

A pair whose session already exists gets its transcript written where
`claude --resume` looks for it, ~/.claude/projects/<encoded cwd>/<id>.jsonl,
which is why the clone always lives at the same path under the same HOME.
"""

import base64
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "reviewer"))

import passes  # noqa: E402
from common import ConfigError, Pair, log  # noqa: E402
from review_loop import lock_git_dir  # noqa: E402

# test-worker.sh rewrites this line to a fixture.
PROC_STATUS = "/proc/self/status"
JOB_SPEC_MAX = 65536
# The overview's channel cap: a decoded transcript of at most 64 MiB.
TRANSCRIPT_MAX = 64 * 1024 * 1024
REQUEST_TIMEOUT = 120
_SESSION_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
_PERSONA_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
MODEL_VARS = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
              "ANTHROPIC_SMALL_FAST_MODEL")
AUTH_ENVS = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


class WorkerError(Exception):
    """Something that ends the job: the worker exits non-zero with this message."""


def posture_problems(status_text: str, uid: int) -> List[str]:
    """The image's three startup checks, run as the reviewer, with no escape hatch.

    ALLOW_UNHARDENED is never set in a worker, so a target that cannot give the
    posture fails its first job loudly.
    """
    problems = []
    if uid == 0:
        problems.append("running as root")
    fields = dict(line.split(":", 1) for line in status_text.splitlines() if ":" in line)
    if fields.get("NoNewPrivs", "").strip() != "1":
        problems.append("NoNewPrivs is not 1")
    cap = fields.get("CapBnd", "").strip()
    try:
        empty = int(cap, 16) == 0
    except ValueError:
        empty = False
    if not empty:
        problems.append(f"the capability bounding set is not empty (CapBnd {cap or 'absent'})")
    return problems


def encode_cwd(path: str) -> str:
    """Claude Code's projects-directory name for a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def transcript_path(home: str, cwd: str, session_id: str) -> str:
    if not _SESSION_RE.fullmatch(session_id or ""):
        raise WorkerError(f"not a session id: {session_id!r}")
    return os.path.join(home, ".claude", "projects", encode_cwd(cwd), f"{session_id}.jsonl")


def session_id_of(transcript: bytes) -> Optional[str]:
    """The session id recorded in a Claude Code transcript, or None.

    The spec reads it from the transcript itself rather than trusting a field
    beside it: the file is what `--resume` will open, so its id is the one
    that resumes.
    """
    for line in transcript.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        sid = entry.get("sessionId") if isinstance(entry, dict) else None
        if isinstance(sid, str) and _SESSION_RE.fullmatch(sid):
            return sid
    return None


def read_job_spec(stream) -> Dict[str, Any]:
    """The job spec root handed over on stdin. Read once, capped, then stdin is closed.

    The spec carries the single-use bootstrap token, so it never travels in the
    environment or argv; stdin is a pipe only this process holds.
    """
    raw = stream.read(JOB_SPEC_MAX + 1)
    if len(raw) > JOB_SPEC_MAX:
        raise WorkerError("the job spec on stdin is too large")
    if not raw.strip():
        raise WorkerError("no job spec on stdin; the worker entrypoint passes it there")
    try:
        spec = json.loads(raw)
    except ValueError:
        raise WorkerError("the job spec on stdin is not JSON") from None
    if not isinstance(spec, dict):
        raise WorkerError("the job spec on stdin is not a JSON object")
    for key in ("job_id", "bootstrap_token", "channel_url", "gateway_url"):
        if not isinstance(spec.get(key), str) or not spec[key]:
            raise WorkerError(f"the job spec has no {key}")
    return spec


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Channel:
    """The worker channel's client. Every call carries a token; nothing follows a redirect."""

    def __init__(self, base_url: str, opener=None):
        self.base_url = base_url.rstrip("/")
        self.opener = opener or urllib.request.build_opener(_NoRedirect)

    def post(self, path: str, token: str, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
                status, body = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise WorkerError(f"the control plane is unreachable: {getattr(exc, 'reason', exc)}") from None
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {}


def git(args: List[str], cwd: Optional[str] = None, run: Callable[..., Any] = subprocess.run) -> str:
    result = run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or ["no output"]
        raise WorkerError(f"git {args[0]} failed: {tail[0][:300]}")
    return result.stdout


def clone(job: Dict[str, Any], repo_dir: str, run: Callable[..., Any] = subprocess.run) -> None:
    """A blobless clone with the PR head checked out, authenticated by the read token.

    The token goes into the clone's own config as an extra header, scoped to
    github.com, because a blobless clone fetches history lazily whenever a
    persona asks for it. The reviewer holds that token anyway in 2a (the
    spec's trial phase); 2b's read route takes it away.
    """
    url = job["clone_url"]
    if not url.startswith("https://github.com/"):
        raise WorkerError(f"refusing a clone URL off github.com: {url}")
    head = job["head"]
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", head or ""):
        raise WorkerError(f"not a commit id: {head!r}")
    basic = base64.b64encode(f"x-access-token:{job['read_token']}".encode()).decode()
    header = f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"
    git(["-c", header, "clone", "--filter=blob:none", "--no-checkout", url, repo_dir], run=run)
    git(["config", "http.https://github.com/.extraheader", f"AUTHORIZATION: basic {basic}"],
        cwd=repo_dir, run=run)
    git(["fetch", "--filter=blob:none", "origin", head], cwd=repo_dir, run=run)
    git(["checkout", "--detach", head], cwd=repo_dir, run=run)
    actual = git(["rev-parse", "HEAD"], cwd=repo_dir, run=run).strip()
    if actual != head:
        raise WorkerError(f"checked out {actual}, not the job's head {head}")


def claude_env(job: Dict[str, Any], home: str, path: str, provider_token: str,
               capability: str, persona: str) -> Dict[str, str]:
    """The environment one pair's claude runs in. Built, never inherited."""
    env = {
        "HOME": home,
        "PATH": path,
        "DISABLE_AUTOUPDATER": "1",
        "ANTHROPIC_BASE_URL": job["gateway_url"],
        job["auth_env"]: provider_token,
        "GH_TOKEN": job["read_token"],
        "CLAUDEBOX_CHANNEL_URL": job["channel_url"],
        "CLAUDEBOX_CAPABILITY": capability,
        "CLAUDEBOX_PERSONA": persona,
        "CLAUDEBOX_PR": str(job["pr"]),
    }
    for name in MODEL_VARS:
        env[name] = job["model"]
    return env


class Worker:
    def __init__(self, home: str, path: str, channel: Channel,
                 run: Callable[..., Any] = subprocess.run,
                 popen: Callable[..., Any] = subprocess.Popen):
        self.home = home
        self.path = path
        self.channel = channel
        self.run = run
        self.popen = popen
        self.repo_dir = os.path.join(home, "work", "repo")

    def fetch_job(self, bootstrap_token: str) -> Tuple[str, Dict[str, Any]]:
        status, data = self.channel.post("/channel/fetch-job", bootstrap_token, {})
        if status != 200 or "capability" not in data or "job" not in data:
            raise WorkerError(f"fetch-job was refused ({status}); the bootstrap token is "
                              "single-use and expires five minutes after dispatch")
        job = data["job"]
        if job.get("auth_env") not in AUTH_ENVS:
            raise WorkerError(f"the job names an unknown auth_env {job.get('auth_env')!r}")
        if not isinstance(job.get("shared_worktree"), bool):
            raise WorkerError("the job does not say whether its personas share the worktree")
        return data["capability"], job

    def prepare(self, job: Dict[str, Any]) -> None:
        work = os.path.dirname(self.repo_dir)
        os.makedirs(work, mode=0o700, exist_ok=True)
        if not os.access(work, os.W_OK):
            raise WorkerError(f"{work} is not writable by the reviewer")
        clone(job, self.repo_dir, run=self.run)
        # The control plane decided this once, and put WORKTREE_STANZA in the
        # prompts by the same answer; the worker honours it rather than
        # recomputing, so the stanza and the lock cannot disagree.
        if job["shared_worktree"]:
            try:
                lock_git_dir(self.repo_dir)
            except ConfigError as exc:
                raise WorkerError(str(exc)) from None

    def run_pair(self, job: Dict[str, Any], capability: str, pair: Dict[str, Any]) -> Optional[passes.PassResult]:
        persona = pair["persona"]
        if not _PERSONA_RE.fullmatch(persona or ""):
            raise WorkerError(f"not a persona name: {persona!r}")
        tag = Pair(int(job["pr"]), job["mode"], persona)
        status, data = self.channel.post("/channel/start-pair", capability, {"persona": persona})
        if status != 200 or not data.get("provider_token"):
            log(f"start-pair refused ({status}: {data.get('reason', 'no reason')}); this pair "
                "stays owed", pair=tag)
            return None
        session_id = None
        if pair.get("transcript_b64"):
            transcript = base64.b64decode(pair["transcript_b64"])
            session_id = session_id_of(transcript)
            if session_id is None:
                log("WARN: the transcript records no session id; starting a fresh session", pair=tag)
            else:
                if pair.get("session_id") not in (None, session_id):
                    log(f"WARN: the job names session {pair['session_id']} but the transcript "
                        f"is {session_id}; resuming the transcript's", pair=tag)
                target = transcript_path(self.home, self.repo_dir, session_id)
                os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                with open(target, "wb") as f:
                    f.write(transcript)
        env = claude_env(job, self.home, self.path, data["provider_token"], capability, persona)
        try:
            result = passes.run_pass(tag, pair["prompt"], session_id, pair["system_prompt"],
                                     job["model"], ["--strict-mcp-config"], self.repo_dir,
                                     popen=partial(self.popen, env=env))
        except Exception as exc:
            # A pass that never gets off the ground (EAGAIN against
            # --pids-limit, ENOMEM against --memory, no claude on PATH) is an
            # ordinary failed pass, as in local mode's _run_one. Letting it
            # out would abort the pool, and this pair, already started, would
            # never complete; nor would its siblings or the next phase.
            log(f"WARN: could not run claude: {exc!r}", pair=tag)
            result = passes.PassResult(rc=1, session_id=session_id, limited=False, limit_line="")
        self.complete(capability, tag, result)
        return result

    def complete(self, capability: str, tag: Pair, result: passes.PassResult) -> None:
        notes: Dict[str, Any] = {"rc": result.rc, "limited": result.limited, "transcript": "missing"}
        payload: Dict[str, Any] = {"persona": tag.persona, "session_id": result.session_id,
                                   "transcript_b64": None, "notes": notes}
        if result.session_id:
            try:
                path = transcript_path(self.home, self.repo_dir, result.session_id)
                size = os.path.getsize(path)
            except (WorkerError, OSError):
                size = -1
            if size > TRANSCRIPT_MAX:
                notes["transcript"] = "over-cap"
            elif size >= 0:
                with open(path, "rb") as f:
                    payload["transcript_b64"] = base64.b64encode(f.read()).decode("ascii")
                notes["transcript"] = "ok"
        status, _ = self.channel.post("/channel/complete-pair", capability, payload)
        if status != 200:
            log(f"WARN: complete-pair answered {status}", pair=tag)

    def run_group(self, job: Dict[str, Any], capability: str) -> None:
        """Phase 1 together, then phase 2, each under its own pool, as run_group does.

        A usage limit in phase 1 withholds phase 2: its pairs never start, so
        the control plane still owes them.
        """
        cap = int(job.get("max_concurrent_personas") or 0)
        phases = sorted({int(p.get("phase", 1)) for p in job["pairs"]})
        for phase in phases:
            pairs = [p for p in job["pairs"] if int(p.get("phase", 1)) == phase]
            workers = min(cap, len(pairs)) if cap > 0 else len(pairs)
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                results = list(pool.map(lambda p: self.run_pair(job, capability, p), pairs))
            if any(r is not None and r.limited for r in results):
                log(f"a usage limit in phase {phase}; later phases are withheld and stay owed")
                return


def main() -> int:
    home = os.environ.get("HOME", "")
    names = sorted(os.environ)
    log(f"worker starting; environment holds: {', '.join(names)}")
    try:
        with open(PROC_STATUS) as f:
            status_text = f.read()
    except OSError:
        status_text = ""
    problems = posture_problems(status_text, os.getuid())
    if problems:
        sys.stderr.write("ERROR: the worker's posture is wrong: " + "; ".join(problems) + "\n")
        return 1
    try:
        spec = read_job_spec(sys.stdin.buffer)
        # Release the pipe: claude and every shell a pass spawns inherit fd 0.
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
        worker = Worker(home, os.environ.get("PATH", ""), Channel(spec["channel_url"]))
        capability, job = worker.fetch_job(spec["bootstrap_token"])
        job.setdefault("gateway_url", spec["gateway_url"])
        job.setdefault("channel_url", spec["channel_url"])
        log(f"job {job['job_id']}: PR #{job['pr']} at {job['head'][:12]}, "
            f"{len(job['pairs'])} pairs ({job['mode']})")
        worker.prepare(job)
        worker.run_group(job, capability)
    except WorkerError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1
    log("job finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run it to see it pass, then the whole suite**

Run: `./test-python.sh -p 'test_worker.py'`, then `./test-python.sh`
Expected: PASS, 30 tests. The whole suite passes.

- [ ] **Step 6: Commit**

```bash
git add worker/worker.py tests/_path.py tests/test_worker.py
git commit -m "worker: one PR group, reviewed as the reviewer with a per-pair provider token"
```

### Task 5: The image and the documentation

**Files:**
- Modify: `Dockerfile` (after the `workersai-shim.py` lines, before `USER reviewer`)
- Modify: `CLAUDE.md` (a paragraph in "Hosted App libraries", after the 2a.2 paragraph; the suite list in "Commands")
- Modify: `README.md` ("Tests": the command list and one paragraph)
- Modify: `HISTORY.md` (one entry at the top of "Unreleased")

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: an image whose `/opt/claudebox/worker/` holds Task 2–4's files, root-owned.

- [ ] **Step 1: Add the worker to the image**

Apply this change to `Dockerfile`:

```diff
diff --git a/Dockerfile b/Dockerfile
index 428e0ee..4591dc2 100644
--- a/Dockerfile
+++ b/Dockerfile
@@ -164,6 +164,17 @@ RUN python3 -c 'import sys; sys.path.insert(0, "/opt/claudebox/reviewer"); impor
 # installed above with no venv of its own. See the file header for why it exists.
 COPY --chown=reviewer:reviewer workersai-shim.py /usr/local/bin/workersai-shim.py
 RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/workersai-shim.py
+# The hosted worker (phase 2a.3): the same image, started by the job runner
+# with --user 0 and --entrypoint /opt/claudebox/worker/entrypoint.sh, which
+# drops to the reviewer with setpriv. Root-owned for the reason reviewer/ is:
+# the gh shim and the worker run as the reviewer, which must never be able to
+# rewrite them. setpriv comes from util-linux; the check fails the build if a
+# base image ever ships without it, rather than a worker that cannot start.
+COPY worker/ /opt/claudebox/worker/
+RUN chmod 755 /opt/claudebox/worker/entrypoint.sh /opt/claudebox/worker/wrapper.sh \
+      /opt/claudebox/worker/bin/gh \
+ && setpriv --help | grep -q -- '--bounding-set' \
+ && test "$(sed -n 's/^REAL_GH = "\(.*\)"$/\1/p' /opt/claudebox/worker/bin/gh)" = "$(command -v gh)"
 
 USER reviewer
 WORKDIR /home/reviewer
```

The `RUN` line fails the build if `setpriv` is missing or lacks `--bounding-set`, or if the real `gh` is not where the shim's `REAL_GH` says.

- [ ] **Step 2: Build it and check the files' owner**

Run: `./claudebox.sh build && docker run --rm --entrypoint stat claudebox -c '%U %a %n' /opt/claudebox/worker /opt/claudebox/worker/worker.py /opt/claudebox/worker/bin/gh /opt/claudebox/worker/entrypoint.sh`
Expected: every line starts `root`. `gh` and `entrypoint.sh` are `755`.

- [ ] **Step 3: Describe the worker and the runner**

In `CLAUDE.md`'s "Hosted App libraries" subsection, after the paragraph about the gateway, add:

```markdown
Phase 2a.3 adds the worker and the job runner. A worker is this image started by the runner with `--user 0` and `--entrypoint /opt/claudebox/worker/entrypoint.sh`, holding only `SETUID`, `SETGID` and `SETPCAP`. The entrypoint unsets `CLAUDEBOX_JOB` and runs `setpriv` with the job spec on its stdin, writing no file, since root holds no capability that would let it write into the reviewer's home. `setpriv` empties the bounding set, sets `no_new_privs`, changes to the reviewer and resets the environment. `wrapper.sh` hands `worker.py` exactly `HOME` and `PATH`. `worker.py` proves its posture with the image's three checks and no `ALLOW_UNHARDENED`, trades the single-use bootstrap token for the job's capability, makes a blobless clone of the PR head at `/home/reviewer/work/repo`, and runs each pair through `passes.run_pass`, phase 1 together and then phase 2. It locks `.git` exactly when the job spec's `shared_worktree` says the personas share the worktree, an answer the control plane computes once and also uses to put `WORKTREE_STANZA` in the prompts, and it refuses a spec that does not say. Each pair asks the control plane for its own provider token first and hands back its transcript and session id after; the worker never reports an outcome, because the gateway decides that. A pair with a session has its transcript written where `--resume` looks for it, and its session id is read from the transcript. `worker/bin/gh` is first on `PATH`: `gh pr comment` becomes the channel's `post-finding`, a fixed set of reads goes to the real `gh` with the job's read token, and everything that writes is refused. `hosted/runner.py` is the job-runner interface and its local Docker implementation, which builds every `docker` command from one template and checks every field first. `hosted/runnerd.py` is the only process holding the Docker socket, and serves start, list, stop and stop-all over `gwproto`. `test-worker.sh` covers the two shell steps with `id`, `chown` and `setpriv` stubbed, so it needs neither Docker nor root, and it runs on the host's bash 3.2.
```

In "Commands", where the suites are listed, change "the five test suites" to "the six test suites" in the opening paragraph's file list and add `test-worker.sh` after `test-launcher.sh`, and add to the sentence that lists how to run them: `, and \`/bin/bash ./test-worker.sh\` (the hosted worker's root entrypoint and wrapper, with \`setpriv\` stubbed)`.

- [ ] **Step 4: Add the suite to the README**

In `README.md`'s "Tests" code block, after the `./test-shim.sh` line, add:

```bash
./test-worker.sh                 # the hosted worker's entrypoint and wrapper
```

After the `test-shim.sh` paragraph, add:

```markdown
`test-worker.sh` covers the hosted worker's two shell steps: the root entrypoint that writes the job spec to a private file and drops to the reviewer with `setpriv`, and the wrapper that hands the worker exactly `HOME` and `PATH`. It stubs `id`, `chown` and `setpriv`, so it needs no Docker and no root, and it checks the flags `setpriv` is given rather than whether a kernel honours them. The worker proves that itself when it starts, and refuses to run if it is wrong.
```

- [ ] **Step 5: Add the history entry**

At the top of "Unreleased" in `HISTORY.md`, add:

```markdown
* **Phase 2a.3 of the hosted GitHub App: the worker and the job runner.** A hosted worker reviews one PR at one head with each of its personas, as an unprivileged reviewer that drops every capability itself, so the posture holds on runtimes with no `--cap-drop` switch. It never holds the provider credential: each persona gets a per-pair token the gateway checks. Its `gh pr comment` hands findings to the control plane instead of posting them, and sessions resume across containers from transcripts the control plane keeps. The local Docker runner starts and stops workers from a separate process that is the only holder of the Docker socket. Local mode is unchanged.
```

- [ ] **Step 6: Run every suite**

Run: `./test-python.sh && ./test-providers.sh && ./test-personas.sh && ./test-shim.sh && /bin/bash ./test-launcher.sh && /bin/bash ./test-worker.sh`
Expected: all six pass.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile CLAUDE.md README.md HISTORY.md
git commit -m "docs: hosted phase 2a.3, the worker and the job runner"
```
