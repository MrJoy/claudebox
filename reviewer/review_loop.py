#!/usr/bin/env python3
"""The review supervisor.

One Claude session per (PR, mode, persona) triple. Each cycle: fetch refs,
enumerate candidate PRs with their review mode, review each pair in its own
session (new, or --resume of that triple's session so a persona will not
re-raise findings it already raised), then sleep.

Claude Code's /loop cannot be used here because it needs a live interactive
session, which headless -p is not. This loop plus --resume gives the same
continuous, context-retaining behavior while staying headless and crash-safe.

Reached by `exec` from entrypoint.sh, which owns everything upstream of this:
hardening checks, gh/git auth, the provider environment, the working clone, and
the LiteLLM translator when one is running.
"""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import gh
import passes
import personas as personas_mod
import prompts as prompts_mod
from common import ConfigError, Pair, die, log

# Not operator-configurable: this is a guard against a dead provider, not a
# tuning knob. Connection refused, a dead LiteLLM translator, a gateway 502:
# none classify as a limit, and each failure drops its pair's session, so
# walking a whole list into a dead endpoint costs a duplicate-comment burst per
# pair.
MAX_CONSECUTIVE_FAILURES = 3


@dataclass(frozen=True)
class Group:
    """One PR's personas, run together behind a barrier.

    The group is the unit of both concurrency and cut-short accounting: a limit
    reported by one persona cannot recall its in-flight siblings, so the group
    finishes and only then does the cycle stop.
    """

    pr: int
    mode: str
    pairs: Tuple[Pair, ...]


def parse_max_concurrent(env: Mapping[str, str]) -> int:
    """How many passes may run at once inside a group. 0 means unlimited.

    Unlimited is the default: the group's persona count. An operator who hits
    rate-limit or memory pressure dials it down without a rebuild.

    A cap of 1 is a one-worker pool rather than a separate sequential path, so
    there is no second code path to drift from the concurrent one.
    """
    raw = env.get("MAX_CONCURRENT_PASSES", "").strip()
    if not raw:
        return 0
    # int() in a try rather than a str.isdigit() gate, for the reason
    # parse_max_cycles spells out: isdigit() is True for characters int()
    # refuses, so the gate turns a typo into a traceback.
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("MAX_CONCURRENT_PASSES must be a non-negative integer")
    if value < 0:
        raise ConfigError("MAX_CONCURRENT_PASSES must be a non-negative integer")
    return value


def parse_max_cycles(env: Mapping[str, str]) -> Optional[int]:
    """How many cycles to run before exiting. Unset or 0 means forever.

    Exists so the acceptance suites can stop the loop deterministically instead
    of relying on a stubbed `sleep` exiting non-zero. The launcher passes it to
    nothing, so making `claudebox.sh test` a one-shot means setting it in the
    env file.
    """
    raw = env.get("MAX_CYCLES", "").strip()
    if not raw:
        return None
    # int(), not str.isdigit(): isdigit() is True for characters int() refuses
    # ('\u00b2' among them), so gating on it turned a typo into an uncaught
    # ValueError traceback instead of the startup error the docs promise.
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError("MAX_CYCLES must be a non-negative integer")
    if value < 0:
        raise ConfigError("MAX_CYCLES must be a non-negative integer")
    return value or None


def prompt_token_warnings(review: Mapping[str, str], followup: Mapping[str, str]) -> List[str]:
    """One warning per prompt that will not name the PR it is reviewing.

    Reachable only through an operator override, and worth a line because the
    resulting reviews look like a model ignoring instructions rather than like
    a configuration mistake.
    """
    out = []
    for label, table in (("review", review), ("followup", followup)):
        for mode in sorted(table):
            if "{{PR}}" not in table[mode]:
                out.append(
                    f"WARN: the {mode} {label} prompt has no {{{{PR}}}} token; "
                    "reviews won't name the specific PR."
                )
    return out


