"""What changed on a pull request since we last reviewed it.

A cycle used to re-review every candidate whether or not anything had happened
to it, which costs (PRs x personas) provider sessions per cycle to conclude
that nothing had. This module holds the decision: a per-PR fingerprint, the
test that tells claudebox's own comments from everybody else's, and the settle
arithmetic that batches a burst of pushes into one review.

Everything here is pure. The GitHub calls live in gh.py, and the state that
outlives a cycle lives on Supervisor.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Mapping, Optional, Tuple

# Comments claudebox posts carry this, because personas/*/_shared.md tells every
# persona to sign its findings with it. Author login is deliberately NOT
# consulted: claudebox is commonly run under the operator's own PAT, where
# matching on login would classify the operator's own comments as ours and the
# comment trigger would never fire for the person most likely to want it.
MARKER = "-claudebox"

_OFF = ("0", "false", "no", "off")


@dataclass(frozen=True)
class Signal:
    """One PR's fingerprint. Two of these differ when a re-review is owed.

    `updatedAt` is deliberately absent. It moves for edits this design decided
    to ignore (a typo fix in the PR body, a title change), and including it
    would turn each of those into a full persona fan-out. Its job is to gate
    the second GitHub lookup, which is Tracker's business, not this record's.
    """

    head_oid: str
    mode: str
    # Timestamp of the newest comment that does not carry MARKER, or "".
    # ISO-8601 Z strings compare correctly as strings, so nothing here parses.
    newest_human: str


def is_own(body: Optional[str]) -> bool:
    """True when this comment body is one claudebox posted."""
    return MARKER in (body or "").lower()


def newest_unsigned(entries: Iterable[Tuple[Optional[str], Optional[str]]]) -> str:
    """The latest timestamp among entries whose body lacks the marker.

    Entries are (timestamp, body). One missing a timestamp is skipped rather
    than raising: a gh payload we cannot read must not take the cycle with it.
    """
    best = ""
    for stamp, body in entries:
        if not isinstance(stamp, str) or not stamp:
            continue
        if is_own(body):
            continue
        if stamp > best:
            best = stamp
    return best


def _epoch(stamp: str) -> float:
    """RFC 3339 UTC as GitHub writes it, e.g. 2026-08-31T12:00:00Z."""
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def age_seconds(updated_at: str, now: float) -> Optional[float]:
    """Seconds since the timestamp, or None when it cannot be read."""
    try:
        return now - _epoch(updated_at)
    except (AttributeError, ValueError):
        return None


def is_settling(updated_at: str, settle_seconds: int, now: float) -> bool:
    """True when this change is too fresh to review yet.

    A negative age means the container clock is behind GitHub's. It runs rather
    than settling, so skew costs the batching and never the review. An
    unreadable timestamp runs for the same reason.
    """
    if settle_seconds <= 0:
        return False
    age = age_seconds(updated_at, now)
    if age is None:
        return False
    return 0 <= age < settle_seconds


def enabled(env: Mapping[str, str]) -> bool:
    """Whether the change gate is on. Default on; REVIEW_ON_CHANGE=0 turns it off."""
    return (env.get("REVIEW_ON_CHANGE", "") or "").strip().lower() not in _OFF


# Prefix that marks a degraded newest_human as fabricated rather than observed.
# A real value here is an RFC-3339 comment timestamp, which never starts with
# a letter, so no real Signal can collide with one this module synthesizes --
# recovery from an outage is guaranteed to look like a change.
_DEGRADED_PREFIX = "degraded:"


def is_degraded(sig: Signal) -> bool:
    """True when this fingerprint's comment field was fabricated, not observed."""
    return sig.newest_human.startswith(_DEGRADED_PREFIX)


def degraded(snapshot) -> Optional[Signal]:
    """The fingerprint to use when stage two fails but updatedAt is known.

    head_oid and mode come from the snapshot, exactly as a real Signal's do --
    a push or a mode flip must still be noticed while the lookup that would
    have confirmed it is down. newest_human is updatedAt itself, marked so it
    can never be mistaken for a comment timestamp. Keying on updatedAt is what
    bounds a durable failure to one review per change: the PR reviews once and
    then goes quiet until updatedAt moves again, whether that's a human
    comment, a push, or the reviewer's own comment on the pass this produced.

    An empty updatedAt returns None, same as a real lookup would have nothing
    to report: there is nothing to key on, so the caller must fail open on
    every poll rather than caching a review it can never re-trigger.
    """
    if not snapshot.updated_at:
        return None
    return Signal(
        head_oid=snapshot.head_oid,
        mode=snapshot.mode,
        newest_human=_DEGRADED_PREFIX + snapshot.updated_at,
    )


def change_reason(old: Optional[Signal], new: Signal) -> str:
    """Why this PR is being reviewed, for the log. Empty when nothing changed."""
    if old is None:
        return "first review"
    reasons = []
    if old.head_oid != new.head_oid:
        reasons.append(f"new head {new.head_oid[:7]}")
    if old.newest_human != new.newest_human:
        old_deg, new_deg = is_degraded(old), is_degraded(new)
        if old_deg and new_deg:
            # Both sides are fabricated: the lookup is still failing and only
            # updatedAt moved underneath it. No comment was seen either time.
            reasons.append("updatedAt changed during a failed lookup")
        elif new_deg:
            reasons.append("stage-two lookup failed")
        elif old_deg:
            reasons.append("stage-two lookup recovered")
        else:
            reasons.append("new comment activity")
    if old.mode != new.mode:
        reasons.append(f"mode changed to {new.mode}")
    return ", ".join(reasons)


class Tracker:
    """Decides when a second GitHub lookup is worth making.

    `updatedAt` moves on a push, a comment, a review and a label change, so a
    PR whose value has not moved cannot have changed in any way this design
    cares about. That is what keeps a cycle in which nothing happened at the
    cost it has today: one request.

    In memory, like the session map. It must NOT be persisted: `sessions` is in
    memory too, so a fingerprint that survived a restart would leave a fresh
    session that has never read the PR believing it had already reviewed it.
    """

    def __init__(self):
        # PR number -> the updatedAt its last lookup ran against.
        self.polled: Dict[int, str] = {}
        # PR number -> the fingerprint that lookup produced, or None when it
        # failed. Cached either way, so a persistent gh outage fails open once
        # per updatedAt change instead of once per cycle.
        self._cache: Dict[int, Optional[Signal]] = {}

    def signal_for(self, snapshot, fetch) -> Optional[Signal]:
        number = snapshot.number
        stamp = snapshot.updated_at
        # An empty stamp is not a value to cache against: gh did not tell us
        # when the PR last moved, so every poll has to look for itself.
        if stamp and self.polled.get(number) == stamp:
            cached = self._cache.get(number)
            if cached is None:
                return None
            # The mode can change without updatedAt reaching us as a new value
            # in the same cycle; keep the cached comment timestamp and take the
            # rest from the snapshot we were just handed.
            return Signal(
                head_oid=snapshot.head_oid,
                mode=snapshot.mode,
                newest_human=cached.newest_human,
            )
        result = fetch(snapshot)
        if stamp:
            self.polled[number] = stamp
        self._cache[number] = result
        return result
