"""The decision core: what to review, at which round, and what a cut owes.

This is the gate and round logic that used to live on review_loop.Supervisor,
moved out so the hosted control plane can import the decisions without the
local group runner. It reads and writes nothing but a StateStore and the
persona configuration it is built with. Running a pass, the worker pool, the
clone lock and the poll loop all stay in review_loop.py, which is local mode's
runner; a remote runner reaches the same decisions through this module.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

import prompts as prompts_mod
import signals as signals_mod
from common import Pair, log
from state import StateStore


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


class Decider:
    """The decisions a cycle makes, over one StateStore.

    `personas`, `persona_prompts` and `persona_phases` are held by reference,
    not copied: they are fixed for the container's life, and the caller that
    built them is the one place they come from.
    """

    def __init__(
        self,
        state: StateStore,
        personas: Dict[str, List[str]],
        persona_prompts: Dict[Tuple[str, str], str],
        persona_phases: Dict[Tuple[str, str], int],
        max_passes_per_session: int,
        plan_max_rounds: int,
    ):
        self.state = state
        self.personas = personas
        self.persona_prompts = persona_prompts
        # (mode, persona) -> 1 or 2. Absent means 1. Phase 2 runs after every
        # phase-1 pass in the group has finished, which is what lets Sage read
        # what its siblings posted this round.
        self.persona_phases = persona_phases
        self.max_passes_per_session = max_passes_per_session
        # PLAN_MAX_ROUNDS, the backstop behind the plan ladder. 0 is no cap.
        self.plan_max_rounds = plan_max_rounds

    def build_groups(self, candidates: Sequence[Tuple[int, str]]) -> List[Group]:
        return [
            Group(
                pr=pr,
                mode=mode,
                pairs=tuple(Pair(pr, mode, p) for p in self.personas[mode]),
            )
            for pr, mode in candidates
        ]

    def phase_of(self, pair: Pair) -> int:
        return self.persona_phases.get((pair.mode, pair.persona), 1)

    def round_for(self, pair: Pair) -> int:
        """The round the pair's next pass runs at."""
        return self.state.rounds.get(pair, 0) + 1

    def system_prompt_for(self, pair: Pair) -> str:
        """Persona, then the round line. Composed per pass, since the round moves.

        This is the whole --append-system-prompt value. The task prompt is
        not touched, which keeps the verbatim-operator-prompt guarantee.
        """
        base = self.persona_prompts[(pair.mode, pair.persona)]
        line = prompts_mod.round_stanza(pair.mode, self.round_for(pair))
        return f"{base}\n{line}"

    def pairs_to_run(
        self, group: Group, signal: Optional["signals_mod.Signal"] = None
    ) -> List[Pair]:
        """Which of this group's personas run this cycle.

        A group that owes something runs ONLY what it owes: those personas did
        not run last cycle, and the rest of the group did. A group that owes
        nothing runs in full. The narrowing lasts until the group is served
        without being cut again, which is one cycle in the ordinary case and
        longer while a pair keeps reporting a limit -- the siblings the OWED
        narrowing leaves out are owed by the cut that stopped it, so they come
        back on the visit after. The gate narrowing below is not like that: the
        pairs it leaves out are excused rather than deferred, and debt_for
        keeps them out of the debt for the same reason.

        A group that owes nothing is then filtered by the change gate, whose
        one definition of "unchanged" lives in gate_holds. A failed lookup
        against a non-empty updatedAt arrives here as a degraded Signal rather
        than as None, so it's gated like a real one and stops re-running once
        it's been served -- see signals.degraded. A mode flip needs no case of
        its own: it makes a different Pair, and that Pair has no session.
        """
        owed_here = [p for p in group.pairs if p in self.state.owed]
        if owed_here:
            return owed_here
        return [p for p in group.pairs if not self.gate_holds(p, signal)]

    def gate_holds(
        self, pair: Pair, signal: Optional["signals_mod.Signal"]
    ) -> bool:
        """True when the change gate has nothing for this pair to review.

        One definition of "unchanged", three callers: the group about to run,
        the group the cycle never reached, and the debt a cut leaves behind.
        They were written in three sittings and disagreed, which is how a cut
        came to owe personas the gate had just excused. Keep it that way -- a
        change to what "unchanged" means belongs here and nowhere else.

        `signal` is None when the gate is off, and when stage two failed
        against an empty updatedAt (nothing to key a degraded fingerprint on);
        both mean "run it". A pair with no session always runs, which is what
        makes first sight, a session dropped by record_failure, and
        MAX_PASSES_PER_SESSION rotation work without knowing about the gate.
        A plan pair at PLAN_MAX_ROUNDS holds whatever the signal says.
        """
        if self.capped(pair):
            return True
        if signal is None:
            return False
        return (
            self.state.sessions.get(pair) is not None
            and self.state.reviewed.get(pair) == signal
        )

    def capped(self, pair: Pair) -> bool:
        """True when a plan pair has completed PLAN_MAX_ROUNDS rounds.

        Checked ahead of the signal, so it holds with the gate off too, and
        it counts as the gate holding everywhere gate_holds is asked, so a
        capped pair is never owed. A moved head does not lift it: a revision
        per round is the very thing the cap exists to stop. The count is in
        memory, so a restart lifts it.
        """
        return (
            pair.mode == "plan"
            and self.plan_max_rounds > 0
            and self.state.rounds.get(pair, 0) >= self.plan_max_rounds
        )

    def finished(self, group: Group) -> bool:
        """True when every pair in the group is capped, for the cycle's log."""
        return bool(group.pairs) and all(self.capped(p) for p in group.pairs)

    def debt_for(
        self, group: Group, signal: Optional["signals_mod.Signal"] = None
    ) -> List[Pair]:
        """What a group owes the next cycle, before this cycle's results.

        Two callers, and neither can use `pairs_to_run`: that narrows a group
        to what it already owes, which is right for a group about to run and
        wrong for one a cut left behind, since the narrowing was justified by a
        cut two cycles back. So the whole persona set is owed, minus the pairs
        the change gate would have withheld had the cycle got that far --
        otherwise a limit owes the entire tail of an unchanged PR list and the
        next cycle spends the budget that just ran out re-reviewing it. A pair
        already owed keeps its debt whatever the gate says: it has no result to
        preserve.

        The cut group's own debt is this minus the pairs that did produce a
        result; the groups the cycle never reached take it whole.
        """
        return [
            p for p in group.pairs
            if p in self.state.owed or not self.gate_holds(p, signal)
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
        cut_group = self.state.cut_group
        if cut_group is None:
            return list(groups)
        keys = [(g.pr, g.mode) for g in groups]
        try:
            start = keys.index(cut_group) + 1
        except ValueError:
            # The group the cut stopped in is gone -- closed, or relabelled into
            # the other mode. Start at the head rather than skipping a cycle.
            return list(groups)
        return list(groups[start:]) + list(groups[:start])

    def settle_cycle(
        self,
        ordered: List[Group],
        cut_index: Optional[int],
        cut_owes: Set[Pair],
        signals_by_pr: Dict[int, "signals_mod.Signal"],
    ) -> List[Pair]:
        """Rebuild `owed` and `cut_group` from this cycle's own groups.

        Returns the pairs of the groups the cycle never reached, for the log.
        Both exits rebuild from this cycle's groups, which is what keeps a
        debt whose PR closed out of it: a dead pair matches no live group
        while the cycle runs, and does not survive the end of it.
        """
        if cut_index is None:
            self.state.owed = set()
            self.state.cut_group = None
            return []

        # The cut group's own debt, plus every pair of every group the cycle
        # never reached -- their whole persona set, narrowed or not, since none
        # of them ran. Both go through debt_for, which is the one place that
        # decides what "unchanged" means, so a persona the gate excused is in
        # neither; the pairs a limit or the pool prevented are, because they
        # were selected to run and the gate has no say over them.
        # state.owed still holds last cycle's debt at this point, which is what
        # keeps a group that was already owed and was never reached owed.
        new_owed = set(cut_owes)
        skipped: List[Pair] = []
        for group in ordered[cut_index + 1:]:
            for pair in self.debt_for(group, signals_by_pr.get(group.pr)):
                new_owed.add(pair)
                skipped.append(pair)
        self.state.owed = new_owed
        self.state.cut_group = (ordered[cut_index].pr, ordered[cut_index].mode)
        return skipped

    def record_success(
        self, pair: Pair, result, signal: Optional["signals_mod.Signal"] = None
    ) -> None:
        state = self.state
        # Only when one was supplied: None arrives from the gate being off and
        # from a failed lookup against an empty updatedAt (a failed lookup
        # against a non-empty one arrives as a degraded Signal, not None), and
        # recording it would claim knowledge we do not have.
        if signal is not None:
            state.reviewed[pair] = signal
        if result.session_id:
            state.sessions[pair] = result.session_id
        state.passes_done[pair] = state.passes_done.get(pair, 0) + 1
        state.rounds[pair] = state.rounds.get(pair, 0) + 1
        log(f"review complete (session {state.sessions.get(pair)}, "
            f"pass {state.passes_done[pair]}, round {state.rounds[pair]}).", pair=pair)
        if self.capped(pair):
            log(f"reached PLAN_MAX_ROUNDS={self.plan_max_rounds}; no further "
                "reviews of this plan by this persona until the container "
                "restarts.", pair=pair)
        if (
            self.max_passes_per_session > 0
            and state.passes_done[pair] >= self.max_passes_per_session
        ):
            log(f"reached MAX_PASSES_PER_SESSION={self.max_passes_per_session}; "
                "rotating its session next cycle.", pair=pair)
            state.sessions.pop(pair, None)
            state.passes_done[pair] = 0

    def record_limit(self, pair: Pair, result) -> None:
        # The session is kept. Dropping it would make the next attempt re-read
        # the whole PR and re-post findings already posted, spending more of the
        # resource that just ran out.
        if result.session_id:
            self.state.sessions[pair] = result.session_id
            log("WARN: hit a usage or rate limit; keeping its session and "
                "ending this cycle after the group finishes.", pair=pair)
        else:
            log("WARN: hit a usage or rate limit before it had a session; "
                "ending this cycle after the group finishes.", pair=pair)
        if result.limit_line:
            log(f"  limit reported by claude: {result.limit_line}", pair=pair)

    def record_failure(self, pair: Pair) -> None:
        log("WARN: review failed; starting a fresh session for it next cycle.", pair=pair)
        self.state.sessions.pop(pair, None)
        self.state.passes_done[pair] = 0
