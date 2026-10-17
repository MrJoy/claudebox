"""The decision core's state, behind one interface.

Everything the gate, the round ladder and the cut accounting remember lives
here. Local mode keeps all of it in memory, which is MemoryStateStore and is
the only implementation today; the hosted control plane supplies a SQL-backed
one through the same interface.

The per-pair records are MutableMappings rather than methods so a store can
present a table the way MemoryStateStore presents a dict, and the decision
core reads and writes either without knowing which it holds.
"""

from abc import ABC, abstractmethod
from typing import Dict, MutableMapping, Optional, Set, Tuple

import signals as signals_mod
from common import Pair


class StateStore(ABC):
    """What the decision core remembers between passes and between cycles."""

    @property
    @abstractmethod
    def sessions(self) -> MutableMapping[Pair, str]:
        """The Claude session each pair resumes."""

    @property
    @abstractmethod
    def passes_done(self) -> MutableMapping[Pair, int]:
        """Completed passes in the pair's current session."""

    @property
    @abstractmethod
    def rounds(self) -> MutableMapping[Pair, int]:
        """Completed passes per pair, whatever session they ran in.

        Drives the round ladder in the system prompt. Not reset by rotation or
        by a failed pass: those are about the session, the round is about the
        PR's history with this persona.
        """

    @property
    @abstractmethod
    def reviewed(self) -> MutableMapping[Pair, "signals_mod.Signal"]:
        """The fingerprint each pair last successfully reviewed at.

        A pair whose PR still fingerprints the same has nothing new to read,
        so it does not run.
        """

    @property
    @abstractmethod
    def owed(self) -> Set[Pair]:
        """Pairs the last cycle owed but did not run.

        The personas a limit cut, plus everything in the groups it never
        reached. Normally empty. Without it, a limit that allows only a few
        passes per backoff window would review the leading pairs forever and
        the trailing ones never.
        """

    @owed.setter
    @abstractmethod
    def owed(self, value: Set[Pair]) -> None: ...

    @property
    @abstractmethod
    def cut_group(self) -> Optional[Tuple[int, str]]:
        """The (pr, mode) of the group the last cut stopped in.

        So the next cycle can start at the one after it. Serving the debt
        first instead would let a PR whose persona reports a limit on every
        attempt re-cut the cycle at the head of the list forever, and nothing
        else would ever be reviewed again.
        """

    @cut_group.setter
    @abstractmethod
    def cut_group(self, value: Optional[Tuple[int, str]]) -> None: ...


class MemoryStateStore(StateStore):
    """Today's dicts. Lost on restart, deliberately.

    A restart re-reviews each pair once and may re-comment once. Persisting
    any one of these without the session map would be worse: a fingerprint
    or a round count that outlived a restart would tell a fresh session that
    has never read the PR that it already had.
    """

    def __init__(self) -> None:
        self._sessions: Dict[Pair, str] = {}
        self._passes_done: Dict[Pair, int] = {}
        self._rounds: Dict[Pair, int] = {}
        self._reviewed: Dict[Pair, signals_mod.Signal] = {}
        self._owed: Set[Pair] = set()
        self._cut_group: Optional[Tuple[int, str]] = None

    @property
    def sessions(self) -> Dict[Pair, str]:
        return self._sessions

    @property
    def passes_done(self) -> Dict[Pair, int]:
        return self._passes_done

    @property
    def rounds(self) -> Dict[Pair, int]:
        return self._rounds

    @property
    def reviewed(self) -> Dict[Pair, "signals_mod.Signal"]:
        return self._reviewed

    @property
    def owed(self) -> Set[Pair]:
        return self._owed

    @owed.setter
    def owed(self, value: Set[Pair]) -> None:
        self._owed = value

    @property
    def cut_group(self) -> Optional[Tuple[int, str]]:
        return self._cut_group

    @cut_group.setter
    def cut_group(self, value: Optional[Tuple[int, str]]) -> None:
        self._cut_group = value
