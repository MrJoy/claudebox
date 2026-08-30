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


def parse_max_cycles(env: Mapping[str, str]) -> Optional[int]:
    """How many cycles to run before exiting. Unset or 0 means forever.

    Exists so the acceptance suites can stop the loop deterministically instead
    of relying on a stubbed `sleep` exiting non-zero, and so `claudebox.sh test`
    can be a genuine one-shot.
    """
    raw = env.get("MAX_CYCLES", "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ConfigError("MAX_CYCLES must be a non-negative integer")
    return int(raw) or None


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


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise ConfigError(f"{name} must be a non-negative integer")
    return int(raw)


def check_litellm(env: Mapping[str, str]) -> None:
    """Are the workersai translators still up?

    Called each cycle so a dead one is a loud, fatal error rather than every
    review pass failing on connection refused. No-op for every other provider.

    We are PID 1 after the exec, so a dead child becomes a zombie that os.kill
    would still find. Reap first, non-blockingly, or the check never fires.
    """
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break

    for var, label, logfile, lines in (
        ("SHIM_PID", "Workers AI normalizer", "shim.log", 20),
        ("LITELLM_PID", "LiteLLM translator", "litellm.log", 40),
    ):
        raw = env.get(var, "").strip()
        if not raw.isdigit():
            continue
        try:
            os.kill(int(raw), 0)
        except OSError:
            path = os.path.join(env.get("HOME", ""), logfile)
            log(f"--- last {lines} lines of the {label} log ---")
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
    ):
        self.personas = personas
        self.persona_prompts = persona_prompts
        self.review_prompts = review_prompts
        self.followup_prompts = followup_prompts
        self.model = model
        self.mcp_args = mcp_args
        self.cwd = cwd
        self.max_passes_per_session = max_passes_per_session

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
        return passes.run_pass(
            pair=pair,
            prompt=prompt,
            session_id=session_id,
            persona_prompt=self.persona_prompts[(pair.mode, pair.persona)],
            model=self.model,
            mcp_args=self.mcp_args,
            cwd=self.cwd,
        )

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
                log("Not reviewed this cycle: " + " ".join(str(p) for p in skipped)
                    + f". The next cycle starts at {self.resume_at}.")
            else:
                log(f"The next cycle starts at {self.resume_at}.")
        else:
            self.resume_at = None

        return was_limited


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    env = os.environ

    try:
        selector = gh.resolve_pr_selection(env)
        persona_dir = env.get("PERSONA_DIR") or "/opt/claudebox/personas"
        # Resolved for EVERY mode at startup, even one no PR currently uses, so
        # a broken definition fails at boot rather than the first time somebody
        # adds a label to a PR.
        resolved = {}
        for mode in personas_mod.REVIEW_MODES:
            resolved[mode] = personas_mod.resolve(mode, persona_dir, env)
            log(f"{mode} personas: " + " ".join(p.id for p in resolved[mode]))

        built = prompts_mod.build(env)
        for warning in prompt_token_warnings(built.review, built.followup):
            log(warning)
        max_cycles = parse_max_cycles(env)
        interval = _positive_int(env, "REVIEW_INTERVAL_SECONDS", 300)
        backoff = _positive_int(env, "LIMIT_BACKOFF_SECONDS", 1800)
        max_passes = _positive_int(env, "MAX_PASSES_PER_SESSION", 0)
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
        model=env["REVIEW_MODEL"],
        mcp_args=mcp_args,
        cwd=env["WORK_REPO"],
        max_passes_per_session=max_passes,
    )

    cycles = 0
    while True:
        check_litellm(env)

        log("Fetching latest refs...")
        fetched = subprocess.run(
            ["git", "fetch", "--all", "--prune", "--quiet"],
            cwd=supervisor.cwd, capture_output=True, text=True, check=False,
        )
        if fetched.returncode != 0:
            log("WARN: git fetch failed; continuing")

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