def _required(env: Mapping[str, str], name: str) -> str:
    """A var entrypoint.sh must export before it execs us.

    A bare KeyError here would surface as a traceback, and under
    `--restart unless-stopped` that is a silent crash loop rather than a
    startup error somebody can read.
    """
    value = env.get(name, "")
    if not value:
        raise ConfigError(f"{name} is not set; entrypoint.sh must export it")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    # int() in a try rather than a str.isdigit() gate, for the reason
    # parse_max_cycles spells out: isdigit() is True for characters int()
    # refuses, so the gate turns a typo into a traceback.
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a non-negative integer")
    if value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return value


def check_litellm(env: Mapping[str, str]) -> None:
    """Are the workersai translators still up?

    Called each cycle so a dead one is a loud, fatal error rather than every
    review pass failing on connection refused. No-op for every other provider.

    We are PID 1 after the exec, so a dead child becomes a zombie that os.kill
    would still find. Reap first, non-blockingly, or the check never fires.

    Reaping only these two pids, never waitpid(-1): a wildcard reap would also
    collect a review pass's own claude, stealing the exit status its Popen is
    waiting for. Phase A runs one pass at a time so the window is narrow; Phase B
    runs them concurrently and the window is always open.
    """
    for var in ("SHIM_PID", "LITELLM_PID"):
        raw = env.get(var, "")
        if not raw:
            continue
        try:
            os.waitpid(int(raw), os.WNOHANG)
        except (OSError, ValueError):
            pass

    for var, label, short, logfile, lines in (
        ("SHIM_PID", "Workers AI normalizer", "normalizer", "shim.log", 20),
        ("LITELLM_PID", "LiteLLM translator", "translator", "litellm.log", 40),
    ):
        raw = env.get(var, "")
        if not raw:
            continue
        try:
            os.kill(int(raw), 0)
        except (OSError, ValueError):
            # ValueError is junk in the var. The shell's `kill -0 "$SHIM_PID"`
            # rejects that too and dies, so a garbled pid is a dead translator
            # here as well rather than a silently skipped check.
            path = os.path.join(env.get("HOME", ""), logfile)
            log(f"--- last {lines} lines of the {short} log ---")
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh.read().splitlines()[-lines:]:
                        log(line)
            except OSError:
                pass
            die(f"the {label} died (log above). Restarting the container will bring it back.")


