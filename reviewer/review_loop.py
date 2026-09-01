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
import stat
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

import gh
import passes
import personas as personas_mod
import prompts as prompts_mod
import signals as signals_mod
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


def shared_worktree_modes(personas: Dict[str, List[str]], max_concurrent: int) -> FrozenSet[str]:
    """Modes whose personas actually run together this run.

    Per mode rather than globally, because effective concurrency is
    min(cap, that mode's persona count): a mode dialed down to one persona has
    nothing running beside it and gets byte-identical prompts to the sequential
    loop. Persona sets are fixed for the life of the container, so a resumed
    session cannot gain or lose the stanza between passes.

    This has to agree with Supervisor.workers_for, which is what makes the
    claim true.
    """
    return frozenset(
        mode
        for mode, ids in personas.items()
        if (len(ids) if max_concurrent <= 0 else min(max_concurrent, len(ids))) > 1
    )


# What lock_git_dir cleared, so unlock_git_dir can put it back exactly. Keyed by
# path; entries are popped on unlock, so a lock/unlock/lock round trip re-reads
# the restored mode. Only ever touched from the main thread -- the startup lock
# and the cycle's fetch both run before any worker exists.
_ORIGINAL_MODES: Dict[str, int] = {}


def _git_dir(work_repo: str) -> str:
    return os.path.join(work_repo, ".git")


def _locked_dirs(work_repo: str) -> List[str]:
    """The .git inode, plus .git/refs and every directory under it.

    .git/refs and NOT .git/objects, because that is where the cost lives: refs
    is O(ref count) -- a handful of directories on any repo -- where objects is
    the O(object count) walk the file-count guard in commit fdd0ac1 exists to
    avoid. Every ref write takes its lock beside the ref it is writing, so the
    refs subtree is what closes them and the object store buys nothing.
    """
    path = _git_dir(work_repo)
    if not os.path.isdir(path):
        return []
    out = [path]
    for root, _dirs, _files in os.walk(os.path.join(path, "refs")):
        out.append(root)
    return out


def lock_git_dir(work_repo: str) -> None:
    """Drop write permission on .git and on the .git/refs subtree.

    Creating a file in a directory needs write permission on that directory, so
    this stops every git operation that has to make a lock file in one of them.
    In .git: index.lock covers add, commit, checkout, stash, reset and the merge
    half of a pull; config.lock covers config and remote; gc.pid.lock covers gc;
    packed-refs.lock covers pack-refs. Under .git/refs: branch, tag, update-ref,
    notes add, checkout -b and worktree add all fail on <ref>.lock, and a fetch
    fails on the remote-tracking ref it would move -- including under
    --no-write-fetch-head, which skips the .git half of the lock and still
    cannot move a ref. Reading creates nothing, so log, show, diff, status,
    blame, cat-file and for-each-ref keep working.

    The refs subtree is here because the ref namespace is reachable from a
    review and reads out of one. `git update-ref refs/remotes/origin/main HEAD`
    empties `git log origin/main..HEAD`, so a sibling sees a PR containing
    nothing; `git notes add` puts attacker-chosen text into `git log` and
    `git show`, where it renders as commit metadata. Both exit 0 against a lock
    that covers .git alone.

    What stays open: object writes into .git/objects/**, so a fetch can still
    download, and writes to files that already sit directly in .git, since
    dropping a directory's write bit stops entries being CREATED in it rather
    than writes to what is already inside. Neither moves a ref or touches the
    index, and file permissions cannot narrow them further without the walk this
    design exists to avoid. prompts.WORKTREE_STANZA is the other half of the
    defense rather than a redundant restatement of this one.

    The working tree itself is left writable, so a persona that drops a scratch
    file does not hit a confusing error. A scratch file cannot corrupt another
    persona's review, because reviews read the diff through `gh pr diff`.

    Raises ConfigError when there is no .git to lock, which main turns into the
    same ERROR-on-stderr and exit 1 every other startup failure produces.

    Measured against git 2.54 on 2026-08-30, as an unprivileged user.
    """
    paths = _locked_dirs(work_repo)
    if not paths:
        # Refused rather than warned past. Reaching here means concurrency is
        # on, since that is the only thing that calls this, and prompts.build
        # has already put WORKTREE_STANZA in front of every persona telling it
        # the working copy is shared and protected. Six personas against an
        # unenforced tree after saying that is the failure the stanza exists to
        # prevent. Unreachable through entrypoint.sh, which makes the clone
        # before it execs us, so a container that gets here is broken in a way
        # that should be loud.
        raise ConfigError(
            f"no .git at {_git_dir(work_repo)}; refusing to run personas "
            "concurrently against an unenforced working copy"
        )
    for path in paths:
        mode = os.stat(path).st_mode
        _ORIGINAL_MODES.setdefault(path, mode)
        os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def unlock_git_dir(work_repo: str) -> None:
    """Put back exactly what the lock cleared.

    The recorded mode rather than a bare u+w: the lock drops group and other as
    well, so adding only the owner bit back would quietly walk a 775 .git down
    to 755 within a single run and leave it there.

    A path with nothing recorded falls back to adding the owner bit to what it
    finds. That is the case main leans on at startup to repair a clone a
    previous container left locked, since the modes that lock recorded died
    with that process. Note what that fallback is: it ADDS the owner write bit
    to whatever it finds, so a .git this process never locked comes back
    writable whether or not somebody meant it to be read-only. That is the
    point -- an inherited lock is exactly such a directory, and repairing it is
    the whole reason main calls this at startup -- but it is a loosening, not a
    restoration, and it cannot put back what it never saw: a 775 .git comes
    back 755 after a restart and stays there. Harmless in the container, which is single-user
    under umask 022, and 755 is what that umask would have produced anyway.
    """
    for path in _locked_dirs(work_repo):
        recorded = _ORIGINAL_MODES.pop(path, None)
        if recorded is None:
            recorded = os.stat(path).st_mode | stat.S_IWUSR
        os.chmod(path, recorded)