class Supervisor:
    """Owns the per-(PR, mode, persona) state and the shape of a cycle.

    All of this is in memory, so a container restart re-reviews each PR once per
    persona and may re-comment once. Persisting it is deferred.
    """

    def __init__(
        self,
        personas: Dict[str, List[str]],
        persona_prompts: Dict[Tuple[str, str], str],
        review_prompts: Dict[str, str],
        followup_prompts: Dict[str, str],
        model: str,
        mcp_args: List[str],
        cwd: str,
        max_passes_per_session: int,
        max_concurrent: int = 0,
    ):
        self.personas = personas
        self.persona_prompts = persona_prompts
        self.review_prompts = review_prompts
        self.followup_prompts = followup_prompts
        self.model = model
        self.mcp_args = mcp_args
        self.cwd = cwd
        self.max_passes_per_session = max_passes_per_session
        self.max_concurrent = max_concurrent

        self.sessions: Dict[Pair, str] = {}
        self.passes_done: Dict[Pair, int] = {}
        # The pair the next cycle starts at. None means "start at the first".
        # Without it, a limit that allows only a few passes per backoff window
        # would review the leading pairs forever and the trailing ones never.
        self.resume_at: Optional[Pair] = None

    def build_pairs(self, candidates: Sequence[Tuple[int, str]]) -> List[Pair]:
        out: List[Pair] = []
        for pr, mode in candidates:
            for persona in self.personas[mode]:
                out.append(Pair(pr, mode, persona))
        return out

    def build_groups(self, candidates: Sequence[Tuple[int, str]]) -> List[Group]:
        return [
            Group(
                pr=pr,
                mode=mode,
                pairs=tuple(Pair(pr, mode, p) for p in self.personas[mode]),
            )
            for pr, mode in candidates
        ]

    def workers_for(self, group: Group, to_run: Sequence[Pair]) -> int:
        """Effective concurrency for this group: the cap, or the group's size."""
        if self.max_concurrent > 0:
            return max(1, min(self.max_concurrent, len(to_run)))
        return max(1, len(to_run))

    def run_group(self, group: Group, to_run: Sequence[Pair]) -> Dict[Pair, "passes.PassResult"]:
        """Run to_run concurrently and wait for all of them.

        Nothing is killed. A limit reported by one persona leaves its siblings
        running, because a killed pass may have posted some findings and not
        others, and its session-id recovery is unreliable.
        """
        results: Dict[Pair, passes.PassResult] = {}
        if not to_run:
            return results

        with ThreadPoolExecutor(max_workers=self.workers_for(group, to_run)) as pool:
            futures = {pool.submit(self._dispatch, pair): pair for pair in to_run}
            for future, pair in futures.items():
                results[pair] = future.result()
        return results

    def _dispatch(self, pair: Pair) -> "passes.PassResult":
        """One pass, chosen prompt and all, on a worker thread.

        Every log line from here goes through common.log's lock, so the
        interleaving is between lines and never inside one. Ordering across
        personas is not deterministic; ordering within a persona is.
        """
        session_id = self.sessions.get(pair)
        if session_id:
            log(f"Reviewing PR #{pair.pr} [{pair.mode}/{pair.persona}] "
                f"(resuming session {session_id})...")
            template = self.followup_prompts[pair.mode]
        else:
            log(f"Reviewing PR #{pair.pr} [{pair.mode}/{pair.persona}] (new session)...")
            template = self.review_prompts[pair.mode]

        try:
            return self._run_one(pair, prompts_mod.render(template, pair.pr), session_id)
        except Exception as exc:  # noqa: BLE001
            # A raise here is this supervisor's bug, not the provider's, so it
            # must not be classified as a limit and must not take the group with
            # it. Reported as an ordinary non-limit failure.
            log(f"WARN: pass raised {exc!r}", pair=pair)
            return passes.PassResult(rc=1, session_id=session_id, limited=False, limit_line="")

    def start_index(self, pairs: Sequence[Pair]) -> int:
        """Where this cycle begins.

        A resume point that no longer exists (its PR closed, the persona set
        changed) falls back to the head of the list rather than skipping a cycle.
        """
        if self.resume_at is None:
            return 0
        try:
            index = pairs.index(self.resume_at)
        except ValueError:
            return 0
        if index:
            log(f"Starting this cycle at {pairs[index]}, where the last one was cut.")
        return index

    def _run_one(self, pair: Pair, prompt: str, session_id: Optional[str]):
        try:
            return passes.run_pass(
                pair=pair,
                prompt=prompt,
                session_id=session_id,
                persona_prompt=self.persona_prompts[(pair.mode, pair.persona)],
                model=self.model,
                mcp_args=self.mcp_args,
                cwd=self.cwd,
            )
        except OSError as exc:
            # A pass that never gets off the ground is an ordinary failed pass.
            # EAGAIN against --pids-limit, ENOMEM against --memory, a claude
            # that is not on PATH: the shell read PIPESTATUS[0] and saw a
            # non-zero rc for all three. Letting the exception out instead exits
            # PID 1, and since the session map lives only in memory the restart
            # makes every pair re-post findings it already posted.
            log(f"WARN: could not run claude: {exc}", pair=pair)
            return passes.PassResult(rc=1, session_id=session_id, limited=False, limit_line="")

    def run_cycle(self, pairs: Sequence[Pair]) -> bool:
        """Walk the pair list. Returns True when a usage limit cut it short."""
        count = len(pairs)
        if not count:
            # An empty list keeps the resume point rather than clearing it:
            # enumeration failures degrade to an empty candidate list, and that
            # must not silently send the next cycle back to the head.
            return False

        start = self.start_index(pairs)
        consecutive_failures = 0
        cut_at: Optional[int] = None
        cut_offset = -1
        was_limited = False

        for offset in range(count):
            index = (start + offset) % count
            pair = pairs[index]
            session_id = self.sessions.get(pair)

            if session_id:
                log(f"Reviewing PR #{pair.pr} [{pair.mode}/{pair.persona}] "
                    f"(resuming session {session_id})...")
                template = self.followup_prompts[pair.mode]
            else:
                log(f"Reviewing PR #{pair.pr} [{pair.mode}/{pair.persona}] (new session)...")
                template = self.review_prompts[pair.mode]

            result = self._run_one(pair, prompts_mod.render(template, pair.pr), session_id)

            if result.rc == 0:
                consecutive_failures = 0
                if result.session_id:
                    self.sessions[pair] = result.session_id
                self.passes_done[pair] = self.passes_done.get(pair, 0) + 1
                log(f"PR #{pair.pr} [{pair.mode}/{pair.persona}] review complete "
                    f"(session {self.sessions.get(pair)}, pass {self.passes_done[pair]}).")
                if (
                    self.max_passes_per_session > 0
                    and self.passes_done[pair] >= self.max_passes_per_session
                ):
                    log(f"PR #{pair.pr} [{pair.mode}/{pair.persona}] reached "
                        f"MAX_PASSES_PER_SESSION={self.max_passes_per_session}; "
                        "rotating its session next cycle.")
                    self.sessions.pop(pair, None)
                    self.passes_done[pair] = 0
                continue

            if result.limited:
                # Keep the session. Abandon the rest of the cycle rather than
                # walking the remaining pairs into the same wall.
                if result.session_id:
                    self.sessions[pair] = result.session_id
                    log(f"WARN: PR #{pair.pr} [{pair.mode}/{pair.persona}] hit a usage or "
                        "rate limit; keeping its session and ending this cycle early.")
                else:
                    log(f"WARN: PR #{pair.pr} [{pair.mode}/{pair.persona}] hit a usage or "
                        "rate limit before it had a session; ending this cycle early.")
                if result.limit_line:
                    log(f"  limit reported by claude: {result.limit_line}")
                was_limited = True
                cut_at, cut_offset = index, offset
                break

            log(f"WARN: PR #{pair.pr} [{pair.mode}/{pair.persona}] review failed; "
                "starting a fresh session for it next cycle.")
            self.sessions.pop(pair, None)
            self.passes_done[pair] = 0
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Not a limit, so no backoff: the next cycle comes at the
                # ordinary interval, and starts where this one stopped.
                log(f"WARN: {consecutive_failures} passes in a row failed for reasons "
                    "other than a limit; the provider looks unhealthy. Abandoning this cycle.")
                cut_at, cut_offset = index, offset
                break

        if cut_at is not None:
            self.resume_at = pairs[(cut_at + 1) % count]
            skipped = [pairs[(start + o) % count] for o in range(cut_offset + 1, count)]
            if skipped:
                log("Not reviewed this cycle: " + ", ".join(str(p) for p in skipped)
                    + f". The next cycle starts at {self.resume_at}.")
            else:
                log(f"The next cycle starts at {self.resume_at}.")
        else:
            self.resume_at = None

        return was_limited


def preflight(env: Mapping[str, str]) -> Tuple[str, Dict[str, List[personas_mod.Persona]]]:
    """The PR selector and both modes' persona sets.

    Personas are resolved for EVERY mode, even one no PR currently uses, so a
    broken definition fails at boot rather than the first time somebody adds a
    label to a PR.

    entrypoint.sh runs this on its own through --check, ahead of gh/git auth,
    the working clone and the translator's blocking startup. A typo'd PERSONAS
    would otherwise cost a network clone and 120s of LiteLLM on every restart
    under `--restart unless-stopped` before the operator got to read the error.
    Which means it may read only what the environment already holds at that
    point: nothing entrypoint.sh exports on its way to the exec.
    """
    selector = gh.resolve_pr_selection(env)
    persona_dir = env.get("PERSONA_DIR") or "/opt/claudebox/personas"
    resolved = {
        mode: personas_mod.resolve(mode, persona_dir, env)
        for mode in personas_mod.REVIEW_MODES
    }
    return selector, resolved


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Line buffering keeps `docker logs -f` live. Guarded because a redirected
    # stdout (a test capturing the log) is not a real stream and has no
    # reconfigure.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    unknown = [a for a in args if a != "--check"]
    if unknown:
        die(f"unknown argument: {unknown[0]} (the only flag is --check)")
    env = os.environ

    try:
        selector, resolved = preflight(env)
        if check_only:
            return 0
        for mode in personas_mod.REVIEW_MODES:
            log(f"{mode} personas: " + " ".join(p.id for p in resolved[mode]))

        built = prompts_mod.build(env)
        for warning in prompt_token_warnings(built.review, built.followup):
            log(warning)
        max_cycles = parse_max_cycles(env)
        interval = _positive_int(env, "REVIEW_INTERVAL_SECONDS", 300)
        backoff = _positive_int(env, "LIMIT_BACKOFF_SECONDS", 1800)
        max_passes = _positive_int(env, "MAX_PASSES_PER_SESSION", 0)
        max_concurrent = parse_max_concurrent(env)
        review_model = _required(env, "REVIEW_MODEL")
        work_repo = _required(env, "WORK_REPO")
    except ConfigError as exc:
        die(str(exc))

    mcp_args = ["--strict-mcp-config"]
    mcp_config = env.get("MCP_CONFIG_FILE", "")
    if mcp_config and os.path.isfile(mcp_config):
        mcp_args += ["--mcp-config", mcp_config]

    supervisor = Supervisor(
        personas={m: [p.id for p in ps] for m, ps in resolved.items()},
        persona_prompts={(m, p.id): p.prompt for m, ps in resolved.items() for p in ps},
        review_prompts=built.review,
        followup_prompts=built.followup,
        model=review_model,
        mcp_args=mcp_args,
        cwd=work_repo,
        max_passes_per_session=max_passes,
        max_concurrent=max_concurrent,
    )

    cycles = 0
    while True:
        check_litellm(env)

        log("Fetching latest refs...")
        try:
            fetched = subprocess.run(
                ["git", "fetch", "--all", "--prune", "--quiet"],
                cwd=supervisor.cwd, capture_output=True, text=True, check=False,
            )
            if fetched.returncode != 0:
                log("WARN: git fetch failed; continuing")
        except OSError as exc:
            # The shell wrote `git fetch ... || log WARN`, which covers a git
            # that cannot be spawned as well as one that exits non-zero. A stale
            # working clone still reviews; a dead supervisor reviews nothing.
            log(f"WARN: git fetch could not run ({exc}); continuing")

        try:
            candidates = gh.enumerate_candidate_prs(selector, env)
        except ConfigError as exc:
            die(str(exc))

        if not candidates:
            log(f"No candidate PRs for selector '{selector}'.")
        else:
            log(f"Candidate PRs ({selector}): "
                + " ".join(f"{pr}:{mode}" for pr, mode in candidates))

        limited = supervisor.run_cycle(supervisor.build_pairs(candidates))

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            log(f"Reached MAX_CYCLES={max_cycles}; exiting.")
            return 0

        if limited:
            log(f"Backing off {backoff}s after a usage limit...")
            time.sleep(backoff)
        else:
            log(f"Sleeping {interval}s...")
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