@contextmanager
def unlocked_git_dir(work_repo: str, enabled: bool):
    """Writable for the duration, locked again on the way out, raise or not.

    The finally is the point: `git fetch` is already allowed to fail, and an
    exception on the way out would otherwise leave the clone writable for every
    pass of every remaining cycle with nothing in the log to say so.

    The relock walks the refs subtree again, so a directory the fetch created
    under refs/remotes is locked along with the rest.

    Groups are strictly serialized after the fetch, so no pass ever observes
    the window this opens.
    """
    if not enabled:
        yield
        return
    unlock_git_dir(work_repo)
    try:
        yield
    finally:
        lock_git_dir(work_repo)


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


def partition_settling(snapshots, settle_seconds: int, now: float):
    """Split candidates into (ready, settling).

    A PR whose newest change is younger than the setting is left for the next
    poll, so a burst of pushes costs one review instead of one per push.
    Nothing about it is recorded, which is what makes the next poll reconsider
    it from scratch.
    """
    ready, settling = [], []
    for snap in snapshots:
        if signals_mod.is_settling(snap.updated_at, settle_seconds, now):
            settling.append(snap)
        else:
            ready.append(snap)
    return ready, settling


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
        # Pairs the last cycle owed but did not run: the personas a limit cut,
        # plus everything in the groups it never reached. Normally empty.
        # Without it, a limit that allows only a few passes per backoff window
        # would review the leading pairs forever and the trailing ones never.
        # In memory alongside the session map; surviving a restart is deferred.
        self.owed: Set[Pair] = set()
        # The group a cut stopped in, so the next cycle can start at the one
        # after it. Serving the debt first instead would let a PR whose persona
        # reports a limit on every attempt re-cut the cycle at the head of the
        # list forever, and nothing else would ever be reviewed again.
        self.cut_group: Optional[Tuple[int, str]] = None
        # The fingerprint each pair last successfully reviewed at. A pair whose
        # PR still fingerprints the same has nothing new to read, so it does not
        # run. In memory alongside the session map, and deliberately not
        # persisted: sessions are in memory too, so a fingerprint that outlived
        # a restart would leave a fresh session that has never read the PR
        # believing it had already reviewed it.
        self.reviewed: Dict[Pair, signals_mod.Signal] = {}

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
        """Effective concurrency for this group: the cap, or how many pairs run."""
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
            futures: Dict[Future, Pair] = {}
            for pair in to_run:
                try:
                    futures[pool.submit(self._dispatch, pair)] = pair
                except RuntimeError as exc:
                    # A container out of threads raises this on the submitting
                    # thread, so neither _run_one's OSError guard nor
                    # _dispatch's catch-all is anywhere near it. Letting it out
                    # would discard the results the pool already holds, and
                    # those passes have posted their comments. Stop submitting
                    # instead: the caller sees a pair with no result and owes it
                    # to the next cycle. The rest of to_run goes unsubmitted
                    # because the next submit would hit the same wall.
                    log(f"WARN: could not start a worker for {pair} ({exc}); "
                        "leaving it and the rest of its group for the next cycle.")
                    break
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

    def pairs_to_run(
        self, group: Group, signal: Optional["signals_mod.Signal"] = None
    ) -> List[Pair]:
        """Which of this group's personas run this cycle.

        A group that owes something runs ONLY what it owes: those personas did
        not run last cycle, and the rest of the group did. A group that owes
        nothing runs in full. The narrowing lasts until the group is served
        without being cut again, which is one cycle in the ordinary case and
        longer while a pair keeps reporting a limit -- the siblings a narrowed
        group leaves out are owed by the cut that stopped it, so they come back
        on the visit after.

        A group that owes nothing is then filtered by the change gate. `signal`
        is None when the gate is off, and when stage two failed against an
        empty updatedAt (nothing to key a degraded fingerprint on); both mean
        "run it". A failed lookup against a non-empty updatedAt arrives here as
        a degraded Signal instead, so it's gated like a real one and stops
        re-running once it's been served -- see signals.degraded. A pair with
        no session always runs, which
        is what makes first sight, a session dropped by _record_failure, and
        MAX_PASSES_PER_SESSION rotation work without knowing about the gate. A
        mode flip needs no case of its own either: it makes a different Pair,
        and that Pair has no session.
        """
        owed_here = [p for p in group.pairs if p in self.owed]
        if owed_here:
            return owed_here
        if signal is None:
            return list(group.pairs)
        return [
            p for p in group.pairs
            if self.sessions.get(p) is None or self.reviewed.get(p) != signal
        ]

    def unreached_debt(
        self, group: Group, signal: Optional["signals_mod.Signal"] = None
    ) -> List[Pair]:
        """What a group the cycle never reached owes the next one.

        Not `pairs_to_run`: that narrows a group to what it already owes, which
        is right for a group about to run and wrong here, since a group the
        cycle never reached ran none of itself and the narrowing was justified
        by a cut two cycles back. So the whole persona set is owed, minus the
        pairs the change gate would have withheld had the cycle got that far --
        otherwise a limit owes the entire tail of an unchanged PR list and the
        next cycle spends the budget that just ran out re-reviewing it. A pair
        already owed keeps its debt whatever the gate says: it has no result to
        preserve.
        """
        return [
            p for p in group.pairs
            if p in self.owed
            or signal is None
            or self.sessions.get(p) is None
            or self.reviewed.get(p) != signal
        ]

    def order_groups(self, groups: List[Group]) -> List[Group]:
        """This cycle's groups, rotated to start after the last cut.

        Phase A resumed at the pair after the one a limit cut and wrapped
        around; this is that rotation, at group granularity. Serving the debt
        first instead reads as the obvious thing and is a trap: a pair that
        reports a limit on every attempt would re-cut the cycle at the head of
        the list every time, and no other PR would ever be reviewed again. The
        cut group goes last, keeps its debt, and is served when the rotation
        reaches it.
        """
        if self.cut_group is None:
            return list(groups)
        keys = [(g.pr, g.mode) for g in groups]
        try:
            start = keys.index(self.cut_group) + 1
        except ValueError:
            # The group the cut stopped in is gone -- closed, or relabelled into
            # the other mode. Start at the head rather than skipping a cycle.
            return list(groups)
        return list(groups[start:]) + list(groups[:start])

    def run_cycle(
        self,
        groups: List[Group],
        signals_by_pr: Optional[Dict[int, "signals_mod.Signal"]] = None,
    ) -> bool:
        """Walk the groups. Returns True when a usage limit cut the cycle.

        `signals_by_pr` is this cycle's fingerprints, keyed by PR number. A PR
        missing from it -- and an empty map, which is what the gate switched off
        looks like -- is reviewed unconditionally.
        """
        signals_by_pr = signals_by_pr or {}
        if not groups:
            # An empty list keeps what is owed rather than clearing it:
            # enumeration failures degrade to an empty candidate list, and that
            # must not silently cancel the debt.
            return False

        ordered = self.order_groups(groups)
        owed_now = [p for g in ordered for p in self.pairs_to_run(g) if p in self.owed]
        if owed_now:
            log("Resuming with " + " ".join(str(p) for p in owed_now) + ".")

        consecutive_failures = 0
        cut_index: Optional[int] = None
        cut_owes: Set[Pair] = set()
        was_limited = False

        for index, group in enumerate(ordered):
            signal = signals_by_pr.get(group.pr)
            to_run = self.pairs_to_run(group, signal)
            if not to_run:
                # Nothing to review here. This is a cost and noise guard, not a
                # correctness one: run_group already short-circuits an empty
                # pair list, and the cut accounting below is rebuilt per group.
                # Skipping keeps a per-cycle log line out of the log for a PR
                # nobody touched and does not build a pool for no work.
                continue
            if signal is not None:
                prior = next(
                    (self.reviewed[p] for p in group.pairs if p in self.reviewed), None
                )
                reason = signals_mod.change_reason(prior, signal) or "no session"
                log(f"PR #{group.pr} [{group.mode}]: {reason}.")
            results = self.run_group(group, to_run)

            any_success = False
            group_failures = 0
            limited_here = set()
            # A pair the pool could not accept. It did not run, so it is neither
            # a success nor a failure, and its session stands.
            unstarted = [p for p in to_run if p not in results]

            for pair in to_run:
                result = results.get(pair)
                if result is None:
                    continue
                if result.rc == 0:
                    any_success = True
                    self._record_success(pair, result, signal)
                elif result.limited:
                    limited_here.add(pair)
                    self._record_limit(pair, result)
                else:
                    group_failures += 1
                    self._record_failure(pair)

            # Evaluated at the barrier rather than per pass. A success anywhere
            # in the group resets it: the provider is alive.
            if any_success:
                consecutive_failures = 0
            else:
                consecutive_failures += group_failures

            # Everything in the group that did not run: the pairs the pool
            # refused, and the siblings a narrowed group left out. A pair that
            # ran and failed is not here -- it had its turn, and its session was
            # dropped, so the next cycle to reach the group starts it fresh.
            did_not_run = {p for p in group.pairs if p not in results}

            if limited_here or unstarted:
                was_limited = bool(limited_here)
                if unstarted and not limited_here:
                    log("WARN: the worker pool refused a pass; abandoning this cycle.")
                cut_index, cut_owes = index, limited_here | did_not_run
                break

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log(f"WARN: {consecutive_failures} passes in a row failed for reasons "
                    "other than a limit; the provider looks unhealthy. Abandoning this cycle.")
                cut_index, cut_owes = index, did_not_run
                break

        # Both exits rebuild `owed` from this cycle's own groups, which is what
        # keeps a debt whose PR closed out of it: a dead pair matches no live
        # group while the cycle runs, and does not survive the end of it.
        if cut_index is None:
            self.owed = set()
            self.cut_group = None
            return was_limited

        # The cut group's own debt, plus every pair of every group the cycle
        # never reached. Their whole persona set is owed, narrowed or not: none
        # of them ran.
        # cut_owes is taken as it stands: those pairs were selected to run this
        # cycle and were prevented, so the gate has no say over them. The groups
        # the cycle never reached go through unreached_debt, which puts them
        # through the gate they would have faced. self.owed still holds last
        # cycle's debt at this point, which is what keeps a group that was
        # already owed and was never reached owed.
        new_owed = set(cut_owes)
        skipped: List[Pair] = []
        for group in ordered[cut_index + 1:]:
            for pair in self.unreached_debt(group, signals_by_pr.get(group.pr)):
                new_owed.add(pair)
                skipped.append(pair)
        self.owed = new_owed
        self.cut_group = (ordered[cut_index].pr, ordered[cut_index].mode)

        if skipped:
            log("Not reviewed this cycle: " + " ".join(str(p) for p in skipped) + ".")
        if new_owed:
            log("Owed next cycle: " + " ".join(str(p) for p in sorted(new_owed)) + ".")
        return was_limited

    def _record_success(
        self, pair: Pair, result, signal: Optional["signals_mod.Signal"] = None
    ) -> None:
        # Only when one was supplied: None arrives both from the gate being off
        # and from a lookup that failed, and recording it would claim knowledge
        # we do not have.
        if signal is not None:
            self.reviewed[pair] = signal
        if result.session_id:
            self.sessions[pair] = result.session_id
        self.passes_done[pair] = self.passes_done.get(pair, 0) + 1
        log(f"review complete (session {self.sessions.get(pair)}, "
            f"pass {self.passes_done[pair]}).", pair=pair)
        if (
            self.max_passes_per_session > 0
            and self.passes_done[pair] >= self.max_passes_per_session
        ):
            log(f"reached MAX_PASSES_PER_SESSION={self.max_passes_per_session}; "
                "rotating its session next cycle.", pair=pair)
            self.sessions.pop(pair, None)
            self.passes_done[pair] = 0

    def _record_limit(self, pair: Pair, result) -> None:
        # The session is kept. Dropping it would make the next attempt re-read
        # the whole PR and re-post findings already posted, spending more of the
        # resource that just ran out.
        if result.session_id:
            self.sessions[pair] = result.session_id
            log("WARN: hit a usage or rate limit; keeping its session and "
                "ending this cycle after the group finishes.", pair=pair)
        else:
            log("WARN: hit a usage or rate limit before it had a session; "
                "ending this cycle after the group finishes.", pair=pair)
        if result.limit_line:
            log(f"  limit reported by claude: {result.limit_line}", pair=pair)

    def _record_failure(self, pair: Pair) -> None:
        log("WARN: review failed; starting a fresh session for it next cycle.", pair=pair)
        self.sessions.pop(pair, None)
        self.passes_done[pair] = 0


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
    # Validated here as well as in main, because --check returns before main's
    # config block and a typo should cost a startup error rather than a clone
    # and a translator. Reads only the environment, which is what --check is
    # allowed to touch.
    _positive_int(env, "SETTLE_SECONDS", 30)
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

        max_concurrent = parse_max_concurrent(env)
        persona_ids = {m: [p.id for p in ps] for m, ps in resolved.items()}
        worktree_modes = shared_worktree_modes(persona_ids, max_concurrent)
        if worktree_modes:
            log("Shared-worktree constraint active for: " + " ".join(sorted(worktree_modes)))

        built = prompts_mod.build(env, shared_worktree_modes=worktree_modes)
        for warning in prompt_token_warnings(built.review, built.followup):
            log(warning)
        max_cycles = parse_max_cycles(env)
        interval = _positive_int(env, "REVIEW_INTERVAL_SECONDS", 60)
        settle = _positive_int(env, "SETTLE_SECONDS", 30)
        gate_on = signals_mod.enabled(env)
        backoff = _positive_int(env, "LIMIT_BACKOFF_SECONDS", 1800)
        max_passes = _positive_int(env, "MAX_PASSES_PER_SESSION", 0)
        review_model = _required(env, "REVIEW_MODEL")
        work_repo = _required(env, "WORK_REPO")

        # A clone this container has run before comes back LOCKED: the process
        # that locked it recorded the modes in memory and took them with it,
        # and entrypoint.sh restores $WORK_REPO/.git alone. Without this, the
        # lock below records the still-read-only .git/refs modes as the
        # originals and every unlock restores them faithfully, so the fetch
        # window opens with refs unwritable for the life of the container and
        # the working clone silently stops advancing.
        #
        # Unconditional, not gated on enforce_lock: a run with concurrency off
        # inherits the same locked clone and its fetch fails the same way, and
        # it has no lock of its own to repair it. A no-op on a clone nobody
        # locked, and on a WORK_REPO with no .git at all -- the refusal for
        # that case belongs to lock_git_dir, below.
        try:
            unlock_git_dir(work_repo)
        except OSError as exc:
            # chmod can fail on its own -- a clone owned by another uid, a
            # read-only mount. Without this the traceback exits PID 1 in place
            # of the ERROR: line every other startup failure produces, and
            # under --restart unless-stopped that reads as a silent crash loop.
            raise ConfigError(
                f"cannot repair the working clone's .git permissions: {exc}"
            )

        # The enforcement half of the shared-worktree constraint the stanza
        # states. Tied to the same set, so a run whose personas do not overlap
        # gets neither the instruction nor the read-only clone. Inside the try
        # because a clone it cannot lock is a startup failure like any other.
        enforce_lock = bool(worktree_modes)
        if enforce_lock:
            lock_git_dir(work_repo)
            log("The working clone's .git is read-only except during the "
                "cycle's own fetch (shared-worktree enforcement).")
    except ConfigError as exc:
        die(str(exc))

    mcp_args = ["--strict-mcp-config"]
    mcp_config = env.get("MCP_CONFIG_FILE", "")
    if mcp_config and os.path.isfile(mcp_config):
        mcp_args += ["--mcp-config", mcp_config]

    supervisor = Supervisor(
        personas=persona_ids,
        persona_prompts={(m, p.id): p.prompt for m, ps in resolved.items() for p in ps},
        review_prompts=built.review,
        followup_prompts=built.followup,
        model=review_model,
        mcp_args=mcp_args,
        cwd=work_repo,
        max_passes_per_session=max_passes,
        max_concurrent=max_concurrent,
    )
    tracker = signals_mod.Tracker()

    cycles = 0
    while True:
        check_litellm(env)

        log("Fetching latest refs...")
        try:
            # FETCH_HEAD lands directly in .git, so the lock stops our own fetch
            # as surely as it stops a persona's. Lifted only here, and only for
            # as long as the fetch runs.
            with unlocked_git_dir(supervisor.cwd, enforce_lock):
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
            snapshots = gh.enumerate_candidate_prs(selector, env)
        except ConfigError as exc:
            die(str(exc))

        if not snapshots:
            log(f"No candidate PRs for selector '{selector}'.")
        else:
            log(f"Candidate PRs ({selector}): "
                + " ".join(f"{s.number}:{s.mode}" for s in snapshots))

        signals_by_pr = {}
        if gate_on:
            snapshots, settling = partition_settling(snapshots, settle, time.time())
            if settling:
                log(f"Settling ({settle}s), left for the next poll: "
                    + " ".join(f"#{s.number}" for s in settling) + ".")
            for snap in snapshots:
                sig = tracker.signal_for(
                    snap, lambda s: gh.pr_signal(s, env))
                if sig is None:
                    log(f"WARN: could not read PR #{snap.number}'s comment "
                        "activity; reviewing it rather than skipping it.")
                    # Tracker only remembers a real lookup's outcome, so a
                    # repeated failure against the same updatedAt lands here
                    # every poll. Synthesizing the degraded fingerprint here,
                    # from the snapshot in hand, gives the same answer each
                    # time without Tracker needing to know it exists -- that's
                    # what turns "fail open" into "fail open once per change"
                    # instead of "fail open forever".
                    sig = signals_mod.degraded(snap)
                if sig is not None:
                    signals_by_pr[snap.number] = sig

        groups = supervisor.build_groups([(s.number, s.mode) for s in snapshots])
        skipped = [
            g for g in groups
            if not supervisor.pairs_to_run(g, signals_by_pr.get(g.pr))
        ]
        if skipped:
            log("Unchanged since their last review: "
                + " ".join(f"#{g.pr}" for g in skipped) + ".")

        limited = supervisor.run_cycle(groups, signals_by_pr)

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            log(f"Reached MAX_CYCLES={max_cycles}; exiting.")
            return 0

        if limited:
            log(f"Backing off {backoff}s after a usage limit...")
            time.sleep(backoff)
        else:
            log(f"Polling again in {interval}s...")
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
